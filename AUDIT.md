# AUDIT: HVAC Email Auto-Responder

Audit date: 2026-11-15. No code was changed during this audit.
Phase 1 status: see "Phase 1 results" at the end of this file.

Scope: `README.md`, `CONFIG_REFERENCE.md`, `hvac_email_agent.py` (4,034 lines), `app/*.py`,
`scripts/migrate_csv_to_db.py`, `hvac_agent.py`, `hvac_agent_ui.py`, `hvac_installer.py`,
all `.bat` / `.ps1` / `.spec` files, `config.json` (read with secrets redacted), `valid_licenses.json`,
and the shape of `data/` and `logs/`.

How claims were checked:
- **[VERIFIED]**: reproduced offline (pure-function calls, `--demo`, or a throwaway copy in a temp dir). No network, no email sent.
- **[CODE]**: follows directly from reading the code. The line numbers are cited.
- **[LIKELY]**: depends on mail-server behavior I could not test without a live mailbox.

Line numbers refer to `hvac_email_agent.py` unless another file is named.

---

## Severity summary

| ID | Severity | Title |
|----|----------|-------|
| C1 | Critical | Cycle state is saved only at the end, so a mid-cycle failure loses dedup/cooldown and silently drops leads |
| C2 | Critical | `--dry-run` is not safe: it marks mail as read, records Message-IDs and cooldowns, creates tickets, and clears the dispatch queue |
| C3 | Critical | No single-instance lock, so UI "Start" plus the startup-task daemon can run at once and double-reply |
| C4 | Critical | Dispatch summary goes to the agent's own inbox and can be re-ingested as a lead (self-loop) |
| C5 | Critical | Plaintext mailbox password in `config.json`, which is not gitignored and is bundled into two EXEs |
| H1 | High | Auto-reply suppression misses List-*, bounces, Return-Path `<>`, X-Autoreply, and outgoing mail has no `Auto-Submitted` |
| H2 | High | Job follow-up emails bypass every reply-safety check and burst-send to all old jobs when enabled |
| H3 | High | Substring fallback in `keyword_hits` creates false leads ("deadline", "theater", "contact", "event") |
| H4 | High | Common customer words ("confirm", "update", "availability", "deal", "specialist") push real leads to spam |
| H5 | High | Non-atomic writes for every state file; a corrupt file is treated as empty, which wipes cooldown/dedup |
| H6 | High | One bad message aborts the whole cycle; with `read_unseen_only=false` it blocks every later message forever |
| H7 | High | "Reset All Data" (UI) and `reset_install.bat` delete DB, dedup and cooldown with no backup |
| H8 | High | Customer upsert overwrites a known phone/address with blanks from a later email |
| H9 | High | Auto-reply echoes attacker-controlled text to whatever `From:` claims (spoofable backscatter relay) |
| H10 | High | No IMAP timeout, so the daemon can hang forever on a dead connection |
| M1–M16 | Medium | See below |
| L1–L14 | Low | See below |

---

## CRITICAL

### C1. State persisted only at end of cycle → lost leads, duplicate replies [CODE]
`process_inbox` (1847–2274) keeps `processed_ids`, `sender_cooldown`, `follow_up_state`, and `dispatch_state`
in memory and saves them only at 2265–2268, after the loop and after the dispatch and job follow-up sends.
Anything that raises mid-cycle skips the save. Examples: SMTP failure, SMS gateway failure, a locked DB, a malformed header, or a bad `occurrence_count` in the CSV.

Consequences:
- Replies already sent this cycle have **no cooldown and no processed-ID record** on disk.
- `iter_messages` fetches `(RFC822)` on a read-write mailbox (1597). That **sets `\Seen`** (RFC 3501). The message that triggered the exception is now read but unprocessed. With `read_unseen_only=true` (the current setting) **it is never looked at again: a silently dropped lead.**
- CSV rows, DB jobs, and tickets for earlier messages were already written. If those messages are seen again (for example, staff marks them unread, or `read_unseen_only=false`), they get duplicate rows and duplicate replies.
- If the dispatch summary was sent and a later step (`process_job_followups`) raises, `pending` is not cleared on disk, so **the same summary is re-sent** next cycle.

### C2. `--dry-run` has real side effects [CODE]
With `dry_run=True` the code still:
- fetches `RFC822`, which marks messages `\Seen` in the real mailbox (1597);
- adds Message-IDs to `processed_ids` (2260) and saves them;
- sets `sender_cooldown[sender] = now` (2252) as if a reply went out;
- writes CSVs and creates DB customers and jobs;
- stamps `followup_sent_at` on jobs (2362–2366) even though nothing was sent;
- clears the dispatch `pending` queue (2313).

**Running `--dry-run` against the client's real mailbox permanently consumes unread leads.** After switching to live, those customers never get the acknowledgment. Until this is fixed, do not run `--dry-run` against a real mailbox; use `--demo` only.

