"""Sandboxed, read-only query execution with timeout, row cap and an EXPLAIN-based cost check."""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from sqlagent.core.schemas import Violation
from sqlagent.guardrails.codes import violation

_EC_RE = re.compile(r"~\s*([\d,]+)\s+rows?|EC:\s*([\d,]+)", re.IGNORECASE)


@dataclass
class ExecResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    elapsed_ms: int


class Executor:
    """One read-only connection per call: cheap in DuckDB and avoids cross-request state."""

    def __init__(self, db_path: Path):
        self.db_path = str(db_path)

    def _connect(self) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect(self.db_path, read_only=True)
        con.execute("SET enable_external_access = false")
        con.execute("SET lock_configuration = true")
        return con

    def estimate_rows(self, sql: str) -> int:
        con = self._connect()
        try:
            plan = "\n".join(str(r[-1]) for r in con.execute(f"EXPLAIN {sql}").fetchall())
        finally:
            con.close()
        nums = [int((a or b).replace(",", "")) for a, b in _EC_RE.findall(plan)]
        return max(nums) if nums else 0

    def cost_check(self, sql: str, max_rows_estimate: int) -> list[Violation]:
        try:
            est = self.estimate_rows(sql)
        except duckdb.Error as e:
            return [violation("V060", f"EXPLAIN failed: {str(e).splitlines()[0][:300]}")]
        if est > max_rows_estimate:
            return [violation("V050", f"estimated {est:,} intermediate rows exceeds budget {max_rows_estimate:,}",
                              "Add selective filters (date range, state) or aggregate earlier.")]
        return []

    def run(self, sql: str, max_rows: int, timeout_s: float) -> ExecResult | Violation:
        con = self._connect()
        timer = threading.Timer(timeout_s, con.interrupt)
        t0 = time.perf_counter()
        timer.start()
        try:
            cur = con.execute(sql)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(max_rows + 1)
        except duckdb.InterruptException:
            return violation("V061", f"query exceeded {timeout_s}s", "Simplify the query or add filters.")
        except duckdb.Error as e:
            return violation("V060", str(e).splitlines()[0][:400])
        finally:
            timer.cancel()
            con.close()
        truncated = len(rows) > max_rows
        return ExecResult(cols, [list(r) for r in rows[:max_rows]], truncated,
                          int((time.perf_counter() - t0) * 1000))
