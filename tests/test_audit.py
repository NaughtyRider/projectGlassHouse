"""Audit trail (Task 2.5, 2.7 D)."""

import dataclasses
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from glasshouse.app import create_app
from glasshouse.audit import AuditLog, AuditRecord
from glasshouse.executor import open_readonly
from glasshouse.provider import FakeProvider

from conftest import audit_rows, make_client, table_counts

FIXTURES = {
    "ok": {"sql": "SELECT account_id, account_number FROM accounts LIMIT 3"},
    "bad sql": {"sql": "DROP TABLE alerts"},
    "policy": {"sql": "SELECT full_name FROM customers"},
    "refuse": {"refusal_reason": "not about the ledger"},
    "malformed": {"sql": 123},
    "boom": {"sql": "SELECT abs(-9223372036854775808) AS x FROM alerts"},
    "slow": {"sql": "SELECT COUNT(*) FROM transactions a, transactions b"},
    "audit table": {"sql": "SELECT request_id FROM audit_log"},
    "attach audit": {"sql": "ATTACH DATABASE 'data/audit.db' AS a"},
}


@pytest.fixture
def client(app_settings):
    return make_client(app_settings, FIXTURES)


def ask(client, question, role="l1_analyst"):
    return client.post("/ask", json={"question": question, "role": role})


@pytest.mark.parametrize("question,status,reason_part", [
    ("ok", "ok", None),
    ("bad sql", "rejected", "only SELECT"),
    ("policy", "rejected", "customers.full_name"),
    ("refuse", "refused", "not about the ledger"),
    ("malformed", "error", "malformed"),
    ("boom", "error", "query failed"),
    ("slow", "error", "timeout"),
    ("not in fixtures", "refused", None),
])
def test_exactly_one_audit_record_per_outcome(client, app_settings, question, status, reason_part):
    body = ask(client, question).json()
    assert body["status"] == status
    rows = audit_rows(app_settings)
    assert len(rows) == 1
    request_id, role, final_status, row_count, masked, generated_sql = rows[0]
    assert request_id == body["request_id"]
    assert role == "l1_analyst" and final_status == status
    assert row_count == len(body["rows"])
    assert json.loads(masked) == body["masked_columns"]
    if reason_part:
        assert reason_part in body["reason"]


def test_audit_captures_sql_rowcount_and_masking(client, app_settings):
    body = ask(client, "ok").json()
    _, _, _, row_count, masked, generated_sql = audit_rows(app_settings)[0]
    assert row_count == 3 and json.loads(masked) == ["account_number"]
    assert generated_sql == FIXTURES["ok"]["sql"]
    assert body["sql"].endswith("LIMIT 3")


def test_one_record_per_request_over_many_requests(client, app_settings):
    questions = ["ok", "bad sql", "policy", "refuse", "malformed", "boom", "ok"]
    ids = [ask(client, q).json()["request_id"] for q in questions]
    client.post("/ask", json={"question": "ok", "role": "nobody"})  # 403 is audited too
    rows = audit_rows(app_settings)
    assert len(rows) == len(questions) + 1
    assert [r[0] for r in rows[:-1]] == ids
    assert rows[-1][1] == "nobody" and rows[-1][2] == "rejected"


def test_get_audit_returns_recent_first(client):
    for q in ("ok", "bad sql", "refuse"):
        ask(client, q)
    data = client.get("/audit?limit=2").json()
    assert data["count"] == 2
    assert [r["final_status"] for r in data["records"]] == ["refused", "rejected"]
    rec = data["records"][0]
    assert set(rec) == {"request_id", "timestamp", "role", "question", "generated_sql", "final_status",
                        "reason", "row_count", "masked_columns"}
    assert client.get("/audit").json()["count"] == 3  # default limit=50


@pytest.mark.parametrize("limit", ["0", "-1", "501", "abc", "1.5"])
def test_get_audit_validates_limit(client, limit):
    assert client.get(f"/audit?limit={limit}").status_code == 422


def test_get_audit_missing_db_is_empty(client, app_settings):
    assert not app_settings.audit_db_path.exists()
    assert client.get("/audit").json() == {"count": 0, "records": []}


def test_get_audit_tolerates_malformed_rows(client, app_settings):
    ask(client, "ok")
    conn = sqlite3.connect(app_settings.audit_db_path)
    with conn:
        conn.execute("INSERT INTO audit_log (request_id, timestamp, final_status, row_count, masked_columns) "
                     "VALUES ('x', 't', 'ok', 'not-a-number', '{not json')")
    conn.close()
    recs = client.get("/audit").json()["records"]
    assert recs[0]["request_id"] == "x" and recs[0]["masked_columns"] == [] and recs[0]["row_count"] == 0


def test_corrupt_audit_db(app_settings):
    app_settings.audit_db_path.write_bytes(b"this is not a sqlite database" * 100)
    client = make_client(app_settings, FIXTURES)
    assert client.get("/audit").status_code == 503
    resp = ask(client, "ok")  # fail closed: data is not served without an audit record
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error" and body["rows"] == [] and "audit" in body["reason"]


def test_audit_is_append_only(client, app_settings):
    ask(client, "ok")
    conn = sqlite3.connect(app_settings.audit_db_path)
    for stmt in ("UPDATE audit_log SET final_status = 'ok'", "DELETE FROM audit_log"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute(stmt)
    conn.close()
    assert len(audit_rows(app_settings)) == 1


def test_ask_cannot_read_audit_table(client, app_settings, db_path):
    for q in ("audit table", "attach audit"):
        body = ask(client, q, role="l2_investigator").json()
        assert body["status"] == "rejected" and body["rows"] == []
    # The ledger connection simply has no such table.
    conn = open_readonly(db_path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    conn.close()
    assert "audit_log" not in names


def test_audit_db_is_separate_file(app_settings, db_path):
    assert app_settings.audit_db_path.resolve() != db_path.resolve()
    before = table_counts(db_path)
    client = make_client(app_settings, FIXTURES)
    ask(client, "ok")
    assert table_counts(db_path) == before  # auditing never writes to the ledger
    assert app_settings.audit_db_path.exists()


def test_app_refuses_audit_db_equal_to_ledger(settings):
    same = dataclasses.replace(settings, audit_db_path=settings.db_path)
    with pytest.raises(ValueError, match="different file"):
        create_app(same, FakeProvider())


def test_audit_record_roundtrip(tmp_path):
    log = AuditLog(tmp_path / "a.db")
    log.record(AuditRecord("r1", "auditor", "q", None, "refused", "nope", 0, []))
    (rec,) = log.recent(10)
    assert rec["request_id"] == "r1" and rec["role"] == "auditor" and rec["timestamp"]


def test_unexpected_exception_still_audited_once(app_settings):
    class Boom(FakeProvider):
        def generate(self, question, schema=None):
            raise RuntimeError("kaboom")

    client = TestClient(create_app(app_settings, Boom({})))
    body = client.post("/ask", json={"question": "x", "role": "auditor"}).json()
    assert body["status"] == "error"
    assert len(audit_rows(app_settings)) == 1
