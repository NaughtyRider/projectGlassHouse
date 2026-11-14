"""Regenerate src/glasshouse/fixtures/fake_responses.json.

    python scripts/build_fixtures.py

Keys are human-readable questions; FakeProvider normalises them at load time.
"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "src" / "glasshouse" / "fixtures" / "fake_responses.json"

STRUCTURING_SQL = """\
WITH cash_deps AS (
  SELECT account_id, txn_id, txn_ts
  FROM transactions
  WHERE channel = 'CASH' AND direction = 'CR' AND amount_inr BETWEEN 40000 AND 49999.99
), windows AS (
  SELECT a.account_id, a.txn_ts AS window_start, COUNT(*) AS deposits_in_window
  FROM cash_deps AS a
  JOIN cash_deps AS b
    ON b.account_id = a.account_id
   AND b.txn_ts >= a.txn_ts
   AND b.txn_ts < datetime(a.txn_ts, '+7 days')
  GROUP BY a.account_id, a.txn_id, a.txn_ts
  HAVING COUNT(*) >= 3
)
SELECT account_id, MIN(window_start) AS first_window_start, MAX(deposits_in_window) AS max_deposits_in_7_days
FROM windows
GROUP BY account_id
ORDER BY account_id"""

DORMANT_SQL = """\
WITH ordered AS (
  SELECT account_id, txn_ts,
         LAG(txn_ts) OVER (PARTITION BY account_id ORDER BY txn_ts) AS prev_ts
  FROM transactions
), reactivations AS (
  SELECT account_id, prev_ts, txn_ts AS reactivated_at
  FROM ordered
  WHERE prev_ts IS NOT NULL AND julianday(txn_ts) - julianday(prev_ts) >= 90
)
SELECT r.account_id, r.prev_ts AS last_activity_before_gap, r.reactivated_at,
       SUM(t.amount_inr) AS credits_within_7_days
FROM reactivations AS r
JOIN transactions AS t
  ON t.account_id = r.account_id
 AND t.direction = 'CR'
 AND t.txn_ts >= r.reactivated_at
 AND t.txn_ts < datetime(r.reactivated_at, '+7 days')
GROUP BY r.account_id, r.prev_ts, r.reactivated_at
HAVING SUM(t.amount_inr) >= 500000
ORDER BY r.account_id"""

HR_WIRE_SQL = """\
SELECT account_id, COUNT(*) AS wire_count, SUM(amount_inr) AS total_inr,
       GROUP_CONCAT(DISTINCT counterparty_country) AS countries
FROM transactions
WHERE channel = 'SWIFT' AND counterparty_country IN ('IR', 'KP', 'MM', 'SY', 'YE', 'AF')
GROUP BY account_id
ORDER BY account_id"""

RESPONSES = {
    # --- the three planted patterns (README examples) --------------------
    "Which accounts have 3 or more cash deposits between ₹40,000 and ₹49,999 within 7 days?": {
        "sql": STRUCTURING_SQL, "tables_used": ["transactions"], "refusal_reason": None},
    "Which dormant accounts were reactivated with at least ₹5,00,000 in credits within 7 days?": {
        "sql": DORMANT_SQL, "tables_used": ["transactions"], "refusal_reason": None},
    "Which accounts sent or received SWIFT wires involving high-risk countries?": {
        "sql": HR_WIRE_SQL, "tables_used": ["transactions"], "refusal_reason": None},

    # --- everyday analyst questions --------------------------------------
    "Show all cash deposits on account A00746 in the last 30 days": {
        "sql": "SELECT txn_id, txn_ts, amount_inr, narration FROM transactions "
               "WHERE account_id = 'A00746' AND channel = 'CASH' AND direction = 'CR' "
               "AND txn_ts >= datetime((SELECT MAX(txn_ts) FROM transactions), '-30 days') ORDER BY txn_ts",
        "tables_used": ["transactions"], "refusal_reason": None},
    "List high risk customers with expired KYC": {
        "sql": "SELECT customer_id, full_name, city, risk_rating, kyc_status FROM customers "
               "WHERE risk_rating = 'HIGH' AND kyc_status = 'EXPIRED' ORDER BY customer_id",
        "tables_used": ["customers"], "refusal_reason": None},
    "How many open alerts are there per rule?": {
        "sql": "SELECT rule_code, COUNT(*) AS open_alerts FROM alerts WHERE status = 'OPEN' "
               "GROUP BY rule_code ORDER BY open_alerts DESC",
        "tables_used": ["alerts"], "refusal_reason": None},
    "List all transactions": {
        "sql": "SELECT txn_id, account_id, txn_ts, amount_inr, channel FROM transactions ORDER BY txn_id",
        "tables_used": ["transactions"], "refusal_reason": None},
    "List 5000 transactions": {
        "sql": "SELECT txn_id, account_id, amount_inr FROM transactions ORDER BY txn_id LIMIT 5000",
        "tables_used": ["transactions"], "refusal_reason": None},
    "Find transactions whose narration mentions drop": {
        "sql": "SELECT txn_id, narration FROM transactions WHERE narration LIKE '%drop%' LIMIT 50",
        "tables_used": ["transactions"], "refusal_reason": None},

    # --- Task 2: PII / masking demos --------------------------------------
    "Show identity details for high risk customers": {
        "sql": "SELECT customer_id, full_name, pan, phone FROM customers WHERE risk_rating = 'HIGH' "
               "ORDER BY customer_id LIMIT 5",
        "tables_used": ["customers"], "refusal_reason": None},
    "Show account numbers for accounts with open alerts": {
        "sql": "SELECT DISTINCT a.account_id, a.account_number FROM accounts AS a "
               "JOIN alerts AS al ON al.account_id = a.account_id WHERE al.status = 'OPEN' "
               "ORDER BY a.account_id LIMIT 5",
        "tables_used": ["accounts", "alerts"], "refusal_reason": None},

    # --- adversarial / failure-mode fixtures ------------------------------
    "Delete all the alerts": {
        "sql": "DELETE FROM alerts", "tables_used": ["alerts"], "refusal_reason": None},
    "Clean up the transactions table": {
        "sql": "SELECT 1; DROP TABLE transactions", "tables_used": ["transactions"], "refusal_reason": None},
    "Show me the database schema": {
        "sql": "SELECT name, sql FROM sqlite_master", "tables_used": ["sqlite_master"], "refusal_reason": None},
    "Count every pair of transactions": {
        "sql": "SELECT COUNT(*) FROM transactions AS a, transactions AS b",
        "tables_used": ["transactions"], "refusal_reason": None},
    "What is the weather in Mumbai?": {
        "sql": None, "tables_used": [],
        "refusal_reason": "The ledger has no weather data; I can only answer questions about customers, "
                          "accounts, transactions and alerts."},
    "Give me malformed output": {
        "sql": ["SELECT", "1"], "tables_used": "transactions", "refusal_reason": None},
    "Give me an empty answer": {
        "sql": None, "tables_used": [], "refusal_reason": None},
}

if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"responses": RESPONSES}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(RESPONSES)} fixtures to {OUT}")
