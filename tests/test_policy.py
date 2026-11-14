"""Access policy loading/validation and role-filtered prompt schemas (Task 2.1, 2.4)."""

import re

import pytest
import yaml

from glasshouse.config import DEFAULT_POLICY_PATH
from glasshouse.policy import AccessPolicy, PolicyError, UnknownRoleError
from glasshouse.provider import FakeProvider
from glasshouse.schema import CATALOG

from conftest import make_client


@pytest.fixture(scope="module")
def policy() -> AccessPolicy:
    return AccessPolicy.load(DEFAULT_POLICY_PATH)


def has_word(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text) is not None


# --- loading -------------------------------------------------------------------

def test_exactly_three_roles(policy):
    assert set(policy.roles) == {"l1_analyst", "l2_investigator", "auditor"}


@pytest.mark.parametrize("role,table,column,expected", [
    ("l1_analyst", "customers", "full_name", "deny"),
    ("l1_analyst", "customers", "pan", "deny"),
    ("l1_analyst", "customers", "phone", "deny"),
    ("l1_analyst", "accounts", "account_number", "mask"),
    ("l1_analyst", "customers", "city", "allow"),             # unlisted -> default allow
    ("l1_analyst", "transactions", "narration", "allow"),     # unlisted -> default allow
    ("l2_investigator", "customers", "full_name", "allow"),
    ("l2_investigator", "customers", "pan", "mask"),
    ("l2_investigator", "customers", "phone", "mask"),
    ("l2_investigator", "accounts", "account_number", "mask"),
    ("l2_investigator", "transactions", "counterparty_account", "allow"),
    ("auditor", "accounts", "account_number", "deny"),
    ("auditor", "transactions", "counterparty_account", "deny"),
    ("auditor", "transactions", "narration", "deny"),
    ("auditor", "transactions", "amount_inr", "allow"),
    ("auditor", "alerts", "score", "allow"),
])
def test_permission_matrix(policy, role, table, column, expected):
    assert policy.get_role(role).action_for(table, column) == expected


def test_auditor_denied_every_customers_column(policy):
    auditor = policy.get_role("auditor")
    assert all(auditor.action_for("customers", c) == "deny" for c in CATALOG["customers"])
    assert auditor.is_table_denied("customers")


def _load(tmp_path, data) -> AccessPolicy:
    p = tmp_path / "policy.yaml"
    p.write_text(yaml.safe_dump(data) if not isinstance(data, str) else data, encoding="utf-8")
    return AccessPolicy.load(p)


@pytest.mark.parametrize("bad", [
    {"roles": {"r": {"columns": {"customers.nope": "deny"}}}},          # unknown column
    {"roles": {"r": {"columns": {"nope.pan": "deny"}}}},                # unknown table
    {"roles": {"r": {"columns": {"customers.pan": "hide"}}}},           # unknown action
    {"roles": {"r": {"columns": {"pan": "deny"}}}},                     # not table.column
    {"roles": {"r": {"tables": {"customers": "mask"}}}},                # table-level must be deny
    {"roles": {"r": {"colums": {}}}},                                   # typo'd key
    {"roles": {}},                                                      # no roles
    {"default_column_action": "maybe", "roles": {"r": {}}},             # bad default
    {"roles": {"r": {}}, "extra": 1},                                   # unexpected top-level key
    "roles: [unclosed",                                                 # invalid YAML
])
def test_invalid_policy_rejected_at_load(tmp_path, bad):
    with pytest.raises(PolicyError):
        _load(tmp_path, bad)


def test_missing_policy_file(tmp_path):
    with pytest.raises(PolicyError):
        AccessPolicy.load(tmp_path / "absent.yaml")


@pytest.mark.parametrize("role", [None, "", "   ", "admin", "L1_ANALYST", " l1_analyst", 123, ["auditor"]])
def test_unknown_role_never_defaulted(policy, role):
    with pytest.raises(UnknownRoleError):
        policy.get_role(role)


# --- role-filtered prompt schema --------------------------------------------------

def test_l1_schema_hides_denied_columns(policy):
    schema = policy.get_role("l1_analyst").prompt_schema()
    for col in ("full_name", "pan", "phone"):
        assert not has_word(schema, col), col
    assert has_word(schema, "account_number") and "[masked in output]" in schema
    assert has_word(schema, "customers")  # other customer columns remain visible


def test_l2_schema_shows_full_name(policy):
    schema = policy.get_role("l2_investigator").prompt_schema()
    assert has_word(schema, "full_name")
    assert has_word(schema, "pan") and has_word(schema, "phone")  # masked, not denied


def test_auditor_schema_excludes_customers(policy):
    schema = policy.get_role("auditor").prompt_schema()
    for col in CATALOG["customers"]:
        if col != "customer_id":  # also an accounts column (FK); checked separately below
            assert not has_word(schema, col), col
    assert not has_word(schema, "customers")  # no customers table and no FK hint pointing to it
    assert "customers(" not in schema
    for col in ("account_number", "counterparty_account", "narration"):
        assert not has_word(schema, col)


@pytest.mark.parametrize("role,hidden,visible", [
    ("l1_analyst", ["full_name", "pan", "phone"], ["account_number"]),
    ("l2_investigator", [], ["full_name", "pan", "phone"]),
    ("auditor", ["full_name", "pan", "phone", "narration", "account_number", "counterparty_account"], ["amount_inr"]),
])
def test_prompt_actually_sent_to_provider_is_filtered(app_settings, role, hidden, visible):
    provider = FakeProvider()
    from fastapi.testclient import TestClient
    from glasshouse.app import create_app

    client = TestClient(create_app(app_settings, provider))
    client.post("/ask", json={"question": "How many open alerts are there per rule?", "role": role})
    prompt = provider.last_prompt
    assert prompt
    for col in hidden:
        assert not has_word(prompt, col), (role, col)
    for col in visible:
        assert has_word(prompt, col), (role, col)


def test_full_schema_unchanged_in_database(db_path):
    import sqlite3
    conn = sqlite3.connect(db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(customers)")}
    assert {"full_name", "pan", "phone"} <= cols
