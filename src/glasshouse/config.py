"""Runtime configuration, read from environment variables with safe defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = Path("data") / "glasshouse.db"
DEFAULT_FIXTURE_PATH = PACKAGE_DIR / "fixtures" / "fake_responses.json"

# ISO-3166 alpha-2 codes. Overridable with GLASSHOUSE_HIGH_RISK_COUNTRIES="IR,KP,...".
DEFAULT_HIGH_RISK_COUNTRIES: tuple[str, ...] = ("IR", "KP", "MM", "SY", "YE", "AF")

# Tables the assistant may query. Everything else (sqlite_master, planted_patterns, ...) is off-limits.
ALLOWED_TABLES: frozenset[str] = frozenset({"customers", "accounts", "transactions", "alerts"})


def _csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if not raw:
        return default
    return tuple(part.strip().upper() for part in raw.split(",") if part.strip())


def high_risk_countries() -> tuple[str, ...]:
    return _csv_env("GLASSHOUSE_HIGH_RISK_COUNTRIES", DEFAULT_HIGH_RISK_COUNTRIES)


@dataclass(frozen=True)
class Settings:
    db_path: Path = DEFAULT_DB_PATH
    provider: str = "fake"  # "fake" | "ollama"
    fixture_path: Path = DEFAULT_FIXTURE_PATH
    timeout_seconds: float = 2.0
    default_limit: int = 200
    max_limit: int = 1000
    allowed_tables: frozenset[str] = field(default_factory=lambda: ALLOWED_TABLES)
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=Path(os.environ.get("GLASSHOUSE_DB", str(DEFAULT_DB_PATH))),
            provider=os.environ.get("GLASSHOUSE_PROVIDER", "fake").lower(),
            fixture_path=Path(os.environ.get("GLASSHOUSE_FIXTURES", str(DEFAULT_FIXTURE_PATH))),
            timeout_seconds=float(os.environ.get("GLASSHOUSE_TIMEOUT_SECONDS", "2.0")),
            ollama_url=os.environ.get("GLASSHOUSE_OLLAMA_URL", "http://localhost:11434"),
            ollama_model=os.environ.get("GLASSHOUSE_OLLAMA_MODEL", "llama3.1"),
        )
