"""Guarded query execution against a read-only SQLite connection.

Defence in depth (the validator is the primary gate; these hold even if it were bypassed):
  * the connection is opened with ``mode=ro`` and ``PRAGMA query_only=ON``;
  * a SQLite authorizer permits only SELECT/READ/FUNCTION/recursive actions and only reads
    of allow-listed tables;
  * the row cap is applied to the parsed AST, and fetchmany() bounds the result again;
  * a progress handler aborts the statement once the wall-clock deadline passes.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from sqlglot import exp

from glasshouse.validator import DIALECT


class QueryTimeout(Exception):
    pass


class ExecutionError(Exception):
    pass


@dataclass(frozen=True)
class QueryResult:
    sql: str
    columns: list[str]
    rows: list[list[Any]]
    elapsed_ms: float


def apply_row_cap(tree: exp.Expression, default_limit: int = 200, max_limit: int = 1000) -> exp.Expression:
    """Return a copy of ``tree`` whose outermost LIMIT is present and <= max_limit.

    * no LIMIT              -> LIMIT default_limit
    * integer LIMIT > max   -> LIMIT max_limit
    * non-literal LIMIT     -> LIMIT max_limit (we cannot prove it is bounded)
    OFFSET is preserved. For set operations the LIMIT applies to the combined result.
    """
    tree = tree.copy()
    limit_node = tree.args.get("limit")
    if limit_node is None:
        new_limit = default_limit
    else:
        value = limit_node.expression
        if isinstance(value, exp.Literal) and not value.is_string and value.is_int:
            current = int(value.this)
            new_limit = current if 0 <= current <= max_limit else max_limit
        else:
            new_limit = max_limit
    tree.set("limit", exp.Limit(expression=exp.Literal.number(new_limit)))
    return tree


# Authorizer action codes that a pure read query needs.
_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
_RECURSIVE = getattr(sqlite3, "SQLITE_RECURSIVE", 33)
_ALLOWED_ACTIONS.add(_RECURSIVE)


def _make_authorizer(allowed_tables: frozenset[str]):
    def authorizer(action: int, arg1: str | None, arg2: str | None, db_name: str | None, trigger: str | None) -> int:
        if action not in _ALLOWED_ACTIONS:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_READ:
            # arg1 = table name. Reads with no table (e.g. CTE / subquery columns) come through as None.
            if arg1 is not None and arg1.lower() not in allowed_tables:
                return sqlite3.SQLITE_DENY
            if db_name not in (None, "main"):
                return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION and arg2 and arg2.lower() in {"load_extension", "readfile", "writefile"}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return authorizer


def open_readonly(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.exists():
        raise ExecutionError(f"database not found at {path}; run `python -m glasshouse.generate --seed 42`")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
    conn.execute("PRAGMA query_only = ON")
    return conn


def execute_query(
    tree: exp.Expression,
    db_path: Path | str,
    allowed_tables: Iterable[str],
    timeout_seconds: float = 2.0,
    default_limit: int = 200,
    max_limit: int = 1000,
) -> QueryResult:
    capped = apply_row_cap(tree, default_limit, max_limit)
    sql = capped.sql(dialect=DIALECT)
    allowed = frozenset(t.lower() for t in allowed_tables)

    conn = open_readonly(db_path)
    try:
        conn.set_authorizer(_make_authorizer(allowed))
        start = time.monotonic()
        deadline = start + timeout_seconds
        timed_out = False

        def progress() -> int:
            nonlocal timed_out
            if time.monotonic() > deadline:
                timed_out = True
                return 1  # non-zero aborts the running statement
            return 0

        conn.set_progress_handler(progress, 10_000)  # called every N VM instructions
        try:
            cur = conn.execute(sql)
            columns = [d[0] for d in (cur.description or [])]
            rows = [list(r) for r in cur.fetchmany(max_limit)]
        except sqlite3.Error as e:
            if timed_out:
                raise QueryTimeout(f"query exceeded the {timeout_seconds:g}s time limit and was aborted") from None
            raise ExecutionError(f"query failed: {e}") from None
        elapsed_ms = (time.monotonic() - start) * 1000
        return QueryResult(sql=sql, columns=columns, rows=rows, elapsed_ms=round(elapsed_ms, 1))
    finally:
        conn.close()
