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

# wyszukiwarka producenta - dopasowanie PO KODZIE (slug koduje tylko zwierzaka,
# nie kolor/sezon, wiec sama nazwa bywa myląca - patrz pamiec affenzahn-podmiana)
SEARCH = "https://www.affenzahn.com/en-eu/search"
_PAGE_CODE_RE = re.compile(r'sku:"(\d{4,5}-\d{5})-\d+"')   # kod artykulu na stronie
_PAGE_CODE_ALT = re.compile(r'\b(\d{4,5}-\d{5})-\d{3}\b')
_SLUG_RE = re.compile(r'/p/([a-z0-9-]+)/')                 # slugi z wynikow szukajki


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


def _fetch_html(slug: str) -> str:
    try:
        r = requests.get(PAGE.format(slug=slug), headers=UA, timeout=30)
    except requests.RequestException as e:
        raise AffenzahnError(f"Blad sieci: {e}")
    if r.status_code == 404:
        raise AffenzahnError(f"Strona producenta nie istnieje (slug '{slug}')")
    if r.status_code != 200:
        raise AffenzahnError(f"HTTP {r.status_code} dla slug '{slug}'")
    return r.text


def _images_from_html(html: str) -> dict:
    out = {}
    for u in _IMG_RE.findall(html):
        master = _master(u)
        m = _KIND_RE.search(master)
        if not m:
            continue
        kind = m.group("kind").lower()
        if kind.startswith("size") or kind.startswith("usp"):
            continue
        out.setdefault(kind, master)
    return out


def scrape_images(slug: str) -> dict:
    """Zwraca {kind: master_url} dla strony produktu (kind: front/top/sole/
    right/left/back/detail_NN/model_NN). Pomija size/usp_*."""
    return _images_from_html(_fetch_html(slug))


def page_code(html: str) -> str | None:
    """Kod artykulu producenta widoczny na stronie (baza bez sufiksu rozmiaru)."""
    m = _PAGE_CODE_RE.search(html) or _PAGE_CODE_ALT.search(html)
    return m.group(1) if m else None


def _norm_code(code) -> tuple:
    """Kod jako krotka intow per segment - znosi zera wiodace (sklep trzyma
    '844-30119', producent '00844-30119' = to samo)."""
    return tuple(int(p) for p in re.split(r'[-\s]+', (code or "").strip())
                 if p.isdigit())


def resolve_slug_by_code(code: str) -> str | None:
    """Slug producenta, ktorego strona ma DOKLADNIE ten kod (po segmentach, bez
    zer wiodacych). Szuka przez wyszukiwarke producenta po pelnym kodzie - bo
    slug koduje tylko zwierzaka, nie kolor/sezon. None gdy brak trafienia."""
    want = _norm_code(code)
    if not want:
        return None
    try:
        r = requests.get(SEARCH, params={"q": code}, headers=UA, timeout=20)
    except requests.RequestException as e:
        raise AffenzahnError(f"Blad sieci (search {code}): {e}")
    if r.status_code != 200:
        return None
    seen, cands = set(), []
    for s in _SLUG_RE.findall(r.text):
        if s not in seen:
            seen.add(s)
            cands.append(s)
    for slug in cands[:8]:
        try:
            pc = page_code(_fetch_html(slug))
        except AffenzahnError:
            continue
        if pc and _norm_code(pc) == want:
            return slug
    return None


def gallery(product_id) -> list[dict]:
    """Uporzadkowana lista ujec do importu: [{kind, url, studio, fashion, pos}].
    studio=True -> ich alfa + nasz cien; studio=False (model_*) -> 1:1 fashion.
    GUARD: kod na stronie musi zgadzac sie z kodem z mapowania (== kod sklepu) -
    inaczej slug wskazuje inny kolor/sezon (rozjazd) i import jest wstrzymany."""
    entry = load_mapping().get(str(product_id))
    if not entry or not entry.get("slug"):
        raise AffenzahnError(f"Brak mapowania dla produktu {product_id}")
    slug = entry["slug"]
    html = _fetch_html(slug)
    want, pc = entry.get("code"), page_code(html)
    if want and pc and _norm_code(pc) != _norm_code(want):
        raise AffenzahnError(
            f"Rozjazd kodu dla {product_id}: strona '{slug}' ma {pc}, "
            f"oczekiwano {want}. Import wstrzymany - popraw slug w mapowaniu.")
    imgs = _images_from_html(html)
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
