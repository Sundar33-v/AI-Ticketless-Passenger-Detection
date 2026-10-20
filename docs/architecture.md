# Architecture

Two processes share one SQLite database:

- the **detection process** (`python -m app.main`, `scripts\run_detection.bat`) reads video, detects and tracks people, records detection samples and boarding events, and validates QR codes linked to a boarding;
- the **dashboard** (`streamlit run dashboard/app.py`, `scripts\run_dashboard.bat`) reads the database and writes only when the conductor submits the Ticket Check form.

Scripts (`scripts/*.py`) and tests use the same `app` modules. Business rules exist once and are called by every entry point.

## Module map

| Module | Responsibility |
|---|---|
| `app/__init__.py` | Package marker, `__version__` |
| `app/settings_schema.py` | `ConfigError`, the config dataclasses per section with type and range validation, camera constants (`SOURCE_TYPES`, `VIDEO_EXTENSIONS`, `DEFAULT_STREAM_URL`). Imports nothing from `app`. |
| `app/config.py` | `PROJECT_ROOT`, `load_config()` (YAML, then `.env`, then `TPD_*` environment), `resolve_path()`, `resolve_db_path()`, `configure_logging()` |
| `app/database.py` | `Database` (`connect()`, `read_connection()`, `initialize()`), `transaction()`, the schema DDL, `utc_now()`, `to_iso()`, `parse_iso()` |
| `app/video_source.py` | `VideoSource` for phone, webcam and video; `check_video_file()`, `choose_video_file()`, reconnects, URL redaction, error help texts |
| `app/detector.py` | Model loading (never auto-downloads), device resolution, person-only box parsing (`PERSON_CLASS_ID = 0`) |
| `app/tracker.py` | `PersonTracker` around `YOLO.track` with ByteTrack, track bookkeeping (`hits`, `first_seen_frame`, `max_confidence`), effective ByteTrack YAML |
| `app/entry_line.py` | Entry-line geometry and crossing detection (pure, no I/O) |
| `app/passenger_service.py` | The only session, detection and boarding SQL; `DetectionThrottle`, `BoardingRules`, `PassengerService`, `delete_synthetic()` |
| `app/qr_service.py` | Token creation and hashing, `TPD1:` payloads, QR PNG writing, OpenCV QR decoding |
| `app/ticket_validator.py` | `Outcome`, `build_journey()` (the only `JourneyContext` builder), pure `evaluate_ticket()`, transactional `TicketValidator.validate()`, `issue_ticket()`, `cancel_ticket()`, `rotate_token()`, `get_ticket()`. All ticket, passenger and validation SQL. |
| `app/alert_service.py` | Alert wording and labels, `verification_alerts()` for the dashboard, `AlertService` for the pipeline overlay and log |
| `app/analytics.py` | `Period`, `period_for()`, `compute_metrics()` (the seven metrics), table queries, `source_status()`, `boardings_needing_resolution()` |
| `app/pipeline.py` | `DetectionPipeline.process_frame()`: tracker, throttle, boarding, QR link, validator, alerts, MOT export, heartbeat and preview |
| `app/overlay.py` | OpenCV drawing of boxes, IDs, entry line and messages |
| `app/main.py` | CLI parsing, source overrides, build, run loop, exit codes |
| `dashboard/app.py` | Main page: filters, seven metrics, source status, alerts, two tables |
| `dashboard/pages/1_Ticket_Check.py` | Ticket Check form (the only dashboard write path) |
| `dashboard/components/` | `data.py` (cached `Database` per resolved path, read and write connections), `filters.py`, `metrics.py`, `status.py`, `tables.py` |
| `scripts/` | `init_db.py`, `generate_qr.py`, `validate_qr.py`, `generate_dummy_data.py`, `_bootstrap.py` (puts the project root on `sys.path`), and the four `.bat` files |

### Import direction

The graph is acyclic:

```
settings_schema <- config <- database <- {passenger_service, ticket_validator, analytics, alert_service}
config, settings_schema <- video_source
qr_service <- ticket_validator
detector <- tracker
entry_line <- passenger_service
everything <- pipeline <- main
```

- `ticket_validator` never imports `passenger_service`. It updates only the `verification_status`, `passenger_ref`, `ticket_id` and `last_validation_id` columns of a boarding row; `passenger_service` owns boarding inserts and deletes.
- `app` never imports `streamlit`.
- The dashboard imports `config`, `database`, `analytics`, `alert_service`, `ticket_validator` and `qr_service`. It never imports `ultralytics` or opens a `VideoCapture`.

### Configuration

