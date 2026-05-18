#!/usr/bin/env python3
"""Migrate existing CSV job tickets into SQLite database."""

from __future__ import annotations

import csv
from pathlib import Path

from app import crm, database


def migrate(csv_path: Path, db_path: Path) -> None:
    database.init_db(db_path)
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)

    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            customer_id = crm.upsert_customer(
                db_path,
                row.get("first_name", ""),
                row.get("last_name", ""),
                row.get("sender_email", ""),
                row.get("phone", ""),
                row.get("address", ""),
            )
            crm.create_job(
                db_path,
                customer_id,
                row.get("service_detected", ""),
                row.get("issue", ""),
                row.get("priority", ""),
                "new",
                row.get("timestamp", ""),
                "",
                "",
                row.get("timestamp", ""),
                1,
                row.get("address", ""),
            )


if __name__ == "__main__":
    migrate(Path("data/job_tickets.csv"), Path("data/hvac_agent.db"))
