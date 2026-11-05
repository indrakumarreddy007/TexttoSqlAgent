from datetime import date

from sqlagent.conformance import check_conformance
from sqlagent.schemas import QualifiedRequest, TimeWindow

TW = TimeWindow(basis="fiscal", start=date(2017, 4, 1), end=date(2018, 4, 1))
GOOD = ("SELECT c.cust_state, SUM(l.price_lcl) FROM sales.ord_hdr h JOIN sales.ord_line l ON l.ord_id = h.ord_id "
        "JOIN crm.\"CUST_MSTR_V2\" c ON c.cust_id = h.cust_id AND c.is_current "
        "WHERE h.purch_ts >= TIMESTAMP '2017-04-01' AND h.purch_ts < TIMESTAMP '2018-04-01' GROUP BY 1 ORDER BY 2 DESC")


def codes(sql, **kw):
    return [v.code for v in check_conformance(sql, QualifiedRequest(**kw))]


def test_conforming_sql_passes():
    assert codes(GOOD, dimensions=["state"], intent="ranking", time_window=TW) == []


def test_dropped_dimension_is_flagged():
    sql = "SELECT SUM(l.price_lcl) FROM sales.ord_line l"
    assert codes(sql, dimensions=["customer state"]) == ["V070"]


def test_ranking_without_order_by_is_flagged():
    sql = "SELECT l.seller_id, COUNT(*) FROM sales.ord_line l GROUP BY 1"
    assert "V071" in codes(sql, intent="ranking", dimensions=["seller"])


def test_wrong_dates_are_flagged():
    sql = GOOD.replace("2017-04-01", "2018-04-01")
    assert "V072" in codes(sql, time_window=TW)


def test_trend_without_time_grouping_is_flagged():
    sql = "SELECT SUM(l.price_lcl) FROM sales.ord_line l"
    assert codes(sql, intent="trend", grain="month") == ["V070"]
    ok = "SELECT date_trunc('month', h.purch_ts), COUNT(*) FROM sales.ord_hdr h GROUP BY 1 ORDER BY 1"
    assert codes(ok, intent="trend", grain="month") == []
