"""H.264 decoding for live display.

Compressed samples are never dropped (later frames depend on earlier ones); they go through
one ordered queue together with format changes. Only decoded frames are dropped: the display
always takes the newest one.

Decoder settings: slice threading + low-delay. Frame threading (PyAV's "AUTO") is faster but
holds back one frame per thread; on this 20-core PC that was 15 frames (~250 ms).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional

import av
import numpy as np
from av.codec.context import Flags

log = logging.getLogger("projector.video")


@dataclass
class DisplayFrame:
    rgb: np.ndarray      # (h, w, 3) uint8, C-contiguous
    width: int
    height: int
    src_width: int       # size of the decoded picture before scaling
    src_height: int
    seq: int


def fit_size(src_w: int, src_h: int, box_w: int, box_h: int) -> tuple[int, int]:
    """Largest size with the source aspect ratio that fits the box (even dimensions)."""
    if src_w <= 0 or src_h <= 0 or box_w <= 0 or box_h <= 0:
        return max(2, box_w // 2 * 2), max(2, box_h // 2 * 2)
    scale = min(box_w / src_w, box_h / src_h)
    w = max(2, int(src_w * scale) // 2 * 2)
    h = max(2, int(src_h * scale) // 2 * 2)
    return w, h


class VideoDecoder:
    def __init__(self):
        self._q: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._latest: Optional[DisplayFrame] = None
        self._target = (590, 1278)
        self._ctx = None
        self._avcc: Optional[bytes] = None
        self._seq = 0
        self._thread = threading.Thread(target=self._run, name="video-decode", daemon=True)
        self._stop = threading.Event()
        # stats
        self.decoded = 0
        self.errors = 0
        self.decode_time = 0.0
        self.received = 0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._q.put(None)
        self._thread.join(2.0)

    # -- producer side (capture thread) --------------------------------------

    def submit_format(self, fd) -> None:
        if fd.avc is not None:
            self._q.put(("format", fd.avc.raw))

    def submit_sample(self, sb) -> None:
        if sb.data:
            self.received += 1
            self._q.put(("sample", sb.data))

    def reset(self) -> None:
        """New session: forget the old decoder state."""
        self._q.put(("reset", None))

    # -- consumer side (UI thread) --------------------------------------------

    def set_target(self, width: int, height: int) -> None:
        with self._lock:
            self._target = (max(2, width), max(2, height))

    def take_latest(self, after_seq: int) -> Optional[DisplayFrame]:
        with self._lock:
            f = self._latest
        if f is not None and f.seq > after_seq:
            return f
        return None

    def backlog(self) -> int:
        return self._q.qsize()

    # -- worker ---------------------------------------------------------------

    def _open(self, avcc: bytes) -> None:
        ctx = av.CodecContext.create("h264", "r")
        ctx.extradata = avcc
        ctx.thread_type = "SLICE"
        ctx.flags |= Flags.low_delay
        self._ctx = ctx
        self._avcc = avcc
        log.info("H.264 解碼器已建立（avcC %d bytes）", len(avcc))

    def _run(self) -> None:
        while not self._stop.is_set():
            item = self._q.get()
            if item is None:
                return
            kind, payload = item
            try:
                if kind == "format":
                    if payload != self._avcc:
                        self._open(payload)
                elif kind == "reset":
                    self._ctx = None
                    self._avcc = None
                elif kind == "sample" and self._ctx is not None:
                    self._decode(payload)
            except Exception:  # noqa: BLE001
                self.errors += 1
                if self.errors <= 5 or self.errors % 100 == 0:
                    log.exception("解碼失敗（第 %d 次）", self.errors)

    def _decode(self, data: bytes) -> None:
        t0 = time.perf_counter()
        frames = self._ctx.decode(av.Packet(data))
        for frame in frames:
            # Only scale the newest frame if we're behind; skip conversion for stale ones.
            if self._q.qsize() > 2 and frame is not frames[-1]:
                continue
            with self._lock:
                box_w, box_h = self._target
            w, h = fit_size(frame.width, frame.height, box_w, box_h)
            rgb = frame.reformat(width=w, height=h, format="rgb24", interpolation="BILINEAR").to_ndarray()
            rgb = np.ascontiguousarray(rgb)
            self._seq += 1
            df = DisplayFrame(rgb, w, h, frame.width, frame.height, self._seq)
            with self._lock:
                self._latest = df
            self.decoded += 1
        self.decode_time += time.perf_counter() - t0
