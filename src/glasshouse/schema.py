"""Database DDL and the schema description that is embedded in the LLM prompt."""

from __future__ import annotations

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

# Human-readable schema for the prompt. Kept separate from the DDL so that
# non-queryable tables (planted_patterns) are never advertised to the model.
PROMPT_SCHEMA = """\
SQLite database of a bank's AML ledger. Amounts are in INR. Timestamps are ISO-8601 text
('YYYY-MM-DD HH:MM:SS'); dates are 'YYYY-MM-DD'. Use SQLite date functions (datetime, julianday).

customers(customer_id TEXT PK, full_name TEXT, pan TEXT, phone TEXT, city TEXT,
          risk_rating TEXT in LOW|MEDIUM|HIGH, kyc_status TEXT in VERIFIED|PENDING|EXPIRED,
          onboarded_on TEXT date)
accounts(account_id TEXT PK, account_number TEXT, customer_id TEXT FK->customers,
         account_type TEXT in SAVINGS|CURRENT|NRE|NRO, branch_code TEXT, opened_on TEXT date,
         status TEXT in ACTIVE|DORMANT|FROZEN|CLOSED)
transactions(txn_id TEXT PK, account_id TEXT FK->accounts, txn_ts TEXT timestamp, amount_inr REAL,
             direction TEXT in CR|DR, channel TEXT in CASH|NEFT|RTGS|IMPS|UPI|SWIFT,
             counterparty_account TEXT, counterparty_country TEXT ISO-3166 alpha-2, narration TEXT)
alerts(alert_id TEXT PK, account_id TEXT FK->accounts, rule_code TEXT, created_at TEXT timestamp,
       status TEXT in OPEN|UNDER_REVIEW|ESCALATED|CLOSED, score REAL 0-100)
"""
