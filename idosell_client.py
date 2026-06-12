# -*- coding: utf-8 -*-
"""Klient IdoSell Admin API v8 - FAZA 1: wylacznie odczyt.

Search produktow (POST, ale tylko odczyt danych), lista zdjec, download.
Zapis zdjec (FAZA 2) powstanie osobno i bedzie wywolywany wylacznie po
dwustopniowym potwierdzeniu w UI - ten modul nie zmienia niczego w sklepie.

Konfiguracja: idosell_config.json (poza gitem):
  api_key               - klucz do naglowka X-API-KEY (wymagany)
  base_url              - domyslnie https://www.bosastopka.pl/api/admin/v8
  archive_tag_value_id  - id wartosci parametru Product Tag oznaczajacej
                          archiwum; domyslnie 2386 (parametr id 993,
                          wartosc "Archiwum"). Produkty z tym tagiem sa
                          pomijane w skanie, dostepne tylko przez reczne
                          wyszukanie.
"""
import json
import time
from pathlib import Path

import requests

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "idosell_config.json"

DEFAULT_BASE_URL = "https://www.bosastopka.pl/api/admin/v8"
DEFAULT_ARCHIVE_TAG_VALUE_ID = 2386
TIMEOUT = 30
MIN_INTERVAL = 0.5   # throttling: minimalny odstep miedzy zadaniami [s]
MAX_RETRIES = 5      # liczba prob przy 429

SCAN_RETURN_ELEMENTS = ["code", "pictures", "pictures_count", "lang_data"]

_last_request = 0.0


class IdoSellError(Exception):
    pass


def load_config() -> dict | None:
    if not CONFIG_FILE.exists():
        return None
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if cfg.get("api_key"):
            return cfg
    except (json.JSONDecodeError, OSError):
        pass
    return None


def is_configured() -> bool:
    return load_config() is not None


def archive_tag_value_id() -> int:
    cfg = load_config() or {}
    return int(cfg.get("archive_tag_value_id", DEFAULT_ARCHIVE_TAG_VALUE_ID))


