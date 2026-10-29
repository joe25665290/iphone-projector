"""iPhone USB screen-capture protocol (the one QuickTime on a Mac uses).

Ported from danielpaulus/quicktime_video_hack (MIT license).

Wire format basics:
- Every packet starts with a 4-byte little-endian length that includes itself.
- Four-character codes are stored as little-endian uint32, so 'sync' shows up
  on the wire as b'cnys'.
- All integers are little endian, except the H.264 NAL length prefixes inside
  video samples, which are big endian (standard AVCC).
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger("projector.protocol")


def fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


def fourcc_str(value: int) -> str:
    try:
        return value.to_bytes(4, "big").decode("ascii")
    except (UnicodeDecodeError, OverflowError):
        return f"0x{value:08x}"


# Packet types
PING = fourcc("ping")
SYNC = fourcc("sync")
ASYN = fourcc("asyn")
RPLY = fourcc("rply")

# SYNC subtypes (device asks, we must reply)
TIME = fourcc("time")
CWPA = fourcc("cwpa")
AFMT = fourcc("afmt")
CVRP = fourcc("cvrp")
CLOK = fourcc("clok")
OG = fourcc("go! ")
SKEW = fourcc("skew")
STOP = fourcc("stop")

# ASYN subtypes (one-way messages)
FEED = fourcc("feed")  # video sample
EAT = fourcc("eat!")  # audio sample
TJMP = fourcc("tjmp")
SRAT = fourcc("srat")
SPRP = fourcc("sprp")
TBAS = fourcc("tbas")
RELS = fourcc("rels")
HPD1 = fourcc("hpd1")
HPA1 = fourcc("hpa1")
HPD0 = fourcc("hpd0")
HPA0 = fourcc("hpa0")
NEED = fourcc("need")

# Dictionary encoding
KEYV = fourcc("keyv")
STRK = fourcc("strk")
IDXK = fourcc("idxk")
BULV = fourcc("bulv")
DICT = fourcc("dict")
DATV = fourcc("datv")
STRV = fourcc("strv")
NMBV = fourcc("nmbv")

# CMSampleBuffer atoms
SBUF = fourcc("sbuf")
OPTS = fourcc("opts")
STIA = fourcc("stia")
SDAT = fourcc("sdat")
SATT = fourcc("satt")
SARY = fourcc("sary")
SSIZ = fourcc("ssiz")
NSMP = fourcc("nsmp")

# Format description atoms
FDSC = fourcc("fdsc")
MDIA = fourcc("mdia")
VDIM = fourcc("vdim")
CODC = fourcc("codc")
EXTN = fourcc("extn")
ASBD = fourcc("asbd")
MEDIA_VIDEO = fourcc("vide")
MEDIA_SOUND = fourcc("soun")
CODEC_AVC1 = fourcc("avc1")
AUDIO_FORMAT_LPCM = fourcc("lpcm")

EMPTY_CF_TYPE = 1

# Restart the audio skew measurement when audio stops or jumps, and clamp wild answers.
# Switchable for A/B tests against qvh's plain behaviour.
SKEW_GUARD = True
PING_HEADER = 0x0000000100000000
MAX_PACKET = 64 * 1024 * 1024


class ProtocolError(Exception):
    pass


# ---------------------------------------------------------------------------
# Basic value types


@dataclass(frozen=True)
class NSNumber:
    type_specifier: int
    value: float | int

    @staticmethod
    def u32(v: int) -> "NSNumber":
        return NSNumber(3, v)

    @staticmethod
    def u64(v: int) -> "NSNumber":
        return NSNumber(4, v)

    @staticmethod
    def f64(v: float) -> "NSNumber":
        return NSNumber(6, v)

    def to_bytes(self) -> bytes:
        t = self.type_specifier
        if t in (3, 5):
            return struct.pack("<BI", t, self.value)
        if t == 4:
            return struct.pack("<BQ", t, self.value)
        if t == 6:
            return struct.pack("<Bd", t, self.value)
        raise ProtocolError(f"cannot serialize NSNumber type {t}")

    @staticmethod
    def parse(b: bytes) -> "NSNumber":
        t = b[0]
        if t in (3, 5) and len(b) >= 5:
            return NSNumber(t, struct.unpack_from("<I", b, 1)[0])
        if t == 4 and len(b) >= 9:
            return NSNumber(t, struct.unpack_from("<Q", b, 1)[0])
        if t == 6 and len(b) >= 9:
            return NSNumber(t, struct.unpack_from("<d", b, 1)[0])
        # Unknown width: keep the raw integer so parsing never fails on it.
        return NSNumber(t, int.from_bytes(b[1:], "little"))


@dataclass(frozen=True)
class CMTime:
    value: int = 0
    scale: int = 0
    flags: int = 0
    epoch: int = 0

    SIZE = 24
    FLAG_HAS_BEEN_ROUNDED = 0x1

    def pack(self) -> bytes:
        return struct.pack("<qIIq", self.value, self.scale, self.flags, self.epoch)

    @staticmethod
    def parse(b: bytes, offset: int = 0) -> "CMTime":
        return CMTime(*struct.unpack_from("<qIIq", b, offset))

    def seconds(self) -> float:
        return self.value / self.scale if self.scale else 0.0


@dataclass(frozen=True)
class AudioStreamBasicDescription:
    sample_rate: float = 48000.0
    format_id: int = AUDIO_FORMAT_LPCM
    format_flags: int = 12  # signed integer | packed, little endian
    bytes_per_packet: int = 4
    frames_per_packet: int = 1
    bytes_per_frame: int = 4
    channels: int = 2
    bits_per_channel: int = 16
    reserved: int = 0

    _FMT = "<dIIIIIIII"
    SIZE = struct.calcsize(_FMT)  # 40

    @staticmethod
    def parse(b: bytes, offset: int = 0) -> "AudioStreamBasicDescription":
        return AudioStreamBasicDescription(
            *struct.unpack_from(AudioStreamBasicDescription._FMT, b, offset)
        )

    def pack(self) -> bytes:
        return struct.pack(
            self._FMT,
            self.sample_rate,
            AUDIO_FORMAT_LPCM,
            self.format_flags,
            self.bytes_per_packet,
            self.frames_per_packet,
            self.bytes_per_frame,
            self.channels,
            self.bits_per_channel,
            self.reserved,
        )

    def pack_for_hpa1(self) -> bytes:
        # The HPA1 "formats" blob is the ASBD followed by the sample rate twice (56 bytes).
        return self.pack() + struct.pack("<dd", self.sample_rate, self.sample_rate)


# ---------------------------------------------------------------------------
# Dictionary serialization


def _lm(length: int, magic: int) -> bytes:
    return struct.pack("<II", length, magic)


def _read_lm(b: bytes, offset: int = 0) -> tuple[int, int]:
    if len(b) - offset < 8:
        raise ProtocolError("truncated length/magic header")
    length, magic = struct.unpack_from("<II", b, offset)
    if length < 8 or offset + length > len(b):
        raise ProtocolError(
            f"bad atom length {length} for {fourcc_str(magic)} (have {len(b) - offset} bytes)"
        )
    return length, magic


def serialize_value(v) -> bytes:
    if isinstance(v, bool):
        return _lm(9, BULV) + (b"\x01" if v else b"\x00")
    if isinstance(v, NSNumber):
        nb = v.to_bytes()
        return _lm(8 + len(nb), NMBV) + nb
    if isinstance(v, str):
        sb = v.encode("utf-8")
        return _lm(8 + len(sb), STRV) + sb
    if isinstance(v, (bytes, bytearray)):
        return _lm(8 + len(v), DATV) + bytes(v)
    if isinstance(v, dict):
        return serialize_dict(v)
    raise ProtocolError(f"cannot serialize value of type {type(v).__name__}")


def serialize_dict(d: dict) -> bytes:
    body = bytearray()
    for key, value in d.items():
        kb = key.encode("utf-8")
        key_bytes = _lm(8 + len(kb), STRK) + kb
        value_bytes = serialize_value(value)
        body += _lm(8 + len(key_bytes) + len(value_bytes), KEYV) + key_bytes + value_bytes
    return _lm(8 + len(body), DICT) + bytes(body)


@dataclass(frozen=True)
class UnknownValue:
    magic: int
    payload: bytes


def parse_value(b: bytes, offset: int = 0):
    length, magic = _read_lm(b, offset)
    payload = b[offset + 8 : offset + length]
    if magic == STRV:
        return payload.decode("utf-8", errors="replace")
    if magic == DATV:
        return bytes(payload)
    if magic == BULV:
        return bool(payload[0]) if payload else False
    if magic == NMBV:
        return NSNumber.parse(payload)
    if magic == DICT:
        return parse_dict(b[offset : offset + length])
    if magic == FDSC:
        return FormatDescription.parse(b[offset : offset + length])
    return UnknownValue(magic, bytes(payload))


def parse_dict(b: bytes, expected_magic: int = DICT) -> dict:
    """Parse a dictionary with string keys, integer keys, or a mix."""
    length, magic = _read_lm(b)
    if magic != expected_magic:
        raise ProtocolError(f"expected {fourcc_str(expected_magic)}, got {fourcc_str(magic)}")
    result: dict = {}
    pos = 8
    while pos < length:
        entry_len, entry_magic = _read_lm(b, pos)
        if entry_magic != KEYV:
            raise ProtocolError(f"expected keyv, got {fourcc_str(entry_magic)}")
        entry = b[pos + 8 : pos + entry_len]
        key_len, key_magic = _read_lm(entry)
        if key_magic == STRK:
            key = entry[8:key_len].decode("utf-8", errors="replace")
        elif key_magic == IDXK:
            key = struct.unpack_from("<H", entry, 8)[0]
        else:
            raise ProtocolError(f"unknown dict key type {fourcc_str(key_magic)}")
        result[key] = parse_value(entry, key_len)
        pos += entry_len
    return result


# ---------------------------------------------------------------------------
# Format description and sample buffers


@dataclass
class AvcConfig:
    """Parsed avcC record (H.264 decoder configuration)."""

    raw: bytes
    profile: int
    level: int
    nal_length_size: int
    sps: list[bytes]
    pps: list[bytes]

    @staticmethod
    def parse(b: bytes) -> "AvcConfig":
        if len(b) < 7:
            raise ProtocolError("avcC too short")
        nal_length_size = (b[4] & 0x03) + 1
        pos = 5
        sps = []
        for _ in range(b[pos] & 0x1F):
            n = struct.unpack_from(">H", b, pos + 1)[0]
            sps.append(bytes(b[pos + 3 : pos + 3 + n]))
            pos += 2 + n
        pos += 1
        pps = []
        for _ in range(b[pos]):
            n = struct.unpack_from(">H", b, pos + 1)[0]
            pps.append(bytes(b[pos + 3 : pos + 3 + n]))
            pos += 2 + n
        return AvcConfig(bytes(b), b[1], b[3], nal_length_size, sps, pps)


@dataclass
class FormatDescription:
    media_type: int
    width: int = 0
    height: int = 0
    codec: int = 0
    extensions: dict = field(default_factory=dict)
    avc: Optional[AvcConfig] = None
    asbd: Optional[AudioStreamBasicDescription] = None

    @staticmethod
    def parse(b: bytes) -> "FormatDescription":
        length, magic = _read_lm(b)
        if magic != FDSC:
            raise ProtocolError(f"expected fdsc, got {fourcc_str(magic)}")
        fd = FormatDescription(media_type=0)
        pos = 8
        while pos < length:
            atom_len, atom_magic = _read_lm(b, pos)
            if atom_magic == MDIA:
                fd.media_type = struct.unpack_from("<I", b, pos + 8)[0]
            elif atom_magic == VDIM:
                fd.width, fd.height = struct.unpack_from("<II", b, pos + 8)
            elif atom_magic == CODC:
                fd.codec = struct.unpack_from("<I", b, pos + 8)[0]
            elif atom_magic == EXTN:
                fd.extensions = parse_dict(b[pos : pos + atom_len], EXTN)
            elif atom_magic == ASBD:
                fd.asbd = AudioStreamBasicDescription.parse(b, pos + 8)
            pos += atom_len
        # The avcC record sits at extensions[49][105] (CoreMedia's sample description atoms).
        atoms = fd.extensions.get(49)
        if isinstance(atoms, dict) and isinstance(atoms.get(105), bytes):
            fd.avc = AvcConfig.parse(atoms[105])
        return fd


@dataclass
class SampleBuffer:
    media_type: int
    output_pts: Optional[CMTime] = None
    timing: list[tuple[CMTime, CMTime, CMTime]] = field(default_factory=list)
    data: bytes = b""
    num_samples: int = 0
    sample_sizes: list[int] = field(default_factory=list)
    format: Optional[FormatDescription] = None

    @staticmethod
    def parse(b: bytes, media_type: int) -> "SampleBuffer":
        length, magic = _read_lm(b)
        if magic != SBUF:
            raise ProtocolError(f"expected sbuf, got {fourcc_str(magic)}")
        sb = SampleBuffer(media_type)
        pos = 8
        while pos < length:
            atom_len, atom_magic = _read_lm(b, pos)
            body = pos + 8
            end = pos + atom_len
            if atom_magic == OPTS:
                sb.output_pts = CMTime.parse(b, body)
            elif atom_magic == STIA:
                for i in range(body, end - 71, 72):
                    sb.timing.append(
                        (CMTime.parse(b, i), CMTime.parse(b, i + 24), CMTime.parse(b, i + 48))
                    )
            elif atom_magic == SDAT:
                sb.data = bytes(b[body:end])
            elif atom_magic == NSMP:
                sb.num_samples = struct.unpack_from("<I", b, body)[0]
            elif atom_magic == SSIZ:
                sb.sample_sizes = list(struct.unpack_from(f"<{(end - body) // 4}I", b, body))
            elif atom_magic == FDSC:
                sb.format = FormatDescription.parse(b[pos:end])
            # satt / sary carry attachments we don't need.
            pos = end
        return sb

    def presentation_time(self) -> Optional[CMTime]:
        if self.timing:
            return self.timing[0][1]
        return self.output_pts


def avcc_to_nalus(data: bytes, nal_length_size: int = 4) -> list[bytes]:
    nalus = []
    pos = 0
    while pos + nal_length_size <= len(data):
        n = int.from_bytes(data[pos : pos + nal_length_size], "big")
        pos += nal_length_size
        if n == 0 or pos + n > len(data):
            break
        nalus.append(data[pos : pos + n])
        pos += n
    return nalus


# ---------------------------------------------------------------------------
# Clocks


class CMClock:
    def __init__(self, clock_id: int, timescale: int = 1_000_000_000):
        self.id = clock_id
        self.timescale = timescale
        self._start = time.perf_counter_ns()

    def get_time(self) -> CMTime:
        elapsed = time.perf_counter_ns() - self._start
        if self.timescale != 1_000_000_000:
            elapsed = int(elapsed * self.timescale / 1_000_000_000)
        return CMTime(elapsed, self.timescale, CMTime.FLAG_HAS_BEEN_ROUNDED, 0)


def calculate_skew(local_start: CMTime, local_end: CMTime, dev_start: CMTime, dev_end: CMTime) -> float:
    """How many device-clock ticks elapse per second of our clock (about 48000 for audio)."""
    local_diff = local_end.value - local_start.value
    dev_diff = dev_end.value - dev_start.value
    if local_diff <= 0 or dev_diff <= 0 or not local_start.scale or not dev_start.scale:
        return float(dev_start.scale or 48000)
    scaled = local_diff * dev_start.scale / local_start.scale
    return dev_start.scale * scaled / dev_diff


# ---------------------------------------------------------------------------
# Packet builders


def ping_packet() -> bytes:
    return struct.pack("<IIQ", 16, PING, PING_HEADER)


def reply_og(correlation: int) -> bytes:
    return struct.pack("<IIQQ", 24, RPLY, correlation, 0)


def reply_clock_ref(correlation: int, clock_ref: int) -> bytes:
    return struct.pack("<IIQIQ", 28, RPLY, correlation, 0, clock_ref)


def reply_time(correlation: int, t: CMTime) -> bytes:
    return struct.pack("<IIQI", 44, RPLY, correlation, 0) + t.pack()


def reply_afmt(correlation: int) -> bytes:
    d = serialize_dict({"Error": NSNumber.u32(0)})
    return struct.pack("<IIQI", 20 + len(d), RPLY, correlation, 0) + d


def reply_skew(correlation: int, skew: float) -> bytes:
    return struct.pack("<IIQId", 28, RPLY, correlation, 0, skew)


def reply_stop(correlation: int) -> bytes:
    return struct.pack("<IIQII", 24, RPLY, correlation, 0, 0)


def asyn_dict_packet(subtype: int, clock_ref: int, d: dict) -> bytes:
    body = serialize_dict(d)
    return struct.pack("<IIQI", 20 + len(body), ASYN, clock_ref, subtype) + body


def asyn_need(clock_ref: int) -> bytes:
    return struct.pack("<IIQI", 20, ASYN, clock_ref, NEED)


def asyn_hpd0() -> bytes:
    return struct.pack("<IIQI", 20, ASYN, EMPTY_CF_TYPE, HPD0)


def asyn_hpa0(clock_ref: int) -> bytes:
    return struct.pack("<IIQI", 20, ASYN, clock_ref, HPA0)


def hpd1_device_info() -> dict:
    # Tells the phone what kind of "display" we are. Mirrors what macOS sends.
    return {
        "Valeria": True,
        "HEVCDecoderSupports444": True,
        "DisplaySize": {"Width": NSNumber.f64(1920.0), "Height": NSNumber.f64(1200.0)},
    }


def hpa1_device_info() -> dict:
    return {
        "BufferAheadInterval": NSNumber.f64(0.07300000000000001),
        "deviceUID": "Valeria",
        "ScreenLatency": NSNumber.f64(0.04),
        "formats": AudioStreamBasicDescription().pack_for_hpa1(),
        "EDIDAC3Support": NSNumber.u32(0),
        "deviceName": "Valeria",
    }


# ---------------------------------------------------------------------------
# Framing and the message state machine


class Framer:
    """Splits a raw byte stream from the USB bulk endpoint into packets (length prefix removed)."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buf += chunk
        frames = []
        while len(self._buf) >= 4:
            length = struct.unpack_from("<I", self._buf, 0)[0]
            if length < 8 or length > MAX_PACKET:
                raise ProtocolError(f"invalid packet length {length}; stream out of sync")
            if len(self._buf) < length:
                break
            frames.append(bytes(self._buf[4:length]))
            del self._buf[:length]
        return frames


