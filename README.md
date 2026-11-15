# HVAC Automated Emailing

Local-first HVAC inquiry responder designed for one-time install deployments.

## Purpose

This agent continuously monitors a mailbox, classifies incoming emails, sends safe acknowledgments for new leads, and stores audit data proving response coverage.

## Architecture Summary

1. Poll IMAP inbox (`UNSEEN` by default).
2. Classify message content with weighted keyword scoring.
3. Log every processed message to `hvac_leads.csv`.
4. Auto-reply to lead-class messages with office-hours-aware messaging.
5. Enforce per-sender cooldown to avoid repeated auto-replies.
6. Optionally send delayed follow-ups if no manual sent reply is detected.
7. Extract structured service request data (name, address, issue, priority, type).
8. Generate operational draft replies for office staff.
9. Create automatic job tickets for each lead.
10. Generate Google Maps links from detected addresses.
11. Maintain CRM and job scheduling in a local SQLite database.
12. Send periodic dispatch summary emails.
13. Persist runtime state to local text/JSON files.

## Project Files (Annotated)

- `hvac_email_agent.py`
  Main runtime and CLI entrypoint.
- `config.json`
  Business settings, classifier keywords, office hours, mailbox credentials, runtime tuning.
- `start_agent.bat`
  Windows launcher for daemon mode (`--daemon`).
- `install_startup_task.ps1`
  Registers a Task Scheduler task to launch the agent at logon.
- `uninstall_startup_task.ps1`
  Removes the startup task.
- `build_exe.bat`
  Builds a single-file executable with PyInstaller.
- `data/hvac_agent.db`
  SQLite CRM + jobs database.
- `data/`
  Runtime CSV and state files.
- `logs/`
  Agent logs and error output.
- `scripts/migrate_csv_to_db.py`
  One-time migration for existing CSV tickets.
- `processed_ids.txt`
  Message-ID de-duplication store.
- `sender_cooldown.json`
  Last auto-reply timestamp per sender.
- `follow_up_state.json`
  Follow-up queue and status metadata.
- `service_requests.csv`
  Structured request extraction output for dispatch/sales tracking.
- `draft_replies.csv`
  Auto-generated draft reply suggestions for staff.
- `job_tickets.csv`
  Automatically created dispatch-ready tickets (ticket ID, status, priority, map link).
- `dispatch_summary_state.json`
  Pending summary queue and last-send timestamp.
- `dispatch_summary.log`
  Local log copy of each summary payload.
- `valid_licenses.json`
  Local valid license key registry.
- `hvac_leads.csv`
  Lead evidence log for reporting/sales proof.
- `error.log`
  Stack traces for operational errors.

## Setup

Run the guided installer:

```powershell
python hvac_email_agent.py --setup
```

Or run the minimal Gmail-focused installer:

```powershell
python hvac_email_agent.py --quick-setup
```

Client-style one-click flow (release folder):

```powershell
start_client.bat
```

The setup flow captures:
- business identity
- office schedule/timezone
- IMAP/SMTP settings
- sent mailbox name
- daemon poll and heartbeat intervals
- dry-run and follow-up defaults

## Runtime Modes

Demo classification:

```powershell
python hvac_email_agent.py --demo
```

One-shot cycle (safe):

```powershell
python hvac_email_agent.py --dry-run
```

Continuous daemon (safe):

```powershell
python hvac_email_agent.py --dry-run --daemon
```

Continuous daemon (live replies enabled):

```powershell
python hvac_email_agent.py --live --daemon
```

Local dashboard UI:

```powershell
python hvac_email_agent.py --ui
```

Custom daemon poll interval:

```powershell
python hvac_email_agent.py --live --daemon --poll-interval 20
```

## Windows Deployment

Manual start:

```powershell
start_agent.bat
```

Install startup task:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_startup_task.ps1
```

Remove startup task:

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall_startup_task.ps1
```

## Optional EXE Packaging

```powershell
build_exe.bat
```

