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

_KEY_SUFFIXES = ("_id", "_uid", "_sk")


def _is_key(name: str) -> bool:
    return name.lower().endswith(_KEY_SUFFIXES)


def _conditions(expr: exp.Expression) -> list[exp.Expression]:
    """Split a predicate on AND into its conditions."""
    if isinstance(expr, exp.And):
        return _conditions(expr.this) + _conditions(expr.expression)
    if isinstance(expr, exp.Paren):
        return _conditions(expr.this)
    return [expr]


def _unqualified(expr: exp.Expression) -> str:
    e = expr.copy()
    for col in e.find_all(exp.Column):
        col.set("table", None)
    return e.sql(dialect="duckdb").lower().replace('"', "")


def check_conformance(sql: str, spec: QualifiedRequest,
                      join_pairs: set[frozenset[str]] | None = None,
                      metric_filters: dict[str, str] | None = None) -> list[Violation]:
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
    if join_pairs is not None:
        for join in tree.find_all(exp.Join):
            on = join.args.get("on")
            for eq in on.find_all(exp.EQ) if on is not None else []:
                left, right = eq.this, eq.expression
                if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                    continue
                a, b = left.name.lower(), right.name.lower()
                if a != b and _is_key(a) and _is_key(b) and frozenset((a, b)) not in join_pairs:
                    out.append(violation("V073", f"join compares different keys {left.sql()} = {right.sql()}",
                                         "Join on the documented key pairs listed under JOINS (usually the same "
                                         "column name on both sides, e.g. h.cust_id = c.cust_id)."))
    if metric_filters:
        present = {_unqualified(c) for node in tree.find_all(exp.Where, exp.Join)
                   for part in ([node.this] if isinstance(node, exp.Where) else [node.args.get("on")])
                   if part is not None for c in _conditions(part)}
        for metric in spec.metrics:
            where = metric_filters.get(metric)
            if not where:
                continue
            for cond in _conditions(sqlglot.parse_one(where, read="duckdb")):
                if _unqualified(cond) not in present:
                    out.append(violation("V074", f"certified metric {metric} requires the condition {cond.sql()}",
                                         "Use the certified metric's WHERE conditions exactly as given in METRICS."))
    tw = spec.time_window
    if tw and tw.start and tw.end:
        text = tree.sql(dialect="duckdb")
        if str(tw.start) not in text or str(tw.end) not in text:
            out.append(violation("V072", f"the query must filter the period {tw.start} (inclusive) to "
                                         f"{tw.end} (exclusive)",
                                 f"Use h.purch_ts >= TIMESTAMP '{tw.start}' AND h.purch_ts < TIMESTAMP '{tw.end}'."))
    return out
