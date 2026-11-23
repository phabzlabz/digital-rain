"""Platform layer: everything that talks to the operating system.

Callers `import osapi` and use the names in INTERFACE; they never import a
platform module directly. The module is picked once, by sys.platform:

    windows.py  Win32 through ctypes (the .scr build)
    mac.py      macOS (the .saver build, mac_saver.py); its docstring says
                      which names are real, harmless no-ops or Windows-only stubs
    common.py   pure-Python helpers shared by both

Settings file: settings_read_path() is the saver's file, and on Windows the
dialog writes the same one (%APPDATA%\\MatrixRain\\settings.json).
On Mac the saver runs sandboxed inside legacyScreenSaver: it can read the real
~/Library/Application Support/MatrixRain/settings.json but not write there, so
anything the saver itself writes goes under state_dir() (its container).
"""

import sys

from digital_rain.osapi.common import monitor_signature, virtual_bounds  # noqa: F401

# Every platform module defines all of these (tools/check.py checks it).
INTERFACE = (
    # files, fonts and diagnostics
    "settings_read_path",
    "state_dir",
    "LATIN_FONT",
    "CJK_FONTS",
    "font_files",
    "log",
    "log_path",
    # monitors, power, timing
    "make_dpi_aware",
    "list_monitors",
    "max_refresh_rate",
    "on_battery",
    "gpu_load_sampler",
    "hdr_info",
    # single instance and cross-process stop signal
    "acquire_single_instance",
    "create_event",
    "event_is_set",
    "signal_event",
    "close_event",
    # windows: preview embedding, full screen
    "is_window",
    "client_size",
    "embed_in_parent",
    "set_topmost",
    "hide_window",
    # settings dialog
    "set_dark_title_bar",
    "capture_window",
    "capture_client_rgb",
    "work_area",
)

if sys.platform == "win32":
    from digital_rain.osapi import windows as _impl
elif sys.platform == "darwin":
    from digital_rain.osapi import mac as _impl
else:
    raise ImportError(f"osapi: unsupported platform {sys.platform}")

PLATFORM = _impl.__name__.rsplit(".", 1)[-1]  # "windows" or "mac"
for _name in INTERFACE:
    globals()[_name] = getattr(_impl, _name)
del _name
