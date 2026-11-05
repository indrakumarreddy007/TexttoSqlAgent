"""Hostile and tricky SQL. Every case must be refused with the expected code, or allowed.
This suite is a hard CI gate: a failure here blocks release."""
import pytest

C = 'crm."CUST_MSTR_V2"'

CORPUS = [
    # ---- statement safety (non-correctable)
    ("analyst", "SELECT 1; DROP TABLE sales.ord_hdr", "V002"),
    ("analyst", "SELECT 1; SELECT 2", "V002"),
    ("analyst", "DROP TABLE sales.ord_hdr", "V003"),
    ("analyst", "DELETE FROM sales.ord_hdr", "V003"),
    ("analyst", "UPDATE sales.ord_hdr SET is_deleted = TRUE", "V003"),
    ("analyst", "INSERT INTO ref.region VALUES ('XX','x','N','North')", "V003"),
    ("analyst", "CREATE TABLE x AS SELECT * FROM ref.region", "V003"),
    ("analyst", "PRAGMA show_tables", "V003"),
    ("analyst", "ATTACH 'other.db' AS o", "V003"),
    ("analyst", "COPY ref.region TO '/tmp/x.csv'", "V003"),
    ("analyst", "SET enable_external_access = true", "V003"),
    ("analyst", "SELECT * INTO ref.copy FROM ref.region", "V003"),
    ("analyst", "/* harmless */ DELETE FROM ref.region -- SELECT", "V003"),
    ("analyst", "WITH x AS (SELECT 1) DELETE FROM ref.region", "V003"),
    ("analyst", "SELECT * FROM read_csv('/etc/passwd')", "V004"),
    ("analyst", "SELECT * FROM read_text('/etc/passwd')", "V004"),
    ("analyst", "SELECT * FROM 'data/raw/olist/olist_orders_dataset.csv'", "V004"),
    ("analyst", "SELECT * FROM 'https://example.com/x.parquet'", "V004"),
    ("analyst", "SELECT * FROM duckdb_settings()", "V004"),
    ("analyst", "SELECT getenv('HOME')", "V004"),
    ("analyst", "SELECT current_setting('threads')", "V004"),
    ("analyst", f"SELECT COLUMNS('c.*') FROM {C} WHERE is_current", "V004"),
    ("analyst", "SELECT * FROM glob('/home/*')", "V004"),
    # ---- object access
    ("analyst", "SELECT * FROM hr.pay", "V011"),
    ("regional_mgr", "SELECT e.emp_id FROM hr.employee e", "V011"),
    ("hr_analyst", "SELECT h.ord_id FROM sales.ord_hdr h", "V011"),
    ("analyst", "SELECT * FROM other_db.sales.ord_hdr", "V014"),
    ("analyst", "SELECT * FROM ord_hdr", "V013"),
    ("analyst", "SELECT * FROM sales.orders", "V010"),
    ("analyst", "SELECT * FROM information_schema.tables", "V010"),
    ("analyst", "SELECT * FROM crm.cust_mstr", "V012"),
    ("analyst", 'SELECT * FROM "CRM"."CUST_MSTR"', "V012"),
    # ---- column access
    ("analyst", f"SELECT c.cpf FROM {C} c WHERE c.is_current", "V021"),
    ("analyst", f"SELECT c.cust_id FROM {C} c WHERE c.is_current AND c.email LIKE '%gmail%'", "V021"),
    ("regional_mgr", f'SELECT c."CPF" FROM {C} c WHERE c.is_current', "V021"),
    ("analyst", f"SELECT x.cpf FROM (SELECT * FROM {C} WHERE is_current) x", "V021"),
    ("analyst", "SELECT r.comment_txt FROM cx.review r", "V021"),
    ("hr_analyst", "SELECT e.emp_id, e.perf_rating FROM hr.employee e", "allowed"),
    ("analyst", f"SELECT c FROM {C} c WHERE c.is_current", "V022"),
    ("analyst", "SELECT h.ord_idd FROM sales.ord_hdr h", "V020"),
    # ---- semantic hygiene
    ("analyst", f"SELECT count(*) FROM {C}", "V030"),
    ("analyst", f"SELECT c.cust_state FROM {C} c WHERE c.cust_state = 'SP'", "V030"),
    ("analyst", "SELECT g.lat FROM ops.geo g", "V030"),
    ("analyst", "SELECT h.ord_id FROM sales.ord_hdr h, sales.ord_line l", "V032"),
    ("analyst", "SELECT h.ord_id FROM sales.ord_hdr h CROSS JOIN sales.ord_line l", "V032"),
    # ---- privacy: aggregate-only HR pay
    ("hr_analyst", "SELECT p.monthly_income FROM hr.pay p", "V040"),
    ("hr_analyst", "SELECT p.emp_id, p.monthly_income FROM hr.pay p ORDER BY 2 DESC", "V040"),
    ("hr_analyst", "SELECT p.emp_id, AVG(p.monthly_income) OVER () FROM hr.pay p", "V040"),
    ("hr_analyst", "SELECT p.monthly_income, COUNT(*) FROM hr.pay p GROUP BY p.monthly_income", "V040"),
    ("hr_analyst", "WITH x AS (SELECT * FROM hr.pay) SELECT AVG(x.monthly_income) FROM x", "V041"),
    ("hr_analyst", "SELECT AVG(s.m) FROM (SELECT p.monthly_income AS m FROM hr.pay p) s", "V041"),
    # ---- legitimate queries must pass
    ("analyst", "SELECT r.region_nm, COUNT(*) AS n FROM ref.region r GROUP BY 1", "allowed"),
    ("analyst", f"SELECT c.cust_state, COUNT(DISTINCT c.cust_uid) FROM {C} c WHERE c.is_current GROUP BY 1",
     "allowed"),
    ("analyst", "SELECT g.geo_city, AVG(g.lat) FROM ops.geo g WHERE g.geo_state = 'SP' GROUP BY 1", "allowed"),
    ("analyst", "WITH m AS (SELECT h.ord_id FROM sales.ord_hdr h) SELECT COUNT(*) FROM m", "allowed"),
    ("analyst", "SELECT 1 AS a UNION ALL SELECT 2", "allowed"),
    ("exec", f"SELECT c.cust_nm, c.cust_state FROM {C} c WHERE c.is_current", "allowed"),
    ("hr_analyst", ("SELECT e.dept_nm, AVG(p.monthly_income) FROM hr.employee e JOIN hr.pay p "
                    "ON p.emp_id = e.emp_id GROUP BY 1"), "allowed"),
    ("hr_analyst", "SELECT AVG(p.monthly_income) FROM hr.pay p WHERE p.monthly_income > 5000", "allowed"),
    ("admin", f"SELECT c.cpf FROM {C} c WHERE c.is_current", "allowed"),
]