### C3. No single-instance protection [CODE]
`install_startup_task.ps1` uses `MultipleInstances IgnoreNew`. That only stops the *task* from starting twice. Nothing stops the UI's Start button (worker thread, 2700) and the `start_agent.bat` daemon from running at once, or two UIs.

Two processes then poll the same inbox. Each loads `processed_ids` and the cooldown file at the start of its cycle and overwrites them at the end (last writer wins). Both can fetch the same UNSEEN message before either marks it, so the result is **duplicate auto-replies** plus lost dedup entries.

### C4. Dispatch summary self-loop [CODE + LIKELY]
`config.json` sets `dispatch_summary_recipient` to the agent's own address, and setup forces this (3711, 3898). The summary body contains each issue sentence and address, for example "My furnace stopped working - 12 Main St".

Messages from the agent's own address are **still classified and ticketed**; only the reply is skipped (1414). There is also no check for the agent's own `X-HVAC-Auto` header on incoming mail.

Gmail normally delivers self-sent mail to the Inbox [LIKELY]. The summary is then processed as a lead, which creates a new job and a new dispatch item. That item produces another summary next interval: an hourly loop of junk tickets.

### C5. Plaintext credentials and their distribution [CODE]
- `config.json` holds the Gmail app password in plaintext (key `email_password`).
- `.gitignore` does **not** exclude `config.json` or `valid_licenses.json`.
- `build_exe.bat` and `hvac_email_agent.spec` / `hvac_email_agent_ui.spec` bundle `config.json` into `hvac_email_agent.exe` and `hvac_email_agent_ui.exe` (`--add-data "config.json;."`). **Any EXE built from this folder embeds the developer's or client's live Gmail app password.**
- `write_config_files` (640–659) also copies the config, with the password, into `release/config.json` if that file exists.
- Recommendation: move the secret to Windows Credential Manager via `keyring`. Ship a `config.example.json`. Stop bundling `config.json`. **Rotate the current Gmail app password**, because it has been stored in plaintext and may have been built into EXEs.

---

## HIGH

### H1. Auto-reply suppression gaps [CODE]
`should_skip_auto_reply` (1409–1427) checks: empty sender, exact own address, `noreply`-style local parts, `Auto-Submitted != no`, and `Precedence: bulk|junk|list`.

It does **not** check:
- `List-Id`, `List-Unsubscribe`, `List-Post`, or any `List-*` header (mailing lists).
- `Precedence: auto_reply`.
- `X-Autoreply`, `X-Autorespond`, `X-Auto-Response-Suppress`, or `X-HVAC-Auto` (our own marker).
- Bounces: `Return-Path: <>`, `Content-Type: multipart/report`, or senders `mailer-daemon@` / `postmaster@`. Bounce *subjects* only add spam score; they do not block a reply if the score still says "lead".
- Aliases of the agent's own address (`+tags`, Gmail dot variants, alias domains).
- `Reply-To` vs `From`: the reply always goes to `From`.

Outgoing replies also do not set `Auto-Submitted: auto-replied` or `X-Auto-Response-Suppress: All`. Another autoresponder will therefore answer us. Its "Thanks for contacting us about your furnace repair" reply can score as a lead, giving one ping-pong per cooldown window (24 h).

### H2. Job follow-ups bypass all safety checks [CODE]
`process_job_followups` (2317–2371) emails the customer for **every** DB job with `status='new'` older than `follow_up_delay_hours`. It:
- does not call `should_skip_auto_reply`. Jobs are created for every lead, including no-reply, auto-submitted, and own-address senders.
- does not check cooldown;
- does not check the Sent folder for a manual reply. Unlike the legacy queue, it only looks at whether staff changed the status in the UI. Staff who phoned the customer still trigger an email.
- sends to every qualifying job at once when the feature is first enabled. Months-old jobs all get mailed in one burst.

There are also two follow-up systems with inverted gates. The legacy queue runs only when ticketing is **off** (1930–1934); the job follow-up runs only when ticketing is **on** (2319–2323).

### H3. Substring fallback causes false leads [VERIFIED]
`keyword_hits` (796–805) tries a word-boundary match and, **if that finds nothing, falls back to raw `str.count`**. Short keywords then match inside other words: `ac` in "contact"/"back", `heat` in "theater", `vent` in "event"/"prevent", `dead` in "deadline", `deal` in "ideal", `trial` in "industrial", `special` in "specialist", `sale` in "wholesale".

Verified: *"Deadline for the theater event is next week, contact me back."* → **lead, service = Furnace Repair**. With auto-reply on, that sender gets an HVAC acknowledgment and a ticket is created.

