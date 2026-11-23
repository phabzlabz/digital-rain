"""macOS side of osapi, used by the .saver (mac_saver.py). Import `osapi`, not
this module.

The module must import on Windows too (tools/check.py checks its names), so
AppKit and CoreGraphics are only loaded inside the functions that need them.

What each name does on Mac:

    real          hdr_info, settings_read_path, state_dir, LATIN_FONT,
                  CJK_FONTS, list_monitors, max_refresh_rate, on_battery,
                  client_size, is_window, log, log_path, font_files
    harmless      make_dpi_aware (Retina is handled by the view's backing store),
    no-ops        acquire_single_instance (legacyScreenSaver runs one process and
                  makes one view per display itself), create_event, event_is_set,
                  signal_event, close_event (named events only serve the Windows
                  settings window's preview)
    stubs         gpu_load_sampler raises RuntimeError, which live_load reports
                  as "unavailable" (no public GPU counter; maybe IOKit later).
                  Windows-only window plumbing raises NotImplementedError:
                  embed_in_parent, set_topmost, hide_window (the saver never reparents or
                  places windows; macOS does it), and the settings-dialog helpers
                  set_dark_title_bar, capture_window, capture_client_rgb,
                  work_area (the Tk settings window is Windows only; the Mac
                  options sheet is mac_options.py).

"Window" arguments (hwnd on Windows) are NSView objects here.

Coordinates: like Windows, monitors and the cursor are in pixels with the
origin at the top left of the primary display and y growing downwards. macOS
works in points from the bottom left, so points are flipped and multiplied by
each display's backing scale (2.0 on Retina). With displays of mixed scales the
pixel layout is approximate; nothing in the saver depends on it being exact.

Logging: inside legacyScreenSaver NSLog output never reaches the system log,
but syslog does. log() sends every line to syslog and appends it to log_path(),
a plain file under state_dir().
"""

import ctypes
import glob
import os
import sys
import time

from digital_rain.osapi.common import APP_NAME, clamp_dpi, dedupe_ids, valid_refresh_rate


def _real_home():
    """The user's real home. Inside the sandbox HOME (and so "~") points into
    ~/Library/Containers/com.apple.ScreenSaver.Engine.legacyScreenSaver/Data,
    so it comes from the password database instead. mac_saver logs both homes
    at start-up."""
    import pwd  # Unix only: osapi.mac must still import on Windows

    return pwd.getpwuid(os.getuid()).pw_dir


def settings_read_path():
    """The legacy settings file. The saver reads it only when ScreenSaverDefaults
    hold no saved settings (mac_settings.resolve); nothing writes it any more."""
    return os.path.join(_real_home(), "Library", "Application Support", APP_NAME, "settings.json")


def state_dir():
    """The saver's own writable folder (logs, runtime state): "~" inside the
    sandbox, i.e. the legacyScreenSaver container. Run unsandboxed (the host
    runner, tools/dev/mac_host.py) it is the real ~/Library/Application Support/MatrixRain."""
    return os.path.join(os.path.expanduser("~"), "Library", "Application Support", APP_NAME)


_LOG_MAX_BYTES = 1_000_000  # the file is cut back to empty past this size


def log_path():
    return os.path.join(state_dir(), "logs", "saver.log")


