import time

import pytest

from glasshouse.provider import FakeProvider, build_prompt, normalize_question
from glasshouse.schema import PROMPT_SCHEMA

from conftest import make_client, planted, table_counts

RESPONSE_KEYS = {"request_id", "status", "sql", "columns", "rows", "masked_columns", "reason"}

README_EXAMPLES = {
    "STRUCTURING": "Which accounts have 3 or more cash deposits between ₹40,000 and ₹49,999 within 7 days?",
    "DORMANT_REACTIVATION": "Which dormant accounts were reactivated with at least ₹5,00,000 in credits within 7 days?",
    "HIGH_RISK_WIRE": "Which accounts sent or received SWIFT wires involving high-risk countries?",
}


def ask(client, question, role="analyst"):
    resp = client.post("/ask", json={"question": question, "role": role})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == RESPONSE_KEYS
    return body


@pytest.mark.parametrize("pattern", sorted(README_EXAMPLES))
def test_readme_examples_return_planted_accounts(client, db_path, pattern):
    body = ask(client, README_EXAMPLES[pattern])
    assert body["status"] == "ok", body["reason"]
    assert body["columns"][0] == "account_id"
    assert {row[0] for row in body["rows"]} == planted(db_path, pattern)
    assert body["masked_columns"] == []


def test_question_normalisation(client):
    body = ask(client, "  which ACCOUNTS sent or received swift wires involving   high-risk countries  ")
    assert body["status"] == "ok"


def test_unknown_question_is_refused(client):
    body = ask(client, "What's the capital of France?")
    assert body["status"] == "refused"
    assert body["sql"] is None and body["rows"] == []


def test_provider_refusal(client):
    body = ask(client, "What is the weather in Mumbai?")
    assert body["status"] == "refused" and "weather" in body["reason"]


@pytest.mark.parametrize("question", ["Delete all the alerts", "Clean up the transactions table",
                                      "Show me the database schema"])
def test_unsafe_fixture_sql_rejected(client, db_path, question):
    before = table_counts(db_path)
    body = ask(client, question)
    assert body["status"] == "rejected"
    assert body["rows"] == []
    assert table_counts(db_path) == before


@pytest.mark.parametrize("sql", [
    "DROP TABLE transactions",
    "DELETE FROM customers",
    "UPDATE accounts SET status = 'CLOSED'",
    "SELECT 1; DELETE FROM alerts",
    "WITH d AS (DELETE FROM alerts RETURNING *) SELECT * FROM d",
    "/**/dElEtE/**/FROM transactions",
])
def test_configured_destructive_statement_is_rejected_and_db_unchanged(settings, db_path, sql):
    client = make_client(settings, {"wipe it": {"sql": sql, "tables_used": [], "refusal_reason": None}})
    before = table_counts(db_path)
    body = ask(client, "wipe it")
    assert body["status"] == "rejected", body
    assert body["reason"]
    assert table_counts(db_path) == before


@pytest.mark.parametrize("payload", [
    {"sql": ["SELECT", "1"], "tables_used": [], "refusal_reason": None},  # wrong type
    {"sql": None, "tables_used": [], "refusal_reason": None},  # neither sql nor refusal
    {"sql": "SELECT 1", "tables_used": "customers", "refusal_reason": None},  # wrong type
    {"sql": "SELECT 1", "unexpected": True},  # unknown field
    "not even json {",  # raw garbage string
    42,
])
def test_malformed_provider_output_is_error_not_500(settings, payload):
    client = make_client(settings, {"bad": payload})
    body = ask(client, "bad")
    assert body["status"] == "error"
    assert "malformed" in body["reason"]


def test_timeout_via_endpoint(client):
    start = time.monotonic()
    body = ask(client, "Count every pair of transactions")
    elapsed = time.monotonic() - start
    assert body["status"] == "error"
    assert "timeout" in body["reason"]
    assert elapsed < 3.0


def test_row_cap_via_endpoint(client):
    assert len(ask(client, "List all transactions")["rows"]) == 200
    body = ask(client, "List 5000 transactions")
    assert len(body["rows"]) == 1000 and body["sql"].endswith("LIMIT 1000")


def test_ok_response_shape(client):
    body = ask(client, "Show all cash deposits on account A00746 in the last 30 days")
    assert body["status"] == "ok"
    assert body["columns"] == ["txn_id", "txn_ts", "amount_inr", "narration"]
    assert body["rows"] and all(len(r) == 4 for r in body["rows"])
    assert len(body["request_id"]) == 32


def test_request_ids_unique(client):
    ids = {ask(client, "List high risk customers with expired KYC")["request_id"] for _ in range(5)}
    assert len(ids) == 5


def test_role_accepted_but_not_enforced_yet(client):
    a = ask(client, "How many open alerts are there per rule?", role="analyst")
    b = ask(client, "How many open alerts are there per rule?", role="intern")
    assert a["status"] == b["status"] == "ok" and a["rows"] == b["rows"]


def test_execution_failure_is_error(settings):
    client = make_client(settings, {"bad col": {"sql": "SELECT no_such_column FROM customers"}})
    body = ask(client, "bad col")
    assert body["status"] == "error" and "query failed" in body["reason"]


def test_provider_exception_is_error_not_500(settings):
    from glasshouse.app import create_app
    from fastapi.testclient import TestClient

    class Boom(FakeProvider):
        def generate(self, question):
            raise RuntimeError("kaboom")

    client = TestClient(create_app(settings, Boom({})))
    body = ask(client, "anything")
    assert body["status"] == "error"


def test_invalid_request_is_422(client):
    assert client.post("/ask", json={"question": ""}).status_code == 422


def test_prompt_includes_schema():
    prompt = build_prompt("how many customers?")
    assert PROMPT_SCHEMA in prompt
    for table in ("customers", "accounts", "transactions", "alerts"):
        assert table in prompt
    assert "planted_patterns" not in prompt


def test_fake_provider_builds_prompt():
    p = FakeProvider({})
    p.generate("hello")
    assert p.last_prompt and PROMPT_SCHEMA in p.last_prompt


def test_normalize_question():
    assert normalize_question("  Hello   WORLD?? ") == "hello world"
