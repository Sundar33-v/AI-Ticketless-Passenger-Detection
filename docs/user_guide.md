# User guide (conductor)

The system helps you notice boardings and check tickets. It never decides that a passenger has no ticket. A boarding without a checked ticket shows as **Ticket verification required**, which means "please check this passenger's ticket".

## Before the trip

1. Mount the phone (or webcam) so boarding passengers cross the entry line in the configured direction. The default line is horizontal across the middle of the frame, and boarding is top to bottom (`boarding.entry_line` in `config/config.yaml`).
2. Start the IP camera app on the phone. The phone and the computer must be on the same Wi-Fi or hotspot.
3. Set the station and trip for this run (see [Station and trip per run](#station-and-trip-per-run)).
4. Start detection in one terminal:

   ```bat
   scripts\run_detection.bat --station "Central Bus Stand"
   ```

   The window shows person boxes with `ID n` and confidence, the entry line, session counts and the last messages. Press `q` to stop.
5. Start the dashboard in a second terminal and open http://127.0.0.1:8501:

   ```bat
   scripts\run_dashboard.bat
   ```

## Station and trip per run

- The route (`route.route_id`, `route.stations`) and direction (`journey.direction`: `outbound` uses the station list in order, `inbound` reverses it) come from `config/config.yaml`.
- The current station is `journey.current_station`, overridden per run with `--station NAME` or `TPD_CURRENT_STATION`. It must be one of the route's stations, otherwise detection stops with exit code 1. With no station set, station checks are skipped and only the ticket's route, direction and validity are checked.
- The trip ID is `--trip-id`, else `journey.trip_id`, else `<route_id>-<direction>-<date>` (for example `R1-outbound-2026-01-15`). It is fixed when detection starts. A ticket is bound to the trip on its first valid check, so the same ticket on another trip is reported as Already used.
- The running detection process keeps its station for the whole run. To change station, stop it with `q` and start it again with the new `--station`, or set the station on the Ticket Checker page for desk checks.

## Dashboard tour

Start it with `scripts\run_dashboard.bat` and open http://127.0.0.1:8501.

The dashboard opens directly; there is no sign-in.

The Dashboard page reads top to bottom as follows.

**Top bar:** the product name with a bus icon, the local date and time, your name and role with "Online" or "Offline" (whether this browser could read the database at the last refresh) and the alert bell with the unread count (`99+` above 99). Hover the conductor status to see the detection status, which is read from the latest detection session:

| Header text | Meaning |
|---|---|
| Detection: Online | A detection session is RUNNING and sent a heartbeat within `runtime.heartbeat_stale_sec` |
| Detection: Not responding (last heartbeat N ago) | The session says RUNNING but no heartbeat arrived in time (camera reconnecting, or the process was killed). It stays until the next detection run |
| Camera: Disconnected | The last session failed because the camera or video could not be opened or was lost |
| Detection: Failed | The last session failed for another reason (see Live Feed) |
| Detection: Stopped | The last session ended normally |
| Detection: Not started | No detection session in this database yet |

The tooltip reads "Conductor console: Online · <detection status>" while the database is readable, or "Conductor console: Offline – database unavailable" when it is not.

**Sidebar:** the six pages **Dashboard**, **Live Feed**, **Passenger Tracking**, **Ticket Checker**, **Reports** and **Settings** (the current page is highlighted), a collapsed **Filters & data** section and the tagline "Smart Detection for Safer Journeys" at the bottom. On a phone the sidebar collapses into a drawer (the arrow at the top left).

**Filters & data** (open it to change the filters): Period (Today, Last 24 hours, Yesterday, Last 7 days, All time, Custom range), Validation status (filters the tables and the table CSV exports only; metrics, charts and alerts always count every outcome), the database path, and "Demo data: N synthetic rows present" when demo rows exist. The filters stay the same when you switch pages. "Last 24 hours" is a rolling window that moves with every refresh; all other periods use local midnights in `app.timezone`.

**Banner:** "Welcome, Conductor!", a status pill and the route card. The pill says "System Online" only while a detection session is RUNNING with a fresh heartbeat; otherwise it shows the detection status from the table above. The route card shows `route_id | first station → last station` in the journey direction (from `config/config.yaml`), or "No route configured".

**Five KPI cards** for the selected period (hover a card for its definition):

- Valid ("Tickets verified successfully"): distinct tickets with a valid check
- Invalid ("Ticketless / Invalid QR"): distinct credentials that were Invalid, Wrong route or Already used
- Total Passenger: verified passengers (distinct passenger refs with a valid ticket) + boarded Track IDs not linked to a valid ticket (verification required or failed)
- Total Detection ("People detected by AI"): distinct Track IDs seen, per detection session
- Expired Tickets: distinct tickets that failed because they had expired

Each card shows the change "vs previous period": Today is compared with yesterday up to the same clock time, Yesterday, Last 24 hours, Last 7 days and custom ranges with the equally long span just before them. All time and custom ranges that start in the future have no comparison and show "—". More Invalid or Expired tickets counts as a worse change. Boarding Events and the recorded detection samples are on Passenger Tracking and Reports. When all metrics are 0, the page suggests starting detection or loading the demo data; when the database has records in other periods, it suggests choosing All time or re-running the demo data script to move the demo data to today.

**Passenger Trend (Last 10 Min):** per minute, the people detected (distinct Track IDs) and the failed ticket checks (Invalid, Wrong route, Already used) of the last 10 minutes. This chart ignores the Period filter.

**Ticket Status:** a donut of the distinct credentials checked in the selected period, split into Valid, Invalid (including Wrong route and Already used), Expired and Unverified (Ticket verification required), with counts and percentages next to it.

**Live Feed card:** the latest processed frame while detection is online, otherwise a message for the detection state (waiting for a frame, stopped, not responding, camera disconnected, preview disabled). Below it is the latest failed ticket check of the period (Invalid, Wrong route, Already used or an unrecognised QR code). "Ticketless Passenger Detected!" appears only after a completed failed check; a boarding that has not been checked yet never shows it. **Notify Conductor** marks that one alert as read (saved, see [Notification bell](#notification-bell)). It sends nothing (there is no external notification). **View Tracking** opens Passenger Tracking.

**Recent Validation Results:** the newest checks of the selected period, following the Validation status filter. Choose Last 10, 25 or 50 records and search by ticket ID, Track ID or channel code (camera, dashboard, cli, demo). There is no paging; the full list and its CSV are on Reports.

**Quick Actions:** Scan Ticket (QR) opens Ticket Checker, View Live Feed, Track Passenger and Generate Report (opens Reports with the same period). The "AI Powered" badge below them points to the metrics calculator below.

**AI Model Performance & Metrics Calculator** (below Recent Validation Results and Quick Actions; it does not refresh by itself, so typed values stay):

- Four cards: Accuracy Score (%), Precision (%), Recall (%) and F1-Score (%), shown with 2 decimals, plus the evaluation status, total evaluated cases, date/time, model version and dataset. N/A means the metric cannot be calculated (its denominator is 0). With no evaluation you see "No evaluated dataset available. Enter verified evaluation counts or run model evaluation."
- **Evaluation mode** selector: "Dummy dataset (synthetic)", "Manual evaluation" or "Model evaluation results". The synthetic mode is the default unless a model evaluation is stored.
- **Dummy dataset (synthetic):** the cards are calculated from `data/dummy_evaluation_dataset.csv` (label 1 = passenger without a valid ticket, 0 = valid-ticket passenger). Below the cards you see the dataset name, a "Synthetic test data" marker, total samples, TP/TN/FP/FN with the check TP + TN + FP + FN = total, the evaluation date/time and the method. Press **Calculate / Refresh Metrics** to re-read the file and **Regenerate dummy dataset** to rewrite it with the fixed-seed generator (seed 20260115, 200 samples). If the file is missing, open in another program or malformed, an error explains why and the cards show N/A; close the file or regenerate it. A file with rows that are not marked synthetic (`SYN-`) is never overwritten. These numbers are synthetic and are not real-world accuracy, and they are never saved.
- **Manual evaluation:** type the True Positive, True Negative, False Positive and False Negative counts from a labelled test dataset (whole numbers, 0 or more) and press **Calculate Metrics**. These are evaluation counts, not the ticket counts on this page, and the result is labelled "Manual evaluation" (it is not verified model performance). **Reset** clears the fields. **Save Evaluation** needs a dataset description and stores the run; nothing is saved automatically, and saving the same values twice is refused. Calculate before switching mode to keep your counts.
- **Model evaluation results:** the latest result of `scripts\run_evaluation.bat` on a labelled dataset. mAP and an end-to-end ticket decision evaluation are not available.
- **Recent Evaluation Runs:** saved manual runs and pipeline results, newest first, with their counts.

The evaluation history is also on Reports, the detection source sentence and the full alert list are on Live Feed, recent boardings are on Passenger Tracking, and the outcome legend with its shares is on Reports. The page refreshes itself every `dashboard.refresh_sec` seconds.

## Live Feed page

Shows the configured source (phone URL without credentials, webcam index, video file name), the commands to start and stop detection (the dashboard cannot start or stop it), the latest processed frame while detection is online (boxes and Track IDs are drawn by the detection process, people only), a clear panel when the camera is disconnected or detection is stopped, the live Track IDs of the running session, and all alerts.

## Passenger Tracking page

Total Passenger, Boarding Events and Tracking IDs; recent boardings; detection sessions with their state and boardings; and a Track ID lookup (choose the Track ID and the session, because Track IDs restart every run).

## Reports page

The selected period's metrics, failure reasons, outcome distribution and the latest AI evaluation with its history. Downloads: summary CSV, validations CSV and boardings CSV (UTF-8, opens in Excel), built from the same queries as the screen. PDF export is not available.

## Settings page

Read-only effective settings (detection, camera, route, alerts, dashboard, runtime, schema version). Secrets and full paths are not shown. Change values in `config/config.yaml` and restart the affected process.

## Alerts

| Alert | Meaning | What to do |
|---|---|---|
| Ticket verification required for Track ID n (boarded hh:mm). Please check the passenger's ticket. | The boarding has had no ticket check for more than `alerts.verification_timeout_sec` seconds | Check the passenger's ticket and record it on Ticket Checker, selecting this boarding |
| Ticket verification failed for Track ID n: Invalid – ... | No ticket record, malformed credential, cancelled ticket, or not valid yet | Ask for another ticket; recheck on Ticket Checker |
| ... Expired – Ticket expired at ... | The ticket's validity window has ended | Follow your operator's rules |
| ... Wrong route – ... | Other route, opposite direction, bus before the boarding station or past the destination | Check the ticket's stations |
| ... Already used – ... | The ticket was used on another trip or for another boarding on this trip | Check whether the ticket was shared |

Alerts are grouped: repeated checks of the same ticket for the same boarding (or at the desk) show as one alert with "×N"; every underlying record stays in the database and is listed under **Details**. Other alert kinds: "Unrecognised QR code", "Ticket verification required" (boarded, not checked after the timeout), "Camera disconnected", "Tracking failure", "Detection failed", "Detection not responding" and "Detection stopped" (information only). A person who is detected but has not boarded is never alerted.

### Notification bell

The bell in the top bar counts unread error and warning alerts of the selected period (`99+` above 99; `–` when the database can't be read, `…` while loading). Click it, or press Enter/Space on it, to open the notification panel; click it again, click outside the panel or press Escape to close it. Opening or closing the panel never changes anything else on the page.

The panel lists up to 15 alerts, unread first, each with its title, message, time, Track ID and ticket ID when known, the number of underlying events, its type (Ticket check, QR scan, Boarding, Detection) and its state (Unread, Read or Info). Long lists scroll under the panel heading. Only stored events are shown: failed ticket checks, unrecognised QR codes, boardings not verified in time and detection problems. Successful checks stay in Recent Validation Results. "Detection stopped" items are information only and are never counted.

**Mark as read** (one alert) and **Mark all as read** (every alert of the period, including the ones not shown) are saved in the database (`alert_reads`), so the read state survives a reload, a restart and other browser tabs (within one refresh). **Mark all as read** on Live Feed and **Notify Conductor** on the Dashboard save the same way. A new failure in an alert group makes it unread again. If the database is busy, the alert is marked read for this browser session only and the panel says so.

Keyboard: after opening, focus moves to the panel heading; Tab then reaches Mark all as read, each Mark as read and the Live Feed link in order, and Escape returns to the bell. The panel has no focus trap. Each Mark as read button follows the alert it belongs to (Streamlit buttons can't carry their own label). New alerts are not announced live; only the bell's screen-reader text changes.

After upgrading, restart the dashboard once so its database connection creates the `alert_reads` table. Until then read status is kept for the browser session only.

An alert is a prompt to check, not proof of anything about the passenger. Tracking can mistake one person for two (see [limitations](limitations.md)).

## Ticket Checker page

Open **Ticket Checker** in the page navigation. Its address is now http://127.0.0.1:8501/ticket-checker; update old bookmarks to `/Ticket_Check`.

1. **Ticket presented as**: Scan QR image (upload a PNG/JPG up to 5 MB), Enter token, or Enter ticket ID.
2. **Boarding event (optional)**: the period's boardings that are not yet Valid, newest first, shown as `Track ID n – hh:mm – <status>`. Pick the boarding that belongs to this passenger. Leave it at "(no boarding – desk check)" for a check that isn't tied to a camera boarding.

   Without selecting a boarding, repeat checks of the same ticket on the same trip are treated as re-checks (VALID). Select the boarding to detect reuse.
3. **Current station**: the station for this check. The first option "(not set – skip station checks)" skips the position checks.
4. **Record that no ticket was shown**: tick this and submit without a QR image, token or ticket ID to record a check with outcome "Ticket verification required". If a credential is entered, the credential is checked instead.
5. Press **Check ticket**. The result shows as green (Valid), amber (Ticket verification required) or red (any failure), with the reason and the ticket ID.

Submitting with nothing entered and the box unticked records nothing. "Database busy, try again" means the detection process held a lock; submit again.

### Resolving a failed verification

A boarding that ended as Invalid, Expired, Wrong route, Already used or Ticket verification required stays in the boarding selector. To resolve it:

1. Select that boarding.
2. Check the passenger's valid ticket.
3. The boarding becomes Valid and leaves the selector. The "Ticket verification required" alert for it stops; earlier failed-check alerts stay in the list as a record of what happened.

Rules: a Valid boarding is never changed by a later failed check (the check is still recorded and alerted). Recording "no ticket shown" never changes a boarding's status. Any other result replaces a non-valid status.

Linking the boarding also keeps Total Passenger correct: a desk check that isn't linked to the passenger's boarding counts the same person once as a verified passenger and once as an unlinked Track ID.

## Command-line checks

For checks without the dashboard:

```bat
.venv\Scripts\python.exe scripts\validate_qr.py --image data\qr_codes\TKT-1A2B3C4D.png --station "City Hospital"
.venv\Scripts\python.exe scripts\validate_qr.py --ticket-id TKT-1A2B3C4D --boarding-id 12
```

It prints the outcome and reason and exits 0 for Valid, 10 for any other outcome.

## Issuing tickets

```bat
.venv\Scripts\python.exe scripts\generate_qr.py --boarding "Market Road" --destination "Airport"
```

Prints the ticket ID, the pseudonymous passenger ref and the PNG path (`data\qr_codes\<ticket_id>.png`). To replace a lost or leaked QR: `--ticket-id <id> --rotate`; the old QR stops working.

## After the trip

Press `q` in the detection window (or Ctrl+C). The session is closed as STOPPED and the dashboard shows "Detection is not running". The data stays in `data\ticketless.db` for later review with the Period filter.