`config/config.yaml` documents every key with its default and range. Layering, lowest to highest: built-in defaults, the YAML (`--config` or `TPD_CONFIG`), `.env`, process environment (`TPD_SOURCE`, `TPD_PHONE_URL`, `TPD_CAMERA_INDEX`, `TPD_VIDEO_PATH`, `TPD_MODEL_PATH`, `TPD_DEVICE`, `TPD_DB_PATH`, `TPD_LOG_LEVEL`, `TPD_CURRENT_STATION`), command-line flags. Invalid values raise `ConfigError` with the dotted key (`detection.confidence: ...`), and the process exits with code 1. Relative paths are resolved against the project root.

## Database schema

One file, default `data/ticketless.db`, resolved by `resolve_db_path()`: `--db` > `TPD_DB_PATH` > `database.path` > `data/ticketless.db`. Only `.db`, `.sqlite` and `.sqlite3` are accepted.

Connections: `Database.connect()` sets `foreign_keys=ON`, `busy_timeout`, `journal_mode=WAL`, `synchronous=NORMAL`, autocommit mode and `sqlite3.Row`. `read_connection()` opens the file with `mode=ro`, so the dashboard's refresh can't write. `transaction(conn, immediate=True)` runs `BEGIN IMMEDIATE` ... `COMMIT`, rolls back on any exception, and refuses to nest. Whoever opens a connection closes it; connections are never shared across threads.

Timestamps are `TEXT` in the fixed-width format `YYYY-MM-DDTHH:MM:SS.ffffffZ` (UTC), produced only by `to_iso()`, so string comparison is chronological. `travel_date` is a local `YYYY-MM-DD`. Booleans are `INTEGER` 0/1. `is_synthetic` marks demo rows in every table.

| Table | Key columns | Notes |
|---|---|---|
| `system_settings` | `key`, `value`, `updated_at` | `schema_version = 1`, `created_at`. A newer version stops the program. |
| `passengers` | `passenger_ref` (4-64 chars) | Pseudonymous reference only. |
| `tickets` | `ticket_id`, `passenger_ref`, `route_id`, `boarding_station`, `destination_station`, `travel_date`, `valid_from`, `expires_at`, `status` (ACTIVE/USED/CANCELLED), `qr_token_hash` (unique sha256), `used_at`, `used_trip_id`, `used_boarding_id` | Checks: boarding <> destination, `expires_at > valid_from`. Indexes on passenger, status, travel date. |
| `detection_sessions` | `session_id`, `source_type` (phone/webcam/video/demo), `source_label` (redacted URL, `webcam N` or file name), `status` (RUNNING/STOPPED/FAILED), `started_at`, `last_heartbeat_at`, `ended_at`, `frames_processed`, `fps`, `station`, `trip_id`, `last_error` | The system status and heartbeat. |
| `detection_records` | `session_id`, `track_id`, `frame_index`, `detected_at`, `confidence`, `x1..y2` | `UNIQUE(session_id, track_id, frame_index)`. Indexes on time and (session, track). |
| `boarding_events` | `session_id`, `track_id`, `boarded_at`, `direction`, `station`, `trip_id`, `verification_status`, `passenger_ref`, `ticket_id`, `last_validation_id` | `UNIQUE(session_id, track_id)`: one boarding per Track ID per session. `passenger_ref` and `ticket_id` are set only on VALID. |
| `validation_events` | `event_key` (unique, demo only), `validated_at`, `outcome`, `reason`, `credential_type` (qr_token/ticket_id/none), `token_hash`, `presented_ref`, `ticket_id`, `passenger_ref`, `boarding_id`, `session_id`, `trip_id`, `station`, `channel` (camera/dashboard/cli/demo) | Every check is recorded. Plaintext tokens are never stored. |

No names, faces, images or contact data are stored.

## Metric definitions

`compute_metrics(conn, period)` in `app/analytics.py`. The period is a half-open UTC range built from local days in `app.timezone`; "All time" is unbounded. The period applies to all seven metrics. The validation-status filter applies only to the two tables.

| # | Card | Definition |
|---|---|---|
| 1 | Valid | Distinct credential key (`ticket_id`, else typed ticket ID, else token hash) with outcome VALID |
| 2 | Invalid | Same distinct key with outcome INVALID, WRONG_ROUTE or ALREADY_USED |
| 3 | Total Passenger | Total Passenger = verified passengers (distinct passenger refs with a valid ticket) + boarded Track IDs not linked to a valid ticket (verification required or failed). |
| 4 | Total Detection | Rows in `detection_records`: first sighting of each Track ID plus at most one sample per `detection.record_interval_sec` |
| 5 | Expired Tickets | Same distinct key with outcome EXPIRED (not merged into Invalid) |
| 6 | Boarding Events | Rows in `boarding_events` |
| 7 | Tracking IDs | Distinct (session, track) pairs seen in detection records or boardings |

