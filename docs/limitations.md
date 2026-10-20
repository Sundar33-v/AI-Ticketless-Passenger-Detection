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
- **Total Passenger overlap.** Total Passenger = verified passengers (distinct passenger refs with a valid ticket) + boarded Track IDs not linked to a valid ticket (verification required or failed). If a ticket is checked at the desk without selecting the passenger's boarding, and that boarding stays unlinked, the same person counts twice: once as a passenger ref and once as a Track ID. Select the boarding on the Ticket Check page to link them.
- **Desk-only re-checks.** Without selecting a boarding, repeat checks of the same ticket on the same trip are treated as re-checks (VALID). Select the boarding to detect reuse.
- **Sessions past midnight.** A detection session that runs past local midnight keeps the trip_id of its start day. Dashboard and command-line re-checks inherit the trip_id from the selected boarding or the running session, so they stay consistent with the camera's checks.

## Tickets and QR codes

- **QR at a distance or under motion blur.** OpenCV's QR decoder needs a reasonably sharp, large enough code. A moving passenger or a small code far from the camera is often not read. A QR that isn't read never fails a ticket; the boarding stays "Ticket verification required" for the conductor to check.
- **Camera QR linking.** A QR in the camera frame is validated only when it is inside a boarded person's box, or, when it is inside no tracked person's box, when exactly one boarding happened within `qr.fallback_window_sec`. A QR held by a tracked person who has not boarded yet is ignored. With several people boarding together, an unlinked QR is ignored and the ticket must be checked on the Ticket Check page.
- **Station is set per run.** The detection process uses the station it was started with. Moving to the next stop requires restarting it with `--station`, or using the station selector on the Ticket Check page.
- **Re-printing a QR** requires rotating the token (`generate_qr.py --ticket-id X --rotate`), because only the token's hash is stored.

## Time

- **Wall-clock timestamps for video.** Recorded videos are timestamped with the time they are processed, not the time they were filmed, so their records appear under "Today". MOT export (`--export-mot`) uses frame numbers instead.

## Security and privacy

- **No dashboard authentication.** The dashboard binds to 127.0.0.1 and has no login. Anyone using this computer can open it, and the Ticket Check page can write validation results. Add authentication before exposing it on a network.
- No names, faces, images or contact details are stored. Passenger refs are pseudonymous. Plaintext QR tokens are never stored. The live preview frame (`data/runtime/latest_frame.jpg`) is overwritten continuously and is gitignored; treat it as camera footage.

## Evaluation method

### Definitions

Match detections to ground-truth boxes frame by frame at IoU ≥ 0.5 (each ground-truth box matches at most one detection).

- **True positive (TP):** a detection matched to a ground-truth person.
- **False detection (false positive, FP):** a detection with no matching ground-truth person, including non-person objects detected as a person.
- **False negative (FN):** a ground-truth person with no matching detection.
- **Precision** = TP / (TP + FP): of the people the system reports, the share that are real.
- **Recall** = TP / (TP + FN): of the real people, the share the system finds.
- **ID switch (IDSW):** a ground-truth person whose matched Track ID changes from one frame to a later frame. Lower is better.
- MOTA and IDF1 summarise detection errors and identity consistency together; report them alongside the counts above.
- **Boarding precision / recall:** the same formulas applied to boarding events against a list of real boardings (a predicted boarding matches a real one if it is within a small time window, for example ±2 s).

### Procedure

1. **Label 3–5 representative videos** (different light, crowd levels, camera positions) with a tool such as CVAT or LabelImg. Export person boxes with persistent identities in MOTChallenge `gt.txt` format (`frame,id,x,y,w,h,...`, 1-based frames), and write a ground-truth list of boarding events (person and frame or time of each real boarding).
2. **Run the system on each video** without the window, into a separate database:

   ```bat
   .venv\Scripts\python.exe -m app.main --source video --path data\videos\eval_01.mp4 --no-display --export-mot out_01.txt --db %TEMP%\tpd_eval.db
   ```

   `out_01.txt` has one line per tracked person per frame: `frame,id,x,y,w,h,conf,-1,-1,-1` with 1-based frames.
3. **Compute the tracking metrics** (per-frame matching at IoU ≥ 0.5, precision, recall, false detections, ID switches, MOTA, IDF1) with `py-motmetrics` or TrackEval. Install them in a separate environment; they are not project dependencies.
4. **Compare boarding events** from the `boarding_events` table of the evaluation database against the ground-truth boardings to get boarding precision and recall.
5. **Report** the results per video and in aggregate, together with the settings used. Tune `detection.confidence`, `track_buffer` in `config/bytetrack.yaml` and the entry line (`boarding.entry_line`), then repeat from step 2. Keep a held-out video that is not used for tuning.

Report the numbers you measure with the conditions they were measured in. Results on a few videos don't guarantee the same performance on other buses, cameras or lighting.
