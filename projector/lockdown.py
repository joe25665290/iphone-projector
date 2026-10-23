"""lockdownd client: proves to the iPhone that this PC is a host it already trusts.

Uses the pairing record Apple's software left in C:\\ProgramData\\Apple\\Lockdown.
The record holds a private key, so it is only read into memory; the TLS layer needs it as a
file for a moment, which goes to the system temp folder and is deleted right after loading.
"""

from __future__ import annotations

import logging
import os
import plistlib
import ssl
import struct
import tempfile
from pathlib import Path

from .usbmux import MuxConnection

log = logging.getLogger("projector.lockdown")

PAIR_DIR = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Apple" / "Lockdown"
LABEL = "projector"


class LockdownError(Exception):
    pass


def udid_from_usb_serial(serial: str) -> str:
    serial = serial.strip("\x00")
    if len(serial) == 24:  # newer devices insert a dash after 8 chars
        return f"{serial[:8]}-{serial[8:]}"
    return serial


def load_pair_record(udid: str) -> dict:
    for name in (udid, udid.replace("-", "")):
        path = PAIR_DIR / f"{name}.plist"
        if path.exists():
            with open(path, "rb") as f:
                record = plistlib.load(f)
            log.info("使用配對紀錄 %s", path.name)
            return record
    raise LockdownError(
        f"找不到這支 iPhone 的配對紀錄（{PAIR_DIR}\\{udid}.plist）。需要先重新配對。"
    )


class _TlsStream:
    """TLS on top of a MuxConnection, driven through memory BIOs."""

    def __init__(self, conn: MuxConnection, pair: dict):
        self.conn = conn
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers("ALL:@SECLEVEL=0")
        fd, path = tempfile.mkstemp(prefix="projector-", suffix=".pem")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(pair["HostCertificate"])
                if not pair["HostCertificate"].endswith(b"\n"):
                    f.write(b"\n")
                f.write(pair["HostPrivateKey"])
            ctx.load_cert_chain(path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        self.inc = ssl.MemoryBIO()
        self.out = ssl.MemoryBIO()
        self.obj = ctx.wrap_bio(self.inc, self.out, server_side=False)
        self._handshake()

    def _flush(self) -> None:
        data = self.out.read()
        if data:
            self.conn.send(data)

    def _handshake(self) -> None:
        while True:
            try:
                self.obj.do_handshake()
                self._flush()
                log.info("TLS 建立完成（%s）", self.obj.version())
                return
            except ssl.SSLWantReadError:
                self._flush()
                self.inc.write(self.conn.recv(65536))

    def send(self, data: bytes) -> None:
        self.obj.write(data)
        self._flush()

    def recv_exact(self, n: int) -> bytes:
        parts = bytearray()
        while len(parts) < n:
            try:
                parts += self.obj.read(n - len(parts))
            except ssl.SSLWantReadError:
                self._flush()
                self.inc.write(self.conn.recv(65536))
        return bytes(parts)


class LockdownClient:
    def __init__(self, conn: MuxConnection):
        self.conn = conn
        self.stream = conn  # replaced by the TLS stream after StartSession
        self.session_id = None

    def _send(self, msg: dict) -> None:
        body = plistlib.dumps(msg)
        self.stream.send(struct.pack(">I", len(body)) + body)

    def _recv(self) -> dict:
        n = struct.unpack(">I", self.stream.recv_exact(4))[0]
        return plistlib.loads(self.stream.recv_exact(n))

    def request(self, name: str, **fields) -> dict:
        self._send({"Label": LABEL, "Request": name, **fields})
        reply = self._recv()
        if "Error" in reply:
            raise LockdownError(f"{name} 失敗: {reply['Error']}")
        return reply

    def query_type(self) -> str:
        return self.request("QueryType").get("Type", "")

    def start_session(self, pair: dict) -> None:
        fields = {"HostID": pair["HostID"]}
        if pair.get("SystemBUID"):
            fields["SystemBUID"] = pair["SystemBUID"]
        reply = self.request("StartSession", **fields)
        self.session_id = reply.get("SessionID")
        log.info("StartSession 成功（EnableSessionSSL=%s）", reply.get("EnableSessionSSL"))
        if reply.get("EnableSessionSSL"):
            self.stream = _TlsStream(self.conn, pair)

    def get_value(self, key: str):
        return self.request("GetValue", Key=key).get("Value")
