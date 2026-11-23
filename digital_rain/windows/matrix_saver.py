"""Digital Rain, a Matrix-inspired screensaver for Windows (multi-monitor): the .scr entry
point. The macOS .saver has its own entry point (mac_saver.py); both share
everything below the osapi platform layer.

Windows screensaver contract (the .scr is just a renamed .exe):
    /s       run the screensaver full screen
    /c       show the settings dialog
    /p HWND  render inside the tiny preview window of the settings picker
    /w       no longer supported (the wallpaper mode was removed): exits at once with
             status 0 and draws nothing

Dev flags:  --seconds N     auto-exit after N seconds (ignores input)
            --shot FILE     save a screenshot just before exiting
            --renderer R    force `gpu` or `software`
            --preset NAME   look preset: custom, film_1999, reloaded, resurrections
            --direction D   trail direction: down, sideways, diagonal
            --pulse         turn the 1999 pulse (stammer) on
            --preview-sim   draw at the /p preview size and scale, no parent window
            --dialog-shot FILE  with /c: save a PNG of the settings dialog and exit
            --dialog-scale F    with --dialog-shot: draw the dialog as on a monitor at
                            F x 96 DPI
            --split-sim     split the primary monitor into two simulated monitors
                            (sim:left, sim:right)
            --settings-file PATH  load the settings from this JSON file instead of the saved ones
            --fps N         cap the frame rate at N
            --run N         fix the rain layout (0: the fixed layout; GPU)
            --preview-scale K  with /p: draw the glyphs at K x 96 DPI (default 0.3)
            --ready-event NAME  with /p and --settings-file: start hidden, embed after the
                            first frame, then set this named event (the settings window's preview)
            --dialog-size WxH   with /c and --dialog-shot: window size in 96 DPI units
            --warm-up       load everything a launch loads with no window on screen, draw
                            for a second and exit; run once after installing (see _warm_up)
"""

import argparse
import ctypes
import dataclasses
import json
import math
import os
import statistics
import sys
import time

_T0 = time.perf_counter()

# after _T0, so the first-frame time includes the imports
from digital_rain import exit_gate, osapi, settings  # noqa: E402

MONITOR_CHECK_SECS = 3.0
POWER_CHECK_SECS = 5.0
FPS_BATTERY = 30
MOUSE_THRESHOLD_PX = 25
CHECK_HIDDEN = False  # set from --check-hidden (test tooling) or --warm-up in main()
WARM_UP_SECONDS = 1.0

# Preview pane of the settings picker (/p): glyph scale, and the size --preview-sim draws at.
PREVIEW_DPI_SCALE = 0.3
PREVIEW_SIM_SIZE = (152, 112)
# A longer frame gap (a stall, a drag) is clamped to this when stepping the animation.
MAX_FRAME_DT = 0.1
# Frame pacing: sleep until this close to the deadline, then spin.
SPIN_WINDOW_SECS = 0.001
SLEEP_MARGIN_SECS = 0.0008

# Timed runs skip the first frames when collecting render times (shader warm-up).
PERF_WARMUP_FRAMES = 5


SDL_GL_FLOATBUFFERS = 27  # SDL 2.32 attribute pygame-ce does not name: float pixel format


def _tick(label):
    """MATRIX_TIMING=1 (off by default): print a wall-clock stamp, for startup measurements."""
    if os.environ.get("MATRIX_TIMING"):
        print(f"TIMING {label} {time.time():.4f}", file=sys.stderr, flush=True)


def _sdl():
    """The SDL2 library pygame already loaded (None if it cannot be reached)."""
    try:
        return ctypes.CDLL("SDL2.dll")
    except OSError:
        return None


def _set_float_buffers(on):
    """pygame-ce rejects attribute 27 ("Unknown OpenGL attribute"), so go to SDL itself."""
    sdl = _sdl()
    if sdl is None:
        if on:
            raise RuntimeError("SDL2 not reachable for the float pixel format")
        return
    if sdl.SDL_GL_SetAttribute(SDL_GL_FLOATBUFFERS, 1 if on else 0) != 0 and on:
        raise RuntimeError("SDL has no float pixel format attribute")


