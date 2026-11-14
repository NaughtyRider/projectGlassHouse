"""Database DDL, the column catalog derived from it, and the schema text embedded in LLM prompts."""

from __future__ import annotations

from typing import Callable

import sqlglot
from sqlglot import exp

from glasshouse.config import ALLOWED_TABLES

DDL = """
CREATE TABLE customers (
    customer_id   TEXT PRIMARY KEY,
    full_name     TEXT NOT NULL,
    pan           TEXT NOT NULL UNIQUE,
    phone         TEXT NOT NULL,
    city          TEXT NOT NULL,
    risk_rating   TEXT NOT NULL CHECK (risk_rating IN ('LOW','MEDIUM','HIGH')),
    kyc_status    TEXT NOT NULL CHECK (kyc_status IN ('VERIFIED','PENDING','EXPIRED')),
    onboarded_on  TEXT NOT NULL
);

CREATE TABLE accounts (
    account_id     TEXT PRIMARY KEY,
    account_number TEXT NOT NULL UNIQUE,
    customer_id    TEXT NOT NULL REFERENCES customers(customer_id),
    account_type   TEXT NOT NULL CHECK (account_type IN ('SAVINGS','CURRENT','NRE','NRO')),
    branch_code    TEXT NOT NULL,
    opened_on      TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('ACTIVE','DORMANT','FROZEN','CLOSED'))
);

CREATE TABLE transactions (
    txn_id               TEXT PRIMARY KEY,
    account_id           TEXT NOT NULL REFERENCES accounts(account_id),
    txn_ts               TEXT NOT NULL,
    amount_inr           REAL NOT NULL CHECK (amount_inr > 0),
    direction            TEXT NOT NULL CHECK (direction IN ('CR','DR')),
    channel              TEXT NOT NULL CHECK (channel IN ('CASH','NEFT','RTGS','IMPS','UPI','SWIFT')),
    counterparty_account TEXT,
    counterparty_country TEXT,
    narration            TEXT
);

CREATE TABLE alerts (
    alert_id   TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    rule_code  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('OPEN','UNDER_REVIEW','ESCALATED','CLOSED')),
    score      REAL NOT NULL
);

-- Ground truth for tests/eval. Deliberately NOT in the query allow-list.
CREATE TABLE planted_patterns (
    pattern    TEXT NOT NULL,
    account_id TEXT NOT NULL,
    detail     TEXT
);

CREATE INDEX idx_txn_account_ts ON transactions(account_id, txn_ts);
CREATE INDEX idx_txn_channel ON transactions(channel, direction);
CREATE INDEX idx_accounts_customer ON accounts(customer_id);
CREATE INDEX idx_alerts_account ON alerts(account_id);
"""


def _catalog_from_ddl(ddl: str) -> dict[str, dict[str, str]]:
    """{table: {column: type}} for allow-listed tables, in DDL column order, parsed with sqlglot."""
    catalog: dict[str, dict[str, str]] = {}
    for stmt in sqlglot.parse(ddl, read="sqlite"):
        if not isinstance(stmt, exp.Create) or stmt.kind != "TABLE":
            continue
        table = stmt.this.this.name.lower()
        if table not in ALLOWED_TABLES:
            continue
        catalog[table] = {
            col.name.lower(): (col.args["kind"].sql("sqlite") if col.args.get("kind") else "TEXT")
            for col in stmt.this.expressions
            if isinstance(col, exp.ColumnDef)
        }
    return catalog


# The single source of truth for which (table, column) pairs exist and are queryable.
CATALOG: dict[str, dict[str, str]] = _catalog_from_ddl(DDL)

# Extra hints for the model. Keyed by "table.column"; a hint is dropped with its column.
COLUMN_NOTES: dict[str, str] = {
    "customers.customer_id": "PK",
    "customers.risk_rating": "LOW|MEDIUM|HIGH",
    "customers.kyc_status": "VERIFIED|PENDING|EXPIRED",
    "customers.onboarded_on": "date",
    "accounts.account_id": "PK",
    "accounts.customer_id": "FK->customers",
    "accounts.account_type": "SAVINGS|CURRENT|NRE|NRO",
    "accounts.opened_on": "date",
    "accounts.status": "ACTIVE|DORMANT|FROZEN|CLOSED",
    "transactions.txn_id": "PK",
    "transactions.account_id": "FK->accounts",
    "transactions.txn_ts": "timestamp",
    "transactions.direction": "CR|DR",
    "transactions.channel": "CASH|NEFT|RTGS|IMPS|UPI|SWIFT",
    "transactions.counterparty_country": "ISO-3166 alpha-2",
    "alerts.alert_id": "PK",
    "alerts.account_id": "FK->accounts",
    "alerts.created_at": "timestamp",
    "alerts.status": "OPEN|UNDER_REVIEW|ESCALATED|CLOSED",
    "alerts.score": "0-100",
}

SCHEMA_PREAMBLE = """\
SQLite database of a bank's AML ledger. Amounts are in INR. Timestamps are ISO-8601 text
('YYYY-MM-DD HH:MM:SS'); dates are 'YYYY-MM-DD'. Use SQLite date functions (datetime, julianday).
"""

# Returns "allow" | "mask" | "deny" for (table, column), or None to show everything.
ColumnFilter = Callable[[str, str], str]


def render_schema(column_action: ColumnFilter | None = None) -> str:
    """Schema text for the prompt. Denied columns (and fully denied tables) are omitted entirely:
    neither their names nor their hints reach the model."""
    def action(table: str, col: str) -> str:
        return column_action(table, col) if column_action else "allow"

    visible_tables = {t for t, cols in CATALOG.items() if any(action(t, c) != "deny" for c in cols)}
    lines = [SCHEMA_PREAMBLE.rstrip("\n"), ""]
    for table, columns in CATALOG.items():
        parts = []
        for col, ctype in columns.items():
            if action(table, col) == "deny":
                continue
            note = COLUMN_NOTES.get(f"{table}.{col}")
            if note and note.startswith("FK->") and note[4:] not in visible_tables:
                note = None  # don't advertise tables the role cannot see
            text = f"{col} {ctype}" + (f" {note}" if note else "")
            if action(table, col) == "mask":
                text += " [masked in output]"
            parts.append(text)
        if parts:
            lines.append(f"{table}({', '.join(parts)})")
    return "\n".join(lines) + "\n"


# Unfiltered schema (all queryable columns). Role-specific prompts use render_schema(policy).
PROMPT_SCHEMA = render_schema()
