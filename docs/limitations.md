# Limitations and evaluation

This system assists a conductor. It doesn't replace one, and it makes mistakes. No accuracy figure is claimed here; measure it on your own labelled videos with the method below.

## Detection and tracking

- **Detection is not perfect.** YOLO can miss people (crowds, poor light, unusual poses, partial views at the door) and can report false detections (posters, reflections, objects that look like a person). Only the COCO person class is used; bags, tables, tools and vehicles are ignored as passengers, but a misclassified object can still appear as a person.
- **Confidence shown may be below the threshold for an already-tracked person.** A new person is accepted only at `detection.confidence` (default 0.35), but the tracker is fed boxes down to `tracking.input_confidence` (default 0.1) so an existing track survives brief occlusion. Those continuation boxes are drawn and recorded with their real, lower confidence.
- **ID switches, re-entry and occlusion.** ByteTrack keeps a lost track for `track_buffer` frames (60, about 2 s at 30 FPS). After a longer occlusion, a tracker failure, two people crossing closely, or a person leaving and re-entering, the same person can get a new Track ID. A new ID that crosses the entry line creates a new boarding event, so Boarding Events, Tracking IDs and Total Passenger can over-count. Raising `track_buffer` tolerates longer occlusion but raises the chance of two people swapping IDs. Placing the entry line where people are seen clearly and separately helps most.
- **Track IDs are per session.** ByteTrack numbers restart at 1 for every detection run, so Track ID 3 today and Track ID 3 yesterday are different people. Metrics count (session, Track ID) pairs.
- **Untracked boxes are not counted.** A detection without a ByteTrack ID is never recorded.

## Counting rules

- **Total Detection counts samples, not people or frames.** Each Track ID is recorded on first sight and then at most once per `detection.record_interval_sec` (default 1 s). A person standing in view for a minute produces about 60 samples.
- **`min_frames_seen`.** A crossing by a track seen in fewer than `boarding.min_frames_seen` frames is held as pending. It is confirmed if the track reaches that count within `boarding.entry_line.cooldown_seconds` while still on the entry side; otherwise it is dropped and no boarding is recorded. Very fast boardings or short-lived tracks can be missed this way.
- **Total Passenger overlap.** Total Passenger = verified passengers (distinct passenger refs with a valid ticket) + boarded Track IDs not linked to a valid ticket (verification required or failed). If a ticket is checked at the desk without selecting the passenger's boarding, and that boarding stays unlinked, the same person counts twice: once as a passenger ref and once as a Track ID. Select the boarding on the Ticket Checker page to link them.
- **Desk-only re-checks.** Without selecting a boarding, repeat checks of the same ticket on the same trip are treated as re-checks (VALID). Select the boarding to detect reuse.
- **Sessions past midnight.** A detection session that runs past local midnight keeps the trip_id of its start day. Dashboard and command-line re-checks inherit the trip_id from the selected boarding or the running session, so they stay consistent with the camera's checks.

## Tickets and QR codes

- **QR at a distance or under motion blur.** OpenCV's QR decoder needs a reasonably sharp, large enough code. A moving passenger or a small code far from the camera is often not read. A QR that isn't read never fails a ticket; the boarding stays "Ticket verification required" for the conductor to check.
- **Camera QR linking.** A QR in the camera frame is validated only when it is inside a boarded person's box, or, when it is inside no tracked person's box, when exactly one boarding happened within `qr.fallback_window_sec`. A QR held by a tracked person who has not boarded yet is ignored. With several people boarding together, an unlinked QR is ignored and the ticket must be checked on the Ticket Checker page.
- **Station is set per run.** The detection process uses the station it was started with. Moving to the next stop requires restarting it with `--station`, or using the station selector on the Ticket Checker page.
- **Re-printing a QR** requires rotating the token (`generate_qr.py --ticket-id X --rotate`), because only the token's hash is stored.

## Time

- **Wall-clock timestamps for video.** Recorded videos are timestamped with the time they are processed, not the time they were filmed, so their records appear under "Today". MOT export (`--export-mot`) uses frame numbers instead.

## Security and privacy

- **No dashboard sign-in.** The dashboard has no user accounts or access control: anyone who can reach it can check tickets and save evaluation runs. It binds to 127.0.0.1; add HTTPS, a reverse proxy and access control before exposing it on a network.
- **Metrics calculator.** Manual counts are entered by a user and are not verified model performance. F1 is N/A when Precision and Recall are both 0 in the manual calculator; the pipeline stores 0.0 by its documented methodology. mAP is not computed, and there is no end-to-end ticket decision evaluation (no verified ground-truth boarding records exist).
- No names, faces, images or contact details are stored. Passenger refs are pseudonymous. Plaintext QR tokens are never stored. The live preview frame (`data/runtime/latest_frame.jpg`) is overwritten continuously and is gitignored; treat it as camera footage.

## Dashboard

