"""Catalog, glossary and policy loading.

Semantics come from YAML; column types come from the warehouse, so the two can be
checked against each other (tests/test_catalog.py). All lookups are case-insensitive,
matching DuckDB identifier semantics.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import yaml

SENSITIVITY_CLASSES = {"public", "internal", "pii_direct", "pii_sensitive", "free_text"}


@dataclass(frozen=True)
class Column:
    name: str            # as declared (original case)
    type: str
    description: str
    sensitivity: str
    synonyms: tuple[str, ...] = ()


@dataclass(frozen=True)
class Join:
    to: str
    left_col: str
    right_col: str
    cardinality: str
    note: str | None = None


@dataclass
class Table:
    fqn: str                              # e.g. crm.CUST_MSTR_V2 (original case)
    schema: str
    name: str
    description: str
    owner: str
    size_class: str
    synonyms: tuple[str, ...]
    columns: dict[str, Column]            # key: lowercase column name
    joins: list[Join] = field(default_factory=list)
    default_filter: str | None = None
    required_filter_any: tuple[str, ...] = ()
    deprecated: bool = False
    replaced_by: str | None = None

    @property
    def key(self) -> str:
        return self.fqn.lower()

    def column(self, name: str) -> Column | None:
        return self.columns.get(name.lower())


class Catalog:
    def __init__(self, tables: dict[str, Table]):
        self.tables = tables  # key: lowercase fqn

    @classmethod
    def load(cls, path: Path, con: duckdb.DuckDBPyConnection) -> Catalog:
        doc = yaml.safe_load(path.read_text())
        types: dict[str, dict[str, str]] = {}
        for schema, table, col, dtype in con.execute(
            "SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns "
            "ORDER BY table_schema, table_name, ordinal_position"
        ).fetchall():
            types.setdefault(f"{schema}.{table}".lower(), {})[col.lower()] = dtype

        tables: dict[str, Table] = {}
        for fqn, spec in doc["tables"].items():
            schema, name = fqn.split(".", 1)
            wh_cols = types.get(fqn.lower())
            if wh_cols is None:
                raise ValueError(f"catalog table {fqn} not found in warehouse")
            cols: dict[str, Column] = {}
            for cname, cspec in spec["columns"].items():
                sens = cspec.get("sensitivity", "internal")
                if sens not in SENSITIVITY_CLASSES:
                    raise ValueError(f"{fqn}.{cname}: unknown sensitivity {sens}")
                if cname.lower() not in wh_cols:
                    raise ValueError(f"catalog column {fqn}.{cname} not found in warehouse")
                cols[cname.lower()] = Column(cname, wh_cols[cname.lower()], cspec.get("description", ""), sens,
                                             tuple(cspec.get("synonyms", [])))
            missing = set(wh_cols) - set(cols)
            if missing:
                raise ValueError(f"warehouse columns not documented in catalog for {fqn}: {sorted(missing)}")
            joins = [Join(j["to"], j["keys"][0], j["keys"][1], j.get("cardinality", "many_to_one"), j.get("note"))
                     for j in spec.get("joins", [])]
            tables[fqn.lower()] = Table(
                fqn=fqn, schema=schema, name=name, description=spec.get("description", ""),
                owner=spec.get("owner", ""), size_class=spec.get("size_class", "small"),
                synonyms=tuple(spec.get("synonyms", [])), columns=cols, joins=joins,
                default_filter=spec.get("default_filter"),
                required_filter_any=tuple(c.lower() for c in spec.get("required_filter_any", [])),
                deprecated=bool(spec.get("deprecated", False)), replaced_by=spec.get("replaced_by"),
            )
        return cls(tables)

    def get(self, fqn: str) -> Table | None:
        return self.tables.get(fqn.lower())

    def sqlglot_schema(self, exclude: set[tuple[str, str]] | None = None,
                       tables: set[str] | None = None) -> dict[str, dict[str, dict[str, str]]]:
        """{schema: {table: {column: type}}}, lower-cased; optionally excluding (table, column) pairs."""
        exclude = exclude or set()
        out: dict[str, dict[str, dict[str, str]]] = {}
        for key, t in self.tables.items():
            if tables is not None and key not in tables:
                continue
            cols = {c: col.type for c, col in t.columns.items() if (key, c) not in exclude}
            out.setdefault(t.schema.lower(), {})[t.name.lower()] = cols
        return out


# --------------------------------------------------------------------------- glossary
@dataclass(frozen=True)
class Metric:
    id: str
    label: str
    description: str
    owner: str
    synonyms: tuple[str, ...]
    sql: str
    from_sql: str
    where: str | None
    time_column: str | None


@dataclass
class Glossary:
    defaults: dict[str, Any]
    metrics: dict[str, Metric]
    ambiguous_terms: dict[str, dict[str, Any]]
    time_expressions: list[dict[str, str]]

    @classmethod
    def load(cls, path: Path) -> Glossary:
        doc = yaml.safe_load(path.read_text())
        metrics = {
            mid: Metric(mid, m["label"], m["description"], m.get("owner", ""), tuple(m.get("synonyms", [])),
                        m["sql"], " ".join(m["from"].split()), m.get("where"), m.get("time_column"))
            for mid, m in doc["metrics"].items()
        }
        for term, spec in doc.get("ambiguous_terms", {}).items():
            for opt in spec["options"]:
                if opt not in metrics:
                    raise ValueError(f"ambiguous term {term!r} references unknown metric {opt!r}")
        return cls(doc["defaults"], metrics, doc.get("ambiguous_terms", {}), doc.get("time_expressions", []))


# --------------------------------------------------------------------------- policy
@dataclass(frozen=True)
class Role:
    name: str
    description: str
    schemas: tuple[str, ...]
    sensitivity: dict[str, str]
    row_filters: dict[str, str]          # key: lowercase fqn
    aggregate_only: dict[str, dict[str, Any]]  # key: lowercase fqn
    max_rows: int
    timeout_s: int

    def can_access_schema(self, schema: str) -> bool:
        return "*" in self.schemas or schema.lower() in {s.lower() for s in self.schemas}

    def action_for(self, sensitivity: str) -> str:
        if sensitivity in ("public", "internal"):
            return "allow"
        return self.sensitivity.get(sensitivity, "deny")  # fail closed


@dataclass
class Policy:
    allowed_roots: tuple[str, ...]
    denied_functions: frozenset[str]
    masking: dict[str, str]
    roles: dict[str, Role]
    max_attempts: int

    @classmethod
    def load(cls, path: Path) -> Policy:
        doc = yaml.safe_load(path.read_text())
        roles = {}
        for name, r in doc["roles"].items():
            roles[name] = Role(
                name=name, description=r.get("description", ""), schemas=tuple(r["schemas"]),
                sensitivity=r.get("sensitivity", {}),
                row_filters={k.lower(): " ".join(v.split()) for k, v in r.get("row_filters", {}).items()},
                aggregate_only={k.lower(): v for k, v in r.get("aggregate_only", {}).items()},
                max_rows=int(r.get("max_rows", 1000)), timeout_s=int(r.get("timeout_s", 30)),
            )
        st = doc["statement"]
        return cls(tuple(st["allowed_roots"]), frozenset(f.lower() for f in st["denied_functions"]),
                   doc.get("masking", {}), roles, int(st.get("max_attempts", 3)))

    def role(self, name: str) -> Role:
        if name not in self.roles:
            raise KeyError(f"unknown role {name!r}")
        return self.roles[name]
