"""AST-level column enforcement and lineage (Task 2.2, 2.3), without the HTTP layer."""

import pytest
import sqlglot

from glasshouse.access import check_access
from glasshouse.config import ALLOWED_TABLES, DEFAULT_POLICY_PATH
from glasshouse.executor import ExecutionError, execute_query
from glasshouse.policy import AccessPolicy
from glasshouse.validator import validate_sql

POLICY = AccessPolicy.load(DEFAULT_POLICY_PATH)
L1, L2, AUD = (POLICY.get_role(r) for r in ("l1_analyst", "l2_investigator", "auditor"))


def decide(sql, role):
    v = validate_sql(sql, ALLOWED_TABLES)
    assert v.ok, v.reason
    return check_access(v.ast, role)


# Denied column in every clause / construct -> rejected for l1 (full_name/pan/phone denied).
DENIED_PLACEMENTS = [
    ("select", "SELECT full_name FROM customers"),
    ("select_alias", "SELECT full_name AS n FROM customers"),
    ("where", "SELECT customer_id FROM customers WHERE full_name LIKE 'A%'"),
    ("join_on", "SELECT a.account_id FROM accounts a JOIN customers c ON c.customer_id = a.customer_id AND c.pan <> ''"),
    ("group_by", "SELECT city, COUNT(*) FROM customers GROUP BY city, phone"),
    ("having", "SELECT city FROM customers GROUP BY city HAVING MAX(phone) > '0'"),
    ("order_by", "SELECT customer_id FROM customers ORDER BY full_name"),
    ("where_subquery", "SELECT account_id FROM accounts WHERE customer_id IN "
                       "(SELECT customer_id FROM customers WHERE pan LIKE 'A%')"),
    ("exists_correlated", "SELECT a.account_id FROM accounts a WHERE EXISTS "
                          "(SELECT 1 FROM customers c WHERE c.customer_id = a.customer_id AND c.phone LIKE '+919%')"),
    ("scalar_subquery", "SELECT a.account_id, (SELECT c.full_name FROM customers c "
                        "WHERE c.customer_id = a.customer_id) AS who FROM accounts a"),
    ("derived_table", "SELECT t.n FROM (SELECT full_name AS n FROM customers) AS t"),
    ("derived_unused", "SELECT t.customer_id FROM (SELECT customer_id, pan FROM customers) AS t"),
    ("cte", "WITH x AS (SELECT customer_id, pan FROM customers) SELECT customer_id FROM x"),
    ("union_second_branch", "SELECT city FROM customers UNION SELECT full_name FROM customers"),
    ("table_alias", "SELECT zz.phone FROM customers AS zz"),
    ("unqualified_in_join", "SELECT account_id FROM accounts JOIN customers USING (customer_id) WHERE pan = 'X'"),
    ("window_partition", "SELECT customer_id, ROW_NUMBER() OVER (PARTITION BY pan) FROM customers"),
    ("case_expr", "SELECT CASE WHEN pan LIKE 'A%' THEN 1 ELSE 0 END AS f FROM customers"),
    ("function_arg", "SELECT length(full_name) FROM customers"),
    ("quoted_identifier", 'SELECT "PAN" FROM customers'),
]


@pytest.mark.parametrize("sql", [s for _, s in DENIED_PLACEMENTS], ids=[n for n, _ in DENIED_PLACEMENTS])
def test_denied_column_rejected_anywhere(sql):
    d = decide(sql, L1)
    assert not d.ok
    assert any(col in d.reason for col in ("customers.full_name", "customers.pan", "customers.phone")), d.reason


@pytest.mark.parametrize("sql", [
    "SELECT * FROM transactions",
    "SELECT t.* FROM transactions t",
    "SELECT txn_id FROM transactions WHERE account_id IN (SELECT * FROM accounts)",
])
def test_star_projection_rejected(sql):
    d = decide(sql, L2)
    assert not d.ok and "SELECT *" in d.reason


def test_count_star_still_allowed():
    assert decide("SELECT COUNT(*) FROM transactions", L1).ok