def _gl_attributes(pygame, hdr):
    """GL 3.3 core, plus a float16 RGBA back buffer for HDR (scRGB on Windows)."""
    channel_bits = 16 if hdr else 8
    for attr, val in (
        (pygame.GL_CONTEXT_MAJOR_VERSION, 3),
        (pygame.GL_CONTEXT_MINOR_VERSION, 3),
        (pygame.GL_CONTEXT_PROFILE_MASK, pygame.GL_CONTEXT_PROFILE_CORE),
        (pygame.GL_RED_SIZE, channel_bits),
        (pygame.GL_GREEN_SIZE, channel_bits),
        (pygame.GL_BLUE_SIZE, channel_bits),
        (pygame.GL_ALPHA_SIZE, channel_bits),
    ):
        pygame.display.gl_set_attribute(attr, val)
    _set_float_buffers(hdr)


def _make_gl(pygame, cfg, mons, origin, size, flags, run=None, hdr_ok=False):
    # Imported here so the software renderer never loads the OpenGL stack.
    from digital_rain.gl_renderer import GLRenderer

    _tick("make_gl_start")
    hdr = osapi.hdr_info() if hdr_ok and cfg["hdr"] == "auto" else None
    _tick("hdr_info_done")
    mode = pygame.OPENGL | pygame.DOUBLEBUF | flags
    if hdr:
        try:
            _gl_attributes(pygame, True)
            pygame.display.set_mode(size, mode, vsync=1)
            # SDL cannot read the float attribute back; a 16 bit channel is only offered as float
            if pygame.display.gl_get_attribute(pygame.GL_RED_SIZE) < 16:
                raise RuntimeError("no float16 pixel format")
        except (pygame.error, RuntimeError) as exc:
            print(f"HDR output unavailable ({exc}); using SDR", file=sys.stderr)
            hdr = None
            # Windows cannot change the pixel format of an existing window: start over
            pygame.display.quit()
            pygame.display.init()
    if not hdr:
        _gl_attributes(pygame, False)
        pygame.display.set_mode(size, mode, vsync=1)
    _tick("set_mode_done")
    renderer = GLRenderer(mons, cfg, origin, size, run=run, hdr=hdr)
    _tick("renderer_init_done")
    return renderer


def _make_soft(pygame, cfg, mons, origin, size, flags, run=None):
    # Imported here so the GPU path never loads the software renderer.
    from digital_rain.soft_renderer import SoftRenderer

    screen = pygame.display.set_mode(size, flags)
    return SoftRenderer(mons, cfg, origin, size, screen, run=run)


def make_renderer(pygame, cfg, mons, origin, size, preference, flags, run=None, hdr_ok=False):
    """GPU first (unless told otherwise), software if OpenGL is unavailable."""
    if preference != "software":
        try:
            return _make_gl(pygame, cfg, mons, origin, size, flags, run, hdr_ok)
        except Exception as exc:
            # Any failure to start the GPU path (no GL driver, RDP session, VM, shader
            # build error) falls back to software: the saver must still draw something.
            print(f"GPU renderer unavailable ({exc}); using software", file=sys.stderr)
            pygame.display.quit()
            pygame.display.init()
    return _make_soft(pygame, cfg, mons, origin, size, flags, run)


class FrameLimiter:
    """Precise frame pacing: sleep for most of the gap, spin for the last ms.

    pygame's Clock.tick() sleeps in whole milliseconds, which makes a 75 Hz
    target land anywhere between ~66 and ~83 FPS.
    """

    def __init__(self):
        self.next = time.perf_counter()

    def wait(self, fps):
        step = 1.0 / fps
        self.next += step
        now = time.perf_counter()
        if self.next < now - step:  # fell far behind (e.g. a stall): resync
            self.next = now
        while True:
            left = self.next - time.perf_counter()
            if left <= 0:
                break
            if left > SPIN_WINDOW_SECS:
                time.sleep(left - SLEEP_MARGIN_SECS)


