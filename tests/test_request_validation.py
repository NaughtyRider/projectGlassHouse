"""Every POST /ask is audited exactly once, including bodies FastAPI rejects; role is checked first.

Rules under test:
  * missing / unknown / wrong-typed role -> HTTP 403 (AskResponse body), even if other fields are bad
  * valid role but otherwise invalid body -> FastAPI's standard 422 ({"detail": [...]}) + request_id
  * exactly one audit record either way; the provider (and therefore the ledger) is never called
  * validation on other endpoints is untouched and not audited
"""

import json

import pytest
from fastapi.testclient import TestClient

from glasshouse.app import create_app
from glasshouse.provider import FakeProvider

from conftest import audit_rows


class CountingProvider(FakeProvider):
    def __init__(self):
        super().__init__({})
        self.calls = 0

    def generate(self, question, schema=None):
        self.calls += 1
        return super().generate(question)


@pytest.fixture
def ctx(app_settings):
    provider = CountingProvider()
    return TestClient(create_app(app_settings, provider)), provider, app_settings


JSON = {"content-type": "application/json"}

FORBIDDEN = [  # (id, request kwargs) -> 403
    ("no_role", dict(json={"question": "x"})),
    ("no_role_no_question", dict(json={})),
    ("null_role", dict(json={"question": "x", "role": None})),
    ("unknown_role", dict(json={"question": "x", "role": "admin"})),
    ("unknown_role_bad_question", dict(json={"question": "", "role": "admin"})),
    ("unknown_role_question_wrong_type", dict(json={"question": ["x"], "role": "root"})),
    ("wrong_case_role_missing_question", dict(json={"role": "AUDITOR"})),
    ("role_wrong_type_bad_question", dict(json={"question": 5, "role": 1})),
    ("invalid_json", dict(content=b"{not json", headers=JSON)),
    ("json_array", dict(content=b'["l1_analyst"]', headers=JSON)),
    ("empty_body", dict()),
    ("form_encoded", dict(data={"question": "x", "role": "l1_analyst"})),
]

INVALID = [  # (id, request kwargs, field) -> 422, valid role present
    ("missing_question", dict(json={"role": "l1_analyst"}), "question"),
    ("empty_question", dict(json={"role": "l2_investigator", "question": ""}), "question"),
    ("question_too_long", dict(json={"role": "auditor", "question": "x" * 2001}), "question"),
    ("question_wrong_type", dict(json={"role": "l1_analyst", "question": {"a": 1}}), "question"),
]


@pytest.mark.parametrize("kwargs", [k for _, k in FORBIDDEN], ids=[i for i, _ in FORBIDDEN])
def test_missing_or_unknown_role_is_403_and_audited(ctx, kwargs):
    client, provider, settings = ctx
    resp = client.post("/ask", **kwargs)
    assert resp.status_code == 403
    body = resp.json()
    assert set(body) == {"request_id", "status", "sql", "columns", "rows", "masked_columns", "reason"}
    assert body["status"] == "rejected" and "role" in body["reason"]
    assert body["rows"] == [] and body["sql"] is None
    assert resp.headers.get("X-Request-ID", body["request_id"]) == body["request_id"]
    rows = audit_rows(settings)
    assert len(rows) == 1
    assert rows[0][0] == body["request_id"] and rows[0][2] == "rejected"
    assert provider.calls == 0


@pytest.mark.parametrize("kwargs,field", [(k, f) for _, k, f in INVALID], ids=[i for i, _, _ in INVALID])
def test_invalid_body_with_valid_role_is_standard_422_and_audited(ctx, kwargs, field):
    client, provider, settings = ctx
    resp = client.post("/ask", **kwargs)
    assert resp.status_code == 422
    body = resp.json()
    # FastAPI's standard validation payload is preserved...
    assert isinstance(body["detail"], list) and body["detail"]
    assert any(err["loc"][-1] == field for err in body["detail"])
    # ...with the audit correlation id added.
    assert body["request_id"] == resp.headers["X-Request-ID"]
    rows = audit_rows(settings)
    assert len(rows) == 1
    request_id, role, final_status, row_count, masked, sql = rows[0]
    assert request_id == body["request_id"]
    assert role == kwargs["json"]["role"]
    assert final_status == "rejected" and row_count == 0 and sql is None
    assert provider.calls == 0


def test_invalid_request_audit_reason_and_question(ctx):
    client, _, _ = ctx
    client.post("/ask", json={"role": "l1_analyst", "question": ["not", "a", "string"]})
    rec = client.get("/audit?limit=1").json()["records"][0]
    assert rec["reason"].startswith("invalid request: question")
    assert rec["question"] == repr(["not", "a", "string"])


def test_mixed_sequence_one_record_each(ctx):
    client, _, settings = ctx
    statuses = [
        client.post("/ask", json={"question": "x", "role": "l1_analyst"}).status_code,  # 200 refused
        client.post("/ask", json={"role": "l1_analyst"}).status_code,                    # 422
        client.post("/ask", json={"question": "x"}).status_code,                         # 403
        client.post("/ask", content=b"garbage", headers=JSON).status_code,               # 403
    ]
    assert statuses == [200, 422, 403, 403]
    assert len(audit_rows(settings)) == 4


def test_other_endpoints_keep_default_validation_and_are_not_audited(ctx):
    client, _, settings = ctx
    resp = client.get("/audit?limit=0")
    assert resp.status_code == 422 and "request_id" not in resp.json()
    assert audit_rows(settings) == []


def test_valid_request_still_works(ctx):
    client, provider, settings = ctx
    resp = client.post("/ask", json={"question": "anything", "role": "auditor"})
    assert resp.status_code == 200 and resp.json()["status"] == "refused"
    assert provider.calls == 1 and len(audit_rows(settings)) == 1


def test_invalid_json_logged_without_crashing(ctx):
    client, _, _ = ctx
    client.post("/ask", content="{\"role\": \"l1_analyst\", \"question\": ".encode(), headers=JSON)
    rec = client.get("/audit?limit=1").json()["records"][0]
    assert rec["role"] is None and rec["final_status"] == "rejected"
    json.dumps(rec)  # serialisable
