"""Metadata consistency: catalog <-> warehouse, glossary metrics execute and pass guardrails."""
import duckdb
import pytest

from sqlagent.metadata import SENSITIVITY_CLASSES


def test_catalog_documents_every_warehouse_table(catalog, warehouse):
    con = duckdb.connect(str(warehouse), read_only=True)
    wh = {f"{s}.{t}".lower() for s, t in con.execute(
        "SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema <> 'meta'").fetchall()}
    con.close()
    assert wh == set(catalog.tables), f"undocumented: {wh - set(catalog.tables)}"


def test_every_column_has_valid_sensitivity_and_description(catalog):
    for t in catalog.tables.values():
        assert t.description and t.owner, t.fqn
        for c in t.columns.values():
            assert c.sensitivity in SENSITIVITY_CLASSES
            assert c.description, f"{t.fqn}.{c.name}"


def test_join_keys_exist(catalog):
    for t in catalog.tables.values():
        for j in t.joins:
            other = catalog.get(j.to)
            assert other is not None, f"{t.fqn} -> {j.to}"
            assert t.column(j.left_col) and other.column(j.right_col), f"{t.fqn} join {j}"


def test_deprecated_tables_name_a_replacement(catalog):
    for t in catalog.tables.values():
        if t.deprecated:
            assert catalog.get(t.replaced_by) is not None


def _metric_sql(m):
    sql = f"SELECT {m.sql} AS v FROM {m.from_sql}"
    return sql + (f" WHERE {m.where}" if m.where else "")


def test_glossary_metrics_execute(glossary, executor):
    for m in glossary.metrics.values():
        res = executor.run(_metric_sql(m), 5, 30)
        assert hasattr(res, "rows"), f"{m.id}: {res}"
        assert res.rows[0][0] is not None, m.id


@pytest.mark.parametrize("metric,role", [
    ("net_revenue_usd", "analyst"), ("gmv_usd", "exec"), ("order_count", "regional_mgr"),
    ("active_customers", "analyst"), ("avg_review_score", "regional_mgr"), ("late_delivery_rate", "analyst"),
    ("headcount", "hr_analyst"), ("attrition_rate", "hr_analyst"), ("avg_monthly_income", "hr_analyst"),
])
def test_certified_metrics_pass_guardrails_for_intended_roles(glossary, engine, users, metric, role):
    user = {"analyst": users["analyst"], "exec": users["exec"], "regional_mgr": users["mgr_se"],
            "hr_analyst": users["hr"]}[role]
    r = engine.check(_metric_sql(glossary.metrics[metric]), user)
    assert r.allowed, r.violations
