"""Klient Allegro REST API.

Odczyt: oferty, zdjecia. Zapis (FAZA 2): wylacznie dwie operacje -
upload zdjecia na serwer Allegro i PATCH listy zdjec oferty wg planu
zatwierdzonego przez uzytkownika w UI. Zadnych innych zmian ofert.

Scope write wymaga ponownej autoryzacji (przycisk w UI).
Konfiguracja: allegro_config.json (client_id, client_secret z apps.developer.allegro.pl)
Token: allegro_token.json (tworzony automatycznie po autoryzacji)
"""
import base64
import json
import time
from pathlib import Path

import requests

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "allegro_config.json"
TOKEN_FILE = BASE / "allegro_token.json"

AUTH_URL = "https://allegro.pl/auth/oauth"
API_URL = "https://api.allegro.pl"
UPLOAD_URL = "https://upload.allegro.com"
SCOPE_READ = "allegro:api:sale:offers:read"
SCOPE_WRITE = "allegro:api:sale:offers:read allegro:api:sale:offers:write"
TIMEOUT = 30


class AllegroError(Exception):
    pass


def load_config() -> dict | None:
    if not CONFIG_FILE.exists():
        return None
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if cfg.get("client_id") and cfg.get("client_secret"):
            return cfg
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _basic_auth(cfg: dict) -> dict:
    raw = f"{cfg['client_id']}:{cfg['client_secret']}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


def _load_token() -> dict | None:
    if not TOKEN_FILE.exists():
        return None
    try:
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _save_token(data: dict):
    data["expires_at"] = time.time() + int(data.get("expires_in", 43200)) - 120
    TOKEN_FILE.write_text(json.dumps(data), encoding="utf-8")


def start_device_flow(write: bool = False) -> dict:
    """Krok 1: zwraca user_code, verification_uri, device_code, interval."""
    cfg = load_config()
    if not cfg:
        raise AllegroError("Brak konfiguracji (allegro_config.json)")
    resp = requests.post(
        f"{AUTH_URL}/device",
        headers=_basic_auth(cfg),
        data={"client_id": cfg["client_id"],
              "scope": SCOPE_WRITE if write else SCOPE_READ},
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise AllegroError(f"Blad device flow ({resp.status_code}): {resp.text[:200]}")
    return resp.json()


def poll_device_token(device_code: str) -> str:
    """Krok 2: jedna proba pobrania tokena. Zwraca status:
    'ok' / 'pending' / komunikat bledu."""
    cfg = load_config()
    if not cfg:
        return "Brak konfiguracji"
    resp = requests.post(
        f"{AUTH_URL}/token",
        headers=_basic_auth(cfg),
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
        },
        timeout=TIMEOUT,
    )
    if resp.status_code == 200:
        _save_token(resp.json())
        return "ok"
    err = resp.json().get("error", "") if resp.headers.get(
        "content-type", "").startswith("application/json") else ""
    if err in ("authorization_pending", "slow_down"):
        return "pending"
    return f"Blad autoryzacji: {err or resp.text[:200]}"


