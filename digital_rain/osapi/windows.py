"""Windows side of osapi: thin ctypes wrappers around the Win32 bits a
screensaver needs. Import `osapi`, not this module."""

import ctypes
import os
import sys
from ctypes import wintypes

from digital_rain.osapi.common import APP_NAME, clamp_dpi, dedupe_ids, valid_refresh_rate

# One handle per DLL. The kernel32 and user32 handles keep the last error so
# that acquire_single_instance can read it.
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32")
shcore = ctypes.WinDLL("shcore")
dwmapi = ctypes.WinDLL("dwmapi")

GWL_STYLE = -16
WS_POPUP, WS_CHILD, WS_VISIBLE = 0x80000000, 0x40000000, 0x10000000
HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0010, 0x0040
SW_HIDE = 0
ERROR_ALREADY_EXISTS = 183
EDD_GET_DEVICE_INTERFACE_NAME = 0x1
DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x1
ENUM_CURRENT_SETTINGS = -1
EVENT_MODIFY_STATE = 0x0002
MONITOR_DEFAULTTONEAREST = 2
MDT_EFFECTIVE_DPI = 0
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
PW_CLIENTONLY, PW_RENDERFULLCONTENT = 0x1, 0x2
PROCESS_DPI_PER_MONITOR_AWARE = 2
QDC_ONLY_ACTIVE_PATHS = 2


class _MonitorInfoEx(ctypes.Structure):  # MONITORINFOEXW
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class _PowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


class _DevMode(ctypes.Structure):
    """DEVMODEW, display-device flavour."""

    _fields_ = [
        ("dmDeviceName", wintypes.WCHAR * 32),
        ("dmSpecVersion", wintypes.WORD),
        ("dmDriverVersion", wintypes.WORD),
        ("dmSize", wintypes.WORD),
        ("dmDriverExtra", wintypes.WORD),
        ("dmFields", wintypes.DWORD),
        ("dmPositionX", wintypes.LONG),
        ("dmPositionY", wintypes.LONG),
        ("dmDisplayOrientation", wintypes.DWORD),
        ("dmDisplayFixedOutput", wintypes.DWORD),
        ("dmColor", wintypes.SHORT),
        ("dmDuplex", wintypes.SHORT),
        ("dmYResolution", wintypes.SHORT),
        ("dmTTOption", wintypes.SHORT),
        ("dmCollate", wintypes.SHORT),
        ("dmFormName", wintypes.WCHAR * 32),
        ("dmLogPixels", wintypes.WORD),
        ("dmBitsPerPel", wintypes.DWORD),
        ("dmPelsWidth", wintypes.DWORD),
        ("dmPelsHeight", wintypes.DWORD),
        ("dmDisplayFlags", wintypes.DWORD),
        ("dmDisplayFrequency", wintypes.DWORD),
        ("dmICMMethod", wintypes.DWORD),
        ("dmICMIntent", wintypes.DWORD),
        ("dmMediaType", wintypes.DWORD),
        ("dmDitherType", wintypes.DWORD),
        ("dmReserved1", wintypes.DWORD),
        ("dmReserved2", wintypes.DWORD),
        ("dmPanningWidth", wintypes.DWORD),
        ("dmPanningHeight", wintypes.DWORD),
    ]


class _DisplayDevice(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("DeviceName", wintypes.WCHAR * 32),
        ("DeviceString", wintypes.WCHAR * 128),
        ("StateFlags", wintypes.DWORD),
        ("DeviceID", wintypes.WCHAR * 128),
        ("DeviceKey", wintypes.WCHAR * 128),
    ]


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


# Display configuration structures, for hdr_info.
class _ConfigHeader(ctypes.Structure):  # DISPLAYCONFIG_DEVICE_INFO_HEADER
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("size", ctypes.c_uint32),
        ("luid_lo", ctypes.c_uint32),
        ("luid_hi", ctypes.c_int32),
        ("id", ctypes.c_uint32),
    ]


class _AdvancedColorInfo(ctypes.Structure):  # DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO
    _fields_ = [
        ("header", _ConfigHeader),
        ("value", ctypes.c_uint32),
        ("encoding", ctypes.c_uint32),
        ("bits", ctypes.c_uint32),
    ]


