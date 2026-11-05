"""Schema linking: pick the tables, columns, join paths and certified metrics relevant to a
question, restricted to what the caller's role may see.

Hybrid and deterministic: BM25 over catalog text + synonyms, glossary phrase matching, then
breadth-first join-path expansion over the catalog's foreign-key graph so the selected
tables are connected. Embeddings can be added behind the same interface later.
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field

import sqlglot
from rank_bm25 import BM25Okapi
from sqlglot import exp

from sqlagent.metadata import Catalog, Glossary, Metric, Role, Table

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "of", "by", "for", "in", "on", "and", "or", "to", "what", "which", "how", "is", "are",
         "was", "were", "me", "show", "give", "list", "our", "my", "we", "per", "with", "from", "last", "this",
         "all", "do", "does", "did", "that", "it", "be", "as", "at", "id"}


def tokenize(text: str) -> list[str]:
    toks = []
    for t in _TOKEN_RE.findall(text.lower().replace("_", " ")):
        if t in _STOP:
            continue
        toks.append(t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t)
    return toks


def _phrase_in(phrase: str, text: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase.lower())}(?![a-z0-9])", text.lower()) is not None


@dataclass
class SchemaContext:
    tables: list[str]
    metrics: list[str]
    ddl: str
    joins: str
    metric_defs: str
    notes: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)


class Retriever:
    def __init__(self, catalog: Catalog, glossary: Glossary):
        self.catalog = catalog
        self.glossary = glossary
        self._metric_tables = {m.id: self._tables_in(m.from_sql) for m in glossary.metrics.values()}
        self._graph: dict[str, set[str]] = {}
        for t in catalog.tables.values():
            for j in t.joins:
                self._graph.setdefault(t.key, set()).add(j.to.lower())
                self._graph.setdefault(j.to.lower(), set()).add(t.key)

    @staticmethod
    def _tables_in(from_sql: str) -> set[str]:
        tree = sqlglot.parse_one(f"SELECT 1 FROM {from_sql}", read="duckdb")
        return {f"{t.db}.{t.name}".lower() for t in tree.find_all(exp.Table) if t.db}

    def visible_tables(self, role: Role) -> list[Table]:
        return [t for t in self.catalog.tables.values() if role.can_access_schema(t.schema) and not t.deprecated]

    def match_metrics(self, text: str) -> list[str]:
        hits = []
        for m in self.glossary.metrics.values():
            names = [m.id.replace("_", " "), m.label.lower(), *m.synonyms]
            if any(_phrase_in(n, text) for n in names):
                hits.append(m.id)
        return hits

    def retrieve(self, question: str, role: Role, top_k: int = 6, extra_metrics: list[str] | None = None
                 ) -> SchemaContext:
        visible = self.visible_tables(role)
        vis_keys = {t.key for t in visible}
        docs = []
        for t in visible:
            parts = [t.fqn, t.name, t.description, " ".join(t.synonyms) * 2]
            for c in t.columns.values():
                if role.action_for(c.sensitivity) != "deny":
                    parts += [c.name, c.description, " ".join(c.synonyms)]
            docs.append(tokenize(" ".join(parts)))
        bm25 = BM25Okapi(docs)
        scores = bm25.get_scores(tokenize(question))
        ranked = sorted(zip(visible, scores, strict=True), key=lambda x: -x[1])
        chosen = [t.key for t, s in ranked[:top_k] if s > 0]

        metrics = list(dict.fromkeys(self.match_metrics(question) + (extra_metrics or [])))
        metrics = [m for m in metrics if self._metric_tables[m] <= vis_keys]
        for m in metrics:
            chosen += [k for k in self._metric_tables[m] if k not in chosen]
        if any(w in question.lower() for w in ("quarter", "fiscal", "fy", "year", "month", "ytd")):
            if "finance.fiscal_cal" in vis_keys and "finance.fiscal_cal" not in chosen:
                chosen.append("finance.fiscal_cal")
        chosen = self._connect(list(dict.fromkeys(chosen)), vis_keys)
        return self._render(chosen, metrics, role, {t.key: float(s) for t, s in ranked})

    def _connect(self, chosen: list[str], allowed: set[str]) -> list[str]:
        """Add bridge tables so every chosen table is reachable from the first one."""
        if len(chosen) < 2:
            return chosen
        result = list(chosen)
        anchor = chosen[0]
        for target in chosen[1:]:
            path = self._shortest_path(anchor, target, allowed)
            for node in path or []:
                if node not in result:
                    result.append(node)
        return result

    def _shortest_path(self, src: str, dst: str, allowed: set[str]) -> list[str] | None:
        q, prev = deque([src]), {src: None}
        while q:
            cur = q.popleft()
            if cur == dst:
                path = []
                while cur is not None:
                    path.append(cur)
                    cur = prev[cur]
                return path[::-1]
            for nxt in self._graph.get(cur, ()):
                if nxt in allowed and nxt not in prev:
                    prev[nxt] = cur
                    q.append(nxt)
        return None

    def _render(self, keys: list[str], metrics: list[str], role: Role, scores: dict[str, float]) -> SchemaContext:
        ddl_parts, join_lines, notes = [], [], []
        key_set = set(keys)
        for k in keys:
            t = self.catalog.get(k)
            lines = [f"TABLE {t.fqn}  -- {t.description}"]
            for c in t.columns.values():
                action = role.action_for(c.sensitivity)
                if action == "deny":
                    continue
                tag = " [MASKED for your role]" if action == "mask" else ""
                syn = f" (aka {', '.join(c.synonyms)})" if c.synonyms else ""
                lines.append(f"  {c.name} {c.type}  -- {c.description}{syn}{tag}")
            ddl_parts.append("\n".join(lines))
            for j in t.joins:
                if j.to.lower() in key_set:
                    other = self.catalog.get(j.to)
                    note = f"  -- {j.note}" if j.note else ""
                    join_lines.append(f"{t.fqn}.{j.left_col} = {other.fqn}.{j.right_col} ({j.cardinality}){note}")
            if t.required_filter_any:
                notes.append(f"{t.fqn}: always filter on one of {list(t.required_filter_any)}")
            if t.default_filter:
                notes.append(f"{t.fqn}: '{t.default_filter}' is applied automatically by policy")
        metric_defs = "\n".join(self._metric_text(self.glossary.metrics[m]) for m in metrics)
        return SchemaContext(keys, metrics, "\n\n".join(ddl_parts), "\n".join(join_lines), metric_defs, notes,
                             {k: scores.get(k, 0.0) for k in keys})

    @staticmethod
    def _metric_text(m: Metric) -> str:
        where = f"\n  WHERE {m.where}" if m.where else ""
        return f"METRIC {m.id} ({m.label}): {m.description}\n  SELECT {m.sql}\n  FROM {m.from_sql}{where}"

    def metric_catalog_text(self, role: Role) -> str:
        vis = {t.key for t in self.visible_tables(role)}
        return "\n".join(f"- {m.id}: {m.label}. {m.description} (synonyms: {', '.join(m.synonyms)})"
                         for m in self.glossary.metrics.values() if self._metric_tables[m.id] <= vis)