def perf_line(first_flip, render_ms):
    """The `perf:` line of a timed run; `none` for any number that was not measured."""
    first_frame = "none" if first_flip is None else str(int((first_flip - _T0) * 1000))
    if render_ms:
        ordered = sorted(render_ms)
        median = f"{statistics.median(ordered):.2f}"
        p95 = f"{ordered[math.ceil(0.95 * len(ordered)) - 1]:.2f}"
    else:
        median = p95 = "none"
    return f"perf: first_frame_ms={first_frame} render_median_ms={median} render_p95_ms={p95}"


@dataclasses.dataclass
class SessionOptions:
    """How one session runs: the launch mode and the developer and test options.

    start_hidden: create the /p window hidden and embed it after the first flip,
    then set ready_event (the settings window swaps previews on that event).
    run: fixes the rain layout (a test hook)."""

    preview_hwnd: int | None = None
    seconds: float | None = None
    shot: str | None = None
    force: str | None = None
    preview_sim: bool = False
    split_sim: bool = False
    fps_limit: float | None = None
    preview_scale: float = PREVIEW_DPI_SCALE
    start_hidden: bool = False
    ready_event: str | None = None
    run: int | None = None


def _split_monitors(primary):
    """The primary monitor cut into two simulated monitors, side by side."""
    half = primary["w"] // 2
    return [
        dict(
            x=primary["x"],
            y=primary["y"],
            w=half,
            h=primary["h"],
            dpi=primary["dpi"],
            id="sim:left",
            name="Simulated left",
        ),
        dict(
            x=primary["x"] + half,
            y=primary["y"],
            w=primary["w"] - half,
            h=primary["h"],
            dpi=primary["dpi"],
            id="sim:right",
            name="Simulated right",
        ),
    ]


def layout_monitors(real, opts):
    """-> the rectangles to draw into, given the real monitors and the launch mode."""
    if opts.preview_hwnd:
        width, height = osapi.client_size(opts.preview_hwnd)
        return [dict(x=0, y=0, w=width, h=height, dpi=opts.preview_scale)]
    if opts.preview_sim:
        width, height = PREVIEW_SIM_SIZE
        return [dict(x=0, y=0, w=width, h=height, dpi=PREVIEW_DPI_SCALE)]
    if opts.split_sim:
        return _split_monitors(real[0])
    return real


class DevRun:
    """What only timed developer and check runs (--seconds) do: measure frames,
    synthesise the cursor, save the screenshot and print the report."""

    def __init__(self, opts):
        self.opts = opts
        self.render_ms = []
        self.first_flip = None

    def render_and_flip(self, session, dt):
        """The frame of the production loop, timed."""
        start = time.perf_counter()
        session.renderer.render(session.t, dt, session.frame)
        if session.frame >= PERF_WARMUP_FRAMES:
            self.render_ms.append((time.perf_counter() - start) * 1000)
        session.pygame.display.flip()
        if self.first_flip is None:
            self.first_flip = time.perf_counter()
            _tick("first_flip")

    def save_screenshot(self, session):
        """Called once the run reaches its deadline; does nothing without --shot."""
        shot, renderer = self.opts.shot, session.renderer
        if not shot:
            return
        renderer.screenshot(shot)

    def print_report(self, session):
        """The lines the check tools parse from a finished timed run."""
        cfg, renderer = session.cfg, session.renderer
        look = f"{cfg['preset']}/{cfg['direction']}/{'on' if cfg['pulse'] else 'off'}"
        overrides = sum(1 for m in session.mons if m.get("id") in cfg["monitors"])
        fps = session.frame / max(session.t, 1e-6)
        print(
            f"renderer: {renderer.name}, glyphs: {renderer.glyph_source}, look: {look}, "
            f"mons: {len(session.mons)}, overrides: {overrides}, cap: {session.fps_cap} Hz, "
            f"avg fps at exit: {fps:.1f}"
        )
        print(f"run: {renderer.run}")
        print(perf_line(self.first_flip, self.render_ms))
        live = renderer.live
        load = "unavailable" if live.load is None else f"{live.load:.2f}"
        print(
            f"live: source={cfg['live_source']} load={load} "
            f"speed_k={live.speed_k:.2f} density_k={live.density_k:.2f}"
        )


