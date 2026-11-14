import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from glasshouse.config import high_risk_countries
from glasshouse.generate import generate, row_counts, transactions_checksum

from conftest import planted

ROOT = Path(__file__).resolve().parents[1]


def test_same_seed_is_reproducible(tmp_path):
    a = generate(42, tmp_path / "a.db")
    b = generate(42, tmp_path / "b.db")
    assert row_counts(a) == row_counts(b)
    assert transactions_checksum(a) == transactions_checksum(b)


def test_different_seed_differs(tmp_path, db_path):
    other = generate(7, tmp_path / "other.db")
    assert transactions_checksum(other) != transactions_checksum(db_path)


def test_cli_entrypoint(tmp_path):
    out = subprocess.run(
        [sys.executable, "-m", "glasshouse.generate", "--seed", "42", "--db", str(tmp_path / "cli.db")],
        capture_output=True, text=True, check=True, cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert "transactions sha256:" in out.stdout
    assert transactions_checksum(tmp_path / "cli.db") in out.stdout


def test_minimum_volumes(db_path):
    counts = row_counts(db_path)
    assert counts["customers"] >= 1000
    assert counts["accounts"] >= 1500
    assert counts["transactions"] >= 50000
    assert counts["alerts"] > 0
    conn = sqlite3.connect(db_path)
    lo, hi = conn.execute("SELECT MIN(txn_ts), MAX(txn_ts) FROM transactions").fetchone()
    days = conn.execute("SELECT julianday(?) - julianday(?)", (hi, lo)).fetchone()[0]
    assert 170 <= days <= 180
    channels = {r[0] for r in conn.execute("SELECT DISTINCT channel FROM transactions")}
    assert channels == {"CASH", "NEFT", "RTGS", "IMPS", "UPI", "SWIFT"}


def test_at_least_ten_of_each_pattern(db_path):
    for pattern in ("STRUCTURING", "DORMANT_REACTIVATION", "HIGH_RISK_WIRE"):
        assert len(planted(db_path, pattern)) >= 10


def test_planted_structuring_matches_definition(db_path):
    conn = sqlite3.connect(db_path)
    for acct in planted(db_path, "STRUCTURING"):
        ts = [r[0] for r in conn.execute(
            "SELECT julianday(txn_ts) FROM transactions WHERE account_id=? AND channel='CASH' AND direction='CR' "
            "AND amount_inr BETWEEN 40000 AND 49999 ORDER BY txn_ts", (acct,))]
        assert any(sum(1 for t in ts if s <= t < s + 7) >= 3 for s in ts), acct


def test_planted_dormant_matches_definition(db_path):
    conn = sqlite3.connect(db_path)
    for acct in planted(db_path, "DORMANT_REACTIVATION"):
        rows = conn.execute("SELECT julianday(txn_ts), direction, amount_inr FROM transactions "
                            "WHERE account_id=? ORDER BY txn_ts", (acct,)).fetchall()
        hits = [i for i in range(1, len(rows)) if rows[i][0] - rows[i - 1][0] >= 90]
        assert hits, acct
        start = rows[hits[0]][0]
        credits = sum(r[2] for r in rows if r[1] == "CR" and start <= r[0] < start + 7)
        assert credits >= 500_000, acct


def test_planted_high_risk_wires_use_configured_countries(db_path):
    conn = sqlite3.connect(db_path)
    hr = set(high_risk_countries())
    accts = {r[0] for r in conn.execute(
        f"SELECT DISTINCT account_id FROM transactions WHERE channel='SWIFT' "
        f"AND counterparty_country IN ({','.join('?' * len(hr))})", tuple(hr))}
    assert accts == planted(db_path, "HIGH_RISK_WIRE")
