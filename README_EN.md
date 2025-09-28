# VMAF Tool (`main.py`)

`main.py` combines a PySide6 desktop UI with a full CLI fallback to run VMAF
measurements via `ffmpeg`/`libvmaf`. It supports optional crop suggestions,
partial analysis windows, custom VMAF models, and a "Native" pipeline that
avoids extra format/scaling filters.

## Requirements

- Linux / macOS / WSL with Python 3.10 (tested with 3.10.12)
- `ffmpeg` **built with** `libvmaf`, plus `ffprobe` available in `$PATH`
- (GUI) X11 or Wayland with the Qt/XCB runtime libraries installed
- Optional: your own VMAF model JSON

## Quick Setup

```bash
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

`requirements.txt` reflects the current project virtual environment:

- PySide6 6.9.2 + shiboken6
- Matplotlib 3.10.6 (with numpy, pillow, …)

## Usage

### CLI (default)

```bash
source .venv/bin/activate
python main.py --dist dist.mp4 --ref ref.mp4 \
  --scale Native \
  --crop 3840:1600:0:280 \
  --json-out results/vmaf.json
```

Key options:

- `--scale {1080p,Native,WxH}` – choose the scaling preset. `Native` leaves the
  streams untouched.
- `--crop W:H:X:Y` – apply the same crop to reference and distorted. In Native
  mode the crop must be valid for both; if the distorted video already matches
  the target dimensions exactly, the crop is applied to the reference only.
- `--start/--end/--duration` – analyse a time window (seconds or HH:MM:SS(.ms)).
- `--model` – custom VMAF model file.
- `--subsample N` – evaluate every N-th frame only.
- `--plot` / `--json-out` – write a PNG chart / JSON result to disk.

### GUI

```bash
source .venv/bin/activate
python main.py --gui
```

The GUI provides:

- File pickers for reference/distorted videos and an optional model file
- Live display of the video dimensions (requires `ffprobe`)
- Spin boxes for the crop (W/H must be > 0; setting X/Y = 0 effectively means
  "auto/empty")
- Text fields for start/end (same formats as the CLI)
- Auto-crop via `ffmpeg cropdetect`
- Status bar, log panel, embedded Matplotlib plot, and a Cancel button that
  terminates the running `ffmpeg`

> **Linux/X11 note:** if Qt complains about missing libraries
> (`libxcb-cursor.so.0`, `libxkbcommon-x11.so.0`), install them via your package
> manager or set `QT_QPA_PLATFORM=wayland` when running on Wayland.

### Examples

1. **Native without crop** – only works when both streams share resolution and
   pixel format:
   ```bash
   python main.py --dist dist.mp4 --ref ref.mp4 --scale Native
   ```

2. **Native with crop** – reference is 3840×2160, distorted is 3840×1600. We cut
   280 px off the top/bottom:
   ```bash
   python main.py --dist dist.mp4 --ref ref.mp4 --scale Native --crop 3840:1600:0:280
   ```
   If the crop would exceed the distorted frame the tool automatically keeps the
   crop on the reference only (as long as the distorted stream already matches
   the crop size) and the analysis still runs.

3. **1080p pipeline with JSON + plot output**:
   ```bash
   python main.py --dist dist.mp4 --ref ref.mp4 \
       --scale 1080p --json-out out/vmaf.json --plot out/vmaf.png
   ```

### Auto-crop from the CLI

```bash
python main.py --suggest-crop 300 --ref ref.mp4
```

This analyses the first 300 frames of the reference stream and prints the best
crop candidate.

## Troubleshooting

- **`ffmpeg/libvmaf Fehler (rc=...)` / `ffmpeg/libvmaf error`** – inspect the
  status/log output. Typical reasons: ffmpeg without libvmaf, mismatching
  dimensions in Native mode, or incompatible pixel formats.
- **`Crop überschreitet…` / `Crop exceeds…`** – adjust the crop values or use a
  scaling preset. In Native mode the distorted stream is only cropped if the
  crop fits the frame entirely.
- **Qt fails to start / dimensions stay `—`** – ensure `ffprobe` is in `$PATH`
  and the required Qt system libraries are installed.
- **Cancel** – the cancel button sends SIGTERM and escalates to SIGKILL after a
  few seconds if ffmpeg refuses to exit.

## Development

- Main logic lives in `main.py`.
- Run parser/self-contained tests via `python main.py --selftest` (no external
  files needed).
- When editing the GUI, only destroy Qt objects via `deleteLater()` – the current
  thread/cleanup wiring depends on it.

Happy benchmarking!
