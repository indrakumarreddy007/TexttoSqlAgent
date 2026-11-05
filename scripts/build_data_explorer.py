#!/usr/bin/env python3
"""Generate docs/data-explorer.html: raw Kaggle files, warehouse tables, certified queries,
few-shot examples, roles and the eval set (with each gold query's real result).

    python scripts/build_data_explorer.py

Reads data/raw, data/warehouse.duckdb, config/*.yaml and evals/datasets/dev.yaml. Direct-identifier
columns (names, emails, phones, tax ids) are synthetic, but are still hidden in the samples.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sqlagent.metadata import Catalog, Glossary, Policy  # noqa: E402

LINEAGE = {
    "olist_orders_dataset.csv": ["sales.ord_hdr"],
    "olist_order_items_dataset.csv": ["sales.ord_line"],
    "olist_order_payments_dataset.csv": ["sales.pmt"],
    "olist_customers_dataset.csv": ["crm.CUST_MSTR_V2", "crm.cust_mstr"],
    "olist_products_dataset.csv": ["product.prod"],
    "product_category_name_translation.csv": ["product.cat_xlate"],
    "olist_sellers_dataset.csv": ["ops.seller"],
    "olist_geolocation_dataset.csv": ["ops.geo"],
    "olist_order_reviews_dataset.csv": ["cx.review"],
    "WA_Fn-UseC_-HR-Employee-Attrition.csv": ["hr.employee", "hr.pay"],
}
KAGGLE = {
    "olist": ("Olist Brazilian E-Commerce", "https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce"),
    "hr": ("IBM HR Analytics Employee Attrition",
           "https://www.kaggle.com/datasets/pavansubhasht/ibm-hr-analytics-attrition-dataset"),
}


def j(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")[:19]
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, str) and len(v) > 60:
        return v[:57] + "..."
    return v


def collect() -> dict:
    con = duckdb.connect(str(ROOT / "data" / "warehouse.duckdb"), read_only=True)
    catalog = Catalog.load(ROOT / "config" / "catalog.yaml", con)
    glossary = Glossary.load(ROOT / "config" / "glossary.yaml")
    policy = Policy.load(ROOT / "config" / "policy.yaml")
    info = dict(zip(["as_of_date", "data_source", "fx_source", "built_at"],
                    con.execute("SELECT * FROM meta.build_info").fetchone(), strict=True))

    raw = []
    for folder in ("olist", "hr"):
        for f in sorted((ROOT / "data" / "raw" / folder).glob("*.csv")):
            rel = con.execute("SELECT * FROM read_csv(?, header=true, all_varchar=true) LIMIT 3", [str(f)])
            cols = [d[0].replace("﻿", "") for d in rel.description]
            sample = [[j(v) for v in r] for r in rel.fetchall()]
            n = con.execute("SELECT count(*) FROM read_csv(?, header=true, all_varchar=true)", [str(f)]).fetchone()[0]
            if folder == "hr":  # wide file: show the first 12 columns
                cols, sample = cols[:12] + ["…"], [r[:12] + ["…"] for r in sample]
            raw.append({"dataset": KAGGLE[folder][0], "url": KAGGLE[folder][1], "file": f.name, "rows": n,
                        "bytes": f.stat().st_size, "columns": cols, "sample": sample,
                        "targets": LINEAGE.get(f.name, [])})

    tables = []
    for t in catalog.tables.values():
        rel = con.execute(f'SELECT * FROM {t.schema}."{t.name}" LIMIT 5')
        names = [d[0] for d in rel.description]
        hide = {i for i, n in enumerate(names) if (c := t.column(n)) and c.sensitivity == "pii_direct"}
        sample = [["(hidden)" if i in hide else j(v) for i, v in enumerate(r)] for r in rel.fetchall()]
        n = con.execute(f'SELECT count(*) FROM {t.schema}."{t.name}"').fetchone()[0]
        tables.append({
            "fqn": t.fqn, "schema": t.schema, "description": t.description, "owner": t.owner,
            "size_class": t.size_class, "rows": n, "deprecated": t.deprecated, "replaced_by": t.replaced_by,
            "default_filter": t.default_filter, "required_filter_any": list(t.required_filter_any),
            "synonyms": list(t.synonyms),
            "columns": [{"name": c.name, "type": c.type, "sensitivity": c.sensitivity,
                         "description": c.description} for c in t.columns.values()],
            "joins": [f"{j_.left_col} = {j_.to}.{j_.right_col} ({j_.cardinality})" for j_ in t.joins],
            "sample_columns": names, "sample": sample,
            "sources": [f for f, ts in LINEAGE.items() if t.fqn in ts],
        })

    metrics = [{"id": m.id, "label": m.label, "description": m.description, "owner": m.owner,
                "synonyms": list(m.synonyms),
                "sql": f"SELECT {m.sql}\nFROM {m.from_sql}" + (f"\nWHERE {m.where}" if m.where else "")}
               for m in glossary.metrics.values()]
    examples = yaml.safe_load((ROOT / "config" / "examples.yaml").read_text())["examples"]
    roles = [{"name": r.name, "description": r.description, "schemas": list(r.schemas),
              "sensitivity": r.sensitivity, "row_filters": list(r.row_filters),
              "aggregate_only": list(r.aggregate_only), "max_rows": r.max_rows} for r in policy.roles.values()]

    oracle = {}
    rep = ROOT / "evals" / "reports" / "last-oracle.json"
    if rep.exists():
        oracle = {r["id"]: r["pass"] for r in json.loads(rep.read_text())["results"]}
    cases = []
    for c in yaml.safe_load((ROOT / "evals" / "datasets" / "dev.yaml").read_text())["cases"]:
        g = None
        if c.get("gold_sql"):
            rel = con.execute(c["gold_sql"])
            cols = [d[0] for d in rel.description]
            rows = rel.fetchall()
            g = {"columns": cols, "rows": [[j(v) for v in r] for r in rows[:8]], "row_count": len(rows)}
        cases.append({"id": c["id"], "category": c["category"], "user": c["user"], "question": c["question"],
                      "expect": c["expect"], "clarify_slots": c.get("clarify_slots", []),
                      "follow_up": c.get("follow_up"), "note": c.get("note"), "ordered": c.get("compare", {}).get(
                          "ordered", False), "gold_sql": (c.get("gold_sql") or "").strip(), "gold": g,
                      "oracle_pass": oracle.get(c["id"])})
    con.close()
    return {"info": info, "raw": raw, "tables": tables, "metrics": metrics, "examples": examples,
            "roles": roles, "cases": cases}


TEMPLATE = (Path(__file__).parent / "templates" / "data_explorer.html").read_text()


def main() -> None:
    data = collect()
    payload = json.dumps(data, default=str, separators=(",", ":")).replace("</", "<\\/")
    out = ROOT / "docs" / "data-explorer.html"
    out.write_text(TEMPLATE.replace("/*__DATA__*/null", payload))
    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
