# AI Ticketless Passenger Detection

Counts passengers boarding through a door using an Android phone camera, links each entry to a ticket check, and shows the results on a conductor dashboard.

```
Android phone (IP Webcam) -> OpenCV -> YOLO (person class only) -> ByteTrack -> Track ID
  -> entry-line crossing -> passenger event -> ticket verification (QR / manual) -> SQLite
  -> Streamlit conductor dashboard
```

The system gives estimates. Detection and tracking can miss people or count them twice when the door is crowded, the light is poor, or people are hidden behind each other. A `PENDING` or `NEEDS_CHECK` status means "the conductor should check this ticket". It does not prove anyone is ticketless. There is no facial recognition: a Track ID is an anonymous number that ByteTrack assigns to a moving person and only lasts for that session.

## Requirements

- Windows 10/11 with Python 3.10+ on PATH
- An Android phone running an IP camera app (e.g. "IP Webcam"), on the same Wi-Fi/hotspot as the laptop
- The first run downloads the YOLO weights (`yolov8n.pt`, ~6 MB), so you need internet access once

## Setup

```bat
scripts\setup_windows.bat
```

This creates `.venv`, installs dependencies (from `requirements.txt` if it exists, otherwise a pinned core set), and generates demo tickets in `data\tickets.json` plus QR images in `data\qr_codes\` if they are missing.

## Run

1. On the phone, start the IP camera server and note the URL. The default is `http://192.0.0.4:8080/video`. To confirm it works, open it in a laptop browser.
2. Terminal 1, detection:
   ```bat
   scripts\run_detection.bat
   ```
   A preview window shows the entry line, person boxes with Track IDs, and live counts. Press `q` to stop.
3. Terminal 2, dashboard:
   ```bat
   scripts\run_dashboard.bat
   ```
   Then open http://127.0.0.1:8501.

Useful detection options (pass them to `run_detection.bat` or `python -m app.main`):

| Option | Purpose |
|---|---|
| `--source URL\|0\|file.mp4` | Override the camera: phone URL, webcam index, or a video file |
| `--no-fallback` | Don't fall back to the laptop webcam if the phone is unreachable |
| `--no-display` | Run without the preview window |
| `--no-qr` | Disable QR ticket scanning in camera frames |
| `--conf 0.5` / `--model yolov8s.pt` / `--device cuda:0` | Detector settings |
| `--db PATH` / `--tickets PATH` | Database / ticket list location |
| `--max-frames N` | Stop after N frames (for testing) |

Dashboard options go after `--`, for example `scripts\run_dashboard.bat --db data\passengers.db`.

## Dashboard

- Metrics: total passenger events, Verified, Pending, Needs check. These auto-refresh every 2 s by default.
- Event table columns: Event ID, Track ID, entry time (local), exit time, ticket ID, ticket status (`VALID`, `INVALID`, `USED`, `NOT_FOUND`, or `NOT SCANNED`), and passenger status.
- Conductor actions:
  - Enter or scan a ticket ID for an event. The result is `VALID` → VERIFIED, or `INVALID`/`USED`/`NOT_FOUND` → NEEDS_CHECK. A ticket that is already linked to another passenger shows as `USED`.
  - Mark an event NEEDS_CHECK.
  - Record an exit. An event still PENDING at exit becomes NEEDS_CHECK.

The dashboard has no login and `run_dashboard.bat` binds it to `127.0.0.1` only. Don't expose it on a network without adding authentication.

## How tickets get linked

- Automatic: when a ticket QR code shows up in the camera frame, `app.main` decodes it. If the QR is inside a tracked person's box and that person has an entry event, the ticket is linked to that event. If the QR is not inside any box, it is linked only when exactly one entry happened in the last 30 s. Otherwise it is ignored.
- Manual: the conductor enters the ticket ID on the dashboard.

To regenerate demo tickets: `python -m app.ticket.generate_test_tickets`.

## Configuration

`config/config.yaml` has a `camera:` section (source URL, webcam fallback, timeouts). `app.main` also reads these optional sections. If a section is missing, the module defaults apply.

```yaml
tracking:        # TrackerConfig: model_path, tracker (bytetrack.yaml), conf, iou, imgsz, device
  conf: 0.4
entry_line:      # EntryLineConfig: orientation, position (0..1), entry_direction, cooldown_seconds, ...
  orientation: horizontal
  position: 0.5
  entry_direction: top_to_bottom
database:
  path: data/passengers.db
tickets:
  path: data/tickets.json
qr:
  enabled: true
  scan_every_n_frames: 3
```

The default entry line is horizontal across the middle of the frame. An entry is counted when a person's foot point crosses it from top to bottom. Mount the phone so boarding passengers move in that direction, or change `entry_line` to match.

## Project layout

```
app/camera/      video source (phone URL, webcam fallback, reconnect)
app/detection/   YOLO person-only detector
app/tracking/    YOLO + ByteTrack Track IDs
app/passenger/   entry-line crossing detection
app/database/    SQLite models + repository
app/ticket/      QR scanning, ticket validation, demo ticket generator
app/dashboard/   Streamlit conductor dashboard
app/main.py      integration / detection entry point
scripts/         Windows setup and run scripts
tests/           unit tests (python -m pytest)
```

## Troubleshooting

- "Could not connect to the phone camera stream": the phone and laptop must be on the same network, and the IP camera server must be running. Check the IP shown in the app and update `camera.source`. By default the app falls back to the laptop webcam.
- Low FPS: YOLO runs on the CPU by default. Use `--device cuda:0` with a CUDA build of PyTorch, or lower `tracking.imgsz`.
- "Ticket list not found": run `python -m app.ticket.generate_test_tickets`.
- The SQLite DB uses WAL mode, so the detection process and the dashboard can run at the same time.