class Session:
    """One run of the saver in one window: the monitors, the renderer and the
    frame loop. `tick` draws one frame; `run` ticks until the session ends."""

    def __init__(self, cfg, opts):
        self.cfg = cfg
        self.opts = opts
        real = osapi.list_monitors()
        self.mons = layout_monitors(real, opts)
        # Hot-plug signature of the real displays, whatever rectangles we draw into.
        self.hotplug_signature = osapi.monitor_signature(real)
        self.bounds = osapi.virtual_bounds(self.mons)
        self.hidden = CHECK_HIDDEN
        self.dev = DevRun(opts) if opts.seconds is not None else None
        self.result = "quit"
        self.embedded = False
        self.shown = False
        self._open_window()
        self._start_loop()

    def _open_window(self):
        vx, vy, vw, vh = self.bounds
        opts = self.opts
        os.environ["SDL_VIDEO_WINDOW_POS"] = f"{vx},{vy}"
        os.environ["SDL_VIDEO_ALLOW_SCREENSAVER"] = "1"
        # Imported here: the /c dialog and the argument parser never need pygame.
        import pygame

        self.pygame = pygame
        pygame.display.init()
        pygame.font.init()
        flags = pygame.NOFRAME
        # Check runs only: the explicit --check-hidden flag (added by tools/checks/hidden.py
        # to saver launches). The environment never hides a real launch.
        # A full-screen window also starts hidden and is shown after its first frame
        # (_show_after_first_frame): building the renderer takes seconds, and a window
        # shown before it draws is a blank grey sheet over the desktop.
        if self.hidden or opts.start_hidden or self._full_screen():
            flags |= pygame.HIDDEN
        hdr_ok = not (opts.preview_hwnd or opts.preview_sim or self.hidden)
        self.renderer = make_renderer(
            pygame,
            self.cfg,
            self.mons,
            (vx, vy),
            (vw, vh),
            opts.force or self.cfg["renderer"],
            flags,
            opts.run,
            hdr_ok=hdr_ok,
        )
        pygame.display.set_caption("Digital Rain")
        self.hwnd = pygame.display.get_wm_info()["window"]
        self._place_window()

    def _place_window(self):
        """Embed or raise the new window for the launch mode."""
        opts = self.opts
        vx, vy, vw, vh = self.bounds
        if opts.preview_hwnd:
            if not opts.start_hidden:
                osapi.embed_in_parent(self.hwnd, opts.preview_hwnd, vw, vh)
                self.embedded = True
        elif self._full_screen():
            self.pygame.mouse.set_visible(False)  # shown by _show_after_first_frame

    def _full_screen(self):
        """The real saver window: not a preview, simulated or hidden check run."""
        opts = self.opts
        return not (opts.preview_hwnd or opts.preview_sim or self.hidden)

    def _show_after_first_frame(self):
        """Raise the full-screen window once it holds a finished frame."""
        if self.shown or not self._full_screen():
            return
        self.shown = True
        vx, vy, vw, vh = self.bounds
        osapi.set_topmost(self.hwnd, vx, vy, vw, vh)

    def _start_loop(self):
        """Frame pacing and the exit gate."""
        # vsync is ignored for a window spanning several monitors (we measured 200+
        # FPS), so pace frames ourselves at the fastest display's refresh rate.
        self.ac_fps = osapi.max_refresh_rate()
        self.limiter = FrameLimiter()
        self.start = self.last_monitor_check = self.last_power_check = time.perf_counter()
        self.last = self.start
        self.fps_cap = self._pick_fps_cap()
        self.gate = exit_gate.ExitGate(MOUSE_THRESHOLD_PX)
        self.frame = 0
        self.t = 0.0
        self.running = True

    def _pick_fps_cap(self):
        cap = FPS_BATTERY if osapi.on_battery() else self.ac_fps
        cap = min(cap, float(self.cfg["frame_cap"]))
        fps_limit = self.opts.fps_limit
        return min(cap, fps_limit) if fps_limit else cap

    def run(self):
        """Tick until the session ends. Returns "quit" or "restart" (monitors changed)."""
        while self.running:
            self.tick()
        if self.dev:
            self.dev.print_report(self)
        self.close()
        return self.result

    def close(self):
        # Hidden first: freeing the GL resources on a visible window can show it blank.
        if self.shown:
            osapi.hide_window(self.hwnd)
        self.renderer.close()
        self.pygame.display.quit()

    def tick(self):
        """One frame: pace, read input, draw, then the periodic checks."""
        self.limiter.wait(self.fps_cap)
        now = time.perf_counter()
        self.t = now - self.start
        dt = min(now - self.last, MAX_FRAME_DT)
        self.last = now

        self._handle_events()
        if not self.running:
            return
        if not self._draw(dt):
            return
        self.frame += 1
        self._embed_after_first_frame()
        self._show_after_first_frame()

        if self.opts.preview_hwnd and not osapi.is_window(self.opts.preview_hwnd):
            self.running = False
        self._periodic_checks(now)
        if self.dev and self.t >= self.opts.seconds:
            self.dev.save_screenshot(self)
            self.running = False

    def _handle_events(self):
        pygame, opts = self.pygame, self.opts
        for event in pygame.event.get():
            if opts.seconds is not None or opts.preview_hwnd:
                continue  # dev/test and preview: input never closes it
            closes = event.type in (pygame.KEYDOWN, pygame.MOUSEBUTTONDOWN, pygame.QUIT) or (
                event.type == pygame.MOUSEMOTION and self.gate.motion(self.t, event.pos)
            )
            if closes:
                self.running = False

    def _draw(self, dt):
        if self.dev:
            self.dev.render_and_flip(self, dt)
        else:
            self.renderer.render(self.t, dt, self.frame)
            self.pygame.display.flip()
        return True

    def _embed_after_first_frame(self):
        """A hidden preview shows in its parent after the first frame is drawn, so the
        old preview is replaced by a finished frame, never a blank window."""
        opts = self.opts
        if self.embedded or not opts.preview_hwnd:
            return
        self.embedded = True
        osapi.embed_in_parent(self.hwnd, opts.preview_hwnd, self.bounds[2], self.bounds[3])
        if opts.ready_event:
            osapi.signal_event(opts.ready_event)

    def _periodic_checks(self, now):
        """Re-pick the frame cap on battery changes, and watch for monitor hot-plugs."""
        opts = self.opts
        if now - self.last_power_check > POWER_CHECK_SECS:
            self.last_power_check = now
            self.fps_cap = self._pick_fps_cap()
        watch_monitors = not (opts.preview_hwnd or opts.preview_sim or opts.split_sim)
        if watch_monitors and now - self.last_monitor_check > MONITOR_CHECK_SECS:
            self.last_monitor_check = now
            if osapi.monitor_signature(osapi.list_monitors()) != self.hotplug_signature:
                self.result, self.running = "restart", False  # hot-plugged a display