def test_auditor_blocked_from_customers_even_without_columns():
    d = decide("SELECT COUNT(*) FROM customers", AUD)
    assert not d.ok and "customers" in d.reason


@pytest.mark.parametrize("sql", [
    "SELECT account_number FROM accounts",
    "SELECT txn_id FROM transactions WHERE narration LIKE '%x%'",
    "SELECT t.account_id FROM transactions t ORDER BY t.counterparty_account",
])
def test_auditor_denied_columns(sql):
    assert not decide(sql, AUD).ok


@pytest.mark.parametrize("sql,plan", [
    ("SELECT pan FROM customers", ["partial"]),
    ("SELECT pan AS customer_identifier FROM customers", ["partial"]),
    ("SELECT substr(pan, 1, 10) AS identifier FROM customers", ["full"]),
    ("SELECT upper(pan) AS identifier FROM customers", ["full"]),
    ("SELECT 'PAN:' || pan AS identifier FROM customers", ["full"]),
    ("SELECT 'XXXX' || substr(pan, 1, 4) AS sneaky FROM customers", ["full"]),
    ("SELECT customer_id, full_name, pan FROM customers", [None, None, "partial"]),
    ("SELECT t.p FROM (SELECT pan AS p FROM customers) t", ["partial"]),
    ("SELECT t.p FROM (SELECT lower(pan) AS p FROM customers) t", ["full"]),
    ("WITH a AS (SELECT phone AS q FROM customers) SELECT q AS z FROM a", ["partial"]),
    ("WITH a AS (SELECT phone AS q FROM customers), b AS (SELECT q || '' AS r FROM a) SELECT r FROM b", ["full"]),
    ("SELECT city FROM customers UNION SELECT pan FROM customers", ["partial"]),
    ("SELECT customer_id FROM customers UNION ALL SELECT upper(pan) FROM customers", ["full"]),
    ("SELECT pan FROM customers UNION SELECT city FROM customers UNION SELECT phone FROM customers", ["partial"]),
    ("SELECT a.account_id, (SELECT c.pan FROM customers c WHERE c.customer_id = a.customer_id) AS s "
     "FROM accounts a", [None, "full"]),
    ("SELECT city, GROUP_CONCAT(pan) FROM customers GROUP BY city", [None, "full"]),
    ("SELECT customer_id FROM customers WHERE pan LIKE 'A%' ORDER BY phone", [None]),  # predicates OK, not output
    ("SELECT a.account_number FROM accounts a JOIN customers c ON c.customer_id = a.customer_id", ["partial"]),
])
def test_lineage_mask_plan_l2(sql, plan):
    d = decide(sql, L2)
    assert d.ok, d.reason
    assert d.mask_plan == plan


def test_account_number_masked_for_l1_lineage():
    assert decide("SELECT account_id, 'acct ' || account_number FROM accounts", L1).mask_plan == [None, "full"]


def test_task1_pattern_queries_unaffected():
    import json
    from glasshouse.config import DEFAULT_FIXTURE_PATH
    fixtures = json.loads(DEFAULT_FIXTURE_PATH.read_text(encoding="utf-8"))["responses"]
    for q in [k for k in fixtures if k.startswith("Which")]:
        for role in (L1, L2, AUD):
            d = decide(fixtures[q]["sql"], role)
            assert d.ok and all(m is None for m in d.mask_plan), (q, role.name, d.reason)


# --- engine-level second layer --------------------------------------------------

def test_authorizer_blocks_denied_column_even_if_ast_check_bypassed(db_path):
    tree = sqlglot.parse_one("SELECT pan FROM customers", read="sqlite")  # skip check_access on purpose
    with pytest.raises(ExecutionError):
        execute_query(tree, db_path, ALLOWED_TABLES, denied_columns=L1.denied_columns())


def test_authorizer_blocks_denied_column_in_where(db_path):
    tree = sqlglot.parse_one("SELECT customer_id FROM customers WHERE full_name = 'x'", read="sqlite")
    with pytest.raises(ExecutionError):
        execute_query(tree, db_path, ALLOWED_TABLES, denied_columns=L1.denied_columns())
