"""Execution-level invariants: run the ENFORCED SQL and check what actually comes back."""
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

C = 'crm."CUST_MSTR_V2"'
SE_STATES = {"SP", "RJ", "MG", "ES"}


def run(engine, executor, sql, user, rows=10_000):
    r = engine.check(sql, user)
    assert r.allowed, r.violations
    res = executor.run(r.enforced_sql, rows, 30)
    assert hasattr(res, "rows"), res
    return r, res


def test_row_level_security_limits_to_caller_regions(engine, executor, users):
    _, res = run(engine, executor, f"SELECT DISTINCT c.cust_state FROM {C} c WHERE c.is_current", users["mgr_se"])
    assert {r[0] for r in res.rows} <= SE_STATES and res.rows


def test_rls_applies_to_fact_tables_without_customer_join(engine, executor, users):
    sql = "SELECT COUNT(DISTINCT h.ord_id) FROM sales.ord_hdr h"
    _, mgr = run(engine, executor, sql, users["mgr_se"])
    _, adm = run(engine, executor, sql, users["admin"])
    assert 0 < mgr.rows[0][0] < adm.rows[0][0]


def test_rls_cannot_be_bypassed_with_union_or_subquery(engine, executor, users):
    sql = f"""SELECT s FROM (SELECT c.cust_state AS s FROM {C} c WHERE c.is_current
              UNION SELECT c2.cust_state FROM {C} c2 WHERE c2.valid_to > DATE '2000-01-01') u"""
    _, res = run(engine, executor, sql, users["mgr_se"])
    assert {r[0] for r in res.rows} <= SE_STATES


def test_manager_without_regions_sees_nothing(engine, executor, users):
    _, res = run(engine, executor, "SELECT COUNT(*) FROM sales.ord_hdr h", users["mgr_none"])
    assert res.rows[0][0] == 0


def test_soft_deleted_orders_are_always_excluded(engine, executor, users, warehouse):
    _, res = run(engine, executor, "SELECT COUNT(*) FROM sales.ord_hdr h WHERE h.is_deleted", users["admin"])
    assert res.rows[0][0] == 0


def test_masking_for_exec(engine, executor, users):
    r, res = run(engine, executor, f"SELECT c.cust_nm, c.email, c.cust_city FROM {C} c WHERE c.is_current",
                 users["exec"], rows=20)
    assert all(row[0] == "***" and row[1] == "***" for row in res.rows)
    assert all(row[2] != "***" for row in res.rows)
    assert "crm.CUST_MSTR_V2.email" in r.masked_columns


def test_star_never_expands_to_denied_columns(engine, executor, users):
    _, res = run(engine, executor, f"SELECT * FROM {C} c WHERE c.is_current", users["analyst"], rows=3)
    assert not {"cpf", "email", "phone", "cust_nm"} & {c.lower() for c in res.columns}


def test_k_anonymity_suppresses_small_groups(engine, executor, users):
    # grouping by employee gives groups of 1: every row must be suppressed
    _, res = run(engine, executor, "SELECT p.emp_id, AVG(p.monthly_income) FROM hr.pay p GROUP BY 1", users["hr"])
    assert res.rows == []
    _, res = run(engine, executor, """SELECT e.job_role, AVG(p.monthly_income), COUNT(*) FROM hr.employee e
                                      JOIN hr.pay p ON p.emp_id = e.emp_id GROUP BY 1""", users["hr"])
    assert res.rows and all(r[2] >= 5 for r in res.rows)


def test_limit_is_capped(engine, executor, users):
    r = engine.check("SELECT h.ord_id FROM sales.ord_hdr h LIMIT 999999", users["analyst"])
    assert r.allowed and r.enforced_sql.rstrip().endswith("LIMIT 1000")
    r = engine.check("SELECT h.ord_id FROM sales.ord_hdr h LIMIT 10", users["analyst"])
    assert r.enforced_sql.rstrip().endswith("LIMIT 10")


def test_executor_is_read_only(executor):
    res = executor.run("CREATE TABLE ref.x AS SELECT 1", 1, 5)
    assert getattr(res, "code", None) == "V060"


DIMENSIONS = ["c.cust_state", "c.cust_city", "h.ord_stat_cd", "CAST(h.purch_ts AS DATE)", "r.region_cd"]


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(dims=st.lists(st.sampled_from(DIMENSIONS), min_size=1, max_size=3, unique=True),
       use_cte=st.booleans())
def test_property_regional_manager_never_sees_other_regions(engine, executor, users, dims, use_cte):
    select = ", ".join(dims)
    core = (f"SELECT r.region_cd AS rg, {select}, COUNT(*) AS n FROM sales.ord_hdr h "
            f"JOIN {C} c ON c.cust_id = h.cust_id AND c.is_current "
            f"JOIN ref.region r ON r.state_cd = c.cust_state GROUP BY ALL")
    sql = f"WITH q AS ({core}) SELECT q.rg FROM q" if use_cte else core
    r = engine.check(sql, users["mgr_se"])
    assert r.allowed, r.violations
    res = executor.run(r.enforced_sql, 100_000, 30)
    assert {row[0] for row in res.rows} <= {"SE"}