def log(msg):
    """One diagnostic line to syslog and the log file. Never raises."""
    text = str(msg)
    try:
        import syslog  # Unix only

        for chunk in text.splitlines() or [""]:
            syslog.syslog(syslog.LOG_ERR, f"{APP_NAME}: {chunk}")
    except Exception:
        pass
    try:
        path = log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "a"
        try:
            if os.path.getsize(path) > _LOG_MAX_BYTES:
                mode = "w"
        except OSError:
            pass
        with open(path, mode, encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [pid {os.getpid()}] {text}\n")
    except Exception:
        pass


LATIN_FONT = "/System/Library/Fonts/Supplemental/Courier New Bold.ttf"


def _cjk_fonts():
    """Hiragino Sans, lightest weight first: glyphs.py synthesises bold on the
    Japanese font (as it does for MS Gothic on Windows), so the W3 weight plus
    synthetic bold comes closest to the Windows look. Any other Hiragino face is a fallback."""
    base = "/System/Library/Fonts"
    named = [os.path.join(base, f"ヒラギノ角ゴシック {w}.ttc") for w in ("W3", "W4", "W5", "W6")]
    others = sorted(
        set(
            glob.glob(os.path.join(base, "Hiragino*.tt[cf]"))
            + glob.glob(os.path.join(base, "ヒラギノ*.tt[cf]"))
        )
        - set(named)
    )
    return named + others


CJK_FONTS = _cjk_fonts() if sys.platform == "darwin" else []


def make_dpi_aware():
    """No-op: an NSOpenGLView with best-resolution surfaces draws in pixels already."""


_cg = None


def _coregraphics():
    """CoreGraphics through ctypes, for the display identity numbers (these
    calls take and return plain uint32s, so no Quartz bindings are needed)."""
    global _cg
    if _cg is None:
        lib = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        for fn in ("CGDisplayVendorNumber", "CGDisplayModelNumber", "CGDisplaySerialNumber"):
            getattr(lib, fn).argtypes = [ctypes.c_uint32]
            getattr(lib, fn).restype = ctypes.c_uint32
        _cg = lib
    return _cg


def _display_id(screen):
    """The CGDirectDisplayID of an NSScreen (changes when a display is replugged)."""
    num = screen.deviceDescription().get("NSScreenNumber")
    return int(num) if num is not None else 0


def _stable_id(cgid):
    """Vendor, model and serial: survives replugging and restarts, like the
    device interface path used as the id on Windows."""
    try:
        cg = _coregraphics()
        v, m, s = (
            cg.CGDisplayVendorNumber(cgid),
            cg.CGDisplayModelNumber(cgid),
            cg.CGDisplaySerialNumber(cgid),
        )
        if v or m or s:
            return f"cg:{v:x}-{m:x}-{s:x}"
    except Exception:
        pass
    return f"cgid:{cgid}"


def list_monitors():
    """[{x, y, w, h, dpi, id, name, cgid}] in pixels, origin at the top left of
    the primary display; primary first. dpi is the backing scale (2.0 on
    Retina), matching Windows' effective DPI / 96. cgid is the display's
    CGDirectDisplayID, which mac_saver uses to find its own entry."""
    from AppKit import NSScreen

    screens = list(NSScreen.screens() or [])
    if not screens:
        return [
            dict(x=0, y=0, w=1920, h=1080, dpi=1.0, id="cgid:0", name="Unknown display", cgid=0)
        ]
    top = screens[0].frame().size.height
    out = []
    for scr in screens:
        f = scr.frame()
        k = float(scr.backingScaleFactor() or 1.0)
        cgid = _display_id(scr)
        try:
            name = str(scr.localizedName())
        except Exception:
            name = "Display"
        out.append(
            dict(
                x=int(round(f.origin.x * k)),
                y=int(round((top - f.origin.y - f.size.height) * k)),
                w=int(round(f.size.width * k)),
                h=int(round(f.size.height * k)),
                dpi=clamp_dpi(k),
                id=_stable_id(cgid),
                name=name,
                cgid=cgid,
            )
        )
    return dedupe_ids(out)


def max_refresh_rate(default=60):
    """The fastest display's refresh rate (ProMotion panels report 120)."""
    try:
        from AppKit import NSScreen

        best = max(int(s.maximumFramesPerSecond()) for s in NSScreen.screens())
    except Exception:
        return default
    return valid_refresh_rate(best, default)


def hdr_info(view=None):
    """None, or {"white": 1.0, "sdr_nits": 100.0, "max_headroom": EDR limit} when the screen
    can show extended dynamic range (EDR: XDR and HDR displays). `view` is the saver view:
    its own screen decides; with no window yet, the best attached screen. Output
    is extended linear sRGB, where 1.0 is SDR white. Never raises."""
    try:
        from AppKit import NSScreen

        screen = None
        try:
            window = view.window() if view is not None else None
            screen = window.screen() if window is not None else None
        except Exception:
            screen = None
        screens = [screen] if screen is not None else list(NSScreen.screens())
        best = max(
            float(s.maximumPotentialExtendedDynamicRangeColorComponentValue()) for s in screens
        )
    except Exception:
        return None
    return {"white": 1.0, "sdr_nits": 100.0, "max_headroom": best} if best > 1.05 else None


def on_battery():
    try:
        import psutil  # optional dependency

        bat = psutil.sensors_battery()
    except Exception:
        return False
    return bat is not None and bat.power_plugged is False


def gpu_load_sampler():
    """No public GPU-load counter on macOS; live_load treats this as unavailable."""
    raise RuntimeError("GPU load is not available on macOS")


def acquire_single_instance(_name):
    """Always True: legacyScreenSaver owns the process and its views."""
    return True


def create_event(_name):
    return None


def event_is_set(_handle):
    return False


def signal_event(_name):
    return False


def close_event(_handle):
    pass


def is_window(view):
    return view is not None and view.window() is not None


def client_size(view):
    """The view's size in backing pixels."""
    px = view.convertRectToBacking_(view.bounds()).size
    return max(1, int(round(px.width))), max(1, int(round(px.height)))


def _windows_only(name, why):
    def stub(*_args, **_kwargs):
        raise NotImplementedError(f"osapi.mac.{name}: {why}")

    stub.__name__ = name
    return stub


for _name in (
    "embed_in_parent",
    "set_topmost",
    "hide_window",
):
    globals()[_name] = _windows_only(_name, "Windows-only window plumbing")
for _name in ("set_dark_title_bar", "capture_window", "capture_client_rgb", "work_area"):
    globals()[_name] = _windows_only(
        _name, "Windows settings window helper; the Mac options sheet does not use it"
    )
del _name


def font_files():
    """Installed fonts: {face name: file path}, named after the file ("Courier New
    Bold.ttf" -> "Courier New Bold") in the system, local and user font folders."""
    out = {}
    for folder in (
        os.path.expanduser("~/Library/Fonts"),
        "/Library/Fonts",
        "/System/Library/Fonts/Supplemental",
        "/System/Library/Fonts",
    ):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for fn in names:
            stem, ext = os.path.splitext(fn)
            if ext.lower() in (".ttf", ".otf", ".ttc"):
                out.setdefault(stem, os.path.join(folder, fn))
    return out
