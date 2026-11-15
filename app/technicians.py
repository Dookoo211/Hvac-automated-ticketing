#!/usr/bin/env python3
"""Technician management functions."""

from __future__ import annotations

from pathlib import Path

from .database import get_connection


def list_technicians(db_path: Path) -> list[dict]:
    """Return all technicians."""
    conn = get_connection(db_path)
    try:
        return conn.execute("SELECT * FROM technicians ORDER BY name").fetchall()
    finally:
        conn.close()


def add_technician(db_path: Path, name: str, phone: str, service_area: str, active: int = 1) -> None:
    """Add a new technician."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO technicians (name, phone, service_area, active)
            VALUES (?, ?, ?, ?)
            """,
            (name, phone, service_area, active),
        )
        conn.commit()
    finally:
        conn.close()


def set_technician_active(db_path: Path, technician_id: int, active: int) -> None:
    """Enable or disable a technician."""
    conn = get_connection(db_path)
    try:
        conn.execute("UPDATE technicians SET active = ? WHERE technician_id = ?", (active, technician_id))
        conn.commit()
    finally:
        conn.close()


def technician_workload(db_path: Path) -> list[dict]:
    """Return count of open jobs per technician."""
    conn = get_connection(db_path)
    try:
        return conn.execute(
            """
            SELECT technician_assigned AS technician, COUNT(*) AS open_jobs
            FROM jobs
            WHERE status IN ('new', 'scheduled', 'in progress')
            GROUP BY technician_assigned
            ORDER BY open_jobs DESC
            """
        ).fetchall()
    finally:
        conn.close()