def run_session(cfg, opts):
    """Run one session until the user quits. Returns "quit" or "restart"."""
    return Session(cfg, opts).run()


# Dev flags that, given alone, make the saver run (/s) instead of opening the dialog.
_RUN_SWITCHES = ("--pulse", "--preview-sim", "--split-sim", "--warm-up")
_RUN_VALUE_FLAGS = ("--seconds", "--shot", "--renderer", "--preset", "--direction")
# The dev flags that leave the mode alone.
_PLAIN_FLAGS = ("--check-hidden",)
_PLAIN_VALUE_FLAGS = (
    "--dialog-shot",
    "--dialog-scale",
    "--settings-file",
    "--fps",
    "--run",
    "--preview-scale",
    "--ready-event",
    "--dialog-size",
)
_VALUE_FLAGS = _RUN_VALUE_FLAGS + _PLAIN_VALUE_FLAGS
_KNOWN_FLAGS = _RUN_SWITCHES + _PLAIN_FLAGS + _VALUE_FLAGS
LEGACY_MODES = ("s", "c", "p", "w")


def _positive_float(text):
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"bad value: {text}") from None
    if not 0 < value < float("inf"):
        raise argparse.ArgumentTypeError(f"bad value: {text}")
    return value


def _size(text):
    parts = text.lower().split("x")
    if len(parts) != 2 or not all(p.isdigit() and int(p) > 0 for p in parts):
        raise argparse.ArgumentTypeError(f"bad value: {text}")
    return int(parts[0]), int(parts[1])


