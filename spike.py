"""Feasibility test: can this PC pull the iPhone's screen and audio over USB?

    python spike.py --list          # only show what USB sees (safe, changes nothing)
    python spike.py --reset         # USB-reset the phone, then list
    python spike.py                 # record 30 s into out/  (video.h264, audio.wav, spike.log)
    python spike.py --seconds 10

Needs the iPhone's whole-device driver switched to libusb-win32 (libusb0) with Zadig.
See README.md for the exact steps and how to undo them.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

from projector import capture as cap
from projector import usb_transport as ut
from projector.sinks import H264Writer, Stats, WavWriter

log = logging.getLogger("spike")


def setup_logging(out: Path, verbose: bool, name: str = "spike.log") -> None:
    out.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    logfile = logging.FileHandler(out / name, "w", encoding="utf-8")
    logfile.setLevel(logging.DEBUG)
    logfile.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(console)
    root.addHandler(logfile)


def list_devices(backend) -> int:
    phones = ut.find_iphones(backend)
    if not phones:
        log.error("libusb0 看不到任何 iPhone。請確認已接上 USB、已解鎖，而且 Zadig 換的是整台裝置。")
        return 1
    for dev in phones:
        log.info("\n%s", ut.describe(dev))
    return 0


def report(stats: Stats, result: cap.SessionResult, out: Path) -> bool:
    counts = result.counts
    mp = result.processor
    feeds = counts.get("asyn-feed", 0)
    eats = counts.get("asyn-eat!", 0)
    vs = stats.video_seconds()
    log.info("========== 結果 ==========")
    log.info("結束狀態: %s %s", result.status, result.error)
    if mp:
        log.info("收到 ping: %s，握手完成: %s", mp.ping_received, mp.handshake_done)
    log.info("訊息統計: %s", dict(sorted(counts.items())))
    log.info(
        "畫面: 收到 %d 個封包，編碼 %s，%dx%d，寫出 %d 張（關鍵影格 %d 張，缺少 H.264 設定而略過 %d 張）",
        feeds, stats.codec or "?", stats.width, stats.height,
        stats.video_samples, stats.keyframes, stats.samples_without_avcc,
    )
    if vs > 0:
        log.info("  約 %.1f fps，%.1f Mbps", stats.video_samples / vs, stats.video_bytes * 8 / vs / 1e6)
    log.info(
        "聲音: 收到 %d 個封包，%.0f Hz，%d 聲道，%d bit，共 %.1f 秒（實際經過 %.1f 秒）",
        eats, stats.audio_rate, stats.audio_channels, stats.audio_bits, stats.audio_seconds(), result.elapsed,
    )
    ok_video = feeds > 0
    ok_audio = stats.audio_bytes > 0
    log.info("畫面 %s，聲音 %s", "OK" if ok_video else "沒收到", "OK" if ok_audio else "沒收到")
    if ok_video and stats.video_samples == 0:
        log.warning("有收到畫面封包但不是可寫出的 H.264（編碼 %s），需要調整解析", stats.codec or "?")
    log.info("檔案在 %s", out.resolve())
    return ok_video and ok_audio


def capture(args, out: Path, backend) -> int:
    stats = Stats()
    h264 = H264Writer(out / "video.h264", stats)
    wav = WavWriter(out / "audio.wav", stats)
    engine = cap.CaptureEngine(
        backend,
        on_video_format=h264.on_format,
        on_video=h264.on_sample,
        on_audio_format=wav.on_format,
        on_audio=wav.on_sample,
        serial=args.serial,
        trust_first=args.trust_first,
        mux_level=cap.MUX_LEVELS.index(args.mux),
        clear_halt=args.clear_halt,
    )
    stop = threading.Event()
    result_box: dict = {}

    def worker():
        result_box["r"] = engine.run_once(stop, deadline=time.monotonic() + args.seconds + 10)

    # Run the session in a thread so Ctrl+C here can still trigger a clean stop.
    t = threading.Thread(target=worker, name="capture")
    t.start()
    log.info("錄 %d 秒（Ctrl+C 可提早結束）", args.seconds)
    try:
        streaming_since = None
        while t.is_alive():
            t.join(0.2)
            if engine.status == cap.STREAMING and streaming_since is None:
                streaming_since = time.monotonic()
            if streaming_since and time.monotonic() - streaming_since >= args.seconds:
                stop.set()
    except KeyboardInterrupt:
        log.info("使用者中斷，收尾中...")
        stop.set()
        t.join()
    finally:
        h264.close()
        wav.close()

    result = result_box.get("r") or cap.SessionResult(status=cap.ERROR, error="沒有結果")
    ok = report(stats, result, out)
    if result.status in (cap.ERROR, cap.STUCK, cap.DISCONNECTED):
        return 2
    return 0 if ok else 3


def main() -> int:
    ap = argparse.ArgumentParser(description="iPhone USB 畫面/聲音擷取可行性測試")
    ap.add_argument("--list", action="store_true", help="只列出 USB 看到的 iPhone 與其設定")
    ap.add_argument("--reset", action="store_true", help="重設 iPhone 的 USB 連線（不用拔插）後列出設定")
    ap.add_argument("--seconds", type=int, default=30, help="開始收到畫面後錄幾秒（預設 30）")
    ap.add_argument("--out", default="out", help="輸出資料夾（預設 out）")
    ap.add_argument("--serial", default="", help="指定 iPhone 的 USB 序號（多支手機時用）")
    ap.add_argument(
        "--no-trust-first", dest="trust_first", action="store_false",
        help="不要先在一般設定建立受信任連線（預設會先建立，再切換到擷取模式）",
    )
    ap.add_argument("--clear-halt", action="store_true", help="開啟端點後先清除 halt（qvh 的做法）")
    ap.add_argument(
        "--mux", choices=cap.MUX_LEVELS, default="session",
        help="在 usbmux 上做到哪一步：off / version / lockdown / session（預設，完整受信任連線）",
    )
    ap.add_argument("--no-skew-guard", action="store_true", help="（除錯）聲音時鐘 SKEW 回答改用 qvh 原本的算法")
    ap.add_argument("--libusb0-dll", default=None, help="libusb0.dll 的路徑（找不到時才需要）")
    ap.add_argument("-v", "--verbose", action="store_true", help="在畫面上顯示除錯訊息")
    args = ap.parse_args()

    out = Path(args.out)
    setup_logging(out, args.verbose)
    if args.no_skew_guard:
        from projector import protocol

        protocol.SKEW_GUARD = False
        log.info("SKEW 保護已關閉（qvh 原本的算法）")
    try:
        backend = ut.get_backend(args.libusb0_dll)
        if args.reset:
            dev = ut.find_by_serial(backend, args.serial)
            if dev is None:
                log.error("找不到 iPhone")
                return 1
            ut.CaptureSession(backend, dev).reset_device()
            return list_devices(backend)
        if args.list:
            return list_devices(backend)
        if ut.find_by_serial(backend, args.serial) is None:
            log.error("找不到 iPhone（libusb0 看不到）。先執行 python spike.py --list 檢查。")
            return 1
        return capture(args, out, backend)
    except ut.UsbSetupError as e:
        log.error("%s", e)
        return 1
    except Exception:  # noqa: BLE001
        log.exception("未預期的錯誤（請把 out\\spike.log 給我看）")
        return 2


if __name__ == "__main__":
    sys.exit(main())
