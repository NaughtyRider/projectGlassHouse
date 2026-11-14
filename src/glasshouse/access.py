"""Column-level access enforcement and output-column lineage on the sqlglot AST.

Pipeline (all on a *copy* of the validated AST; the original is what gets executed):

1. Reject star projections (``SELECT *``, ``t.*``). We cannot guarantee a stable column set
   for them, so the safest rule is to require explicit columns. ``COUNT(*)`` is unaffected.
2. Reject any reference to a table the role is fully denied (catches ``COUNT(*) FROM customers``).
3. ``qualify()`` the tree against the catalog: every column becomes ``<source_alias>.<column>``,
   ``USING`` joins are expanded, ORDER BY aliases are resolved, and unknown or ambiguous
   columns raise. Anything we cannot resolve is rejected, never ignored.
4. Walk every scope (root, CTEs, derived tables, subqueries, UNION branches). Each column is
   resolved through the scope's sources, walking up to parent scopes for correlated
   references, to either a base table or a derived scope. A base-table reference to a denied
   column rejects the whole query, whichever clause it appears in.
5. Compute lineage for each *output position* of the root query: the set of base columns that
   flow into it, following aliases, CTEs, derived tables, scalar subqueries and every UNION
   branch. If any of them is masked, the output position is masked:
     * ``partial`` (last 4 characters visible) only when every masked contribution is a
       direct passthrough of the column (alias / CTE / subquery hops only);
     * ``full`` otherwise, because a transformed value can move hidden characters into the
       visible tail (e.g. ``'XXXX' || substr(pan, 1, 4)``).

Positions, not names, connect lineage to the result set, so renaming an output column
cannot dodge masking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope

from glasshouse.policy import RolePolicy
from glasshouse.schema import CATALOG
from glasshouse.validator import DIALECT

MaskMode = Literal["partial", "full"]


class AccessDenied(Exception):
    pass


@dataclass(frozen=True)
class Lineage:
    sources: frozenset[tuple[str, str]] = frozenset()
    direct: bool = False  # output is a bare column passthrough (no transformation)

    def merge(self, other: "Lineage") -> "Lineage":
        return Lineage(self.sources | other.sources, self.direct and other.direct)


@dataclass(frozen=True)
class AccessDecision:
    ok: bool
    reason: str | None = None
    # One entry per output column of the root query: None (clear), "partial" or "full".
    mask_plan: list[MaskMode | None] = field(default_factory=list)
    referenced: frozenset[tuple[str, str]] = frozenset()


def _qualify_schema() -> dict[str, dict[str, str]]:
    return {t: dict(cols) for t, cols in CATALOG.items()}


def _reject_star_projections(tree: exp.Expression) -> None:
    for select in tree.find_all(exp.Select):
        for proj in select.expressions:
            inner = proj.unalias()
            if isinstance(inner, exp.Star) or (isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star)):
                raise AccessDenied("SELECT * is not allowed; list the columns you need explicitly")


def _reject_denied_tables(tree: exp.Expression, role: RolePolicy) -> None:
    for table in tree.find_all(exp.Table):
        if role.is_table_denied(table.name):
            raise AccessDenied(f"role {role.name!r} may not access table {table.name.lower()!r}")


def _resolve_source(column: exp.Column, scope: Scope) -> exp.Table | Scope:
    """Find what ``column.table`` refers to, walking outwards for correlated references."""
    current: Scope | None = scope
    while current is not None:
        source = current.sources.get(column.table)
        if source is not None:
            return source
        current = current.parent
    raise AccessDenied(f"could not resolve column reference {column.sql(DIALECT)}")


def _check_scope_columns(scope: Scope, role: RolePolicy, referenced: set[tuple[str, str]]) -> None:
    for column in scope.columns:
        if not column.table:
            # After qualify, only references to a projection alias (e.g. ORDER BY n) stay
            # unqualified; their underlying columns are checked via the projection itself.
            aliases = scope.expression.named_selects if isinstance(scope.expression, exp.Select) else []
            if column.name in aliases:
                continue
            raise AccessDenied(f"could not resolve column reference {column.sql(DIALECT)}")
        source = _resolve_source(column, scope)
        if isinstance(source, exp.Table):
            key = (source.name.lower(), column.name.lower())
            referenced.add(key)
            if role.action_for(*key) == "deny":
                raise AccessDenied(f"role {role.name!r} may not access column {key[0]}.{key[1]}")


class _LineageResolver:
    def __init__(self) -> None:
        self._cache: dict[int, list[Lineage]] = {}

    def outputs(self, scope: Scope) -> list[Lineage]:
        key = id(scope)
        if key not in self._cache:
            self._cache[key] = self._compute(scope)
        return self._cache[key]

    def _compute(self, scope: Scope) -> list[Lineage]:
        node = scope.expression
        if isinstance(node, exp.SetOperation):
            branches = [self.outputs(s) for s in scope.set_operation_scopes]
            if not branches or any(len(b) != len(branches[0]) for b in branches):
                raise AccessDenied("UNION branches have different column counts")
            merged = branches[0]
            for branch in branches[1:]:
                merged = [a.merge(b) for a, b in zip(merged, branch)]
            return merged
        if not isinstance(node, exp.Select):
            raise AccessDenied(f"unsupported query shape for lineage: {type(node).__name__}")
        return [self._projection(proj, scope) for proj in node.expressions]

    def _projection(self, proj: exp.Expression, scope: Scope) -> Lineage:
        expr = proj.unalias()
        sources: set[tuple[str, str]] = set()
        direct = isinstance(expr, exp.Column)

        # Scalar subqueries inside this projection: everything they output flows here.
        for sub in scope.subquery_scopes:
            if _is_within(sub.expression, proj):
                for lin in self.outputs(sub):
                    sources |= lin.sources
                direct = False

        for column in expr.find_all(exp.Column):
            if column.find_ancestor(exp.Select) is not scope.expression:
                continue  # belongs to a nested subquery, handled above
            if not column.table:
                raise AccessDenied(f"could not trace lineage of {column.sql(DIALECT)}")  # fail closed
            source = _resolve_source(column, scope)
            if isinstance(source, exp.Table):
                sources.add((source.name.lower(), column.name.lower()))
            else:
                child = self._named_output(source, column.name)
                sources |= child.sources
                direct = direct and child.direct
        return Lineage(frozenset(sources), direct)

    def _named_output(self, scope: Scope, name: str) -> Lineage:
        outputs = self.outputs(scope)
        names = [n.lower() for n in scope.expression.named_selects]
        if name.lower() not in names:
            raise AccessDenied(f"could not trace column {name!r} through a subquery/CTE")
        return outputs[names.index(name.lower())]


def _is_within(node: exp.Expression, ancestor: exp.Expression) -> bool:
    current = node
    while current is not None:
        if current is ancestor:
            return True
        current = current.parent
    return False


def _mask_mode(lineage: Lineage, role: RolePolicy) -> MaskMode | None:
    masked = [s for s in lineage.sources if role.action_for(*s) == "mask"]
    if not masked:
        return None
    return "partial" if lineage.direct else "full"


def check_access(tree: exp.Expression, role: RolePolicy) -> AccessDecision:
    """Decide whether ``role`` may run ``tree`` and how each output column must be masked."""
    try:
        _reject_star_projections(tree)
        _reject_denied_tables(tree, role)
        try:
            qualified = qualify(
                tree.copy(), schema=_qualify_schema(), dialect=DIALECT,
                quote_identifiers=False, identify=False, validate_qualify_columns=True,
            )
        except SqlglotError as e:
            raise AccessDenied(f"could not resolve columns: {str(e).splitlines()[0]}") from None

        root = build_scope(qualified)
        if root is None:
            raise AccessDenied("could not analyse query scopes")
        referenced: set[tuple[str, str]] = set()
        for scope in root.traverse():
            _check_scope_columns(scope, role, referenced)

        lineage = _LineageResolver().outputs(root)
        plan = [_mask_mode(lin, role) for lin in lineage]
    except AccessDenied as e:
        return AccessDecision(ok=False, reason=str(e))
    return AccessDecision(ok=True, mask_plan=plan, referenced=frozenset(referenced))