class _SdrWhiteLevel(ctypes.Structure):  # DISPLAYCONFIG_SDR_WHITE_LEVEL
    _fields_ = [("header", _ConfigHeader), ("level", ctypes.c_uint32)]


class _ConfigPathInfo(ctypes.Structure):
    """DISPLAYCONFIG_PATH_INFO (72 bytes): only the target's adapter and id are read."""

    _fields_ = [
        ("source", ctypes.c_ubyte * 20),
        ("target_luid_lo", ctypes.c_uint32),
        ("target_luid_hi", ctypes.c_int32),
        ("target_id", ctypes.c_uint32),
        ("rest", ctypes.c_ubyte * 40),
    ]


CONFIG_MODE_INFO_SIZE = 64  # sizeof(DISPLAYCONFIG_MODE_INFO)
CONFIG_TYPE_ADVANCED_COLOR_INFO, CONFIG_TYPE_SDR_WHITE_LEVEL = 9, 11
ADVANCED_COLOR_ENABLED = 0x2
SDR_WHITE_DEFAULT_NITS = 80.0

# Callback prototypes, built once: list_monitors is polled for hot-plugs.
_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
)

_c_int, _c_uint = ctypes.c_int, ctypes.c_uint
_rect_ptr = ctypes.POINTER(wintypes.RECT)

