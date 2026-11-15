# HANDOFF
## Goal (1-2 lines)
Make the local-first HVAC email auto-responder (Python, Windows) reliable and safe-failing: audit, then fix in phases (safety bugs → modules → tests → README).

## Current status (what works now)
- Phase 0 and Phase 1 are DONE and pushed to https://github.com/Dookoo211/Hvac-automated-ticketing (branch `main`).
- The inbox cycle is crash-safe. SQLite is the single source of truth and CSVs are exports. Dry-run is read-only. Reply-safety rules, caps, the single-instance lock and backoff are all in place.
- 82 pytest tests pass against fake IMAP/SMTP (no network).
- NOT yet run against the real mailbox.
- Local config.json migrated 2026-11-15: no password in the file, password in Windows Credential Manager (service HVACEmailAgent), flags collapsed; backup without password: config.json.bak-20261115-122820.

## Done (checklist, newest last)
- [x] Located project folder; created HANDOFF.md
- [x] Audit → AUDIT.md (approved by user 2026-11-15 with the default answers to all 9 decisions)
- [x] Found that the public GitHub repo exposed the Gmail app password (AUDIT N1); told the user to revoke it
- [x] Phase 0: full backup in `backups/phase0-20261115-120325/`; nested git repo built on top of GitHub history; secrets/data/logs untracked + gitignored; `config.example.json`; pushed (commit 9d12af8)
- [x] Created `.venv` (keyring, tzdata, pytest, pyflakes); requirements.txt / requirements-dev.txt
- [x] Config validation + flag collapse (app/settings.py), atomic writes, credentials module (commit 80011c4)
- [x] Classifier/extraction fixes (commit 97b02c0)
- [x] Crash-safe cycle, SQLite state store, legacy import, mail wrappers, safety rules, UI/setup wiring (commit c318590)
- [x] Fake-mailbox test suite (commit a3c1ba9); mutation-checked (4 deliberate regressions each caught)
- [x] reset_install.bat backs up first (tested in a temp folder); build no longer bundles config; scripts/migrate_config.py (commit e8dfa1d)
- [x] AUDIT.md "Phase 1 results" + this file

## In progress (exact file + what's left)
- Nothing in progress. Waiting on the user for: password revocation and the history-rewrite decision.

## Next steps (ordered)
1. USER: revoke the exposed Gmail app password (Google Account → Security → App passwords) and create a new one.
2. DONE 2026-11-15: migrate_config.py run (with user approval). After revoking, the user saves the NEW app password with `.venv\Scripts\python hvac_email_agent.py --quick-setup` (it overwrites the Credential Manager entry). Note: quick setup currently resets auto_reply/dry_run (M16).
3. Decide on a history rewrite of the public repo (needs a force-push) to purge old commits containing the password. Revoking is what actually protects the account.
4. First live verification with the user present: `--dry-run` (now safe) against the real mailbox, then a one-shot live run with auto_reply off.
5. Phase 2: split into modules (hvac/config, mail, classifier, extraction, store, tickets, followups, dispatch, safety, ui, cli). Also: headless daemon EXE + startup task (M15, decision 7), setup must not clobber settings (M16), M13, delete or fix migrate_csv_to_db.py (L1), dead UI functions (N3), L2–L4, L8, L10, L11, Gmail category filtering for `ignore_promotions` (M4).
6. Phase 3: expand tests (more classifier tables, UI-free CLI tests, mailbox wrapper parsing with recorded IMAP responses).
7. Phase 4: rewrite README to match the code.
8. Licensing: present options (AUDIT "Decisions needed" 6) and wait for the user's choice.