### H4. Real leads misclassified as spam or ignored [VERIFIED]
`DEFAULT_MARKETING_KEYWORDS` adds +2 spam per hit for everyday words: `update`, `confirm`, `account`, `verify`, `welcome`, `feedback`, `special`, `deal`, `sale`, `tips`, `trial`, `calendar`, `availability`. `is_marketing_email` (846–858) returns True on **any** single hit.

Verified results:
- *"Can you confirm availability for a quote on a new heat pump? Any update would be great."* → **spam** (no reply, no ticket).
- *"Looking for an HVAC specialist, is there a deal on duct cleaning?"* → **spam**.
- *"My AC is blowing warm air. Can someone come out?"* → **existing** (score 0/0/0 → default).
- *"Our boiler won't fire up, radiators are cold."* → **existing**.

Ties and all-zero scores default to "existing", which means **no ticket and no reply**. That is safe for replies but **loses leads**. It is the wrong default for a lead-capture product. A safer default is "needs review", which creates a ticket but sends no auto-reply.

### H5. Non-atomic state writes; corruption = silent reset [CODE]
`save_processed_ids`, `save_json_dict`, `save_csv_rows`, the UI toggle save (2751), `write_config_files`, and the license binding all use `write_text` / `open("w")` directly. A crash or power loss mid-write leaves a truncated file.

`load_json_dict` (451–459) turns a JSON decode error into `{}`, so a corrupt cooldown file **silently resets all cooldowns**. A truncated `config.json` makes the installer think there is no login and re-run setup.

### H6. One bad email can block processing [CODE]
There is no per-message try/except in the loop (1954–2261). The daemon does catch the exception (3369), so the process survives, but the cycle ends.
- With `read_unseen_only=true`, the bad message is now `\Seen`, so it is dropped (C1).
- With `read_unseen_only=false`, the same message is fetched first every cycle and **every message after it is never processed again**: a poison pill.

### H7. Destructive resets without backup [CODE]
- UI "Reset All Data" (3276–3306) deletes the DB, every CSV, `processed_ids`, cooldowns, follow-up state, and the logs. The only confirmation is a double-click within 8 s.
- `reset_install.bat` runs `Remove-Item -Recurse -Force data, logs` and deletes `config.json`.

After a reset, dedup and cooldown are empty. With `read_unseen_only=false` the next cycle re-processes and **re-replies to the whole inbox**.

### H8. CRM upsert loses customer data [CODE]
`crm.upsert_customer` (app/crm.py 21–31) overwrites `first_name`, `last_name`, `phone`, and `address` with whatever the newest email yielded. Take a customer whose first email had a phone and address. Their follow-up *"any update?"* (if classified lead) sets phone to `""` and address to `"Unknown"`. `update_job_duplicate` does the same to job phone and name.

### H9. Auto-reply content is attacker-controlled and the recipient is spoofable [CODE]
The acknowledgment includes `Issue noted: {issue}` (up to 180 chars of sender text) and `Location: {address}`. The `Address:` label line is **unbounded** (1057–1061). A Maps link built from that text is also included.

The recipient is the unauthenticated `From:` header. Anyone can forge `From: victim@x.com` with a body that contains a link, and the business's Gmail account sends that text to the victim. No `Authentication-Results` (SPF/DKIM/DMARC) check is made, and there is no daily send cap.

That gives a spam and phishing relay, and a Gmail suspension risk: Gmail allows roughly 500 messages a day on consumer accounts.

### H10. IMAP has no timeout [CODE]
`imaplib.IMAP4_SSL(server, port)` (1922, 3724, 3910) is called without `timeout=`. A half-open connection blocks `process_inbox` forever. The daemon looks alive but does nothing. SMTP does have `timeout=30`. There is no reconnect or backoff logic, and the 30 s polling continues at full rate against an auth failure (see `logs/error.log`: repeated `AUTHENTICATIONFAILED`).

---

## MEDIUM

- **M1. Duplicated storage, no source of truth.** Lead log, service requests, drafts, and tickets go to CSV. Jobs and customers go to SQLite. Dedup, cooldown, follow-up, and dispatch state go to JSON/txt. CSV tickets have status `Open` and never change; the UI updates only the DB. CSV dedup matches closed tickets, but DB dedup matches only open ones. The dashboard mixes the sources: emails and quotes from CSV, jobs from the DB.
- **M2. `ticket_duplicate_mode`.**
  - `"replace"` (CSV, 1355–1361) overwrites the existing row with a new ticket. **The original `ticket_id`, `created_at`, `status`, and `assigned_technician` are lost.**
  - `"delete"` **does not delete anything**. It behaves like "ignore duplicate, bump count", so it is misnamed but safe.
  - In the DB, all three modes behave identically (update).
  - Dedup keys on exact issue-sentence equality, so near-identical repeat emails are not treated as duplicates.