def _refresh(token: dict) -> dict | None:
    cfg = load_config()
    if not cfg or not token.get("refresh_token"):
        return None
    resp = requests.post(
        f"{AUTH_URL}/token",
        headers=_basic_auth(cfg),
        data={
            "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
        },
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        return None
    _save_token(resp.json())
    return _load_token()


def get_access_token() -> str | None:
    token = _load_token()
    if not token:
        return None
    if time.time() >= token.get("expires_at", 0):
        token = _refresh(token)
        if not token:
            return None
    return token.get("access_token")


def is_authorized() -> bool:
    return get_access_token() is not None


def has_write_scope() -> bool:
    token = _load_token()
    return bool(token and "offers:write" in token.get("scope", ""))


def api_get(path: str, params: dict | None = None) -> dict:
    """Wylacznie GET - ten klient nie wykonuje zadnych zmian na Allegro."""
    token = get_access_token()
    if not token:
        raise AllegroError("Brak autoryzacji - polacz konto Allegro")
    resp = requests.get(
        f"{API_URL}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.allegro.public.v1+json",
        },
        params=params or {},
        timeout=TIMEOUT,
    )
    if resp.status_code == 401:
        raise AllegroError("Token wygasl - polacz konto ponownie")
    if resp.status_code != 200:
        raise AllegroError(f"Allegro API {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _offer_row(o: dict) -> dict:
    return {
        "id": o.get("id"),
        "name": o.get("name"),
        "image": (o.get("primaryImage") or {}).get("url"),
        "status": (o.get("publication") or {}).get("status"),
        "stock": (o.get("stock") or {}).get("available"),
        "sku": (o.get("external") or {}).get("id"),
    }


def list_offers(phrase: str = "", sku: str = "", offer_id: str = "",
                status: str = "", offset: int = 0, limit: int = 20) -> dict:
    # wyszukiwanie po numerze oferty: bezposrednie pobranie szczegolow
    if offer_id:
        try:
            o = api_get(f"/sale/product-offers/{offer_id}")
        except AllegroError:
            return {"offers": [], "total": 0}
        images = o.get("images") or []
        first = images[0] if images else None
        url = first.get("url") if isinstance(first, dict) else first
        return {"offers": [{
            "id": o.get("id"),
            "name": o.get("name"),
            "image": url,
            "status": (o.get("publication") or {}).get("status"),
            "stock": (o.get("stock") or {}).get("available"),
            "sku": (o.get("external") or {}).get("id"),
        }], "total": 1}

    params = {
        "offset": offset,
        "limit": limit,
        "sort": "-stats.watchersCount",
    }
    if phrase:
        params["name"] = phrase
    if sku:
        params["external.id"] = sku
    if status:
        params["publication.status"] = status
    data = api_get("/sale/offers", params)
    offers = [_offer_row(o) for o in data.get("offers", [])]
    return {"offers": offers, "total": data.get("totalCount", len(offers))}


def get_offer_images(offer_id: str) -> list[str]:
    data = api_get(f"/sale/product-offers/{offer_id}")
    urls = []
    for img in data.get("images", []):
        url = img.get("url") if isinstance(img, dict) else img
        if url:
            urls.append(url)
    return urls


def thumb_url(url: str, size: str = "s128") -> str:
    """Wariant miniaturki allegroimg (do szybkiego skanu tla)."""
    import re
    if "/original/" in url:
        return url.replace("/original/", f"/{size}/")
    return re.sub(r"(allegroimg\.com)/s\d+(?:x\d+)?/", rf"\1/{size}/", url)


def original_url(url: str) -> str:
    """Zamien wariant rozmiarowy allegroimg na /original/."""
    import re
    return re.sub(r"(allegroimg\.com)/s\d+(?:x\d+)?/", r"\1/original/", url)


def download_image(url: str) -> bytes:
    resp = requests.get(original_url(url), timeout=TIMEOUT)
    if resp.status_code != 200:
        raise AllegroError(f"Nie udalo sie pobrac zdjecia ({resp.status_code})")
    return resp.content


# ---------------- FAZA 2: zapis (tylko zdjecia ofert) ----------------

def _write_token() -> str:
    if not has_write_scope():
        raise AllegroError(
            "Token bez uprawnien zapisu - polacz konto ponownie "
            "z uprawnieniami do edycji ofert")
    token = get_access_token()
    if not token:
        raise AllegroError("Brak autoryzacji - polacz konto Allegro")
    return token


def upload_image(data: bytes) -> str:
    """Wgrywa zdjecie na serwer Allegro, zwraca URL (wazny do podpiecia
    pod oferte). To NIE zmienia zadnej oferty."""
    resp = requests.post(
        f"{UPLOAD_URL}/sale/images",
        headers={
            "Authorization": f"Bearer {_write_token()}",
            "Accept": "application/vnd.allegro.public.v1+json",
            "Content-Type": "image/jpeg",
        },
        data=data,
        timeout=120,
    )
    if resp.status_code not in (200, 201):
        raise AllegroError(f"Upload zdjecia: {resp.status_code} {resp.text[:300]}")
    body = resp.json() if resp.content else {}
    url = body.get("location") or resp.headers.get("Location")
    if not url:
        raise AllegroError("Upload OK, ale brak URL zdjecia w odpowiedzi")
    return url


def get_offer_raw_images(offer_id: str) -> list:
    """Surowa lista images (w oryginalnym ksztalcie API) - do backupu
    i do zbudowania PATCHa w tym samym formacie."""
    data = api_get(f"/sale/product-offers/{offer_id}")
    return data.get("images", [])


def _strip_size(url: str) -> str:
    """Normalizacja URL allegroimg (rozne warianty rozmiarowe = to samo zdjecie)."""
    import re
    return re.sub(r"(allegroimg\.com)/(?:original|s\d+(?:x\d+)?)/", r"\1/", url or "")


def swap_description_images(description: dict, mapping: dict) -> tuple:
    """Podmienia zdjecia w opisie oferty wg mappingu stary->nowy URL
    (None = zdjecie usuniete z galerii -> usun tez z opisu, bo Allegro
    wymaga, by zdjecia w opisie nalezaly do oferty).
    Zwraca (nowy_opis_lub_None, liczba_podmian, liczba_usuniec)."""
    if not description or not description.get("sections"):
        return None, 0, 0
    norm_map = {_strip_size(k): v for k, v in mapping.items()}
    swapped = removed = 0
    new_sections = []
    for section in description["sections"]:
        new_items = []
        for item in section.get("items", []):
            if item.get("type") == "IMAGE":
                key = _strip_size(item.get("url", ""))
                if key in norm_map:
                    new_url = norm_map[key]
                    if new_url is None:
                        removed += 1
                        continue  # usuniete z galerii -> wypada z opisu
                    if _strip_size(new_url) != key:
                        item = {**item, "url": new_url}
                        swapped += 1
            new_items.append(item)
        if new_items:
            new_sections.append({**section, "items": new_items})
    if not swapped and not removed:
        return None, 0, 0
    return {**description, "sections": new_sections}, swapped, removed


def patch_offer_images(offer_id: str, images: list,
                       suppress_product_images: bool = True,
                       description: dict | None = None) -> dict:
    """JEDYNA operacja zmieniajaca oferte: podmiana zdjec galerii
    (oraz opcjonalnie opisu - podmiana tych samych zdjec w tresci).
    Wywolywana wylacznie po jawnym potwierdzeniu w UI.

    suppress_product_images: zdjecia produktu katalogowego sa automatycznie
    dolaczane do galerii oferty i licza sie do limitu 16; pusta tablica
    product.images to wylacza (github.com/allegro/allegro-api issue 4691).
    Domyslnie WLACZONE - galeria ma zawierac wylacznie nasze zdjecia.
    """
    payload = {"images": images}
    if description is not None:
        payload["description"] = description
    if suppress_product_images:
        detail = api_get(f"/sale/product-offers/{offer_id}")
        product_set = detail.get("productSet") or []
        if product_set:
            payload["productSet"] = [
                {"product": {"id": (p.get("product") or {}).get("id"),
                             "images": []}}
                for p in product_set
                if (p.get("product") or {}).get("id")
            ]
    resp = requests.patch(
        f"{API_URL}/sale/product-offers/{offer_id}",
        headers={
            "Authorization": f"Bearer {_write_token()}",
            "Accept": "application/vnd.allegro.public.v1+json",
            "Content-Type": "application/vnd.allegro.public.v1+json",
        },
        json=payload,
        timeout=60,
    )
    if resp.status_code not in (200, 202):
        raise AllegroError(
            f"Zmiana oferty odrzucona: {resp.status_code} {resp.text[:400]}")
    return resp.json() if resp.content else {}
