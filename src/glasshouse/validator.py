"""AST-based SQL safety validator.

The validator parses SQL with sqlglot (SQLite dialect) and accepts exactly one read-only
query: SELECT, optionally with CTEs and set operations (UNION / INTERSECT / EXCEPT).

Checks, in order:
  1. The text parses. Unparseable SQL is rejected (that's how ``SEL/**/ECT`` style tricks fail).
  2. Exactly one statement.
  3. The root node is a query (Select or a set operation).
  4. No node anywhere in the tree is a write/DDL/session statement. This catches DML nested
     in CTEs, e.g. ``WITH x AS (DELETE ... RETURNING *) SELECT ...``.
  5. Every table reference is a plain name in the allow-list (or a CTE defined in the query),
     with no schema/catalog qualifier. This blocks sqlite_master, sqlite_schema, temp.*, main.*,
     attached databases and table-valued functions like pragma_table_info().
  6. No denied functions (load_extension, readfile, writefile, sqlite_* introspection, ...).

Because checks run on the parsed tree, keyword case and comments make no difference, and
keywords inside string literals (``narration LIKE '%drop%'``) cannot cause false rejections.
Execution adds its own layers on top: a read-only connection and a SQLite authorizer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

DIALECT = "sqlite"

# Statement / clause node types that must never appear anywhere in an accepted tree.
_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = tuple(
    t for t in (
        getattr(exp, name, None)
        for name in (
            "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "AlterColumn",
            "RenameTable", "TruncateTable", "Command", "Pragma", "Attach", "Detach", "Transaction",
            "Commit", "Rollback", "Set", "Use", "Into", "Analyze", "Copy", "LoadData", "Grant",
            "Revoke", "Kill", "Cache", "Uncache", "Refresh", "Describe", "Show", "Lock",
        )
    )
    if isinstance(t, type)
)

# Functions that read/write files, load code or reveal internals. Compared lower-case.
_DENIED_FUNCTIONS = frozenset({
    "load_extension", "readfile", "writefile", "edit", "fts3_tokenizer", "zipfile",
    "sqlar_compress", "sqlar_uncompress", "randomblob", "zeroblob",
})
_DENIED_FUNCTION_PREFIXES = ("sqlite_", "pragma_")


class ValidationError(Exception):
    """Raised internally; surfaced to callers as ValidationResult(ok=False)."""


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str | None = None
    ast: exp.Expression | None = None
    tables: frozenset[str] = frozenset()


def _function_names(node: exp.Func) -> set[str]:
    """Names a function node may be known by.

    sqlglot canonicalises some functions (e.g. ``sqlite_version()`` becomes ``CurrentVersion``),
    so we check both the parser's name and the name as rendered for SQLite, which is what
    actually executes.
    """
    names = {str(node.this).lower()} if isinstance(node, exp.Anonymous) else {node.sql_name().lower()}
    rendered = node.sql(dialect=DIALECT)
    if "(" in rendered:
        names.add(rendered.split("(", 1)[0].strip().strip('"`[]').lower())
    return names


def _cte_names(tree: exp.Expression) -> set[str]:
    return {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}


def _check_tables(tree: exp.Expression, allowed: frozenset[str]) -> frozenset[str]:
    ctes = _cte_names(tree)
    used: set[str] = set()
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            # Table-valued function (pragma_table_info(...), generate_series(...), json_each(...)).
            raise ValidationError(f"table-valued functions are not allowed: {table.this.sql(DIALECT)}")
        if table.args.get("db") is not None or table.args.get("catalog") is not None:
            raise ValidationError(f"schema-qualified table references are not allowed: {table.sql(DIALECT)}")
        name = table.name.lower()
        if name in ctes:
            continue
        if name not in allowed:
            raise ValidationError(f"table not in allow-list: {table.name}")
        used.add(name)
    return frozenset(used)


def _check_functions(tree: exp.Expression) -> None:
    for fn in tree.find_all(exp.Func):
        for name in _function_names(fn):
            if name in _DENIED_FUNCTIONS or name.startswith(_DENIED_FUNCTION_PREFIXES):
                raise ValidationError(f"function not allowed: {name}")


def _parse_single(sql: str) -> exp.Expression:
    if not isinstance(sql, str) or not sql.strip():
        raise ValidationError("empty SQL")
    try:
        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except SqlglotError as e:
        raise ValidationError(f"SQL could not be parsed: {str(e).splitlines()[0]}") from None
    if len(statements) != 1:
        raise ValidationError(f"exactly one statement is allowed, got {len(statements)}")
    return statements[0]


def _walk(tree: exp.Expression) -> Iterable[exp.Expression]:
    # exp.Expression.walk() yields nodes in BFS order; normalise across sqlglot versions.
    for node in tree.walk():
        yield node[0] if isinstance(node, tuple) else node


def validate_sql(sql: str, allowed_tables: Iterable[str]) -> ValidationResult:
    allowed = frozenset(t.lower() for t in allowed_tables)
    try:
        tree = _parse_single(sql)
        if not isinstance(tree, (exp.Select, exp.SetOperation)):
            raise ValidationError(f"only SELECT queries are allowed, got {type(tree).__name__.upper()}")
        for node in _walk(tree):
            if isinstance(node, _FORBIDDEN_NODES):
                raise ValidationError(f"forbidden statement inside query: {type(node).__name__.upper()}")
        tables = _check_tables(tree, allowed)
        _check_functions(tree)
    except ValidationError as e:
        return ValidationResult(ok=False, reason=str(e))
    return ValidationResult(ok=True, ast=tree, tables=tables)
