"""Nocne dokolejkowanie letnich produktow (sezon=lato) do obrobki - SERWEROWO,
z pominieciem klienckiego throttle (runBulkProcess). Budzet ~16h pracy.

- bierze sezon=lato, tylko nieprzetworzone (not processed, not executed),
- wyklucza produkty ktore SA juz w kolejce (queued/processing) - by nie
  resetowac trwajacych (_ido_process_default robi _reset_product_jobs),
- budzetuje po ~PHOTO_BUDGET zdjec (ID rosnaco), reszta zostaje na pozniej,
- POST /api/idosell/bulk-process w paczkach (endpoint kolejkuje od razu).
"""
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
cfg = json.loads((BASE / "app_config.json").read_text(encoding="utf-8"))
CK = hashlib.sha256((cfg["pin"] + cfg["cookie_secret"]).encode()).hexdigest()
API = "http://127.0.0.1:5001"
HDR = {"Cookie": f"bs_auth_ido={CK}"}
PHOTO_BUDGET = 4000     # cala pula lato (~591 prod, ~11.5h pracy) - miesci sie w 16h
CHUNK = 8


def get_json(url):
    return json.load(urllib.request.urlopen(
        urllib.request.Request(url, headers=HDR), timeout=120))


def active_pids():
    """pid-y zadan juz queued/processing (z nazwy 'pid_idx.jpg')."""
    out = set()
    try:
        for j in get_json(f"{API}/api/jobs"):
            if j.get("status") in ("queued", "processing"):
                m = re.match(r"^(.+?)_\d+$", Path(j.get("name", "")).stem)
                if m:
                    out.add(m.group(1))
    except Exception as e:
        print("WARN active_pids:", e, flush=True)
    return out


def main():
    allp, off, total = [], 0, None
    while True:
        d = get_json(f"{API}/api/idosell/products?season=lato&sort=id_asc"
                     f"&offset={off}&limit=100")
        ps = d.get("products", [])
        if not ps:
            break
        allp += ps
        total = d.get("total")
        off += len(ps)
        if (total and off >= total) or off > 2500:
            break

    skip = active_pids()
    need = [p for p in allp
            if not p.get("processed") and not p.get("executed")
            and str(p["id"]) not in skip]
    sel, ph = [], 0
    for p in need:
        c = p.get("images_count") or 6
        if ph + c > PHOTO_BUDGET and sel:
            break
        sel.append(p["id"])
        ph += c
    print(f"lato: {total} w API | do obrobki {len([p for p in allp if not p.get('processed') and not p.get('executed')])}"
          f" | w kolejce pomijam {len(skip)} | WYBRANO {len(sel)} prod (~{ph} zdjec)", flush=True)

    ok = jobs = fail = 0
    for i in range(0, len(sel), CHUNK):
        part = sel[i:i + CHUNK]
        data = urllib.parse.urlencode({"product_ids": json.dumps(part)}).encode()
        req = urllib.request.Request(
            f"{API}/api/idosell/bulk-process", data=data,
            headers={**HDR, "Content-Type": "application/x-www-form-urlencoded"})
        try:
            r = json.load(urllib.request.urlopen(req, timeout=600))
            ok += r.get("ok", 0)
            jobs += r.get("total_jobs", 0)
            fail += r.get("failed", 0)
            print(f"  paczka {i//CHUNK+1}/{(len(sel)+CHUNK-1)//CHUNK}: "
                  f"+{r.get('total_jobs',0)} zadan (suma {jobs}) ok={ok} fail={fail}",
                  flush=True)
        except Exception as e:
            fail += len(part)
            print(f"  paczka {i//CHUNK+1} ERR {e}", flush=True)
        time.sleep(1)
    print(f"GOTOWE: produktow ok={ok} fail={fail} | zadan zakolejkowanych={jobs}",
          flush=True)


if __name__ == "__main__":
    main()
