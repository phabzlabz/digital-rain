"""Digital Rain for macOS: the .saver's principal class, MatrixRainView.

The Windows .scr starts at matrix_saver.py; this is the Mac entry point. Both
draw with the same GLRenderer, shaders (assets/shaders, #version 330 core,
which macOS runs on its OpenGL 4.1 core profile) and settings.

How macOS runs it: legacyScreenSaver loads the bundle (py2app),
creates one MatrixRainView per display for the full-screen saver, and one with
isPreview for the System Settings thumbnail, and calls animateOneFrame on a
timer. macOS itself dismisses the saver on any key or mouse input, so there is
no exit gate here.

Each view holds an NSOpenGLView (3.2-core pixel format, which macOS serves as
4.1 core). The renderer is built on the first frame that has a real size: the
thumbnail view starts at 0 x 0, and the window and its screen are only known
then. A size change (a resized preview) rebuilds it.

Superseded views (macOS 27, WallpaperLegacyExtension): the host keeps older
full-screen views animating while it shows a newer one for the same display,
so two full-size renderers would run at once. Window visibility cannot tell them
apart: every view sits in an NSServiceViewControllerWindow whose occlusion
state never has the Visible bit. So only the newest live view per display
draws; older ones tick at IDLE_FPS and free their renderer after
IDLE_RELEASE_SECS. The System Settings thumbnail is one of these full-size
views, scaled down by the host. The isPreview views the host also makes stay
0 x 0 in a 64 x 16 window, are never started and draw nothing.

Settings: read once per view by load_settings(): the JSON blob the options
sheet (mac_options.py, opened by the Options button) saves to
ScreenSaverDefaults, else the old osapi.settings_read_path() file, else
built-in values (mac_settings.resolve). After a Save every view in the process
reloads on its next frame, and other processes on CHANGED_NOTE. The saver's own
files (the log) go under osapi.state_dir().

Diagnostics: every step goes to osapi.log() (syslog plus
<state_dir>/logs/saver.log), because NSLog from legacyScreenSaver never reaches
the system log. Any failure is logged with its traceback and the view goes
black instead of taking the process down.

Test it outside legacyScreenSaver with tools/dev/mac_host.py (see its docstring).
"""

import functools
import json
import os
import platform
import sys
import time
import traceback

try:
    from digital_rain import osapi
except BaseException:
    # osapi.log is not available yet, so write this with the stdlib alone:
    # syslog (shown as <private>) and ~/Library/Logs/MatrixRain-early.log,
    # which inside the sandbox is the legacyScreenSaver container's Logs.
    _msg = (
        f"MatrixRain: mac_saver: import osapi FAILED\nsys.path={sys.path}\n"
        + traceback.format_exc()
    )
    try:
        import syslog

        syslog.syslog(syslog.LOG_ERR, _msg)
    except ImportError, OSError:
        pass
    try:
        _d = os.path.expanduser("~/Library/Logs")
        os.makedirs(_d, exist_ok=True)
        with open(os.path.join(_d, "MatrixRain-early.log"), "a", encoding="utf-8") as _fh:
            _fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + _msg + "\n")
    except OSError:
        pass
    raise

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")  # no banner in the saver's output
osapi.log(
    f"mac_saver: loading, python {sys.version.split()[0]} {platform.machine()}, "
    f"frozen={getattr(sys, 'frozen', False)}"
)

try:
    import AppKit
    import objc
    from AppKit import (
        NSBackingStoreBuffered,
        NSButton,
        NSColor,
        NSMakeRect,
        NSOpenGLPFAAccelerated,
        NSOpenGLPFAColorSize,
        NSOpenGLPFADoubleBuffer,
        NSOpenGLPFAOpenGLProfile,
        NSOpenGLPixelFormat,
        NSOpenGLProfileVersion3_2Core,
        NSOpenGLView,
        NSRectFill,
        NSTextField,
        NSViewHeightSizable,
        NSViewWidthSizable,
        NSWindow,
        NSWindowStyleMaskTitled,
    )
    from Foundation import NSBundle, NSDistributedNotificationCenter, NSObject
    from ScreenSaver import ScreenSaverView
