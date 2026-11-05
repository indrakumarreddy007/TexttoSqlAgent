#!/usr/bin/env python3
"""Build the Halcyon Group warehouse (DuckDB) from the Kaggle raw files.

    python setup/build_warehouse.py [--raw data/raw] [--out data/warehouse.duckdb]

Pipeline: raw CSV -> `stg` schema (as-is) -> curated domain schemas with deliberate
enterprise problems (cryptic names, soft deletes, SCD2, deprecated table, BRL amounts,
fiscal calendar, synthetic PII) -> `meta.build_info`. Every random choice is seeded or
md5-based, so the same input always produces the same warehouse and the same gold answers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd
from faker import Faker

ROOT = Path(__file__).resolve().parents[1]
AS_OF_DATE = "2018-08-31"  # last complete month of Olist data
FX_START, FX_END = date(2016, 1, 1), date(2019, 12, 31)

OLIST_FILES = {
    "customers": "olist_customers_dataset.csv",
    "geolocation": "olist_geolocation_dataset.csv",
    "order_items": "olist_order_items_dataset.csv",
    "payments": "olist_order_payments_dataset.csv",
    "reviews": "olist_order_reviews_dataset.csv",
    "orders": "olist_orders_dataset.csv",
    "products": "olist_products_dataset.csv",
    "sellers": "olist_sellers_dataset.csv",
    "cat_xlate": "product_category_name_translation.csv",
}
HR_FILE = "WA_Fn-UseC_-HR-Employee-Attrition.csv"

REGIONS = [  # state_cd, state_nm, region_cd, region_nm
    ("AC", "Acre", "N", "North"), ("AP", "Amapa", "N", "North"), ("AM", "Amazonas", "N", "North"),
    ("PA", "Para", "N", "North"), ("RO", "Rondonia", "N", "North"), ("RR", "Roraima", "N", "North"),
    ("TO", "Tocantins", "N", "North"),
    ("AL", "Alagoas", "NE", "Northeast"), ("BA", "Bahia", "NE", "Northeast"), ("CE", "Ceara", "NE", "Northeast"),
    ("MA", "Maranhao", "NE", "Northeast"), ("PB", "Paraiba", "NE", "Northeast"),
    ("PE", "Pernambuco", "NE", "Northeast"), ("PI", "Piaui", "NE", "Northeast"),
    ("RN", "Rio Grande do Norte", "NE", "Northeast"), ("SE", "Sergipe", "NE", "Northeast"),
    ("DF", "Distrito Federal", "CO", "Center-West"), ("GO", "Goias", "CO", "Center-West"),
    ("MT", "Mato Grosso", "CO", "Center-West"), ("MS", "Mato Grosso do Sul", "CO", "Center-West"),
    ("ES", "Espirito Santo", "SE", "Southeast"), ("MG", "Minas Gerais", "SE", "Southeast"),
    ("RJ", "Rio de Janeiro", "SE", "Southeast"), ("SP", "Sao Paulo", "SE", "Southeast"),
    ("PR", "Parana", "S", "South"), ("RS", "Rio Grande do Sul", "S", "South"), ("SC", "Santa Catarina", "S", "South"),
]


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def _bucket(key: str, salt: str) -> int:
    """Stable 0..99 bucket for deterministic sampling."""
    return int(hashlib.md5(f"{salt}:{key}".encode()).hexdigest()[:8], 16) % 100


# --------------------------------------------------------------------------- staging
def load_staging(con: duckdb.DuckDBPyConnection, raw: Path) -> str:
    con.execute("CREATE SCHEMA stg")
    olist, hr = raw / "olist", raw / "hr"
    for name, file in OLIST_FILES.items():
        path = olist / file
        if not path.exists():
            sys.exit(f"missing {path}. Run setup/download_kaggle.py (or setup/make_sample_data.py)")
        con.execute(
            f"CREATE TABLE stg.{name} AS SELECT * FROM read_csv(?, header=true, quote='\"', escape='\"', "
            f"all_varchar=true, strict_mode=false)",
            [str(path)],
        )
    # The real translation file starts with a UTF-8 BOM in its header.
    cols = [r[0] for r in con.execute("DESCRIBE stg.cat_xlate").fetchall()]
    if cols[0] != "product_category_name":
        con.execute(f'ALTER TABLE stg.cat_xlate RENAME "{cols[0]}" TO product_category_name')
    con.execute("CREATE TABLE stg.hr AS SELECT * FROM read_csv(?, header=true, all_varchar=true)",
                [str(hr / HR_FILE)])
    src = (olist / "SOURCE").read_text().strip() if (olist / "SOURCE").exists() else "kaggle"
    return src


# --------------------------------------------------------------------------- FX
# Federal Reserve H.10 daily rates (BRL per USD), pinned copy from github.com/datasets/exchange-rates
FED_H10_URL = ("https://raw.githubusercontent.com/datasets/exchange-rates/"
               "2c93f64390781ffa082441909b6ea86a760bc214/data/daily.csv")


def _fx_frankfurter() -> pd.DataFrame:
    url = f"https://api.frankfurter.dev/v1/{FX_START}..{FX_END}?from=BRL&to=USD"
    with urllib.request.urlopen(url, timeout=20) as r:
        data = json.load(r)["rates"]
    return pd.DataFrame({"rate_dt": pd.to_datetime(list(data)), "rate": [v["USD"] for v in data.values()]})


def _fx_fed_h10() -> pd.DataFrame:
    with urllib.request.urlopen(FED_H10_URL, timeout=60) as r:
        df = pd.read_csv(r)
    df = df[(df["Country"] == "Brazil") & df["Exchange rate"].notna()]
    df = df[(df["Date"] >= str(FX_START)) & (df["Date"] <= str(FX_END))]
    if len(df) < 900:
        raise ValueError(f"too few BRL rates: {len(df)}")
    return pd.DataFrame({"rate_dt": pd.to_datetime(df["Date"]), "rate": (1.0 / df["Exchange rate"]).round(6)})


def fx_series(raw: Path) -> tuple[pd.DataFrame, str]:
    cache = raw / "fx" / "brl_usd.csv"
    if cache.exists():
        return pd.read_csv(cache, parse_dates=["rate_dt"]), "cache"
    df, source = None, None
    for name, fetch in (("frankfurter-ecb", _fx_frankfurter), ("fed-h10", _fx_fed_h10)):
        if os.environ.get("SQLAGENT_OFFLINE"):
            break
        try:
            df, source = fetch(), name
            log(f"FX rates from {name}: {len(df)} business days")
            break
        except Exception as e:
            log(f"FX source {name} unavailable ({type(e).__name__})")
    if df is None:  # offline / blocked: seeded random walk in the real historical band
        log("using seeded synthetic BRL->USD series")
        rng = random.Random(42)
        days = pd.date_range(FX_START, FX_END, freq="B")
        rate, vals = 0.252, []
        for d in days:
            target = 0.31 if d.year == 2017 else (0.26 if d.year >= 2018 and d.month >= 6 else 0.29)
            rate += (target - rate) * 0.01 + rng.gauss(0, 0.0025)
            vals.append(round(rate, 5))
        df = pd.DataFrame({"rate_dt": days, "rate": vals})
        source = "synthetic"
    cache.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(cache, index=False)
    (cache.parent / "SOURCE").write_text(source + "\n")
    return df, source


# --------------------------------------------------------------------------- curated
def build_reference(con) -> None:
    con.execute("CREATE SCHEMA ref")
    con.execute("CREATE TABLE ref.region(state_cd VARCHAR PRIMARY KEY, state_nm VARCHAR, region_cd VARCHAR, "
                "region_nm VARCHAR)")
    con.executemany("INSERT INTO ref.region VALUES (?,?,?,?)", REGIONS)


def build_finance(con, raw: Path) -> str:
    con.execute("CREATE SCHEMA finance")
    df, source = fx_series(raw)
    con.register("_fx", df)
    # Rates are published on business days only; carry the last rate forward.
    con.execute(f"""
        CREATE TABLE finance.fx_rate_daily AS
        WITH days AS (SELECT CAST(d AS DATE) AS rate_dt FROM range(DATE '{FX_START}', DATE '{FX_END}' + 1,
                                                                    INTERVAL 1 DAY) t(d))
        SELECT d.rate_dt, 'BRL' AS from_ccy, 'USD' AS to_ccy,
               CAST(last_value(f.rate IGNORE NULLS) OVER (ORDER BY d.rate_dt) AS DECIMAL(10,6)) AS rate,
               f.rate IS NULL AS is_filled
        FROM days d LEFT JOIN (SELECT CAST(rate_dt AS DATE) AS rate_dt, rate FROM _fx) f USING (rate_dt)
    """)
    con.execute("UPDATE finance.fx_rate_daily SET rate = (SELECT rate FROM finance.fx_rate_daily "
                "WHERE rate IS NOT NULL ORDER BY rate_dt LIMIT 1) WHERE rate IS NULL")
    con.unregister("_fx")
    # Fiscal year starts 1 April and is named by the calendar year in which it ends.
    con.execute(f"""
        CREATE TABLE finance.fiscal_cal AS
        SELECT CAST(d AS DATE) AS cal_dt,
               year(d) AS cal_yr, quarter(d) AS cal_qtr, month(d) AS cal_mo,
               CASE WHEN month(d) >= 4 THEN year(d) + 1 ELSE year(d) END AS fiscal_yr,
               ((month(d) + 8) % 12) // 3 + 1 AS fiscal_qtr,
               (month(d) + 8) % 12 + 1 AS fiscal_mo,
               'FY' || (CASE WHEN month(d) >= 4 THEN year(d) + 1 ELSE year(d) END)
                    || '-Q' || (((month(d) + 8) % 12) // 3 + 1) AS fiscal_qtr_label,
               dayofweek(d) IN (0, 6) AS is_weekend
        FROM range(DATE '{FX_START}', DATE '{FX_END}' + 1, INTERVAL 1 DAY) t(d)
    """)
    return source


def build_sales(con) -> None:
    con.execute("CREATE SCHEMA sales")
    con.execute("""
        CREATE TABLE sales.ord_hdr AS
        SELECT order_id AS ord_id, customer_id AS cust_id, order_status AS ord_stat_cd,
               TRY_CAST(order_purchase_timestamp AS TIMESTAMP) AS purch_ts,
               TRY_CAST(order_approved_at AS TIMESTAMP) AS appr_ts,
               TRY_CAST(order_delivered_carrier_date AS TIMESTAMP) AS carrier_ts,
               TRY_CAST(order_delivered_customer_date AS TIMESTAMP) AS dlvr_ts,
               CAST(TRY_CAST(order_estimated_delivery_date AS TIMESTAMP) AS DATE) AS est_dlvr_dt,
               substr(md5('del:' || order_id), 1, 2) IN ('00', '01', '02') AS is_deleted,
               'olist' AS src_sys_cd
        FROM stg.orders
    """)
    con.execute("""
        CREATE TABLE sales.ord_line AS
        SELECT order_id AS ord_id, CAST(order_item_id AS INTEGER) AS line_no, product_id AS prod_id,
               seller_id, TRY_CAST(shipping_limit_date AS TIMESTAMP) AS ship_lmt_ts,
               CAST(price AS DECIMAL(12,2)) AS price_lcl, CAST(freight_value AS DECIMAL(12,2)) AS frt_lcl,
               'BRL' AS ccy_cd
        FROM stg.order_items
    """)
    con.execute("""
        CREATE TABLE sales.pmt AS
        SELECT order_id AS ord_id, CAST(payment_sequential AS INTEGER) AS pmt_seq,
               payment_type AS pmt_type_cd, CAST(payment_installments AS INTEGER) AS n_inst,
               CAST(payment_value AS DECIMAL(12,2)) AS pmt_amt_lcl, 'BRL' AS ccy_cd
        FROM stg.payments
    """)


def build_crm(con) -> None:
    con.execute("CREATE SCHEMA crm")
    cust = con.execute("""
        SELECT c.customer_id AS cust_id, c.customer_unique_id AS cust_uid,
               lpad(c.customer_zip_code_prefix, 5, '0') AS cust_zip5, c.customer_city AS cust_city,
               c.customer_state AS cust_state, CAST(min(TRY_CAST(o.order_purchase_timestamp AS TIMESTAMP)) AS DATE)
               AS first_ord_dt
        FROM stg.customers c LEFT JOIN stg.orders o USING (customer_id)
        GROUP BY ALL ORDER BY cust_uid, cust_id
    """).df()

    fake = Faker("pt_BR")
    fake.seed_instance(2024)
    persons = {}
    for u in cust["cust_uid"].drop_duplicates():  # sorted -> deterministic
        nm = fake.name()
        handle = "".join(ch for ch in nm.lower().split()[0] if ch.isalpha())[:12] or "cliente"
        persons[u] = (nm, f"{handle}.{u[:6]}@{fake.free_email_domain()}", fake.cellphone_number(), fake.cpf())
    cust["cust_nm"] = cust["cust_uid"].map(lambda u: persons[u][0])
    cust["email"] = cust["cust_uid"].map(lambda u: persons[u][1])
    cust["phone"] = cust["cust_uid"].map(lambda u: persons[u][2])
    cust["cpf"] = cust["cust_uid"].map(lambda u: persons[u][3])

    states = sorted({r[0] for r in REGIONS})
    rows = []
    for r in cust.itertuples(index=False):
        base = {k: getattr(r, k) for k in ("cust_id", "cust_uid", "cust_nm", "email", "phone", "cpf")}
        first = r.first_ord_dt if pd.notna(r.first_ord_dt) else date(2017, 1, 1)
        if _bucket(r.cust_id, "scd") < 10:  # 10% moved: one historical version
            moved_on = first - timedelta(days=30 + _bucket(r.cust_id, "days") * 3)
            prev_state = states[(states.index(r.cust_state) + 1 + _bucket(r.cust_id, "st") % 5) % len(states)] \
                if r.cust_state in states else "SP"
            rows.append({**base, "cust_zip5": f"{(int(r.cust_zip5) + 1371) % 99999:05d}",
                         "cust_city": "unknown" if _bucket(r.cust_id, "c") < 30 else "cidade anterior",
                         "cust_state": prev_state, "valid_from": date(2016, 1, 1), "valid_to": moved_on,
                         "is_current": False})
            valid_from = moved_on
        else:
            valid_from = date(2016, 1, 1)
        rows.append({**base, "cust_zip5": r.cust_zip5, "cust_city": r.cust_city, "cust_state": r.cust_state,
                     "valid_from": valid_from, "valid_to": date(9999, 12, 31), "is_current": True})
    scd = pd.DataFrame(rows)
    con.register("_scd", scd)
    con.execute("""
        CREATE TABLE crm."CUST_MSTR_V2" AS
        SELECT row_number() OVER (ORDER BY cust_id, valid_from) AS cust_sk, cust_id, cust_uid, cust_nm, email,
               phone, cpf, cust_zip5, cust_city, cust_state, CAST(valid_from AS DATE) AS valid_from,
               CAST(valid_to AS DATE) AS valid_to, is_current
        FROM _scd
    """)
    con.unregister("_scd")
    # Deprecated legacy snapshot: stale (Feb 2018), legacy column names, no PII columns.
    con.execute("""
        CREATE TABLE crm.cust_mstr AS
        SELECT c.customer_id AS "CUST_ID", c.customer_city AS "CUST_CITY", c.customer_state AS "CUST_ST",
               DATE '2018-02-28' AS "SNAPSHOT_DT"
        FROM stg.customers c JOIN stg.orders o USING (customer_id)
        WHERE TRY_CAST(o.order_purchase_timestamp AS TIMESTAMP) < TIMESTAMP '2018-03-01'
    """)


def build_product_ops_cx(con) -> None:
    con.execute("CREATE SCHEMA product")
    con.execute("""
        CREATE TABLE product.prod AS
        SELECT product_id AS prod_id, NULLIF(product_category_name, '') AS cat_nm_pt,
               TRY_CAST(product_name_lenght AS INTEGER) AS name_len,
               TRY_CAST(product_description_lenght AS INTEGER) AS desc_len,
               TRY_CAST(product_photos_qty AS INTEGER) AS photo_qty,
               COALESCE(TRY_CAST(product_weight_g AS INTEGER), -1) AS wt_g,   -- -1 = unknown (legacy sentinel)
               TRY_CAST(product_length_cm AS INTEGER) AS len_cm,
               TRY_CAST(product_height_cm AS INTEGER) AS ht_cm,
               TRY_CAST(product_width_cm AS INTEGER) AS wd_cm
        FROM stg.products
    """)
    con.execute("""
        CREATE TABLE product.cat_xlate AS
        SELECT product_category_name AS cat_nm_pt, product_category_name_english AS cat_nm_en
        FROM stg.cat_xlate
    """)
    con.execute("CREATE SCHEMA ops")
    con.execute("""
        CREATE TABLE ops.seller AS
        SELECT seller_id, lpad(seller_zip_code_prefix, 5, '0') AS seller_zip5, seller_city,
               COALESCE(NULLIF(seller_state, ''), 'N/A') AS seller_state
        FROM stg.sellers
    """)
    con.execute("""
        CREATE TABLE ops.geo AS
        SELECT lpad(geolocation_zip_code_prefix, 5, '0') AS geo_zip5, CAST(geolocation_lat AS DOUBLE) AS lat,
               CAST(geolocation_lng AS DOUBLE) AS lng, geolocation_city AS geo_city, geolocation_state AS geo_state
        FROM stg.geolocation
    """)
    con.execute("CREATE SCHEMA cx")
    con.execute("""
        CREATE TABLE cx.review AS
        SELECT review_id, order_id AS ord_id, TRY_CAST(review_score AS INTEGER) AS score,
               NULLIF(review_comment_title, '') AS title, NULLIF(review_comment_message, '') AS comment_txt,
               CAST(TRY_CAST(review_creation_date AS TIMESTAMP) AS DATE) AS created_dt,
               TRY_CAST(review_answer_timestamp AS TIMESTAMP) AS answered_ts
        FROM stg.reviews
    """)


def build_hr(con) -> None:
    con.execute("CREATE SCHEMA hr")
    df = con.execute("SELECT * FROM stg.hr ORDER BY CAST(EmployeeNumber AS INTEGER)").df()
    fake = Faker("pt_BR")
    fake.seed_instance(1470)
    states = ["SP"] * 6 + ["RJ", "MG", "PR", "RS", "BA", "PE", "DF"]
    as_of = datetime.strptime(AS_OF_DATE, "%Y-%m-%d").date()
    emp, pay = [], []
    for r in df.itertuples(index=False):
        eid = int(r.EmployeeNumber)
        years = int(r.YearsAtCompany)
        emp.append({
            "emp_id": eid, "emp_nm": fake.name(), "national_id": fake.cpf(),
            "gender": r.Gender, "age": int(r.Age), "marital_stat": r.MaritalStatus,
            "dept_nm": r.Department, "job_role": r.JobRole, "job_lvl": int(r.JobLevel),
            "edu_field": r.EducationField, "work_state": states[_bucket(str(eid), "ws") % len(states)],
            "hire_dt": as_of - timedelta(days=years * 365 + _bucket(str(eid), "hd") * 3),
            "is_attrited": r.Attrition == "Yes", "overtime_flg": r.OverTime == "Yes",
            "perf_rating": int(r.PerformanceRating),
        })
        pay.append({
            "emp_id": eid, "monthly_income": int(r.MonthlyIncome), "hourly_rate": int(r.HourlyRate),
            "pct_salary_hike": int(r.PercentSalaryHike), "stock_opt_lvl": int(r.StockOptionLevel),
            "eff_dt": as_of,
        })
    con.register("_emp", pd.DataFrame(emp))
    con.register("_pay", pd.DataFrame(pay))
    con.execute("CREATE TABLE hr.employee AS SELECT * REPLACE (CAST(hire_dt AS DATE) AS hire_dt) FROM _emp")
    con.execute("CREATE TABLE hr.pay AS SELECT * REPLACE (CAST(eff_dt AS DATE) AS eff_dt) FROM _pay")
    con.unregister("_emp")
    con.unregister("_pay")


def build(raw: Path, out: Path) -> dict:
    tmp = out.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    data_source = load_staging(con, raw)
    log(f"staged raw files (source={data_source})")
    build_reference(con)
    fx_source = build_finance(con, raw)
    build_sales(con)
    build_crm(con)
    build_product_ops_cx(con)
    build_hr(con)
    con.execute("DROP SCHEMA stg CASCADE")
    con.execute("CREATE SCHEMA meta")
    info = {
        "as_of_date": AS_OF_DATE, "data_source": data_source, "fx_source": fx_source,
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    con.execute("CREATE TABLE meta.build_info AS SELECT * FROM (VALUES (?, ?, ?, ?)) "
                "t(as_of_date, data_source, fx_source, built_at)", list(info.values()))
    counts = {
        f"{s}.{t}": con.execute(f'SELECT count(*) FROM {s}."{t}"').fetchone()[0]
        for s, t in con.execute("SELECT table_schema, table_name FROM information_schema.tables "
                                "WHERE table_schema NOT IN ('meta') ORDER BY 1, 2").fetchall()
    }
    con.execute("CHECKPOINT")
    con.close()
    tmp.replace(out)
    return {"info": info, "counts": counts}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", type=Path, default=ROOT / "data" / "raw")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "warehouse.duckdb")
    args = ap.parse_args()
    res = build(args.raw, args.out)
    for k, v in res["counts"].items():
        log(f"{k:<28} {v:>10,}")
    log(f"done -> {args.out}  {res['info']}")


if __name__ == "__main__":
    main()