def _user(users, role):
    return {"regional_mgr": users["mgr_se"], "hr_analyst": users["hr"]}.get(role) or users[role]


@pytest.mark.parametrize("role,sql,expected", CORPUS, ids=[f"{i:02d}-{c[2]}" for i, c in enumerate(CORPUS)])
def test_corpus(engine, users, role, sql, expected):
    r = engine.check(sql, _user(users, role))
    if expected == "allowed":
        assert r.allowed, r.violations
        assert r.enforced_sql
    else:
        assert not r.allowed, f"should be blocked: {sql}\nenforced: {r.enforced_sql}"
        codes = [v.code for v in r.violations]
        assert expected in codes, codes


def test_refusals_are_not_correctable(engine, users):
    for code_sql in ["SELECT 1; DROP TABLE x", "SELECT * FROM hr.pay"]:
        r = engine.check(code_sql, users["analyst"])
        assert r.refusable


def test_correctable_violations_carry_hints(engine, users):
    r = engine.check("SELECT * FROM crm.cust_mstr", users["analyst"])
    assert r.violations[0].hint and "CUST_MSTR_V2" in r.violations[0].hint
    r = engine.check("SELECT h.ord_dt FROM sales.ord_hdr h", users["analyst"])
    assert r.violations[0].code == "V020"


def test_unknown_role_fails_closed(engine):
    from sqlagent.core.schemas import UserContext
    r = engine.check("SELECT 1", UserContext(user_id="x", role="superuser"))
    assert not r.allowed
