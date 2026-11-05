"""Append-only JSONL audit log: one record per agent turn (the system of record for online evals)."""
from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlagent.core.schemas import AgentResponse, UserContext


class AuditLogger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, resp: AgentResponse, user: UserContext, state: dict, prompt_version: str, settings: Any) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "request_id": resp.request_id,
            "conversation_id": resp.conversation_id,
            "user_id": user.user_id,
            "role": user.role,
            "regions": user.regions,
            "question": state.get("question"),
            "clarifications": state.get("clarifications", []),
            "status": resp.status.value,
            "message": resp.message,
            "risk_flags": state.get("risk_flags", []),
            "spec": resp.spec.model_dump(mode="json") if resp.spec else None,
            "candidate_sql": state.get("candidate_sql"),
            "enforced_sql": state.get("enforced_sql"),
            "violations": [v.model_dump() for v in resp.violations],
            "row_count": resp.row_count,
            "attempts": resp.attempts,
            "latency_ms": resp.latency_ms,
            "trace": state.get("trace", []),
            "prompt_version": prompt_version,
            "llm": f"{settings.llm_provider}:{settings.llm_model}",
        }
        line = json.dumps(record, default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
