#!/usr/bin/env python3
"""Generate seeded synthetic data with the SAME file names and columns as the Kaggle
datasets (Olist Brazilian E-Commerce, IBM HR Attrition).

Use it for CI and for working offline. The real data comes from
`halcyon-warehouse download`. The warehouse build accepts either.

    halcyon-warehouse sample [--orders 4000] [--seed 7]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import random
from datetime import datetime, timedelta
from pathlib import Path

from sqlagent.config import ROOT  # noqa: E402  (project root: config/ and data/)

RAW = ROOT / "data" / "raw"

STATES = {  # state: (weight, sample cities)
    "SP": (42, ["sao paulo", "campinas", "santos", "guarulhos"]),
    "RJ": (13, ["rio de janeiro", "niteroi"]),
    "MG": (12, ["belo horizonte", "uberlandia"]),
    "RS": (6, ["porto alegre", "caxias do sul"]),
    "PR": (5, ["curitiba", "londrina"]),
    "SC": (4, ["florianopolis", "joinville"]),
    "BA": (3, ["salvador"]),
    "DF": (2, ["brasilia"]),
    "GO": (2, ["goiania"]),
    "ES": (2, ["vitoria"]),
    "PE": (2, ["recife"]),
    "CE": (1, ["fortaleza"]),
    "PA": (1, ["belem"]),
    "MT": (1, ["cuiaba"]),
    "MS": (1, ["campo grande"]),
    "AM": (1, ["manaus"]),
    "RN": (1, ["natal"]),
}
CATEGORIES = {
    "cama_mesa_banho": "bed_bath_table",
    "beleza_saude": "health_beauty",
    "esporte_lazer": "sports_leisure",
    "moveis_decoracao": "furniture_decor",
    "informatica_acessorios": "computers_accessories",
    "utilidades_domesticas": "housewares",
    "relogios_presentes": "watches_gifts",
    "telefonia": "telephony",
    "ferramentas_jardim": "garden_tools",
    "automotivo": "auto",
    "brinquedos": "toys",
    "cool_stuff": "cool_stuff",
    "perfumaria": "perfumery",
    "bebes": "baby",
    "eletronicos": "electronics",
    "papelaria": "stationery",
    "fashion_bolsas_e_acessorios": "fashion_bags_accessories",
    "pet_shop": "pet_shop",
}
STATUSES = [("delivered", 96.5), ("shipped", 1.1), ("canceled", 0.7), ("unavailable", 0.6),
            ("invoiced", 0.4), ("processing", 0.4), ("created", 0.1), ("approved", 0.2)]
PAY_TYPES = [("credit_card", 74), ("boleto", 19), ("voucher", 5), ("debit_card", 2)]
START, END = datetime(2016, 9, 4), datetime(2018, 10, 17)


def uid(rng: random.Random) -> str:
    return hashlib.md5(str(rng.random()).encode()).hexdigest()


def wchoice(rng, pairs):
    return rng.choices([p[0] for p in pairs], weights=[p[1] for p in pairs])[0]


def ts(d: datetime | None) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S") if d else ""


def write(path: Path, header: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def make_olist(n_orders: int, rng: random.Random, raw: Path = RAW) -> None:
    out = raw / "olist"
    state_pairs = [(s, w) for s, (w, _) in STATES.items()]
    zips = {s: [f"{rng.randint(1000, 99999):05d}" for _ in range(8)] for s in STATES}

    sellers = []
    for _ in range(max(50, n_orders // 20)):
        st = wchoice(rng, state_pairs)
        sellers.append([uid(rng), rng.choice(zips[st]), rng.choice(STATES[st][1]), st])
    products = []
    for _ in range(max(100, n_orders // 6)):
        cat = rng.choice(list(CATEGORIES)) if rng.random() > 0.015 else ""
        products.append([uid(rng), cat, rng.randint(20, 60), rng.randint(100, 3000), rng.randint(1, 6),
                         rng.randint(100, 15000), rng.randint(10, 80), rng.randint(2, 60), rng.randint(10, 60)])

    persons = [uid(rng) for _ in range(int(n_orders * 0.97))]
    customers, orders, items, pays, reviews = [], [], [], [], []
    span = (END - START).total_seconds()
    for i in range(n_orders):
        # growth curve: more orders later in the period
        purchase = START + timedelta(seconds=span * (rng.random() ** 0.6))
        st = wchoice(rng, state_pairs)
        cust_id = uid(rng)
        person = persons[i] if i < len(persons) else rng.choice(persons)
        customers.append([cust_id, person, rng.choice(zips[st]), rng.choice(STATES[st][1]), st])
        status = wchoice(rng, STATUSES)
        approved = purchase + timedelta(hours=rng.uniform(0.2, 30)) if status not in ("created",) else None
        shipped = approved and status in ("delivered", "shipped")
        carrier = approved + timedelta(days=rng.uniform(1, 5)) if shipped else None
        delivered = carrier + timedelta(days=rng.uniform(2, 20)) if carrier and status == "delivered" else None
        est = (purchase + timedelta(days=rng.randint(15, 40))).replace(hour=0, minute=0, second=0)
        oid = uid(rng)
        orders.append([oid, cust_id, status, ts(purchase), ts(approved), ts(carrier), ts(delivered), ts(est)])
        total = 0.0
        n_items = 1 if rng.random() < 0.88 else rng.randint(2, 4)
        if status in ("created", "unavailable") and rng.random() < 0.8:
            n_items = 0
        for k in range(1, n_items + 1):
            p = rng.choice(products)
            price = round(rng.lognormvariate(4.4, 0.8), 2)
            freight = round(rng.uniform(7, 45), 2)
            total += price + freight
            items.append([oid, k, p[0], rng.choice(sellers)[0], ts(purchase + timedelta(days=6)), price, freight])
        if total == 0:
            total = round(rng.uniform(30, 300), 2)
        ptype = wchoice(rng, PAY_TYPES)
        n_pay = 1 if rng.random() < 0.95 else 2
        for s in range(1, n_pay + 1):
            amt = round(total / n_pay, 2)
            inst = rng.randint(1, 10) if ptype == "credit_card" else 1
            pays.append([oid, s, ptype if s == 1 else "voucher", inst, amt])
        if rng.random() < 0.97:
            created = (delivered or purchase + timedelta(days=20)).replace(hour=0, minute=0, second=0)
            score = rng.choices([1, 2, 3, 4, 5], weights=[11, 3, 8, 19, 59])[0]
            comment = "" if rng.random() < 0.58 else rng.choice([
                "Produto chegou antes do prazo, recomendo",
                "Nao recebi o produto ate agora",
                "Muito bom, qualidade excelente",
                "Veio com defeito, quero devolver",
                "Entrega atrasou mas o produto e bom",
            ])
            reviews.append([uid(rng), oid, score, "", comment, ts(created),
                            ts(created + timedelta(hours=rng.uniform(5, 72)))])

    geo = []
    for _ in range(n_orders * 5):
        st = wchoice(rng, state_pairs)
        geo.append([rng.choice(zips[st]), round(rng.uniform(-33, 2), 6), round(rng.uniform(-73, -35), 6),
                    rng.choice(STATES[st][1]), st])

    write(out / "olist_customers_dataset.csv",
          ["customer_id", "customer_unique_id", "customer_zip_code_prefix", "customer_city", "customer_state"],
          customers)
    write(out / "olist_orders_dataset.csv",
          ["order_id", "customer_id", "order_status", "order_purchase_timestamp", "order_approved_at",
           "order_delivered_carrier_date", "order_delivered_customer_date", "order_estimated_delivery_date"],
          orders)
    write(out / "olist_order_items_dataset.csv",
          ["order_id", "order_item_id", "product_id", "seller_id", "shipping_limit_date", "price",
           "freight_value"], items)
    write(out / "olist_order_payments_dataset.csv",
          ["order_id", "payment_sequential", "payment_type", "payment_installments", "payment_value"], pays)
    write(out / "olist_order_reviews_dataset.csv",
          ["review_id", "order_id", "review_score", "review_comment_title", "review_comment_message",
           "review_creation_date", "review_answer_timestamp"], reviews)
    write(out / "olist_products_dataset.csv",
          ["product_id", "product_category_name", "product_name_lenght", "product_description_lenght",
           "product_photos_qty", "product_weight_g", "product_length_cm", "product_height_cm",
           "product_width_cm"], products)
    write(out / "olist_sellers_dataset.csv",
          ["seller_id", "seller_zip_code_prefix", "seller_city", "seller_state"], sellers)
    write(out / "olist_geolocation_dataset.csv",
          ["geolocation_zip_code_prefix", "geolocation_lat", "geolocation_lng", "geolocation_city",
           "geolocation_state"], geo)
    write(out / "product_category_name_translation.csv",
          ["product_category_name", "product_category_name_english"], [list(kv) for kv in CATEGORIES.items()])
    (out / "SOURCE").write_text("sample\n")


def make_hr(rng: random.Random, n: int = 1470, raw: Path = RAW) -> None:
    roles = {
        "Sales": ["Sales Executive", "Sales Representative", "Manager"],
        "Research & Development": ["Research Scientist", "Laboratory Technician", "Manufacturing Director",
                                   "Healthcare Representative", "Research Director", "Manager"],
        "Human Resources": ["Human Resources", "Manager"],
    }
    header = ["Age", "Attrition", "BusinessTravel", "DailyRate", "Department", "DistanceFromHome", "Education",
              "EducationField", "EmployeeCount", "EmployeeNumber", "EnvironmentSatisfaction", "Gender",
              "HourlyRate", "JobInvolvement", "JobLevel", "JobRole", "JobSatisfaction", "MaritalStatus",
              "MonthlyIncome", "MonthlyRate", "NumCompaniesWorked", "Over18", "OverTime", "PercentSalaryHike",
              "PerformanceRating", "RelationshipSatisfaction", "StandardHours", "StockOptionLevel",
              "TotalWorkingYears", "TrainingTimesLastYear", "WorkLifeBalance", "YearsAtCompany",
              "YearsInCurrentRole", "YearsSinceLastPromotion", "YearsWithCurrManager"]
    rows = []
    emp_no = 1
    for _ in range(n):
        dept = rng.choices(list(roles), weights=[30, 65, 5])[0]
        role = rng.choice(roles[dept])
        level = 5 if "Director" in role else (rng.randint(3, 5) if role == "Manager" else rng.randint(1, 3))
        income = int(level * 3500 + rng.uniform(-1500, 2500))
        total_years = rng.randint(level, 40)
        at_co = rng.randint(0, min(total_years, 30))
        in_role = rng.randint(0, at_co)
        rows.append([rng.randint(18, 60), rng.choices(["Yes", "No"], weights=[16, 84])[0],
                     rng.choice(["Travel_Rarely", "Travel_Frequently", "Non-Travel"]), rng.randint(100, 1500),
                     dept, rng.randint(1, 29), rng.randint(1, 5),
                     rng.choice(["Life Sciences", "Medical", "Marketing", "Technical Degree", "Other",
                                 "Human Resources"]),
                     1, emp_no, rng.randint(1, 4), rng.choice(["Male", "Female"]), rng.randint(30, 100),
                     rng.randint(1, 4), level, role, rng.randint(1, 4),
                     rng.choice(["Single", "Married", "Divorced"]), income, rng.randint(2000, 27000),
                     rng.randint(0, 9), "Y", rng.choice(["Yes", "No"]), rng.randint(11, 25), rng.choice([3, 4]),
                     rng.randint(1, 4), 80, rng.randint(0, 3), total_years, rng.randint(0, 6),
                     rng.randint(1, 4), at_co, in_role, rng.randint(0, in_role), rng.randint(0, in_role)])
        emp_no += rng.choice([1, 1, 2])
    write(raw / "hr" / "WA_Fn-UseC_-HR-Employee-Attrition.csv", header, rows)
    (raw / "hr" / "SOURCE").write_text("sample\n")


def generate(raw: Path, orders: int = 4000, seed: int = 7) -> None:
    rng = random.Random(seed)
    make_olist(orders, rng, raw)
    make_hr(rng, raw=raw)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--orders", type=int, default=4000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path, default=RAW, help="raw data directory")
    ap.add_argument("--force", action="store_true", help="overwrite even if real Kaggle files are present")
    args = ap.parse_args()
    for folder in ("olist", "hr"):
        src = args.out / folder / "SOURCE"
        if src.exists() and src.read_text().strip() == "kaggle" and not args.force:
            raise SystemExit(f"data/raw/{folder} holds real Kaggle data; pass --force to overwrite")
    generate(args.out, args.orders, args.seed)
    print(f"sample data written to {args.out}")


if __name__ == "__main__":
    main()
