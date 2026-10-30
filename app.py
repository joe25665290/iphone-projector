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
import warnings
from datetime import datetime
from pathlib import Path

from projector import capture as cap
from projector import settings
from projector import usb_transport as ut
from projector import winutil
from projector.audio import MAX_VOLUME, AudioPlayer, list_output_devices
from projector.video import SCALE_FAST, SCALE_SHARP, VideoDecoder, fit_size

TITLE = "Projector - iPhone"
FONT_PATH = r"C:\Windows\Fonts\msjh.ttc"  # Microsoft JhengHei: has Traditional Chinese glyphs
DEFAULT_SRC = (1180, 2556)

HELP_LINES = [
    ("上／下、+／-", "音量 ±10%"),
    ("滾輪、Shift + 上／下", "音量 ±1%"),
    ("M", "靜音"),
    ("V", "畫面開／關"),
    ("T", "視窗置頂"),
    ("S", "截圖（存到「圖片\\Projector」）"),
    ("D", "聲音改用 Windows 目前的預設裝置"),
    ("Q", "畫質：清晰／快速"),
    ("R", "出錯後重新連線"),
    ("H、F1", "顯示／隱藏這個說明"),
]

STATUS_TEXT = {
    cap.WAITING: "等待 iPhone 連接…（USB 線接上並解鎖）",
    cap.CONNECTING: "連線中…",
    cap.STREAMING: "",
    cap.STOPPING: "正在結束…",
    cap.STUCK: "iPhone 沒有回應，請重開 iPhone 後再接上",
    cap.ENDED: ("iPhone 已停止螢幕鏡像，要重開 iPhone 才能再投影。\n"
                "下次想暫停請按 V，想結束請直接關閉這個視窗"),
    cap.DISCONNECTED: "iPhone 已中斷連線",
    cap.ERROR: "發生錯誤：",
    cap.STOPPED: "已停止",
}

log = logging.getLogger("app")


def setup_logging(path: Path, verbose: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    console = None
    if sys.stdout is not None:  # pythonw.exe (no console) has no stdout
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.DEBUG if verbose else logging.INFO)
        console.setFormatter(fmt)
    logfile = logging.FileHandler(path, "w", encoding="utf-8")
    logfile.setLevel(logging.DEBUG)
    logfile.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    if console is not None:
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


