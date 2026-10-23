"""Writers used by the feasibility test: raw H.264 (Annex B) and WAV, plus simple stats."""

from __future__ import annotations

import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import protocol as p

START_CODE = b"\x00\x00\x00\x01"


@dataclass
class Stats:
    video_samples: int = 0
    video_bytes: int = 0
    keyframes: int = 0
    first_video_pts: Optional[float] = None
    last_video_pts: Optional[float] = None
    width: int = 0
    height: int = 0
    codec: str = ""
    samples_without_avcc: int = 0
    audio_samples: int = 0
    audio_bytes: int = 0
    audio_rate: float = 0.0
    audio_channels: int = 0
    audio_bits: int = 0
    nal_types: dict = field(default_factory=dict)

    def video_seconds(self) -> float:
        if self.first_video_pts is None or self.last_video_pts is None:
            return 0.0
        return max(0.0, self.last_video_pts - self.first_video_pts)

    def audio_seconds(self) -> float:
        frame_bytes = self.audio_channels * self.audio_bits // 8
        if not frame_bytes or not self.audio_rate:
            return 0.0
        return self.audio_bytes / frame_bytes / self.audio_rate


class H264Writer:
    def __init__(self, path: Path, stats: Stats):
        self.f = open(path, "wb")
        self.stats = stats
        self.nal_length_size = 4
        self._last_avcc: Optional[bytes] = None

    def on_format(self, fd: p.FormatDescription) -> None:
        self.stats.width, self.stats.height = fd.width, fd.height
        self.stats.codec = p.fourcc_str(fd.codec)
        if fd.avc and fd.avc.raw != self._last_avcc:
            self._last_avcc = fd.avc.raw
            self.nal_length_size = fd.avc.nal_length_size
            for nal in fd.avc.sps + fd.avc.pps:
                self.f.write(START_CODE + nal)

    def on_sample(self, sb: p.SampleBuffer) -> None:
        if not sb.data:
            return
        if self._last_avcc is None:
            # Can't write anything before the first SPS/PPS (or the codec isn't H.264).
            self.stats.samples_without_avcc += 1
            return
        s = self.stats
        s.video_samples += 1
        s.video_bytes += len(sb.data)
        pts = sb.presentation_time()
        if pts and pts.scale:
            sec = pts.seconds()
            if s.first_video_pts is None:
                s.first_video_pts = sec
            s.last_video_pts = sec
        for nal in p.avcc_to_nalus(sb.data, self.nal_length_size):
            t = nal[0] & 0x1F
            s.nal_types[t] = s.nal_types.get(t, 0) + 1
            if t == 5:
                s.keyframes += 1
            self.f.write(START_CODE + nal)

    def close(self) -> None:
        self.f.close()


class WavWriter:
    def __init__(self, path: Path, stats: Stats):
        self.path = path
        self.stats = stats
        self.w: Optional[wave.Wave_write] = None

    def on_format(self, asbd: p.AudioStreamBasicDescription) -> None:
        if self.w is None:
            self._open(asbd)

    def _open(self, asbd: p.AudioStreamBasicDescription) -> None:
        self.w = wave.open(str(self.path), "wb")
        self.w.setnchannels(asbd.channels)
        self.w.setsampwidth(asbd.bits_per_channel // 8)
        self.w.setframerate(int(asbd.sample_rate))
        s = self.stats
        s.audio_rate, s.audio_channels, s.audio_bits = asbd.sample_rate, asbd.channels, asbd.bits_per_channel

    def on_sample(self, sb: p.SampleBuffer) -> None:
        if not sb.data:
            return
        if self.w is None:
            self._open(sb.format.asbd if sb.format and sb.format.asbd else p.AudioStreamBasicDescription())
        self.stats.audio_samples += 1
        self.stats.audio_bytes += len(sb.data)
        self.w.writeframes(sb.data)

    def close(self) -> None:
        if self.w is not None:
            self.w.close()
