"""H.264 decoding for live display.

Compressed samples are never dropped (later frames depend on earlier ones); they go through
one ordered queue together with format changes. Only decoded frames are dropped: the display
always takes the newest one.

Decoder settings: slice threading + low-delay. Frame threading (PyAV's "AUTO") is faster but
holds back one frame per thread; on this 20-core PC that was 15 frames (~250 ms).
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import av
import numpy as np
from av.codec.context import Flags

from .protocol import avcc_to_nalus

log = logging.getLogger("projector.video")

# Catch-up rules (see VideoDecoder._run)
MAX_AGE = 0.3          # a sample that waited longer than this in the queue means we're behind
MAX_DROP_TIME = 1.5    # give up waiting for a keyframe after this long and decode anyway
CONVERT_EVERY = 1 / 60  # while behind, convert to RGB at most this often (display rate)

# Scaling to the window size. Measured on a 1180x2556 frame: BILINEAR 2-5 ms, LANCZOS 4-9 ms.
SCALE_SHARP = "LANCZOS"   # crisper text
SCALE_FAST = "BILINEAR"   # cheaper, slightly softer

# Debug: pretend decoding is slow (ms per frame) to exercise the catch-up path.
SLOW_DECODE_MS = float(os.environ.get("PROJECTOR_SLOW_DECODE_MS", "0") or 0)


def is_keyframe(data: bytes) -> bool:
    """True if an AVCC sample contains an IDR slice (NAL type 5)."""
    return any(n and (n[0] & 0x1F) == 5 for n in avcc_to_nalus(data))


@dataclass
class DisplayFrame:
    rgb: np.ndarray      # (h, w, 3) uint8, C-contiguous
    width: int
    height: int
    src_width: int       # size of the decoded picture before scaling
    src_height: int
    seq: int
    t_arrival: float = 0.0  # perf_counter when the compressed sample arrived over USB


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
        self._pending: deque = deque()  # items taken back out of the queue while skipping ahead
        self._lock = threading.Lock()
        self._latest: Optional[DisplayFrame] = None
        self._raw = None  # newest decoded frame at full size (for screenshots)
        self.scale_method = SCALE_SHARP
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
        # latency diagnostics
        self._t0: Optional[float] = None      # arrival of the first sample (perf_counter)
        self._pts0: Optional[float] = None    # its presentation time (s)
        self.arrival_lag = 0.0                # how late the latest sample arrived vs. its pts (s)
        self.arrival_lag_min: Optional[float] = None
        self.queue_wait_max = 0.0             # longest wait in the decode queue since last read (s)
        self.dropped = 0                      # compressed samples skipped to catch up
        self.drop_events = 0
        self._last_idr: Optional[float] = None
        self.idr_gap_max = 0.0                # longest time between keyframes since last read (s)
        self._dropping_since: Optional[float] = None
        self._last_convert = 0.0
        self._video_off = False  # worker thread only; changed through the queue

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
            now = time.perf_counter()
            pts = sb.presentation_time()
            if pts is not None and pts.scale:
                sec = pts.seconds()
                if self._t0 is None or self._pts0 is None or sec < self._pts0:
                    self._t0, self._pts0, self.arrival_lag_min = now, sec, None
                lag = (now - self._t0) - (sec - self._pts0)
                self.arrival_lag = lag
                if self.arrival_lag_min is None or lag < self.arrival_lag_min:
                    self.arrival_lag_min = lag
            idr = is_keyframe(sb.data)
            if idr:
                if self._last_idr is not None:
                    self.idr_gap_max = max(self.idr_gap_max, now - self._last_idr)
                self._last_idr = now
            self._q.put(("sample", (sb.data, now, idr)))

    def take_idr_gap_max(self) -> float:
        v, self.idr_gap_max = self.idr_gap_max, 0.0
        return v

    def take_queue_wait_max(self) -> float:
        v, self.queue_wait_max = self.queue_wait_max, 0.0
        return v

    def arrival_drift(self) -> float:
        """Extra arrival delay compared with the best moment of this session (s)."""
        if self.arrival_lag_min is None:
            return 0.0
        return self.arrival_lag - self.arrival_lag_min

    def reset(self) -> None:
        """New session: forget the old decoder state."""
        self._t0 = self._pts0 = self.arrival_lag_min = None
        self._last_idr = None
        self._dropping_since = None
        self._q.put(("reset", None))

    def set_enabled(self, on: bool) -> None:
        """Turn decoding off (saves CPU while the picture is hidden) or back on."""
        self._q.put(("enable", on))

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

    def snapshot(self) -> Optional[np.ndarray]:
        """Newest picture at the phone's full resolution, as (h, w, 3) RGB."""
        with self._lock:
            raw = self._raw
        if raw is None:
            return None
        return np.ascontiguousarray(raw.reformat(format="rgb24").to_ndarray())

    def backlog(self) -> int:
        return self._q.qsize() + len(self._pending)

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
            item = self._pending.popleft() if self._pending else self._q.get()
            if item is None:
                return
            kind, payload = item
            try:
                if kind == "format":
                    if payload != self._avcc:
                        self._open(payload)
                elif kind == "reset":
                    with self._lock:
                        self._raw = None
                    self._ctx = None
                    self._avcc = None
                elif kind == "enable":
                    self._set_enabled(payload)
                elif kind == "sample" and self._ctx is not None:
                    self._handle_sample(*payload)
            except Exception:  # noqa: BLE001
                self.errors += 1
                if self.errors <= 5 or self.errors % 100 == 0:
                    log.exception("解碼失敗（第 %d 次）", self.errors)

    def _set_enabled(self, on: bool) -> None:
        if on and self._video_off:
            # Frames depend on earlier ones we skipped: resume from the next keyframe
            # (one every ~0.5 s), the same way catching up does.
            self._dropping_since = time.perf_counter()
        self._video_off = not on
        if not on:
            with self._lock:
                self._raw = None  # so a screenshot never shows a picture from before

    def _handle_sample(self, data: bytes, t_put: float, idr: bool) -> None:
        """Decode one sample, or drop it if we're too far behind.

        Compressed samples depend on earlier ones, so we can only skip ahead to a keyframe
        (the phone sends one every 30 frames, about 0.5 s at 60 fps). When the oldest sample
        has waited longer than MAX_AGE, drop everything up to the next keyframe; this bounds
        the delay instead of letting the queue grow without end.
        """
        if self._video_off:
            return
        now = time.perf_counter()
        age = now - t_put
        self.queue_wait_max = max(self.queue_wait_max, age)

        if self._dropping_since is None and age > MAX_AGE:
            self._skip_ahead(("sample", (data, t_put, idr)), age)
            return
        if self._dropping_since is not None:
            if idr:
                log.info("從關鍵影格接續（跳過 %.2f 秒）", now - self._dropping_since)
                self._dropping_since = None
            elif now - self._dropping_since > MAX_DROP_TIME:
                log.warning("%.1f 秒內沒有關鍵影格，直接繼續解碼（可能短暫出現殘影）", MAX_DROP_TIME)
                self._dropping_since = None
            else:
                self.dropped += 1
                return
        self._decode(data, t_put)

    def _skip_ahead(self, current, age: float) -> None:
        """Jump to the newest keyframe already waiting; if there is none, drop until one arrives."""
        items = [current] + list(self._pending)
        self._pending.clear()
        while True:
            try:
                items.append(self._q.get_nowait())
            except queue.Empty:
                break
        last_idr = max((i for i, it in enumerate(items)
                        if it is not None and it[0] == "sample" and it[1][2]), default=None)
        self.drop_events += 1
        keep_from = last_idr if last_idr is not None else len(items)
        skipped = 0
        for i, it in enumerate(items):
            if i < keep_from and it is not None and it[0] == "sample":
                skipped += 1
            else:
                self._pending.append(it)  # formats/resets/stop, and everything from the keyframe on
        self.dropped += skipped
        if last_idr is not None:
            log.warning("解碼落後 %.0f ms，跳到最新的關鍵影格（略過 %d 張）", age * 1000, skipped)
        else:
            self._dropping_since = time.perf_counter()
            log.warning("解碼落後 %.0f ms，略過 %d 張並等待下一張關鍵影格", age * 1000, skipped)

    def _decode(self, data: bytes, t_arrival: float = 0.0) -> None:
        t0 = time.perf_counter()
        if SLOW_DECODE_MS:
            time.sleep(SLOW_DECODE_MS / 1000)
        frames = self._ctx.decode(av.Packet(data))
        for frame in frames:
            self.decoded += 1
            with self._lock:
                self._raw = frame
            # While more samples are waiting, skip the RGB conversion for frames that would
            # be replaced before the screen could show them anyway.
            now = time.perf_counter()
            if self.backlog() > 0 and now - self._last_convert < CONVERT_EVERY:
                continue
            self._last_convert = now
            with self._lock:
                box_w, box_h = self._target
            w, h = fit_size(frame.width, frame.height, box_w, box_h)
            rgb = frame.reformat(width=w, height=h, format="rgb24", interpolation=self.scale_method).to_ndarray()
            rgb = np.ascontiguousarray(rgb)
            self._seq += 1
            df = DisplayFrame(rgb, w, h, frame.width, frame.height, self._seq, t_arrival)
            with self._lock:
                self._latest = df
        self.decode_time += time.perf_counter() - t0
