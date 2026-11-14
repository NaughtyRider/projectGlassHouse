"""Append-only audit log in its own SQLite database.

Separation from the ledger:
  * a different file (``GLASSHOUSE_AUDIT_DB``); the app refuses to start if it is the ledger file;
  * the ledger query connection is read-only, opened on the ledger file only, and ATTACH is
    blocked by both the validator and the SQLite authorizer, so /ask can never reach it;
  * ``audit_log`` is not in the query allow-list.

Append-only: the table has triggers that abort any UPDATE or DELETE, and this module only
ever INSERTs or SELECTs.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS audit_log (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id     TEXT NOT NULL UNIQUE,
    timestamp      TEXT NOT NULL,
    role           TEXT,
    question       TEXT,
    generated_sql  TEXT,
    final_status   TEXT NOT NULL,
    reason         TEXT,
    row_count      INTEGER NOT NULL DEFAULT 0,
    masked_columns TEXT NOT NULL DEFAULT '[]'
);
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""

MAX_TEXT = 10_000  # cap stored question / SQL length


class AuditError(Exception):
    pass


@dataclass(frozen=True)
class AuditRecord:
    request_id: str
    role: str | None
    question: str | None
    generated_sql: str | None
    final_status: str
    reason: str | None
    row_count: int
    masked_columns: list[str]
    timestamp: str = ""


def _clip(value: Any) -> str | None:
    if value is None:
        return None
    s = value if isinstance(value, str) else repr(value)
    return s[:MAX_TEXT]


class AuditLog:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5)
        conn.executescript(AUDIT_DDL)
        return conn

    def record(self, rec: AuditRecord) -> None:
        """Insert exactly one row. Raises AuditError on any failure (caller decides policy)."""
        ts = rec.timestamp or datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        try:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO audit_log (request_id, timestamp, role, question, generated_sql, final_status,"
                        " reason, row_count, masked_columns) VALUES (?,?,?,?,?,?,?,?,?)",
                        (rec.request_id, ts, _clip(rec.role), _clip(rec.question), _clip(rec.generated_sql),
                         rec.final_status, _clip(rec.reason), int(rec.row_count), json.dumps(list(rec.masked_columns))),
                    )
            finally:
                conn.close()
        except (sqlite3.Error, OSError, TypeError, ValueError) as e:
            raise AuditError(f"could not write audit record: {e}") from None

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Most recent records first. A missing audit DB is an empty log; a corrupt one raises AuditError."""
        if not self.path.exists():
            return []
        try:
            conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
            try:
                has_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_log'").fetchone()
                if not has_table:
                    return []
                rows = conn.execute(
                    "SELECT request_id, timestamp, role, question, generated_sql, final_status, reason,"
                    " row_count, masked_columns FROM audit_log ORDER BY seq DESC LIMIT ?", (int(limit),)
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as e:
            raise AuditError(f"audit log unreadable: {e}") from None
        return [_row_to_dict(r) for r in rows]


def _row_to_dict(r: tuple) -> dict[str, Any]:
    try:
        masked = json.loads(r[8]) if r[8] else []
        if not isinstance(masked, list):
            masked = []
    except (ValueError, TypeError):
        masked = []
    try:
        row_count = int(r[7])
    except (ValueError, TypeError):
        row_count = 0
    return {
        "request_id": r[0], "timestamp": r[1], "role": r[2], "question": r[3], "generated_sql": r[4],
        "final_status": r[5], "reason": r[6], "row_count": row_count,
        "masked_columns": [str(m) for m in masked],
    }
