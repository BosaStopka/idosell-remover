# -*- coding: utf-8 -*-
"""Testy regresji (pytest) - lapia to, co dotad walidowalem recznie:
  - skladnia/bilans JS w index.html (blad = CZARNE TLO - najwazniejszy test),
  - obecnosc kluczowych funkcji UI,
  - endpointy serwera (auth gate, filtry /api/jobs, nowe endpointy) - jesli
    serwer na 5001 dziala (inaczej skip),
  - czyste funkcje pipeline (smooth_sole_edge, compose) i affenzahn_client.

Uruchom: python -m pytest tests/ -q
"""
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
INDEX = ROOT / "static" / "index.html"
API = "http://127.0.0.1:5001"


def _js():
    html = INDEX.read_text(encoding="utf-8")
    return "\n".join(re.findall(
        r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S))


# ---------- UI: skladnia (regresja "czarnego tla") ----------
def test_ui_backticks_balanced():
    assert _js().count("`") % 2 == 0, "nieparzyste backticki - urwany template literal"


def test_ui_braces_parens_balanced():
    js = _js()
    assert js.count("{") == js.count("}"), "niezbilansowane klamry { }"
    assert js.count("(") == js.count(")"), "niezbilansowane nawiasy ( )"


@pytest.mark.parametrize("fn", [
    "function render(", "async function refresh(", "function studioJobsUrl(",
    "function toggleStudioAll(", "async function smoothSole(",
    "async function fullBleedFromMask(", "async function importAffenzahn(",
    "async function reprocessPhoto(", "async function toPriority(",
    "function studioToPos2(", "function edUpdateCursor(",
    "function edUndoOnce(",
])
def test_ui_key_functions_present(fn):
    assert fn in _js(), f"brak funkcji UI: {fn}"


# ---------- pipeline: czyste funkcje (bez BiRefNet) ----------
def test_smooth_sole_edge():
    import numpy as np
    from PIL import Image
    import pipeline
    a = np.zeros((200, 200), "uint8")
    a[40:170, 50:150] = 255          # prostokat = "but"
    a[168:172, 60:62] = 255          # wystajacy "zabek"
    out = pipeline.smooth_sole_edge(Image.fromarray(a, "L"))
    assert out.size == (200, 200) and out.mode == "L"


def test_compose_returns_canvas():
    import numpy as np
    from PIL import Image
    import pipeline
    arr = np.zeros((200, 200, 4), "uint8")
    arr[40:160, 50:150, :3] = (180, 120, 60)
    arr[40:160, 50:150, 3] = 255
    rgba = Image.fromarray(arr, "RGBA")
    out = pipeline.compose(rgba, {**pipeline.DEFAULTS, "shadow": False, "size": 400})
    assert out.size == (400, 400)


def test_compose_small_source_fills_to_padding():
    # Opcja 1 (spojnosc galerii): male zrodlo TEZ wypelnia kadr do ~padding
    # (max_upscale juz nie ucina rozmiaru, tylko UPSCALE_CEILING). Regresja: ze
    # cala galeria ma rowny rozmiar produktu niezaleznie od rozdzielczosci.
    import numpy as np
    from PIL import Image
    import pipeline
    arr = np.zeros((200, 200, 4), "uint8")
    arr[75:125, 75:125, :3] = (150, 90, 40)   # maly produkt 50x50
    arr[75:125, 75:125, 3] = 255
    rgba = Image.fromarray(arr, "RGBA")
    out = pipeline.compose(rgba, {**pipeline.DEFAULTS, "shadow": False, "size": 400})
    a = np.asarray(out.convert("RGB"))
    nw = (a < 248).any(axis=2)
    ys, xs = np.where(nw)
    fill = max(xs.max() - xs.min() + 1, ys.max() - ys.min() + 1) / 400
    assert fill > 0.85, f"male zrodlo ma wypelnic ~padding (0.90), jest {fill:.0%}"


def test_compose_from_returns_canvas():
    # compose_from sklada maske 1:1 (BEZ globalnego rozmycia - to rozmazywalo
    # kontur calego produktu; wygladzenie robi teraz pedzel w edytorze lokalnie).
    import numpy as np
    from PIL import Image
    import pipeline
    rgb = Image.new("RGB", (300, 300), (150, 90, 40))
    a = np.zeros((300, 300), "uint8")
    a[60:240, 60:240] = 255
    out = pipeline.compose_from(rgb, Image.fromarray(a, "L"),
                                {"shadow": False, "size": 300})
    assert out.size == (300, 300)


