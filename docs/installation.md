# Installation

## Requirements

- Windows 10 or 11 (the batch files are Windows-only; the Python code also runs elsewhere with the manual steps below)
- Python 3.11 (64-bit) from https://www.python.org/downloads/. The setup script refuses other versions.
- No GPU is required. Inference runs on the CPU by default (`detection.device: cpu`).
- For the phone camera: an Android phone with an IP camera app such as "IP Webcam", on the same network as the computer.

## Automatic setup

From the project folder:

```bat
scripts\setup_windows.bat
```

The script:

1. finds Python 3.11 with `py -3.11`, or `python` if that is 3.11; otherwise it prints "Python 3.11 is required" and stops;
2. creates `.venv` if it doesn't exist (an existing `.venv` must be Python 3.11; delete it to rebuild);
3. runs `pip install -r requirements.txt` and `pip check`;
4. runs `scripts\init_db.py`, which creates the tables in `data\ticketless.db` and prints the row counts;
5. checks for `models\yolov8n.pt` and prints the download command if it is missing.

It does not upgrade pip and does not download the model.

## Manual setup

```bat
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip check
.venv\Scripts\python.exe scripts\init_db.py
```

### CPU-only PyTorch

`requirements.txt` pins `torch==2.14.0` and `torchvision==0.29.0`. On a machine without CUDA you can take the smaller CPU builds from the PyTorch index:

```bat
.venv\Scripts\python.exe -m pip install -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cpu
```

A CPU build reports itself as `2.14.0+cpu`, which satisfies the `==2.14.0` pin.

### GPU (optional)

Install a CUDA build of the same torch and torchvision versions from the PyTorch index for your CUDA version, then run detection with `--device auto` (uses `cuda:0` when available) or `--device cuda:0`. If CUDA isn't available, `cuda:N` falls back to the CPU with a warning.

## Model download

The YOLO weights are not in the repository. Expected path: `models\yolov8n.pt`.

```bat
cd models
..\.venv\Scripts\python.exe -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"
cd ..
```

Details and alternatives: [models/README.md](../models/README.md). The detection process never downloads the model during a run; a missing file stops it with exit code 4.

## Configuration

- `config/config.yaml` holds every setting, with defaults and ranges in the comments.
- Copy `.env.example` to `.env` to override the main settings (`TPD_*` variables) without editing the YAML. `.env` is gitignored.
- Command-line flags override both for one run.

## Pinned versions

All dependencies are pinned with `==` in `requirements.txt`:

| Package | Version | Used for |
|---|---|---|
| ultralytics | 8.4.165 | YOLO detection and bundled ByteTrack |
| torch / torchvision | 2.14.0 / 0.29.0 | Inference backend |
| lap | 0.5.13 | Assignment solver used by ByteTrack |
| opencv-python | 5.0.0.93 | Video capture, drawing, QR decoding |
| numpy | 2.4.6 | Arrays |
| streamlit | 1.64.0 | Dashboard (`st.fragment` auto-refresh, multipage) |
| pandas | 3.0.6 | Dashboard tables |
| PyYAML | 6.0.3 | Config |
| qrcode / pillow | 8.2 / 12.3.0 | QR PNG generation |
| tzdata | 2026.4 | Time zones for `zoneinfo` on Windows |
| pytest | 8.3.3 | Tests |

These versions were installed and checked together in the project venv: `pip install --dry-run -r requirements.txt` changes nothing and `pip check` reports no broken requirements. If you change one pin, run `pip check` and the tests (`scripts\run_tests.bat`) again. SQLite comes with Python (`sqlite3`); no database server is needed. QR scanning uses OpenCV only, so no zbar DLL is required.

## Verify the installation

```bat
scripts\run_tests.bat
.venv\Scripts\python.exe scripts\generate_dummy_data.py
scripts\run_dashboard.bat
```

The tests don't need the model or a camera. The demo data lets you check the dashboard before connecting a camera.
