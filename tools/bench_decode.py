"""Decode a recorded .h264 file: save a screenshot and measure how fast each display path runs.

    python tools/bench_decode.py out/run12/video.h264

The phone sends about 1180x2556 at up to 60 fps, so the display path must keep up with that.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import av


def decode_frames(path: Path):
    container = av.open(str(path), format="h264")
    stream = container.streams.video[0]
    stream.thread_type = "AUTO"
    for packet in container.demux(stream):
        for frame in packet.decode():
            yield frame
    container.close()


def save_png(frame: av.VideoFrame, out: Path, width: int | None = None) -> None:
    if width:
        height = round(frame.height * width / frame.width / 2) * 2
        frame = frame.reformat(width=width, height=height)
    container = av.open(str(out), "w", format="image2")
    s = container.add_stream("png")
    s.width, s.height, s.pix_fmt = frame.width, frame.height, "rgb24"
    for pkt in s.encode(frame.reformat(format="rgb24")):
        container.mux(pkt)
    for pkt in s.encode(None):
        container.mux(pkt)
    container.close()


def bench(label: str, path: Path, work, limit: int = 900) -> float:
    n = 0
    start = time.perf_counter()
    try:
        for frame in decode_frames(path):
            work(frame)
            n += 1
            if n >= limit:
                break
    except Exception as e:  # noqa: BLE001
        print(f"{label:<38} 失敗: {e}")
        return 0.0
    fps = n / (time.perf_counter() - start)
    print(f"{label:<38} {fps:6.1f} fps  ({n} 張)")
    return fps


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "out/run12/video.h264")
    out_dir = path.parent

    first = None
    for i, frame in enumerate(decode_frames(path)):
        if i == 120:  # ~2 s in, past any black first frames
            first = frame
            break
    if first is None:
        print("檔案裡沒有可解碼的畫面")
        return 1
    print(f"解析度 {first.width}x{first.height}，格式 {first.format.name}")
    shot = out_dir / "screenshot.png"
    save_png(first, shot, width=590)
    print(f"截圖存到 {shot.resolve()}")

    half_w, half_h = 590, 1278
    bench("只解碼（CPU，多執行緒）", path, lambda f: None)
    bench("解碼 + 轉 RGB 全解析度", path, lambda f: f.to_ndarray(format="rgb24"))
    bench("解碼 + 縮成一半再轉 RGB", path,
          lambda f: f.reformat(width=half_w, height=half_h, format="rgb24").to_ndarray())
    return 0


if __name__ == "__main__":
    sys.exit(main())
