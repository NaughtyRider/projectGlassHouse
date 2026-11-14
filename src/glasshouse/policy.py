"""Role-based column access policy, loaded from config/access_policy.yaml.

The policy is validated strictly at load time: unknown tables or columns, unknown actions or
unexpected keys are configuration errors (fail at startup, not at query time).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from glasshouse.schema import CATALOG, render_schema

Action = Literal["allow", "mask", "deny"]
ACTIONS: frozenset[str] = frozenset({"allow", "mask", "deny"})


class PolicyError(Exception):
    """The policy file is missing, malformed or refers to things that don't exist."""


class UnknownRoleError(Exception):
    """The request's role is missing or not defined in the policy. Maps to HTTP 403."""


@dataclass(frozen=True)
class RolePolicy:
    name: str
    description: str = ""
    column_actions: dict[tuple[str, str], Action] = field(default_factory=dict)
    denied_tables: frozenset[str] = frozenset()
    default_action: Action = "allow"

    def action_for(self, table: str, column: str) -> Action:
        key = (table.lower(), column.lower())
        if key[0] in self.denied_tables:  # a table-wide deny cannot be re-opened per column
            return "deny"
        return self.column_actions.get(key, self.default_action)

    def is_table_denied(self, table: str) -> bool:
        return table.lower() in self.denied_tables

    def denied_columns(self) -> frozenset[tuple[str, str]]:
        """Every (table, column) in the catalog this role may not read. Used by the SQLite authorizer."""
        return frozenset((t, c) for t, cols in CATALOG.items() for c in cols if self.action_for(t, c) == "deny")

    def prompt_schema(self) -> str:
        return render_schema(self.action_for)


@dataclass(frozen=True)
class AccessPolicy:
    roles: dict[str, RolePolicy]

    def get_role(self, role: Any) -> RolePolicy:
        """Exact, case-sensitive lookup. Never falls back to a default role."""
        if role is None or (isinstance(role, str) and not role.strip()):
            raise UnknownRoleError("role is required; valid roles: " + ", ".join(sorted(self.roles)))
        if not isinstance(role, str) or role not in self.roles:
            shown = role if isinstance(role, str) else type(role).__name__
            raise UnknownRoleError(f"unknown role {shown!r}; valid roles: " + ", ".join(sorted(self.roles)))
        return self.roles[role]

    @classmethod
    def load(cls, path: Path | str) -> "AccessPolicy":
        path = Path(path)
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise PolicyError(f"access policy not found: {path}") from None
        except yaml.YAMLError as e:
            raise PolicyError(f"access policy is not valid YAML: {e}") from None
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: Any) -> "AccessPolicy":
        if not isinstance(raw, dict):
            raise PolicyError("access policy must be a mapping")
        _check_keys(raw, {"version", "default_column_action", "roles"}, "policy")
        default = raw.get("default_column_action", "allow")
        if default not in ACTIONS:
            raise PolicyError(f"default_column_action must be one of {sorted(ACTIONS)}, got {default!r}")
        roles_raw = raw.get("roles")
        if not isinstance(roles_raw, dict) or not roles_raw:
            raise PolicyError("policy must define at least one role under 'roles'")

        roles: dict[str, RolePolicy] = {}
        for name, spec in roles_raw.items():
            if not isinstance(name, str) or not name.strip():
                raise PolicyError(f"invalid role name: {name!r}")
            spec = spec or {}
            if not isinstance(spec, dict):
                raise PolicyError(f"role {name!r} must be a mapping")
            _check_keys(spec, {"description", "columns", "tables"}, f"role {name!r}")

            denied_tables: set[str] = set()
            for table, action in (spec.get("tables") or {}).items():
                t = str(table).lower()
                if t not in CATALOG:
                    raise PolicyError(f"role {name!r}: unknown table {table!r}")
                if action != "deny":
                    raise PolicyError(f"role {name!r}: table-level action must be 'deny', got {action!r}")
                denied_tables.add(t)

            column_actions: dict[tuple[str, str], Action] = {}
            for key, action in (spec.get("columns") or {}).items():
                if not isinstance(key, str) or key.count(".") != 1:
                    raise PolicyError(f"role {name!r}: column key must be 'table.column', got {key!r}")
                t, c = (p.strip().lower() for p in key.split("."))
                if t not in CATALOG or c not in CATALOG[t]:
                    raise PolicyError(f"role {name!r}: unknown column {key!r}")
                if action not in ACTIONS:
                    raise PolicyError(f"role {name!r}: action for {key!r} must be one of {sorted(ACTIONS)}")
                column_actions[(t, c)] = action

            roles[name] = RolePolicy(
                name=name,
                description=str(spec.get("description", "")),
                column_actions=column_actions,
                denied_tables=frozenset(denied_tables),
                default_action=default,
            )
        return cls(roles=roles)


def _check_keys(mapping: dict, allowed: set[str], where: str) -> None:
    extra = set(mapping) - allowed
    if extra:
        raise PolicyError(f"{where}: unexpected keys {sorted(extra)}")