# name -> (argtypes, restype) for every Win32 call made below, so no call
# relies on ctypes' default int conversion (which truncates handles).
_SIGNATURES = {
    user32: {
        "SetParent": ([wintypes.HWND, wintypes.HWND], wintypes.HWND),
        "ShowWindow": ([wintypes.HWND, _c_int], wintypes.BOOL),
        "SetWindowPos": (
            [wintypes.HWND, wintypes.HWND, _c_int, _c_int, _c_int, _c_int, wintypes.UINT],
            wintypes.BOOL,
        ),
        "GetWindowLongPtrW": ([wintypes.HWND, _c_int], ctypes.c_ssize_t),
        "SetWindowLongPtrW": ([wintypes.HWND, _c_int, ctypes.c_ssize_t], ctypes.c_ssize_t),
        "IsWindow": ([wintypes.HWND], wintypes.BOOL),
        "GetClientRect": ([wintypes.HWND, _rect_ptr], wintypes.BOOL),
        "GetWindowRect": ([wintypes.HWND, _rect_ptr], wintypes.BOOL),
        "GetMonitorInfoW": ([wintypes.HANDLE, ctypes.POINTER(_MonitorInfoEx)], wintypes.BOOL),
        "MonitorFromWindow": ([wintypes.HWND, wintypes.DWORD], wintypes.HANDLE),
        "EnumDisplayMonitors": (
            [wintypes.HDC, _rect_ptr, _MONITORENUMPROC, wintypes.LPARAM],
            wintypes.BOOL,
        ),
        "EnumDisplayDevicesW": (
            [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_DisplayDevice), wintypes.DWORD],
            wintypes.BOOL,
        ),
        "EnumDisplaySettingsW": (
            [wintypes.LPCWSTR, _c_int, ctypes.POINTER(_DevMode)],
            wintypes.BOOL,
        ),
        "SetProcessDPIAware": ([], wintypes.BOOL),
        "GetDC": ([wintypes.HWND], wintypes.HDC),
        "ReleaseDC": ([wintypes.HWND, wintypes.HDC], _c_int),
        "PrintWindow": ([wintypes.HWND, wintypes.HDC, wintypes.UINT], wintypes.BOOL),
        "GetDisplayConfigBufferSizes": (
            [wintypes.UINT, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)],
            ctypes.c_long,
        ),
        "QueryDisplayConfig": (
            [
                wintypes.UINT,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_void_p,
                ctypes.c_void_p,
            ],
            ctypes.c_long,
        ),
        "DisplayConfigGetDeviceInfo": ([ctypes.c_void_p], ctypes.c_long),
    },
    kernel32: {
        "GetSystemPowerStatus": ([ctypes.POINTER(_PowerStatus)], wintypes.BOOL),
        "CreateMutexW": ([ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        "CreateEventW": (
            [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR],
            wintypes.HANDLE,
        ),
        "OpenEventW": ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetEvent": ([wintypes.HANDLE], wintypes.BOOL),
        "ResetEvent": ([wintypes.HANDLE], wintypes.BOOL),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
    },
    gdi32: {
        "CreateCompatibleDC": ([wintypes.HDC], wintypes.HDC),
        "CreateDIBSection": (
            [
                wintypes.HDC,
                ctypes.c_void_p,
                wintypes.UINT,
                ctypes.POINTER(ctypes.c_void_p),
                wintypes.HANDLE,
                wintypes.DWORD,
            ],
            wintypes.HBITMAP,
        ),
        "SelectObject": ([wintypes.HDC, wintypes.HGDIOBJ], wintypes.HGDIOBJ),
        "DeleteObject": ([wintypes.HGDIOBJ], wintypes.BOOL),
        "DeleteDC": ([wintypes.HDC], wintypes.BOOL),
    },
    shcore: {
        "SetProcessDpiAwareness": ([_c_int], ctypes.c_long),
        "GetDpiForMonitor": (
            [wintypes.HANDLE, _c_int, ctypes.POINTER(_c_uint), ctypes.POINTER(_c_uint)],
            ctypes.c_long,
        ),
    },
    dwmapi: {
        "DwmSetWindowAttribute": (
            [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD],
            _c_int,
        ),
    },
}


def _declare_signatures():
    for dll, table in _SIGNATURES.items():
        for name, (argtypes, restype) in table.items():
            func = getattr(dll, name)
            func.argtypes = argtypes
            func.restype = restype


_declare_signatures()


def make_dpi_aware():
    try:
        shcore.SetProcessDpiAwareness(PROCESS_DPI_PER_MONITOR_AWARE)
    except AttributeError, OSError:
        user32.SetProcessDPIAware()


def list_monitors():
    """[{x, y, w, h, dpi, id, name}] in virtual-desktop pixels; primary first.

    id is the monitor's device interface path (stable across layout, resolution,
    scaling and restarts), or "gdi:<device>" when Windows gives none."""
    out = []

    def on_monitor(hmon, _dc, rect, _lparam):
        r = rect.contents
        dpi_x, dpi_y = ctypes.c_uint(96), ctypes.c_uint(96)
        shcore.GetDpiForMonitor(hmon, MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
        mon_id, name = "gdi:unknown", "Unknown display"
        info = _MonitorInfoEx()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            dev = _DisplayDevice()
            dev.cb = ctypes.sizeof(dev)
            got = user32.EnumDisplayDevicesW(
                info.szDevice, 0, ctypes.byref(dev), EDD_GET_DEVICE_INTERFACE_NAME
            )
            if got and dev.DeviceID:
                mon_id = dev.DeviceID.lower()
            else:
                mon_id = "gdi:" + info.szDevice.lower()
            name = (got and dev.DeviceString) or info.szDevice or name
        out.append(
            dict(
                x=r.left,
                y=r.top,
                w=r.right - r.left,
                h=r.bottom - r.top,
                dpi=clamp_dpi(dpi_x.value / 96.0),
                id=mon_id,
                name=name,
            )
        )
        return True

    user32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(on_monitor), 0)
    out.sort(key=lambda m: m["x"] != 0 or m["y"] != 0)  # primary at (0,0)
    return dedupe_ids(out)


def on_battery():
    status = _PowerStatus()
    if kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return status.ACLineStatus == 0
    return False


def max_refresh_rate(default=60):
    """Highest current refresh rate (Hz) among the attached displays."""
    best, i = 0, 0
    while True:
        dev = _DisplayDevice()
        dev.cb = ctypes.sizeof(dev)
        if not user32.EnumDisplayDevicesW(None, i, ctypes.byref(dev), 0):
            break
        i += 1
        if not dev.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP:
            continue
        mode = _DevMode()
        mode.dmSize = ctypes.sizeof(mode)
        if user32.EnumDisplaySettingsW(dev.DeviceName, ENUM_CURRENT_SETTINGS, ctypes.byref(mode)):
            best = max(best, mode.dmDisplayFrequency)
    return valid_refresh_rate(best, default)


_mutexes = []


def acquire_single_instance(name):
    """False if another process already holds the named mutex."""
    handle = kernel32.CreateMutexW(None, False, name)
    already_exists = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
    _mutexes.append(handle)  # keep alive for the life of the process
    return not already_exists


def is_window(hwnd):
    return bool(user32.IsWindow(hwnd))


def client_size(hwnd):
    rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    return max(1, rect.right - rect.left), max(1, rect.bottom - rect.top)


def _make_child_style(hwnd):
    style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
    style = (style & ~WS_POPUP) | WS_CHILD | WS_VISIBLE
    user32.SetWindowLongPtrW(hwnd, GWL_STYLE, style)


def embed_in_parent(child, parent, w, h):
    """Turn `child` into a child window of `parent` (screensaver preview pane)."""
    user32.SetParent(child, parent)
    _make_child_style(child)
    user32.SetWindowPos(child, None, 0, 0, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW)


def set_topmost(hwnd, x, y, w, h):
    user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, w, h, SWP_SHOWWINDOW)