def _layout_seed(text):
    try:
        value = int(text, 16) if text.lower().startswith("0x") else int(text, 10)
    except ValueError:
        raise argparse.ArgumentTypeError(f"bad value: {text}") from None
    if not 0 <= value < 2**32:
        raise argparse.ArgumentTypeError(f"bad value: {text}")
    return value


def _build_flag_parser():
    parser = argparse.ArgumentParser(prog="matrix_saver.py", add_help=False, allow_abbrev=False)
    for flag in _RUN_SWITCHES + _PLAIN_FLAGS:
        parser.add_argument(flag, action="store_true")
    for flag, kind in (
        ("--seconds", float),
        ("--dialog-scale", float),
        ("--fps", _positive_float),
        ("--preview-scale", _positive_float),
        ("--dialog-size", _size),
        ("--run", _layout_seed),
    ):
        parser.add_argument(flag, type=kind)
    for flag in (
        "--shot",
        "--renderer",
        "--preset",
        "--direction",
        "--dialog-shot",
        "--settings-file",
        "--ready-event",
    ):
        parser.add_argument(flag)
    return parser


_FLAG_PARSER = _build_flag_parser()


def _legacy_mode(argv):
    """-> (mode, preview hwnd) from the /s /c /p HWND /w tokens of `argv`.

    The dev flags in _RUN_SWITCHES and _RUN_VALUE_FLAGS turn the default mode "c"
    into "s", at their position in the command line."""
    mode, hwnd = "c", None
    i = 0
    while i < len(argv):
        low = argv[i].lower()
        takes_value = low in _VALUE_FLAGS and i + 1 < len(argv)
        if low in _RUN_SWITCHES or (takes_value and low in _RUN_VALUE_FLAGS):
            mode = "s" if mode == "c" else mode
        if takes_value:
            i += 2
            continue
        body = low.lstrip("/-")
        if low[:1] in "/-" and not low.startswith("--") and body[:1] in LEGACY_MODES:
            mode = body[0]
            rest = body[1:].lstrip(":")
            if rest.isdigit():
                hwnd = int(rest)
            elif i + 1 < len(argv) and argv[i + 1].isdigit():
                hwnd = int(argv[i + 1])
                i += 1
        i += 1
    return mode, hwnd


def parse_args(argv):
    """-> (mode, preview_hwnd, dev_options). Exits with status 2 on a bad flag value."""
    # Flag names are case-insensitive, as they always were.
    folded = [a.lower() if a.lower() in _KNOWN_FLAGS else a for a in argv]
    parsed, _legacy = _FLAG_PARSER.parse_known_args(folded)
    dev = vars(parsed)
    mode, hwnd = _legacy_mode(argv)
    return mode, hwnd, dev


def _acquire_instance(mode, dev):
    """Single-instance handling. -> keep going."""
    if dev["seconds"] is None and not (mode == "p" and dev["settings_file"]):
        # The settings window's preview runs beside Windows' own /p preview,
        # so it skips the Preview mutex; /s keeps the Run one.
        name = "Local\\MatrixRainScreensaver_" + ("Preview" if mode == "p" else "Run")
        if not osapi.acquire_single_instance(name):
            return False  # already running
    return True


