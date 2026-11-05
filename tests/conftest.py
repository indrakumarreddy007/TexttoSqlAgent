"""Shared fixtures: an isolated warehouse built from seeded sample data (same schema as Kaggle)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "setup"))

import build_warehouse
import make_sample_data

from sqlagent.executor import Executor
from sqlagent.guardrails import GuardrailEngine
from sqlagent.metadata import Catalog, Glossary, Policy
from sqlagent.schemas import UserContext


@pytest.fixture(scope="session")
def warehouse(tmp_path_factory) -> Path:
    os.environ["SQLAGENT_OFFLINE"] = "1"
    base = tmp_path_factory.mktemp("wh")
    make_sample_data.generate(base / "raw", orders=2500, seed=11)
    out = base / "warehouse.duckdb"
    build_warehouse.build(base / "raw", out)
    return out


@pytest.fixture(scope="session")
def catalog(warehouse) -> Catalog:
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        return Catalog.load(ROOT / "config" / "catalog.yaml", con)
    finally:
        con.close()


@pytest.fixture(scope="session")
def policy() -> Policy:
    return Policy.load(ROOT / "config" / "policy.yaml")


@pytest.fixture(scope="session")
def glossary() -> Glossary:
    return Glossary.load(ROOT / "config" / "glossary.yaml")


@pytest.fixture(scope="session")
def engine(catalog, policy) -> GuardrailEngine:
    return GuardrailEngine(catalog, policy)


@pytest.fixture(scope="session")
def executor(warehouse) -> Executor:
    return Executor(warehouse)


@pytest.fixture
def users() -> dict[str, UserContext]:
    return {
        "admin": UserContext(user_id="t-admin", role="admin"),
        "exec": UserContext(user_id="t-exec", role="exec"),
        "mgr_se": UserContext(user_id="t-mgr", role="regional_mgr", regions=["SE"]),
        "mgr_none": UserContext(user_id="t-mgr0", role="regional_mgr", regions=[]),
        "analyst": UserContext(user_id="t-an", role="analyst"),
        "hr": UserContext(user_id="t-hr", role="hr_analyst"),
    }
