"""Klient Affenzahn (producent, sklep Shopify) - pobranie master-zdjec wg
mapowania nasz product_id -> slug en-eu. Architektura jak allegro_client/
idosell_client (CLAUDE.md): tu TYLKO sieciowka/URL-e, kompozycja w app.py.

Fakty (patrz pamiec affenzahn-podmiana-zdjec):
- strona /en-eu/p/{slug}/ w surowym HTML zawiera wszystkie URL-e cdn.shopify
  (master bez sufiksu _WxH + warianty). Bez JS.
- master (2000px) = URL bez sufiksu _WxH.
- studyjne ujecia: przezroczyste PNG (uzywamy ICH alfy, bez BiRefNet).
  lifestyle: model_* (JPG, 1:1 fashion). size/usp_* (tabela rozmiarow,
  marketing) - POMIJAMY.
"""
import json
import re
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
MAPPING_FILE = BASE / "experiments" / "_affenzahn" / "mapping_proposed.json"
PAGE = "https://www.affenzahn.com/en-eu/p/{slug}/"
UA = {"User-Agent": "Mozilla/5.0"}

# kolejnosc galerii: hero profil #1, lifestyle (fashion) #2, FRONT #3, reszta
GALLERY_ORDER = ["right", "model_01", "front", "left", "top", "back", "sole",
                 "detail_01", "detail_02", "detail_03", "detail_04", "model_02",
                 "model_03"]

_IMG_RE = re.compile(r'https://cdn\.shopify\.com/s/files/[^\s"\\)?]+\.(?:png|jpg)',
                     re.I)
_KIND_RE = re.compile(
    r'img_(?P<kind>.+?)_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}', re.I)
_SIZE_SUFFIX = re.compile(r'_\d+x\d+(\.[a-zA-Z]+)(\?|$)')


class AffenzahnError(Exception):
    pass


def load_mapping() -> dict:
    if not MAPPING_FILE.exists():
        return {}
    return json.loads(MAPPING_FILE.read_text(encoding="utf-8"))


def slug_for(product_id) -> str | None:
    entry = load_mapping().get(str(product_id))
    return entry.get("slug") if entry else None


def download(url: str) -> bytes:
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    return r.content


def _master(url: str) -> str:
    return _SIZE_SUFFIX.sub(r'\1\2', url)


def scrape_images(slug: str) -> dict:
    """Zwraca {kind: master_url} dla strony produktu (kind: front/top/sole/
    right/left/back/detail_NN/model_NN). Pomija size/usp_*."""
    try:
        r = requests.get(PAGE.format(slug=slug), headers=UA, timeout=30)
    except requests.RequestException as e:
        raise AffenzahnError(f"Blad sieci: {e}")
    if r.status_code == 404:
        raise AffenzahnError(f"Strona producenta nie istnieje (slug '{slug}')")
    if r.status_code != 200:
        raise AffenzahnError(f"HTTP {r.status_code} dla slug '{slug}'")
    out = {}
    for u in _IMG_RE.findall(r.text):
        master = _master(u)
        m = _KIND_RE.search(master)
        if not m:
            continue
        kind = m.group("kind").lower()
        if kind.startswith("size") or kind.startswith("usp"):
            continue
        out.setdefault(kind, master)
    return out


def gallery(product_id) -> list[dict]:
    """Uporzadkowana lista ujec do importu: [{kind, url, studio, fashion, pos}].
    studio=True -> ich alfa + nasz cien; studio=False (model_*) -> 1:1 fashion."""
    slug = slug_for(product_id)
    if not slug:
        raise AffenzahnError(f"Brak mapowania dla produktu {product_id}")
    imgs = scrape_images(slug)
    if not imgs:
        raise AffenzahnError(f"Brak zdjec na stronie (slug '{slug}')")
    ordered = [k for k in GALLERY_ORDER if k in imgs]
    # ujecia spoza znanej kolejnosci (np. nowy detail) - dorzuc na koniec
    ordered += [k for k in sorted(imgs) if k not in ordered]
    out = []
    for pos, kind in enumerate(ordered, 1):
        is_model = kind.startswith("model")
        out.append({"kind": kind, "url": imgs[kind], "studio": not is_model,
                    "fashion": is_model, "pos": pos})
    return out