def hide_window(hwnd):
    user32.ShowWindow(hwnd, SW_HIDE)


def create_event(name):
    """Manual-reset, non-signalled named event; None when it cannot be created."""
    handle = kernel32.CreateEventW(None, True, False, name)
    if not handle:
        return None
    kernel32.ResetEvent(handle)
    return handle


def event_is_set(handle):
    return bool(handle) and kernel32.WaitForSingleObject(handle, 0) == 0


def close_event(handle):
    """Close a handle returned by create_event."""
    if handle:
        kernel32.CloseHandle(handle)


def signal_event(name):
    handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, name)
    if not handle:
        return False
    ok = bool(kernel32.SetEvent(handle))
    kernel32.CloseHandle(handle)
    return ok


def set_dark_title_bar(hwnd):
    """Ask DWM for the dark title bar; returns the HRESULT."""
    value = ctypes.c_int(1)
    return int(
        dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(value), ctypes.sizeof(value)
        )
    )


def _window_size(hwnd, get_rect):
    """(w, h) from a GetWindowRect or GetClientRect call; RuntimeError when empty."""
    rect = wintypes.RECT()
    get_rect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w < 1 or h < 1:
        raise RuntimeError("window has zero size")
    return w, h


def _print_window_bgra(hwnd, w, h, flags):
    """The window as w*h*4 BGRA bytes: PrintWindow into a top-down 32-bit DIB.
    Raises RuntimeError when the bitmap cannot be made or PrintWindow fails."""
    screen_dc = user32.GetDC(None)
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    header = _BitmapInfoHeader()
    header.biSize = ctypes.sizeof(header)
    header.biWidth, header.biHeight = w, -h  # negative height: top-down
    header.biPlanes, header.biBitCount = 1, 32
    bits = ctypes.c_void_p()
    bmp = gdi32.CreateDIBSection(screen_dc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
    try:
        if not bmp or not bits:
            raise RuntimeError("PrintWindow failed")
        previous = gdi32.SelectObject(mem_dc, bmp)
        ok = user32.PrintWindow(hwnd, mem_dc, flags)
        gdi32.SelectObject(mem_dc, previous)
        if not ok:
            raise RuntimeError("PrintWindow failed")
        return ctypes.string_at(bits.value, w * h * 4)
    finally:
        if bmp:
            gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, screen_dc)


def capture_window(hwnd, path):
    """Save a PNG of the whole top-level window (title bar included); returns (w, h).

    Raises RuntimeError when the window has no size or PrintWindow fails."""
    import numpy as np
    import pygame

    w, h = _window_size(hwnd, user32.GetWindowRect)
    bgra = np.frombuffer(
        _print_window_bgra(hwnd, w, h, PW_RENDERFULLCONTENT), dtype=np.uint8
    ).reshape(h, w, 4)
    rgb = np.ascontiguousarray(bgra[:, :, 2::-1])  # BGRA -> RGB
    pygame.image.save(pygame.image.frombuffer(rgb.tobytes(), (w, h), "RGB"), path)
    return w, h


def work_area(hwnd):
    """-> (x, y, w, h) of the work area (screen minus taskbar) of the monitor
    showing `hwnd`, in physical pixels."""
    hmon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    info = _MonitorInfoEx()
    info.cbSize = ctypes.sizeof(info)
    if not hmon or not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
        raise OSError("GetMonitorInfoW failed")
    r = info.rcWork
    return r.left, r.top, r.right - r.left, r.bottom - r.top


