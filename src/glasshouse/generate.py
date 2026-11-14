"""Seeded synthetic AML ledger generator.

    python -m glasshouse.generate --seed 42 [--db data/glasshouse.db]

Everything is driven by one ``random.Random(seed)`` instance and fixed calendar
anchors, so the same seed always yields byte-identical table contents.

Planted patterns (ground truth is written to the non-queryable ``planted_patterns`` table):

* STRUCTURING   - 3-5 CASH credits of Rs 40,000-49,999 on one account within 7 days.
* DORMANT_REACTIVATION - an account with no transactions for 90+ days that then receives
  credits totalling >= Rs 5,00,000 within 7 days.
* HIGH_RISK_WIRE - SWIFT transactions whose counterparty_country is in the configured
  high-risk list (GLASSHOUSE_HIGH_RISK_COUNTRIES).

Background ("noise") data is generated so that it cannot accidentally match a pattern:
background CASH credits never fall in 40,000-49,999, background SWIFT never uses a
high-risk country, and dormant accounts receive no background transactions.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from glasshouse.config import DEFAULT_DB_PATH, high_risk_countries
from glasshouse.schema import DDL

N_CUSTOMERS = 1_000
N_ACCOUNTS = 1_500
N_BACKGROUND_TXNS = 50_000
N_PER_PATTERN = 12
WINDOW_START = datetime(2026, 1, 1)
WINDOW_DAYS = 180

STRUCT_MIN, STRUCT_MAX = 40_000, 49_999
DORMANT_MIN_GAP_DAYS = 90
DORMANT_MIN_CREDIT = 500_000

FIRST_NAMES = [
    "Aarav", "Vivaan", "Aditya", "Vihaan", "Arjun", "Sai", "Reyansh", "Krishna", "Ishaan", "Rohan",
    "Ananya", "Diya", "Aadhya", "Saanvi", "Priya", "Kavya", "Meera", "Isha", "Neha", "Pooja",
    "Rahul", "Amit", "Suresh", "Ramesh", "Lakshmi", "Deepak", "Sunita", "Farhan", "Zoya", "Harpreet",
]
LAST_NAMES = [
    "Sharma", "Verma", "Gupta", "Agarwal", "Iyer", "Nair", "Reddy", "Patel", "Shah", "Mehta",
    "Singh", "Kaur", "Khan", "Das", "Banerjee", "Mukherjee", "Joshi", "Kulkarni", "Rao", "Pillai",
]
CITIES = ["Mumbai", "Delhi", "Bengaluru", "Hyderabad", "Chennai", "Kolkata", "Pune",
          "Ahmedabad", "Jaipur", "Lucknow", "Indore", "Gwalior", "Surat", "Kochi"]
BRANCHES = [f"BR{n:03d}" for n in range(1, 41)]
SAFE_FOREIGN_COUNTRIES = ["AE", "US", "GB", "SG", "HK", "DE", "AU", "CA", "SA", "QA"]
NARRATIONS = {
    "CASH": ["Cash deposit at branch", "Cash withdrawal", "ATM cash"],
    "NEFT": ["NEFT salary credit", "NEFT vendor payment", "NEFT rent", "NEFT transfer"],
    "RTGS": ["RTGS property payment", "RTGS supplier settlement", "RTGS loan disbursal"],
    "IMPS": ["IMPS transfer", "IMPS refund", "IMPS family support"],
    "UPI": ["UPI grocery", "UPI p2p transfer", "UPI bill payment", "UPI fuel", "UPI dining"],
    "SWIFT": ["Inward remittance", "Outward remittance", "Import payment", "Export proceeds"],
}
# Channel mix for background traffic, with an amount range for each channel.
CHANNEL_WEIGHTS = [("UPI", 40), ("IMPS", 15), ("NEFT", 20), ("CASH", 12), ("RTGS", 5), ("SWIFT", 8)]
AMOUNT_RANGES = {
    "UPI": (50, 50_000), "IMPS": (500, 200_000), "NEFT": (1_000, 400_000),
    "CASH": (500, 150_000), "RTGS": (200_000, 900_000), "SWIFT": (10_000, 800_000),
}


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _rand_ts(rng: random.Random, day_lo: int, day_hi: int) -> datetime:
    """Random timestamp on a day in [day_lo, day_hi] (inclusive) of the window, during banking hours."""
    day = rng.randint(day_lo, day_hi)
    return WINDOW_START + timedelta(days=day, seconds=rng.randint(8 * 3600, 20 * 3600))


def _pan(rng: random.Random) -> str:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    # 4th char 'P' = individual, as on real PANs.
    return ("".join(rng.choice(letters) for _ in range(3)) + "P" + rng.choice(letters)
            + f"{rng.randint(0, 9999):04d}" + rng.choice(letters))


def _cp_account(rng: random.Random) -> str:
    return f"{rng.randint(10**11, 10**12 - 1)}"


def _background_amount(rng: random.Random, channel: str, direction: str) -> float:
    lo, hi = AMOUNT_RANGES[channel]
    while True:
        # Log-uniform: many small payments, few large ones.
        amt = round(lo * (hi / lo) ** rng.random(), 2)
        if channel == "CASH" and direction == "CR" and STRUCT_MIN <= amt < STRUCT_MAX + 1:
            continue  # keep the structuring band clean in background data
        return amt


def build_rows(seed: int) -> dict[str, list[tuple]]:
    rng = random.Random(seed)
    hr_countries = list(high_risk_countries())

    # --- customers -------------------------------------------------------
    customers = []
    pans: set[str] = set()
    for i in range(1, N_CUSTOMERS + 1):
        pan = _pan(rng)
        while pan in pans:
            pan = _pan(rng)
        pans.add(pan)
        onboarded = (WINDOW_START - timedelta(days=rng.randint(200, 2500))).date().isoformat()
        customers.append((
            f"C{i:05d}",
            f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
            pan,
            f"+91{rng.choice('6789')}{rng.randint(0, 10**9 - 1):09d}",
            rng.choice(CITIES),
            rng.choices(["LOW", "MEDIUM", "HIGH"], weights=[70, 22, 8])[0],
            rng.choices(["VERIFIED", "PENDING", "EXPIRED"], weights=[88, 7, 5])[0],
            onboarded,
        ))

    # --- accounts --------------------------------------------------------
    # Every customer gets one account; the remainder go to random customers.
    owners = [c[0] for c in customers] + [rng.choice(customers)[0] for _ in range(N_ACCOUNTS - N_CUSTOMERS)]
    onboard_by_cust = {c[0]: c[7] for c in customers}
    accounts = []
    for i, cust in enumerate(owners, start=1):
        onboarded = date.fromisoformat(onboard_by_cust[cust])
        latest_open = (WINDOW_START - timedelta(days=150)).date()
        span = max((latest_open - onboarded).days, 0)
        opened = onboarded + timedelta(days=rng.randint(0, span))
        accounts.append((
            f"A{i:05d}",
            f"5010{rng.randint(10**7, 10**8 - 1)}{i:04d}",
            cust,
            rng.choices(["SAVINGS", "CURRENT", "NRE", "NRO"], weights=[65, 25, 6, 4])[0],
            rng.choice(BRANCHES),
            opened.isoformat(),
            rng.choices(["ACTIVE", "FROZEN", "CLOSED"], weights=[96, 2, 2])[0],
        ))

    # --- choose disjoint planted accounts --------------------------------
    eligible = [a[0] for a in accounts if a[6] == "ACTIVE"]
    picked = rng.sample(eligible, 3 * N_PER_PATTERN)
    structuring_accts = sorted(picked[:N_PER_PATTERN])
    dormant_accts = sorted(picked[N_PER_PATTERN:2 * N_PER_PATTERN])
    hr_wire_accts = sorted(picked[2 * N_PER_PATTERN:])
    dormant_set = set(dormant_accts)
    background_accts = [a[0] for a in accounts if a[0] not in dormant_set]

    txns: list[list] = []  # [account_id, ts(datetime), amount, dir, channel, cp_acct, cp_country, narration]
    planted: list[tuple] = []

    # --- background transactions ----------------------------------------
    channels = [c for c, _ in CHANNEL_WEIGHTS]
    weights = [w for _, w in CHANNEL_WEIGHTS]
    for _ in range(N_BACKGROUND_TXNS):
        acct = rng.choice(background_accts)
        channel = rng.choices(channels, weights=weights)[0]
        direction = rng.choice(["CR", "DR"])
        amount = _background_amount(rng, channel, direction)
        if channel == "CASH":
            cp_acct, country = None, "IN"
        elif channel == "SWIFT":
            cp_acct, country = f"SW{_cp_account(rng)}", rng.choice(SAFE_FOREIGN_COUNTRIES)
        else:
            cp_acct, country = _cp_account(rng), "IN"
        txns.append([acct, _rand_ts(rng, 0, WINDOW_DAYS - 1), amount, direction, channel,
                     cp_acct, country, rng.choice(NARRATIONS[channel])])

    # --- pattern 1: structuring ------------------------------------------
    for acct in structuring_accts:
        n = rng.randint(3, 5)
        start_day = rng.randint(5, WINDOW_DAYS - 10)
        # All deposits fall within a 6-day span, so strictly inside a 7-day window.
        offsets = sorted(rng.sample(range(0, 6), n))
        for off in offsets:
            ts = _rand_ts(rng, start_day + off, start_day + off)
            txns.append([acct, ts, float(rng.randint(STRUCT_MIN, STRUCT_MAX)), "CR", "CASH", None, "IN",
                         "Cash deposit at branch"])
        planted.append(("STRUCTURING", acct, f"{n} cash deposits from day {start_day}"))

    # --- pattern 2: dormant reactivation ---------------------------------
    for acct in dormant_accts:
        # Light, normal activity in the first month...
        last_active_day = rng.randint(10, 30)
        for _ in range(rng.randint(3, 8)):
            ch = rng.choice(["UPI", "NEFT", "IMPS"])
            d = rng.choice(["CR", "DR"])
            txns.append([acct, _rand_ts(rng, 0, last_active_day), _background_amount(rng, ch, d), d, ch,
                         _cp_account(rng), "IN", rng.choice(NARRATIONS[ch])])
        # ...guarantee the last pre-dormancy txn sits exactly on last_active_day...
        txns.append([acct, WINDOW_START + timedelta(days=last_active_day, hours=9), 1_250.0, "DR", "UPI",
                     _cp_account(rng), "IN", "UPI bill payment"])
        # ...then silence for 100+ days, then a burst of large credits within 5 days.
        react_day = rng.randint(last_active_day + DORMANT_MIN_GAP_DAYS + 10, WINDOW_DAYS - 8)
        n = rng.randint(2, 4)
        total_target = rng.randint(DORMANT_MIN_CREDIT + 50_000, 2_500_000)
        parts = [total_target // n] * n
        parts[-1] += total_target - sum(parts)
        for k, amt in enumerate(parts):
            ch = rng.choice(["RTGS", "NEFT"])
            ts = WINDOW_START + timedelta(days=react_day + min(k, 4), hours=10 + k)
            txns.append([acct, ts, float(amt), "CR", ch, _cp_account(rng), "IN", rng.choice(NARRATIONS[ch])])
        planted.append(("DORMANT_REACTIVATION", acct,
                        f"idle days {last_active_day}->{react_day}, credits {total_target}"))

    # --- pattern 3: high-risk-country wires ------------------------------
    for acct in hr_wire_accts:
        n = rng.randint(1, 3)
        for _ in range(n):
            d = rng.choice(["CR", "DR"])
            txns.append([acct, _rand_ts(rng, 0, WINDOW_DAYS - 1), round(rng.uniform(150_000, 2_000_000), 2), d,
                         "SWIFT", f"SW{_cp_account(rng)}", rng.choice(hr_countries),
                         "Inward remittance" if d == "CR" else "Outward remittance"])
        planted.append(("HIGH_RISK_WIRE", acct, f"{n} SWIFT txns"))

    # Stable order, then sequential ids.
    txns.sort(key=lambda t: (t[1], t[0], t[2]))
    txn_rows = [(f"T{i:07d}", t[0], _ts(t[1]), t[2], t[3], t[4], t[5], t[6], t[7])
                for i, t in enumerate(txns, start=1)]

    # --- alerts ----------------------------------------------------------
    alerts = []
    rule_for = {"STRUCTURING": "AML_STRUCT_01", "DORMANT_REACTIVATION": "AML_DORM_02",
                "HIGH_RISK_WIRE": "AML_HRC_03"}
    for pattern, acct, _ in planted:
        alerts.append([acct, rule_for[pattern], _rand_ts(rng, 30, WINDOW_DAYS - 1),
                       rng.choice(["OPEN", "UNDER_REVIEW", "ESCALATED"]), round(rng.uniform(70, 99), 1)])
    for _ in range(400):  # noisy alerts from other rules
        alerts.append([rng.choice(background_accts),
                       rng.choice(["AML_VEL_04", "AML_HV_05", "AML_RND_06", "AML_PEP_07"]),
                       _rand_ts(rng, 0, WINDOW_DAYS - 1),
                       rng.choices(["OPEN", "UNDER_REVIEW", "ESCALATED", "CLOSED"], weights=[35, 20, 5, 40])[0],
                       round(rng.uniform(20, 90), 1)])
    alerts.sort(key=lambda a: (a[2], a[0], a[1]))
    alert_rows = [(f"AL{i:05d}", a[0], a[1], _ts(a[2]), a[3], a[4]) for i, a in enumerate(alerts, start=1)]

    return {
        "customers": customers,
        "accounts": accounts,
        "transactions": txn_rows,
        "alerts": alert_rows,
        "planted_patterns": sorted(planted),
    }


def generate(seed: int = 42, db_path: Path | str = DEFAULT_DB_PATH) -> Path:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    rows = build_rows(seed)
    tmp = db_path.with_suffix(db_path.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript(DDL)
        for table, data in rows.items():
            placeholders = ",".join("?" * len(data[0]))
            conn.executemany(f"INSERT INTO {table} VALUES ({placeholders})", data)
        conn.commit()
    finally:
        conn.close()
    os.replace(tmp, db_path)  # atomic swap: readers never see a half-built DB
    return db_path


def row_counts(db_path: Path | str) -> dict[str, int]:
    conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("customers", "accounts", "transactions", "alerts", "planted_patterns")}
    finally:
        conn.close()


def transactions_checksum(db_path: Path | str) -> str:
    """SHA-256 over every column of every transaction, in txn_id order."""
    conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        h = hashlib.sha256()
        for row in conn.execute("SELECT * FROM transactions ORDER BY txn_id"):
            h.update(repr(row).encode("utf-8"))
            h.update(b"\n")
        return h.hexdigest()
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Generate the synthetic AML ledger.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("GLASSHOUSE_DB", DEFAULT_DB_PATH)))
    args = parser.parse_args(argv)
    path = generate(args.seed, args.db)
    print(f"database: {path}")
    for table, n in row_counts(path).items():
        print(f"  {table:<17}{n:>7}")
    print(f"transactions sha256: {transactions_checksum(path)}")


if __name__ == "__main__":
    main()
