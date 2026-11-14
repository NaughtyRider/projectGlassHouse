"""End-to-end masking / access bypass tests through POST /ask (Task 2.7 A, C, E).

Every bypass test checks the *data*, not just the status: it loads every real PAN, phone
and account number from the ledger and asserts none of them appears inside any returned cell.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from glasshouse.app import create_app
from glasshouse.masking import FULL_MASK
from glasshouse.provider import FakeProvider

from conftest import audit_rows, make_client


@pytest.fixture(scope="module")
def pii(db_path) -> dict[str, set[str]]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            "pan": {r[0] for r in conn.execute("SELECT pan FROM customers")},
            "phone": {r[0] for r in conn.execute("SELECT phone FROM customers")},
            "account_number": {r[0] for r in conn.execute("SELECT account_number FROM accounts")},
        }
    finally:
        conn.close()


def assert_no_pii(body, pii, kinds=("pan", "phone", "account_number")):
    secrets = set().union(*(pii[k] for k in kinds))
    for row in body["rows"]:
        for cell in row:
            if cell is None:
                continue
            text = str(cell)
            assert not any(s in text for s in secrets), f"PII leaked in cell {text!r}"


def post(client, sql, role):
    resp = client.post("/ask", json={"question": "q", "role": role})
    return resp


def run(app_settings, sql, role):
    client = make_client(app_settings, {"q": {"sql": sql, "tables_used": [], "refusal_reason": None}})
    resp = client.post("/ask", json={"question": "q", "role": role})
    assert resp.status_code == 200, resp.text
    return resp.json()


# (name, sql, expected outcome for l2_investigator). For l1_analyst every one is rejected.
BYPASS_CASES = [
    ("alias", "SELECT pan AS x FROM customers", "partial"),
    ("substr", "SELECT substr(pan, 1, 10) FROM customers", "full"),
    ("upper", "SELECT upper(pan) FROM customers", "full"),
    ("concat", "SELECT 'id:' || pan FROM customers", "full"),
    ("tail_shift", "SELECT 'XXXX' || substr(pan, 1, 4) AS t FROM customers", "full"),
    ("subquery", "SELECT s.v FROM (SELECT pan AS v FROM customers) AS s", "partial"),
    ("subquery_transformed", "SELECT s.v FROM (SELECT lower(phone) AS v FROM customers) AS s", "full"),
    ("cte", "WITH c AS (SELECT phone AS v FROM customers) SELECT v AS contact FROM c", "partial"),
    ("cte_chain", "WITH a AS (SELECT pan AS p FROM customers), b AS (SELECT replace(p, 'A', '') AS q FROM a) "
                  "SELECT q FROM b", "full"),
    ("union_second_branch", "SELECT city FROM customers UNION SELECT pan FROM customers", "partial"),
    ("union_all_transformed", "SELECT customer_id FROM customers UNION ALL SELECT 'p' || phone FROM customers",
     "full"),
    ("scalar_subquery", "SELECT a.account_id, (SELECT c.pan FROM customers c WHERE c.customer_id = a.customer_id) "
                        "AS p FROM accounts a", "full"),
    ("group_concat", "SELECT city, GROUP_CONCAT(pan, ',') AS ps FROM customers GROUP BY city", "full"),
    ("case_when", "SELECT CASE WHEN 1 = 1 THEN pan END AS p FROM customers", "full"),
    ("table_alias", "SELECT zz.pan FROM customers AS zz", "partial"),
]


@pytest.mark.parametrize("sql,mode", [(s, m) for _, s, m in BYPASS_CASES], ids=[n for n, _, _ in BYPASS_CASES])
def test_bypass_masked_for_l2(app_settings, pii, sql, mode):
    body = run(app_settings, sql, "l2_investigator")
    assert body["status"] == "ok", body["reason"]
    assert body["rows"], "test needs data to be meaningful"
    assert body["masked_columns"], "lineage should have flagged a masked column"
    assert_no_pii(body, pii)
    masked_idx = [body["columns"].index(c) for c in body["masked_columns"]]
    for row in body["rows"]:
        for i in masked_idx:
            if row[i] is None:
                continue
            if mode == "full":
                assert row[i] == FULL_MASK
            else:
                assert str(row[i]).startswith("X") and len(str(row[i])) >= 5


@pytest.mark.parametrize("sql", [s for _, s, _ in BYPASS_CASES], ids=[n for n, _, _ in BYPASS_CASES])
def test_bypass_rejected_for_l1(app_settings, sql):
    body = run(app_settings, sql, "l1_analyst")
    assert body["status"] == "rejected"
    assert body["rows"] == []
    assert any(c in body["reason"] for c in ("customers.pan", "customers.phone")), body["reason"]


DENIED_ELSEWHERE = [
    ("select_star", "SELECT * FROM customers", "SELECT *"),
    ("where", "SELECT customer_id FROM customers WHERE full_name LIKE 'A%'", "customers.full_name"),
    ("order_by", "SELECT customer_id FROM customers ORDER BY phone", "customers.phone"),
    ("join", "SELECT a.account_id FROM accounts a JOIN customers c ON c.customer_id = a.customer_id "
             "AND c.pan LIKE 'A%'", "customers.pan"),
    ("alias", "SELECT q.full_name AS who FROM customers AS q", "customers.full_name"),
    ("having", "SELECT city FROM customers GROUP BY city HAVING COUNT(DISTINCT pan) > 1", "customers.pan"),
]


@pytest.mark.parametrize("sql,needle", [(s, n) for _, s, n in DENIED_ELSEWHERE], ids=[n for n, _, _ in DENIED_ELSEWHERE])
def test_denied_column_anywhere_rejected_l1(app_settings, pii, sql, needle):
    body = run(app_settings, sql, "l1_analyst")
    assert body["status"] == "rejected"
    assert needle in body["reason"]
    assert body["rows"] == []


def test_partial_mask_keeps_last_four_digits(app_settings, db_path):
    # PAN format AAAAA9999A: the 4-digit serial is shown, the check letter is hidden (ABCDE1234F -> XXXXXX1234).
    body = run(app_settings, "SELECT customer_id, pan FROM customers ORDER BY customer_id LIMIT 3", "l2_investigator")
    conn = sqlite3.connect(db_path)
    real = dict(conn.execute("SELECT customer_id, pan FROM customers ORDER BY customer_id LIMIT 3").fetchall())
    for cid, masked in body["rows"]:
        assert masked == "XXXXXX" + real[cid][5:9]
        assert len(masked) == len(real[cid])
    assert body["masked_columns"] == ["pan"]


def test_masked_column_usable_in_predicate(app_settings, db_path):
    conn = sqlite3.connect(db_path)
    cid, pan = conn.execute("SELECT customer_id, pan FROM customers ORDER BY customer_id LIMIT 1").fetchone()
    body = run(app_settings, f"SELECT customer_id FROM customers WHERE pan = '{pan}'", "l2_investigator")
    assert body["status"] == "ok" and body["rows"] == [[cid]] and body["masked_columns"] == []


def test_column_names_preserved_when_masked(app_settings):
    body = run(app_settings, "SELECT customer_id, upper(pan) AS identifier FROM customers LIMIT 2", "l2_investigator")
    assert body["columns"] == ["customer_id", "identifier"]
    assert body["masked_columns"] == ["identifier"]


# --- C: account number across roles -------------------------------------------------

ACCT_SQL = "SELECT account_id, account_number FROM accounts ORDER BY account_id LIMIT 20"


@pytest.mark.parametrize("role", ["l1_analyst", "l2_investigator"])
def test_account_number_masked(app_settings, pii, role):
    body = run(app_settings, ACCT_SQL, role)
    assert body["status"] == "ok"
    assert body["masked_columns"] == ["account_number"]
    assert_no_pii(body, pii, kinds=("account_number",))
    assert all(r[1].startswith("XXXX") for r in body["rows"])


def test_account_number_rejected_for_auditor(app_settings):
    body = run(app_settings, ACCT_SQL, "auditor")
    assert body["status"] == "rejected" and "accounts.account_number" in body["reason"]


@pytest.mark.parametrize("sql", [
    "SELECT customer_id, city FROM customers",
    "SELECT COUNT(*) FROM customers",
    "SELECT a.account_id FROM accounts a JOIN customers c USING (customer_id)",
    "SELECT txn_id, narration FROM transactions",
    "SELECT counterparty_account FROM transactions",
])
def test_auditor_customer_and_free_text_rejected(app_settings, sql):
    assert run(app_settings, sql, "auditor")["status"] == "rejected"


def test_auditor_can_review_flows(app_settings):
    body = run(app_settings, "SELECT channel, SUM(amount_inr) FROM transactions GROUP BY channel", "auditor")
    assert body["status"] == "ok" and len(body["rows"]) == 6


# --- E: role validation ---------------------------------------------------------------

class CountingProvider(FakeProvider):
    def __init__(self):
        super().__init__({"q": {"sql": "SELECT customer_id, city FROM customers"}})
        self.calls = 0

    def generate(self, question, schema=None):
        self.calls += 1
        return super().generate(question) if schema is None else super().generate(question, schema)


@pytest.mark.parametrize("payload", [
    {"question": "q"},                                  # missing role
    {"question": "q", "role": None},
    {"question": "q", "role": ""},
    {"question": "q", "role": "admin"},                 # unknown
    {"question": "q", "role": "L2_INVESTIGATOR"},       # wrong case
    {"question": "q", "role": 7},                       # wrong type
], ids=["missing", "null", "empty", "unknown", "case", "type"])
def test_missing_or_unknown_role_is_403(app_settings, payload):
    provider = CountingProvider()
    client = TestClient(create_app(app_settings, provider))
    resp = client.post("/ask", json=payload)
    assert resp.status_code == 403
    body = resp.json()
    assert body["status"] == "rejected"
    assert "role" in body["reason"]
    assert body["rows"] == [] and body["columns"] == [] and body["sql"] is None
    assert provider.calls == 0, "provider (and therefore the ledger) must not be reached"
    rows = audit_rows(app_settings)
    assert len(rows) == 1 and rows[0][2] == "rejected"


def test_valid_role_reaches_ledger(app_settings):
    provider = CountingProvider()
    client = TestClient(create_app(app_settings, provider))
    resp = client.post("/ask", json={"question": "q", "role": "l1_analyst"})
    assert resp.status_code == 200 and resp.json()["status"] == "ok" and provider.calls == 1
