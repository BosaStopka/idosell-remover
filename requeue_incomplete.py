"""Ponowne zakolejkowanie CALYCH niekompletnych produktow (ofiary pauzy).

Po pauzie kolejki czesc produktow ma w done/ tylko czesc zdjec z plan.json.
W Studio wygladaja na kompletne ("2/2 gotowych"), co myli. Zamiast doszywac
brakujace pojedynczo, odtwarzamy CALY produkt od nowa (decyzja uzytkownika -
jednolity stan, latwiejsza nawigacja): wolamy istniejacy endpoint
/api/idosell/products/{id}/process z decyzjami z plan.json. Endpoint sam
kasuje stare zadania (_reset_product_jobs) i kolejkuje wszystko na nowo.

Kolejka jest najpierw PAUZOWANA - 131+ zadan BiRefNet nie ruszy samo.
Uruchom przy DZIALAJACYM serwerze (port 5001).
"""
import hashlib
import json
import re
import sys
import time
from pathlib import Path

import requests

import pipeline

BASE = Path(__file__).resolve().parent
DONE = BASE / "done"
API = "http://127.0.0.1:5001"
RECENT_CUTOFF = "2026-06-20"   # zero-done starsze niz to pomijamy (stare testy)


def auth_cookie() -> dict:
    cfg = json.loads((BASE / "app_config.json").read_text(encoding="utf-8"))
    val = hashlib.sha256((cfg["pin"] + cfg["cookie_secret"]).encode()).hexdigest()
    return {"bs_auth_ido": val}


def options_form() -> dict:
    out = {}
    for k, v in pipeline.DEFAULTS.items():
        out[k] = "1" if v is True else ("0" if v is False else str(v))
    return out


def scan_incomplete():
    """Zwraca liste (pid, have, planned, decisions, saved_at, visible)."""
    targets = []
    for d in sorted(DONE.iterdir(), key=lambda p: p.name):
        if not d.is_dir():
            continue
        pf = d / "plan.json"
        if not pf.exists():
            continue
        try:
            plan = json.loads(pf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        decs = plan.get("decisions") or []
        planned = {int(x["index"]) for x in decs if x.get("index") is not None}
        if not planned:
            continue
        have = set()
        for f in d.glob("*.jpg"):
            m = re.match(r"^(.+?)_(\d+)\.jpg$", f.name)
            if m and m.group(1) == d.name:
                have.add(int(m.group(2)))
        missing = planned - have
        if not missing:
            continue
        targets.append({
            "pid": d.name, "have": len(have), "planned": len(planned),
            "decisions": decs, "saved_at": plan.get("saved_at", ""),
            "visible": len(have) > 0,
        })
    return targets


def main():
    do = "--run" in sys.argv
    cookies = auth_cookie()
    targets = scan_incomplete()

    selected, skipped_old = [], []
    for t in targets:
        if t["visible"] or t["saved_at"] >= RECENT_CUTOFF:
            selected.append(t)
        else:
            skipped_old.append(t)

    print("== DO PONOWNEGO ZAKOLEJKOWANIA ==")
    for t in selected:
        tag = "widoczny" if t["visible"] else "zero-done(swiezy)"
        print(f"  {t['pid']}: {t['have']}/{t['planned']} -> cale {t['planned']} "
              f"[{tag}, plan {t['saved_at']}]")
    print(f"  RAZEM: {len(selected)} produktow, "
          f"{sum(t['planned'] for t in selected)} zdjec do obrobki")
    if skipped_old:
        print("== POMINIETE (stare, zero-done) ==")
        print("  " + ", ".join(f"{t['pid']}({t['saved_at'][:10]})"
                                for t in skipped_old))
    if not do:
        print("\n[DRY-RUN] uruchom z --run zeby wykonac")
        return

    # 1) pauza
    r = requests.post(f"{API}/api/queue/pause", json={"paused": True},
                      cookies=cookies, timeout=15)
    print(f"\nPauza kolejki: {r.json()}")

    # 2) re-queue per produkt
    opts = options_form()
    ok = err = 0
    for t in selected:
        form = dict(opts)
        form["decisions"] = json.dumps(t["decisions"], ensure_ascii=False)
        try:
            resp = requests.post(
                f"{API}/api/idosell/products/{t['pid']}/process",
                data=form, cookies=cookies, timeout=180)
            j = resp.json()
            nj = len(j.get("jobs", []))
            ne = len(j.get("errors", []))
            kept = j.get("kept", 0)
            print(f"  {t['pid']}: HTTP {resp.status_code} "
                  f"queued={nj} kept={kept} errors={ne}"
                  + (f" {j['errors'][:2]}" if ne else ""))
            ok += 1
        except Exception as e:
            print(f"  {t['pid']}: WYJATEK {e}")
            err += 1
        time.sleep(0.3)   # lekki throttle (klient IdoSell tez ma backoff)

    # 3) potwierdz pauze
    r = requests.get(f"{API}/api/system", cookies=cookies, timeout=15)
    print(f"\nGotowe: {ok} produktow przetworzonych, {err} bledow.")
    print(f"System: {r.json()}  (paused_low_ram to inne - reczna pauza zostaje)")
    print("Kolejka ZOSTAJE zapauzowana - wznow w UI ('Wznow') kiedy chcesz.")


if __name__ == "__main__":
    main()
