"""Stable violation codes. `correctable` decides whether the agent may retry
(feed the violation back to the SQL generator) or must refuse."""
from __future__ import annotations

from sqlagent.core.schemas import Violation

CODES: dict[str, tuple[str, bool]] = {
    # statement safety
    "V001": ("PARSE_ERROR", True),
    "V002": ("MULTIPLE_STATEMENTS", False),
    "V003": ("NOT_READ_ONLY", False),
    "V004": ("FORBIDDEN_FUNCTION", False),
    # object access
    "V010": ("UNKNOWN_TABLE", True),
    "V011": ("TABLE_NOT_PERMITTED", False),
    "V012": ("DEPRECATED_TABLE", True),
    "V013": ("UNQUALIFIED_TABLE", True),
    "V014": ("CROSS_DATABASE", False),
    "V020": ("UNKNOWN_COLUMN", True),
    "V021": ("COLUMN_NOT_PERMITTED", False),
    "V022": ("UNRESOLVED_REFERENCE", True),
    # semantic hygiene / cost
    "V030": ("MISSING_REQUIRED_FILTER", True),
    "V032": ("CARTESIAN_JOIN", True),
    # privacy
    "V040": ("AGGREGATE_ONLY", False),
    "V041": ("AGGREGATE_SCOPE", True),
    # runtime
    "V050": ("COST_BUDGET", True),
    "V060": ("EXECUTION_ERROR", True),
    "V061": ("TIMEOUT", True),
    # spec conformance (does the SQL answer the qualified request?)
    "V070": ("MISSING_GROUPING", True),
    "V071": ("MISSING_RANKING", True),
    "V072": ("WRONG_TIME_WINDOW", True),
    "V073": ("MISMATCHED_JOIN_KEYS", True),
    "V074": ("METRIC_DEFINITION_NOT_USED", True),
    "V075": ("WRONG_GRAIN", True),
}


def violation(code: str, message: str, hint: str | None = None, *, table: str | None = None,
              column: str | None = None) -> Violation:
    name, correctable = CODES[code]
    return Violation(code=code, message=f"{name}: {message}", correctable=correctable, hint=hint,
                     table=table, column=column)
