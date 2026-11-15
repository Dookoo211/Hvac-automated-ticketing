# Config Reference

This document annotates every supported `config.json` field.

## Business Identity

- `business_name`
  Company display name used in reply templates.
- `phone_number`
  Direct phone number used in acknowledgment/follow-up templates.
- `default_service_area`
  Default service area for dispatch notes.
- `dispatcher_phone`
  Dispatcher phone used in notifications.
- `office_hours`
  Human-readable office-hours text shown in outgoing emails.

## Office-Time Logic

- `office_timezone`
  IANA timezone for schedule evaluation (example: `America/Toronto`).
- `office_schedule`
  Day map for office-hour checks.
  Format per day: `{ "start": "HH:MM", "end": "HH:MM" }` or `null` for closed.
  Keys: `mon`, `tue`, `wed`, `thu`, `fri`, `sat`, `sun`.

## Service Detection

- `services`
  Canonical service list used for direct matching and output normalization.
- `service_keyword_map`
  Keyword mapping used for structured `service_detected` inference.

## Classification Keywords

- `lead_keywords`
  Keywords scored as lead intent (`+2` each hit).
- `existing_keywords`
  Keywords scored as existing-customer intent (`+2` each hit).
- `spam_keywords`
  Keywords scored as spam intent (`+3` each hit).

## Mailbox Connectivity

- `imap_server`
  IMAP host.
- `imap_port`
  IMAP port (typically `993`).
- `smtp_server`
  SMTP host.
- `smtp_port`
  SMTP port (`587` STARTTLS or `465` implicit TLS).
- `email_address`
  Mailbox login and sender address.
- `email_password`
  Mailbox password or app password.
- `mailbox`
  Inbox folder to scan (default `INBOX`).
- `sent_mailbox`
  Sent folder used to detect manual replies.
- `read_unseen_only`
  If `true`, scans only unread emails.

## Core Behavior

- `auto_reply_enabled`
  Enables automatic acknowledgment replies for lead-class messages.
- `auto_reply_cooldown_hours`
  Minimum hours between automatic replies to the same sender.
- `follow_up_enabled`
  Enables delayed follow-up reminders.
- `follow_up_delay_hours`
  Delay before follow-up eligibility.
- `dispatch_summary_enabled`
  Enables periodic batched dispatch summaries.
- `dispatch_summary_interval_minutes`
  Minimum minutes between summary sends.
- `dispatch_summary_recipient`
  Recipient address for summary emails.
- `dispatch_summary_state_file`
  JSON state file with pending summary queue.
- `dispatch_summary_log_file`
  Plain text log file of sent summary payloads.
- `ticket_duplicate_mode`
  Duplicate ticket handling mode for identical details.
  Allowed values: `update`, `replace`, `delete`.
- `sms_enabled`
  Enable SMS alerts (gateway-based).
- `sms_gateway_email`
  Email-to-SMS gateway address.

## Feature Toggles

- `feature_auto_reply`
  Toggle automatic acknowledgment replies.
- `feature_ticketing`
  Toggle ticketing and CRM updates.
- `feature_dispatch_summary`
  Toggle dispatch summary emails.
- `feature_sms_notifications`
  Toggle SMS alerts.
- `feature_follow_up`
  Toggle follow-up emails.

## Runtime and Polling

- `dry_run`
  If `true`, prints actions without sending emails.
- `poll_interval_seconds`
  Sleep interval between daemon cycles.
- `heartbeat_interval_minutes`
  Console heartbeat interval. Set `0` to disable.
- `ui_refresh_seconds`
  Dashboard UI refresh interval in seconds.
- `agent_log_file`
  Structured JSONL log file path.

## Licensing

- `license_key`
  Local key required when enforcement is enabled.
- `license_keys_file`
  Path to local valid key list (JSON).
- `license_binding_file`
  Local binding file that locks the license key to a single email address.
- `license_enforcement`
  Enables/disables key validation.

## State and Logging Paths

- `processed_ids_file`
  Text file of processed Message-IDs.
- `leads_csv_file`
  CSV output path for lead logs.
- `service_requests_file`
  CSV output path for structured request extraction.
- `draft_replies_file`
  CSV output path for generated draft replies.
- `job_tickets_file`
  CSV output path for automatic lead ticket creation.
- `db_path`
  SQLite database path for CRM/jobs.
- `sender_cooldown_file`
  JSON map storing last auto-reply time per sender.
- `follow_up_state_file`
  JSON map storing follow-up queue state.
- `error_log_file`
  Error log path for stack traces.
