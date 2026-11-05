"""Deterministic resolution of a QualifiedRequest: relative dates against the fiscal calendar,
metric validation, glossary defaults and the clarify-or-proceed decision.

The LLM proposes an interpretation; this module makes the parts that must be exact exact,
and records every default it applies as an assumption the user will see.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from sqlagent.metadata import Glossary
from sqlagent.retrieval import _phrase_in
from sqlagent.schemas import Ambiguity, QualifiedRequest, TimeWindow


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


@dataclass(frozen=True)
class FiscalCalendar:
    start_month: int = 4  # April

    def year_start(self, d: date) -> date:
        y = d.year if d.month >= self.start_month else d.year - 1
        return date(y, self.start_month, 1)

    def fiscal_year(self, d: date) -> int:  # named by the year it ends
        return self.year_start(d).year + (1 if self.start_month > 1 else 0)

    def quarter_start(self, d: date, fiscal: bool = True) -> date:
        base = self.start_month if fiscal else 1
        offset = (d.month - base) % 12
        return _add_months(date(d.year, d.month, 1), -(offset % 3))

    def quarter_label(self, start: date, fiscal: bool = True) -> str:
        if not fiscal:
            return f"{start.year}-Q{(start.month - 1) // 3 + 1}"
        q = ((start.month - self.start_month) % 12) // 3 + 1
        return f"FY{self.fiscal_year(start)}-Q{q}"


def resolve_relative(code: str, as_of: date, basis: str, cal: FiscalCalendar) -> TimeWindow:
    """Map a relative-time code to [start, end) with a human label. `as_of` is 'today'."""
    fiscal = basis != "calendar"
    nxt = as_of + timedelta(days=1)
    if code == "previous_complete_quarter":
        cur = cal.quarter_start(as_of, fiscal)
        start = _add_months(cur, -3)
        return TimeWindow(basis=basis, start=start, end=cur, label=cal.quarter_label(start, fiscal))
    if code == "current_quarter_to_date":
        start = cal.quarter_start(as_of, fiscal)
        return TimeWindow(basis=basis, start=start, end=nxt, label=f"{cal.quarter_label(start, fiscal)} to date")
    if code == "previous_complete_year":
        cur = cal.year_start(as_of) if fiscal else date(as_of.year, 1, 1)
        start = date(cur.year - 1, cur.month, 1)
        label = f"FY{cal.fiscal_year(start)}" if fiscal else str(start.year)
        return TimeWindow(basis=basis, start=start, end=cur, label=label)
    if code == "current_year_to_date":
        start = cal.year_start(as_of) if fiscal else date(as_of.year, 1, 1)
        label = f"FY{cal.fiscal_year(start)} to date" if fiscal else f"{start.year} to date"
        return TimeWindow(basis=basis, start=start, end=nxt, label=label)
    if code == "previous_complete_month":
        cur = date(as_of.year, as_of.month, 1)
        start = _add_months(cur, -1)
        return TimeWindow(basis="calendar", start=start, end=cur, label=start.strftime("%B %Y"))
    if code == "current_month_to_date":
        start = date(as_of.year, as_of.month, 1)
        return TimeWindow(basis="calendar", start=start, end=nxt, label=f"{start:%B %Y} to date")
    if code == "trailing_12_months":
        start = _add_months(date(nxt.year, nxt.month, 1), -12) if nxt.day == 1 else nxt.replace(year=nxt.year - 1)
        return TimeWindow(basis="calendar", start=start, end=nxt, label=f"12 months to {as_of}")
    if code == "trailing_30_days":
        return TimeWindow(basis="calendar", start=nxt - timedelta(days=30), end=nxt, label=f"30 days to {as_of}")
    raise ValueError(f"unknown relative time code {code}")


_FY_RE = re.compile(r"\bfy\s?'?(\d{2,4})\b", re.I)
_YEAR_RE = re.compile(r"\b(20\d{2})\b")


class Resolver:
    def __init__(self, glossary: Glossary):
        self.g = glossary
        self.as_of = date.fromisoformat(str(glossary.defaults["as_of_date"]))
        self.cal = FiscalCalendar(int(glossary.defaults.get("fiscal_year_start_month", 4)))

    # ------------------------------------------------------------------ time
    def detect_time(self, text: str) -> TimeWindow | None:
        t = text.lower()
        basis = "calendar" if "calendar" in t else self.g.defaults.get("time_basis", "fiscal")
        for te in self.g.time_expressions:
            if _phrase_in(te["pattern"], t):
                tw = resolve_relative(te["resolves_to"], self.as_of, basis, self.cal)
                tw.expression = te["pattern"]
                return tw
        m = _FY_RE.search(t)
        if m:
            fy = int(m.group(1))
            fy = fy + 2000 if fy < 100 else fy
            start = date(fy - 1, self.cal.start_month, 1)
            return TimeWindow(basis="fiscal", expression=m.group(0), start=start, end=date(fy, self.cal.start_month, 1),
                              label=f"FY{fy}")
        m = _YEAR_RE.search(t)
        if m:
            y = int(m.group(1))
            return TimeWindow(basis="calendar", expression=m.group(0), start=date(y, 1, 1), end=date(y + 1, 1, 1),
                              label=str(y))
        return None

    # ------------------------------------------------------------------ main
    def resolve(self, spec: QualifiedRequest, question: str, clarifications: list[str],
                known_metrics: set[str], matched_metrics: list[str]) -> QualifiedRequest:
        spec = spec.model_copy(deep=True)
        text = " ".join([question, *clarifications])
        assumptions = list(spec.assumptions)

        # metrics: keep only certified ids; add those named explicitly in the text
        unknown = [m for m in spec.metrics if m not in known_metrics]
        spec.metrics = list(dict.fromkeys([m for m in spec.metrics if m in known_metrics] + matched_metrics))
        if unknown:
            assumptions.append(f"Ignored non-certified metric(s) {unknown}; using certified definitions only.")

        # time: deterministic resolution beats the LLM for relative expressions
        tw = self.detect_time(text)
        if tw is not None:
            if tw.expression and tw.label:
                assumptions.append(f"'{tw.expression}' = {tw.label} ({tw.start} to {tw.end - timedelta(days=1)}), "
                                   f"{tw.basis} basis, as of {self.as_of}")
            spec.time_window = tw
        elif spec.time_window and spec.time_window.start and spec.time_window.end:
            if spec.time_window.start >= spec.time_window.end:
                spec.time_window = None
                assumptions.append("Discarded an invalid date range; using all available data.")

        # deterministic ambiguity detection from the glossary
        ambiguities = [a for a in spec.ambiguities if a.slot != "metric"]
        for term, rule in self.g.ambiguous_terms.items():
            if not _phrase_in(term, question):
                continue
            if any(m in rule["options"] for m in spec.metrics) and (
                    set(matched_metrics) & set(rule["options"]) or clarifications):
                continue  # the user named (or chose) a metric
            if rule.get("blocking"):
                ambiguities.append(Ambiguity(slot="metric", question=rule["question"],
                                             options=[self.g.metrics[o].label for o in rule["options"]],
                                             blocking=True))
            elif rule.get("default"):
                d = rule["default"]
                if d not in spec.metrics:
                    spec.metrics = [d, *[m for m in spec.metrics if m not in rule["options"]]]
                others = ", ".join(self.g.metrics[o].label for o in rule["options"] if o != d)
                assumptions.append(f"'{term}' interpreted as {self.g.metrics[d].label} (other options: {others})")
        # after the user answered once, don't ask the same slot again
        if clarifications:
            ambiguities = [a for a in ambiguities if a.slot != "metric" or not spec.metrics]

        if spec.intent == "ranking" and not spec.limit:
            spec.limit = int(self.g.defaults.get("top_n", 10))
            assumptions.append(f"Showing top {spec.limit}.")
        if spec.metrics and any(m.endswith("_usd") for m in spec.metrics):
            assumptions.append("Amounts converted from BRL to USD at the daily rate of the order date.")

        spec.ambiguities = ambiguities
        spec.assumptions = list(dict.fromkeys(assumptions))
        return spec

    @staticmethod
    def blocking(spec: QualifiedRequest) -> list[Ambiguity]:
        return [a for a in spec.ambiguities if a.blocking]