def _load_config(dev):
    """The settings: from --settings-file when given, otherwise the saved ones."""
    if not dev["settings_file"]:
        return settings.load()
    try:
        with open(dev["settings_file"], encoding="utf-8") as fh:
            return settings.clean(json.load(fh))
    except (OSError, ValueError) as exc:
        print(f"settings file unreadable: {exc}", file=sys.stderr)
        sys.exit(2)


def _apply_dev_overrides(cfg, dev):
    """Apply --preset, --direction and --pulse to cfg; exit 2 on bad names."""
    for key, names in (("preset", settings.PRESET_NAMES), ("direction", settings.DIRECTIONS)):
        if dev[key] is not None:
            if dev[key] not in names:
                print(f"unknown {key} {dev[key]}", file=sys.stderr)
                sys.exit(2)
            cfg[key] = dev[key]
    if dev["pulse"]:
        cfg["pulse"] = True


def _session_options(mode, hwnd, dev):
    return SessionOptions(
        preview_hwnd=hwnd if mode == "p" else None,
        seconds=dev["seconds"],
        shot=dev["shot"],
        force=dev["renderer"],
        preview_sim=dev["preview_sim"],
        split_sim=dev["split_sim"],
        fps_limit=dev["fps"],
        preview_scale=dev["preview_scale"] or PREVIEW_DPI_SCALE,
        start_hidden=mode == "p" and bool(dev["settings_file"]),
        ready_event=dev["ready_event"],
        run=dev["run"],
    )


def _warm_up(dev):
    """--warm-up: a hidden one-second run that loads what a first launch loads.

    Windows Defender, and Gatekeeper/XProtect on macOS, scan freshly written libraries
    the first time they load. With about a hundred of them (numpy's 21 MB math library
    the largest), the first real launch after an install took seconds to show a frame,
    and every launch after it was quick. Running this once right after copying the files
    moves that cost to install time. It also loads the settings dialog's Tk libraries.
    The run is timed, so it takes no single-instance lock and a real launch is never
    turned away; it reads the settings and writes nothing."""
    dev["seconds"] = WARM_UP_SECONDS if dev["seconds"] is None else dev["seconds"]
    dev["shot"] = None
    try:
        from digital_rain.windows import config_dialog  # noqa: F401  (loads the Tk libraries the dialog uses)
    except ImportError as exc:
        print(f"warm-up: dialog libraries not loaded: {exc}", file=sys.stderr)


def main():
    mode, hwnd, dev = parse_args(sys.argv[1:])
    global CHECK_HIDDEN
    CHECK_HIDDEN = dev["check_hidden"] or dev["warm_up"]
    if dev["warm_up"]:
        mode, hwnd = "s", None
        _warm_up(dev)
    if mode == "c":
        # Imported here: the dialog (tkinter) is not needed by any other mode.
        from digital_rain.windows import config_dialog

        osapi.make_dpi_aware()
        try:
            config_dialog.show(dev["dialog_shot"], dev["dialog_scale"], dev["dialog_size"])
        except RuntimeError as exc:
            print(f"dialog shot failed: {exc}", file=sys.stderr)
            sys.exit(2)
        return

    if dev["split_sim"] and dev["preview_sim"]:
        print("--split-sim and --preview-sim cannot be combined", file=sys.stderr)
        sys.exit(2)
    if mode == "w":
        return  # the removed wallpaper mode: nothing to draw
    if mode == "p" and hwnd is None:
        return  # preview requested without a window to draw into
    osapi.make_dpi_aware()
    if not _acquire_instance(mode, dev):
        return
    cfg = _load_config(dev)
    _apply_dev_overrides(cfg, dev)
    opts = _session_options(mode, hwnd, dev)
    while run_session(cfg, opts) == "restart":
        pass


if __name__ == "__main__":
    main()