def _request(method: str, path: str, params, timeout: int = TIMEOUT) -> dict:
    """Zadanie z throttlingiem i retry przy 429. Zwraca zdekodowany JSON.

    207 Multi-Status nie jest bledem calosci (search zwraca 207 m.in.
    przy pustym wyniku) - decyzja nalezy do wywolujacego.
    """
    global _last_request
    cfg = load_config()
    if not cfg:
        raise IdoSellError("Brak konfiguracji (idosell_config.json)")
    url = cfg.get("base_url", DEFAULT_BASE_URL) + path

    for attempt in range(MAX_RETRIES):
        wait = MIN_INTERVAL - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()

        resp = requests.request(
            method,
            url,
            headers={
                "X-API-KEY": cfg["api_key"],
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={"params": params},
            timeout=timeout,
        )
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else 2.0 * (attempt + 1)
            time.sleep(delay)
            continue
        if resp.status_code in (401, 403):
            raise IdoSellError(f"Blad autoryzacji ({resp.status_code}) - sprawdz klucz API")
        if resp.status_code not in (200, 207):
            raise IdoSellError(f"IdoSell API {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
        except ValueError:
            raise IdoSellError(f"Nieprawidlowa odpowiedz API: {resp.text[:300]}")
        if not isinstance(data, dict):
            raise IdoSellError(f"Nieoczekiwany ksztalt odpowiedzi: {str(data)[:300]}")
        return data
    raise IdoSellError("Rate limit (429) - wyczerpano proby")


def _post(path: str, params, timeout: int = TIMEOUT) -> dict:
    return _request("POST", path, params, timeout)


def search_products(params: dict) -> dict:
    """Niskopoziomowy POST /products/products/search.

    UWAGA na format filtrow (zweryfikowane empirycznie):
    - po ID: productParams: [{"productId": 334}] (lista obiektow!);
      wariant {"productIds": [...]} jest IGNOROWANY i zwraca wszystko
    - po kodzie/frazie kodu: containsCodePart: "636401"
    - wykluczenie tagu: productParametersParams:
      [{"productParameterIds": {"productParameterIdsDisabled": [2386]}}]
    """
    return _post("/products/products/search", params)


def _image_row(img: dict) -> dict:
    iid = img.get("productImageId") or ""
    # numer slotu zakodowany w ID ("334_2.jpg" -> 2)
    try:
        slot = int(iid.rsplit("_", 1)[1].split(".")[0])
    except (IndexError, ValueError):
        slot = None
    return {
        "id": iid,
        "slot": slot,
        "priority": img.get("productImagePriority"),
        "url": img.get("productImageLargeUrl"),
        "thumb": img.get("productImageSmallUrl"),
        "width": img.get("productImageWidth"),
        "height": img.get("productImageHeight"),
        "hash": img.get("productImageHash"),
    }


def _product_name(prod: dict) -> str:
    for lang in prod.get("productDescriptionsLangData") or []:
        if lang.get("langId") == "pol":
            return lang.get("productName") or ""
    return ""


def _has_archive_tag(prod: dict) -> bool:
    tag_id = archive_tag_value_id()
    for param in prod.get("productParameters") or []:
        for value in param.get("parameterValues") or []:
            if value.get("parameterValueId") == tag_id:
                return True
    return False


def _product_row(prod: dict, with_tags: bool = False) -> dict:
    images = sorted(
        (_image_row(i) for i in prod.get("productImages") or []),
        key=lambda r: (r["priority"] is None, r["priority"]),
    )
    row = {
        "id": prod.get("productId"),
        "code": prod.get("productDisplayedCode") or "",
        "name": _product_name(prod),
        "images": images,
        "images_count": prod.get("productImagesCount", len(images)),
    }
    if with_tags:
        row["archived_tag"] = _has_archive_tag(prod)
    return row


def scan_page(page: int = 0, limit: int = 100) -> dict:
    """Jedna strona pelnego skanu: aktywne produkty bez tagu Archiwum."""
    data = search_products({
        "returnProducts": "active",
        "returnElements": SCAN_RETURN_ELEMENTS,
        "productParametersParams": [{
            "productParameterIds": {
                "productParameterIdsDisabled": [archive_tag_value_id()],
            },
        }],
        "resultsPage": page,
        "resultsLimit": limit,
    })
    return {
        "total": data.get("resultsNumberAll", 0),
        "pages": data.get("resultsNumberPage", 0),
        "page": page,
        "products": [_product_row(p) for p in data.get("results") or []],
    }


def _find_active(query: str, by_id: bool, limit: int) -> list[dict]:
    params = {
        "returnProducts": "active",
        "returnElements": SCAN_RETURN_ELEMENTS + ["parameters"],
        "resultsPage": 0,
        "resultsLimit": limit,
    }
    if by_id:
        params["productParams"] = [{"productId": int(query)}]
    else:
        params["containsCodePart"] = query
    data = search_products(params)
    rows = [_product_row(p, with_tags=True) for p in data.get("results") or []]
    for row in rows:
        row["deleted"] = False
    return rows


def _find_deleted(query: str, limit: int) -> list[dict]:
    """Szukanie w usunietych (archiwum IdoSell) - filtry serwera tu NIE
    dzialaja (by id zwraca 0 dla istniejacych, containsCodePart jest
    ignorowany i zwraca wszystko), wiec przegladamy strony i filtrujemy
    po stronie klienta (id / fragment kodu / fragment nazwy)."""
    q = query.lower()
    matches: list[dict] = []
    page = 0
    while True:
        data = search_products({
            "returnProducts": "deleted",
            "returnElements": SCAN_RETURN_ELEMENTS,
            "resultsPage": page,
            "resultsLimit": 100,
        })
        results = data.get("results") or []
        for prod in results:
            row = _product_row(prod)
            hit = (query.isdigit() and row["id"] == int(query)) \
                or q in row["code"].lower() or (q in row["name"].lower() and len(q) >= 3)
            if hit:
                row["archived_tag"] = False
                row["deleted"] = True
                matches.append(row)
                if len(matches) >= limit:
                    return matches
        page += 1
        if page >= data.get("resultsNumberPage", 0) or not results:
            return matches


def find_products(query: str, limit: int = 20) -> dict:
    """Reczne wyszukanie po ID lub kodzie (czesci kodu) - bez filtra tagu
    Archiwum; szuka najpierw w aktywnych, potem w usunietych (archiwum
    IdoSell). Kazdy wiersz ma archived_tag oraz deleted."""
    query = (query or "").strip()
    if not query:
        return {"total": 0, "products": []}

    # dla liczb najpierw ID, dopiero potem kod - inaczej containsCodePart
    # "1" zalewa wynikami czastkowych trafien
    if query.isdigit():
        rows = _find_active(query, by_id=True, limit=limit)
        if not rows:
            rows = _find_active(query, by_id=False, limit=limit)
    else:
        rows = _find_active(query, by_id=False, limit=limit)
    if not rows:
        rows = _find_deleted(query, limit)
    return {"total": len(rows), "products": rows}


def get_product_images(product_id: int) -> list[dict]:
    """Lista zdjec jednego produktu (active, w razie braku - deleted)."""
    data = search_products({
        "returnProducts": "active",
        "returnElements": ["code", "pictures", "pictures_count"],
        "productParams": [{"productId": int(product_id)}],
        "resultsPage": 0,
        "resultsLimit": 1,
    })
    results = data.get("results") or []
    if results:
        return _product_row(results[0])["images"]
    # filtr po ID nie dziala dla deleted - szukamy przegladem stron
    rows = _find_deleted(str(int(product_id)), limit=1)
    if rows:
        return rows[0]["images"]
    raise IdoSellError(f"Nie znaleziono produktu {product_id}")


def download_image(url: str) -> bytes:
    resp = requests.get(url, timeout=TIMEOUT)
    if resp.status_code != 200:
        raise IdoSellError(f"Nie udalo sie pobrac zdjecia ({resp.status_code})")
    return resp.content


# ---------------- FAZA 2: zapis (tylko zdjecia produktow) ----------------
# Wywolywane WYLACZNIE z endpointow UI po dwustopniowym potwierdzeniu.
# Przed kazdym uzyciem app.py robi fizyczny backup aktualnych zdjec.

def _collect_faults(node, where: str = "") -> list[str]:
    """Rekurencyjnie zbiera errors/faultString z odpowiedzi zapisu -
    207 Multi-Status wymaga sprawdzenia per element, nie tylko HTTP."""
    faults = []
    if isinstance(node, dict):
        err = node.get("errors")
        if isinstance(err, dict) and err.get("faultCode") not in (None, 0):
            slot = node.get("productImageNumber")
            ctx = f"{where} slot {slot}" if slot else where
            faults.append(f"{ctx}: [{err.get('faultCode')}] "
                          f"{err.get('faultString', '')}".strip())
        for key, val in node.items():
            if key != "errors":
                faults.extend(_collect_faults(val, where))
    elif isinstance(node, list):
        for item in node:
            faults.extend(_collect_faults(item, where))
    return faults


def apply_macro_setting() -> bool:
    """Czy IdoSell ma przetwarzac wgrywane zdjecia swoim makrem.
    Domyslnie False - wgrywamy gotowe kadry 1:1."""
    cfg = load_config() or {}
    return bool(cfg.get("apply_macro", False))


def put_product_images(product_id: int, images_b64: list[str],
                       shop_id: int = 1) -> dict:
    """Wgrywa galerie per slot: zdjecie i-te -> productImageNumber i.
    Nadpisuje istniejace sloty, nie kasuje slotow powyzej len(images_b64)
    (to robi delete_product_images). Rzuca IdoSellError przy bledzie
    ktoregokolwiek zdjecia (207)."""
    if not images_b64:
        raise IdoSellError("Pusta lista zdjec do wgrania")
    params = {
        "productsImagesSettings": {
            "productsImagesSourceType": "base64",
            "productsImagesApplyMacro": apply_macro_setting(),
        },
        "productsImages": [{
            "productIdent": {"productIdentType": "id",
                             "identValue": str(product_id)},
            "shopId": shop_id,
            "productImages": [{
                "productImageSource": b64,
                "productImageNumber": i,
                "productImagePriority": i,
                "deleteProductImage": False,
            } for i, b64 in enumerate(images_b64, 1)],
        }],
    }
    data = _request("PUT", "/products/images", params, timeout=300)
    faults = _collect_faults(data, f"produkt {product_id}")
    if faults:
        raise IdoSellError("PUT zdjec: " + "; ".join(faults)[:500])
    return data


def delete_product_images(product_id: int, image_ids: list[str],
                          shop_id: int = 1) -> dict:
    """Kasuje wskazane zdjecia (productImageId z searcha, np. '334_5.jpg')."""
    if not image_ids:
        return {}
    data = _post("/products/images/delete", [{
        "productId": int(product_id),
        "shopId": shop_id,
        "productImagesId": list(image_ids),
        "deleteAll": False,
    }])
    faults = _collect_faults(data, f"produkt {product_id}")
    if faults:
        raise IdoSellError("Kasowanie zdjec: " + "; ".join(faults)[:500])
    return data