# Byte table that scales a channel to 60%: the paused preview is dimmed by 40%.
_DIM_40 = bytes(round(i * 0.6) for i in range(256))


def capture_client_rgb(hwnd):
    """-> (w, h, rgb bytes) of the client area of `hwnd`, child windows included,
    dimmed by 40% (the settings window's paused preview). No numpy.

    Raises RuntimeError when the window has no size or PrintWindow fails."""
    w, h = _window_size(hwnd, user32.GetClientRect)
    bgra = _print_window_bgra(hwnd, w, h, PW_CLIENTONLY | PW_RENDERFULLCONTENT)
    bgra = bgra.translate(_DIM_40)
    rgb = bytearray(w * h * 3)
    rgb[0::3] = bgra[2::4]
    rgb[1::3] = bgra[1::4]
    rgb[2::3] = bgra[0::4]
    return w, h, bytes(rgb)


def _appdata():
    return os.environ.get("APPDATA") or os.path.expanduser("~")


def settings_read_path():
    """settings.json in %APPDATA%/MatrixRain, for the saver and the dialog alike."""
    return os.path.join(_appdata(), APP_NAME, "settings.json")


def state_dir():
    """Folder for files the saver itself writes (logs, runtime state)."""
    return os.path.join(_appdata(), APP_NAME)


def log(msg):
    """Diagnostics go to stderr on Windows (the Mac saver also needs syslog and a file)."""
    print(msg, file=sys.stderr)


def log_path():
    """None: Windows keeps no log file."""
    return None


LATIN_FONT = r"C:\Windows\Fonts\courbd.ttf"  # Courier New Bold
CJK_FONTS = [
    r"C:\Windows\Fonts\msgothic.ttc",
    r"C:\Windows\Fonts\YuGothM.ttc",
    r"C:\Windows\Fonts\meiryo.ttc",
]


PDH_FMT_DOUBLE = 0x00000200
PDH_MORE_DATA = 0x800007D2
PDH_NO_DATA = 0x800007D5
PDH_VALID = (0, 1)  # PDH_CSTATUS_VALID_DATA, PDH_CSTATUS_NEW_DATA
GPU_COUNTER = r"\GPU Engine(*engtype_3D)\Utilization Percentage"


class _FmtValue(ctypes.Structure):
    _fields_ = [
        ("CStatus", wintypes.DWORD),
        ("pad", wintypes.DWORD),
        ("doubleValue", ctypes.c_double),
    ]


class _FmtItem(ctypes.Structure):
    _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", _FmtValue)]


_pdh = None


def _load_pdh():
    """pdh.dll with its signatures declared, loaded on first use (only the GPU
    load option needs it). Raises OSError when the DLL is missing."""
    global _pdh
    if _pdh is None:
        pdh = ctypes.WinDLL("pdh.dll")
        signatures = {
            "PdhOpenQueryW": [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.c_void_p],
            "PdhAddEnglishCounterW": [
                ctypes.c_void_p,
                wintypes.LPCWSTR,
                ctypes.c_size_t,
                ctypes.c_void_p,
            ],
            "PdhCollectQueryData": [ctypes.c_void_p],
            "PdhGetFormattedCounterArrayW": [
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.DWORD),
                ctypes.c_void_p,
            ],
            "PdhCloseQuery": [ctypes.c_void_p],
        }
        for name, argtypes in signatures.items():
            func = getattr(pdh, name)
            func.argtypes = argtypes
            func.restype = ctypes.c_uint32
        _pdh = pdh
    return _pdh