except BaseException:
    osapi.log("mac_saver: PyObjC imports FAILED\n" + traceback.format_exc())
    raise

from digital_rain import settings  # noqa: E402
from digital_rain.mac import mac_options, mac_settings  # noqa: E402

SAVER_BUNDLE_ID = mac_settings.MODULE_NAME  # the .saver's CFBundleIdentifier
PREVIEW_FPS = 30
IDLE_FPS = 4  # tick rate of a superseded full-screen view
SCREEN_CHECK_SECS = 0.25  # how often a view re-reads its window's screen
IDLE_RELEASE_SECS = 30.0  # a view superseded this long frees its renderer
GEOMETRY_LOG_LIMIT = 6  # window and frame-size lines logged per view
BATTERY_FPS = 30
# The animation timer ticks this many times per frame and a tick draws only once a frame's time
# has passed since the last drawn frame. flushBuffer waits for the GPU, so a frame that runs past
# its slot made a plain 1/fps timer skip a whole tick: the saver settled at half the frame rate
# with the GPU idle a third of the time, too idle to clock up. With finer ticks the next frame
# starts at once after a slow one.
TIMER_SUBSTEPS = 8
POWER_CHECK_SECS = 5.0
HEARTBEAT_SECS = 10.0
STOP_NOTE = "com.apple.screensaver.willstop"
NOTES = (STOP_NOTE, "com.apple.screensaver.didstart", "com.apple.screensaver.didstop")
SWAP_INTERVAL_PARAM = 222  # NSOpenGLContextParameterSwapInterval
NS_OPENGL_PFA_COLOR_FLOAT = 58  # NSOpenGLPFAColorFloat, for PyObjC builds that lack the name

# Test hooks for tools/dev/mac_host.py only; the real saver leaves them alone.
settings_file = None  # a JSON file to read instead of the saved settings
cfg_overrides = {}  # keys laid over the loaded settings (e.g. {"preset": "film_1999"})

_views = 0
_started = False
_full_views = []  # every full-screen _Saver in this process, oldest first
_all_savers = []  # every _Saver in this process (reloaded after a Save)


