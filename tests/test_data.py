"""Warehouse build invariants: these protect the gold answers of the eval set."""
import duckdb
import pytest


@pytest.fixture(scope="module")
def con(warehouse):
    c = duckdb.connect(str(warehouse), read_only=True)
    yield c
    c.close()


def one(con, sql):
    return con.execute(sql).fetchone()[0]


def test_expected_tables_exist(con):
    tables = {f"{s}.{t}".lower() for s, t in con.execute(
        "SELECT table_schema, table_name FROM information_schema.tables").fetchall()}
    for t in ["sales.ord_hdr", "sales.ord_line", "sales.pmt", "crm.cust_mstr_v2", "crm.cust_mstr",
              "product.prod", "product.cat_xlate", "ops.seller", "ops.geo", "cx.review", "hr.employee",
              "hr.pay", "finance.fx_rate_daily", "finance.fiscal_cal", "ref.region", "meta.build_info"]:
        assert t in tables, t
    assert not any(t.startswith("stg.") for t in tables), "staging schema must be dropped"


def test_one_current_version_per_customer(con):
    assert one(con, 'SELECT count(*) FROM (SELECT cust_id FROM crm."CUST_MSTR_V2" WHERE is_current '
                    'GROUP BY 1 HAVING count(*) <> 1)') == 0
    assert one(con, 'SELECT count(DISTINCT cust_id) FROM crm."CUST_MSTR_V2"') == one(
        con, 'SELECT count(*) FROM crm."CUST_MSTR_V2" WHERE is_current')


def test_scd2_history_exists_and_versions_do_not_overlap(con):
    assert one(con, 'SELECT count(*) FROM crm."CUST_MSTR_V2" WHERE NOT is_current') > 0
    assert one(con, '''SELECT count(*) FROM crm."CUST_MSTR_V2" a JOIN crm."CUST_MSTR_V2" b
                       ON a.cust_id = b.cust_id AND a.cust_sk < b.cust_sk
                       WHERE a.valid_from < b.valid_to AND b.valid_from < a.valid_to''') == 0


def test_fx_covers_every_order_date(con):
    assert one(con, """SELECT count(*) FROM sales.ord_hdr h LEFT JOIN finance.fx_rate_daily fx
                       ON fx.rate_dt = CAST(h.purch_ts AS DATE) WHERE fx.rate IS NULL""") == 0
    lo, hi = con.execute("SELECT min(rate), max(rate) FROM finance.fx_rate_daily").fetchone()
    assert 0.15 < float(lo) < float(hi) < 0.45, "BRL->USD should be in the historical band"


def test_fiscal_calendar(con):
    row = con.execute("SELECT fiscal_yr, fiscal_qtr, fiscal_mo, fiscal_qtr_label FROM finance.fiscal_cal "
                      "WHERE cal_dt = DATE '2018-04-01'").fetchone()
    assert row == (2019, 1, 1, "FY2019-Q1")
    row = con.execute("SELECT fiscal_yr, fiscal_qtr FROM finance.fiscal_cal WHERE cal_dt = DATE '2018-03-31'"
                      ).fetchone()
    assert row == (2018, 4)


def test_soft_deletes_present_and_deterministic(con):
    n = one(con, "SELECT count(*) FROM sales.ord_hdr WHERE is_deleted")
    total = one(con, "SELECT count(*) FROM sales.ord_hdr")
    assert 0 < n < total * 0.03


def test_referential_integrity(con):
    assert one(con, """SELECT count(*) FROM sales.ord_hdr h LEFT JOIN crm."CUST_MSTR_V2" c
                       ON c.cust_id = h.cust_id AND c.is_current WHERE c.cust_id IS NULL""") == 0
    assert one(con, """SELECT count(*) FROM sales.ord_line l LEFT JOIN sales.ord_hdr h USING (ord_id)
                       WHERE h.ord_id IS NULL""") == 0
    assert one(con, """SELECT count(*) FROM crm."CUST_MSTR_V2" c LEFT JOIN ref.region r
                       ON r.state_cd = c.cust_state WHERE r.state_cd IS NULL""") == 0


def test_pii_is_synthetic_and_populated(con):
    assert one(con, """SELECT count(*) FROM crm."CUST_MSTR_V2" WHERE cpf IS NULL OR email IS NULL""") == 0
    assert one(con, "SELECT count(*) FROM hr.employee WHERE national_id IS NULL") == 0
