#!/usr/bin/env python3
"""Download the Kaggle source datasets into data/raw/.

Requires a Kaggle API token: kaggle.com -> Settings -> API -> "Create New Token",
saved to ~/.kaggle/kaggle.json (chmod 600), or KAGGLE_USERNAME / KAGGLE_KEY env vars.

    python setup/download_kaggle.py

No Kaggle access (e.g. CI)? Use `python setup/make_sample_data.py` instead: it writes
seeded synthetic files with the exact same names and columns.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"

DATASETS = {
    "olist": (
        "olistbr/brazilian-ecommerce",
        [
            "olist_customers_dataset.csv",
            "olist_geolocation_dataset.csv",
            "olist_order_items_dataset.csv",
            "olist_order_payments_dataset.csv",
            "olist_order_reviews_dataset.csv",
            "olist_orders_dataset.csv",
            "olist_products_dataset.csv",
            "olist_sellers_dataset.csv",
            "product_category_name_translation.csv",
        ],
    ),
    "hr": (
        "pavansubhasht/ibm-hr-analytics-attrition-dataset",
        ["WA_Fn-UseC_-HR-Employee-Attrition.csv"],
    ),
}


def _has_token() -> bool:
    return (Path.home() / ".kaggle" / "kaggle.json").exists() or bool(
        os.environ.get("KAGGLE_USERNAME") and os.environ.get("KAGGLE_KEY")
    )


def main() -> None:
    if not _has_token():
        sys.exit("No Kaggle credentials found. See the docstring of this script.")
    if shutil.which("kaggle") is None:
        sys.exit("`kaggle` CLI not found. Install it with: pip install kaggle")
    for folder, (slug, files) in DATASETS.items():
        out = RAW / folder
        out.mkdir(parents=True, exist_ok=True)
        if all((out / f).exists() for f in files):
            print(f"[{folder}] already present")
            continue
        print(f"[{folder}] downloading {slug}")
        subprocess.run(["kaggle", "datasets", "download", "-d", slug, "-p", str(out), "--unzip"], check=True)
        missing = [f for f in files if not (out / f).exists()]
        if missing:
            sys.exit(f"[{folder}] missing after download: {missing}")
        (out / "SOURCE").write_text("kaggle\n")
        print(f"[{folder}] ok")


if __name__ == "__main__":
    main()