def _logged(what):
    """Decorator for the entry points macOS calls into. A Python exception that
    escaped into AppKit would take the screen saver host down, so it is logged
    with its traceback instead and the call returns None."""

    def wrap(fn):
        @functools.wraps(fn)
        def run(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception:
                tag = args[0].tag() if args and hasattr(args[0], "tag") else "mac_saver"
                osapi.log(f"mac_saver: {tag}: {what} FAILED\n" + traceback.format_exc())
                return None

        return run

    return wrap


@_logged("environment report")
def _environment_report():
    """One-off start-up lines: confirms the sandbox paths on every run."""
    # Local imports: pwd is Unix-only (the checks import this module on Windows),
    # and glyphs pulls in pygame, which the saver loads only when it first draws.
    import pwd

    from digital_rain import glyphs, resources

    osapi.log(
        f"mac_saver: home={os.path.expanduser('~')} real_home={pwd.getpwuid(os.getuid()).pw_dir}"
    )
    path = osapi.settings_read_path()
    osapi.log(
        f"mac_saver: settings {path} exists={os.path.exists(path)} "
        f"readable={os.access(path, os.R_OK)}"
    )
    osapi.log(f"mac_saver: state_dir={osapi.state_dir()} log={osapi.log_path()}")
    osapi.log(
        f"mac_saver: assets={resources.asset_path()} exists={os.path.isdir(resources.asset_path())}"
    )
    osapi.log(
        f"mac_saver: latin font {glyphs.LATIN_FONT} "
        f"exists={os.path.exists(glyphs.LATIN_FONT)}; "
        f"cjk fonts found: {[p for p in glyphs.JP_FONTS if os.path.exists(p)][:2]}"
    )
    mons = osapi.list_monitors()
    osapi.log(
        "mac_saver: monitors "
        + "; ".join(
            f"{m['name']} {m['w']}x{m['h']} at {m['x']},{m['y']} scale {m['dpi']} id {m['id']}"
            for m in mons
        )
        + f"; max refresh {osapi.max_refresh_rate()} Hz, "
        f"on battery {osapi.on_battery()}"
    )


@_logged("bundle check")
def _check_bundle():
    """Logs the .saver's bundle id and path, and a WARNING when the id is not the
    ScreenSaverDefaults module name the options sheet saves under."""
    bundle = NSBundle.bundleForClass_(MatrixRainView)
    bid = bundle.bundleIdentifier()
    bid = str(bid) if bid is not None else None
    path = str(bundle.bundlePath())
    osapi.log(f"mac_saver: bundle id={bid} path={path}")
    warning = mac_settings.check_module_name(bid, path)
    if warning:
        osapi.log(f"mac_saver: WARNING {warning}")


def load_settings_source():
    """-> (cfg, source): the settings_file test hook, else mac_settings.resolve of
    the ScreenSaverDefaults blob and the old file; cfg_overrides laid over."""
    if settings_file:
        with open(settings_file, encoding="utf-8") as fh:
            cfg = settings.clean(json.load(fh))
        source = "settings_file"
    else:
        blob = mac_settings.read_saved_blob("mac_saver")
        cfg, source, problems = mac_settings.resolve(blob, osapi.settings_read_path())
        for p in problems:
            osapi.log(f"mac_saver: settings problem: {p}")
    cfg.update(cfg_overrides)
    return cfg, source


def load_settings():
    return load_settings_source()[0]


def _reload_all():
    """After a Save in the options sheet: every view reloads on its next frame."""
    for s in list(_all_savers):
        s.request_reload()


def _srgb_space(hdr):
    """The NSColorSpace to tag a view with, or None when AppKit has none. EDR
    content is linear light with 1.0 = SDR white, so it gets the extended linear one."""
    cls = getattr(AppKit, "NSColorSpace", None)
    if cls is None:
        return None
    return cls.extendedLinearSRGBColorSpace() if hdr else cls.sRGBColorSpace()


def _tag_window(win, space):
    win.setColorSpace_(space)
    return True


def _tag_layer(view, space):
    """Tag the layer of `view` when it has one; True if it did."""
    layer = view.layer() if view is not None else None
    if layer is None:
        return False
    layer.setColorspace_(space.CGColorSpace())
    return True


_gl = None  # OpenGL.framework through ctypes, loaded by _clear_black


def _clear_black(glview):
    """Clear a GL view's surface to black and show it. Covers the window's grey
    background while the renderer is built (the first frame takes seconds) and
    after it is freed on stop, when the host can still show the view."""
    global _gl
    if glview is None:
        return
    if _gl is None:
        import ctypes

        _gl = ctypes.CDLL("/System/Library/Frameworks/OpenGL.framework/OpenGL")
        _gl.glClearColor.argtypes = [ctypes.c_float] * 4
        _gl.glClear.argtypes = [ctypes.c_uint]
    glc = glview.openGLContext()
    glc.makeCurrentContext()
    _gl.glClearColor(0.0, 0.0, 0.0, 1.0)
    _gl.glClear(0x4000)  # GL_COLOR_BUFFER_BIT
    glc.flushBuffer()


def _screen_id(screen):
    num = screen.deviceDescription().get("NSScreenNumber") if screen is not None else None
    return int(num) if num is not None else None


def _where(view):
    """Where the host put a view: its window, level, visibility and screen."""
    try:
        win = view.window()
        f = view.frame().size
        sup = view.superview()
        head = (
            f"view {f.width:.0f}x{f.height:.0f} pt, superview "
            f"{type(sup).__name__ if sup is not None else None}"
        )
        if win is None:
            return head + ", no window"
        wf = win.frame()
        return (
            f"{head}, window {type(win).__name__} #{win.windowNumber()} "
            f"{wf.size.width:.0f}x{wf.size.height:.0f} at {wf.origin.x:.0f},{wf.origin.y:.0f} "
            f"level {win.level()} isVisible={bool(win.isVisible())} "
            f"occlusion={int(win.occlusionState())} "
            f"activeSpace={bool(win.isOnActiveSpace())} "
            f"screen={_screen_id(win.screen())}"
        )
    except Exception as exc:
        return f"(window details failed: {exc!r})"


class _Saver:
    """Everything one view does, kept in plain Python so the Objective-C class
    below only forwards the ScreenSaverView calls."""

    def __init__(self, view, preview, idx):
        self.view = view
        self.preview = preview
        self.idx = idx
        self.glview = None
        self.renderer = None
        self.size = None
        self.cfg = None
        self.hdr = None  # osapi.hdr_info() when the surface is float16 EDR
        self.failed = False
        self.stopped = False
        self.t0 = self.last = None
        self.frame = 0
        self.beat_t = None
        self.beat_frames = 0
        self.last_pow = 0.0
        self.fps = None
        self.shot_path = None  # tools/dev/mac_host.py: save the next frame as a PNG
        self.sheet_target = None
        self.screen_t = None
        self.screen_key = None  # NSScreenNumber of the view's window
        self.idle = False  # superseded: ticking at IDLE_FPS, drawing nothing
        self.idle_since = None
        self.idle_ticks = 0
        self.geom_logs = 0
        self.reload = False  # settings changed: reload them on the next frame
        self.color_done = False  # apply_srgb has run (once per view, with a window)
        _all_savers.append(self)
        if not preview:
            _full_views.append(self)
        try:
            self._make_gl_view()
        except Exception:  # a view that cannot get a GL surface stays black
            self._fail("GL view")

    def tag(self):
        return f"view {self.idx}{' (preview)' if self.preview else ''}"

    def _fail(self, what):
        osapi.log(
            f"mac_saver: {self.tag()}: {what} FAILED, view stays black\n" + traceback.format_exc()
        )
        self.failed = True

    def _make_gl_view(self):
        base = (
            NSOpenGLPFAOpenGLProfile,
            NSOpenGLProfileVersion3_2Core,
            NSOpenGLPFADoubleBuffer,
            NSOpenGLPFAAccelerated,
        )
        pf = None
        self.hdr = None
        if not self.preview:  # EDR: float16 surface, extended linear sRGB (1.0 = SDR white)
            try:
                if load_settings_source()[0]["hdr"] == "auto":
                    self.hdr = osapi.hdr_info(self.view)
            except Exception:
                osapi.log(f"mac_saver: {self.tag()}: hdr check failed\n" + traceback.format_exc())
        if self.hdr:
            pf = NSOpenGLPixelFormat.alloc().initWithAttributes_(
                base
                + (
                    getattr(AppKit, "NSOpenGLPFAColorFloat", NS_OPENGL_PFA_COLOR_FLOAT),
                    NSOpenGLPFAColorSize,
                    64,
                    0,
                )
            )
            if pf is None:
                osapi.log(f"mac_saver: {self.tag()}: no float pixel format, using SDR")
                self.hdr = None
        if pf is None:
            pf = NSOpenGLPixelFormat.alloc().initWithAttributes_(
                base + (NSOpenGLPFAColorSize, 24, 0)
            )
        if pf is None:
            raise RuntimeError("no OpenGL core pixel format")
        gv = NSOpenGLView.alloc().initWithFrame_pixelFormat_(self.view.bounds(), pf)
        gv.setWantsBestResolutionOpenGLSurface_(True)
        if self.hdr:
            try:
                gv.setWantsLayer_(True)
                gv.layer().setWantsExtendedDynamicRangeContent_(True)
            except Exception:
                osapi.log(
                    f"mac_saver: {self.tag()}: EDR layer FAILED, using SDR\n"
                    + traceback.format_exc()
                )
                self.hdr = None
        gv.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.view.addSubview_(gv)
        self.glview = gv
        try:  # the ScreenSaverView timer paces frames; never block on vsync
            gv.openGLContext().setValues_forParameter_([0], SWAP_INTERVAL_PARAM)
        except Exception as exc:
            osapi.log(f"mac_saver: {self.tag()}: swap interval not set ({exc!r})")

    def apply_srgb(self):
        """Tag this view's output as sRGB, so macOS converts the shader colours to
        the display's colour space. Runs once per view, when it first has a window.
        Logs one line saying what was tagged; whether a window colour space reaches
        an NSOpenGLView surface is an open question (design/mac-options-test.md)."""
        if self.color_done:
            return
        win = self.view.window()
        if win is None:  # not in a window yet: the next call tries again
            return
        self.color_done = True
        try:  # the host window's default grey shows wherever the GL view has not drawn
            win.setBackgroundColor_(NSColor.blackColor())
        except (AttributeError, objc.error) as exc:
            osapi.log(f"mac_saver: {self.tag()}: window background not set ({exc!r})")
        space = _srgb_space(self.hdr)
        tagged, notes = [], []
        if space is None:
            notes.append("no NSColorSpace")
        else:
            targets = (
                ("window", _tag_window, win),
                ("GL view layer", _tag_layer, self.glview),
                ("view layer", _tag_layer, self.view),
            )
            for name, tag, target in targets:
                try:
                    if tag(target, space):
                        tagged.append(name)
                except (AttributeError, objc.error) as exc:
                    notes.append(f"{name} not set ({exc!r})")
        screen = win.screen()
        shown = str(screen.colorSpace().localizedName()) if screen is not None else None
        osapi.log(
            f"mac_saver: {self.tag()}: colour space sRGB set on "
            f"{' and '.join(tagged) or 'neither window nor layer'}; screen colour "
            f"space {shown}" + (f"; {'; '.join(notes)}" if notes else "")
        )

    def _monitor(self, pw, ph):
        """This view's monitor entry, sized to the view. The thumbnail gets the
        scale that makes it a miniature of its display, as /p does on Windows."""
        win = self.view.window()
        screen = win.screen() if win is not None else None
        sid = _screen_id(screen)
        mons = osapi.list_monitors()
        mon = next((m for m in mons if m.get("cgid") == sid), mons[0])
        dpi = mon["dpi"]
        if self.preview and screen is not None:
            pts_w = self.view.bounds().size.width
            dpi = max(0.15, min(dpi, dpi * pts_w / max(1.0, screen.frame().size.width)))
        return dict(x=0, y=0, w=pw, h=ph, dpi=dpi, id=mon["id"], name=mon["name"])

    def _ensure_renderer(self):
        """True when a renderer of the view's current pixel size is ready."""
        pw, ph = osapi.client_size(self.glview)
        if pw < 8 or ph < 8 or self.view.window() is None:
            return False
        if self.renderer is not None and self.size == (pw, ph):
            return True
        if self.renderer is not None:
            osapi.log(f"mac_saver: {self.tag()}: size {self.size} -> {(pw, ph)}, rebuilding")
            self._close_renderer()
        self._blank("before the renderer build")
        glc = self.glview.openGLContext()
        glc.makeCurrentContext()
        if self.cfg is None:
            self.cfg, source = load_settings_source()
            osapi.log(
                f"mac_saver: {self.tag()}: settings source={source} "
                f"preset={self.cfg['preset']} direction={self.cfg['direction']} "
                f"renderer={self.cfg['renderer']}"
            )
        mon = self._monitor(pw, ph)
        # Heavy imports, wanted only once a renderer is actually built.
        import pygame

        from digital_rain.gl_renderer import GLRenderer

        pygame.font.init()
        t0 = time.perf_counter()
        self.renderer = GLRenderer([mon], self.cfg, (0, 0), (pw, ph), hdr=self.hdr)
        self.size = (pw, ph)
        info = self.renderer.ctx.info
        osapi.log(
            f"mac_saver: {self.tag()}: renderer ready in "
            f"{(time.perf_counter() - t0) * 1000:.0f} ms, {pw}x{ph} px, "
            f"scale {mon['dpi']:.2f}, {mon['name']}, glyphs {self.renderer.glyph_source}, "
            f"GL {info['GL_VERSION']} / {info['GL_RENDERER']}"
        )
        self._pick_fps(force=True)
        return True

    def _blank(self, when):
        """_clear_black on this view's GL surface; a failure is logged, never raised."""
        try:
            _clear_black(self.glview)
        except Exception:
            osapi.log(
                f"mac_saver: {self.tag()}: black clear {when} failed\n" + traceback.format_exc()
            )

    def _close_renderer(self):
        r, self.renderer = self.renderer, None
        self.size = None
        if r is None:
            return
        try:
            if self.glview is not None:
                self.glview.openGLContext().makeCurrentContext()
            r.close()
        except Exception:
            osapi.log(f"mac_saver: {self.tag()}: renderer close failed\n" + traceback.format_exc())

    def _pick_fps(self, force=False):
        now = time.perf_counter()
        if not force and now - self.last_pow < POWER_CHECK_SECS:
            return
        self.last_pow = now
        if self.preview:
            fps = PREVIEW_FPS
        else:
            fps = BATTERY_FPS if osapi.on_battery() else osapi.max_refresh_rate()
        if fps != self.fps:
            self.fps = fps
            self.view.setAnimationTimeInterval_(1.0 / (fps * TIMER_SUBSTEPS))
            osapi.log(f"mac_saver: {self.tag()}: frame rate cap {fps} Hz")

    def _due(self, now):
        """True when a frame's time (less half a timer tick of slack) has passed since the last
        drawn frame."""
        if self.last is None or not self.fps:
            return True
        return now - self.last >= (1.0 - 0.5 / TIMER_SUBSTEPS) / self.fps

    @_logged("startAnimation")
    def start(self, who="host"):
        self.stopped = False
        osapi.log(f"mac_saver: {self.tag()}: startAnimation ({who}); {_where(self.view)}")
        self.apply_srgb()
        if not self.preview:
            live = [s.tag() for s in _full_views if s.live()]
            osapi.log(f"mac_saver: {self.tag()}: live full-screen views in this process: {live}")
        if self.cfg is not None and load_settings() != self.cfg:
            osapi.log(f"mac_saver: {self.tag()}: settings changed, reloading")
            self.request_reload()

    def request_reload(self):
        """Load settings again and rebuild the renderer on the next frame."""
        self.reload = True

    def live(self):
        """Animating, not stopped and not failed."""
        return bool(self.view.isAnimating()) and not self.stopped and not self.failed

    @_logged("viewDidMoveToWindow")
    def moved_to_window(self):
        self.geometry("moved to window" if self.view.window() is not None else "left its window")
        self.apply_srgb()

    @_logged("setFrameSize")
    def resized(self, size):
        self.geometry(f"frame size {size.width:.0f}x{size.height:.0f} pt")

    def geometry(self, what):
        """Log the view's placement (a few lines per view) and refit the GL view."""
        if self.geom_logs < GEOMETRY_LOG_LIMIT:
            self.geom_logs += 1
            osapi.log(f"mac_saver: {self.tag()}: {what}; {_where(self.view)}")
        if self.glview is not None:  # autoresizing from a 0 x 0 superview is not trusted
            self.glview.setFrame_(self.view.bounds())

    def _update_screen(self, now):
        if self.screen_t is not None and now - self.screen_t < SCREEN_CHECK_SECS:
            return
        self.screen_t = now
        win = self.view.window()
        self.screen_key = _screen_id(win.screen()) if win is not None else None

    def _should_draw(self, now):
        """False for a full-screen view a newer live view on its display replaced."""
        if self.preview:
            return True
        self._update_screen(now)
        if self.screen_key is None:  # no window: nothing to draw into, nothing to compare
            return True
        return not any(
            s.idx > self.idx and s.screen_key == self.screen_key and s.live() for s in _full_views
        )

    def _set_idle(self, idle, now):
        if idle == self.idle:
            return
        self.idle = idle
        if idle:
            self.idle_since, self.idle_ticks = now, 0
            self.view.setAnimationTimeInterval_(1.0 / IDLE_FPS)
            osapi.log(
                f"mac_saver: {self.tag()}: superseded by a newer view, idling at "
                f"{IDLE_FPS} Hz after {self.frame} frames; {_where(self.view)}"
            )
        else:
            osapi.log(
                f"mac_saver: {self.tag()}: newest view again after "
                f"{now - self.idle_since:.1f} s idle; {_where(self.view)}"
            )
            self.idle_since = None
            self.fps = None
            self.last = now
            self._pick_fps(force=True)

    def stop(self, why):
        if not self.stopped:
            osapi.log(f"mac_saver: {self.tag()}: stopped by {why} after {self.frame} frames")
        self.stopped = True

    @_logged("notification")
    def notified(self, name):
        osapi.log(f"mac_saver: {self.tag()}: notification {name}")
        # Sonoma and later can leave a full-screen view animating after the
        # saver is dismissed (one trial view never got stopAnimation), so the
        # willstop broadcast stops every full-screen view in the process.
        if name == STOP_NOTE and not self.preview:
            self.stop("willstop notification")
            if self.renderer is not None:
                self._blank("on stop")
            self._close_renderer()
        if name == mac_settings.CHANGED_NOTE:
            self.request_reload()

    def tick(self):
        """One animation frame. Any failure blacks this view out and frees its renderer."""
        try:
            self._tick()
        except Exception:
            self._fail(f"frame {self.frame}")
            self._close_renderer()

    def _tick(self):
        if self.stopped or self.failed or self.glview is None:
            return
        if self.reload:
            self.reload = False
            self.cfg = None
            self._close_renderer()
            osapi.log(f"mac_saver: {self.tag()}: reloading settings")
        now = time.perf_counter()
        if self.beat_t is None:
            self.beat_t = now
        draw = self._should_draw(now)
        self._set_idle(not draw, now)
        if not draw:
            self.idle_ticks += 1
            if self.renderer is not None and now - self.idle_since >= IDLE_RELEASE_SECS:
                osapi.log(
                    f"mac_saver: {self.tag()}: idle {IDLE_RELEASE_SECS:.0f} s, renderer released"
                )
                self._close_renderer()
            if now - self.beat_t >= HEARTBEAT_SECS:
                osapi.log(
                    f"mac_saver: {self.tag()}: heartbeat idle (superseded), "
                    f"{self.idle_ticks} ticks skipped, renderer "
                    f"{'kept' if self.renderer is not None else 'none'}; "
                    f"{_where(self.view)}"
                )
                self.beat_t, self.beat_frames, self.idle_ticks = now, 0, 0
            return
        if not self._due(now):
            return
        if not self._ensure_renderer():
            return
        glc = self.glview.openGLContext()
        glc.makeCurrentContext()
        now = time.perf_counter()
        if self.t0 is None:
            self.t0 = self.last = now
        t, dt, self.last = now - self.t0, min(now - self.last, 0.1), now
        self.renderer.render(t, dt, self.frame)
        if self.shot_path:
            path, self.shot_path = self.shot_path, None
            self.renderer.screenshot(path)
            osapi.log(f"mac_saver: {self.tag()}: screenshot {path}")
        glc.flushBuffer()
        self.frame += 1
        self.beat_frames += 1
        self._pick_fps()
        if now - self.beat_t >= HEARTBEAT_SECS:
            fps = self.beat_frames / (now - self.beat_t)
            osapi.log(
                f"mac_saver: {self.tag()}: heartbeat {fps:.1f} fps (cap {self.fps}), "
                f"{self.size[0]}x{self.size[1]} px, "
                f"live full-screen views {sum(1 for s in _full_views if s.live())}"
            )
            self.beat_t, self.beat_frames = now, 0

    @_logged("configureSheet")
    def configure_sheet(self):
        """The options sheet (mac_options); if building it fails, a small sheet
        that says where the details are, so the Options button never does nothing."""
        osapi.log(f"mac_saver: {self.tag()}: configureSheet")
        try:
            sheet = None

            def closed():
                if self.sheet_target is sheet:
                    self.sheet_target = None

            sheet = mac_options.build(on_saved=_reload_all, on_closed=closed)
            self.sheet_target = sheet  # keeps the sheet, its target and window alive
            return sheet.window
        except Exception:  # the Options button must never do nothing
            osapi.log(f"mac_saver: {self.tag()}: options sheet FAILED\n" + traceback.format_exc())
        return self._fallback_sheet()

    def _fallback_sheet(self):
        text = f"Digital Rain options could not open. Details are in {osapi.log_path()}."
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 460, 150), NSWindowStyleMaskTitled, NSBackingStoreBuffered, False
        )
        content = win.contentView()
        label = NSTextField.wrappingLabelWithString_(text)
        label.setFrame_(NSMakeRect(20, 60, 420, 74))
        content.addSubview_(label)
        self.sheet_target = MatrixRainSheetTarget.alloc().init()
        btn = NSButton.buttonWithTitle_target_action_("Close", self.sheet_target, "close:")
        btn.setFrame_(NSMakeRect(350, 16, 90, 32))
        content.addSubview_(btn)
        return win


