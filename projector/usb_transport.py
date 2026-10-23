"""USB access to the iPhone's hidden screen-capture interface, through libusb0 (libusb-win32).

Why libusb0: the capture interface only exists in an extra USB configuration that the phone
adds after a vendor request. Switching to it needs a driver that supports SET_CONFIGURATION.
WinUSB refuses non-default configurations, and libusb's UsbDk backend ignores the call,
so the phone's whole-device node has to be bound to libusb0.sys (via Zadig).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("projector.usb")

APPLE_VID = 0x05AC
VENDOR_CLASS = 0xFF
MUX_SUBCLASS = 0xFE
QT_SUBCLASS = 0x2A

# Vendor requests (from quicktime_video_hack / the Windows port)
REQ_QT_MODE = 0x52  # wIndex 2 = enable capture config, 0 = disable
REQ_CHARGE = 0x40  # extra charging current; the Windows port sends it when reads stall

_TIMEOUT_ERRNOS = {110, 116, 138, 10060}


class UsbSetupError(Exception):
    pass


def _import_usb():
    try:
        import usb.backend.libusb0
        import usb.core
        import usb.util
    except ImportError as e:  # pragma: no cover - depends on environment
        raise UsbSetupError("缺少 pyusb。請先在 .venv 裡執行: pip install -r requirements.txt") from e
    return usb


def get_backend(dll_path: Optional[str] = None):
    usb = _import_usb()
    finder = (lambda _name: dll_path) if dll_path else None
    backend = usb.backend.libusb0.get_backend(find_library=finder) if finder else usb.backend.libusb0.get_backend()
    if backend is None:
        raise UsbSetupError(
            "找不到 libusb0.dll。代表 iPhone 的驅動還沒用 Zadig 換成 libusb-win32 (libusb0)。"
        )
    return backend


def create_buffer(size: int):
    """pyusb only reads into array.array buffers; anything else is taken as a length."""
    return _import_usb().util.create_buffer(size)


def is_timeout(err: Exception) -> bool:
    usb = _import_usb()
    if isinstance(err, getattr(usb.core, "USBTimeoutError", ())):
        return True
    # libusb-win32 reports timeouts as -116, but Python's errno.ETIMEDOUT on Windows is 138,
    # so pyusb raises a plain USBError with backend_error_code=-116 and errno=None.
    for code in (getattr(err, "backend_error_code", None), getattr(err, "errno", None)):
        if code is not None and abs(code) in _TIMEOUT_ERRNOS:
            return True
    text = str(err).lower()
    return "timeout" in text or "timed out" in text


@dataclass
class InterfaceInfo:
    config_value: int
    interface_number: int
    alt_setting: int
    ep_in: int
    ep_out: int
    max_packet: int = 512


def find_interface(dev, subclass: int, config_value: Optional[int] = None) -> Optional[InterfaceInfo]:
    for cfg in dev.configurations():
        if config_value is not None and cfg.bConfigurationValue != config_value:
            continue
        for intf in cfg:
            if intf.bInterfaceClass == VENDOR_CLASS and intf.bInterfaceSubClass == subclass:
                ep_in = ep_out = None
                max_packet = 512
                for ep in intf:
                    if (ep.bmAttributes & 0x03) != 0x02:  # bulk only
                        continue
                    max_packet = ep.wMaxPacketSize or max_packet
                    if ep.bEndpointAddress & 0x80:
                        ep_in = ep.bEndpointAddress
                    else:
                        ep_out = ep.bEndpointAddress
                if ep_in is not None and ep_out is not None:
                    return InterfaceInfo(
                        cfg.bConfigurationValue, intf.bInterfaceNumber,
                        intf.bAlternateSetting, ep_in, ep_out, max_packet,
                    )
    return None


def serial_of(dev) -> str:
    usb = _import_usb()
    try:
        return (usb.util.get_string(dev, dev.iSerialNumber) or "").strip("\x00")
    except Exception as e:  # noqa: BLE001 - descriptor reads can fail while the phone re-enumerates
        log.debug("could not read serial: %s", e)
        return ""


def describe(dev) -> str:
    lines = [f"Apple 裝置 VID={dev.idVendor:04x} PID={dev.idProduct:04x} 序號={serial_of(dev) or '?'}"]
    for cfg in dev.configurations():
        parts = []
        for intf in cfg:
            tag = ""
            if intf.bInterfaceClass == VENDOR_CLASS and intf.bInterfaceSubClass == MUX_SUBCLASS:
                tag = " [usbmux]"
            elif intf.bInterfaceClass == VENDOR_CLASS and intf.bInterfaceSubClass == QT_SUBCLASS:
                tag = " [畫面擷取]"
            parts.append(
                f"if{intf.bInterfaceNumber}.{intf.bAlternateSetting} "
                f"class={intf.bInterfaceClass:02x}/{intf.bInterfaceSubClass:02x}{tag}"
            )
        lines.append(f"  設定 {cfg.bConfigurationValue}: " + ", ".join(parts))
    return "\n".join(lines)


def find_iphones(backend) -> list:
    usb = _import_usb()
    devices = usb.core.find(find_all=True, idVendor=APPLE_VID, backend=backend) or []
    return [d for d in devices if find_interface(d, MUX_SUBCLASS) is not None]


def find_by_serial(backend, serial: str):
    for dev in find_iphones(backend):
        if not serial or serial_of(dev) == serial:
            return dev
    return None


class CaptureSession:
    """Owns the USB handle while the capture interface is in use."""

    def __init__(self, backend, dev, clear_halt: bool = False):
        self.backend = backend
        self.dev = dev
        self.serial = serial_of(dev)
        self.clear_halt = clear_halt
        self.qt: Optional[InterfaceInfo] = None
        self.mux: Optional[InterfaceInfo] = find_interface(dev, MUX_SUBCLASS)
        self._claimed = False
        self.cap_mux: Optional[InterfaceInfo] = None

    # -- setup ---------------------------------------------------------------

    def reset_device(self, wait_seconds: float = 10.0) -> bool:
        """USB port reset; makes the phone re-enumerate (same effect as replugging, hopefully)."""
        usb = _import_usb()
        log.info("重設 iPhone 的 USB 連線")
        try:
            self.dev.reset()
        except usb.core.USBError as e:
            log.debug("reset: %s", e)
        usb.util.dispose_resources(self.dev)
        self.qt = None
        self.cap_mux = None
        self._claimed = False
        deadline = time.monotonic() + wait_seconds
        time.sleep(1.0)
        while time.monotonic() < deadline:
            dev = find_by_serial(self.backend, self.serial)
            if dev is not None:
                self.dev = dev
                still_qt = find_interface(dev, QT_SUBCLASS) is not None
                log.info("重設完成，擷取設定%s", "仍在" if still_qt else "已消失")
                return not still_qt
            time.sleep(0.5)
        log.warning("重設後 %d 秒內沒找到 iPhone", wait_seconds)
        return False

    def active_config(self) -> Optional[int]:
        try:
            return self.dev.get_active_configuration().bConfigurationValue
        except Exception as e:  # noqa: BLE001
            log.debug("get_active_configuration: %s", e)
            return None

    def set_config(self, value: int) -> None:
        self.dev.set_configuration(value)
        log.info("已切到設定 %d", value)

    def mux_config_value(self) -> Optional[int]:
        """Highest normal configuration that has a usbmux interface (4 on current iPhones)."""
        best = None
        for cfg in self.dev.configurations():
            v = cfg.bConfigurationValue
            if find_interface(self.dev, MUX_SUBCLASS, v) and not find_interface(self.dev, QT_SUBCLASS, v):
                best = v if best is None else max(best, v)
        return best

    def enable_capture_config(self, wait_seconds: float = 15.0) -> None:
        usb = _import_usb()
        if find_interface(self.dev, QT_SUBCLASS):
            log.info("擷取設定已經存在，不用再切換")
            return
        log.info("送出切換到擷取模式的請求 (0x40/0x52, wIndex=2)，目前設定 %s", self.active_config())
        try:
            self.dev.ctrl_transfer(0x40, REQ_QT_MODE, 0, 2, None, 1000)
        except usb.core.USBError as e:
            # qvh notes this sometimes errors even though the switch works.
            log.warning("切換請求回報錯誤（常見，通常仍會生效）: %s", e)
        usb.util.dispose_resources(self.dev)

        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            time.sleep(0.5)
            dev = find_by_serial(self.backend, self.serial)
            if dev is not None and find_interface(dev, QT_SUBCLASS):
                self.dev = dev
                log.info("iPhone 重新連線並出現擷取設定（目前設定 %s）", self.active_config())
                return
        raise UsbSetupError(
            f"{wait_seconds:.0f} 秒內沒有出現擷取設定。請確認 iPhone 已解鎖，"
            "且 Zadig 換的是整台裝置（不是 Interface 0/1）。"
        )

    def select_capture_config(self) -> None:
        self.qt = find_interface(self.dev, QT_SUBCLASS)
        if self.qt is None:
            raise UsbSetupError("裝置上沒有擷取介面")
        self.dev.set_configuration(self.qt.config_value)
        log.info("已切到設定 %d", self.qt.config_value)

    def claim_mux(self, config_value: Optional[int] = None) -> InterfaceInfo:
        """Claim the usbmux interface of a configuration (default: the capture configuration)."""
        usb = _import_usb()
        if config_value is None:
            config_value = self.qt.config_value
        info = find_interface(self.dev, MUX_SUBCLASS, config_value)
        if info is None:
            raise UsbSetupError("擷取設定裡沒有 usbmux 介面")
        usb.util.claim_interface(self.dev, info.interface_number)
        self.cap_mux = info
        log.info(
            "usbmux 介面 %d，端點 IN=0x%02x OUT=0x%02x（封包 %d bytes）",
            info.interface_number, info.ep_in, info.ep_out, info.max_packet,
        )
        return info

    def release_mux(self) -> None:
        usb = _import_usb()
        if self.cap_mux:
            try:
                usb.util.release_interface(self.dev, self.cap_mux.interface_number)
            except usb.core.USBError as e:
                log.debug("release mux: %s", e)
            self.cap_mux = None

    def mux_write(self, data: bytes) -> None:
        self.dev.write(self.cap_mux.ep_out, data, 2000)

    def mux_read_into(self, buf, timeout_ms: int) -> int:
        usb = _import_usb()
        try:
            return self.dev.read(self.cap_mux.ep_in, buf, timeout_ms)
        except usb.core.USBError as e:
            if is_timeout(e):
                return 0
            raise

    def open(self) -> None:
        """Claim the capture interface (after select_capture_config)."""
        usb = _import_usb()
        if self.qt is None:
            self.select_capture_config()
        log.info(
            "擷取介面 %d，端點 IN=0x%02x OUT=0x%02x",
            self.qt.interface_number, self.qt.ep_in, self.qt.ep_out,
        )
        usb.util.claim_interface(self.dev, self.qt.interface_number)
        self._claimed = True
        if self.clear_halt:
            for ep in (self.qt.ep_in, self.qt.ep_out):
                self.dev.ctrl_transfer(0x02, 0x01, 0, ep, None, 1000)  # CLEAR_FEATURE(ENDPOINT_HALT)

    # -- I/O -----------------------------------------------------------------

    def read_into(self, buf, timeout_ms: int) -> int:
        """Returns bytes read, or 0 on timeout."""
        usb = _import_usb()
        try:
            return self.dev.read(self.qt.ep_in, buf, timeout_ms)
        except usb.core.USBError as e:
            if is_timeout(e):
                return 0
            raise

    def write(self, data: bytes) -> None:
        self.dev.write(self.qt.ep_out, data, 2000)

    def kick(self) -> None:
        """What the Windows port does when the stream stalls before starting."""
        usb = _import_usb()
        try:
            self.dev.ctrl_transfer(0x40, REQ_CHARGE, 0x6400, 0x6400, None, 1000)
        except usb.core.USBError as e:
            log.debug("charge request failed: %s", e)

    # -- teardown ------------------------------------------------------------

    def close(self, restore_normal_mode: bool = True) -> None:
        usb = _import_usb()
        if self._claimed and self.qt:
            try:
                usb.util.release_interface(self.dev, self.qt.interface_number)
            except usb.core.USBError as e:
                log.debug("release_interface: %s", e)
            self._claimed = False
        if self.cap_mux:
            try:
                usb.util.release_interface(self.dev, self.cap_mux.interface_number)
            except usb.core.USBError as e:
                log.debug("release mux interface: %s", e)
            self.cap_mux = None
        if restore_normal_mode:
            try:
                self.dev.ctrl_transfer(0x40, REQ_QT_MODE, 0, 0, None, 1000)
                log.info("已請 iPhone 關閉擷取模式")
            except usb.core.USBError as e:
                log.debug("disable request: %s", e)
            if self.mux:
                try:
                    self.dev.set_configuration(self.mux.config_value)
                except usb.core.USBError as e:
                    log.debug("restore mux config: %s", e)
        usb.util.dispose_resources(self.dev)
