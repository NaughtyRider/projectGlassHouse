import sqlite3
import time

import pytest
import sqlglot

from glasshouse.config import ALLOWED_TABLES
from glasshouse.executor import ExecutionError, QueryTimeout, apply_row_cap, execute_query, open_readonly
from glasshouse.validator import validate_sql


def run(sql, db_path, **kw):
    v = validate_sql(sql, ALLOWED_TABLES)
    assert v.ok, v.reason
    return execute_query(v.ast, db_path, ALLOWED_TABLES, **kw)


@pytest.mark.parametrize("sql,expected", [
    ("SELECT a FROM t", "SELECT a FROM t LIMIT 200"),
    ("SELECT a FROM t LIMIT 50", "SELECT a FROM t LIMIT 50"),
    ("SELECT a FROM t LIMIT 1000", "SELECT a FROM t LIMIT 1000"),
    ("SELECT a FROM t LIMIT 5000", "SELECT a FROM t LIMIT 1000"),
    ("SELECT a FROM t LIMIT 5000 OFFSET 10", "SELECT a FROM t LIMIT 1000 OFFSET 10"),
    ("SELECT a FROM t LIMIT -1", "SELECT a FROM t LIMIT 1000"),
    ("SELECT a FROM t LIMIT (SELECT 99999)", "SELECT a FROM t LIMIT 1000"),
    ("SELECT a FROM t UNION SELECT b FROM u", "SELECT a FROM t UNION SELECT b FROM u LIMIT 200"),
    ("WITH x AS (SELECT a FROM t LIMIT 9999) SELECT a FROM x",
     "WITH x AS (SELECT a FROM t LIMIT 9999) SELECT a FROM x LIMIT 200"),
])
def test_row_cap_rewrites_ast(sql, expected):
    assert apply_row_cap(sqlglot.parse_one(sql, read="sqlite")).sql("sqlite") == expected


def test_row_cap_injects_default_200(db_path):
    r = run("SELECT txn_id FROM transactions", db_path)
    assert len(r.rows) == 200
    assert r.sql.endswith("LIMIT 200")


def test_row_cap_clamps_to_1000(db_path):
    r = run("SELECT txn_id FROM transactions LIMIT 5000", db_path)
    assert len(r.rows) == 1000
    assert r.sql.endswith("LIMIT 1000")


def test_small_limit_respected(db_path):
    assert len(run("SELECT txn_id FROM transactions LIMIT 7", db_path).rows) == 7


def test_returns_columns(db_path):
    r = run("SELECT customer_id, city FROM customers LIMIT 1", db_path)
    assert r.columns == ["customer_id", "city"]


def test_timeout_aborts_expensive_query(db_path):
    start = time.monotonic()
    with pytest.raises(QueryTimeout):
        run("SELECT COUNT(*) FROM transactions a, transactions b", db_path, timeout_seconds=2.0)
    assert time.monotonic() - start < 3.0


def test_expensive_query_really_is_slow_without_guard(db_path):
    """Shows the timeout test is meaningful: unguarded, the query is still running after 2.5 s."""
    conn = sqlite3.connect(db_path)
    deadline = time.monotonic() + 2.5
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
    with pytest.raises(sqlite3.OperationalError):  # interrupted by our 2.5 s probe, i.e. not finished
        conn.execute("SELECT COUNT(*) FROM transactions a, transactions b").fetchone()
    conn.close()


def test_connection_is_read_only(db_path):
    conn = open_readonly(db_path)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM alerts")
    conn.close()


def test_authorizer_blocks_catalog_even_if_validator_bypassed(db_path):
    tree = sqlglot.parse_one("SELECT name FROM sqlite_master", read="sqlite")  # skip validate_sql on purpose
    with pytest.raises(ExecutionError):
        execute_query(tree, db_path, ALLOWED_TABLES)


def test_authorizer_blocks_non_allowlisted_table(db_path):
    tree = sqlglot.parse_one("SELECT * FROM planted_patterns", read="sqlite")
    with pytest.raises(ExecutionError):
        execute_query(tree, db_path, ALLOWED_TABLES)


def test_missing_database(tmp_path):
    with pytest.raises(ExecutionError, match="database not found"):
        run("SELECT 1 FROM customers", tmp_path / "nope.db")
