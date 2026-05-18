#!/usr/bin/env python3
"""Basic scheduling logic to avoid overlaps."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .database import get_connection


def is_slot_available(db_path: Path, technician: str, scheduled_time: str) -> bool:
    """Return False when technician already has a job at scheduled_time."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            """
            SELECT job_id FROM jobs
            WHERE technician_assigned = ? AND scheduled_time = ?
              AND status IN ('scheduled', 'in progress')
            LIMIT 1
            """,
            (technician, scheduled_time),
        ).fetchone()
        return row is None
    finally:
        conn.close()


def next_available_slot(db_path: Path, technician: str) -> str:
    """Return a naive next available slot (current local time + 1 hour)."""
    return (datetime.now().astimezone().replace(minute=0, second=0, microsecond=0)).isoformat()

