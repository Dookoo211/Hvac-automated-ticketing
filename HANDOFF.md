# HANDOFF
## Goal (1-2 lines)
Make the local-first HVAC email auto-responder (Python, Windows) reliable and safe-failing: audit, then fix in phases (safety bugs → modules → tests → README).

## Current status (what works now)
Step 1 (audit) DONE: see AUDIT.md. No code changed. Waiting for user approval of the phased plan and answers to the "Decisions needed" in AUDIT.md.
Current code is UNSAFE to run against a real mailbox, even with --dry-run (AUDIT C2). Only `--demo` is safe.

## Done (checklist, newest last)
- [x] Located project folder (see Known issues: path differs from brief)
- [x] Checked git status (see Known issues: parent repo)
- [x] Created HANDOFF.md
- [x] Read every source, script, doc, and config file (config read with secrets redacted)
- [x] Offline verification: --demo, classifier probes, migration script in a temp copy (deleted afterwards)
- [x] Wrote AUDIT.md (5 critical, 10 high, 16 medium, 14 low + phased plan + 9 decisions)

## In progress (exact file + what's left)
- Nothing. Blocked on user approval.

## Next steps (ordered)
1. User answers AUDIT.md "Decisions needed" (git nested repo, folder confirm, H9 auth check, review class, licensing option, daemon option, keyring/requirements, tests-in-phase-1)
2. Phase 0: git init (nested, this folder only), .gitignore secrets FIRST, config.example.json, backup data/ logs/ config.json to backups/
3. Phase 1 items 1–14 in AUDIT.md, one commit each
4. Phase 2 modules → Phase 3 pytest → Phase 4 README

## File map (path: one-line purpose)
- HANDOFF.md: this file
- AUDIT.md: ranked findings + phased plan + open decisions
- hvac_email_agent.py: monolith (config, IMAP/SMTP, classifier, extraction, CSV/JSON state, tickets, follow-ups, dispatch, Tk UI, setup wizards, CLI)
- app/database.py: SQLite schema + ad-hoc column migrations
- app/crm.py: customers/jobs CRUD
- app/technicians.py, app/scheduler.py: technician list, slot conflict check
- app/notifications.py: SMS via email gateway
- app/setup_wizard.py: DEAD (unused)
- hvac_agent.py / hvac_agent_ui.py: thin entrypoints
- hvac_installer.py: EXE entry (setup GUI then UI)
- scripts/migrate_csv_to_db.py: BROKEN (import + arg count)
- build_exe.bat, *.spec: PyInstaller (bundles config.json = secret leak)
- start_agent.bat, install/uninstall_startup_task.ps1: daemon + logon task (needs python + source)
- start_client.bat, clean_release.bat, reset_install.bat: client launcher, clean build, destructive reset
- config.json: live config INCLUDING plaintext password (never print/commit)
- valid_licenses.json: local license list (has UTF-8 BOM → fails to parse)
- data/: CSVs, SQLite DB (currently 0 rows), license_binding.json
- logs/error.log: past errors (Tk errors, IMAP auth failures)

## Decisions made (and why)
- Treating `Documents/Projects/CodeVs/Hvac, automated emailing` as the HVAC project folder: it is the only complete copy under Documents/Projects (the brief's `Documents/Projects/hvac` does not exist). Pending user confirmation.
- Did not git init yet: parent folder is a shared repo; asking first.

## Setup/env (tools installed, env vars needed, NO secret values)
- Python 3.11 (C:\Users\Lucap\AppData\Local\Programs\Python\Python311); tzdata 2025.3 present. Nothing installed by me.
- Stdlib only today; no requirements.txt.

## Known issues / gotchas
- Project path contains a comma and spaces: always quote it.
- This folder has NO git repo of its own. It sits inside `Documents/Projects/CodeVs`, which is a git repo (no commits) shared with other projects. Do not commit there.
- A second near-empty copy exists at `Documents/CodeVs/Hvac, automated emailing` (only config.json + empty .git, created 2026-11-15 11:39). Not touched.
- config.json holds a plaintext mailbox app password and license key; `.gitignore` does not exclude config.json. Recommend rotating the password.
- `--dry-run` marks mail read and consumes leads: do NOT run it against a real mailbox until Phase 1 item 4.
- logs/error.log paths show the code previously lived at Documents\CodeVs\...

## Test results (last run: passed / failed / couldn't verify)
- 2026-11-15 `--demo`: 5/5 samples matched expected (the demo set is too easy; see AUDIT H3/H4 for verified failures).
- Couldn't verify: anything needing a live mailbox (Gmail self-sent-to-inbox behavior for C4, real IMAP timeouts).

## Last updated: 2026-11-15
