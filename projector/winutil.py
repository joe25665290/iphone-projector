"""Small Windows helpers (ctypes only). Each one fails soft on other systems or errors."""

from __future__ import annotations

import ctypes
import logging
import threading
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("projector.winutil")


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> "GUID":
        g = cls()
        hr = ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
        if hr != 0:
            raise OSError(f"CLSIDFromString failed: {hr:#x}")
        return g


def pictures_dir() -> Path:
    """The user's Pictures folder (follows OneDrive / moved folders)."""
    try:
        folder_id = GUID.parse("{33E28130-4E1E-4676-835A-98395C3BC3BB}")  # FOLDERID_Pictures
        path = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None,
                                                      ctypes.byref(path)) == 0:
            try:
                return Path(path.value)
            finally:
                ctypes.windll.ole32.CoTaskMemFree(path)
    except Exception:  # noqa: BLE001
        log.debug("SHGetKnownFolderPath failed", exc_info=True)
    return Path.home() / "Pictures"


def point_on_screen(x: int, y: int) -> bool:
    """False when no monitor contains this point (e.g. the monitor it was on is unplugged)."""
    try:
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.restype = ctypes.c_void_p
        user32.MonitorFromPoint.argtypes = [POINT, ctypes.c_uint]
        return bool(user32.MonitorFromPoint(POINT(x, y), 0))  # MONITOR_DEFAULTTONULL
    except Exception:  # noqa: BLE001
        return True


# -- default playback device ----------------------------------------------------------

def _com_method(obj: ctypes.c_void_p, index: int, *argtypes):
    """Function at slot `index` of a COM object's vtable."""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    return proto(vtbl[index])


def _release(obj: ctypes.c_void_p) -> None:
    if obj:
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(
            ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents[2])(obj)


class DefaultOutputWatcher:
    """Calls on_change() (from its own thread) when the Windows default playback device changes."""

    INTERVAL = 1.5

    def __init__(self, on_change: Callable[[], None]):
        self.on_change = on_change
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="audio-device-watch", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        enum = ctypes.c_void_p()
        try:
            ole32 = ctypes.windll.ole32
            ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
            clsid = GUID.parse("{BCDE0395-E52F-467C-8E3D-C4579291692E}")  # MMDeviceEnumerator
            iid = GUID.parse("{A95664D2-9614-4F35-A746-DE8DB63617E6}")    # IMMDeviceEnumerator
            hr = ole32.CoCreateInstance(ctypes.byref(clsid), None, 0x17,  # CLSCTX_ALL
                                        ctypes.byref(iid), ctypes.byref(enum))
            if hr != 0 or not enum:
                raise OSError(f"CoCreateInstance failed: {hr & 0xFFFFFFFF:#x}")
        except Exception:  # noqa: BLE001
            log.info("無法偵測 Windows 預設聲音裝置的變更（按 D 可手動切換）", exc_info=True)
            return
        last = self._default_id(enum)
        while not self._stop.wait(self.INTERVAL):
            cur = self._default_id(enum)
            if cur and cur != last:
                log.info("Windows 預設聲音裝置已變更")
                try:
                    self.on_change()
                except Exception:  # noqa: BLE001
                    log.exception("切換聲音裝置失敗")
            if cur:
                last = cur
        _release(enum)

    @staticmethod
    def _default_id(enum: ctypes.c_void_p) -> Optional[str]:
        device = ctypes.c_void_p()
        # IMMDeviceEnumerator::GetDefaultAudioEndpoint(eRender, eConsole, &device)
        hr = _com_method(enum, 4, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
            enum, 0, 0, ctypes.byref(device))
        if hr != 0 or not device:
            return None  # no playback device at all
        try:
            text = ctypes.c_wchar_p()
            # IMMDevice::GetId(&id)
            hr = _com_method(device, 5, ctypes.POINTER(ctypes.c_wchar_p))(device, ctypes.byref(text))
            if hr != 0:
                return None
            try:
                return text.value
            finally:
                ctypes.windll.ole32.CoTaskMemFree(text)
        finally:
            _release(device)
