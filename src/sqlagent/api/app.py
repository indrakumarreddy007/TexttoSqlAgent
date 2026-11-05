"""HTTP API.

    POST /v1/query     {question, conversation_id?}  -> AgentResponse  (answers a pending clarification too)
    POST /v1/validate  {sql}                          -> GuardrailResult (guardrail engine only, no LLM)
    GET  /v1/metrics                                  -> certified metric catalogue
    GET  /v1/me                                       -> the caller's identity and access scope
    GET  /v1/dev/identities                           -> demo users for the UI (dev mode only)
    GET  /                                            -> web UI
    GET  /explorer                                    -> data explorer (docs/data-explorer.html)
    GET  /healthz

Identity comes from the bearer token (see config/users.yaml), never from the request body.
Run:  uvicorn sqlagent.api.app:app --port 8000   (or: sqlagent-api)
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from sqlagent.agent.service import Agent
from sqlagent.bootstrap import build_agent
from sqlagent.config import ROOT, get_settings
from sqlagent.core.identity import describe, load_dev_users
from sqlagent.core.schemas import AgentResponse, GuardrailResult, UserContext

app = FastAPI(title="Halcyon SQL Copilot", version="0.1.0")


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = Field(None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class ValidateRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=20000)


@lru_cache
def get_agent() -> Agent:
    return build_agent()




def resolve_user(authorization: str = Header(default="")) -> UserContext:
    scheme, _, token = authorization.partition(" ")
    user = load_dev_users().get(token) if scheme.lower() == "bearer" else None
    if user is None:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")
    return user


STATIC = Path(__file__).resolve().parent / "static"




@app.get("/", include_in_schema=False)
def ui() -> FileResponse:
    return FileResponse(STATIC / "index.html", media_type="text/html")


@app.get("/explorer", include_in_schema=False)
def explorer() -> FileResponse:
    page = ROOT / "docs" / "data-explorer.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="run: python scripts/build_data_explorer.py")
    return FileResponse(page, media_type="text/html")


@app.get("/v1/me")
def me(user: UserContext = Depends(resolve_user)) -> dict:
    label, scope = describe(user)
    return {**user.model_dump(), "label": label, "scope": scope}


@app.get("/v1/dev/identities", include_in_schema=False)
def dev_identities() -> list[dict]:
    if not get_settings().dev_mode:
        raise HTTPException(status_code=404, detail="not found")
    out = []
    for tok, u in load_dev_users().items():
        label, scope = describe(u)
        out.append({"token": tok, **u.model_dump(), "label": label, "scope": scope})
    return out


def serve() -> None:
    """Console entry point: sqlagent-api [--host 0.0.0.0] [--port 8000]."""
    import argparse

    import uvicorn
    ap = argparse.ArgumentParser(description="Run the Halcyon SQL Copilot API and web UI")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    uvicorn.run("sqlagent.api.app:app", host=a.host, port=a.port)


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
