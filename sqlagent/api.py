"""HTTP API.

    POST /v1/query     {question, conversation_id?}  -> AgentResponse  (answers a pending clarification too)
    POST /v1/validate  {sql}                          -> GuardrailResult (guardrail engine only, no LLM)
    GET  /v1/metrics                                  -> certified metric catalogue
    GET  /healthz

Identity comes from the bearer token (see config/users.yaml), never from the request body.
Run:  uvicorn sqlagent.api:app --port 8000
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from sqlagent.agent import Agent
from sqlagent.config import ROOT
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
