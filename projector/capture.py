"""Capture engine: everything between "iPhone plugged in" and "samples arrive", plus a clean exit.

The order here was found the hard way (see README, troubleshooting):
1. become a trusted host over usbmux in the normal configuration,
2. ask for the capture configuration (vendor request 0x52) from there,
3. switch to it, claim the capture interface and read at once,
4. repeat the trust handshake on the capture configuration in the background,
5. on exit: HPA0/HPD0 -> wait for RELS (only if streaming started), release, USB reset.

Skipping step 5 after a session started can leave the phone's capture service stuck
until the phone is restarted, so every exit path goes through `_teardown`.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import lockdown as ld
from . import protocol as p
from . import usb_transport as ut
from . import usbmux

log = logging.getLogger("projector.capture")

MUX_LEVELS = ["off", "version", "lockdown", "session"]
PING_TIMEOUT = 5.0


# Status values reported through on_status
WAITING = "waiting"          # no iPhone visible
CONNECTING = "connecting"    # trust + switch in progress
STREAMING = "streaming"      # samples arriving
STOPPING = "stopping"
STUCK = "stuck"              # phone never answered: needs a restart
ENDED = "ended"              # the phone stopped mirroring (Control Center); wait for the user
DISCONNECTED = "disconnected"
ERROR = "error"
STOPPED = "stopped"


@dataclass
class SessionResult:
    status: str = STOPPED
    error: str = ""
    processor: Optional[p.MessageProcessor] = None
    elapsed: float = 0.0
    counts: dict = field(default_factory=dict)


def establish_trust(mux: usbmux.MuxDevice, serial: str, level: int):
    """Run usbmux and the lockdown steps up to `level`. Returns the lockdown connection (or None)."""
    mux.start()
    conn = None
    if level >= 2:
        conn = mux.connect(usbmux.LOCKDOWN_PORT)
        client = ld.LockdownClient(conn)
        log.info("lockdown QueryType: %s", client.query_type())
        if level >= 3:
            pair = ld.load_pair_record(ld.udid_from_usb_serial(serial))
            client.start_session(pair)
            log.info(
                "受信任連線建立：%s，iOS %s",
                client.get_value("DeviceName"), client.get_value("ProductVersion"),
            )
    log.info("配對層級 %s 完成", MUX_LEVELS[level])
    return conn


class CaptureEngine:
    def __init__(
        self,
        backend,
        *,
        on_video_format: Optional[Callable] = None,
        on_video: Optional[Callable] = None,
        on_audio_format: Optional[Callable] = None,
        on_audio: Optional[Callable] = None,
        on_status: Optional[Callable[[str, str], None]] = None,
        serial: str = "",
        trust_first: bool = True,
        mux_level: int = 3,
        clear_halt: bool = False,
    ):
        self.backend = backend
        self.callbacks = dict(
            on_video_format=on_video_format, on_video=on_video,
            on_audio_format=on_audio_format, on_audio=on_audio,
        )
        self.on_status = on_status
        self.serial = serial
        self.trust_first = trust_first
        self.mux_level = mux_level
        self.clear_halt = clear_halt
        self.status = WAITING
        self.processor: Optional[p.MessageProcessor] = None
        self._framer: Optional[p.Framer] = None
        # USB diagnostics: a read that fills the whole buffer means data was waiting (we're behind)
        self.usb_reads = 0
        self.usb_full_reads = 0
        self.usb_bytes = 0
        self.resume = threading.Event()  # set by the app to start again after ENDED

    def _set_status(self, status: str, detail: str = "") -> None:
        self.status = status
        log.info("狀態: %s %s", status, detail)
        if self.on_status:
            try:
                self.on_status(status, detail)
            except Exception:  # noqa: BLE001
                log.exception("on_status callback failed")

    # -- long-running loop for the app ----------------------------------------

    def run_forever(self, stop: threading.Event, poll: float = 1.5) -> None:
        """Wait for the phone, stream until it goes away, repeat. Returns when `stop` is set."""
        stuck_retried = False
        while not stop.is_set():
            dev = ut.find_by_serial(self.backend, self.serial)
            if dev is None:
                if self.status != WAITING:
                    self._set_status(WAITING)
                stuck_retried = False
                stop.wait(poll)
                continue
            result = self.run_once(stop)
            if stop.is_set():
                break
            if result.status == STUCK and not stuck_retried:
                # The teardown just switched the phone back to normal and reset it; that is
                # sometimes enough for its capture service to recover. Try once more.
                stuck_retried = True
                log.warning("iPhone 沒有回應，重設後再試一次")
                stop.wait(3.0)
                continue
            if result.status in (ENDED, STUCK, ERROR):
                # ENDED: the user stopped mirroring on the phone, so don't restart it by ourselves.
                # STUCK/ERROR: don't hammer the phone.
                # Either way wait for an unplug, or for the user to ask again (R key).
                self.resume.clear()
                self._wait_until_gone(stop, poll, self.resume)
                stuck_retried = False
            else:
                stuck_retried = False
                stop.wait(poll)
        self._set_status(STOPPED)

    def _wait_until_gone(self, stop: threading.Event, poll: float,
                         resume: Optional[threading.Event] = None) -> None:
        """Wait until the phone is unplugged (or `resume` is set)."""
        serial = self.serial
        while not stop.is_set():
            if resume is not None and resume.is_set():
                return
            dev = ut.find_by_serial(self.backend, serial)
            if dev is None:
                return
            if resume is not None:
                resume.wait(poll)
            else:
                stop.wait(poll)

    # -- one capture session ----------------------------------------------------

    def run_once(self, stop: threading.Event, deadline: Optional[float] = None) -> SessionResult:
        """One session: set up, stream until `stop`/`deadline`/unplug, then tear down cleanly."""
        result = SessionResult()
        started = time.monotonic()
        dev = ut.find_by_serial(self.backend, self.serial)
        if dev is None:
            result.status = WAITING
            result.error = "找不到 iPhone"
            return result

        self._set_status(CONNECTING)
        self._framer = None
        session = ut.CaptureSession(self.backend, dev, clear_halt=self.clear_halt)
        mp = p.MessageProcessor(session.write, **self.callbacks)
        self.processor = mp
        result.processor = mp
        state = {"opened": False, "mux": None, "trust_thread": None}

        try:
            self._setup(session, state)
            result.status = self._read_loop(session, mp, stop, deadline)
        except (ut.UsbSetupError, usbmux.MuxError, ld.LockdownError) as e:
            log.error("%s", e)
            result.status, result.error = ERROR, str(e)
        except Exception as e:  # noqa: BLE001
            log.exception("擷取過程發生錯誤")
            result.status, result.error = ERROR, str(e)
        finally:
            if result.status == STREAMING:
                result.status = STOPPED
            self._set_status(STOPPING)
            self._teardown(session, mp, state, stop_streams=result.status != DISCONNECTED)

        result.elapsed = time.monotonic() - started
        result.counts = dict(mp.counts)
        if result.status == STUCK:
            self._set_status(STUCK, "iPhone 沒有回應，請重開 iPhone")
        elif result.status == ERROR:
            self._set_status(ERROR, result.error)
        elif result.status == DISCONNECTED:
            self._set_status(DISCONNECTED)
        elif result.status == ENDED:
            self._set_status(ENDED)
        return result

    def _setup(self, session: ut.CaptureSession, state: dict) -> None:
        level = self.mux_level
        # A leftover capture config from an earlier run leaves the phone unresponsive.
        if ut.find_interface(session.dev, ut.QT_SUBCLASS):
            log.info("iPhone 還停在擷取模式，先重設")
            session.reset_device()

        if self.trust_first and level >= 1:
            # What qvh and the Windows usbmuxd do: become a trusted host in the normal
            # usbmux configuration, then ask for the capture configuration from there.
            cfg = session.mux_config_value()
            log.info("先在一般設定 %s 建立受信任連線，再切換", cfg)
            session.set_config(cfg)
            pre_info = session.claim_mux(cfg)
            pre_mux = usbmux.MuxDevice(session.mux_write, session.mux_read_into, pre_info.max_packet)
            try:
                pre_conn = establish_trust(pre_mux, session.serial, level)
                if pre_conn is not None:
                    pre_conn.close()
            finally:
                pre_mux.stop()
                session.release_mux()

        session.enable_capture_config()
        log.info("切換後:\n%s", ut.describe(session.dev))
        session.select_capture_config()

        # Claim the capture interface and start reading right away.
        session.open()
        state["opened"] = True

        # Trust again on the capture configuration, in the background.
        if level >= 1:
            info = session.claim_mux()
            mux = usbmux.MuxDevice(session.mux_write, session.mux_read_into, info.max_packet)
            state["mux"] = mux

            def trust_worker():
                try:
                    state["lockdown"] = establish_trust(mux, session.serial, level)
                except Exception:  # noqa: BLE001
                    log.exception("建立受信任連線失敗（擷取繼續進行）")

            t = threading.Thread(target=trust_worker, name="trust", daemon=True)
            state["trust_thread"] = t
            t.start()

    def _read_loop(self, session, mp: p.MessageProcessor, stop: threading.Event,
                   deadline: Optional[float]) -> str:
        framer = self._framer = p.Framer()
        buf = ut.create_buffer(512 * 1024)
        view = memoryview(buf)
        opened_at = time.monotonic()
        streaming = False
        bad_frames = 0
        log.info("等待 iPhone 開始傳送...")

        while not stop.is_set():
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                return STOPPED
            if not mp.ping_received and now - opened_at > PING_TIMEOUT:
                log.error("切換後 %.0f 秒沒有收到 PING：iPhone 的擷取服務可能卡住了", PING_TIMEOUT)
                return STUCK
            if mp.device_ended():
                log.warning("iPhone 已停止螢幕鏡像，正常結束這次擷取")
                return ENDED

            try:
                n = session.read_into(buf, 500)
                self.usb_reads += 1
                self.usb_bytes += n
                if n == len(buf):
                    self.usb_full_reads += 1
            except Exception as e:  # noqa: BLE001 - not a timeout: the phone went away
                log.warning("USB 讀取中斷（iPhone 可能被拔掉）: %s", e)
                return DISCONNECTED
            if n == 0:
                continue

            chunk = bytes(view[:n])
            try:
                frames = framer.feed(chunk)
            except p.ProtocolError:
                log.exception("資料流錯位，開頭 64 bytes: %s", chunk[:64].hex())
                return ERROR
            for frame in frames:
                try:
                    mp.handle(frame)
                except Exception:  # noqa: BLE001
                    bad_frames += 1
                    log.exception("處理封包失敗，開頭 64 bytes: %s", frame[:64].hex())
                    if bad_frames >= 20:
                        return ERROR
            if not streaming and mp.counts.get("asyn-feed"):
                streaming = True
                self._set_status(STREAMING)
        return STOPPED

    def _teardown(self, session, mp: p.MessageProcessor, state: dict, stop_streams: bool) -> None:
        started_stream = "sync-cwpa" in mp.counts
        if state["opened"] and started_stream and stop_streams:
            log.info("請 iPhone 停止傳送")
            try:
                for pkt in mp.stop_packets():
                    session.write(pkt)
                self._drain_until_released(session, mp, self._framer or p.Framer(), 3.0)
                session.write(p.asyn_hpd0())
            except Exception as e:  # noqa: BLE001
                log.warning("停止指令沒有送達: %s", e)
        t = state.get("trust_thread")
        if t is not None:
            t.join(3.0)
        mux = state.get("mux")
        if mux is not None:
            try:
                mux.stop()
            except Exception:  # noqa: BLE001
                log.exception("關閉 usbmux 時發生錯誤")
        try:
            session.close()
        except Exception:  # noqa: BLE001
            log.debug("close failed", exc_info=True)
        # The disable request alone doesn't bring the phone back to normal; a reset does.
        try:
            session.reset_device()
        except Exception:  # noqa: BLE001
            log.debug("reset failed", exc_info=True)

    @staticmethod
    def _drain_until_released(session, mp: p.MessageProcessor, framer: p.Framer, timeout: float) -> None:
        """Keep reading (with the main loop's framer) so the phone's RELS replies are processed."""
        buf = ut.create_buffer(512 * 1024)
        view = memoryview(buf)
        end = time.monotonic() + timeout
        while mp.releases < 2 and time.monotonic() < end:
            try:
                n = session.read_into(buf, 200)
            except Exception:  # noqa: BLE001
                return
            if not n:
                continue
            try:
                for frame in framer.feed(bytes(view[:n])):
                    mp.handle(frame)
            except Exception:  # noqa: BLE001
                log.debug("ignoring bad data while stopping", exc_info=True)
                framer = p.Framer()
        log.info("iPhone 已釋放擷取（RELS %d 次）", mp.releases)
