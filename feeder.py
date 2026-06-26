"""Feeder kolejki idosell - DRIP-FEED. Niezalezny od sesji Claude
(Start-Process detached, przezywa zamkniecie sesji).

Cel (zyczenie usera 2026-06-25): to JA (skrypt) dopuszczam kolejne zdjecia,
utrzymujac kolejke w pasmie LOW..HIGH - zeby NIE wrzucac za duzo naraz (chroni
CPU i przed dlawieniem serwera / czarnym tlem). Kolejnosc po sezonach:
najpierw dokoncz lato, potem wiosna, potem jesien.

Logika co INTERVAL s:
  - jesli kolejka PAUZOWANA -> nie dosypuj (czekaj az user wznowi),
  - jesli queued >= LOW -> jeszcze duzo, nie dosypuj,
  - inaczej dosyp produkty (bulk-process, paczki po CHUNK) az queued ~ HIGH,
    pomijajac juz active/processed (snapshot kandydatow z startu + biezacy
    active_pids).
Loguje do feeder.log. Start: Start-Process -WindowStyle Hidden.
"""
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
API = "http://127.0.0.1:5001"
LOW, HIGH = 120, 280       # utrzymuj kolejke w tym pasmie
INTERVAL = 45              # s
CHUNK = 6
SEASONS = ["lato", "wiosna", "jesien"]   # kolejnosc dosypywania (zima pomijamy)
LOG = BASE / "feeder.log"


# ---- czysta logika decyzji (testowalna bez serwera/configu) ----
def should_feed(q, low=LOW) -> bool:
    """Czy dosypywac teraz? Tylko gdy serwer odpowiedzial, NIE pauza i kolejka
    spadla ponizej dolnego progu (LOW). Inaczej czekamy."""
    if not q:
        return False
    if q.get("paused"):
        return False
    return q.get("queued", 0) < low


def take_chunk(cand, cursor, act, chunk_size=CHUNK):
    """Wybierz nastepna paczke <= chunk_size, pomijajac produkty juz active
    (w trakcie/w kolejce). Zwraca (chunk, nowy_cursor). Czysta - bez I/O."""
    chunk = []
    while len(chunk) < chunk_size and cursor < len(cand):
        pid = cand[cursor]
        cursor += 1
        if str(pid) not in act:
            chunk.append(pid)
    return chunk, cursor


def select_candidates(products_by_season, act, seasons=SEASONS):
    """Z mapy {sezon: [produkty]} zbuduj liste ID do dosypania: tylko
    nieprzetworzone i niewyslane, nie active, bez duplikatow; kolejnosc wg
    'seasons'. Czysta - logika z build_candidates bez HTTP."""
    seen, cand = set(), []
    for s in seasons:
        for p in products_by_season.get(s, []):
            pid = str(p["id"])
            if (not p.get("processed") and not p.get("executed")
                    and pid not in act and pid not in seen):
                cand.append(int(p["id"]))
                seen.add(pid)
    return cand


def _auth_header():
    """Cookie auth liczone leniwie - zeby import feeder w testach NIE wymagal
    app_config.json (modul ma byc importowalny do testow czystych funkcji)."""
    cfg = json.loads((BASE / "app_config.json").read_text(encoding="utf-8"))
    ck = hashlib.sha256((cfg["pin"] + cfg["cookie_secret"]).encode()).hexdigest()
    return {"Cookie": f"bs_auth_ido={ck}"}


HDR = {}   # wypelniane w main() przez _auth_header()


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def get_json(url, data=None, timeout=120):
    req = urllib.request.Request(API + url, data=data, headers=(
        {**HDR, "Content-Type": "application/x-www-form-urlencoded"} if data else HDR))
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def queue():
    try:
        return get_json("/api/queue", timeout=15)
    except Exception:
        return None


def active_pids():
    out = set()
    try:
        for j in get_json("/api/jobs?active=1", timeout=60):
            m = re.match(r"^(.+?)_\d+$", Path(j.get("name", "")).stem)
            if m:
                out.add(m.group(1))
    except Exception as e:
        log(f"WARN active_pids: {e}")
    return out


def season_products(season):
    off, out, total = 0, [], None
    while True:
        # avail=y: TYLKO produkty ze stanem magazynowym (productIsAvailable).
        # Bez tego feeder dosypywal tez niedostepne (~polowa katalogu) = marnowal
        # robote (~33s/zdjecie) na produkty bez stanu, ktore i tak sie nie sprzedaja.
        d = get_json(f"/api/idosell/products?season={urllib.parse.quote(season)}"
                     f"&avail=y&sort=id_asc&offset={off}&limit=100")
        ps = d.get("products", [])
        if not ps:
            break
        out += ps
        total = d.get("total")
        off += len(ps)
        if (total and off >= total) or off > 3000:
            break
    return out


def build_candidates():
    act = active_pids()
    by_season = {s: season_products(s) for s in SEASONS}
    for s in SEASONS:
        log(f"sezon {s}: {len(by_season[s])} produktow")
    cand = select_candidates(by_season, act)
    log(f"RAZEM kandydatow do dosypania: {len(cand)}")
    return cand


def feed(chunk):
    data = urllib.parse.urlencode({"product_ids": json.dumps(chunk)}).encode()
    try:
        r = get_json("/api/idosell/bulk-process", data=data, timeout=600)
        return r.get("total_jobs", 0)
    except Exception as e:
        log(f"feed ERR {e}")
        return 0


def main():
    global HDR
    HDR = _auth_header()
    log("=== feeder START (drip-feed kolejki) ===")
    cand = build_candidates()
    cursor = 0
    while True:
        q = queue()
        if q is None:
            log("serwer nie odpowiada - czekam")
            time.sleep(INTERVAL)
            continue
        if not should_feed(q):           # pauza albo kolejka jeszcze pelna (>=LOW)
            time.sleep(INTERVAL)
            continue
        if cursor >= len(cand):
            log("wszyscy kandydaci dosypani - czuwam (kolejka sie domiela)")
            time.sleep(INTERVAL * 4)
            continue
        act = active_pids()
        added = 0
        while q.get("queued", 0) < HIGH and cursor < len(cand):
            chunk, cursor = take_chunk(cand, cursor, act)
            if not chunk:
                break
            added += feed(chunk)
            q = queue() or q
        log(f"DOSYPANO +{added} zadan | queued={q.get('queued')} "
            f"cursor={cursor}/{len(cand)}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
