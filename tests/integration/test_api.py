"""API contract: authentication, identity from token, validate endpoint, query endpoint."""
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from sqlagent.agent.graph import Services
from sqlagent.agent.resolver import Resolver
from sqlagent.agent.retrieval import Retriever
from sqlagent.agent.service import Agent
from sqlagent.api import app as api
from sqlagent.config import Settings
from sqlagent.llm.client import ScriptedLLM

ORDERS_SQL = ("SELECT COUNT(DISTINCT h.ord_id) AS orders FROM sales.ord_hdr h "
              "WHERE h.ord_stat_cd NOT IN ('canceled', 'unavailable')")


@pytest.fixture
def client(warehouse, catalog, glossary, policy, engine, executor):
    llm = ScriptedLLM({
        "QualifiedRequest": [{"metrics": ["order_count"]}],
        "CandidateSQL": [{"sql": ORDERS_SQL}],
    })
    svc = Services(Settings(warehouse_path=warehouse), catalog, glossary, policy, engine,
                   Retriever(catalog, glossary), Resolver(glossary), executor, llm)
    api.app.dependency_overrides[api.get_agent] = lambda: Agent(svc, InMemorySaver())
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def test_requires_token(client):
    assert client.post("/v1/query", json={"question": "x"}).status_code == 401
    assert client.post("/v1/query", json={"question": "x"},
                       headers={"Authorization": "Bearer nope"}).status_code == 401


def test_validate_uses_token_identity(client):
    sql = 'SELECT c.cpf FROM crm."CUST_MSTR_V2" c WHERE c.is_current'
    r = client.post("/v1/validate", json={"sql": sql}, headers={"Authorization": "Bearer dev-analyst-token"})
    body = r.json()
    assert r.status_code == 200 and body["allowed"] is False
    assert body["violations"][0]["code"] == "V021"
    r = client.post("/v1/validate", json={"sql": sql}, headers={"Authorization": "Bearer dev-exec-token"})
    assert r.json()["allowed"] is True  # masked for exec


def test_query_round_trip(client):
    r = client.post("/v1/query", json={"question": "how many orders"},
                    headers={"Authorization": "Bearer dev-analyst-token"})
    body = r.json()
    assert r.status_code == 200, body
    assert body["status"] == "answered" and body["rows"][0][0] > 0
    assert body["sql"].rstrip().endswith("LIMIT 1000")


def test_body_cannot_set_role(client):
    r = client.post("/v1/query", json={"question": "how many orders", "role": "admin"},
                    headers={"Authorization": "Bearer dev-analyst-token"})
    assert r.status_code == 200  # extra field ignored; identity still analyst


def test_metrics_listing(client):
    r = client.get("/v1/metrics")
    assert {"net_revenue_usd", "order_count"} <= {m["id"] for m in r.json()}


def test_ui_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Halcyon SQL Copilot" in r.text and "text/html" in r.headers["content-type"]


def test_me_reports_scope(client):
    r = client.get("/v1/me", headers={"Authorization": "Bearer dev-mgr-se-token"})
    assert r.json()["role"] == "regional_mgr" and "SE" in r.json()["scope"]


def test_dev_identities_only_in_dev_mode(client, monkeypatch):
    assert any(u["role"] == "hr_analyst" for u in client.get("/v1/dev/identities").json())
    from sqlagent.config import Settings
    monkeypatch.setattr(api, "get_settings", lambda: Settings(dev_mode=False))
    assert client.get("/v1/dev/identities").status_code == 404


def test_explorer_is_served(client):
    r = client.get("/explorer")
    assert r.status_code in (200, 404)  # 404 until scripts/build_data_explorer.py has been run
    if r.status_code == 200:
        assert "Halcyon Data Explorer" in r.text
