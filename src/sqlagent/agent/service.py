"""Agent facade: runs the compiled graph per conversation thread and maps state to AgentResponse.
Used by the API, the CLI and the eval harness."""
from __future__ import annotations

import time
import uuid
from decimal import Decimal
from typing import Any

from langgraph.types import Command

from sqlagent.agent.graph import Services, build_graph, initial_state
from sqlagent.agent.prompts import PROMPT_VERSION
from sqlagent.core.schemas import (
    AgentResponse,
    Ambiguity,
    Clarification,
    QualifiedRequest,
    Status,
    UserContext,
    Violation,
)
from sqlagent.observability.audit import AuditLogger


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


class Agent:
    def __init__(self, services: Services, checkpointer: Any, audit: AuditLogger | None = None):
        self.svc = services
        self.graph = build_graph(services).compile(checkpointer=checkpointer)
        self.audit = audit

    def ask(self, question: str, user: UserContext, conversation_id: str | None = None) -> AgentResponse:
        """Ask a new question, or answer the pending clarification on this conversation."""
        conversation_id = conversation_id or uuid.uuid4().hex
        config = {"configurable": {"thread_id": conversation_id}}
        t0 = time.perf_counter()
        snapshot = self.graph.get_state(config)
        pending = bool(snapshot and snapshot.next and snapshot.interrupts)
        if pending:
            owner = snapshot.values.get("user", {})
            if owner.get("user_id") != user.user_id or owner.get("role") != user.role:
                raise PermissionError("conversation belongs to another user")
            request_id = snapshot.values["request_id"]
            out = self.graph.invoke(Command(resume=question), config)
        else:
            request_id = uuid.uuid4().hex
            out = self.graph.invoke(initial_state(request_id, question, user), config)
        resp = self._to_response(out, request_id, conversation_id, int((time.perf_counter() - t0) * 1000))
        if self.audit:
            self.audit.write(resp, user, out, PROMPT_VERSION, self.svc.settings)
        return resp

    def _to_response(self, out: dict, request_id: str, conversation_id: str, ms: int) -> AgentResponse:
        spec = QualifiedRequest.model_validate(out["spec"]) if out.get("spec") else None
        base = {"request_id": request_id, "conversation_id": conversation_id, "spec": spec,
                "attempts": out.get("attempts", 0), "latency_ms": ms,
                "assumptions": spec.assumptions if spec else []}
        interrupts = out.get("__interrupt__")
        if interrupts:
            qs = [Ambiguity.model_validate(q) for q in interrupts[0].value["questions"]]
            return AgentResponse(**base, status=Status.needs_clarification,
                                 message=" ".join(q.question for q in qs),
                                 clarification=Clarification(questions=qs))
        status = Status(out.get("status", "error")) if out.get("status") in Status.__members__ else Status.error
        resp = AgentResponse(**base, status=status, message=out.get("message", ""),
                             violations=[Violation.model_validate(v) for v in out.get("violations") or []])
        if status == Status.answered:
            res = out["result"]
            resp.sql = out.get("enforced_sql")
            resp.columns = res["columns"]
            resp.rows = [[_jsonable(v) for v in r] for r in res["rows"]]
            resp.row_count = len(res["rows"])
            resp.truncated = res["truncated"]
            resp.tables_used = (out.get("guardrail") or {}).get("tables", [])
            metrics = spec.metrics if spec else []
            resp.metric_definitions = {m: self.svc.glossary.metrics[m].description for m in metrics
                                       if m in self.svc.glossary.metrics}
        return resp
