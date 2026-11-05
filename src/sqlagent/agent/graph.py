"""The agent as a LangGraph StateGraph.

    START -> input_guard -> retrieve_context -> qualify -> resolve_spec
          -> (clarify -> retrieve_context ...)            # interrupt(): wait for the user
          -> generate_sql -> validate_and_enforce -> cost_check -> execute -> output_guard -> answer -> END
             ^______________ correctable violations, bounded by max_sql_attempts ___|
          -> refuse -> END

Routers are pure functions of the state (unit-tested). The security boundary is
`validate_and_enforce`, which calls the framework-free GuardrailEngine.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypedDict

import duckdb
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from sqlagent.agent.conformance import check_conformance
from sqlagent.agent.input_guard import check_input
from sqlagent.agent.prompts import QUALIFY_SYSTEM, QUALIFY_USER, SQL_SYSTEM, SQL_USER
from sqlagent.agent.resolver import Resolver
from sqlagent.agent.retrieval import Retriever
from sqlagent.config import Settings
from sqlagent.core.schemas import CandidateSQL, QualifiedRequest, UserContext, Violation
from sqlagent.execution.executor import ExecResult, Executor
from sqlagent.guardrails import GuardrailEngine
from sqlagent.llm.client import LLMError, StructuredLLM
from sqlagent.metadata import Catalog, Glossary, Policy


# =========================================================================== services
@dataclass
class Services:
    settings: Settings
    catalog: Catalog
    glossary: Glossary
    policy: Policy
    engine: GuardrailEngine
    retriever: Retriever
    resolver: Resolver
    executor: Executor
    llm: StructuredLLM

    @classmethod
    def from_settings(cls, s: Settings, llm: StructuredLLM) -> Services:
        con = duckdb.connect(str(s.warehouse_path), read_only=True)
        try:
            catalog = Catalog.load(s.catalog_path, con)
        finally:
            con.close()
        glossary = Glossary.load(s.glossary_path)
        policy = Policy.load(s.policy_path)
        from sqlagent.agent.retrieval import load_examples
        retriever = Retriever(catalog, glossary, load_examples(s.examples_path))
        return cls(s, catalog, glossary, policy, GuardrailEngine(catalog, policy), retriever,
                   Resolver(glossary), Executor(s.warehouse_path), llm)


# =========================================================================== state
class AgentState(TypedDict, total=False):
    request_id: str
    question: str
    user: dict
    risk_flags: list[str]
    clarifications: list[str]
    clarification_rounds: int
    context: dict
    spec: dict | None
    candidate_sql: str | None
    enforced_sql: str | None
    guardrail: dict | None
    violations: list[dict]
    attempts: int
    result: dict | None
    status: str
    message: str
    trace: list[dict]


def initial_state(request_id: str, question: str, user: UserContext) -> AgentState:
    return AgentState(request_id=request_id, question=question, user=user.model_dump(), risk_flags=[],
                      clarifications=[], clarification_rounds=0, context={}, spec=None, candidate_sql=None,
                      enforced_sql=None, guardrail=None, violations=[], attempts=0, result=None, status="running",
                      message="", trace=[])


def _traced(name: str, fn: Callable[[AgentState], dict]) -> Callable[[AgentState], dict]:
    def wrapper(state: AgentState) -> dict:
        t0 = time.perf_counter()
        update = fn(state) or {}
        entry = {"node": name, "ms": int((time.perf_counter() - t0) * 1000)}
        for k in ("status", "attempts"):
            if k in update:
                entry[k] = update[k]
        if update.get("violations"):
            entry["violations"] = [v["code"] for v in update["violations"]]
        update["trace"] = [*state.get("trace", []), entry]
        return update
    wrapper.__name__ = name
    return wrapper


# =========================================================================== routers (pure)
NO_RETRY_FLAGS = {"role_claim", "sql_fragment"}


def route_after_guard(state: AgentState) -> str:
    return "refuse" if state.get("status") == "refused" else "retrieve_context"


def route_after_resolve(state: AgentState) -> str:
    if state.get("status") in ("refused", "error"):
        return "refuse"
    spec = state.get("spec") or {}
    if any(a.get("blocking") for a in spec.get("ambiguities", [])):
        return "clarify"
    return "generate_sql"


def route_after_validate(state: AgentState, max_attempts: int) -> str:
    if state.get("status") == "error":
        return "refuse"
    vs = state.get("violations") or []
    if not vs:
        return "cost_check"
    if any(not v["correctable"] for v in vs):
        return "refuse"
    if set(state.get("risk_flags", [])) & NO_RETRY_FLAGS:
        return "refuse"
    return "generate_sql" if state.get("attempts", 0) < max_attempts else "refuse"


def route_after_cost(state: AgentState, max_attempts: int) -> str:
    if not state.get("violations"):
        return "execute"
    return "generate_sql" if state.get("attempts", 0) < max_attempts else "refuse"


def route_after_execute(state: AgentState, max_attempts: int) -> str:
    if state.get("result") is not None:
        return "output_guard"
    return "generate_sql" if state.get("attempts", 0) < max_attempts else "refuse"


# =========================================================================== nodes
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_CPF = re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b")
_PHONE = re.compile(r"\(?\+?\d{2}\)?\s?\d{4,5}-?\d{4}\b")


def build_graph(svc: Services) -> StateGraph:
    s = svc.settings
    max_attempts = s.max_sql_attempts

    def user_of(state: AgentState) -> UserContext:
        return UserContext.model_validate(state["user"])

    def full_question(state: AgentState) -> str:
        return " ".join([state["question"], *state.get("clarifications", [])])

    # ---------------------------------------------------------------- S1
    def input_guard(state: AgentState) -> dict:
        user = user_of(state)
        try:
            role = svc.policy.role(user.role)
        except KeyError:
            return {"status": "refused", "message": f"Unknown role {user.role!r}."}
        g = check_input(state["question"], role, svc.catalog)
        if g.blocked:
            return {"status": "refused", "message": g.reason, "risk_flags": g.risk_flags}
        return {"risk_flags": g.risk_flags}

    # ---------------------------------------------------------------- S2
    def retrieve_context(state: AgentState) -> dict:
        role = svc.policy.role(state["user"]["role"])
        spec = state.get("spec") or {}
        ctx = svc.retriever.retrieve(full_question(state), role, s.retrieval_top_k_tables,
                                     extra_metrics=spec.get("metrics") or [])
        return {"context": ctx.__dict__}

    # ---------------------------------------------------------------- S3a (LLM)
    def qualify(state: AgentState) -> dict:
        user = user_of(state)
        role = svc.policy.role(user.role)
        clar = "\n".join(f"CLARIFICATION: {c}" for c in state.get("clarifications", []))
        prompt = QUALIFY_USER.format(
            question=state["question"], clarifications=clar, role=role.name, role_description=role.description,
            regions=f" regions={user.regions}" if user.regions else "", as_of_date=svc.resolver.as_of,
            metrics=svc.retriever.metric_catalog_text(role), ddl=state["context"]["ddl"])
        try:
            spec = svc.llm.generate(QualifiedRequest, QUALIFY_SYSTEM, prompt)
        except LLMError as e:
            return {"status": "error", "message": f"Could not interpret the question ({e})."}
        return {"spec": spec.model_dump(mode="json")}

    # ---------------------------------------------------------------- S3b (deterministic)
    def resolve_spec(state: AgentState) -> dict:
        if state.get("status") == "error":
            return {}
        spec = QualifiedRequest.model_validate(state["spec"])
        if spec.out_of_scope_reason:
            return {"status": "refused", "message": f"I can't answer this: {spec.out_of_scope_reason}"}
        question = state["question"]
        matched = svc.retriever.match_metrics(full_question(state))
        spec = svc.resolver.resolve(spec, question, state.get("clarifications", []),
                                    set(svc.glossary.metrics), matched)
        blocking = svc.resolver.blocking(spec)
        if blocking and state.get("clarification_rounds", 0) >= s.max_clarification_rounds:
            for a in blocking:  # stop asking: take the first option and say so
                a.blocking = False
                spec.assumptions.append(f"Assumed '{a.options[0] if a.options else 'default'}' for: {a.question}")
                if a.slot == "metric" and a.options:
                    label_to_id = {m.label: m.id for m in svc.glossary.metrics.values()}
                    mid = label_to_id.get(a.options[0])
                    if mid and mid not in spec.metrics:
                        spec.metrics.insert(0, mid)
        role = svc.policy.role(state["user"]["role"])
        ctx = svc.retriever.retrieve(full_question(state), role, s.retrieval_top_k_tables,
                                     extra_metrics=spec.metrics)
        return {"spec": spec.model_dump(mode="json"), "context": ctx.__dict__}

    def clarify(state: AgentState) -> dict:
        spec = state["spec"]
        questions = [a for a in spec["ambiguities"] if a["blocking"]]
        answer = interrupt({"questions": questions})  # graph pauses here; resumes with the user's reply
        qa = "; ".join(q["question"] for q in questions)
        return {"clarifications": [*state.get("clarifications", []), f"Q: {qa} A: {answer}"],
                "clarification_rounds": state.get("clarification_rounds", 0) + 1}

    # ---------------------------------------------------------------- S4 (LLM)
    def generate_sql(state: AgentState) -> dict:
        ctx = state["context"]
        feedback = ""
        if state.get("violations"):
            lines = [f"- {v['code']} {v['message']}" + (f" HINT: {v['hint']}" if v.get("hint") else "")
                     for v in state["violations"]]
            feedback = ("\nPREVIOUS ATTEMPT:\n" + (state.get("candidate_sql") or "") +
                        "\nPREVIOUS ATTEMPT VIOLATIONS:\n" + "\n".join(lines))
        spec = {k: v for k, v in state["spec"].items() if k not in ("ambiguities",)}
        role = svc.policy.role(state["user"]["role"])
        shots = svc.retriever.similar_examples(state["question"], role)
        examples = "\n\n".join(f"-- Q: {e.question}\n{e.sql}" for e in shots) or "-"
        prompt = SQL_USER.format(spec=json.dumps(spec, indent=1, default=str), metric_defs=ctx["metric_defs"] or "-",
                                 examples=examples,
                                 ddl=ctx["ddl"], joins=ctx["joins"] or "-", notes="\n".join(ctx["notes"]) or "-",
                                 feedback=feedback)
        attempts = state.get("attempts", 0) + 1
        try:
            cand = svc.llm.generate(CandidateSQL, SQL_SYSTEM, prompt)
        except LLMError as e:
            return {"status": "error", "message": f"SQL generation failed ({e}).", "attempts": attempts}
        sql = cand.sql.strip().strip("`")
        sql = re.sub(r"^sql\s*\n", "", sql, flags=re.I)
        return {"candidate_sql": sql, "attempts": attempts, "violations": [], "status": "running"}

    # ---------------------------------------------------------------- S5+S6 (security boundary)
    def validate_and_enforce(state: AgentState) -> dict:
        if state.get("status") == "error":
            return {}
        r = svc.engine.check(state["candidate_sql"], user_of(state))
        violations = list(r.violations)
        if r.allowed and state.get("spec"):
            violations = check_conformance(state["candidate_sql"], QualifiedRequest.model_validate(state["spec"]))
        ok = r.allowed and not violations
        return {"guardrail": r.model_dump(), "enforced_sql": r.enforced_sql if ok else None,
                "violations": [v.model_dump() for v in violations]}

    # ---------------------------------------------------------------- S7
    def cost_check(state: AgentState) -> dict:
        vs = svc.executor.cost_check(state["enforced_sql"], s.max_estimated_rows)
        return {"violations": [v.model_dump() for v in vs]}

    # ---------------------------------------------------------------- S9
    def execute(state: AgentState) -> dict:
        role = svc.policy.role(state["user"]["role"])
        res = svc.executor.run(state["enforced_sql"], role.max_rows, role.timeout_s)
        if isinstance(res, Violation):
            return {"result": None, "violations": [res.model_dump()]}
        return {"result": {"columns": res.columns, "rows": res.rows, "truncated": res.truncated,
                           "elapsed_ms": res.elapsed_ms}, "violations": []}

    # ---------------------------------------------------------------- S10
    def output_guard(state: AgentState) -> dict:
        role = svc.policy.role(state["user"]["role"])
        if role.action_for("pii_direct") == "allow":
            return {}
        result = dict(state["result"])
        redacted = 0
        rows = []
        for row in result["rows"]:
            new = []
            for v in row:
                if isinstance(v, str) and (_EMAIL.search(v) or _CPF.search(v)):
                    v, redacted = "***", redacted + 1
                new.append(v)
            rows.append(new)
        result["rows"] = rows
        update: dict[str, Any] = {"result": result}
        if redacted:
            update["risk_flags"] = [*state.get("risk_flags", []), f"output_redacted:{redacted}"]
        return update

    def answer(state: AgentState) -> dict:
        res = state["result"]
        spec = state["spec"] or {}
        n = len(res["rows"])
        if n == 0:
            msg = "No rows matched. " + ("The period was " + spec["time_window"]["label"] + ". "
                                         if spec.get("time_window") and spec["time_window"].get("label") else "")
            msg += "Your access scope or the filters may exclude all data."
        elif n == 1 and len(res["columns"]) <= 4:
            msg = ", ".join(f"{c} = {_fmt(v)}" for c, v in zip(res["columns"], res["rows"][0], strict=False))
        else:
            first = ", ".join(f"{c} = {_fmt(v)}" for c, v in zip(res["columns"], res["rows"][0], strict=False))
            msg = f"{n} rows{' (truncated)' if res['truncated'] else ''}. First row: {first}."
        return {"status": "answered", "message": msg}

    def refuse(state: AgentState) -> dict:
        if state.get("status") in ("refused", "error") and state.get("message"):
            return {"status": state["status"]}
        vs = state.get("violations") or []
        hard = [v for v in vs if not v["correctable"]]
        if hard:
            msg = "This request is blocked by data policy: " + "; ".join(v["message"] for v in hard)
        elif vs:
            msg = (f"I could not produce a valid query after {state.get('attempts', 0)} attempts: "
                   + "; ".join(v["message"] for v in vs))
        else:
            msg = "The request could not be completed."
        return {"status": "refused" if hard or not vs else "error", "message": msg}

    g = StateGraph(AgentState)
    nodes = {"input_guard": input_guard, "retrieve_context": retrieve_context, "qualify": qualify,
             "resolve_spec": resolve_spec, "clarify": clarify, "generate_sql": generate_sql,
             "validate_and_enforce": validate_and_enforce, "cost_check": cost_check, "execute": execute,
             "output_guard": output_guard, "answer": answer, "refuse": refuse}
    for name, fn in nodes.items():
        g.add_node(name, _traced(name, fn))
    g.add_edge(START, "input_guard")
    g.add_conditional_edges("input_guard", route_after_guard, ["retrieve_context", "refuse"])
    g.add_edge("retrieve_context", "qualify")
    g.add_edge("qualify", "resolve_spec")
    g.add_conditional_edges("resolve_spec", route_after_resolve, ["clarify", "generate_sql", "refuse"])
    g.add_edge("clarify", "retrieve_context")
    g.add_edge("generate_sql", "validate_and_enforce")
    g.add_conditional_edges("validate_and_enforce", lambda st: route_after_validate(st, max_attempts),
                            ["cost_check", "generate_sql", "refuse"])
    g.add_conditional_edges("cost_check", lambda st: route_after_cost(st, max_attempts),
                            ["execute", "generate_sql", "refuse"])
    g.add_conditional_edges("execute", lambda st: route_after_execute(st, max_attempts),
                            ["output_guard", "generate_sql", "refuse"])
    g.add_edge("output_guard", "answer")
    g.add_edge("answer", END)
    g.add_edge("refuse", END)
    return g


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


__all__ = ["AgentState", "ExecResult", "Services", "build_graph", "initial_state"]