## File map (path: one-line purpose)
- HANDOFF.md: this file
- AUDIT.md: findings, fix plan, decisions, Phase 1 results/status table
- hvac_email_agent.py: still the main monolith (config load, classifier, extraction, inbox cycle, follow-ups, dispatch, Tk UI, setup wizards, CLI)
- app/settings.py: canonical flags, legacy aliases, startup validation (ConfigError)
- app/credentials.py: Windows Credential Manager via keyring (+ legacy plaintext fallback)
- app/store.py: SQLite state store (dedup, cooldown, outbound log, follow-ups, dispatch queue, lead log, requests, drafts, jobs), CSV export
- app/legacy_import.py: one-time import of old txt/JSON/CSV state (with backup)
- app/mailbox.py: ImapMailbox / SmtpSender with timeouts, PEEK fetch, error categories
- app/safety.py: reply-suppression rules, sender authentication, own-address detection, outgoing auto headers
- app/fileutil.py: atomic writes, backups
- app/instance_lock.py: single-instance OS lock
- app/database.py: CRM schema (jobs/customers/technicians), now also on a given connection
- app/crm.py, technicians.py, scheduler.py: UI-side CRUD (unchanged)
- app/notifications.py: unused now (SMS goes through the cycle's Outbox); remove in Phase 2
- app/setup_wizard.py: dead (unused)
- hvac_installer.py: EXE entry (setup GUI → UI); keyring login check; error dialogs
- scripts/migrate_config.py: config cleanup + password → Credential Manager
- scripts/migrate_csv_to_db.py: BROKEN, superseded by legacy import (Phase 2)
- tests/: fakes.py (FakeMailbox/FakeSmtp/Clock), conftest.py, test_cycle_safety.py, test_followups_dispatch.py, test_units.py, test_migrate_config.py
- build_exe.bat, *.spec: PyInstaller (no config bundling); .spec files are gitignored
- start_agent.bat, install/uninstall_startup_task.ps1: daemon + logon task (still need python + source; Phase 2)
- start_client.bat, clean_release.bat, reset_install.bat
- config.example.json: template (no secrets). config.json: local only, gitignored
- backups/: gitignored backups (phase0 snapshot, legacy-import, ui-reset, reset-*)

## Decisions made (and why)
- Project folder = `Documents/Projects/CodeVs/Hvac, automated emailing` (confirmed by user approval).
- Git: nested repo in this folder, stacked on the existing GitHub history (fast-forward pushes, no force-push). The parent CodeVs repo was never touched.
- Approved defaults: sender auth required for auto-replies; new `review` class; licensing deferred; headless daemon EXE (Phase 2); keyring + tzdata + pytest; tests written during Phase 1.
- Sender auth is slightly stricter than "SPF or DKIM pass": the domain must ALIGN with From: (or DMARC pass). An unaligned SPF pass does not prove the visible From:.
- At-most-once delivery: if a crash leaves a send's outcome unknown, the customer email is NOT resent (a ticket still exists). Internal dispatch summaries ARE retried (a duplicate is harmless).
- Never auto-ack mail older than 48h (protects the first run against a backlog of old unread mail).
- Handled mail is still marked read (old behaviour), but only after processing; set `mark_processed_as_read: false` to leave mail unread for staff.
- Legacy follow-ups are imported as cancelled (no surprise emails after the upgrade).
- Did NOT auto-migrate the plaintext password at runtime: it touches the user's Credential Manager, so it's an explicit script/setup step.

## Setup/env (tools installed, env vars needed, NO secret values)
- Python 3.11. Project venv: `.venv` (keyring 25.7, tzdata 2026.6, pytest 9.2, pyflakes). Run tests: `.venv\Scripts\python -m pytest`
- No env vars. Password: Windows Credential Manager, service "HVACEmailAgent", account = mailbox address.
- Git remote: origin = github.com/Dookoo211/Hvac-automated-ticketing (PUBLIC). gh CLI authenticated as Dookoo211.

## Known issues / gotchas
- Project path contains a comma and spaces: always quote it.
- The PUBLIC repo's history (commits before 9d12af8) still contains the Gmail app password and a third party's email address. Commit d945318 (this session, fixed in e8c38f9) also contains the mailbox's username as an example string. A history rewrite would cover all of these.
- Before every push: `git grep -niE '<real addresses>' HEAD` must return nothing, and the push must be gated on that result (a missed check let d945318 through).
- Plaintext copies of the OLD password still exist locally in: backups/phase0-20261115-120325/config.json (gitignored) and the stray Documents/CodeVs/Hvac, automated emailing/config.json (not touched). They're harmless once the password is revoked; delete only with the user's OK.
- `ignore_promotions` is accepted but has no effect (the old filter never worked).
- Classifier is keyword-based; see AUDIT "Known classifier limits".
- Writing files from bash heredocs with backslash-heavy Python failed once; put patch scripts in the scratchpad and run them.
- Stray near-empty copy at `Documents/CodeVs/Hvac, automated emailing` (not touched).

## Test results (last run: passed / failed / couldn't verify)
- 2026-11-15: `.venv\Scripts\python -m pytest` → 82 passed. pyflakes clean except the pre-existing dead UI functions (N3).
- Mutation check: disabling cooldown, own-mail filter, list-header check, or crash recovery each makes a test fail.
- `--demo`: 5/5. UI smoke test (build + one refresh, dry-run, temp config): OK.
- reset_install.bat: tested in a temp folder; backup made before delete.
- Couldn't verify: real Gmail IMAP/SMTP behaviour, a real Authentication-Results header, PyInstaller build, interactive UI clicking, startup task.

## Last updated: 2026-11-15 (end of Phase 1, config migrated)
