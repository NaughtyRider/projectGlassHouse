import pytest

from glasshouse.config import ALLOWED_TABLES
from glasshouse.validator import validate_sql

UNSAFE = [
    ("drop_table", "DROP TABLE customers"),
    ("delete", "DELETE FROM transactions"),
    ("update", "UPDATE accounts SET status = 'CLOSED'"),
    ("insert", "INSERT INTO alerts VALUES ('X', 'A00001', 'R', '2026-01-01', 'OPEN', 1)"),
    ("replace_into", "REPLACE INTO alerts VALUES ('X', 'A00001', 'R', '2026-01-01', 'OPEN', 1)"),
    ("create_table_as", "CREATE TABLE loot AS SELECT * FROM customers"),
    ("alter_table", "ALTER TABLE customers ADD COLUMN x TEXT"),
    ("stacked_drop", "SELECT 1; DROP TABLE customers"),
    ("stacked_after_comment", "SELECT * FROM customers; -- harmless\nDELETE FROM alerts"),
    ("attach", "ATTACH DATABASE '/tmp/evil.db' AS evil"),
    ("detach", "DETACH DATABASE main"),
    ("pragma", "PRAGMA table_info(customers)"),
    ("pragma_writable_schema", "PRAGMA writable_schema = 1"),
    ("vacuum_into", "VACUUM INTO '/tmp/copy.db'"),
    ("begin", "BEGIN TRANSACTION"),
    ("sqlite_master", "SELECT name, sql FROM sqlite_master"),
    ("sqlite_schema_qualified", "SELECT * FROM main.sqlite_schema"),
    ("sqlite_temp_master", "SELECT * FROM sqlite_temp_master"),
    ("catalog_in_subquery", "SELECT * FROM customers WHERE full_name IN (SELECT name FROM sqlite_master)"),
    ("catalog_in_union", "SELECT customer_id, full_name FROM customers UNION SELECT name, sql FROM sqlite_master"),
    ("non_allowlisted_table", "SELECT * FROM planted_patterns"),
    ("attached_schema", "SELECT * FROM evil.customers"),
    ("pragma_table_function", "SELECT * FROM pragma_table_info('customers')"),
    ("pragma_function_in_where", "SELECT 1 FROM customers WHERE EXISTS (SELECT 1 FROM pragma_database_list)"),
    ("load_extension", "SELECT load_extension('/tmp/evil.so')"),
    ("sqlite_function", "SELECT sqlite_version()"),
    ("randomblob_cost_bomb", "SELECT randomblob(1000000000)"),
    ("insert_in_cte", "WITH x AS (INSERT INTO alerts VALUES ('X','A','R','d','OPEN',1) RETURNING *) SELECT * FROM x"),
    ("delete_in_cte", "WITH d AS (DELETE FROM alerts RETURNING *) SELECT * FROM d"),
    ("select_into", "SELECT * INTO loot FROM customers"),
    ("mixed_case_drop", "dRoP tAbLe CuStOmErS"),
    ("mixed_case_delete", "DeLeTe FrOm alerts WHERE 1=1"),
    ("comment_obfuscated_delete", "/* report */ DELETE /* only old */ FROM alerts"),
    ("comment_split_keyword", "SEL/**/ECT * FROM customers; DR/**/OP TABLE customers"),
    ("comment_hidden_stack", "SELECT 1 /* ; */ ; /**/ DROP /**/ TABLE customers"),
    ("empty", ""),
    ("whitespace_only", "   ;  "),
    ("garbage", "this is not sql"),
]

SAFE = [
    ("simple", "SELECT customer_id, full_name FROM customers WHERE city = 'Mumbai'"),
    ("join_agg", "SELECT a.account_id, SUM(t.amount_inr) FROM accounts a JOIN transactions t "
                 "ON t.account_id = a.account_id GROUP BY a.account_id"),
    ("cte", "WITH big AS (SELECT * FROM transactions WHERE amount_inr > 100000) SELECT COUNT(*) FROM big"),
    ("union", "SELECT account_id FROM alerts UNION SELECT account_id FROM transactions WHERE channel = 'SWIFT'"),
    ("narration_like_drop", "SELECT txn_id, narration FROM transactions WHERE narration LIKE '%drop%'"),
    ("literal_with_sql", "SELECT * FROM transactions WHERE narration = 'x''; DELETE FROM alerts; --'"),
    ("column_named_like_keyword", "SELECT status AS \"delete\", COUNT(*) FROM alerts GROUP BY status"),
    ("window_fn", "SELECT account_id, txn_ts, LAG(txn_ts) OVER (PARTITION BY account_id ORDER BY txn_ts) "
                  "FROM transactions"),
    ("date_fns", "SELECT * FROM transactions WHERE txn_ts >= datetime('2026-06-01', '-30 days')"),
    ("trailing_semicolon", "SELECT COUNT(*) FROM customers;"),
    ("lower_case", "select * from alerts where status = 'OPEN' limit 10"),
    ("comment_in_select", "SELECT /* just reading */ COUNT(*) FROM accounts -- trailing comment"),
]


@pytest.mark.parametrize("sql", [s for _, s in UNSAFE], ids=[n for n, _ in UNSAFE])
def test_unsafe_sql_rejected(sql):
    result = validate_sql(sql, ALLOWED_TABLES)
    assert not result.ok
    assert result.reason


@pytest.mark.parametrize("sql", [s for _, s in SAFE], ids=[n for n, _ in SAFE])
def test_safe_sql_accepted(sql):
    result = validate_sql(sql, ALLOWED_TABLES)
    assert result.ok, result.reason
    assert result.ast is not None


def test_case_is_irrelevant_for_allowlist():
    assert validate_sql("SELECT * FROM CUSTOMERS", ALLOWED_TABLES).ok


def test_reports_tables_used():
    r = validate_sql("WITH x AS (SELECT * FROM accounts) SELECT * FROM x JOIN customers USING (customer_id)",
                     ALLOWED_TABLES)
    assert r.ok and r.tables == {"accounts", "customers"}


def test_cte_cannot_shadow_into_forbidden_table():
    # A CTE named like an allowed table is fine; reading sqlite_master inside it is not.
    assert not validate_sql("WITH customers AS (SELECT * FROM sqlite_master) SELECT * FROM customers",
                            ALLOWED_TABLES).ok


def test_enough_cases():
    assert len(UNSAFE) >= 12 and len(SAFE) >= 5
