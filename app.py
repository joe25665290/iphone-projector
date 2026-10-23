"""Projector: show the iPhone's screen (and play its audio) in a window over USB.

    python app.py                       # run until the window is closed
    python app.py --seconds 20 --screenshot out\\app.png   # self-test: quit after 20 s of video

One process owns both the window and the audio output, which is what Discord's window share
and OBS's Application Audio Capture need. Keep the window un-minimized while sharing.
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import sys
import threading
import time
from pathlib import Path

from projector import capture as cap
from projector import settings
from projector import usb_transport as ut
from projector.audio import MAX_VOLUME, AudioPlayer, list_output_devices
from projector.video import VideoDecoder, fit_size

TITLE = "Projector - iPhone"
FONT_PATH = r"C:\Windows\Fonts\msjh.ttc"  # Microsoft JhengHei: has Traditional Chinese glyphs
DEFAULT_SRC = (1180, 2556)

STATUS_TEXT = {
    cap.WAITING: "等待 iPhone 連接…（USB 線接上並解鎖）",
    cap.CONNECTING: "連線中…",
    cap.STREAMING: "",
    cap.STOPPING: "正在結束…",
    cap.STUCK: "iPhone 沒有回應，請重開 iPhone 後再接上",
    cap.DISCONNECTED: "iPhone 已中斷連線",
    cap.ERROR: "發生錯誤：",
    cap.STOPPED: "已停止",
}

log = logging.getLogger("app")


def setup_logging(path: Path, verbose: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    logfile = logging.FileHandler(path, "w", encoding="utf-8")
    logfile.setLevel(logging.DEBUG)
    logfile.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(console)
    root.addHandler(logfile)

    def thread_hook(args):
        log.error("執行緒 %s 發生未處理的錯誤", args.thread.name if args.thread else "?",
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = thread_hook


def set_dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


class ConsoleCloseGuard:
    """When the console window is closed, give the capture a few seconds to stop cleanly."""

    def __init__(self, stop: threading.Event, done: threading.Event):
        self.stop, self.done = stop, done
        handler_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

        def handler(ctrl_type):
            if ctrl_type in (2, 5, 6):  # CTRL_CLOSE_EVENT, LOGOFF, SHUTDOWN
                self.stop.set()
                self.done.wait(4.5)  # Windows kills the process after ~5 s
                return 1
            return 0  # Ctrl+C / Ctrl+Break: let Python raise KeyboardInterrupt

        self._handler = handler_type(handler)  # keep a reference
        try:
            ctypes.windll.kernel32.SetConsoleCtrlHandler(self._handler, 1)
        except Exception:  # noqa: BLE001
            pass


class App:
    def __init__(self, args):
        self.args = args
        self.stop = threading.Event()
        self.engine_done = threading.Event()
        self.status = cap.WAITING
        self.status_detail = ""
        self.decoder = VideoDecoder()
        self.settings = settings.load()
        volume = args.volume / 100 if args.volume is not None else self.settings.get("volume", 1.0)
        self.audio = AudioPlayer(args.audio_device, volume=volume)
        self.volume_shown_until = 0.0
        self.engine = None
        self.streaming_since = None
        self.src_size = DEFAULT_SRC
        self.frame_seq = 0
        self.frame_surface = None
        self.displayed = 0

    # -- capture side ----------------------------------------------------------

    def on_status(self, status: str, detail: str) -> None:
        self.status, self.status_detail = status, detail
        if status == cap.CONNECTING:
            self.decoder.reset()
            self.frame_surface = None
        if status == cap.STREAMING:
            self.streaming_since = time.monotonic()
        elif status != cap.STOPPING:
            self.streaming_since = None

    def run_engine(self, backend) -> None:
        try:
            self.engine = cap.CaptureEngine(
                backend,
                on_video_format=self.decoder.submit_format,
                on_video=self.decoder.submit_sample,
                on_audio_format=self.audio.set_format,
                on_audio=self.audio.push,
                on_status=self.on_status,
                serial=self.args.serial,
            )
            self.engine.run_forever(self.stop)
        except Exception as e:  # noqa: BLE001
            log.exception("擷取執行緒結束")
            self.on_status(cap.ERROR, str(e))
        finally:
            self.engine_done.set()

    # -- window ------------------------------------------------------------------

    def window_size_for(self, src_w: int, src_h: int) -> tuple[int, int]:
        import pygame

        desk_w, desk_h = pygame.display.get_desktop_sizes()[0]
        return fit_size(src_w, src_h, int(desk_w * 0.9), int(desk_h * 0.85))

    def run(self) -> int:
        import pygame

        set_dpi_aware()
        os.environ.setdefault("SDL_HINT_WINDOWS_DPI_AWARENESS", "permonitorv2")
        pygame.display.init()
        pygame.font.init()
        try:
            font = pygame.font.Font(FONT_PATH, 22)
        except Exception:  # noqa: BLE001
            font = pygame.font.Font(None, 28)

        size = self.window_size_for(*self.src_size)
        screen = pygame.display.set_mode(size, pygame.RESIZABLE)
        pygame.display.set_caption(TITLE)
        # No text entry here: turn the input method off so letter keys (M) arrive as key presses
        # instead of being taken by a Chinese IME.
        pygame.key.stop_text_input()
        self.decoder.set_target(*size)
        self.decoder.start()

        backend = ut.get_backend(self.args.libusb0_dll)
        engine_thread = threading.Thread(target=self.run_engine, args=(backend,), name="capture")
        engine_thread.start()
        ConsoleCloseGuard(self.stop, self.engine_done)

        clock = pygame.time.Clock()
        last_stats = time.monotonic()
        last_counts = (0, 0, 0)
        shot_taken = False
        quitting = False
        exit_code = 0

        try:
            while True:
                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        quitting = True
                    elif event.type == pygame.VIDEORESIZE:
                        self.decoder.set_target(event.w, event.h)
                    elif event.type == pygame.MOUSEWHEEL:
                        self._change_volume(0.1 * event.y)
                    elif event.type == pygame.KEYDOWN:
                        if event.key in (pygame.K_UP, pygame.K_EQUALS, pygame.K_PLUS, pygame.K_KP_PLUS):
                            self._change_volume(0.1)
                        elif event.key in (pygame.K_DOWN, pygame.K_MINUS, pygame.K_KP_MINUS):
                            self._change_volume(-0.1)
                        elif event.key == pygame.K_m or event.scancode == pygame.KSCAN_M:
                            muted = self.audio.toggle_mute()
                            log.info("靜音 %s", "開" if muted else "關")
                            self.volume_shown_until = time.monotonic() + 1.5

                # Self-test: quit after N seconds of video.
                if (self.args.seconds and self.streaming_since
                        and time.monotonic() - self.streaming_since >= self.args.seconds):
                    if self.args.screenshot and not shot_taken:
                        pygame.image.save(screen, self.args.screenshot)
                        log.info("截圖存到 %s", self.args.screenshot)
                        shot_taken = True
                    quitting = True

                if quitting and not self.stop.is_set():
                    log.info("關閉中，正在讓 iPhone 正常結束擷取")
                    self.stop.set()
                    self.status = cap.STOPPING
                if self.stop.is_set() and self.engine_done.is_set():
                    break

                frame = self.decoder.take_latest(self.frame_seq)
                if frame is not None:
                    self.frame_seq = frame.seq
                    if (frame.src_width, frame.src_height) != self.src_size:
                        self._orientation_changed(frame, screen)
                        screen = pygame.display.get_surface()
                    self.frame_surface = pygame.image.frombuffer(
                        frame.rgb, (frame.width, frame.height), "RGB")
                    self.displayed += 1

                self._draw(screen, font)
                pygame.display.flip()

                now = time.monotonic()
                if now - last_stats >= 5.0:
                    last_counts = self._log_stats(now - last_stats, last_counts)
                    last_stats = now
                clock.tick(120)
        except KeyboardInterrupt:
            log.info("Ctrl+C，正在讓 iPhone 正常結束擷取")
            self.stop.set()
            self._wait_engine(pygame, screen, font)
        except Exception:  # noqa: BLE001
            log.exception("主視窗發生錯誤，正在讓 iPhone 正常結束擷取")
            self.stop.set()
            self._wait_engine(pygame, screen, font)
            exit_code = 2
        finally:
            self.stop.set()
            engine_thread.join(10.0)
            self.decoder.stop()
            self.audio.close()
            pygame.quit()
        return exit_code

    def _wait_engine(self, pygame, screen, font) -> None:
        self.status = cap.STOPPING
        end = time.monotonic() + 10.0
        while not self.engine_done.is_set() and time.monotonic() < end:
            pygame.event.pump()
            self._draw(screen, font)
            pygame.display.flip()
            time.sleep(0.05)

    def _orientation_changed(self, frame, screen) -> None:
        import pygame

        old = self.src_size
        self.src_size = (frame.src_width, frame.src_height)
        rotated = (old[0] > old[1]) != (frame.src_width > frame.src_height)
        log.info("畫面大小 %dx%d -> %dx%d", old[0], old[1], frame.src_width, frame.src_height)
        if rotated:
            size = self.window_size_for(*self.src_size)
            pygame.display.set_mode(size, pygame.RESIZABLE)
            self.decoder.set_target(*size)

    def _change_volume(self, delta: float) -> None:
        if not delta:
            return
        volume = self.audio.set_volume(self.audio.volume + delta)
        self.volume_shown_until = time.monotonic() + 1.5
        self.settings["volume"] = volume
        settings.save(self.settings)
        log.info("音量 %d%%", round(volume * 100))

    def _draw_volume(self, screen, font) -> None:
        import pygame

        if time.monotonic() > self.volume_shown_until:
            return
        a = self.audio
        pct = round(a.volume * 100)
        label = "靜音" if a.muted or pct == 0 else f"音量 {pct}%"
        sw = screen.get_width()
        bar_w, bar_h = min(260, sw - 40), 8
        text = font.render(label, True, (240, 240, 240))
        box_w, box_h = max(bar_w, text.get_width()) + 32, text.get_height() + bar_h + 30
        box = pygame.Surface((box_w, box_h), pygame.SRCALPHA)
        box.fill((0, 0, 0, 170))
        x0, y0 = (sw - box_w) // 2, 24
        screen.blit(box, (x0, y0))
        screen.blit(text, ((sw - text.get_width()) // 2, y0 + 10))
        bx, by = (sw - bar_w) // 2, y0 + 16 + text.get_height()
        pygame.draw.rect(screen, (90, 90, 90), (bx, by, bar_w, bar_h), border_radius=4)
        fill = 0 if a.muted else int(bar_w * a.volume / MAX_VOLUME)
        color = (230, 160, 60) if a.volume > 1.0 else (90, 200, 120)
        if fill:
            pygame.draw.rect(screen, color, (bx, by, fill, bar_h), border_radius=4)
        mid = bx + bar_w // 2  # 100% mark
        pygame.draw.line(screen, (220, 220, 220), (mid, by - 3), (mid, by + bar_h + 2), 1)

    def _draw(self, screen, font) -> None:
        screen.fill((0, 0, 0))
        sw, sh = screen.get_size()
        if self.frame_surface is not None and self.status in (cap.STREAMING, cap.STOPPING):
            fw, fh = self.frame_surface.get_size()
            screen.blit(self.frame_surface, ((sw - fw) // 2, (sh - fh) // 2))
        text = STATUS_TEXT.get(self.status, self.status)
        if self.status == cap.ERROR:
            text += self.status_detail
        elif self.status == cap.STREAMING and self.frame_surface is None:
            text = "等待畫面…"
        if text:
            self._draw_text(screen, font, text, sw, sh)
        self._draw_volume(screen, font)

    @staticmethod
    def _draw_text(screen, font, text, sw, sh) -> None:
        import pygame

        lines = []
        for para in text.split("\n"):
            line = ""
            for ch in para:  # wrap by width; Chinese has no spaces
                if font.size(line + ch)[0] > sw - 40 and line:
                    lines.append(line)
                    line = ch
                else:
                    line += ch
            lines.append(line)
        total_h = len(lines) * (font.get_linesize() + 4)
        y = (sh - total_h) // 2
        for line in lines:
            surf = font.render(line, True, (230, 230, 230))
            bg = pygame.Surface((surf.get_width() + 16, surf.get_height() + 4), pygame.SRCALPHA)
            bg.fill((0, 0, 0, 160))
            x = (sw - surf.get_width()) // 2
            screen.blit(bg, (x - 8, y - 2))
            screen.blit(surf, (x, y))
            y += font.get_linesize() + 4

    def _log_stats(self, dt: float, last: tuple[int, int, int]) -> tuple[int, int, int]:
        d, a = self.decoder, self.audio
        rx, dec, shown = d.received, d.decoded, self.displayed
        ms = (d.decode_time / d.decoded * 1000) if d.decoded else 0.0
        log.info(
            "狀態 %s｜收到 %.0f fps｜解碼 %.0f fps（平均 %.1f ms）｜顯示 %.0f fps｜待解碼 %d｜解碼錯誤 %d"
            "｜聲音緩衝 %.0f ms + 裝置 %.0f ms｜峰值 %d｜斷音 %d｜丟棄 %d",
            self.status, (rx - last[0]) / dt, (dec - last[1]) / dt, ms, (shown - last[2]) / dt,
            d.backlog(), d.errors, a.buffered_ms(), a.output_latency_ms(), a.take_peak(),
            a.underruns, a.trims,
        )
        return rx, dec, shown


def main() -> int:
    ap = argparse.ArgumentParser(description="Projector：用 USB 把 iPhone 畫面和聲音投到電腦")
    ap.add_argument("--seconds", type=int, default=0, help="自我測試：收到畫面 N 秒後自動結束")
    ap.add_argument("--screenshot", default="", help="自我測試結束前把視窗存成圖片")
    ap.add_argument("--volume", type=int, default=None, help="起始音量 0–200（%%；預設沿用上次的設定）")
    ap.add_argument("--audio-device", default=None, help="聲音輸出裝置（名稱的一部分或編號；預設為系統預設裝置）")
    ap.add_argument("--list-audio-devices", action="store_true", help="列出可用的聲音輸出裝置")
    ap.add_argument("--serial", default="", help="指定 iPhone 的 USB 序號（多支手機時用）")
    ap.add_argument("--log", default="out/app.log", help="紀錄檔路徑（預設 out/app.log）")
    ap.add_argument("--libusb0-dll", default=None, help="libusb0.dll 的路徑（找不到時才需要）")
    ap.add_argument("-v", "--verbose", action="store_true", help="在主控台顯示除錯訊息")
    args = ap.parse_args()

    if args.list_audio_devices:
        for i, name in list_output_devices():
            print(f"{i:3d}  {name}")
        return 0
    setup_logging(Path(args.log), args.verbose)
    try:
        return App(args).run()
    except ut.UsbSetupError as e:
        log.error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
