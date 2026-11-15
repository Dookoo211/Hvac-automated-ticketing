#!/usr/bin/env python3
"""SQLite storage for HVAC agent."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable


def dict_factory(cursor: sqlite3.Cursor, row: Iterable) -> dict:
    """Return SQLite rows as dictionaries keyed by column name."""
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with safe defaults."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = dict_factory
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: Path) -> None:
    """Create tables if they do not already exist (opens its own connection)."""
    conn = get_connection(db_path)
    try:
        init_schema(conn)
    finally:
        conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
    """Create/upgrade the CRM tables on an existing connection.

    WHY split out: the agent now runs each email inside one transaction on a
    single connection (and dry-run uses an in-memory copy), so schema setup
    must work on a connection it is handed instead of opening its own.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS customers (
            customer_id INTEGER PRIMARY KEY AUTOINCREMENT,
            first_name TEXT,
            last_name TEXT,
            email TEXT UNIQUE,
            phone TEXT,
            address TEXT,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS technicians (
            technician_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT,
            service_area TEXT,
            active INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS jobs (
            job_id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            first_name TEXT,
            last_name TEXT,
            phone TEXT,
            service_type TEXT,
            issue_description TEXT,
            priority TEXT,
            status TEXT,
            created_at TEXT,
            scheduled_time TEXT,
            technician_assigned TEXT,
            last_updated_at TEXT,
            occurrence_count INTEGER DEFAULT 1,
            address TEXT,
            followup_sent_at TEXT,
            FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
        );

        CREATE TABLE IF NOT EXISTS job_history (
            job_id INTEGER,
            technician_notes TEXT,
            completion_time TEXT,
            parts_used TEXT,
            invoice_amount REAL,
            FOREIGN KEY (job_id) REFERENCES jobs(job_id)
        );
        """
    )
    _ensure_columns(
        conn,
        "jobs",
        [
            "address",
            "last_updated_at",
            "occurrence_count",
            "followup_sent_at",
            "first_name",
            "last_name",
            "phone",
            # Phase 1 columns: tickets now live only in SQLite (CSV is an export).
            "ticket_id",
            "maps_link",
            "request_type",
            "source_message_key",
            "needs_review",
        ],
    )
    conn.execute(
        """
        UPDATE jobs
        SET status = 'new'
        WHERE status IS NULL OR TRIM(status) = ''
        """
    )
    conn.execute(
        """
        UPDATE jobs
        SET first_name = COALESCE(
                NULLIF(first_name, ''),
                (SELECT first_name FROM customers WHERE customers.customer_id = jobs.customer_id)
            ),
            last_name = COALESCE(
                NULLIF(last_name, ''),
                (SELECT last_name FROM customers WHERE customers.customer_id = jobs.customer_id)
            ),
            phone = COALESCE(
                NULLIF(phone, ''),
                (SELECT phone FROM customers WHERE customers.customer_id = jobs.customer_id)
            )
        WHERE (first_name IS NULL OR first_name = '')
           OR (last_name IS NULL OR last_name = '')
           OR (phone IS NULL OR phone = '')
        """
    )
    conn.commit()


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: list[str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for column in columns:
        if column in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
