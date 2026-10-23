"""usbmux over USB: the channel iTunes/usbmuxd use to talk to services on the iPhone.

Ported from libimobiledevice's usbmuxd (src/device.c). The screen-capture stream only starts
once the phone sees a trusted host on this channel, so we run a minimal client here.

All fields in this protocol are big endian (unlike the capture protocol).
The phone's services are reached through a tiny TCP-like layer carried inside mux packets.
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("projector.usbmux")

PROTO_VERSION = 0
PROTO_CONTROL = 1
PROTO_SETUP = 2
PROTO_TCP = 6
MAGIC = 0xFEEDFACE

TH_FIN, TH_SYN, TH_RST, TH_PUSH, TH_ACK = 0x01, 0x02, 0x04, 0x08, 0x10

USB_MTU = 3 * 16384
TX_WINDOW = 131072
MAX_PAYLOAD = USB_MTU - 16 - 20

LOCKDOWN_PORT = 62078


class MuxError(Exception):
    pass


class MuxConnection:
    """One TCP-like stream to a port on the phone. send()/recv() block like a socket."""

    def __init__(self, mux: "MuxDevice", sport: int, dport: int):
        self.mux = mux
        self.sport = sport
        self.dport = dport
        self.tx_seq = 0
        self.tx_ack = 0
        self.rx_ack = 0
        self.rx_win = 0
        self.state = "connecting"
        self.reset_reason = ""
        self._buf = bytearray()
        self._cond = threading.Condition()

    # called by the mux reader thread
    def _input(self, seq: int, ack: int, flags: int, win: int, payload: bytes) -> None:
        with self._cond:
            self.rx_ack = ack
            self.rx_win = win << 8
            if flags & TH_RST:
                self.reset_reason = payload[1:].decode("utf-8", "replace").strip() if payload else ""
                self.state = "refused" if self.state == "connecting" else "reset"
                self._cond.notify_all()
                return
            if self.state == "connecting":
                if flags == (TH_SYN | TH_ACK):
                    self.tx_seq += 1
                    self.tx_ack += 1
                    self.mux.send_tcp(self, TH_ACK)
                    self.state = "connected"
                else:
                    self.state = "refused"
                self._cond.notify_all()
            elif self.state == "connected":
                if flags != TH_ACK:
                    self.state = "reset"
                    self._cond.notify_all()
                    return
                if payload:
                    self._buf += payload
                    self.tx_ack += len(payload)
                    self.mux.send_tcp(self, TH_ACK)  # the phone likes prompt ACKs
                    self._cond.notify_all()

    def wait_connected(self, timeout: float) -> None:
        with self._cond:
            self._cond.wait_for(lambda: self.state != "connecting", timeout)
            if self.state != "connected":
                reason = f"（{self.reset_reason}）" if self.reset_reason else ""
                raise MuxError(f"iPhone 拒絕連到 port {self.dport}: {self.state}{reason}")

    def send(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            if self.state != "connected":
                raise MuxError(f"連線已中斷（{self.state}）")
            chunk = bytes(view[:MAX_PAYLOAD])
            with self._cond:
                self.mux.send_tcp(self, TH_ACK, chunk)
                self.tx_seq = (self.tx_seq + len(chunk)) & 0xFFFFFFFF
            view = view[len(chunk):]

    def recv(self, n: int, timeout: float = 10.0) -> bytes:
        """Returns up to n bytes (at least 1)."""
        with self._cond:
            ok = self._cond.wait_for(lambda: self._buf or self.state != "connected", timeout)
            if not self._buf:
                if not ok:
                    raise MuxError("等待 iPhone 回應逾時")
                raise MuxError(f"連線已中斷（{self.state}）")
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out

    def recv_exact(self, n: int, timeout: float = 10.0) -> bytes:
        parts = bytearray()
        while len(parts) < n:
            parts += self.recv(n - len(parts), timeout)
        return bytes(parts)

    def close(self) -> None:
        if self.state == "connected":
            try:
                self.mux.send_tcp(self, TH_RST)
            except Exception:  # noqa: BLE001
                pass
        self.state = "closed"
        self.mux._forget(self)


class MuxDevice:
    """Runs the usbmux protocol on an already-claimed mux interface."""

    def __init__(self, write: Callable[[bytes], None], read_into: Callable, max_packet: int = 512):
        self._write_raw = write
        self._read_into = read_into  # (buffer, timeout_ms) -> bytes read, 0 on timeout
        self.max_packet = max_packet
        self.version = 0
        self.tx_seq = 0
        self.rx_seq = 0xFFFF
        self._send_lock = threading.Lock()
        self._conns: dict[int, MuxConnection] = {}
        self._next_sport = 1
        self._version_event = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pending = bytearray()
        self.error: Optional[BaseException] = None

    # -- low level -----------------------------------------------------------

    def _send(self, proto: int, header: bytes, data: bytes = b"") -> None:
        with self._send_lock:
            if self.version < 2:
                total = 8 + len(header) + len(data)
                mh = struct.pack(">II", proto, total)
            else:
                total = 16 + len(header) + len(data)
                if proto == PROTO_SETUP:
                    self.tx_seq = 0
                    self.rx_seq = 0xFFFF
                mh = struct.pack(">IIIHH", proto, total, MAGIC, self.tx_seq, self.rx_seq)
                self.tx_seq = (self.tx_seq + 1) & 0xFFFF
            packet = mh + header + data
            self._write_raw(packet)
            if len(packet) % self.max_packet == 0:
                try:
                    self._write_raw(b"")  # zero-length packet ends the transfer
                except Exception as e:  # noqa: BLE001
                    log.debug("ZLP write failed: %s", e)

    def send_tcp(self, conn: MuxConnection, flags: int, data: bytes = b"") -> None:
        th = struct.pack(
            ">HHIIBBHHH",
            conn.sport, conn.dport, conn.tx_seq, conn.tx_ack,
            5 << 4, flags, TX_WINDOW >> 8, 0, 0,
        )
        self._send(PROTO_TCP, th, data)

    # -- lifecycle -----------------------------------------------------------

    def start(self, timeout: float = 5.0) -> None:
        self._thread = threading.Thread(target=self._reader, name="usbmux", daemon=True)
        self._thread.start()
        self._send(PROTO_VERSION, struct.pack(">III", 2, 0, 0))
        if not self._version_event.wait(timeout):
            raise MuxError("iPhone 沒有回應 usbmux 版本請求")
        if self.error:
            raise MuxError(f"usbmux 讀取失敗: {self.error}")
        if self.version >= 2:
            self._send(PROTO_SETUP, b"", b"\x07")
        log.info("usbmux 已連線（協定版本 %d）", self.version)

    def connect(self, port: int, timeout: float = 5.0) -> MuxConnection:
        sport = self._next_sport
        self._next_sport += 1
        conn = MuxConnection(self, sport, port)
        conn.tx_seq = 0
        self._conns[sport] = conn
        self.send_tcp(conn, TH_SYN)
        conn.wait_connected(timeout)
        log.info("已連到 iPhone port %d", port)
        return conn

    def _forget(self, conn: MuxConnection) -> None:
        self._conns.pop(conn.sport, None)

    def stop(self) -> None:
        for conn in list(self._conns.values()):
            conn.close()
        self._stop.set()
        if self._thread:
            self._thread.join(2.0)

    # -- reader thread -------------------------------------------------------

    def _reader(self) -> None:
        from .usb_transport import create_buffer

        buf = create_buffer(65536)
        view = memoryview(buf)
        while not self._stop.is_set():
            try:
                n = self._read_into(buf, 200)
            except Exception as e:  # noqa: BLE001
                if not self._stop.is_set():
                    self.error = e
                    log.error("usbmux 讀取失敗: %s", e)
                    self._version_event.set()
                    for conn in list(self._conns.values()):
                        with conn._cond:
                            conn.state = "reset"
                            conn._cond.notify_all()
                return
            if n:
                self._pending += bytes(view[:n])
                self._drain()

    def _drain(self) -> None:
        while len(self._pending) >= 8:
            proto, length = struct.unpack_from(">II", self._pending, 0)
            if length < 8 or length > 1 << 20:
                log.error("usbmux 封包長度異常 %d，丟棄緩衝區", length)
                self._pending.clear()
                return
            if len(self._pending) < length:
                return
            packet = bytes(self._pending[:length])
            del self._pending[:length]
            try:
                self._handle(proto, packet)
            except Exception:  # noqa: BLE001
                log.exception("處理 usbmux 封包失敗: %s", packet[:48].hex())

    def _handle(self, proto: int, packet: bytes) -> None:
        hdr = 16 if self.version >= 2 else 8
        if self.version >= 2 and len(packet) >= 16:
            self.rx_seq = struct.unpack_from(">H", packet, 14)[0]
        if proto == PROTO_VERSION:
            major, minor, _ = struct.unpack_from(">III", packet, hdr)
            if major not in (1, 2):
                log.error("不支援的 usbmux 版本 %d.%d", major, minor)
            self.version = major
            self._version_event.set()
        elif proto == PROTO_TCP:
            sport, dport, seq, ack, _off, flags, win = struct.unpack_from(">HHIIBBH", packet, hdr)
            payload = packet[hdr + 20:]
            conn = self._conns.get(dport)
            if conn is None:
                log.debug("TCP 封包沒有對應連線 %d->%d flags=%x", sport, dport, flags)
                return
            conn._input(seq, ack, flags, win, payload)
        elif proto == PROTO_CONTROL:
            log.info("usbmux 控制訊息: %s", packet[hdr:].hex())
        else:
            log.debug("未知 usbmux 協定 %d", proto)
