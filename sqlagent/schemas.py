"""Typed contracts shared by the graph nodes, the API and the evals."""
from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class UserContext(BaseModel):
    """Who is asking. Built from trusted identity claims, NEVER from the question text."""
    user_id: str
    role: str
    regions: list[str] = Field(default_factory=list)

    @field_validator("regions")
    @classmethod
    def _regions(cls, v: list[str]) -> list[str]:
        allowed = {"N", "NE", "CO", "SE", "S"}
        bad = [r for r in v if r not in allowed]
        if bad:
            raise ValueError(f"unknown region codes: {bad}")
        return v


# --------------------------------------------------------------------------- guardrails
class Violation(BaseModel):
    code: str
    message: str
    correctable: bool
    hint: str | None = None
    table: str | None = None
    column: str | None = None


class GuardrailResult(BaseModel):
    allowed: bool
    violations: list[Violation] = Field(default_factory=list)
    enforced_sql: str | None = None
    tables: list[str] = Field(default_factory=list)
    masked_columns: list[str] = Field(default_factory=list)
    applied_row_filters: list[str] = Field(default_factory=list)

    @property
    def refusable(self) -> bool:
        return any(not v.correctable for v in self.violations)


# --------------------------------------------------------------------------- qualification
class TimeWindow(BaseModel):
    basis: Literal["fiscal", "calendar", "explicit", "all_time"] = "fiscal"
    expression: str | None = Field(None, description="the user's wording, e.g. 'last quarter'")
    start: date | None = None
    end: date | None = Field(None, description="exclusive end date")
    label: str | None = None


class Ambiguity(BaseModel):
    slot: Literal["metric", "time_window", "entity", "grain", "filter", "other"]
    question: str
    options: list[str] = Field(default_factory=list)
    blocking: bool = True


class QualifiedRequest(BaseModel):
    """The fully-qualified interpretation of a business question (LLM output, then resolved)."""
    intent: Literal["aggregate", "ranking", "trend", "lookup", "comparison", "distribution"] = "aggregate"
    metrics: list[str] = Field(default_factory=list, description="certified metric ids from the glossary")
    dimensions: list[str] = Field(default_factory=list, description="table.column or business attribute")
    filters: list[str] = Field(default_factory=list, description="business filters in plain words or SQL")
    time_window: TimeWindow | None = None
    grain: Literal["day", "week", "month", "quarter", "year", "none"] = "none"
    order_by: str | None = None
    order_dir: Literal["asc", "desc"] = "desc"
    limit: int | None = None
    assumptions: list[str] = Field(default_factory=list)
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    out_of_scope_reason: str | None = Field(
        None, description="set when the question cannot be answered from this warehouse or asks for restricted data")


class CandidateSQL(BaseModel):
    sql: str
    tables_used: list[str] = Field(default_factory=list)
    rationale: str = ""


# --------------------------------------------------------------------------- API
class Status(str, Enum):
    answered = "answered"
    needs_clarification = "needs_clarification"
    refused = "refused"
    error = "error"


class Clarification(BaseModel):
    questions: list[Ambiguity]


class AgentResponse(BaseModel):
    request_id: str
    conversation_id: str
    status: Status
    message: str
    clarification: Clarification | None = None
    spec: QualifiedRequest | None = None
    sql: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    assumptions: list[str] = Field(default_factory=list)
    metric_definitions: dict[str, str] = Field(default_factory=dict)
    tables_used: list[str] = Field(default_factory=list)
    violations: list[Violation] = Field(default_factory=list)
    attempts: int = 0
    latency_ms: int = 0
