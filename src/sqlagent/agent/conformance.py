"""Deterministic checks that candidate SQL actually answers the qualified request.

Small models often drop a requested breakdown, forget ORDER BY for a ranking, or invent their own
dates. These checks turn such misses into correctable violations, so the bounded retry loop can
fix them before anything runs. They are about answer quality, not security.
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp

from sqlagent.core.schemas import QualifiedRequest, Violation
from sqlagent.guardrails.codes import violation


def check_conformance(sql: str, spec: QualifiedRequest) -> list[Violation]:
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception:  # noqa: BLE001 - parse errors are reported by the guardrail engine
        return []
    if not isinstance(tree, exp.Query):
        return []
    out: list[Violation] = []
    outer = tree if isinstance(tree, exp.Select) else None
    wants_breakdown = bool(spec.dimensions) or spec.grain != "none" or spec.intent == "trend"
    if wants_breakdown and spec.intent != "lookup":
        has_group = any(s.args.get("group") is not None for s in tree.find_all(exp.Select))
        if not has_group and outer is not None and any(e.find(exp.AggFunc) for e in outer.expressions):
            by = spec.dimensions or [f"{spec.grain if spec.grain != 'none' else 'month'} (time trend)"]
            hint = ("Add the dimension columns to SELECT and GROUP BY them." if spec.dimensions else
                    f"SELECT date_trunc('{spec.grain if spec.grain != 'none' else 'month'}', h.purch_ts) AS period, "
                    "... GROUP BY 1 ORDER BY 1.")
            out.append(violation("V070", f"the request is broken down by {by} but the query returns a single "
                                         "aggregate", hint))
    if spec.intent == "ranking" and outer is not None and outer.args.get("order") is None:
        out.append(violation("V071", "the request is a ranking but the outer query has no ORDER BY",
                             f"ORDER BY the metric {spec.order_dir.upper()} and LIMIT {spec.limit or 10}."))
    tw = spec.time_window
    if tw and tw.start and tw.end:
        text = tree.sql(dialect="duckdb")
        if str(tw.start) not in text or str(tw.end) not in text:
            out.append(violation("V072", f"the query must filter the period {tw.start} (inclusive) to "
                                         f"{tw.end} (exclusive)",
                                 f"Use h.purch_ts >= TIMESTAMP '{tw.start}' AND h.purch_ts < TIMESTAMP '{tw.end}'."))
    return out
