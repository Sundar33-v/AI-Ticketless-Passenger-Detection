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
- The running detection process keeps its station for the whole run. To change station, stop it with `q` and start it again with the new `--station`, or set the station on the Ticket Check page for desk checks.

## Dashboard tour

**Sidebar:** Period (Today, Yesterday, Last 7 days, All time, Custom range), Validation status (filters the two tables only), the database path, and "Demo data: N synthetic rows present" when demo rows exist.

**Seven metrics** for the selected period (hover a card for its definition):

- Valid: distinct tickets with a valid check
- Invalid: distinct credentials that were Invalid, Wrong route or Already used
- Total Passenger: verified passengers (distinct passenger refs with a valid ticket) + boarded Track IDs not linked to a valid ticket (verification required or failed)
- Total Detection: recorded person-detection samples (not a head count)
- Expired Tickets: distinct tickets that failed because they had expired
- Boarding Events: confirmed entry-line crossings, one per Track ID per session
- Tracking IDs: distinct Track IDs seen, per detection session

When all seven are 0, the page suggests starting detection or loading the demo data.

**Detection source:** "Detection running – <source> – last update hh:mm:ss" with a live preview frame, or "Detection is not running" with the last session's status and error. The status uses the session heartbeat, so a crashed process shows as not running after `runtime.heartbeat_stale_sec` seconds.

**Verification alerts**, **Recent boarding events** and **Recent validation results** follow. The page refreshes itself every `dashboard.refresh_sec` seconds.

## Alerts

| Alert | Meaning | What to do |
|---|---|---|
| Ticket verification required for Track ID n (boarded hh:mm). Please check the passenger's ticket. | The boarding has had no ticket check for more than `alerts.verification_timeout_sec` seconds | Check the passenger's ticket and record it on Ticket Check, selecting this boarding |
| Ticket verification failed for Track ID n: Invalid – ... | No ticket record, malformed credential, cancelled ticket, or not valid yet | Ask for another ticket; recheck on Ticket Check |
| ... Expired – Ticket expired at ... | The ticket's validity window has ended | Follow your operator's rules |
| ... Wrong route – ... | Other route, opposite direction, bus before the boarding station or past the destination | Check the ticket's stations |
| ... Already used – ... | The ticket was used on another trip or for another boarding on this trip | Check whether the ticket was shared |

An alert is a prompt to check, not proof of anything about the passenger. Tracking can mistake one person for two (see [limitations](limitations.md)).

## Ticket Check page

Open **Ticket Check** in the page navigation.

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
