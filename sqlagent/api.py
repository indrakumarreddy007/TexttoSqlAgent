"""HTTP API.

    POST /v1/query     {question, conversation_id?}  -> AgentResponse  (answers a pending clarification too)
    POST /v1/validate  {sql}                          -> GuardrailResult (guardrail engine only, no LLM)
    GET  /v1/metrics                                  -> certified metric catalogue
    GET  /v1/me                                       -> the caller's identity and access scope
    GET  /v1/dev/identities                           -> demo users for the UI (dev mode only)
    GET  /                                            -> web UI
    GET  /healthz

Identity comes from the bearer token (see config/users.yaml), never from the request body.
Run:  uvicorn sqlagent.api:app --port 8000
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from sqlagent.agent import Agent
from sqlagent.config import ROOT, get_settings
from sqlagent.runtime import build_agent
from sqlagent.schemas import AgentResponse, GuardrailResult, UserContext

app = FastAPI(title="Halcyon SQL Copilot", version="0.1.0")
USERS_PATH = ROOT / "config" / "users.yaml"


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = Field(None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class ValidateRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=20000)


@lru_cache
def get_agent() -> Agent:
    return build_agent()


@lru_cache
def _tokens(path: Path = USERS_PATH) -> dict[str, UserContext]:
    doc = yaml.safe_load(path.read_text())
    return {tok: UserContext(**u) for tok, u in doc["tokens"].items()}


def resolve_user(authorization: str = Header(default="")) -> UserContext:
    scheme, _, token = authorization.partition(" ")
    user = _tokens().get(token) if scheme.lower() == "bearer" else None
    if user is None:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")
    return user


STATIC = Path(__file__).resolve().parent / "static"


def _scope(u: UserContext) -> tuple[str, str]:
    labels = {"exec": "Executive", "regional_mgr": "Regional manager", "analyst": "Business analyst",
              "hr_analyst": "HR analyst", "admin": "Data steward"}
    scopes = {"exec": "all regions · customer PII masked", "analyst": "all regions · no PII",
              "hr_analyst": "HR only · names masked · pay aggregated (min 5)", "admin": "unrestricted"}
    scope = f"regions {', '.join(u.regions) or 'none'} · no PII" if u.role == "regional_mgr" else scopes.get(u.role, "")
    return labels.get(u.role, u.role), scope


@app.get("/", include_in_schema=False)
def ui() -> FileResponse:
    return FileResponse(STATIC / "index.html", media_type="text/html")


@app.get("/v1/me")
def me(user: UserContext = Depends(resolve_user)) -> dict:
    label, scope = _scope(user)
    return {**user.model_dump(), "label": label, "scope": scope}


@app.get("/v1/dev/identities", include_in_schema=False)
def dev_identities() -> list[dict]:
    if not get_settings().dev_mode:
        raise HTTPException(status_code=404, detail="not found")
    out = []
    for tok, u in _tokens().items():
        label, scope = _scope(u)
        out.append({"token": tok, **u.model_dump(), "label": label, "scope": scope})
    return out


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/v1/query", response_model=AgentResponse)
def query(req: QueryRequest, user: UserContext = Depends(resolve_user), agent: Agent = Depends(get_agent)):
    try:
        return agent.ask(req.question, user, req.conversation_id)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e


@app.post("/v1/validate", response_model=GuardrailResult)
def validate(req: ValidateRequest, user: UserContext = Depends(resolve_user), agent: Agent = Depends(get_agent)):
    return agent.svc.engine.check(req.sql, user)


@app.get("/v1/metrics")
def metrics(agent: Agent = Depends(get_agent)) -> list[dict]:
    return [{"id": m.id, "label": m.label, "description": m.description, "owner": m.owner}
            for m in agent.svc.glossary.metrics.values()]
