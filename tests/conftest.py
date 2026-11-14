from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from glasshouse.app import create_app
from glasshouse.config import Settings
from glasshouse.generate import generate
from glasshouse.provider import FakeProvider


@pytest.fixture(scope="session")
def db_path(tmp_path_factory) -> Path:
    return generate(seed=42, db_path=tmp_path_factory.mktemp("db") / "glasshouse.db")


@pytest.fixture(scope="session")
def settings(db_path) -> Settings:
    return Settings(db_path=db_path, provider="fake")


@pytest.fixture
def client(settings) -> TestClient:
    return TestClient(create_app(settings, FakeProvider()))


def make_client(settings: Settings, fixtures: dict) -> TestClient:
    return TestClient(create_app(settings, FakeProvider(fixtures)))


def table_counts(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        conn.close()


def planted(db_path: Path, pattern: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {r[0] for r in conn.execute("SELECT account_id FROM planted_patterns WHERE pattern = ?", (pattern,))}
    finally:
        conn.close()
