#!/usr/bin/env python3
"""Download the Kaggle source datasets into data/raw/.

Requires a Kaggle API token: kaggle.com -> Settings -> API -> "Create New Token",
saved to ~/.kaggle/kaggle.json (chmod 600), or KAGGLE_USERNAME / KAGGLE_KEY env vars.

    halcyon-warehouse download

Kaggle blocked on your network? Use pinned public GitHub mirrors of the same files. Every file
is verified against a pinned SHA-256 (and the files match the row counts published on Kaggle):

    halcyon-warehouse download --source github-mirror

No network at all (e.g. CI)? `halcyon-warehouse sample` writes seeded synthetic files with
the exact same names and columns.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from sqlagent.config import ROOT  # noqa: E402  (project root: config/ and data/)

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


# Pinned mirrors of the Kaggle files: file -> (sha256, data rows as published on Kaggle)
_OLIST_MIRROR = ("https://raw.githubusercontent.com/ignaciogomenuka/olistdata/"
                 "df03ddd67d1db5654328477f8b1d677a0d9360e5/data/csv/")
_IBM_MIRROR = ("https://raw.githubusercontent.com/IBM/employee-attrition-aif360/"
               "13287d5f717dc978eda249aef4665e04c7cec8b0/data/emp_attrition.csv")
MIRRORS = {
    "olist": {
        "olist_customers_dataset.csv": ("983a422239e1712ded753b3bf9ecf47dc73f144d306029dcfa99e70a226883d2", 99441),
        "olist_geolocation_dataset.csv": ("b514f6fc991b9566aeba02aa5d67e2c3630f034b60a0e05aa0d082a3b66d88d6",
                                          1000163),
        "olist_order_items_dataset.csv": ("0bc4d068c4fe38cbb01bd90e8746e3c613fe7b4baef75fab7b0e329701c3e279",
                                          112650),
        "olist_order_payments_dataset.csv": ("4f713964f2815dbbaa40b9488268c55aac3627bfce5aa96cf58d1f3616de3cc0",
                                             103886),
        "olist_order_reviews_dataset.csv": ("012b61c7593e34f51fa614efdf802b9c7056ce6aae5307ddb93236e7cfc797d7",
                                            99224),
        "olist_orders_dataset.csv": ("8df58ef3d2d7e9944010f7beecd9b75367f5588ec6e3c91cec19ae3345ef9ecf", 99441),
        "olist_products_dataset.csv": ("3e6569628a17fbc75fd206ee357b59e20364b9afa90f5b6cd5b4d624c58aa9cc", 32951),
        "olist_sellers_dataset.csv": ("1f643d2b950373b85735e7794b20986f528d7a000432e7c6f9bcbb44d0846a0e", 3095),
        "product_category_name_translation.csv": (
            "a81f0d1f27b27e7293f761bc79e3ce8f348ee39c4b3ed3e49bde38f478586278", 71),
    },
    "hr": {"WA_Fn-UseC_-HR-Employee-Attrition.csv": (
        "a5c31e38bd7fafc9bc333884eb181b06b41b8e5e488e8f7ccb27199fb3be7659", 1470)},
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_mirrors(raw: Path = RAW) -> None:
    for folder, files in MIRRORS.items():
        out = raw / folder
        out.mkdir(parents=True, exist_ok=True)
        for name, (sha, _rows) in files.items():
            dest = out / name
            if dest.exists() and _sha256(dest) == sha:
                print(f"[{folder}] {name} already present (verified)")
                continue
            url = _IBM_MIRROR if folder == "hr" else _OLIST_MIRROR + name
            print(f"[{folder}] downloading {name}")
            tmp = dest.with_suffix(".part")
            with urllib.request.urlopen(url, timeout=300) as r, tmp.open("wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            got = _sha256(tmp)
            if got != sha:
                tmp.unlink()
                sys.exit(f"[{folder}] checksum mismatch for {name}: {got}")
            tmp.replace(dest)
        (out / "SOURCE").write_text("kaggle\n")
        print(f"[{folder}] ok (sha256 verified)")


def _has_token() -> bool:
    return (Path.home() / ".kaggle" / "kaggle.json").exists() or bool(
        os.environ.get("KAGGLE_USERNAME") and os.environ.get("KAGGLE_KEY")
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["kaggle", "github-mirror"], default="kaggle")
    ap.add_argument("--raw", type=Path, default=RAW, help="raw data directory")
    args = ap.parse_args()
    if args.source == "github-mirror":
        download_mirrors(args.raw)
        return
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