def test_compose_editor_shadow_not_under_product():
    # EDYTOR: cien rysowany tylko tam gdzie NIE ma produktu. Przywrocone biale
    # tlo (produkt) nad cieniem -> na bialym kadrze znika (nie ciemny placek).
    import numpy as np
    from PIL import Image
    import pipeline
    rgb = Image.new("RGB", (400, 400), (245, 245, 245))   # jasne (jak tlo studyjne)
    a = np.zeros((400, 400), "uint8")
    a[120:300, 120:280] = 255                              # "przywrocony" blok jasny
    out = pipeline.compose_from(rgb, Image.fromarray(a, "L"), {"size": 400})
    arr = np.asarray(out.convert("L"))
    # obszar przywroconego jasnego bloku ma zostac jasny (cien pod nim usuniety),
    # a nie zaciemniony przez kaluze
    assert arr[150:280, 140:260].min() > 200, "jasny przywrocony obszar nie moze miec ciemnego cienia pod spodem"


def test_clean_sole_studio_vs_fashion():
    import numpy as np
    from PIL import Image
    import pipeline
    # studyjne (biale rogi) + jasny midsole z czerwonawa plama -> CZYSCI
    a = np.full((300, 300, 3), 255, "uint8")
    a[170:250, 40:260] = (205, 205, 208)
    a[200:230, 80:160] = (188, 148, 138)
    out = np.asarray(pipeline.clean_sole_stains(Image.fromarray(a, "RGB")))
    assert out.shape == a.shape and not np.array_equal(out, a)
    # fashion (rogi NIE biale = kolorowe tlo) -> NO-OP (guard)
    b = np.full((300, 300, 3), (90, 120, 60), "uint8")
    b[170:250, 40:260] = (205, 205, 208)
    outb = np.asarray(pipeline.clean_sole_stains(Image.fromarray(b, "RGB")))
    assert np.array_equal(outb, b), "nie-studyjne powinno byc no-op"


# ---------- affenzahn_client ----------
def test_affenzahn_master_strips_size():
    import affenzahn_client as af
    u = "https://cdn.shopify.com/s/files/1/0/files/img_front_abcd1234_1240x1240.png?v=1"
    assert "_1240x1240" not in af._master(u)
    assert af._master(u).endswith("img_front_abcd1234.png?v=1")


# ---------- serwer (skip jesli nie dziala) ----------
def _cookie():
    c = json.loads((ROOT / "app_config.json").read_text(encoding="utf-8"))
    return hashlib.sha256((c["pin"] + c["cookie_secret"]).encode()).hexdigest()


@pytest.fixture(scope="module")
def server():
    requests = pytest.importorskip("requests")
    try:
        requests.get(f"{API}/api/queue", timeout=4)
    except Exception:
        pytest.skip("serwer 5001 nie odpowiada - pomijam testy API")
    return requests


def test_auth_gate(server):
    r = server.get(f"{API}/api/queue", timeout=8)
    assert r.status_code in (401, 403), "brama PIN powinna blokowac bez cookie"


def test_queue_shape(server):
    r = server.get(f"{API}/api/queue", cookies={"bs_auth_ido": _cookie()}, timeout=8)
    d = r.json()
    assert {"queued", "processing", "paused"} <= set(d)


def test_jobs_light_subset_of_all(server):
    ck = {"bs_auth_ido": _cookie()}
    full = server.get(f"{API}/api/jobs", cookies=ck, timeout=30).json()
    light = server.get(f"{API}/api/jobs?light=1", cookies=ck, timeout=15).json()
    active = server.get(f"{API}/api/jobs?active=1", cookies=ck, timeout=15).json()
    assert len(light) <= len(full), "light nie moze byc wiekszy niz pelna lista"
    assert all(j["status"] in ("queued", "processing") for j in active)
    assert all(not (j.get("source") == "idosell" and j.get("status") == "done")
               for j in light), "light nie powinien zawierac idosell+done"


@pytest.mark.parametrize("ep", ["smooth-sole", "fullbleed-mask", "to-priority"])
def test_new_job_endpoints_registered(server, ep):
    r = server.post(f"{API}/api/jobs/__nope__/{ep}",
                    cookies={"bs_auth_ido": _cookie()}, timeout=8)
    assert r.status_code == 404 and "error" in r.json(), \
        f"endpoint {ep} powinien byc zarejestrowany (JSON 404, nie route-404)"