The Total Passenger sentence is shown as a caption under the cards and in the card's help text. Repeat scans of one ticket with the same outcome count once. A ticket that was VALID once and later ALREADY_USED counts once in each card, because those are two different facts. Track IDs are scoped by session, because ByteTrack restarts at 1 in every process.

## Ticket validation rules

`evaluate_ticket()` is pure (no I/O, no clock). The journey (`route_id`, `direction`, ordered stations, current station, `trip_id`) comes from `build_journey()`: stations from `route.stations`, reversed for `inbound`; current station from `--station` or `journey.current_station` (may be unset); `trip_id` from `--trip-id`, else `journey.trip_id`, else `<route_id>-<direction>-<local date>`.

First match wins:

1. No credential presented: **VERIFICATION_REQUIRED** ("No ticket presented; ticket verification required").
2. Credential not well formed: **INVALID** ("Unrecognised QR code" / "Malformed ticket ID").
3. No matching ticket record: **INVALID**.
4. Ticket CANCELLED: **INVALID**.
5. `now >= expires_at`: **EXPIRED** ("Ticket expired at <local time>").
6. `now < valid_from`: **INVALID** ("Ticket not valid yet").
7. Route checks, all **WRONG_ROUTE**, with `i(x)` = position of station x in the ordered stations: other route; boarding or destination station not on the route; `i(boarding) >= i(destination)` (opposite direction); current station set and before the boarding station; current station set and after the destination.
8. Ticket USED and not a re-check on the same trip: **ALREADY_USED**. A re-check means the same `trip_id`, and either no boarding is given, the ticket has no bound boarding, or it is the same boarding.
9. Otherwise **VALID**. An ACTIVE ticket is bound (set to USED with the trip and boarding).

Onward travel: `i(boarding) <= i(current) < i(destination)` is valid, so an intermediate stop never fails a ticket; `i(current) == i(destination)` is valid as "At destination (alighting)". Expiry depends only on `expires_at` (rule 5), never on station position. `travel_date` is informational; `issue_ticket()` keeps it equal to the local date of `valid_from`.

`TicketValidator.validate()` parses the credential (QR payload `TPD1:<token>` or a bare token of 32-64 URL-safe characters, at most 512 characters; ticket IDs are stripped and uppercased), then in one `BEGIN IMMEDIATE` transaction looks up the ticket by token hash or ID, evaluates it, binds it if needed, inserts the `validation_events` row and updates the boarding. The QR content is only a lookup key; passenger details always come from the database.

Boarding update precedence (amendment M3), in this one place:

- a VALID boarding is never overwritten by a later check (the validation row is still recorded and an alert is shown);
- a VERIFICATION_REQUIRED outcome only sets `last_validation_id` and never changes `verification_status`;
- any other outcome overwrites a non-VALID status; VALID also sets `passenger_ref` and `ticket_id`.

The camera path validates a QR code only when it can be linked to a boarded Track ID: the QR centre is inside that person's box, or the QR is inside no tracked person's box (or has no corner points) and exactly one boarding happened within `qr.fallback_window_sec`. A QR inside the box of a tracked person who has not boarded is not linked, so it cannot verify someone else's boarding. Unlinked QR codes are logged and shown on the overlay but not recorded. Dashboard and CLI checks always record.

## De-duplication rules

- **Detection samples** (`DetectionThrottle`): a tracked person is buffered when the Track ID is new in this session or `detection.record_interval_sec` has passed since its last sample. Boxes without a ByteTrack ID are never recorded. The buffer is written with `INSERT OR IGNORE` in one transaction every `database.flush_interval_sec`, together with the heartbeat.
- **Boardings** (`BoardingRules`): a boarding is created when the entry line reports a crossing in `boarding.entry_line.entry_direction`, the track has been seen in at least `boarding.min_frames_seen` frames, and the Track ID has not boarded in this session. A crossing seen too early is held as pending and confirmed if the track reaches the frame count within `cooldown_seconds` while still on the entry side; otherwise it is dropped. `UNIQUE(session_id, track_id)` with `ON CONFLICT DO NOTHING` is the final guarantee.
- **QR scans**: the same payload is ignored for `qr.rescan_cooldown_sec`, and a (token, boarding) pair is validated once per session.
- **Validations in metrics**: counted by distinct credential key (see above).

## Detection thresholds (amendment M1)