- **M3. Overlapping config flags.**
  - `auto_reply_enabled` / `feature_auto_reply`, `follow_up_enabled` / `feature_follow_up`, `dispatch_summary_enabled` / `feature_dispatch_summary`, and `sms_enabled` / `feature_sms_notifications` are ANDed in some places, single-checked in others, and have different `.get()` defaults (True in `process_inbox`, False in the UI).
  - There is no type or range validation: `ticket_duplicate_mode`, ports, timezone, and HH:MM values are accepted as-is.
  - An unknown timezone silently falls back to UTC (1446).
- **M4. `ignore_promotions` overrides `mailbox`.** When `ignore_promotions=true` (the default), the configured `mailbox` is ignored and INBOX is forced (1925–1926). The promotions filter itself does nothing (dead code). It requests `BODY.PEEK[HEADER.FIELDS (X-GM-LABELS)]`, but X-GM-LABELS is a Gmail FETCH attribute, not a header. It also costs one extra round-trip per message.
- **M5. Message-ID fallback uses an IMAP sequence number** (1955: `imap-msg-{num}`). Sequence numbers are reused after expunge, so a later message without a Message-ID that lands on the same number is **silently skipped forever**. Use the IMAP UID plus UIDVALIDITY instead.
- **M6. Ticket ID collisions [VERIFIED].** `generate_job_ticket_id` uses second-resolution time plus the last 6 alphanumerics of the Message-ID. For Gmail that is always `AILCOM` (from `mail.gmail.com`). Two leads in the same second get identical ticket IDs.
- **M7. Wrong service in the customer email [VERIFIED].** "My AC is broken and not cooling" → `AC Repair`, which is remapped to **`AC Installation`** because `AC Repair` is not in `services` (947–948). The customer is told "Service type: AC Installation". The fallback "smells weird" maps to AC Repair even for a furnace.
- **M8. Name extraction puts garbage in the greeting [VERIFIED].** "Hi, I am having trouble…" → name "Having Trouble With My". "This is urgent…" → "Urgent". The patterns `i am` and `this is` (1036) take priority over the real `From:` display name, and the result goes into "Hi {name}," in the outgoing email.
- **M9. Address and phone false positives [VERIFIED].** "20 minutes ago it stopped working completely" is extracted as an **address**. Any 10-digit number (order or account numbers) counts as a phone. A detected phone or address forces `customer_intent=True`, which overrides the marketing→spam rule.
- **M10. No outgoing rate limit.** Nothing caps replies per cycle or per day, which risks a Gmail sending ban (see H9).
- **M11. Replies are not threaded.** No `In-Reply-To` / `References` headers are set. The subject is "Re: Thanks for contacting X" rather than "Re: <original subject>".
- **M12. UI writes `config.json` on every checkbox toggle** (2751). The write is non-atomic, includes the runtime-only `config_path` key, and happens from the UI thread while the worker reads the same dict. Ticking "Auto Replies" while `dry_run=false` starts **live sending immediately** with no confirmation.
- **M13. Default mode is live.** Setup writes `dry_run=false` (3793, 3902). `start_agent.bat` and the UI pass no flag, so the daemon and the UI **send live** (dispatch summaries today; auto-replies if toggled on). The README implies `--live` is needed for live mode.
- **M14. Licensing is trivially bypassed.**
  - Setting `license_enforcement: false` disables it.
  - Any `HVAC-TRIAL-001…100` passes by regex **even when it is not in the file** (565–573), and that path returns *before* binding is checked.
  - A missing `valid_licenses.json` is auto-created with 100 valid keys (535–538).
  - `valid_licenses.json` has a UTF-8 BOM, so `json.loads` fails [VERIFIED] and the key set is empty. The GUI setup (3718, 3904) then rejects every key, while runtime accepts trial keys by regex.
  - `write_license_binding` overwrites the binding on every setup. The current `data/license_binding.json` is bound to a different email than `config.json`, and nothing notices.
  - Options are listed under "Decisions needed". No redesign without your approval.
