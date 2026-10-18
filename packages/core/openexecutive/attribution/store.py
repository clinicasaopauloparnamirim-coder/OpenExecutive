"""SQLite ledger with idempotent ingest and revenue counted only after verification."""
from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openexecutive.attribution.models import MarketingAttributionEvent

DB_PATH = Path(os.environ.get("MARKETING_ATTRIBUTION_DB_PATH") or "./marketing_attribution.db")


class EventConflict(Exception):
    """An event ID was reused with different content or a sale ID was reused."""


def _connect(path: Path | None = None) -> sqlite3.Connection:
    target = Path(path or DB_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def initialize_db(path: Path | None = None) -> None:
    conn = _connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS marketing_attribution_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                lead_key TEXT NOT NULL,
                source TEXT NOT NULL,
                channel TEXT NOT NULL,
                transaction_id TEXT UNIQUE,
                sale_value_minor INTEGER,
                verification_status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_marketing_attr_source_time
                ON marketing_attribution_events(source, occurred_at);
            CREATE INDEX IF NOT EXISTS idx_marketing_attr_lead_time
                ON marketing_attribution_events(lead_key, occurred_at);
            CREATE INDEX IF NOT EXISTS idx_marketing_attr_type_time
                ON marketing_attribution_events(event_type, occurred_at);
            """
        )
    finally:
        conn.close()


def record_event(event: MarketingAttributionEvent, path: Path | None = None) -> dict[str, Any]:
    """Write an event once. Duplicate identical requests are successful no-ops."""
    target = Path(path or DB_PATH)
    initialize_db(target)
    payload = event.canonical_json()
    payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    received_at = datetime.now(UTC).isoformat()
    conn = _connect(target)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT payload_sha256 FROM marketing_attribution_events WHERE event_id = ?",
            (event.event_id,),
        ).fetchone()
        if existing is not None:
            conn.rollback()
            if existing["payload_sha256"] == payload_hash:
                return {"event_id": event.event_id, "duplicate": True}
            raise EventConflict("event_id already exists with different content")

        try:
            conn.execute(
                """INSERT INTO marketing_attribution_events
                (event_id, event_type, occurred_at, received_at, lead_key, source, channel,
                 transaction_id, sale_value_minor, verification_status, payload_json, payload_sha256)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.event_id,
                    event.event_type,
                    event.occurred_at.isoformat(),
                    received_at,
                    event.lead_key,
                    event.source,
                    event.channel,
                    event.transaction_id,
                    event.sale_value_minor,
                    event.verification_status,
                    payload,
                    payload_hash,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            if "transaction_id" in str(exc):
                raise EventConflict("transaction_id already belongs to a confirmed sale") from exc
            if "event_id" in str(exc):
                raise EventConflict("event_id already exists") from exc
            raise
        return {"event_id": event.event_id, "duplicate": False}
    finally:
        conn.close()


def get_summary(path: Path | None = None) -> dict[str, Any]:
    """Return counts and verified revenue grouped by attributed source."""
    target = Path(path or DB_PATH)
    initialize_db(target)
    conn = _connect(target)
    try:
        total = conn.execute(
            """SELECT COUNT(*) AS event_count,
                      COUNT(DISTINCT lead_key) AS unique_leads,
                      SUM(CASE WHEN event_type='sale_candidate' AND verification_status='pending_review' THEN 1 ELSE 0 END) AS pending_sales,
                      SUM(CASE WHEN event_type='sale_confirmed' AND verification_status='verified' THEN 1 ELSE 0 END) AS verified_sales,
                      COALESCE(SUM(CASE WHEN event_type='sale_confirmed' AND verification_status='verified' THEN sale_value_minor ELSE 0 END), 0) AS revenue_minor
               FROM marketing_attribution_events"""
        ).fetchone()
        rows = conn.execute(
            """SELECT source, COUNT(*) AS event_count, COUNT(DISTINCT lead_key) AS unique_leads,
                      SUM(CASE WHEN event_type='sale_candidate' AND verification_status='pending_review' THEN 1 ELSE 0 END) AS pending_sales,
                      SUM(CASE WHEN event_type='sale_confirmed' AND verification_status='verified' THEN 1 ELSE 0 END) AS verified_sales,
                      COALESCE(SUM(CASE WHEN event_type='sale_confirmed' AND verification_status='verified' THEN sale_value_minor ELSE 0 END), 0) AS revenue_minor
               FROM marketing_attribution_events GROUP BY source ORDER BY source"""
        ).fetchall()
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "event_count": int(total["event_count"] or 0),
            "unique_leads": int(total["unique_leads"] or 0),
            "pending_sales": int(total["pending_sales"] or 0),
            "verified_sales": int(total["verified_sales"] or 0),
            "verified_revenue_brl": round(int(total["revenue_minor"] or 0) / 100, 2),
            "by_source": [
                {
                    "source": row["source"],
                    "event_count": int(row["event_count"] or 0),
                    "unique_leads": int(row["unique_leads"] or 0),
                    "pending_sales": int(row["pending_sales"] or 0),
                    "verified_sales": int(row["verified_sales"] or 0),
                    "verified_revenue_brl": round(int(row["revenue_minor"] or 0) / 100, 2),
                }
                for row in rows
            ],
        }
    finally:
        conn.close()