def keep_full_speed_in_background() -> None:
    """Stop Windows from slowing Projector down while another app (Discord, OBS) is in front.

    Windows 11 applies "EcoQoS" power throttling to background processes and, on hybrid CPUs
    like the i7-12700H, prefers to run them on the efficiency cores. Live video can't afford
    that, so opt out and raise the priority a little.
    """
    try:
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE  # pseudo-handle -1, must stay 64-bit
        k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.SetProcessInformation.restype = wintypes.BOOL
        k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.SetPriorityClass.restype = wintypes.BOOL

        class PowerThrottlingState(ctypes.Structure):
            _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong),
                        ("StateMask", ctypes.c_ulong)]

        PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
        ProcessPowerThrottling = 4
        state = PowerThrottlingState(1, PROCESS_POWER_THROTTLING_EXECUTION_SPEED, 0)
        ok = k32.SetProcessInformation(k32.GetCurrentProcess(), ProcessPowerThrottling,
                                       ctypes.byref(state), ctypes.sizeof(state))
        ABOVE_NORMAL_PRIORITY_CLASS = 0x8000
        ok2 = k32.SetPriorityClass(k32.GetCurrentProcess(), ABOVE_NORMAL_PRIORITY_CLASS)
        log.info("背景降速：%s，優先權：%s", "已關閉" if ok else "無法關閉", "高於標準" if ok2 else "未變更")
    except Exception:  # noqa: BLE001
        log.debug("could not change power throttling / priority", exc_info=True)


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
        self.video_on = True
        self.toast_text = ""
        self.toast_until = 0.0
        self.help_shown = False
        self.on_top = bool(self.settings.get("always_on_top", False))
        self.decoder.scale_method = SCALE_FAST if self.settings.get("scale") == "fast" else SCALE_SHARP
        self.window = None  # pygame.Window of the display, for position and always-on-top
        self.device_watch = None
        self.dirty = True  # window needs a redraw
        self.engine = None
        self.streaming_since = None
        self.src_size = DEFAULT_SRC
        self.frame_seq = 0
        self.frame_surface = None
        self.displayed = 0
        self.pipe_ms_max = 0.0   # USB arrival -> drawn, per stats period
        self.pipe_ms_sum = 0.0
        self.pipe_n = 0
        self._last_decode_time = 0.0

    # -- capture side ----------------------------------------------------------

    def on_status(self, status: str, detail: str) -> None:
        self.status, self.status_detail = status, detail
        self.dirty = True
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
        saved = self.settings.get(self._size_key(src_w, src_h))
        if isinstance(saved, list) and len(saved) == 2:
            w, h = int(saved[0]), int(saved[1])
            if 100 <= w <= desk_w * 2 and 100 <= h <= desk_h * 2:
                return w, h
        return fit_size(src_w, src_h, int(desk_w * 0.9), int(desk_h * 0.85))

    @staticmethod
    def _size_key(src_w: int, src_h: int) -> str:
        return "window_size_landscape" if src_w > src_h else "window_size_portrait"

    def toast(self, text: str, seconds: float = 2.5) -> None:
        """Short message at the bottom of the window (safe to call from any thread)."""
        self.toast_text = text
        self.toast_until = time.monotonic() + seconds
        self.dirty = True

    def run(self) -> int:
        import pygame

        set_dpi_aware()
        keep_full_speed_in_background()
        os.environ.setdefault("SDL_HINT_WINDOWS_DPI_AWARENESS", "permonitorv2")
        pygame.display.init()
        pygame.font.init()
        try:
            font = pygame.font.Font(FONT_PATH, 22)
        except Exception:  # noqa: BLE001
            font = pygame.font.Font(None, 28)

        size = self.window_size_for(*self.src_size)
        pos = self.settings.get("window_pos")
        if isinstance(pos, list) and len(pos) == 2 and winutil.point_on_screen(pos[0] + 40, pos[1] + 10):
            os.environ["SDL_VIDEO_WINDOW_POS"] = f"{int(pos[0])},{int(pos[1])}"
        screen = pygame.display.set_mode(size, pygame.RESIZABLE)
        pygame.display.set_caption(TITLE)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                self.window = pygame.Window.from_display_module()
        except Exception:  # noqa: BLE001
            log.debug("no pygame.Window", exc_info=True)
        if self.on_top:
            self._apply_on_top()
        if not self.args.audio_device:
            self.device_watch = winutil.DefaultOutputWatcher(self._follow_default_output)
            self.device_watch.start()
        # No text entry here: turn the input method off so letter keys (M) arrive as key presses
        # instead of being taken by a Chinese IME.
        pygame.key.stop_text_input()
        self.decoder.set_target(*size)
        self.decoder.start()

        backend = ut.get_backend(self.args.libusb0_dll)
        engine_thread = threading.Thread(target=self.run_engine, args=(backend,), name="capture")
        engine_thread.start()
        ConsoleCloseGuard(self.stop, self.engine_done)

        last_draw = 0.0
        last_stats = time.monotonic()
        last_counts = (0, 0, 0)
        shot_taken = False
        quitting = False
        exit_code = 0

        try:
            while True:
                for event in self._pump_events(pygame):
                    if event.type == pygame.QUIT:
                        quitting = True
                    elif event.type == pygame.VIDEORESIZE:
                        self.decoder.set_target(event.w, event.h)
                        self.settings[self._size_key(*self.src_size)] = [event.w, event.h]
                        self.dirty = True
                    elif event.type in (pygame.WINDOWEXPOSED, pygame.WINDOWRESTORED,
                                        pygame.WINDOWSIZECHANGED):
                        self.dirty = True
                    elif event.type == pygame.MOUSEWHEEL:
                        self._change_volume(0.01 * event.y)
                    elif event.type == pygame.KEYDOWN:
                        # Shift + key: fine steps of 1%; plain key: 10%
                        step = 0.01 if event.mod & pygame.KMOD_SHIFT else 0.1
                        if event.key in (pygame.K_UP, pygame.K_EQUALS, pygame.K_PLUS, pygame.K_KP_PLUS):
                            self._change_volume(step)
                        elif event.key in (pygame.K_DOWN, pygame.K_MINUS, pygame.K_KP_MINUS):
                            self._change_volume(-step)
                        elif event.key == pygame.K_m or event.scancode == pygame.KSCAN_M:
                            muted = self.audio.toggle_mute()
                            log.info("靜音 %s", "開" if muted else "關")
                            self.volume_shown_until = time.monotonic() + 1.5
                            self.dirty = True
                        elif event.key == pygame.K_v or event.scancode == pygame.KSCAN_V:
                            self._toggle_video()
                        elif event.key == pygame.K_t or event.scancode == pygame.KSCAN_T:
                            self._toggle_on_top()
                        elif event.key == pygame.K_s or event.scancode == pygame.KSCAN_S:
                            self._screenshot()
                        elif event.key == pygame.K_d or event.scancode == pygame.KSCAN_D:
                            self._reopen_audio()
                        elif event.key == pygame.K_q or event.scancode == pygame.KSCAN_Q:
                            self._toggle_scale()
                        elif event.key == pygame.K_r or event.scancode == pygame.KSCAN_R:
                            self._reconnect()
                        elif event.key in (pygame.K_h, pygame.K_F1) or event.scancode == pygame.KSCAN_H:
                            self.help_shown = not self.help_shown
                            self.dirty = True

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
                    self.dirty = True
                if self.stop.is_set() and self.engine_done.is_set():
                    break

                frame = self.decoder.take_latest(self.frame_seq)
                if frame is not None and not self.video_on:
                    self.frame_seq = frame.seq  # decoded just before it was turned off: ignore
                    frame = None
                if frame is not None:
                    self.frame_seq = frame.seq
                    if (frame.src_width, frame.src_height) != self.src_size:
                        self._orientation_changed(frame, screen)
                        screen = pygame.display.get_surface()
                    self.frame_surface = pygame.image.frombuffer(
                        frame.rgb, (frame.width, frame.height), "RGB")
                    self.displayed += 1
                    if frame.t_arrival:
                        ms = (time.perf_counter() - frame.t_arrival) * 1000
                        self.pipe_ms_max = max(self.pipe_ms_max, ms)
                        self.pipe_ms_sum += ms
                        self.pipe_n += 1
                    self.dirty = True

                # Redraw only when something changed. Redrawing the full window 120 times a
                # second held the GIL and starved the decoder once Discord was also capturing it.
                now = time.monotonic()
                overlay = now <= max(self.volume_shown_until, self.toast_until) + 0.1
                if self.dirty or overlay or now - last_draw >= 0.25:
                    self._draw(screen, font)
                    pygame.display.flip()
                    self.dirty = False
                    last_draw = now

                if now - last_stats >= 5.0:
                    last_counts = self._log_stats(now - last_stats, last_counts)
                    last_stats = now
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
            self._save_window()
            if self.device_watch is not None:
                self.device_watch.stop()
            self.stop.set()
            engine_thread.join(10.0)
            self.decoder.stop()
            self.audio.close()
            pygame.quit()
        return exit_code

    @staticmethod
    def _pump_events(pygame) -> list:
        """Collect window events without stalling the other threads.

        While the user drags or resizes the window, Windows runs its own message loop inside
        SDL's event pump until the mouse is released. pygame.event.get() pumps while holding
        the GIL, which froze USB reading, decoding and audio for the whole drag.
        event.wait() pumps with the GIL released, so only drawing pauses during a drag.
        """
        first = pygame.event.wait(4)  # also the idle wait of the main loop (GIL released)
        events = [] if first.type == pygame.NOEVENT else [first]
        events.extend(pygame.event.get(pump=False))
        return events

    def _wait_engine(self, pygame, screen, font) -> None:
        self.status = cap.STOPPING
        end = time.monotonic() + 10.0
        while not self.engine_done.is_set() and time.monotonic() < end:
            self._pump_events(pygame)
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
            if self.on_top:
                self._apply_on_top()

    # -- keys ------------------------------------------------------------------------

    def _apply_on_top(self) -> None:
        try:
            self.window.always_on_top = self.on_top
        except Exception:  # noqa: BLE001
            log.warning("無法設定視窗置頂", exc_info=True)

    def _toggle_on_top(self) -> None:
        self.on_top = not self.on_top
        self._apply_on_top()
        self.settings["always_on_top"] = self.on_top
        settings.save(self.settings)
        self.toast("視窗置頂：開" if self.on_top else "視窗置頂：關")

    def _screenshot(self) -> None:
        import pygame

        rgb = self.decoder.snapshot() if self.video_on else None
        if rgb is None:
            self.toast("沒有畫面可以截圖")
            return
        folder = winutil.pictures_dir() / "Projector"
        path = folder / f"iPhone_{datetime.now():%Y%m%d_%H%M%S}.png"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            h, w = rgb.shape[:2]
            pygame.image.save(pygame.image.frombuffer(rgb, (w, h), "RGB"), str(path))
        except Exception as e:  # noqa: BLE001
            log.exception("截圖失敗")
            self.toast(f"截圖失敗：{e}")
            return
        log.info("截圖存到 %s", path)
        self.toast(f"已截圖：{path.name}（{w}×{h}）")

    def _reopen_audio(self) -> None:
        try:
            name = self.audio.reopen()
        except Exception as e:  # noqa: BLE001
            log.exception("切換聲音裝置失敗")
            self.toast(f"切換聲音裝置失敗：{e}")
            return
        log.info("聲音輸出改為 %s", name)
        self.toast(f"聲音輸出：{name}")

    def _follow_default_output(self) -> None:
        """Called from the device watcher thread when Windows' default playback device changes."""
        self._reopen_audio()

    def _reconnect(self) -> None:
        if self.engine is None or self.status != cap.ERROR:
            return
        log.info("使用者要求重新連線")
        self.engine.resume.set()
        self.toast("重新連線中…")

    def _toggle_scale(self) -> None:
        sharp = self.decoder.scale_method != SCALE_SHARP
        self.decoder.scale_method = SCALE_SHARP if sharp else SCALE_FAST
        self.settings["scale"] = "sharp" if sharp else "fast"
        settings.save(self.settings)
        self.toast("畫質：清晰" if sharp else "畫質：快速（比較省 CPU）")

    def _save_window(self) -> None:
        try:
            if self.window is not None:
                self.settings["window_pos"] = list(self.window.position)
        except Exception:  # noqa: BLE001
            log.debug("could not read window position", exc_info=True)
        settings.save(self.settings)

    def _toggle_video(self) -> None:
        self.video_on = not self.video_on
        self.decoder.set_enabled(self.video_on)
        self.frame_surface = None  # don't show a stale picture when it comes back
        self.dirty = True
        log.info("畫面 %s", "開" if self.video_on else "關（聲音照常）")

    def _change_volume(self, delta: float) -> None:
        if not delta:
            return
        volume = self.audio.set_volume(self.audio.volume + delta)
        self.volume_shown_until = time.monotonic() + 1.5
        self.dirty = True
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
        elif self.status == cap.STREAMING and not self.video_on:
            text = "畫面已關閉，聲音照常播放（按 V 開啟）"
        elif self.status == cap.STREAMING and self.frame_surface is None:
            text = "等待畫面…"
        if text and not self.help_shown:
            self._draw_text(screen, font, text, sw, sh)
        self._draw_volume(screen, font)
        if time.monotonic() <= self.toast_until:
            self._draw_toast(screen, font, self.toast_text)
        if self.help_shown:
            self._draw_help(screen, font)

    @staticmethod
    def _draw_toast(screen, font, text) -> None:
        import pygame

        sw, sh = screen.get_size()
        surf = font.render(text, True, (240, 240, 240))
        if surf.get_width() > sw - 32:  # narrow window: shrink rather than cut off
            scale = (sw - 32) / surf.get_width()
            surf = pygame.transform.smoothscale(
                surf, (sw - 32, max(1, int(surf.get_height() * scale))))
        box = pygame.Surface((surf.get_width() + 24, surf.get_height() + 12), pygame.SRCALPHA)
        box.fill((0, 0, 0, 180))
        x, y = (sw - box.get_width()) // 2, sh - box.get_height() - 24
        screen.blit(box, (x, y))
        screen.blit(surf, (x + 12, y + 6))

    @staticmethod
    def _draw_help(screen, font) -> None:
        import pygame

        sw, sh = screen.get_size()
        keys = [font.render(k, True, (255, 210, 120)) for k, _ in HELP_LINES]
        descs = [font.render(d, True, (235, 235, 235)) for _, d in HELP_LINES]
        key_w = max(s.get_width() for s in keys)
        line_h = font.get_linesize() + 6
        w = key_w + 24 + max(s.get_width() for s in descs) + 40
        h = line_h * len(HELP_LINES) + 32
        panel = pygame.Surface((w, h), pygame.SRCALPHA)
        panel.fill((0, 0, 0, 200))
        for i, (k, d) in enumerate(zip(keys, descs)):
            y = 16 + i * line_h
            panel.blit(k, (20, y))
            panel.blit(d, (20 + key_w + 24, y))
        if w > sw - 16 or h > sh - 16:  # small window: shrink the whole panel to fit
            scale = min((sw - 16) / w, (sh - 16) / h)
            panel = pygame.transform.smoothscale(panel, (max(1, int(w * scale)), max(1, int(h * scale))))
        screen.blit(panel, ((sw - panel.get_width()) // 2, (sh - panel.get_height()) // 2))

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
        e = self.engine
        if e is not None:
            log.info(
                "延遲｜到達落後 +%.0f ms｜到達→顯示 平均 %.0f／最長 %.0f ms｜解碼排隊最長 %.0f ms"
                "｜解碼忙碌 %.0f%%｜跳過 %d 張（%d 次）｜關鍵影格間隔最長 %.2f s｜USB 讀滿 %d/%d 次",
                d.arrival_drift() * 1000,
                self.pipe_ms_sum / self.pipe_n if self.pipe_n else 0.0, self.pipe_ms_max,
                d.take_queue_wait_max() * 1000,
                (d.decode_time - self._last_decode_time) / dt * 100, d.dropped, d.drop_events,
                d.take_idr_gap_max(), e.usb_full_reads, e.usb_reads,
            )
            self._last_decode_time = d.decode_time
            self.pipe_ms_max = self.pipe_ms_sum = 0.0
            self.pipe_n = 0
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
        show_error(str(e))
        return 1
    except Exception as e:  # noqa: BLE001
        log.exception("Projector 發生未預期的錯誤")
        show_error(f"Projector 發生未預期的錯誤：\n{e}\n\n詳細紀錄在 {args.log}")
        return 2


def show_error(message: str) -> None:
    """Without a console (pythonw), errors would vanish silently; show a dialog instead."""
    if sys.stdout is not None:
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, message, "Projector", 0x10)  # MB_ICONERROR
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    sys.exit(main())
