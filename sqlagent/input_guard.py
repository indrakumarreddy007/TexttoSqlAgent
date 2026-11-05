"""Cheap, deterministic checks on the question before any LLM call.

This is NOT the security boundary (the guardrail engine is). It exists to refuse obvious
abuse early, to flag risk so the graph disables retries, and to refuse requests for data the
role may never see before the LLM can "helpfully" answer a watered-down version.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlagent.metadata import Catalog, Role

MAX_QUESTION_CHARS = 2000

_INJECTION = [
    r"ignore (all |any )?(the )?(previous|prior|above|earlier) (instructions|rules|prompts?)",
    r"disregard (the |your )?(rules|instructions|policy|guardrails)",
    r"(reveal|show|print|repeat) (me )?(your|the) (system )?(prompt|instructions)",
    r"\byou are now\b", r"\bdeveloper mode\b", r"\bjailbreak\b", r"\bDAN\b",
    r"(bypass|disable|turn off|skip) (the )?(guardrails?|policy|policies|filters?|security|masking|restrictions?)",
    r"pretend (that )?(you|i) (are|am|have)",
]
_ROLE_CLAIM = [
    r"\bi am (the |an? )?(admin|administrator|ceo|cfo|cto|dba|superuser|hr|root)\b",
    r"\bas (the |an? )?(admin|administrator|ceo|cfo|dba|superuser)\b",
    r"\b(grant|give) me (admin|full|elevated) (access|rights|permissions)\b",
    r"\bmy role is\b",
]
_WRITE_INTENT = re.compile(
    r"\b(drop|truncate|alter|insert|update|delete|merge|grant|revoke|attach|copy|pragma)\b\s+"
    r"(table|from|into|database|schema|all|the|every|\w+\s+set|[a-z_]+\.[a-z_]+)", re.I)
_SQL_COMMENT = re.compile(r"(--|/\*|\*/|;\s*\w)")

# Business words that refer to restricted columns, mapped to (table, column)
_SENSITIVE_PHRASES = {
    "email": ("crm.cust_mstr_v2", "email"), "e-mail": ("crm.cust_mstr_v2", "email"),
    "phone": ("crm.cust_mstr_v2", "phone"), "cpf": ("crm.cust_mstr_v2", "cpf"),
    "tax id": ("crm.cust_mstr_v2", "cpf"), "customer name": ("crm.cust_mstr_v2", "cust_nm"),
    "customer names": ("crm.cust_mstr_v2", "cust_nm"), "names of customers": ("crm.cust_mstr_v2", "cust_nm"),
    "national id": ("hr.employee", "national_id"), "employee name": ("hr.employee", "emp_nm"),
    "employee names": ("hr.employee", "emp_nm"), "salary": ("hr.pay", "monthly_income"),
    "salaries": ("hr.pay", "monthly_income"), "income": ("hr.pay", "monthly_income"),
    "pay": ("hr.pay", "monthly_income"), "review comments": ("cx.review", "comment_txt"),
    "comments": ("cx.review", "comment_txt"), "performance rating": ("hr.employee", "perf_rating"),
}
_INDIVIDUAL = re.compile(r"\b(each|every|individual|per|list|named|who|whose|which)\b.*\b(employee|person|staff)\b|"
                         r"\b(employee|person|staff)\b.*\b(each|every|individual|named)\b", re.I)


@dataclass
class GuardResult:
    blocked: bool = False
    reason: str | None = None
    risk_flags: list[str] = field(default_factory=list)
    restricted: list[str] = field(default_factory=list)


def _matches(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def check_input(question: str, role: Role, catalog: Catalog) -> GuardResult:
    q = question.strip()
    if not q:
        return GuardResult(True, "The question is empty.")
    if len(q) > MAX_QUESTION_CHARS:
        return GuardResult(True, f"The question is longer than {MAX_QUESTION_CHARS} characters.")
    res = GuardResult()
    if _matches(_INJECTION, q):
        res.risk_flags.append("prompt_injection")
    if _matches(_ROLE_CLAIM, q):
        res.risk_flags.append("role_claim")
    if _SQL_COMMENT.search(q):
        res.risk_flags.append("sql_fragment")
    if _WRITE_INTENT.search(q):
        return GuardResult(True, "This assistant only reads data; it cannot change, delete or create anything.",
                           ["write_intent", *res.risk_flags])
    if "prompt_injection" in res.risk_flags:
        return GuardResult(True, "The request tries to change the assistant's rules, so it was not processed. "
                                 "Ask a data question instead.", res.risk_flags)

    for phrase, (tkey, col) in _SENSITIVE_PHRASES.items():
        if not re.search(rf"(?<![a-z]){re.escape(phrase)}(?:e?s)?(?![a-z])", q, re.I):
            continue
        t = catalog.get(tkey)
        c = t.column(col) if t else None
        if t is None or c is None:
            continue
        if not role.can_access_schema(t.schema) or role.action_for(c.sensitivity) == "deny":
            res.restricted.append(f"{t.fqn}.{c.name}")
        elif tkey in role.aggregate_only and _INDIVIDUAL.search(q):
            res.restricted.append(f"{t.fqn}.{c.name} (individual values)")
    if res.restricted:
        res.blocked = True
        res.reason = ("Your role is not permitted to see " + ", ".join(sorted(set(res.restricted))) +
                      ". Ask for aggregated or non-personal data instead, or request access from the data owner.")
        res.risk_flags.append("restricted_data_request")
    return res
