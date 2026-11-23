# Changelog

Notable changes to Digital Rain. Versions follow [semantic versioning](https://semver.org/).

## 1.0.1

### Fixed

- No grey screen when the saver starts or closes. On Windows the window now appears with its first frame and hides before it closes; on macOS the view stays black until the rain draws.

## 1.0.0

First public release.

### Windows

- `DigitalRain-Setup.exe`: a per-user installer that needs no administrator rights, upgrades in place, uninstalls from Settings > Apps and keeps your settings. It warms the saver up and offers to set it as the screensaver.
- A GPU renderer, with a software renderer as the fallback, and frames paced at each display's refresh rate.
- Per-monitor settings, and a settings window with a live preview. The screensaver picker's preview shows the rain live too.
- HDR output (`hdr`, `hdr_peak`) and optional AMD FSR 1.0 upscaling (`upscaling`, `sharpness`).
- `--warm-up`: a short hidden run after an install so the first real start is quick.

### macOS

- `DigitalRain.dmg`, holding `Digital Rain.saver`. It uses the same renderer as Windows and has every feature listed under Both.
- A native options sheet, opened from Options in System Settings, with the same six pages as the Windows settings window.

### Both

- Film look presets: `film_1999`, `reloaded` and `resurrections`, measured against film stills, with trail directions and the 1999 pulse.
- Live load mode: CPU or GPU load drives the rain's speed and density.
- A clock or text message formed by the rain, with a fixed clock in nine screen positions.
- A Letter font option for the rain message, the clock and the typed film messages.
- Pictures from a folder, shown over the rain or in it. Over the rain, a picture forms from its own characters. In the rain, the falling characters brighten to draw it, with depth.

### Releases

- Each release carries `DigitalRain-Setup.exe`, `DigitalRain.dmg` and `SHA256SUMS.txt`, with build provenance attestations.
- The saver shows as "Digital Rain"; its files and settings folder keep the name `MatrixRain`.