class MessageProcessor:
    """Answers the phone's handshake/clock requests and hands out audio and video samples.

    `write` sends bytes to the phone. The callbacks receive parsed data; any of them may be None.
    """

    def __init__(
        self,
        write: Callable[[bytes], None],
        on_video_format: Optional[Callable[[FormatDescription], None]] = None,
        on_video: Optional[Callable[[SampleBuffer], None]] = None,
        on_audio_format: Optional[Callable[[AudioStreamBasicDescription], None]] = None,
        on_audio: Optional[Callable[[SampleBuffer], None]] = None,
    ):
        self.write = write
        self.on_video_format = on_video_format
        self.on_video = on_video
        self.on_audio_format = on_audio_format
        self.on_audio = on_audio

        self.clock: Optional[CMClock] = None
        self.local_audio_clock: Optional[CMClock] = None
        self.device_audio_clock_ref = 0
        self.need_clock_ref = 0
        self.need_message: Optional[bytes] = None

        self._audio_start_dev: Optional[CMTime] = None
        self._audio_start_local: Optional[CMTime] = None
        self._audio_last_dev: Optional[CMTime] = None
        self._audio_last_local: Optional[CMTime] = None

        self.ping_received = False
        self.handshake_done = False
        self.releases = 0
        self.release_event = threading.Event()
        self.counts: dict[str, int] = {}
        # Ending from the phone side ("Stop Screen Mirroring" in Control Center)
        self.host_stopping = False                # we sent HPA0/HPD0
        self.device_released = False              # RELS arrived without us asking
        self.video_stop_at: Optional[float] = None  # phone sent STOP for a non-audio stream
        self.last_feed_at: Optional[float] = None

    def _count(self, name: str) -> None:
        self.counts[name] = self.counts.get(name, 0) + 1

    def handle(self, frame: bytes) -> None:
        if len(frame) < 4:
            log.warning("ignoring %d-byte frame", len(frame))
            return
        magic = struct.unpack_from("<I", frame, 0)[0]
        if magic == PING:
            self._count("ping")
            self.ping_received = True
            log.info("PING received, replying")
            self.write(ping_packet())
        elif magic == SYNC:
            self._handle_sync(frame)
        elif magic == ASYN:
            self._handle_asyn(frame)
        else:
            self._count("unknown")
            log.warning("unknown packet type %s: %s", fourcc_str(magic), frame[:32].hex())

    def _handle_sync(self, frame: bytes) -> None:
        clock_ref, subtype, correlation = struct.unpack_from("<QIQ", frame, 4)
        name = fourcc_str(subtype).strip()
        self._count("sync-" + name)
        log.debug("SYNC %s clockRef=%x corr=%x", name, clock_ref, correlation)

        if subtype == OG:
            self.write(reply_og(correlation))
        elif subtype == CWPA:
            device_clock = struct.unpack_from("<Q", frame, 24)[0]
            local_ref = device_clock + 1000
            self.local_audio_clock = CMClock(local_ref)
            self.device_audio_clock_ref = device_clock
            hpd1 = asyn_dict_packet(HPD1, EMPTY_CF_TYPE, hpd1_device_info())
            self.write(hpd1)
            self.write(hpd1)
            self.write(reply_clock_ref(correlation, local_ref))
            self.write(asyn_dict_packet(HPA1, device_clock, hpa1_device_info()))
            log.info("CWPA handshake answered (audio clock %x)", device_clock)
        elif subtype == CVRP:
            device_clock = struct.unpack_from("<Q", frame, 24)[0]
            self.need_clock_ref = device_clock
            self.need_message = asyn_need(device_clock)
            self.write(self.need_message)
            self.write(reply_clock_ref(correlation, device_clock + 0x1000AF))
            self.handshake_done = True
            try:
                payload = parse_dict(frame[32:])
                fd = payload.get("FormatDescription")
                if isinstance(fd, FormatDescription):
                    log.info(
                        "CVRP: phone will send %dx%d, codec %s",
                        fd.width, fd.height, fourcc_str(fd.codec),
                    )
                log.debug("CVRP payload: %s", payload)
            except ProtocolError as e:
                log.info("CVRP video request (payload not parsed: %s)", e)
        elif subtype == CLOK:
            local_ref = clock_ref + 0x10000
            self.clock = CMClock(local_ref)
            self.write(reply_clock_ref(correlation, local_ref))
        elif subtype == TIME:
            now = self.clock.get_time() if self.clock else CMTime(0, 1_000_000_000, 1, 0)
            self.write(reply_time(correlation, now))
        elif subtype == AFMT:
            asbd = AudioStreamBasicDescription.parse(frame, 24)
            log.info("Audio format: %s", asbd)
            if self.on_audio_format:
                self.on_audio_format(asbd)
            self.write(reply_afmt(correlation))
        elif subtype == SKEW:
            # The phone paces its audio by this answer: device ticks per second of our clock.
            nominal = float(self._audio_start_dev.scale) if self._audio_start_dev else 48000.0
            skew = nominal
            if self._audio_start_dev and self._audio_last_dev:
                skew = calculate_skew(
                    self._audio_start_local, self._audio_last_local,
                    self._audio_start_dev, self._audio_last_dev,
                )
                if SKEW_GUARD and not (nominal * 0.98 <= skew <= nominal * 1.02):
                    # A wild answer makes the phone send audio too fast or too slow.
                    log.warning("skew %.1f out of range, answering %.0f and restarting measurement", skew, nominal)
                    skew = nominal
                    self._reset_audio_timing()
            log.debug("SKEW reply %.2f", skew)
            self.write(reply_skew(correlation, skew))
        elif subtype == STOP:
            # The phone stops one stream (usually audio, e.g. when playback pauses). Reply and
            # keep going; only the host ends the session (HPA0/HPD0 -> RELS), like qvh.
            which = "audio" if self.local_audio_clock and clock_ref == self.local_audio_clock.id else "stream"
            log.info("Device sent STOP for %s (clockRef %x); continuing", which, clock_ref)
            self._count("stop-" + which)
            if which != "audio":
                self.video_stop_at = time.monotonic()
            if which == "audio" and SKEW_GUARD:
                # Audio pauses with playback; measure skew afresh when it resumes.
                self._reset_audio_timing()
            self.write(reply_stop(correlation))
        else:
            log.warning("unknown SYNC subtype %s: %s", name, frame[:48].hex())

    def _handle_asyn(self, frame: bytes) -> None:
        subtype = struct.unpack_from("<I", frame, 12)[0]
        name = fourcc_str(subtype).strip()
        self._count("asyn-" + name)

        if subtype == FEED:
            self.last_feed_at = time.monotonic()
            try:
                sb = SampleBuffer.parse(frame[16:], MEDIA_VIDEO)
            except (ProtocolError, struct.error) as e:
                log.error("bad video sample: %s", e)
                sb = None
            if sb is not None:
                if sb.format and self.on_video_format:
                    self.on_video_format(sb.format)
                if self.on_video:
                    self.on_video(sb)
            # Ask for the next frame; the phone stops sending video without this.
            if self.need_message:
                self.write(self.need_message)
        elif subtype == EAT:
            try:
                sb = SampleBuffer.parse(frame[16:], MEDIA_SOUND)
            except (ProtocolError, struct.error) as e:
                log.error("bad audio sample: %s", e)
                return
            if sb.output_pts and self.local_audio_clock:
                now = self.local_audio_clock.get_time()
                if SKEW_GUARD and self._audio_last_local is not None and self._audio_gap(now, sb.output_pts):
                    self._reset_audio_timing()
                if self._audio_start_dev is None:
                    self._audio_start_dev = sb.output_pts
                    self._audio_start_local = now
                self._audio_last_dev = sb.output_pts
                self._audio_last_local = now
            if self.on_audio:
                self.on_audio(sb)
        elif subtype == RELS:
            self.releases += 1
            self.release_event.set()
            log.info("RELS received (%d)", self.releases)
            if not self.host_stopping and not self.device_released:
                self.device_released = True
                log.warning("iPhone 自行結束擷取（RELS，clockRef %x）", struct.unpack_from("<Q", frame, 4)[0])
        elif subtype in (SPRP, TJMP, SRAT, TBAS):
            log.debug("ASYN %s", name)
        else:
            log.warning("unknown ASYN subtype %s: %s", name, frame[:48].hex())

    def _reset_audio_timing(self) -> None:
        self._audio_start_dev = self._audio_start_local = None
        self._audio_last_dev = self._audio_last_local = None

    def _audio_gap(self, now: CMTime, pts: CMTime) -> bool:
        """True if arrival time and sample time disagree by > 0.5 s since the last packet."""
        local = (now.value - self._audio_last_local.value) / now.scale
        dev = pts.seconds() - self._audio_last_dev.seconds() if pts.scale and self._audio_last_dev.scale else local
        return abs(local - dev) > 0.5

    def device_ended(self, grace: float = 3.0) -> bool:
        """True once the phone has ended the session on its own: it released the stream, or it
        stopped the video stream and sent no picture for `grace` seconds afterwards."""
        if self.device_released:
            return True
        stop_at = self.video_stop_at
        if stop_at is None or time.monotonic() - stop_at < grace:
            return False
        return self.last_feed_at is None or self.last_feed_at < stop_at

    def stop_packets(self) -> list[bytes]:
        """Packets that ask the phone to stop streaming. Keep reading afterwards to see RELS."""
        self.host_stopping = True
        return [asyn_hpa0(self.device_audio_clock_ref), asyn_hpd0()]