- `tracking.input_confidence` (default 0.1) is passed as `conf=` to `YOLO.track`, so low-score boxes can keep an existing track alive through brief occlusion.
- `detection.confidence` (default 0.35, `--conf`) is the person acceptance threshold and ByteTrack's `new_track_thresh`. At startup `prepare_tracker_yaml()` reads `config/bytetrack.yaml`, requires `tracker_type: bytetrack`, `input_confidence <= track_low_thresh` and `detection.confidence >= track_low_thresh`, and writes `data/runtime/bytetrack_effective.yaml` with `new_track_thresh = detection.confidence`. A WARNING is logged when the committed value differs.
- A track is returned only once its highest confidence has reached `detection.confidence`. Later low-score boxes of an accepted track are shown and recorded with their real confidence, which can be below the threshold.
- `track_buffer: 60` keeps a lost track for 60 frames (about 2 s at 30 FPS).

## Decision log

- **D-1** The stray-identifier syntax defect reported in the earlier inspection is not in the tree. The final verification greps for it and runs a compile and import probe.
- **D-2** Invalid card = INVALID + WRONG_ROUTE + ALREADY_USED, with the breakdown in the help text. Expired stays separate.
- **D-3** Total Passenger = identified passenger refs plus boarded Track IDs not linked to a ref, labelled in a visible caption (wording fixed by M2). Counting all observed Track IDs would duplicate card 7.
- **D-4** Validation metrics count distinct credential keys per outcome, not rows.
- **D-5** Total Detection counts throttled samples (first sight plus at most one per track per interval), documented as samples, not frames.
- **D-6** The default database is `data/ticketless.db`. The legacy `data/passengers.db` is untouched and unused.
- **D-7** The model lives at `models/yolov8n.pt` and is gitignored.
- **D-8** The validation-status filter applies to the tables only.
- **D-9** The current station is set per run (`journey.current_station`, `--station`). The Ticket Check page has its own station selector. Live station updates to a running detection process are future work.
- **D-10** ALREADY_USED is trip-scoped: re-checks on the same trip and boarding stay VALID; another trip or another boarding gives ALREADY_USED.
- **D-11** The main page holds only the seven metrics and supporting elements; the validation workflow is a separate Ticket Check page.
- **D-12** A video file is selected with `--path` or the `--choose-file` dialog. The dashboard does not start detection.
- **D-13** Allowed video extensions are .mp4, .avi, .mov, .mkv (configurable). The extension is checked before existence, and both before the model loads.
- **D-14** Demo data is anchored to `--date` (default today), reproducible for a date and seed, and idempotent. `--reset` then a re-run refreshes it.
- **D-15** pyzbar is dropped; OpenCV is the only QR decoder.
- **D-16** Recorded video uses wall-clock timestamps; `--export-mot` uses frame indices.
- **D-17** Unlinked QR codes in the camera path are not recorded, so posters or passers-by can't use up tickets. Deliberate dashboard and CLI checks always record.
- **D-18** Legacy flags `--video`, `--phone-url`, `--webcam-index` and `--source <url|index|path>` are kept as aliases. `--tickets` and `--no-fallback` are dropped.
- **D-19** The dashboard has no authentication. It binds to 127.0.0.1 and the gap is documented.
- **D-20** Time validity is the `[valid_from, expires_at)` window only; `travel_date` is informational.
- **D-21** A session's `trip_id` is fixed at its start. Dashboard and CLI checks inherit the boarding's or the running session's `trip_id`, so a re-check after midnight is not read as ALREADY_USED.
- **D-22** `--reset` deletes run in `passenger_service.delete_synthetic()`, including synthetic tickets and passengers, in one transaction.
- **D-23** `ConfigError` subclasses `ValueError`; `main` catches it first.
- **D-24** Tickets become CANCELLED only through `ticket_validator.cancel_ticket()` (used by the demo generator; no dashboard button).
- **D-25** Boarding rules read track age from `TrackedPerson.hits`, so they depend only on the tracker's public output.
- **D-26** Only the file checks (extension, existence) run before the model loads. Live sources open after model load, so a camera is not held open while torch loads.

### Amendments

- **M1** Tracker input confidence (`tracking.input_confidence`) is separate from person confidence (`detection.confidence`, which also sets `new_track_thresh` in the effective ByteTrack YAML). See [Detection thresholds](#detection-thresholds-amendment-m1).
- **M2** The Total Passenger wording is fixed to the sentence in [Metric definitions](#metric-definitions).
- **M3** Failed verifications are resolved on the Ticket Check page by selecting a non-VALID boarding of the period (`analytics.boardings_needing_resolution()`), with the boarding update precedence in [Ticket validation rules](#ticket-validation-rules).
- **M4** The demo generator requires route R1 outbound with the six default stations in order; otherwise it exits 1 without writing. Demo trip IDs are `R1-outbound-<date>`, independent of `journey.trip_id`.
