"""Most idosell-remover -> bg-remover: push zdjec na Allegro przez HTTP.

idosell-remover NIE dotyka Allegro - wola endpointy bg-removera (kontrakt v0.2,
WSPOLPRACA_allegro.md):
  A1  POST /api/allegro/push-from-shop/preview   {codes:[...]}      (read-only)
  A2  POST /api/allegro/push-from-shop/execute   {code, image_paths, all_variants, confirm}

Auth: bramka PIN bg-removera. bg jest kopia idosell-removera, wiec ten sam
mechanizm - logujemy sie POST /api/login {pin} i trzymamy cookie sesji (nie
potrzebujemy cookie_secret bg). Sciezki obrazow to ABSOLUTNE sciezki na tej
samej maszynie (bg czyta pliki wprost z idosell-remover/done/).

Config (poza gitem) - allegro_bridge_config.json:
  { "url": "http://127.0.0.1:5000", "pin": "123456" }
Brak/niepoprawny config => most nieaktywny (load_config() -> None).
"""
import json
from pathlib import Path

import requests

CONFIG_FILE = Path(__file__).parent / "allegro_bridge_config.json"
_session = None


class BridgeError(Exception):
    pass


def load_config():
    """{url, pin} albo None gdy brak/niepoprawny config (most nieaktywny)."""
    if not CONFIG_FILE.exists():
        return None
    try:
        c = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if c.get("url") and c.get("pin"):
            return {"url": str(c["url"]).rstrip("/"), "pin": str(c["pin"])}
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _login(sess, cfg):
    try:
        r = sess.post(f"{cfg['url']}/api/login", json={"pin": cfg["pin"]}, timeout=10)
    except requests.RequestException as e:
        raise BridgeError(f"bg-remover nieosiagalny ({cfg['url']}): {e}")
    if r.status_code != 200:
        raise BridgeError(f"Logowanie do bg-removera nieudane (PIN?) - HTTP {r.status_code}")


def _call(path, payload, timeout=180):
    cfg = load_config()
    if not cfg:
        raise BridgeError("Most Allegro nieskonfigurowany (allegro_bridge_config.json)")
    global _session
    if _session is None:
        _session = requests.Session()
        _login(_session, cfg)
    url = f"{cfg['url']}{path}"
    try:
        r = _session.post(url, json=payload, timeout=timeout)
        if r.status_code == 401:           # sesja wygasla -> zaloguj raz jeszcze
            _login(_session, cfg)
            r = _session.post(url, json=payload, timeout=timeout)
    except requests.RequestException as e:
        raise BridgeError(f"bg-remover nieosiagalny: {e}")
    if r.status_code >= 400:
        try:
            msg = r.json().get("error") or r.text[:200]
        except Exception:
            msg = (r.text or "")[:200]
        raise BridgeError(f"bg-remover HTTP {r.status_code}: {msg}")
    try:
        return r.json()
    except ValueError:
        raise BridgeError("bg-remover zwrocil niepoprawna odpowiedz (nie JSON)")


def preview(codes):
    """A1 - read-only. Zwraca {on_allegro:[{code,model,offers}], skip:[code]}."""
    return _call("/api/allegro/push-from-shop/preview", {"codes": list(codes)})


def execute(code, image_paths, all_variants=True, confirm=True):
    """A2 - zapis (wymaga confirm). Zwraca {results:[{id,ok,verified,error?}], rollback}."""
    return _call("/api/allegro/push-from-shop/execute", {
        "code": code, "image_paths": list(image_paths),
        "all_variants": bool(all_variants), "confirm": bool(confirm)})


def execute_batch(items, confirm=True):
    """Masowy push: items=[{code, image_paths}]. Zwraca {summary, results:[{code,
    model, status(ok|partial|error|skip), ok, failed, results:[{id,ok,verified}]}]}.
    Dluzszy timeout - bg leci sekwencyjnie po wariantach z backoffem 429."""
    return _call("/api/allegro/push-from-shop/execute-batch",
                 {"items": list(items), "confirm": bool(confirm)}, timeout=600)


def rollback(code, confirm=True):
    """Cofa CALY push modelu (wszystkie warianty). Zwraca {ok, failed, results}."""
    return _call("/api/allegro/push-from-shop/rollback",
                 {"code": code, "confirm": bool(confirm)})