def gpu_load_sampler():
    """(sample, closer) for the busiest GPU's 3D-engine load, this process
    excluded. sample() returns 0..1, or False for a failed sample. Raises
    RuntimeError when the counter cannot be opened."""
    try:
        pdh = _load_pdh()
    except OSError as exc:
        raise RuntimeError(f"pdh.dll: {exc}") from exc

    query = ctypes.c_void_p()
    status = pdh.PdhOpenQueryW(None, 0, ctypes.byref(query))
    if status != 0:
        raise RuntimeError(f"PdhOpenQueryW 0x{status:08X}")

    def closer():
        pdh.PdhCloseQuery(query)

    counter = ctypes.c_void_p()
    status = pdh.PdhAddEnglishCounterW(query, GPU_COUNTER, 0, ctypes.byref(counter))
    if status != 0:
        closer()
        raise RuntimeError(f"PdhAddEnglishCounterW 0x{status:08X}")
    mine = f"pid_{os.getpid()}_"

    def sample():
        status = pdh.PdhCollectQueryData(query)
        if status == PDH_NO_DATA:
            return 0.0
        if status != 0:
            return False
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        status = pdh.PdhGetFormattedCounterArrayW(
            counter, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), None
        )
        if status == PDH_NO_DATA:
            return 0.0
        if status != PDH_MORE_DATA:
            return False
        buf = ctypes.create_string_buffer(size.value)
        status = pdh.PdhGetFormattedCounterArrayW(
            counter, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), buf
        )
        if status == PDH_NO_DATA:
            return 0.0
        if status != 0:
            return False
        items = ctypes.cast(buf, ctypes.POINTER(_FmtItem))
        per_adapter = {}
        for i in range(count.value):
            item = items[i]
            name = item.szName or ""
            if name.startswith(mine) or item.FmtValue.CStatus not in PDH_VALID:
                continue
            at = name.find("luid_")
            key = name[at:].split("_phys_")[0] if at >= 0 else ""
            per_adapter[key] = per_adapter.get(key, 0.0) + item.FmtValue.doubleValue
        top = max(per_adapter.values(), default=0.0)
        return min(100.0, max(0.0, top)) / 100.0

    return sample, closer


def hdr_info(view=None):
    """None, or {"white": scRGB value of SDR white, "sdr_nits": SDR white in nits}
    (no "max_headroom": DisplayConfig does not report peak luminance, so the renderer
    caps highlights at 8x SDR white; `view` is unused, for parity with the Mac)
    when Windows has HDR ("Use HDR") switched on for at least one active display.
    Never raises."""
    try:
        path_count, mode_count = ctypes.c_uint32(), ctypes.c_uint32()
        if user32.GetDisplayConfigBufferSizes(
            QDC_ONLY_ACTIVE_PATHS, ctypes.byref(path_count), ctypes.byref(mode_count)
        ):
            return None
        paths = (_ConfigPathInfo * path_count.value)()
        modes = (ctypes.c_ubyte * (CONFIG_MODE_INFO_SIZE * mode_count.value))()
        if user32.QueryDisplayConfig(
            QDC_ONLY_ACTIVE_PATHS,
            ctypes.byref(path_count),
            paths,
            ctypes.byref(mode_count),
            modes,
            None,
        ):
            return None
        for path in paths[: path_count.value]:
            ident = (path.target_luid_lo, path.target_luid_hi, path.target_id)
            color = _AdvancedColorInfo(
                _ConfigHeader(
                    CONFIG_TYPE_ADVANCED_COLOR_INFO, ctypes.sizeof(_AdvancedColorInfo), *ident
                )
            )
            if (
                user32.DisplayConfigGetDeviceInfo(ctypes.byref(color))
                or not color.value & ADVANCED_COLOR_ENABLED
            ):
                continue
            white = _SdrWhiteLevel(
                _ConfigHeader(CONFIG_TYPE_SDR_WHITE_LEVEL, ctypes.sizeof(_SdrWhiteLevel), *ident)
            )
            if user32.DisplayConfigGetDeviceInfo(ctypes.byref(white)) == 0 and white.level:
                nits = 80.0 * white.level / 1000.0
            else:
                nits = SDR_WHITE_DEFAULT_NITS
            return {"white": nits / 80.0, "sdr_nits": nits}
    except OSError, AttributeError, ctypes.ArgumentError:
        pass
    return None


def font_files():
    """Installed fonts: {face name: file path} from the Fonts registry keys (machine, then
    user). A name like "Cambria & Cambria Math (TrueType)" gives one entry per face."""
    import winreg

    fonts_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    sub = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
    out = {}
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(hive, sub)
        except OSError:
            continue
        with key:
            i = 0
            while True:
                try:
                    name, val, _kind = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if not isinstance(val, str) or not val:
                    continue
                path = val if os.path.isabs(val) else os.path.join(fonts_dir, val)
                name = name.rsplit(" (", 1)[0]
                for face in name.split(" & "):
                    out.setdefault(face.strip(), path)
    return out
