#!/usr/bin/env python3
"""Customer and job CRM operations."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from .database import get_connection


def upsert_customer(db_path: Path, first_name: str, last_name: str, email: str, phone: str, address: str) -> int:
    """Insert or update a customer and return customer_id."""
    conn = get_connection(db_path)
    try:
        existing = conn.execute(
            "SELECT customer_id FROM customers WHERE email = ?",
            (email,),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE customers
                SET first_name = ?, last_name = ?, phone = ?, address = ?
                WHERE customer_id = ?
                """,
                (first_name, last_name, phone, address, existing["customer_id"]),
            )
            conn.commit()
            return int(existing["customer_id"])

        conn.execute(
            """
            INSERT INTO customers (first_name, last_name, email, phone, address, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (first_name, last_name, email, phone, address, ""),
        )
        conn.commit()
        customer_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        return int(customer_id)
    finally:
        conn.close()


def get_customer_by_email(db_path: Path, email: str) -> Optional[dict]:
    """Fetch an existing customer record by email."""
    if not email:
        return None
    conn = get_connection(db_path)
    try:
        return conn.execute(
            "SELECT * FROM customers WHERE email = ?",
            (email,),
        ).fetchone()
    finally:
        conn.close()


def find_duplicate_job(
    db_path: Path,
    customer_id: int,
    issue_description: str,
    address: str,
    service_type: str,
) -> Optional[dict]:
    """Find an open job with same core attributes."""
    conn = get_connection(db_path)
    try:
        return conn.execute(
            """
            SELECT * FROM jobs
            WHERE customer_id = ?
              AND LOWER(issue_description) = LOWER(?)
              AND LOWER(IFNULL(address, '')) = LOWER(?)
              AND LOWER(IFNULL(service_type, '')) = LOWER(?)
              AND status IN ('new', 'scheduled', 'in progress')
            ORDER BY job_id DESC
            LIMIT 1
            """,
            (customer_id, issue_description, address, service_type),
        ).fetchone()
    finally:
        conn.close()


def create_job(
    db_path: Path,
    customer_id: int,
    first_name: str,
    last_name: str,
    phone: str,
    service_type: str,
    issue_description: str,
    priority: str,
    status: str,
    created_at: str,
    scheduled_time: str,
    technician_assigned: str,
    last_updated_at: str,
    occurrence_count: int,
    address: str,
) -> int:
    """Create a new job record and return job_id."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO jobs (
                customer_id, first_name, last_name, phone, service_type, issue_description, priority, status,
                created_at, scheduled_time, technician_assigned, last_updated_at, occurrence_count, address
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                customer_id,
                first_name,
                last_name,
                phone,
                service_type,
                issue_description,
                priority,
                status,
                created_at,
                scheduled_time,
                technician_assigned,
                last_updated_at,
                occurrence_count,
                address,
            ),
        )
        conn.commit()
        job_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        return int(job_id)
    finally:
        conn.close()


def update_job_duplicate(
    db_path: Path,
    job_id: int,
    issue: str,
    priority: str,
    service_type: str,
    address: str,
    first_name: str,
    last_name: str,
    phone: str,
) -> None:
    """Update existing job when a duplicate request arrives."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            UPDATE jobs
            SET issue_description = ?, priority = ?, service_type = ?, address = ?,
                first_name = ?, last_name = ?, phone = ?,
                last_updated_at = ?, occurrence_count = occurrence_count + 1
            WHERE job_id = ?
            """,
            (
                issue,
                priority,
                service_type,
                address,
                first_name,
                last_name,
                phone,
                datetime.now().astimezone().isoformat(),
                job_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def list_open_jobs(db_path: Path) -> list[dict]:
    """Return all open jobs for dispatch board."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT
                j.*,
                COALESCE(NULLIF(j.first_name, ''), c.first_name) AS first_name,
                COALESCE(NULLIF(j.last_name, ''), c.last_name) AS last_name,
                c.email,
                COALESCE(NULLIF(j.phone, ''), c.phone) AS phone,
                COALESCE(NULLIF(j.address, ''), c.address) AS customer_address
            FROM jobs j
            JOIN customers c ON c.customer_id = j.customer_id
            WHERE j.status IN ('new', 'scheduled', 'in progress')
            ORDER BY j.priority DESC, j.created_at ASC
            """
        ).fetchall()
        return rows
    finally:
        conn.close()


def list_jobs_by_status(db_path: Path, statuses: list[str]) -> list[dict]:
    """Return jobs matching the supplied status list."""
    if not statuses:
        return []
    placeholders = ",".join("?" for _ in statuses)
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            f"""
            SELECT
                j.*,
                COALESCE(NULLIF(j.first_name, ''), c.first_name) AS first_name,
                COALESCE(NULLIF(j.last_name, ''), c.last_name) AS last_name,
                c.email,
                COALESCE(NULLIF(j.phone, ''), c.phone) AS phone,
                COALESCE(NULLIF(j.address, ''), c.address) AS customer_address
            FROM jobs j
            JOIN customers c ON c.customer_id = j.customer_id
            WHERE j.status IN ({placeholders})
            ORDER BY j.created_at DESC
            """,
            tuple(statuses),
        ).fetchall()
        return rows
    finally:
        conn.close()


def update_job_status(db_path: Path, job_id: int, status: str, scheduled_time: str, technician: str) -> None:
    """Update job status and scheduling details."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, scheduled_time = ?, technician_assigned = ?, last_updated_at = ?
            WHERE job_id = ?
            """,
            (status, scheduled_time, technician, datetime.now().astimezone().isoformat(), job_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_job_assignment(db_path: Path, job_id: int, technician: str) -> None:
    """Update assigned technician for a job."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            UPDATE jobs
            SET technician_assigned = ?, last_updated_at = ?
            WHERE job_id = ?
            """,
            (technician, datetime.now().astimezone().isoformat(), job_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_job_status(db_path: Path, job_id: int, status: str) -> None:
    """Set job status without changing schedule/technician."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, last_updated_at = ?
            WHERE job_id = ?
            """,
            (status, datetime.now().astimezone().isoformat(), job_id),
        )
        conn.commit()
    finally:
        conn.close()
