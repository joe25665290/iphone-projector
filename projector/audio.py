"""Play the phone's audio (raw 48 kHz 16-bit stereo LPCM) through this process.

Discord's window share and OBS's Application Audio Capture take audio from the process that
owns the window, so playback must happen here, not in a helper process.

A small jitter buffer sits between the phone (packets of ~20 ms, unevenly spaced) and the
sound card callback:
- playback starts once START_MS are buffered,
- above MAX_MS the oldest audio is dropped back to TARGET_MS (the two clocks drift apart),
- when the buffer runs dry we play silence and wait for START_MS again.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import numpy as np

log = logging.getLogger("projector.audio")

MAX_VOLUME = 2.0  # 200%: above 100% the signal is amplified (and may clip)

START_MS = 70
TARGET_MS = 100
MAX_MS = 200


def list_output_devices() -> list[tuple[int, str]]:
    import sounddevice as sd

    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] > 0:
            api = sd.query_hostapis(d["hostapi"])["name"]
            out.append((i, f"{d['name']} ({api})"))
    return out


class AudioPlayer:
    def __init__(self, device: Optional[str] = None, volume: float = 1.0):
        self.device = int(device) if device and device.isdigit() else device
        # Applied to what this process plays, so Discord/OBS capture the adjusted level too.
        # It can't bring back audio the phone sends as silence (iPhone volume at 0).
        self.volume = min(MAX_VOLUME, max(0.0, volume))
        self.muted = False
        self._lock = threading.Lock()
        self._dev_lock = threading.RLock()  # opening/closing the stream (capture vs. device switch)
        self._buf = bytearray()
        self._stream = None
        self._retry_at = 0.0  # after a failed open, wait before trying again
        self._fmt: Optional[tuple[int, int, int]] = None  # rate, channels, bytes per sample
        self._playing = False
        self.underruns = 0
        self.trims = 0
        self.received_bytes = 0
        self.peak = 0  # loudest sample since last take_peak(), for the stats line

    # -- capture thread ---------------------------------------------------------

    def set_format(self, asbd) -> None:
        fmt = (int(asbd.sample_rate), asbd.channels, asbd.bits_per_channel // 8)
        with self._dev_lock:
            if fmt != self._fmt or self._stream is None:
                self._open(*fmt)

    def push(self, sb) -> None:
        data = sb.data
        if not data:
            return
        if self._stream is None and time.monotonic() >= self._retry_at:
            asbd = sb.format.asbd if sb.format and sb.format.asbd else None
            if asbd is not None:
                self.set_format(asbd)
            else:
                with self._dev_lock:
                    if self._stream is None:
                        self._open(*(self._fmt or (48000, 2, 2)))
        if self._fmt is None:
            return
        self.received_bytes += len(data)
        samples = np.frombuffer(data, dtype="<i2")
        if samples.size:
            self.peak = max(self.peak, int(np.abs(samples.astype(np.int32)).max()))
        with self._lock:
            self._buf += data
            if len(self._buf) > self._ms_to_bytes(MAX_MS):
                drop = len(self._buf) - self._ms_to_bytes(TARGET_MS)
                del self._buf[:drop]
                self.trims += 1

    # -- sound card thread ----------------------------------------------------------

    def _callback(self, outdata, frames, _time, status) -> None:
        need = frames * self._frame_bytes
        with self._lock:
            if not self._playing:
                if len(self._buf) >= self._ms_to_bytes(START_MS):
                    self._playing = True
                else:
                    outdata[:] = bytes(need)
                    return
            if len(self._buf) >= need:
                chunk = bytes(self._buf[:need])
                del self._buf[:need]
            else:
                have = len(self._buf) - len(self._buf) % self._frame_bytes
                chunk = bytes(self._buf[:have]) + bytes(need - have)
                del self._buf[:have]
                self._playing = False
                self.underruns += 1
        outdata[:] = self._apply_volume(chunk)

    def _apply_volume(self, chunk: bytes) -> bytes:
        if self.muted or self.volume <= 0.0:
            return bytes(len(chunk))
        if self.volume == 1.0:
            return chunk
        samples = np.frombuffer(chunk, dtype="<i2").astype(np.float32) * self.volume
        return np.clip(samples, -32768, 32767).astype("<i2").tobytes()

    # -- volume (UI thread) -----------------------------------------------------------

    def set_volume(self, volume: float) -> float:
        self.volume = round(min(MAX_VOLUME, max(0.0, volume)), 2)
        if self.volume > 0:
            self.muted = False
        return self.volume

    def toggle_mute(self) -> bool:
        self.muted = not self.muted
        return self.muted

    # -- helpers ----------------------------------------------------------------------

    @property
    def _frame_bytes(self) -> int:
        _rate, ch, bps = self._fmt
        return ch * bps

    def _ms_to_bytes(self, ms: int) -> int:
        rate, ch, bps = self._fmt
        return rate * ms // 1000 * ch * bps

    def take_peak(self) -> int:
        peak, self.peak = self.peak, 0
        return peak

    def buffered_ms(self) -> float:
        if not self._fmt:
            return 0.0
        rate, ch, bps = self._fmt
        with self._lock:
            return len(self._buf) / (rate * ch * bps) * 1000

    def output_latency_ms(self) -> float:
        try:
            return self._stream.latency * 1000 if self._stream else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    def reopen(self) -> str:
        """Re-scan the sound devices and reopen on the current one (the Windows default unless
        --audio-device was given). PortAudio only reads the device list when it starts."""
        import sounddevice as sd

        with self._dev_lock:
            fmt = self._fmt
            self.close()
            sd._terminate()
            sd._initialize()
            if fmt:
                self._retry_at = 0.0
                self._open(*fmt)
                if self._stream is None:
                    raise RuntimeError("無法開啟聲音裝置")
            return self.device_name()

    def device_name(self) -> str:
        import sounddevice as sd

        try:
            dev = self._stream.device if self._stream else (
                self.device if self.device is not None else sd.default.device[1])
            return sd.query_devices(dev)["name"]
        except Exception:  # noqa: BLE001
            return "（無）"

    def _open(self, rate: int, channels: int, bytes_per_sample: int) -> None:
        import sounddevice as sd

        self.close()
        if bytes_per_sample != 2:
            log.error("不支援 %d-bit 聲音", bytes_per_sample * 8)
            return
        self._fmt = (rate, channels, bytes_per_sample)
        with self._lock:
            self._buf.clear()
            self._playing = False
        try:
            stream = sd.RawOutputStream(
                samplerate=rate, channels=channels, dtype="int16",
                device=self.device, latency="low", callback=self._callback,
            )
            stream.start()
        except Exception:  # noqa: BLE001 - e.g. the device was just unplugged
            log.warning("無法開啟聲音輸出，2 秒後重試", exc_info=True)
            self._retry_at = time.monotonic() + 2.0
            return
        self._stream = stream
        name = sd.query_devices(self._stream.device)["name"]
        log.info("聲音輸出：%s，%d Hz，%d 聲道，裝置延遲 %.0f ms", name, rate, channels, self.output_latency_ms())

    def close(self) -> None:
        with self._dev_lock:
            self._close()

    def _close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001
                log.debug("closing audio stream", exc_info=True)
            self._stream = None