- **M15. The EXE deployment has no background daemon.** The release folder contains only `hvac_installer.exe`, which is setup plus the UI. The agent runs only while the UI window is open. `install_startup_task.ps1` launches `start_agent.bat`, which needs Python and the `.py` source, and neither is in `release\`. `start_client.bat` calls `hvac_email_agent.exe`, which is not copied to `release\`, and it re-runs setup on every launch (M16).
- **M16. `--setup-and-ui` / quick setup clobbers settings.** Every run forces `feature_auto_reply=false`, `auto_reply_enabled=false`, and `dry_run=false`, and resets IMAP/SMTP to Gmail. `start_client.bat` uses it on every launch.

## LOW

- **L1. `scripts/migrate_csv_to_db.py` is broken twice [VERIFIED].** Run as documented, it fails with `ModuleNotFoundError: app`. Run as a module, it fails with `TypeError` (12 args passed to a 15-parameter `create_job`).
- **L2. Dead code.**
  - `app/setup_wizard.py` is never used, and its `prompt_text` duplicates the main file's.
  - `scheduler.next_available_slot` is unused, and its docstring says "+1 hour" but the code does not add an hour.
  - `technicians.set_technician_active` is unused.
  - `has_customer_intent` has two branches that both return True (870–875).
  - `ensure_csv_header` duplicates `ensure_csv_headers`.
  - `docs/` is empty.
- **L3. `run_setup` / `run_quick_setup` swallow every GUI exception** (`except Exception: pass`) and silently fall back to the console prompts.
- **L4. `list_open_jobs ORDER BY priority DESC`** sorts text, so "Normal" ranks above "High".
- **L5. `init_db` runs migrations and a backfill UPDATE on every call**, including every 5 s from the UI refresh. That causes needless write contention with the worker.
- **L6. Dashboard "today" uses the UTC date in some places and the local date in others.**
- **L7. The fatal-error fallback writes `error.log` at the root** (4021), while normal errors go to `logs/error.log`.
- **L8. PII in logs.** `logs/hvac_agent.log` stores the phone, address, and name of every lead, with no rotation or size cap. `error.log` has no rotation either.
- **L9. `zoneinfo` on Windows needs the `tzdata` package.** It is installed on this dev machine, but nothing declares it (there is no `requirements.txt`). On a machine without it, office hours silently run in UTC.
- **L10. `is_within_office_hours` end is inclusive.** 17:00:59 still counts as in-hours.
- **L11. Ticket card closures capture loop variables correctly**, but `details_text.bind("<Button-1>")` captures the last card's widget.
- **L12. Several runtime artifacts that should not be committed are not ignored:** `config.json`, `valid_licenses.json`, `*.db-wal`, `*.db-shm`.
- **L13. `.spec` files are gitignored but checked into the folder,** and they disagree with `build_exe.bat` flags (`--clean` vs `upx=True`). `build_exe.bat` regenerates them anyway.
- **L14. `hvac_installer.py` imports `validate_license` and runs it,** but does not call `ensure_runtime_paths` the way `main()` does.

---

## README / docs vs code mismatches

| README says | Reality |
|---|---|
| State files at root (`processed_ids.txt`, `sender_cooldown.json`, `follow_up_state.json`, `hvac_leads.csv`, …) | Config puts them all under `data/`; logs under `logs/`. |
| `start_client.bat` and `clean_release.bat` | Both exist. `start_client.bat` calls an EXE that the build does not put in `release\` (M15) and re-runs setup every launch (M16). |
| "`--dry-run` (safe)" | Not safe against a real mailbox (C2). |
| `--live` needed for live sending | Live is already the default after setup (M13). |
| Startup task for Windows deployment | Requires Python plus source; does not work with the EXE release (M15). |
| `scripts/migrate_csv_to_db.py` "one-time migration" | Broken (L1). |
| "ties default to existing for safety" | Ties lose leads (no ticket); not "safe" for the business (H4). |
| Classification "+2 / +2 / +3" | There are also hidden +5 bounce, +2 marketing, +4/+2 HVAC boost, +3 non-HVAC, and post-hoc overrides. |
| `ticket_duplicate_mode: delete` | Deletes nothing (M2). |
| Promotions filtering (implied by `ignore_promotions`) | Non-functional (M4); `ignore_promotions` is not in `CONFIG_REFERENCE.md`. |
| `high_priority_keywords` | Not documented in `CONFIG_REFERENCE.md`. |
| `ticket_duplicate_mode` | Listed twice in the README; the README is truncated mid-"CSV Schema". |
| Single-instance ("prevent duplicate responders") | Only the scheduled task is single-instance (C3). |
| `docs/` | Empty. |

All files referenced by the README exist. The problems are behavioral, not missing files.

---

## Answers to the brief's specific questions

- **Storage duplication.** Confirmed (M1). Plan: SQLite tables for `processed_messages`, `sender_cooldown`, `follow_ups`, `dispatch_queue`, `lead_log`, `service_requests`, `draft_replies`, `customers`, and `jobs`. CSVs become exports generated from the DB. A one-time importer reads existing CSV/JSON/txt state, after a backup.
- **Overlapping flags.** Confirmed (M3). Plan: one canonical key each (`auto_reply_enabled`, `follow_up_enabled`, `dispatch_summary_enabled`, `sms_enabled`). Old `feature_*` keys are still read; if both are present, a disagreement is treated as **off** (fail safe), with a warning. A migration script rewrites `config.json`. Startup validation reports every problem at once in plain English.
- **Credentials.** Confirmed plaintext (C5). Plan: `keyring` (Windows Credential Manager), with `email_password` in `config.json` only as a migration source that is removed after import.
- **Auto-reply safety.** Gaps in H1, H2, H9, C4. Cooldown and dedup do **not** reliably survive crashes (C1, H5) and are defeated by dry-run (C2), concurrency (C3), and reset (H7).
- **Classifier weaknesses.** H3 (substring false positives), H4 (everyday words → spam; HVAC words missing: boiler, radiator, "warm air", leak, noise, water heater, mini-split, AC unit), M7, M8, M9. The "existing" default drops leads.
- **Licensing.** Trivially bypassed (M14). Options are listed below. Not redesigned.
- **`ticket_duplicate_mode: delete`.** It cannot lose data; it does nothing. **`replace` can lose data** (M2).
- **Paths.** Mostly consistent under `data/` and `logs/` because `main()` chdirs to the config folder. Exceptions: fallback defaults in `.get()` point at the root (1851–1857, 2378, 2488), and so do the fatal log (L7) and the auto-created `valid_licenses.json`.
- **IMAP/SMTP errors.** No IMAP timeout (H10), no reconnect or backoff, partial-cycle loss (C1), and a poison message (H6). SMTP logs in again for every email; acceptable, but per-cycle reuse would be better.
- **PyInstaller and startup task.** The build runs but bundles secrets (C5). The release has no daemon (M15). The startup task needs Python plus source (M15).

---

## Phased fix plan (for approval)

### Phase 0: Safety net (no behavior change)
1. `git init` **inside this folder only** (see Decision 1). Add `config.json`, `valid_licenses.json`, `data/`, `logs/`, `dist/`, `build/`, `release/`, and `*.db-*` to `.gitignore` **before** the first commit. Add `config.example.json` with placeholders.
2. Back up `data/`, `logs/`, and `config.json` to `backups/2026-11-15/`, which is also gitignored.

### Phase 1: Stop wrong, duplicate, or lost emails and data (single file, minimal diffs, one commit per item)
1. **Single-instance lock** (lock file with PID and a stale check) shared by the daemon and the UI worker. (C3)
2. **Atomic writes** (temp file → fsync → `os.replace`) for every state, config, and CSV write. Corrupt state becomes a loud error plus a quarantined copy, never a silent `{}`. (H5)
3. **Per-message commit.** Mark a message processed and record its cooldown *before* sending (the "intent" record), then mark it sent. Wrap each message in try/except so one failure is logged and the loop continues. Use `BODY.PEEK[]` so fetching never sets `\Seen`, and set `\Seen` only after successful processing. Key dedup on Message-ID **plus** IMAP UID/UIDVALIDITY. (C1, H6, M5)
   - This moves dedup, cooldown, and the dispatch queue into SQLite now, because SQLite transactions are the simplest correct way to get crash safety. The CSV-to-export change follows in Phase 2.
4. **Dry-run becomes truly read-only** against the mailbox and state. It uses separate scratch state, or none. (C2)
5. **Never ingest our own mail.** Skip messages from our own address or with an `X-HVAC-Auto` header entirely. (C4)
6. **Reply safety.** Add List-*, bounce, Return-Path `<>`, `X-Autoreply` / `X-Autorespond`, `Precedence: auto_reply`, and alias checks. Add `Auto-Submitted: auto-replied` and `X-Auto-Response-Suppress: All` to everything we send. Add a per-day send cap. Stop echoing free-text issue and address in auto-replies (ticket ID plus a fixed text only). Enforcing DMARC/SPF pass via `Authentication-Results` is optional (see Decision 4). (H1, H9, M10)
7. **Follow-ups.** Route both follow-up paths through the same safety checks, cooldown, manual-reply detection, and a "created after feature-enable time" guard so enabling it never bursts old jobs. (H2)
8. **IMAP/SMTP robustness.** Timeouts, a reconnect per cycle, and exponential backoff on auth or network failure. (H10)
9. **Classifier hot fixes.** Remove the substring fallback. Trim marketing words that real customers use. Add missing HVAC terms. Change the default for no-score/tie to `review` (ticket created, **no auto-reply**). Stop "AC Repair → AC Installation" remapping in customer text. Prefer the `From:` display name over `this is` / `i am` captures. (H3, H4, M7, M8, M9)
10. **Customer data.** The upsert never overwrites a non-empty field with an empty or "Unknown" one. (H8)
11. **Duplicates.** `replace` keeps `ticket_id`, `created_at`, `status`, and `technician`. `delete` is renamed to `ignore`, with `delete` kept as an alias. (M2)
12. **Resets.** UI reset and `reset_install.bat` first copy everything to `backups/<timestamp>/`. (H7)
13. **Config.** Validate on startup with clear errors. Collapse the flag pairs (old keys still accepted). Add `scripts/migrate_config.py`. (M3)
14. **Credentials.** Move to Windows Credential Manager via `keyring` with fallback messaging. Remove `config.json` from the PyInstaller bundle. (C5)

### Phase 2: Split into modules
`hvac/config.py`, `mail.py` (IMAP/SMTP), `classifier.py`, `extraction.py`, `store.py` (SQLite: the single source of truth, with CSV export), `tickets.py`, `followups.py`, `dispatch.py`, `safety.py` (reply-suppression rules), `ui.py`, and `cli.py`. `hvac_email_agent.py` stays as a thin entrypoint so every existing CLI flag keeps working. Fix the migration script (L1), remove dead code (L2), and fix the build and startup scripts (M15, M16).

### Phase 3: pytest
Classifier tables (positives, negatives, and the verified failure cases above), extraction, cooldown, dedup across a simulated crash, the dry-run no-side-effect guarantee, reply suppression (each header), and follow-up logic. Tests run against a `FakeIMAP` / `FakeSMTP` with no network.

### Phase 4: README
Rewrite to match the code, and fold `CONFIG_REFERENCE.md` into it or regenerate it from the config schema.

---

## Decisions needed from you

1. **Git.** This folder has no repo of its own. Its parent, `Documents/Projects/CodeVs`, *is* a git repo (no commits) shared with other projects. OK to `git init` a **nested** repo here? I will not touch the parent repo.
2. **Folder path.** The brief says `Documents/Projects/hvac`; the project is at `Documents/Projects/CodeVs/Hvac, automated emailing`. A near-empty stray copy (only `config.json` plus an empty `.git`, created today 11:39) is at `Documents/CodeVs/Hvac, automated emailing`. I am treating the first as the project and ignoring the stray copy. Correct?
3. **Rotate the Gmail app password?** Recommended (C5). You do this in your Google account; I won't.
4. **Spoofing protection (H9).** Only auto-reply when Gmail's `Authentication-Results` shows SPF or DKIM pass? This is safer, but a few legitimate senders with broken mail setups would get a ticket and no auto-reply.
5. **Unclassified mail.** Use a new `review` class (ticket, no reply) instead of the current "existing" default?
6. **Licensing options, pick one later; I won't build any yet.**
   - (a) Signed license file: an Ed25519 signature over {key, email, expiry} with the public key embedded. Offline, but can be patched out of a Python EXE.
   - (b) An online activation server with periodic check-in.
   - (c) Accept it as honor-system and simplify: remove the regex bypass and BOM bug.
7. **Daemon for the EXE deployment (M15).**
   - (a) Ship a headless `hvac_agent.exe --daemon` plus a startup task that points at it.
   - (b) Make the UI minimize to tray and auto-start.
8. **New dependency.** `keyring` (pure Python, uses Windows Credential Manager). OK to add a `requirements.txt` with `keyring` and `tzdata`, plus `pytest` for dev?
9. **Tests in Phase 1?** Phase 1 changes safety-critical paths. I'd like to add a minimal fake-mailbox test harness *during* Phase 1 so each fix is verified, then expand it in Phase 3. OK?

---

## Phase 1 results (2026-11-15)

Decisions applied: the defaults approved on 2026-11-15. Tests: `82 passed` (`.venv\Scripts\python -m pytest`), all against fake IMAP/SMTP servers.

### Found during Phase 1 (not in the original audit)

| ID | Severity | Finding | Status |
|----|----------|---------|--------|
| N1 | Critical | The **public** GitHub repo `Dookoo211/Hvac-automated-ticketing` tracked `config.json`, which contains the live Gmail app password (since 2026-05-18). It also tracked the SQLite DB, `logs/error.log`, `valid_licenses.json`, and `data/license_binding.json`, which holds a third party's email address. | Removed from the tip of `main` (commit 9d12af8). **Still in git history: the password must be revoked by the owner.** A history rewrite (force-push) is pending a decision. |
| N2 | High | Sent-folder names were sent to IMAP unquoted (`SELECT [Gmail]/Sent Mail`), so manual-reply detection never worked and follow-ups would have been sent even after staff replied. | Fixed (`app/mailbox.quote_mailbox`). |
| N3 | Low | UI functions `update_job` and `add_technician_ui` reference variables that are never defined (`job_id_var`, `tech_var`, …). They are not wired to any button today, so they are dead code that would crash if wired. | Open (Phase 2 cleanup). |
| N4 | Medium | `detect_service`, `is_hvac_related`, and `infer_priority` used their own substring checks. For example, "heater" matched inside "theater", and "dead" inside "deadline" set High priority. | Fixed (whole-word matching everywhere). |

### Status of original findings

| ID | Status | Notes |
|----|--------|-------|
| C1 | Fixed | One SQLite transaction per email; intent-to-send committed before sending; interrupted sends are never resent (`outbound_messages.status='unknown'`). |
| C2 | Fixed | Dry-run uses an in-memory DB copy, read-only mailbox selection, and no SMTP. |
| C3 | Fixed | OS-level lock `data/agent.lock` shared by the daemon, one-shot CLI, and UI worker. |
| C4 | Fixed | Own addresses (with +tag and Gmail-dot aliases), extra `own_addresses`, and any `X-HVAC-Auto` mail are ignored entirely. |
| C5 | Fixed in code | Password lives in Windows Credential Manager; config writes strip it; EXEs no longer bundle `config.json`. Your local `config.json` still has the plaintext password until you run `scripts/migrate_config.py` (see HANDOFF). History: see N1. |
| H1 | Fixed | Covers List-\*, Precedence (including auto_reply), Auto-Submitted, X-Autoreply/Autorespond, X-Auto-Response-Suppress, Return-Path `<>`, delivery reports, and no-reply local parts. Outgoing mail sets Auto-Submitted and X-Auto-Response-Suppress. |
| H2 | Fixed | Single follow-up path with safety checks; cancelled on a manual reply, job progress, being >48 h overdue, or the feature being disabled; held if the Sent folder is unreadable. |
| H3, H4 | Fixed | Whole-word matching; trimmed marketing list; new HVAC vocabulary; new `review` class. The classifier is still keyword-based and will have misses: see "Known classifier limits" below. |
| H5 | Fixed (state) | All agent state is in SQLite; config and CSV writes are atomic. The license binding file still uses a plain write (licensing deferred by decision). |
| H6 | Fixed | Per-message try/except; a failure is recorded, retried up to 3 times, and the message is left unread for staff. |
| H7 | Fixed | The UI reset refuses while the agent is running and backs up first; `reset_install.bat` backs up first. Both abort if the backup fails. |
| H8 | Fixed | Customer merge never overwrites a known value with a blank or "Unknown". |
| H9 | Fixed | Requires an aligned DKIM/SPF or DMARC pass in the top-most Authentication-Results from `mx.google.com`. Acks no longer echo the body, address, or Maps link; the subject is echoed for threading only. |
| H10 | Fixed | IMAP/SMTP timeouts (`network_timeout_seconds`), and exponential backoff capped at 15 min (30 min after a login rejection). |
| M1 | Fixed | SQLite is the single source; CSVs are regenerated exports with the same columns. Legacy `job_tickets.csv` rows are not imported, because they were mirrored in the DB already. |
| M2 | Fixed | Every mode keeps the ticket ID, status, technician, and schedule. `delete` is accepted as an alias for `ignore`. |
| M3 | Fixed | Canonical flags plus legacy aliases (disagreement means off); full validation at startup. |
| M4 | Fixed / changed | The configured `mailbox` is honoured. `ignore_promotions` is accepted but currently does nothing (it never worked). Proper Gmail category filtering needs live testing (Phase 2). |
| M5, M6, M7, M8, M9, M10, M11 | Fixed | |
| M12 | Fixed | Toggles patch only their own keys, and enabling live auto-replies asks for confirmation. |
| M13 | Open | Setup still defaults to live mode (auto-reply itself defaults off). To revisit with M16 in Phase 2. |
| M14 | Open (by decision) | Licensing is untouched. Options are listed in "Decisions needed". |
| M15, M16 | Open | Phase 2: headless daemon EXE plus a startup task (decision 7), and a setup that does not clobber settings. |
| L1 | Open | `scripts/migrate_csv_to_db.py` is still broken. Superseded by the automatic legacy import; delete or fix in Phase 2. |
| L5, L6, L7, L9, L12, L13, L14 | Fixed | |
| L2, L3, L4, L8, L10, L11 | Open | Phase 2. |

### New config keys (all optional, with safe defaults)
`own_addresses` [], `mark_processed_as_read` true, `require_sender_auth` true, `trusted_authserv_ids` ["mx.google.com"],
`max_auto_emails_per_day` 50, `max_sms_per_day` 30, `max_reply_age_hours` 48 (0 = no limit), `network_timeout_seconds` 30.

### Known classifier limits (still true after Phase 1)
- The classifier is still keyword-based. Leads that use none of the vocabulary become `review` (ticket, no reply) if they are HVAC-related; otherwise they become `existing` (no ticket).
- `existing` customers reporting a new problem get no ticket, as before.
- Sender authentication assumes Gmail. For another provider, set `trusted_authserv_ids`; otherwise every auto-reply is held (a startup warning says so).

### Not verified (needs a live mailbox or a build)
- Real Gmail behaviour: IMAP `UID`, `BODY.PEEK`, INTERNALDATE parsing, the real `Authentication-Results` format, and whether Gmail drops self-sent mail into the inbox.
- PyInstaller build with keyring/tzdata collection. The UI was only smoke-tested (built, one refresh, closed), never clicked through.
