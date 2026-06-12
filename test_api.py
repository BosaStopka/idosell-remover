# -*- coding: utf-8 -*-
"""Krok 0 - test read-only API IdoSell.

POST /products/products/search, 1 produkt, returnElements
["code", "pictures", "pictures_count"]. Zadnych zapisow.
"""
import json
import sys
from pathlib import Path

import requests

BASE = "https://www.bosastopka.pl/api/admin/v8"
CONFIG = Path(__file__).parent / "idosell_config.json"


def main():
    api_key = json.loads(CONFIG.read_text(encoding="utf-8"))["api_key"]

    body = {
        "params": {
            "returnProducts": "active",
            "returnElements": ["code", "pictures", "pictures_count"],
            "resultsPage": 0,
            "resultsLimit": 1,
        }
    }

    resp = requests.post(
        f"{BASE}/products/products/search",
        headers={
            "X-API-KEY": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json=body,
        timeout=30,
    )

    print(f"HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        print(resp.text[:2000])
        sys.exit(1)

    print(json.dumps(data, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