Clean build artifacts:

```powershell
clean_release.bat
```

Build output:
- `release\hvac_installer.exe` (setup + UI)

## Client Download Instructions

1. Send the client the `release` folder containing `hvac_installer.exe`.
2. Client double-clicks `hvac_installer.exe`.
3. Setup wizard opens. Client enters Gmail + app password.
4. Installer saves config and creates required `data/` and `logs/` folders.
5. UI launches automatically. Client clicks Start.

## Config Reference (Key Fields)

Complete field-by-field documentation is available in `CONFIG_REFERENCE.md`.

- `office_schedule`
  Day-by-day local schedule used for in-hours vs after-hours reply template selection.
- `office_timezone`
  IANA timezone used when evaluating `office_schedule`.
- `auto_reply_cooldown_hours`
  Minimum hours between automatic replies to the same sender.
- `follow_up_enabled`
  Enables delayed reminder messages for unresolved lead emails.
- `follow_up_delay_hours`
  Delay before follow-up eligibility.
- `dispatch_summary_enabled`
  Enables periodic summary email batching for newly extracted requests.
- `dispatch_summary_interval_minutes`
  Minutes between summary sends.
- `dispatch_summary_recipient`
  Email recipient for summaries.
- `license_key`
  Customer license key to authorize runtime.
- `license_enforcement`
  Enables runtime key validation.
- `ui_refresh_seconds`
  UI metric refresh interval.
- `job_tickets_file`
  CSV output path for automatic job ticket creation.
- `db_path`
  SQLite database path for CRM and scheduling.
- `agent_log_file`
  Structured JSONL event log.
- `ticket_duplicate_mode`
  Duplicate ticket behavior for identical details: `update`, `replace`, or `delete`.
- `feature_auto_reply`
  Toggle auto-replies.
- `feature_ticketing`
  Toggle ticketing and CRM updates.
- `feature_dispatch_summary`
  Toggle dispatch summaries.
- `feature_sms_notifications`
  Toggle SMS alerts.
- `feature_follow_up`
  Toggle follow-up emails.

## SQLite CRM

The system maintains a local SQLite database for customers, jobs, technicians, and history.
Default location: `data/hvac_agent.db`.
- `ticket_duplicate_mode`
  Duplicate ticket behavior for identical details: `update`, `replace`, or `delete`.
- `poll_interval_seconds`
  Daemon sleep interval between processing cycles.
- `heartbeat_interval_minutes`
  Interval for console health messages (`0` disables heartbeat).
- `sent_mailbox`
  Folder used to detect manual agent-owner replies.
- `error_log_file`
  Path for stack trace logging.

## Operational Notes

- Startup health line:
  `Agent started at ...`
- Daemon heartbeat line:
  `Agent heartbeat at ...`
- Error line:
  `Error encountered. Details written to error.log`

## Classification Rules

- lead keyword hit: `+2`
- existing keyword hit: `+2`
- spam keyword hit: `+3`
- highest score wins
- ties default to `existing` for safety

## ROI Features

- Service Request Extraction
  - captures customer name (email body takes priority), address, issue, priority, request type.
- Auto Draft Replies
  - generates ready-to-use draft responses in `draft_replies.csv`.
- Automatic Job Tickets
  - creates `job_tickets.csv` entries with ticket ID and status.
- Address to Google Maps
  - appends `maps_link` when address extraction succeeds.
- Dispatch Summary
  - sends hourly-style batched summaries to operations email.
- Local Dashboard UI
  - shows status, processed count, service requests, quotes, spam, open tickets, and dispatch board.
- Technician Dispatch Board
  - view jobs by priority, assign technician, schedule time, update status.
- SMS Alerts
  - optional notifications to dispatcher phone via SMS gateway.
- License Validation
  - validates `license_key` against `valid_licenses.json`.

## CSV Schema

`hvac_leads.csv` columns:

- `timestamp`
- `sender_email`
- `detected_name`
- `service_detected`
- `subject`
- `classification`