- **"Not responding" persists.** A detection process killed without cleanup leaves its session RUNNING. The header shows "Detection: Not responding (last heartbeat N ago)" until the next detection run; the dashboard never edits the database to fix it.
- **The header is the current state; alerts follow the period.** The header chip is the authoritative current detection state; session alerts ("Detection stopped", "Camera disconnected", ...) follow the selected period.
- **Console status is not a user check.** "Conductor console: Online/Offline" only says whether this console could read the database at the last refresh. The signed-in user's name and role are shown next to it; ticket checks are not attributed to a user.
- **Read state is per browser tab.** "Mark all as read" is kept only for the current browser session and resets on reload.
- **No start/stop from the dashboard.** Detection is a separate process with its own window; start it with `scripts\run_detection.bat` and stop it with `q` or Ctrl+C.
- **No PDF export.** Reports are CSV only.
- **Tracking failure alerts are narrow.** They are raised only for a failed session whose error mentions track/tracker/ByteTrack/Ultralytics/model. No detections for a while is not an alert (an empty bus looks the same).
- **Preview latency.** The live frame is about `runtime.preview_interval_sec` + the refresh interval old.
- **Responsive layout and accessibility.** Desktop, tablet and phone layouts (2 or 1 KPI columns, drawer sidebar) are CSS rules checked by tests for presence only; verify them in a browser. Every status is text plus colour and text colours meet 4.5:1, but full WCAG validation needs manual testing with assistive technologies and an expert accessibility review.

## Evaluation method

**Synthetic dataset metrics are not real-world accuracy.** The Dashboard calculator's "Dummy dataset (synthetic)" mode evaluates `data/dummy_evaluation_dataset.csv`, whose ground truth and `predicted_label` values are produced by a fixed-seed generator (`app/evaluation_dataset.py`), not by the camera or the YOLO model. Its Accuracy, Precision, Recall and F1 only show that the confusion-matrix and metric formulas work; they say nothing about how well the system detects passengers or tickets on a real bus. Real performance needs a genuinely labelled test set (manual or model evaluation modes).

The AI evaluation section on Reports shows only numbers from a stored, labelled evaluation (`model_evaluations`). No values are hardcoded, random, computed from ticket records, or taken from live unlabelled detections. Without a stored evaluation the section shows N/A and "Run model evaluation to calculate performance."

### Definitions (what `app.evaluate` computes)

- **Positive class:** person (COCO class 0). Ground-truth boxes of other classes are dropped.
- **Matching:** per image/frame, predictions sorted by confidence (descending) each take the unmatched ground-truth box with the highest IoU; IoU ≥ 0.5 (inclusive, `--iou`) is a true positive (TP) and consumes that box, otherwise a false positive (FP). Unmatched ground-truth boxes are false negatives (FN).
- **Precision** = TP / (TP + FP); **Recall (Sensitivity)** = TP / (TP + FN); **F1-Score** = 2 · Precision · Recall / (Precision + Recall). A zero denominator gives N/A; F1 is N/A when Precision or Recall is N/A, and 0 when both are 0.
- **Accuracy Score:** true negatives don't exist for boxes, so Accuracy is image-level person presence: an image is positive if it has at least one ground-truth person, predicted positive if the model reports at least one person; Accuracy = (TP + TN) / (TP + TN + FP + FN) over images. In `mot` mode it is N/A unless `--num-frames` gives the total frame count.
- **Averaging:** single class, counts pooled over all images (micro).
- **Ignored / distractor ground truth (strict rule):** MOT rows with conf 0 or a class outside `--gt-classes` (default 1,2,7), and non-person YOLO classes, are removed before matching, so a prediction on them counts as FP. MOTChallenge instead ignores such predictions; this rule can only lower Precision. It is recorded in `config_json.ignored_gt`.
- `images` mode evaluates the detection stage (`model.predict` with the pipeline's confidence and NMS IoU), not ByteTrack confirmation; use `mot` mode on `--export-mot` output for the tracked result.

### Running an evaluation (PowerShell, from the repository root)

1. Label representative images in YOLO format (CVAT, LabelImg or Roboflow export) into `data\eval\<name>\images` and `data\eval\<name>\labels` (gitignored; the images show people). A missing label file means "no persons in this image"; an empty labels folder is rejected.
2. Run and store:

   ```powershell
   scripts\run_evaluation.bat images --images data\eval\bus01\images --labels data\eval\bus01\labels
   ```

   or, for tracked video output against a MOT ground truth (CVAT "MOT 1.1" exports need `--gt-classes <person label index>`):

   ```powershell
   .venv\Scripts\python.exe -m app.main --source video --path data\videos\eval_01.mp4 --no-display --export-mot "$env:TEMP\pred.txt" --db "$env:TEMP\tpd_eval.db"
   scripts\run_evaluation.bat mot --gt data\eval\bus01\gt.txt --pred "$env:TEMP\pred.txt" --num-frames 900 --model models\yolov8n.pt
   ```

   or import counts from another tool (`model_name`, `model_version`, `dataset_name`, `test_set_size`, `tp`, `fp`, `fn`, `iou_threshold`, optional `image_counts`; metric values in the file are recomputed):

   ```powershell
   scripts\run_evaluation.bat import --json data\eval\results.json
   scripts\run_evaluation.bat list
   ```

   `--dry-run` prints without storing. Exit codes: 0 ok, 1 dataset/config, 2 arguments, 3 database, 4 model, 5 unexpected.

### Tracking and boarding evaluation (not stored)

- **ID switch (IDSW):** a ground-truth person whose matched Track ID changes between frames. MOTA and IDF1 summarise detection errors and identity consistency; compute them from the `--export-mot` output with `py-motmetrics` or TrackEval in a separate environment (they are not project dependencies).
- **Boarding precision / recall:** compare `boarding_events` of the evaluation database with a ground-truth list of real boardings (match within e.g. ±2 s).

Report the numbers you measure with the conditions they were measured in. Results on a few videos don't guarantee the same performance on other buses, cameras or lighting.