class MatrixRainSheetTarget(NSObject):
    """Close button of the fallback sheet."""

    def close_(self, sender):
        win = sender.window()
        parent = win.sheetParent()
        if parent is not None:
            parent.endSheet_(win)
        else:
            win.close()


class MatrixRainView(ScreenSaverView):
    """The principal class of the .saver bundle."""

    def initWithFrame_isPreview_(self, frame, is_preview):
        self = objc.super(MatrixRainView, self).initWithFrame_isPreview_(frame, is_preview)
        if self is None:
            return None
        global _views, _started
        _views += 1
        if not _started:
            _started = True
            _environment_report()
            _check_bundle()
        self.mr = _Saver(self, bool(is_preview), _views)
        osapi.log(
            f"mac_saver: {self.mr.tag()}: init frame "
            f"{frame.size.width:.0f}x{frame.size.height:.0f} pt"
        )
        self.setAnimationTimeInterval_(1.0 / (PREVIEW_FPS if is_preview else 60))
        center = NSDistributedNotificationCenter.defaultCenter()
        for name in NOTES + (mac_settings.CHANGED_NOTE,):
            center.addObserver_selector_name_object_(self, "matrixRainNotified:", name, None)
        return self

    def startAnimation(self):
        objc.super(MatrixRainView, self).startAnimation()
        self.mr.start()

    def stopAnimation(self):
        objc.super(MatrixRainView, self).stopAnimation()
        self.mr.stop("stopAnimation")

    def viewDidMoveToWindow(self):
        objc.super(MatrixRainView, self).viewDidMoveToWindow()
        mr = getattr(self, "mr", None)
        if mr is not None:  # None during initWithFrame:isPreview:, before _Saver exists
            mr.moved_to_window()

    def setFrameSize_(self, size):
        objc.super(MatrixRainView, self).setFrameSize_(size)
        mr = getattr(self, "mr", None)
        if mr is not None:  # None during initWithFrame:isPreview:, before _Saver exists
            mr.resized(size)

    def matrixRainNotified_(self, note):
        self.mr.notified(str(note.name()))

    def drawRect_(self, rect):
        # Only seen before the GL subview has drawn, or when it failed.
        NSColor.blackColor().set()
        NSRectFill(rect)

    def animateOneFrame(self):
        self.mr.tick()

    def hasConfigureSheet(self):
        return True

    def configureSheet(self):
        return self.mr.configure_sheet()


osapi.log("mac_saver: loaded, MatrixRainView defined")
