"""Aplikacja webowa bg-remover: wklejasz/upuszczasz zdjecia, wybierasz
obrobke, pobierasz wyniki. Modul Allegro: podglad ofert i obrobka ich
zdjec (FAZA 1 - wylacznie odczyt z Allegro, zadnych zmian w ofertach).

Start: python app.py -> http://127.0.0.1:5001
Bezpieczenstwo:
- serwer nasluchuje tylko na 127.0.0.1 (brak dostepu z sieci),
- dostep do aplikacji chroniony PIN-em (app_config.json, wypisywany
  w konsoli przy starcie),
- sekrety Allegro w allegro_config.json, nigdy nie sa logowane
  ani wysylane do przegladarki.
"""
import base64
import hashlib
import io
import json
import logging
import re
import secrets as pysecrets
import threading
import time
import uuid
import zipfile
from pathlib import Path
from queue import Empty, Queue

from flask import (Flask, jsonify, make_response, request, send_file,
                   send_from_directory)

import allegro_bridge
import affenzahn_client
import allegro_client
import idosell_client
import pipeline

BASE = Path(__file__).parent
DONE_DIR = BASE / "done"
DONE_DIR.mkdir(exist_ok=True)
ORIGINALS_DIR = BASE / "originals"
ORIGINALS_DIR.mkdir(exist_ok=True)
MASKS_DIR = BASE / "masks"
MASKS_DIR.mkdir(exist_ok=True)
EXTRAS_DIR = BASE / "extras"   # lokalne zdjecia dodane do produktow z dysku
EXTRAS_DIR.mkdir(exist_ok=True)
APP_CONFIG_FILE = BASE / "app_config.json"
JOBS_STATE_FILE = BASE / "jobs_state.json"

app = Flask(__name__, static_folder="static", static_url_path="/static")

jobs = {}          # job_id -> dict(status, name, result, error, ...)
job_order = []     # kolejnosc dodania
queue = Queue()            # bulk (masowka) - wstrzymywany pauza
priority_queue = Queue()   # interaktywne (Ponow/Obrob/dodaj/upload) - ZAWSZE, tez przy pauzie
lock = threading.Lock()
inference_busy = threading.Event()  # gdy ustawione, skan tla pauzuje
low_ram_pause = threading.Event()   # obrobka wstrzymana - za malo RAM
process_paused = threading.Event()  # RECZNA pauza obrobki (user) - zwalnia CPU, kolejka zostaje
MIN_RAM_GB = 1.2                    # prog wstrzymania obrobki
LOWRES_MAX_PX = 800                 # zrodlo ponizej tego (dluzszy bok) = "male zrodlo"
JOB_TIMEOUT_S = 600                 # watchdog: zadanie dluzsze = uznane za zawieszone
#                                     (zdrowa maszyna ~30s; 600s tylko realne zawieszki)

# --- async kolejka wysylki na Allegro (tlo). OSOBNY lock = izolacja od rdzenia
# obrobki; nie rusza synchronicznej sciezki /bridge/execute (ta zostaje rdzeniem). ---
send_queue = Queue()
send_lock = threading.Lock()
send_records = {}                   # product_id(str) -> rekord {ido:{}, allegro:{}, state}
SENDS_STATE_FILE = BASE / "allegro_sends.json"
SEND_THROTTLE_S = 1.5               # tempo PRODUKTOW (bg robi backoff 429 per wariant)


def free_ram_gb() -> float:
    """Wolny RAM fizyczny w GB (Windows, bez zaleznosci - przez kernel32)."""
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
    return round(st.ullAvailPhys / 1024 ** 3, 1)

allegro_auth = {"status": "idle"}  # stan trwajacego device flow


# ---------------- PIN / dostep ----------------

def load_app_config() -> dict:
    if APP_CONFIG_FILE.exists():
        try:
            return json.loads(APP_CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    cfg = {
        "pin": "".join(pysecrets.choice("0123456789") for _ in range(6)),
        "cookie_secret": pysecrets.token_hex(16),
    }
    APP_CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg


APP_CFG = load_app_config()


def _auth_cookie_value() -> str:
    raw = (APP_CFG["pin"] + APP_CFG["cookie_secret"]).encode()
    return hashlib.sha256(raw).hexdigest()


def _is_logged_in() -> bool:
    # bs_auth_ido, nie bs_auth: przegladarka nie rozroznia portow przy
    # ciasteczkach, wiec wspolna nazwa z bg-removerem (port 5000)
    # wylogowywala by jedna aplikacje przy logowaniu do drugiej
    return request.cookies.get("bs_auth_ido") == _auth_cookie_value()


@app.before_request
def guard():
    # strona glowna i statyki sa dostepne (UI pokaze ekran PIN),
    # wszystkie dane i akcje wymagaja zalogowania
    open_paths = ("/", "/api/login")
    if request.path in open_paths or request.path.startswith("/static/"):
        return None
    if not _is_logged_in():
        return jsonify({"error": "unauthorized"}), 401
    return None


@app.post("/api/login")
def api_login():
    data = request.get_json(silent=True) or {}
    if str(data.get("pin", "")) != APP_CFG["pin"]:
        time.sleep(1)  # spowolnienie zgadywania
        return jsonify({"ok": False}), 403
    resp = make_response(jsonify({"ok": True}))
    resp.set_cookie("bs_auth_ido", _auth_cookie_value(), httponly=True,
                    samesite="Strict", max_age=30 * 24 * 3600)
    return resp


# ---------------- obrobka ----------------

def extract_product_id(stem: str) -> str:
    match = re.match(r"^(.+?)_\d+$", stem)
    return match.group(1) if match else stem


PERSIST_KEYS = ("id", "name", "status", "result", "error", "source",
                "orig", "options", "seconds", "editable", "qa", "kept",
                "fashion", "rev", "dup", "dup_keep")


def persist_jobs():
    with lock:
        snapshot = [{k: jobs[jid].get(k) for k in PERSIST_KEYS}
                    for jid in job_order]
    # GUARD: nie nadpisuj niepustego stanu pustym. Nie ma masowego "wyczysc"
    # (stop tylko oznacza queued->error, job_order zostaje), wiec pusty
    # snapshot przy niepustym pliku = wpadka/wyscig - kiedys wymazalo
    # ~5000 zadan. Lepiej zostawic stary stan niz go skasowac.
    if not snapshot and JOBS_STATE_FILE.exists():
        try:
            existing = json.loads(JOBS_STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = None
        if existing:
            print(f"persist_jobs: pomijam zapis [] na niepusty stan "
                  f"({len(existing)} zadan) - guard")
            return
    try:
        JOBS_STATE_FILE.write_text(
            json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        # kopia ostatniego niepustego stanu jako siatka bezpieczenstwa
        if snapshot:
            JOBS_STATE_FILE.with_suffix(".json.bak").write_text(
                json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def submit_job(name: str, data: bytes, options: dict, source: str = "upload",
               priority: bool = True) -> str:
    # priority=True (domyslnie) = interaktywne (upload/Ponow/Obrob jeden produkt/
    # dodaj zdjecie) -> kolejka priorytetowa, leci ZAWSZE (tez przy pauzie).
    # bulk (_ido_process_default/feeder) wola z priority=False.
    job_id = uuid.uuid4().hex[:12]
    ext = Path(name).suffix or ".jpg"
    orig_file = f"{job_id}{ext}"
    try:
        (ORIGINALS_DIR / orig_file).write_bytes(data)
    except OSError:
        orig_file = None
    with lock:
        jobs[job_id] = {
            "id": job_id, "name": name, "status": "queued",
            # nie trzymaj bajtow w RAM gdy oryginal jest na dysku - worker
            # doczyta z orig na biezaco; w pamieci tylko gdy zapis sie nie udal.
            # Inaczej cala zakolejkowana paczka (hurt) siedzialaby naraz w RAM.
            "data": data if orig_file is None else None,
            "options": options, "result": None,
            "error": None, "source": source, "orig": orig_file,
        }
        job_order.append(job_id)
    (priority_queue if priority else queue).put(job_id)
    persist_jobs()
    return job_id


def submit_kept(product_id, idx, data: bytes, fashion: bool = False,
                source: str = "idosell"):
    """Zadanie 'ZOSTAWIONE' (nie obrabiane): wynik = ORYGINAL bez ciecia, zeby
    bylo widoczne w Studio jako karta (kontrola - jak cos blednie uznane za
    fashion, widac to i mozna nadpisac obrobka). BEZ maski -> wyrownanie barw
    go pomija. fashion=True gdy auto-wykryte."""
    from io import BytesIO

    from PIL import Image
    pid = str(product_id)
    job_id = uuid.uuid4().hex[:12]
    name = f"{pid}_{idx}.jpg"
    rel = f"{pid}/{name}"
    orig_file = f"{job_id}.jpg"
    try:
        (ORIGINALS_DIR / orig_file).write_bytes(data)
    except OSError:
        orig_file = None
    (DONE_DIR / pid).mkdir(parents=True, exist_ok=True)
    try:
        Image.open(BytesIO(data)).convert("RGB").save(DONE_DIR / rel, "JPEG", quality=95)
    except Exception:
        return None
    with lock:
        jobs[job_id] = {
            "id": job_id, "name": name, "status": "done", "result": rel,
            "error": None, "source": source, "orig": orig_file, "data": None,
            "options": {}, "seconds": 0, "editable": False, "rev": 1,
            "kept": True, "fashion": bool(fashion),
            "qa": {"ok": True, "issues": []},
        }
        job_order.append(job_id)
    persist_jobs()
    return job_id


def _remove_product_job(product_id, idx) -> None:
    """Usuwa zadanie JEDNEGO zdjecia (pid_idx) + pliki robocze - przed zmiana
    decyzji/ponowna obrobka tego zdjecia (bez duplikatow kart w Studio)."""
    target = f"{product_id}_{idx}"
    removed = []
    with lock:
        rem = [jid for jid in list(job_order)
               if jobs.get(jid) and jobs[jid].get("source") == "idosell"
               and Path(jobs[jid].get("name", "")).stem == target]
        for jid in rem:
            j = jobs.pop(jid, None)
            if jid in job_order:
                job_order.remove(jid)
            removed.append((jid, (j or {}).get("orig")))
    for jid, orig in removed:
        try:
            if orig:
                (ORIGINALS_DIR / orig).unlink(missing_ok=True)
            (MASKS_DIR / f"{jid}.rgb.jpg").unlink(missing_ok=True)
            (MASKS_DIR / f"{jid}.a.png").unlink(missing_ok=True)
        except OSError:
            pass
    if removed:
        persist_jobs()


def qa_check(out_img, alpha, full_bleed: bool) -> dict:
    """Auto-kontrola jakosci wyniku obrobki - flaguje grube bledy maski,
    zeby przegladu wymagaly tylko podejrzane zdjecia. Niski falszywy alarm:
    - 'maly'      obiekt zajmuje za malo kadru (zle zrodlo / przyciecie),
    - 'fragmenty' kilka duzych rozlacznych plam (szum/cien jako produkt),
    - 'proporcje' skrajnie wydluzony obiekt,
    - 'tlo'       niebiale narozniki wyniku (resztka tla; nie dla detali).
    """
    import numpy as np
    from scipy import ndimage

    issues = []
    a = np.asarray(alpha) > 128
    if not a.any():
        return {"ok": False, "issues": ["pusta"]}
    cov = float(a.mean())
    if cov < 0.10:
        issues.append("maly")

    ys, xs = np.where(a)
    bw, bh = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
    ar = bw / bh if bh else 1
    if ar > 3.3 or ar < 0.30:
        issues.append("proporcje")

    labeled, n = ndimage.label(a)
    if n > 1:
        sizes = ndimage.sum(a, labeled, range(1, n + 1))
        big = int((sizes >= sizes.max() * 0.05).sum())
        if big >= 3:
            issues.append("fragmenty")

    if not full_bleed:
        arr = np.asarray(out_img.convert("RGB"))
        h, w = arr.shape[:2]
        c = max(4, int(min(h, w) * 0.05))
        corners = [arr[:c, :c], arr[:c, -c:], arr[-c:, :c], arr[-c:, -c:]]
        white = min(float((p >= 244).all(axis=2).mean()) for p in corners)
        if white < 0.97:
            issues.append("tlo")
    return {"ok": not issues, "issues": issues}


def worker():
    while True:
        # PRIORYTET: interaktywne (upload/Ponow/Obrob jeden produkt/dodaj) leca
        # ZAWSZE - tez przy pauzie. BULK (masowka) tylko gdy NIE zapauzowane.
        # Dzieki temu pauza zwalnia CPU na prace, a Twoje reczne i tak sie obrabia.
        try:
            job_id = priority_queue.get_nowait()
        except Empty:
            if process_paused.is_set():
                time.sleep(0.5)
                continue                  # bulk wstrzymany - czekaj na priorytet/wznow
            try:
                job_id = queue.get(timeout=1)
            except Empty:
                continue
        # przy krytycznie malym RAM czekaj zamiast mlocic skazane proby
        # (thrashing/bad_alloc zawieszal caly serwer)
        while free_ram_gb() < MIN_RAM_GB:
            low_ram_pause.set()
            time.sleep(10)
        low_ram_pause.clear()
        with lock:
            job = jobs.get(job_id)
            if job is None:
                continue
            job["status"] = "processing"
        persist_jobs()
        try:
            data = job["data"]
            if data is None and job.get("orig"):
                data = (ORIGINALS_DIR / job["orig"]).read_bytes()
            if data is None:
                raise RuntimeError("Brak danych wejsciowych zadania")
            t0 = time.time()
            inference_busy.set()  # wstrzymaj skan na czas inferencji (RAM)
            # WATCHDOG: inferencji onnxruntime nie da sie przerwac w tym samym
            # watku - liczymy ja w watku pobocznym z limitem czasu. Po przekro-
            # czeniu: blad zadania, worker leci dalej (zawieszony watek zostaje
            # daemonem). Przy nocnym batchu jeden zly obraz nie zatrzyma calosci.
            _box = {}

            def _run(_d=data, _o=job["options"], _b=_box):
                try:
                    _b["r"] = pipeline.process_bytes(_d, _o, with_parts=True)
                except BaseException as _e:   # noqa: BLE001 - przekazujemy dalej
                    _b["e"] = _e
            _t = threading.Thread(target=_run, daemon=True)
            _t.start()
            _t.join(JOB_TIMEOUT_S)
            if _t.is_alive():
                raise RuntimeError(
                    f"Przekroczono limit obrobki ({JOB_TIMEOUT_S}s) - pominiete "
                    "(zawieszona inferencja). Mozna ponowic.")
            if "e" in _box:
                raise _box["e"]
            img, work_rgb, work_alpha, full_bleed = _box["r"]
            stem = Path(job["name"]).stem
            pid = extract_product_id(stem)
            out_dir = DONE_DIR / pid
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / (stem + ".jpg")
            img.save(out_path, "JPEG", quality=95)
            # czesci robocze dla edytora maski: prawdziwy oryginal (rgb) + maska
            try:
                work_rgb.save(MASKS_DIR / f"{job_id}.rgb.jpg", "JPEG", quality=90)
                work_alpha.save(MASKS_DIR / f"{job_id}.a.png")
            except OSError:
                pass
            try:
                qa = qa_check(img, work_alpha, full_bleed)
            except Exception:
                qa = {"ok": True, "issues": []}
            with lock:
                job["status"] = "done"
                job["result"] = f"{pid}/{stem}.jpg"
                job["seconds"] = round(time.time() - t0, 1)
                job["editable"] = True
                job["qa"] = qa
                job["data"] = None
                # podbij rev po KAZDEJ obrobce - cache-buster miniatury zawsze
                # sie zmienia (inaczej po re-obrobce o podobnym czasie 'seconds'
                # przegladarka pokazywala stary wynik, np. po 'Zamien link')
                job["rev"] = (job.get("rev") or 0) + 1
        except Exception as e:
            with lock:
                job["status"] = "error"
                job["error"] = str(e)
                job["data"] = None
        finally:
            if queue.empty():
                inference_busy.clear()  # kolejka pusta - skan moze wrocic
            import gc
            gc.collect()  # zwolnij bufory numpy/ort po zadaniu - RAM nisko
        persist_jobs()


def restore_jobs():
    """Po restarcie: przywroc historie i wznow niedokonczone z originals/."""
    if not JOBS_STATE_FILE.exists():
        return
    try:
        snapshot = json.loads(JOBS_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    resumed = 0
    for j in snapshot:
        jid = j.get("id")
        if not jid:
            continue
        j["data"] = None
        if j.get("status") in ("queued", "processing"):
            if j.get("orig") and (ORIGINALS_DIR / j["orig"]).exists():
                j["status"] = "queued"
            else:
                j["status"] = "error"
                j["error"] = "Przerwane przez restart - dodaj ponownie"
        jobs[jid] = j
        job_order.append(jid)
        if j["status"] == "queued":
            queue.put(jid)
            resumed += 1
    if resumed:
        print(f"Wznowiono {resumed} zadan z poprzedniej sesji")


restore_jobs()
threading.Thread(target=worker, daemon=True).start()


def parse_options(form) -> dict:
    options = {}
    for key, default in pipeline.DEFAULTS.items():
        if key in form:
            val = form[key]
            if isinstance(default, bool):
                options[key] = val.lower() in ("1", "true", "on")
            elif isinstance(default, int):
                options[key] = int(float(val))
            elif isinstance(default, float):
                options[key] = float(val)
            else:
                options[key] = val
    return options


@app.get("/")
def index():
    return send_from_directory(BASE / "static", "index.html")


@app.post("/api/process")
def api_process():
    options = parse_options(request.form)
    created = []
    for f in request.files.getlist("files"):
        name = f.filename or f"wklejka_{int(time.time())}.png"
        created.append(submit_job(name, f.read(), options))
    return jsonify({"jobs": created})


@app.get("/api/system")
def api_system():
    return jsonify({"free_ram_gb": free_ram_gb(),
                    "paused_low_ram": low_ram_pause.is_set()})


@app.get("/api/jobs")
def api_jobs():
    # FILTR (skalowanie: przy ~8000 zadan zwracaj tylko podzbior, nie cale).
    # Studio domyslnie pyta ?active=1 (tylko w trakcie) albo z filtrem
    # (?series/?season/?category/?pid); "Zaladuj wszystko" -> bez parametrow.
    f_active = request.args.get("active")
    f_light = request.args.get("light")   # wszystko OPROCZ masowego idosell+done
    f_pid = request.args.get("pid")
    f_series = request.args.get("series")
    f_season = request.args.get("season")
    f_category = request.args.get("category")
    content_filter = bool(f_pid or f_series or f_season or f_category)
    pid_meta = _ido_series_pid_meta()   # z cache indeksu serii (jesli jest)

    def _keep(j):
        if f_active and j.get("status") not in ("queued", "processing"):
            return False
        if f_light and j.get("source") == "idosell" and j.get("status") == "done":
            return False   # masowy balast (obrobione IdoSell) tylko przez filtr
        if not content_filter:
            return True
        pid = extract_product_id(Path(j.get("name", "")).stem)
        if f_pid:
            return pid == f_pid
        meta = pid_meta.get(pid) or {}
        if f_series and str(meta.get("series_id")) != str(f_series):
            return False
        if f_season and f_season not in (meta.get("season") or []):
            return False
        if f_category and f_category not in (meta.get("category") or []):
            return False
        return True

    with lock:
        out = []
        for jid in job_order:
            j = jobs[jid]
            if not _keep(j):
                continue
            out.append({k: j.get(k) for k in
                        ("id", "name", "status", "result", "error",
                         "source", "orig", "seconds", "editable", "qa", "rev",
                         "kept", "fashion", "dup", "dup_keep")})
    # wzbogac zadania IdoSell o pid/index/ikony z planu + kategoria/sezon/seria
    # (tylko podzbior 'out' -> tanio). pid_meta juz policzone wyzej.
    plan_cache = {}
    for o in out:
        if o.get("source") != "idosell" or o.get("status") != "done":
            continue
        stem = Path(o["name"]).stem
        m = re.search(r"_(\d+)$", stem)
        if not m:
            continue
        pid = extract_product_id(stem)
        idx = int(m.group(1))
        o["pid"] = pid
        o["photo_index"] = idx
        meta = pid_meta.get(pid)
        if meta:
            o["category"] = meta["category"]
            o["season"] = meta["season"]
            o["series_id"] = meta["series_id"]
            o["series_name"] = meta["series_name"]
        if pid not in plan_cache:
            pf = DONE_DIR / pid / "plan.json"
            try:
                plan_cache[pid] = (json.loads(pf.read_text(encoding="utf-8"))
                                   if pf.exists() else None)
            except (json.JSONDecodeError, OSError):
                plan_cache[pid] = None
        plan = plan_cache[pid]
        if plan:
            decs = plan.get("decisions") or []
            o["icons"] = next((d.get("icons") or [] for d in decs
                               if d.get("index") == idx), [])
            ordered = _gallery_ordered(decs, plan.get("gallery_order", "fashion_second"))
            o["gallery_pos"] = next((i for i, d in enumerate(ordered)
                                     if d.get("index") == idx), None)
            o["gallery_len"] = len(decs)
            o["executed"] = bool(plan.get("executed_at")) and not plan.get("rolled_back_at")
    # rozmiar zrodla (oryginalu) + flaga "male zrodlo" - liczony raz, cache na zadaniu
    from PIL import Image as _Img
    for o in out:
        if o.get("status") != "done" or not o.get("orig"):
            continue
        jid = o["id"]
        with lock:
            job = jobs.get(jid) or {}
            sw, sh = job.get("src_w"), job.get("src_h")
        if sw is None:
            try:
                with _Img.open(ORIGINALS_DIR / o["orig"]) as im:
                    sw, sh = im.size
            except Exception:
                sw, sh = 0, 0
            with lock:
                if jid in jobs:
                    jobs[jid]["src_w"], jobs[jid]["src_h"] = sw, sh
        o["src_w"], o["src_h"] = sw, sh
        o["lowres"] = bool(sw and sh and max(sw, sh) < LOWRES_MAX_PX)
    return jsonify(out)


@app.get("/api/queue")
def api_queue():
    """Lekki status kolejki do paska: ile czeka/w obrobce, co teraz, mediana
    czasu zadania (do ETA). Bez ciezkiego wzbogacania jak /api/jobs."""
    with lock:
        queued = sum(1 for jid in job_order if jobs[jid]["status"] == "queued")
        proc = [jobs[jid] for jid in job_order if jobs[jid]["status"] == "processing"]
        done_secs = [jobs[jid].get("seconds") for jid in job_order
                     if jobs[jid]["status"] == "done" and jobs[jid].get("seconds")]
    cur = proc[0]["name"] if proc else None
    recent = [s for s in done_secs if s][-40:]   # ostatnie 40 do mediany
    med = sorted(recent)[len(recent) // 2] if recent else None
    remaining = queued + len(proc)
    eta = int(remaining * med) if (med and remaining) else None
    return jsonify({"queued": queued, "processing": len(proc), "current": cur,
                    "median_seconds": med, "remaining": remaining, "eta_seconds": eta,
                    "paused": process_paused.is_set()})


@app.post("/api/clear")
def api_clear():
    with lock:
        finished = [jid for jid in job_order
                    if jobs[jid]["status"] in ("done", "error")]
        for jid in finished:
            orig = jobs[jid].get("orig")
            if orig:
                try:
                    (ORIGINALS_DIR / orig).unlink(missing_ok=True)
                except OSError:
                    pass
            for suffix in (".rgb.jpg", ".a.png"):
                try:
                    (MASKS_DIR / f"{jid}{suffix}").unlink(missing_ok=True)
                except OSError:
                    pass
            job_order.remove(jid)
            del jobs[jid]
    persist_jobs()
    return jsonify({"cleared": len(finished)})


@app.get("/originals/<path:relpath>")
def serve_original(relpath):
    return send_from_directory(ORIGINALS_DIR, relpath)


@app.post("/api/jobs/<job_id>/retry")
def api_retry(job_id):
    """Ponowna obrobka w miejscu: resetuje istniejace zadanie
    (ta sama karta w UI) i wraca z nim do kolejki."""
    options = parse_options(request.form)
    with lock:
        job = jobs.get(job_id)
        if not job or not job.get("orig") or \
                not (ORIGINALS_DIR / job["orig"]).exists():
            return jsonify({"error": "Brak oryginalu - dodaj zdjecie ponownie"}), 404
        if job["status"] in ("queued", "processing"):
            return jsonify({"error": "Zadanie juz jest w kolejce"}), 409
        # zachowaj mirror (per-zdjecie, brak go w formularzu globalnym)
        if "mirror" not in options:
            options["mirror"] = bool((job.get("options") or {}).get("mirror"))
        job.update(status="queued", error=None, result=None,
                   seconds=None, options=options, data=None)
    priority_queue.put(job_id)   # Ponow = interaktywne -> priorytet (tez przy pauzie)
    persist_jobs()
    return jsonify({"job": job_id})


@app.post("/api/jobs/<job_id>/flip")
def api_flip(job_id):
    """Odbicie lustrzane GOTOWEGO zdjecia (kierunek noska) - na gotowym pliku.
    Pipeline jest symetryczny w poziomie, wiec flip wyniku = obrobka z mirror,
    ale NATYCHMIAST (bez re-inference). Aktualizuje flage mirror joba (spojnosc
    przy 'Ponow') i podbija 'rev' (cache-busting miniatury w UI)."""
    from PIL import Image, ImageOps
    with lock:
        job = jobs.get(job_id)
        if not job or job.get("status") != "done" or not job.get("result"):
            return jsonify({"error": "Zdjecie nie jest gotowe"}), 409
        rel = job["result"]
    path = DONE_DIR / rel
    if not path.exists():
        return jsonify({"error": "Brak pliku wyniku"}), 404
    try:
        img = Image.open(path).convert("RGB")
        ImageOps.mirror(img).save(path, quality=95)
    except Exception as e:
        return jsonify({"error": f"Blad odbicia: {e}"}), 500
    with lock:
        opts = dict(job.get("options") or {})
        opts["mirror"] = not bool(opts.get("mirror"))
        job["options"] = opts
        job["rev"] = (job.get("rev") or 0) + 1
        mirror = opts["mirror"]
    persist_jobs()
    return jsonify({"ok": True, "mirror": mirror, "rev": job["rev"]})


@app.post("/api/jobs/<job_id>/keep-original")
def api_keep_original(job_id):
    """Przywroc ORYGINAL zdjecia (zostaw 1:1, bez obrobki) - dla fashion/lifestyle
    blednie obrobionego. Wynik = oryginal 1:1, job oznaczony kept+fashion, maska
    skasowana (recompose-all go pomija), plan: action=process + fashion=True (wysylka
    wrzuci LOKALNY plik 1:1, nie stare bajty ze sklepu)."""
    from io import BytesIO

    from PIL import Image
    with lock:
        job = jobs.get(job_id)
        if not job or not job.get("orig") or \
                not (ORIGINALS_DIR / job["orig"]).exists():
            return jsonify({"error": "Brak oryginalu zadania"}), 404
        if job.get("status") in ("queued", "processing"):
            return jsonify({"error": "Zadanie jest w kolejce"}), 409
        orig = job["orig"]
        result = job.get("result")
        jname = job.get("name") or ""
    try:
        content = (ORIGINALS_DIR / orig).read_bytes()
        with Image.open(BytesIO(content)) as im:
            w, h = im.size
    except Exception as e:
        return jsonify({"error": f"Blad oryginalu: {e}"}), 500
    if not result:                              # brak wyniku -> zloz z nazwy
        stem0 = Path(jname).stem
        result = f"{extract_product_id(stem0)}/{stem0}.jpg"
    try:
        (DONE_DIR / result).parent.mkdir(parents=True, exist_ok=True)
        Image.open(BytesIO(content)).convert("RGB").save(
            DONE_DIR / result, "JPEG", quality=95)
    except Exception as e:
        return jsonify({"error": f"Zapis wyniku: {e}"}), 500
    stem = Path(jname).stem                     # plan: action=process + fashion
    m = re.search(r"_(\d+)$", stem)
    if m:
        pid, idx = extract_product_id(stem), int(m.group(1))
        pf = DONE_DIR / pid / "plan.json"
        if pf.exists():
            try:
                plan = json.loads(pf.read_text(encoding="utf-8"))
                for d in plan.get("decisions", []):
                    if d.get("index") == idx:
                        d["action"] = "process"
                        d["fashion"] = True
                pf.write_text(json.dumps(plan, indent=2, ensure_ascii=False),
                              encoding="utf-8")
            except (json.JSONDecodeError, OSError):
                pass
    # skasuj maske -> recompose-all pomija (zostaje 1:1, nie re-tnie tla)
    (MASKS_DIR / f"{job_id}.a.png").unlink(missing_ok=True)
    (MASKS_DIR / f"{job_id}.rgb.jpg").unlink(missing_ok=True)
    with lock:
        job.update(result=result, kept=True, fashion=True, editable=False,
                   error=None)
        job["qa"] = {"ok": True, "issues": []}
        job["rev"] = (job.get("rev") or 0) + 1
    persist_jobs()
    return jsonify({"ok": True, "rev": job["rev"]})


def _replace_job_image(job_id, content):
    """Podmiana obrazu zadania danymi bajtami (z URL albo z dysku).
    - zadanie KEPT (fashion): zapis 1:1 bez modelu + plan -> action=process
      (fashion=True), by wysylka wrzucila LOKALNY plik 1:1, nie stare bajty;
    - zadanie obrobione: re-kolejka (przerobienie nowego zrodla).
    To UZYTKOWNIK wskazuje wlasciwy obraz, wiec brak ryzyka zlego dopasowania."""
    from io import BytesIO
    from PIL import Image
    with lock:
        job = jobs.get(job_id)
        if not job or not job.get("orig"):
            return jsonify({"error": "Brak zadania lub oryginalu"}), 404
        if job.get("status") in ("queued", "processing"):
            return jsonify({"error": "Zadanie juz jest w kolejce"}), 409
        orig = job["orig"]
        options = dict(job.get("options") or {})
        is_kept = bool(job.get("kept"))
        result = job.get("result")
        jname = job.get("name") or ""
    try:
        with Image.open(BytesIO(content)) as im:   # walidacja: czy to obraz
            w, h = im.size
    except Exception as e:
        return jsonify({"error": f"To nie jest poprawny obraz: {e}"}), 400
    try:
        (ORIGINALS_DIR / orig).write_bytes(content)
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    if is_kept and result:
        try:
            Image.open(BytesIO(content)).convert("RGB").save(
                DONE_DIR / result, "JPEG", quality=95)
        except Exception as e:
            return jsonify({"error": f"Zapis wyniku: {e}"}), 500
        stem = Path(jname).stem
        m = re.search(r"_(\d+)$", stem)
        if m:
            pid, idx = extract_product_id(stem), int(m.group(1))
            pf = DONE_DIR / pid / "plan.json"
            if pf.exists():
                try:
                    plan = json.loads(pf.read_text(encoding="utf-8"))
                    for d in plan.get("decisions", []):
                        if d.get("index") == idx:
                            d["action"] = "process"   # wysylka: lokalny plik 1:1
                            d["fashion"] = True
                    pf.write_text(json.dumps(plan, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
                except (json.JSONDecodeError, OSError):
                    pass
        with lock:
            job["src_w"], job["src_h"] = w, h
            job["rev"] = (job.get("rev") or 0) + 1   # cache-buster miniatury
        persist_jobs()
        return jsonify({"ok": True, "w": w, "h": h, "kept": True})
    with lock:
        job.update(status="queued", error=None, result=None, seconds=None,
                   data=None, options=options)
        job["src_w"], job["src_h"] = w, h
    priority_queue.put(job_id)   # podmiana zrodla = interaktywne -> priorytet
    persist_jobs()
    return jsonify({"ok": True, "w": w, "h": h})


@app.post("/api/jobs/<job_id>/replace-url")
def api_replace_url(job_id):
    """Podmiana zrodla zadania na obraz spod URL (link wskazany recznie)."""
    import requests as rq
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "Podaj poprawny adres http(s)"}), 400
    try:
        resp = rq.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return jsonify({"error": f"Pobranie nieudane ({resp.status_code})"}), 400
        content = resp.content
    except Exception as e:
        return jsonify({"error": f"Nie udalo sie pobrac obrazu: {e}"}), 400
    return _replace_job_image(job_id, content)


@app.post("/api/jobs/<job_id>/replace-file")
def api_replace_file(job_id):
    """Podmiana zrodla zadania na plik z dysku (upload). Jak replace-url, tylko
    obraz przychodzi z dysku - dziala tez dla kart fashion (zostaje 1:1)."""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "Brak pliku"}), 400
    content = f.read()
    if not content:
        return jsonify({"error": "Pusty plik"}), 400
    return _replace_job_image(job_id, content)


@app.post("/api/jobs/stop")
def api_jobs_stop():
    """Anuluje wszystkie zadania w kolejce (biezace dokonczy sie samo)."""
    for q in (queue, priority_queue):   # oproznij obie kolejki
        while True:
            try:
                q.get_nowait()
            except Exception:
                break
    canceled = 0
    with lock:
        for jid in job_order:
            j = jobs[jid]
            if j["status"] == "queued":
                j["status"] = "error"
                j["error"] = "Anulowane - mozna ponowic"
                j["data"] = None
                canceled += 1
    persist_jobs()
    return jsonify({"canceled": canceled})


@app.post("/api/queue/pause")
def api_queue_pause():
    """Reczna pauza/wznowienie obrobki. paused=true: worker konczy biezace zdjecie,
    potem czeka (CPU wolne), kolejka zachowana. paused=false: leci dalej."""
    body = request.get_json(silent=True) or {}
    if body.get("paused"):
        process_paused.set()
    else:
        process_paused.clear()
    return jsonify({"paused": process_paused.is_set()})


@app.get("/done/<path:relpath>")
def serve_done(relpath):
    return send_from_directory(DONE_DIR, relpath)


@app.get("/masks/<path:relpath>")
def serve_mask(relpath):
    return send_from_directory(MASKS_DIR, relpath)


@app.get("/api/jobs/<job_id>/editor")
def api_editor(job_id):
    """Dane do edytora maski: rgb robocze + aktualna alpha."""
    with lock:
        job = jobs.get(job_id)
    rgb = MASKS_DIR / f"{job_id}.rgb.jpg"
    alpha = MASKS_DIR / f"{job_id}.a.png"
    if not job or not rgb.exists() or not alpha.exists():
        return jsonify({"error": "Brak danych edytora - obrob zdjecie "
                                 "ponownie (starsze zadania ich nie maja)"}), 404
    return jsonify({
        "rgb": f"/masks/{job_id}.rgb.jpg?t={int(rgb.stat().st_mtime)}",
        "alpha": f"/masks/{job_id}.a.png?t={int(alpha.stat().st_mtime)}",
        "editable": True,
        "name": job["name"],
    })


@app.post("/api/jobs/<job_id>/recompose")
def api_recompose(job_id):
    """Rekompozycja z poprawiona reczne maska - bez inferencji (szybkie)."""
    from io import BytesIO

    from PIL import Image
    with lock:
        job = jobs.get(job_id)
    rgb_path = MASKS_DIR / f"{job_id}.rgb.jpg"
    if not job or not rgb_path.exists():
        return jsonify({"error": "Brak danych edytora"}), 404
    mask_file = request.files.get("mask")
    if not mask_file:
        return jsonify({"error": "Brak maski"}), 400
    options = parse_options(request.form)
    try:
        rgb = Image.open(rgb_path)
        # edytor koduje maske w kanale ALFA (gumka zeruje alfe, nie RGB) -
        # czytamy alfe, nie luminancje RGB, inaczej 'Wymaz' nie dziala
        m = Image.open(BytesIO(mask_file.read())).convert("RGBA")
        alpha = m.split()[3]
        if alpha.getextrema() == (255, 255):   # brak alfy (np. JPG) -> luminancja
            alpha = m.convert("L")
        if alpha.size != rgb.size:
            alpha = alpha.resize(rgb.size, Image.LANCZOS)
        t0 = time.time()
        img = pipeline.compose_from(rgb, alpha, options)
        stem = Path(job["name"]).stem
        out_path = DONE_DIR / extract_product_id(stem) / (stem + ".jpg")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, "JPEG", quality=95)
        alpha.save(MASKS_DIR / f"{job_id}.a.png")  # utrwal poprawiona maske
        try:
            rgba = rgb.convert("RGB").copy()
            rgba.putalpha(alpha)
            qa = qa_check(img, alpha, pipeline.is_full_bleed(rgba))
        except Exception:
            qa = {"ok": True, "issues": []}
        with lock:
            job["seconds"] = round(time.time() - t0, 1)
            job["qa"] = qa
        persist_jobs()
        return jsonify({"ok": True, "result": job["result"], "qa": qa})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/jobs/recompose-all")
def api_recompose_all():
    """Hurtowe przeliczenie WSZYSTKICH gotowych zadan poprawionym pipeline,
    BEZ modelu - z zapisanych masek (masks/{id}.rgb=oryginal z tlem + .a=maska).
    Uzywa compose(..., src_rgb=oryginal), wiec wiernie odtwarza finalny etap
    (cien ZACHOWAJ / full-bleed jak w oryginale), tylko z aktualnym kodem
    (np. poprawiona biel). Nadpisuje pliki wynikow, podbija rev, liczy QA.
    Nic nie idzie do sklepu - tylko lokalne pliki done/."""
    from PIL import Image
    with lock:
        ids = [jid for jid in job_order
               if jobs[jid].get("status") == "done" and jobs[jid].get("result")]
    done = skipped = errors = 0
    for jid in ids:
        rgb_p = MASKS_DIR / f"{jid}.rgb.jpg"
        a_p = MASKS_DIR / f"{jid}.a.png"
        if not (rgb_p.exists() and a_p.exists()):
            skipped += 1
            continue
        with lock:
            job = jobs.get(jid)
            if not job:
                continue
            options = dict(job.get("options") or {})
            rel = job.get("result")
        try:
            src = Image.open(rgb_p).convert("RGB")
            alpha = Image.open(a_p).convert("L")   # maska juz po refine (zapis workera)
            rgba = src.convert("RGBA")
            rgba.putalpha(alpha)
            img = pipeline.compose(rgba, {**pipeline.DEFAULTS, **options}, src_rgb=src)
            out_path = DONE_DIR / rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(out_path, "JPEG", quality=95)
            try:
                qa = qa_check(img, alpha, pipeline.is_full_bleed(rgba))
            except Exception:
                qa = None
            with lock:
                job["rev"] = (job.get("rev") or 0) + 1
                if qa is not None:
                    job["qa"] = qa
            done += 1
        except Exception:
            errors += 1
    persist_jobs()
    return jsonify({"recomposed": done, "skipped": skipped, "errors": errors,
                    "total": len(ids)})


# ---------------- wyrownanie barw galerii (rozne sesje zdjeciowe) ----------------

COLOR_CAP_CHROMA = 10   # maks. przesuniecie Cb/Cr
COLOR_CAP_LUMA = 8      # maks. przesuniecie Y (jasnosc)
COLOR_OUTLIER_DIST = 12.0  # tylko wyrazne odstepstwo tonacji (sesja),
#                            nie naturalna zmiennosc ujec


def _group_color_stats(pid: str) -> list:
    """Srednie YCbCr pikseli produktu dla kazdego edytowalnego zadania grupy."""
    import numpy as np
    from PIL import Image
    with lock:
        group = [dict(jobs[j]) for j in job_order
                 if jobs[j]["status"] == "done"
                 and extract_product_id(Path(jobs[j]["name"]).stem) == pid]
    stats = []
    for j in group:
        if j.get("kept") or j.get("fashion"):
            continue   # zostawione/fashion NIE biora udzialu w wyrownaniu barw
        rgb_p = MASKS_DIR / f"{j['id']}.rgb.jpg"
        a_p = MASKS_DIR / f"{j['id']}.a.png"
        if not rgb_p.exists() or not a_p.exists() or not j.get("editable", True):
            continue
        ycc = np.asarray(Image.open(rgb_p).convert("YCbCr"), dtype=np.float64)
        mask = np.asarray(Image.open(a_p).convert("L")) > 128
        if mask.sum() < 500:
            continue
        # tint mierzymy z kolorowych pikseli materialu (chroma > 8) - biale
        # wkladki/podeszwa rozcienczalyby pomiar i falszowaly odchylke
        prod = ycc[mask]
        chroma = np.sqrt((prod[:, 1] - 128) ** 2 + (prod[:, 2] - 128) ** 2)
        colored = prod[chroma > 8]
        means = (colored if len(colored) > 200 else prod).mean(axis=0)
        stats.append({"id": j["id"], "name": j["name"],
                      "y": round(float(means[0]), 1),
                      "cb": round(float(means[1]), 1),
                      "cr": round(float(means[2]), 1)})
    return stats


def _color_analysis(pid: str) -> dict:
    import numpy as np
    stats = _group_color_stats(pid)
    if len(stats) < 3:
        return {"error": "Za malo zdjec z danymi (min 3 obrobione)"}
    med = {k: float(np.median([s[k] for s in stats])) for k in ("y", "cb", "cr")}
    for s in stats:
        s["dist"] = round(((s["cb"] - med["cb"]) ** 2 +
                           (s["cr"] - med["cr"]) ** 2) ** 0.5, 1)
        s["outlier"] = s["dist"] > COLOR_OUTLIER_DIST
    return {"median": med, "photos": stats,
            "outliers": sum(1 for s in stats if s["outlier"])}


@app.get("/api/groups/<pid>/colors")
def api_group_colors(pid):
    result = _color_analysis(pid)
    return (jsonify(result), 400) if "error" in result else jsonify(result)


@app.post("/api/groups/<pid>/colors/apply")
def api_group_colors_apply(pid):
    """Koryguje odstajace zdjecia do mediany grupy (z limitem sily),
    zapisuje poprawione rgb robocze i rekomponuje wyniki."""
    import numpy as np
    from PIL import Image
    result = _color_analysis(pid)
    if "error" in result:
        return jsonify(result), 400
    med = result["median"]
    adjusted = []
    for s in result["photos"]:
        if not s["outlier"]:
            continue
        delta = {
            "y": max(-COLOR_CAP_LUMA, min(COLOR_CAP_LUMA, med["y"] - s["y"])),
            "cb": max(-COLOR_CAP_CHROMA, min(COLOR_CAP_CHROMA, med["cb"] - s["cb"])),
            "cr": max(-COLOR_CAP_CHROMA, min(COLOR_CAP_CHROMA, med["cr"] - s["cr"])),
        }
        rgb_p = MASKS_DIR / f"{s['id']}.rgb.jpg"
        a_p = MASKS_DIR / f"{s['id']}.a.png"
        ycc = np.asarray(Image.open(rgb_p).convert("YCbCr"), dtype=np.float64)
        # Waga korekty per piksel = jego nasycenie. Biel/szarosc (wkladki,
        # podeszwa) maja chroma ~0 -> waga ~0 -> nie zmieniaja koloru.
        # Kolorowy material korygowany w pelni - usuwa tint sesji bez
        # przebarwiania neutralnych obszarow.
        chroma = np.sqrt((ycc[..., 1] - 128) ** 2 + (ycc[..., 2] - 128) ** 2)
        w = np.clip((chroma - 8) / (28 - 8), 0, 1)
        ycc[..., 0] += delta["y"] * w
        ycc[..., 1] += delta["cb"] * w
        ycc[..., 2] += delta["cr"] * w
        fixed = Image.fromarray(
            np.clip(ycc, 0, 255).astype("uint8"), "YCbCr").convert("RGB")
        fixed.save(rgb_p, "JPEG", quality=90)

        with lock:
            job = jobs.get(s["id"])
            options = dict(job.get("options") or {}) if job else {}
        alpha = Image.open(a_p).convert("L")
        img = pipeline.compose_from(fixed, alpha, options)
        stem = Path(s["name"]).stem
        out_path = DONE_DIR / extract_product_id(stem) / (stem + ".jpg")
        img.save(out_path, "JPEG", quality=95)
        with lock:
            if job:
                job["seconds"] = (job.get("seconds") or 0) + 0.1
        adjusted.append({"id": s["id"], "name": s["name"], "delta": delta})
    persist_jobs()
    return jsonify({"adjusted": adjusted, "count": len(adjusted)})


@app.get("/api/zip")
def api_zip():
    with lock:
        results = [jobs[jid]["result"] for jid in job_order
                   if jobs[jid]["status"] == "done" and jobs[jid]["result"]]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in results:
            p = DONE_DIR / rel
            if p.exists():
                zf.write(p, rel.replace("/", "_"))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name="bg-remover-wyniki.zip")


# ---------------- skan tla ofert ----------------

SCAN_STATE_FILE = BASE / "scan_state.json"
scan_lock = threading.Lock()
scan_state = {"status": "idle", "total": 0, "checked": 0, "results": {}}

if SCAN_STATE_FILE.exists():
    try:
        scan_state = json.loads(SCAN_STATE_FILE.read_text(encoding="utf-8"))
        if scan_state.get("status") == "running":
            scan_state["status"] = "stopped"
    except (json.JSONDecodeError, OSError):
        pass


def save_scan_state():
    try:
        SCAN_STATE_FILE.write_text(
            json.dumps(scan_state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def analyze_thumb(data: bytes) -> dict:
    """Heurystyka narozników: 4 patche 12% w rogach miniatury (tam prawie
    nigdy nie ma produktu). Zwalidowane na probkach: kolorowe/szare tla
    daja 0.00, biale 0.93-1.00. Dodatkowo proporcje kadru."""
    from io import BytesIO

    import numpy as np
    from PIL import Image
    img = Image.open(BytesIO(data))
    # przezroczyste PNG (produkt juz wyciety, np. Be Lenka) -> kompozycja na
    # biel, inaczej convert("RGB") robi z przezroczystosci CZERN i kazde
    # takie zdjecie ladowalo jako "fashion" (white naroznikow = 0)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.alpha_composite(img)
        img = bg
    arr = np.asarray(img.convert("RGB"))
    h, w = arr.shape[:2]
    mh, mw = max(2, int(h * 0.12)), max(2, int(w * 0.12))
    patches = [arr[:mh, :mw], arr[:mh, -mw:], arr[-mh:, :mw], arr[-mh:, -mw:]]
    corner_white = min(float((p >= 240).all(axis=2).mean()) for p in patches)
    return {"white": corner_white, "ratio": w / h}


def is_white_background(data: bytes) -> bool:
    return analyze_thumb(data)["white"] >= 0.85


def thumb_flags(data: bytes) -> dict:
    """Powody nominacji do obrobki: kolorowe tlo i/lub zle proporcje
    (kadr daleki od kwadratu = warto przyciac)."""
    a = analyze_thumb(data)
    reasons = []
    if a["white"] < 0.85:
        reasons.append("tlo")
    if not (0.80 <= a["ratio"] <= 1.25):
        reasons.append("proporcje")
    return {"needs": bool(reasons), "reasons": reasons, "white": a["white"]}


def _scan_page_with_retry(offset: int, retries: int = 4):
    """Pobranie strony ofert odporne na chwilowe bledy sieci/DNS."""
    for attempt in range(retries):
        try:
            return allegro_client.list_offers(status="ACTIVE",
                                              offset=offset, limit=100)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(15 * (attempt + 1))


def scan_worker():
    import requests as rq
    offset = 0
    try:
        while True:
            with scan_lock:
                if scan_state["status"] != "running":
                    return
            page = _scan_page_with_retry(offset)
            offers = page["offers"]
            with scan_lock:
                scan_state["total"] = page["total"]
            if not offers:
                break
            for o in offers:
                while inference_busy.is_set():  # pauza gdy modele mielą
                    time.sleep(2)
                with scan_lock:
                    if scan_state["status"] != "running":
                        return
                    already = str(o["id"]) in scan_state["results"]
                if already:  # wznowienie - pomijamy sprawdzone
                    continue
                entry = {"name": o["name"], "image": o["image"],
                         "sku": o.get("sku"), "needs": None}
                if o["image"]:
                    for attempt in range(3):
                        try:
                            thumb = rq.get(
                                allegro_client.thumb_url(o["image"]), timeout=15)
                            if thumb.status_code == 200:
                                flags = thumb_flags(thumb.content)
                                entry["needs"] = flags["needs"]
                                entry["reasons"] = flags["reasons"]
                            break
                        except Exception:
                            time.sleep(10 * (attempt + 1))
                with scan_lock:
                    scan_state["results"][str(o["id"])] = entry
                    scan_state["checked"] = len(scan_state["results"])
                    checked = scan_state["checked"]
                if checked % 25 == 0:
                    save_scan_state()
                time.sleep(0.05)
            offset += len(offers)
        with scan_lock:
            scan_state["status"] = "done"
    except Exception as e:
        with scan_lock:
            scan_state["status"] = "error"
            scan_state["error"] = f"{e} - kliknij Skanuj aby wznowic"
    finally:
        save_scan_state()


@app.post("/api/allegro/scan")
def allegro_scan_start():
    fresh = request.args.get("fresh") == "1"
    with scan_lock:
        if scan_state.get("status") == "running":
            return jsonify({"status": "running"})
        prev = scan_state.get("status")
        scan_state.update(status="running", error=None)
        if fresh or prev == "done":
            scan_state["results"] = {}
            scan_state["checked"] = 0
            scan_state["total"] = 0
        # bez fresh: wznowienie - sprawdzone oferty zostaja pominiete
    threading.Thread(target=scan_worker, daemon=True).start()
    return jsonify({"status": "running"})


@app.post("/api/allegro/scan/stop")
def allegro_scan_stop():
    with scan_lock:
        if scan_state.get("status") == "running":
            scan_state["status"] = "stopped"
    save_scan_state()
    return jsonify({"status": scan_state["status"]})


@app.get("/api/allegro/scan/status")
def allegro_scan_status():
    with scan_lock:
        needs = sum(1 for r in scan_state["results"].values() if r.get("needs"))
        return jsonify({
            "status": scan_state.get("status"),
            "checked": scan_state.get("checked", 0),
            "total": scan_state.get("total", 0),
            "needs": needs,
            "error": scan_state.get("error"),
        })


@app.get("/api/allegro/scan/results")
def allegro_scan_results():
    offset = int(request.args.get("offset", 0))
    limit = int(request.args.get("limit", 20))
    with scan_lock:
        rows = [{"id": oid, "name": r["name"], "image": r["image"],
                 "sku": r.get("sku"), "status": "ACTIVE"}
                for oid, r in scan_state["results"].items() if r.get("needs")]
    page = rows[offset:offset + limit]
    for o in page:
        o.update(offer_local_flags(o["id"]))
    return jsonify({"offers": page, "total": len(rows)})


def offer_local_flags(offer_id: str) -> dict:
    """Plakietki: czy oferta ma juz wyniki/plan lokalnie + wynik skanu."""
    out_dir = DONE_DIR / str(offer_id)
    has_results = out_dir.exists() and any(out_dir.glob("*.jpg"))
    has_plan = (out_dir / "plan.json").exists()
    with scan_lock:
        scan = scan_state["results"].get(str(offer_id), {})
    return {"processed": has_results, "plan": has_plan,
            "needs": scan.get("needs"), "reasons": scan.get("reasons")}


# ---------------- Allegro (tylko odczyt) ----------------

@app.get("/api/allegro/status")
def allegro_status():
    configured = allegro_client.load_config() is not None
    authorized = configured and allegro_client.is_authorized()
    return jsonify({
        "configured": configured,
        "authorized": authorized,
        "write": authorized and allegro_client.has_write_scope(),
        "connect": {k: allegro_auth.get(k) for k in
                    ("status", "user_code", "verification_uri", "error")},
    })


def _poll_auth(device_code: str, interval: int, expires_in: int):
    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(max(interval, 5))
        result = allegro_client.poll_device_token(device_code)
        if result == "ok":
            allegro_auth.update(status="done")
            return
        if result != "pending":
            allegro_auth.update(status="error", error=result)
            return
    allegro_auth.update(status="error", error="Kod wygasl - sprobuj ponownie")


@app.post("/api/allegro/connect")
def allegro_connect():
    try:
        flow = allegro_client.start_device_flow(
            write=request.args.get("write") == "1")
    except allegro_client.AllegroError as e:
        return jsonify({"error": str(e)}), 400
    allegro_auth.clear()
    allegro_auth.update(
        status="waiting",
        user_code=flow["user_code"],
        verification_uri=flow.get("verification_uri_complete")
        or flow.get("verification_uri"),
    )
    threading.Thread(
        target=_poll_auth,
        args=(flow["device_code"], int(flow.get("interval", 5)),
              int(flow.get("expires_in", 600))),
        daemon=True,
    ).start()
    return jsonify({k: allegro_auth[k] for k in
                    ("status", "user_code", "verification_uri")})


@app.get("/api/allegro/offers")
def allegro_offers():
    sku_query = request.args.get("sku", "").strip()
    if sku_query:
        # szukanie po SKU lokalnym indeksem: czlon (8024), pelne SKU
        # (8024-L) lub prefiks - Allegro API dopasowuje tylko doslownie
        try:
            offset = int(request.args.get("offset", 0))
            limit = int(request.args.get("limit", 20))
            status = request.args.get("status", "")
            index = get_sku_index()
            q = sku_query.lower()
            rows = []
            for base, entries in index.items():
                for e in entries:
                    sku = (e.get("sku") or "").lower()
                    if base.lower() == q or sku == q or sku.startswith(q):
                        if not status or e.get("status") == status:
                            rows.append(e)
            rows.sort(key=lambda r: (r.get("sku") or "", str(r.get("id"))))
            page = [dict(r) for r in rows[offset:offset + limit]]
            for o in page:
                o.setdefault("stock", None)
                o.update(offer_local_flags(o["id"]))
            return jsonify({"offers": page, "total": len(rows)})
        except allegro_client.AllegroError as e:
            return jsonify({"error": str(e)}), 400
    try:
        data = allegro_client.list_offers(
            phrase=request.args.get("phrase", ""),
            sku=request.args.get("sku", ""),
            offer_id=request.args.get("offer_id", ""),
            status=request.args.get("status", ""),
            offset=int(request.args.get("offset", 0)),
            limit=int(request.args.get("limit", 20)),
        )
        for o in data["offers"]:
            o.update(offer_local_flags(o["id"]))
        return jsonify(data)
    except allegro_client.AllegroError as e:
        return jsonify({"error": str(e)}), 400


@app.get("/api/allegro/offers/<offer_id>/images")
def allegro_offer_images(offer_id):
    """Zdjecia oferty + sugestia akcji per zdjecie:
    biale tlo -> obrob, kolorowe tlo -> prawdopodobnie fashion -> zostaw."""
    import requests as rq
    try:
        urls = allegro_client.get_offer_images(offer_id)
    except allegro_client.AllegroError as e:
        return jsonify({"error": str(e)}), 400
    suggestions = []
    for u in urls:
        suggest = "process"
        try:
            thumb = rq.get(allegro_client.thumb_url(u), timeout=10)
            if thumb.status_code == 200:
                a = analyze_thumb(thumb.content)
                if a["white"] < 0.40:
                    suggest = "keep"  # fashion / stylizacja
        except Exception:
            pass
        suggestions.append(suggest)
    return jsonify({"images": urls, "suggestions": suggestions})


@app.post("/api/allegro/offers/<offer_id>/process")
def allegro_offer_process(offer_id):
    """Wykonuje decyzje per zdjecie: 'process' -> kolejka obrobki,
    'keep'/'delete' -> tylko zapis w planie oferty (done/{id}/plan.json).
    NIC nie jest wysylane do Allegro - usuwanie wykona faza 2 wg planu."""
    options = parse_options(request.form)
    try:
        decisions = json.loads(request.form.get("decisions", "[]"))
    except json.JSONDecodeError:
        return jsonify({"error": "Nieprawidlowy format decyzji"}), 400
    if not decisions:  # brak wyboru = obrob wszystkie
        try:
            urls = allegro_client.get_offer_images(offer_id)
        except allegro_client.AllegroError as e:
            return jsonify({"error": str(e)}), 400
        decisions = [{"url": u, "action": "process"} for u in urls]

    out_dir = DONE_DIR / offer_id
    out_dir.mkdir(parents=True, exist_ok=True)
    plan = {
        "offer_id": offer_id,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        # kolejnosc listy = kolejnosc galerii; "index" = oryginalna pozycja
        # (uzywana w nazwach plikow), zachowywana przy zmianie kolejnosci
        "decisions": [{"index": d.get("index", i + 1), "url": d["url"],
                       "action": d["action"]}
                      for i, d in enumerate(decisions)],
    }
    (out_dir / "plan.json").write_text(
        json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")

    created = []
    errors = []
    for i, d in enumerate(decisions, 1):
        if d.get("action") != "process":
            continue
        idx = d.get("index", i)
        try:
            data = allegro_client.download_image(d["url"])
        except allegro_client.AllegroError as e:
            errors.append(f"zdjecie {idx}: {e}")
            continue
        created.append(submit_job(
            f"{offer_id}_{idx}.jpg", data, options, source="allegro"))
    skipped = sum(1 for d in decisions if d.get("action") == "keep")
    to_delete = sum(1 for d in decisions if d.get("action") == "delete")
    return jsonify({"jobs": created, "errors": errors,
                    "kept": skipped, "marked_delete": to_delete})


# ---------------- FAZA 2: wykonanie planu na Allegro ----------------

AUDIT_FILE = BASE / "allegro_audit.jsonl"


def audit(operation: str, offer_id: str, details: dict):
    """Dziennik KAZDEJ operacji zapisu do Allegro (append-only)."""
    entry = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
             "operation": operation, "offer_id": offer_id} | details
    try:
        with AUDIT_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


@app.get("/api/allegro/audit")
def allegro_audit_list():
    if not AUDIT_FILE.exists():
        return jsonify([])
    lines = AUDIT_FILE.read_text(encoding="utf-8").strip().splitlines()
    return jsonify([json.loads(x) for x in lines[-50:]][::-1])


def build_plan_preview(offer_id: str) -> dict:
    """Sklada podglad: co zostanie wgrane / zostawione / usuniete."""
    plan_file = DONE_DIR / offer_id / "plan.json"
    if not plan_file.exists():
        raise ValueError("Brak planu dla tej oferty - najpierw wybierz zdjecia")
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    items = []
    ready = True
    for d in plan["decisions"]:
        item = {"index": d["index"], "action": d["action"], "url": d["url"]}
        if d["action"] == "process":
            rel = f"{offer_id}/{offer_id}_{d['index']}.jpg"
            path = DONE_DIR / rel
            item["local"] = rel if path.exists() else None
            if item["local"]:
                item["size_kb"] = round(path.stat().st_size / 1024)
            else:
                ready = False
        items.append(item)
    final_count = sum(1 for d in plan["decisions"] if d["action"] != "delete")
    return {
        "offer_id": offer_id,
        "items": items,
        "ready": ready,
        "final_count": final_count,
        "executed_at": plan.get("executed_at"),
    }


@app.get("/api/allegro/offers/<offer_id>/plan")
def allegro_offer_plan(offer_id):
    try:
        return jsonify(build_plan_preview(offer_id))
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


@app.post("/api/allegro/offers/<offer_id>/execute")
def allegro_offer_execute(offer_id):
    """Wykonuje plan NA ALLEGRO: upload obrobionych, finalna lista zdjec
    (bez usunietych), PATCH oferty. Wymaga jawnego confirm z UI.
    Backup poprzedniej listy zdjec zapisywany w plan.json."""
    body = request.get_json(silent=True) or {}
    if body.get("confirm") is not True:
        return jsonify({"error": "Brak potwierdzenia"}), 400
    try:
        preview = build_plan_preview(offer_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    if not preview["ready"]:
        return jsonify({"error": "Nie wszystkie zdjecia z planu sa obrobione"}), 409
    if preview["final_count"] == 0:
        return jsonify({"error": "Plan usunalby wszystkie zdjecia oferty"}), 409

    plan_file = DONE_DIR / offer_id / "plan.json"
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    try:
        # backup aktualnego stanu oferty (do wycofania) + opis do podmiany
        detail = allegro_client.api_get(f"/sale/product-offers/{offer_id}")
        raw_images = detail.get("images", [])
        as_dicts = bool(raw_images and isinstance(raw_images[0], dict))

        final_images = []
        mapping = {}  # stary url -> nowy url (None = usuniety)
        uploaded = 0
        for item in preview["items"]:
            if item["action"] == "delete":
                mapping[item["url"]] = None
                continue
            if item["action"] == "keep":
                url = item["url"]
            else:  # process -> upload obrobionego pliku
                data = (DONE_DIR / item["local"]).read_bytes()
                url = allegro_client.upload_image(data)
                uploaded += 1
                mapping[item["url"]] = url
            final_images.append({"url": url} if as_dicts else url)

        # te same zdjecia wystepuja w opisie - podmien/usun takze tam
        new_desc, desc_swapped, desc_removed = \
            allegro_client.swap_description_images(
                detail.get("description"), mapping)

        audit("execute_attempt", offer_id, {
            "payload_count": len(final_images),
            "uploaded": uploaded,
            "current_count": len(raw_images),
            "desc_swapped": desc_swapped,
            "desc_removed": desc_removed,
        })
        allegro_client.patch_offer_images(
            offer_id, final_images, description=new_desc)
    except allegro_client.AllegroError as e:
        return jsonify({"error": f"{e} (payload: {len(final_images)} zdjec)"}), 502

    plan["backup_images"] = raw_images
    plan["executed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    final_urls = [i["url"] if isinstance(i, dict) else i for i in final_images]
    plan["final_images"] = final_urls
    plan["url_mapping"] = mapping  # do podmiany opisow w wariantach
    plan_file.write_text(json.dumps(plan, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    audit("execute_plan", offer_id, {
        "uploaded": uploaded,
        "deleted": sum(1 for i in preview["items"] if i["action"] == "delete"),
        "kept": sum(1 for i in preview["items"] if i["action"] == "keep"),
        "final_count": len(final_urls),
        "backup_count": len(raw_images),
    })
    return jsonify({"ok": True, "uploaded": uploaded,
                    "final_count": len(final_images),
                    "executed_at": plan["executed_at"]})


SKU_INDEX_FILE = BASE / "sku_index.json"
SKU_INDEX_TTL = 3600  # 1h


def get_sku_index(refresh: bool = False) -> dict:
    """Mapa: baza SKU (czlon przed pierwszym '-') -> lista ofert.
    Budowana z pelnej listy ofert konta, cache 1h."""
    if not refresh and SKU_INDEX_FILE.exists():
        try:
            cached = json.loads(SKU_INDEX_FILE.read_text(encoding="utf-8"))
            if time.time() - cached.get("at", 0) < SKU_INDEX_TTL:
                return cached["index"]
        except (json.JSONDecodeError, OSError):
            pass
    index = {}
    offset = 0
    while True:
        page = allegro_client.list_offers(offset=offset, limit=100)
        if not page["offers"]:
            break
        for o in page["offers"]:
            sku = o.get("sku") or ""
            base = sku.split("-")[0].strip()
            if not base:
                continue
            index.setdefault(base, []).append({
                "id": str(o["id"]), "sku": sku, "name": o["name"],
                "status": o["status"], "image": o["image"],
            })
        offset += len(page["offers"])
        if offset >= page["total"]:
            break
    try:
        SKU_INDEX_FILE.write_text(
            json.dumps({"at": time.time(), "index": index},
                       ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return index


@app.get("/api/allegro/offers/<offer_id>/siblings")
def allegro_offer_siblings(offer_id):
    """Oferty-warianty rozmiarowe: ten sam czlon SKU przed '-'."""
    try:
        detail = allegro_client.list_offers(offer_id=offer_id)
        if not detail["offers"]:
            return jsonify({"error": "Nie znaleziono oferty"}), 404
        sku = detail["offers"][0].get("sku") or ""
        base = sku.split("-")[0].strip()
        if not base:
            return jsonify({"base": None, "siblings": []})
        index = get_sku_index(refresh=request.args.get("refresh") == "1")
        siblings = [s for s in index.get(base, [])
                    if str(s["id"]) != str(offer_id)]
        for s in siblings:
            s.update(offer_local_flags(s["id"]))
        return jsonify({"base": base, "sku": sku, "siblings": siblings})
    except allegro_client.AllegroError as e:
        return jsonify({"error": str(e)}), 400


@app.post("/api/allegro/offers/<offer_id>/apply-to-siblings")
def allegro_apply_to_siblings(offer_id):
    """Nakłada finalna liste zdjec (z wykonanego planu oferty zrodlowej)
    na wskazane oferty-warianty. Per wariant: backup + PATCH + audyt."""
    body = request.get_json(silent=True) or {}
    if body.get("confirm") is not True:
        return jsonify({"error": "Brak potwierdzenia"}), 400
    targets = [str(t) for t in body.get("targets", [])]
    if not targets:
        return jsonify({"error": "Brak wybranych wariantow"}), 400
    plan_file = DONE_DIR / offer_id / "plan.json"
    if not plan_file.exists():
        return jsonify({"error": "Brak planu oferty zrodlowej"}), 404
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    final_urls = plan.get("final_images")
    if not final_urls:
        return jsonify({"error": "Najpierw wykonaj plan na ofercie zrodlowej"}), 409

    # mapowanie POZYCJAMI: warianty maja te same zdjecia, ale pod INNYMI
    # URL-ami; zdjecie nr N wariantu dzieli los zdjecia nr N zrodla.
    # Bez tego stare zdjecia wariantu zostaja w opisie, a Allegro doklada
    # kazde zdjecie z opisu z powrotem do galerii ("dopisywanie").
    src_mapping = plan.get("url_mapping") or {}
    decisions = plan.get("decisions") or []
    results = []
    for target in targets:
        try:
            t_detail = allegro_client.api_get(f"/sale/product-offers/{target}")
            raw = t_detail.get("images", [])
            as_dicts = bool(raw and isinstance(raw[0], dict))
            sib_urls = [i.get("url") if isinstance(i, dict) else i
                        for i in raw]

            t_final = []
            t_mapping = {}
            for d in decisions:
                pos = d.get("index", 0)
                sib_url = sib_urls[pos - 1] if 0 < pos <= len(sib_urls) else None
                if d["action"] == "delete":
                    if sib_url:
                        t_mapping[sib_url] = None
                    continue
                if d["action"] == "keep":
                    t_final.append(sib_url or d["url"])
                    continue
                new_url = src_mapping.get(d["url"])
                if not new_url:
                    continue
                t_final.append(new_url)
                if sib_url:
                    t_mapping[sib_url] = new_url
            # zdjecia wariantu spoza planu zrodla -> usun takze z opisu
            for extra in sib_urls[len(decisions):]:
                t_mapping.setdefault(extra, None)

            images = [{"url": u} for u in t_final] if as_dicts else t_final
            new_desc, _, _ = allegro_client.swap_description_images(
                t_detail.get("description"), t_mapping)
            allegro_client.patch_offer_images(target, images,
                                              description=new_desc)
            t_dir = DONE_DIR / target
            t_dir.mkdir(parents=True, exist_ok=True)
            (t_dir / "plan.json").write_text(json.dumps({
                "offer_id": target,
                "applied_from": offer_id,
                "backup_images": raw,
                "final_images": t_final,
                "executed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }, indent=2, ensure_ascii=False), encoding="utf-8")
            audit("apply_variant", target, {
                "source_offer": offer_id,
                "final_count": len(t_final),
                "backup_count": len(raw),
            })
            results.append({"id": target, "ok": True})
        except Exception as e:
            results.append({"id": target, "ok": False, "error": str(e)})
    ok = sum(1 for r in results if r["ok"])
    return jsonify({"ok": ok, "failed": len(results) - ok, "results": results})


@app.post("/api/allegro/offers/<offer_id>/rollback")
def allegro_offer_rollback(offer_id):
    """Przywraca poprzednia liste zdjec oferty z backupu w plan.json."""
    body = request.get_json(silent=True) or {}
    if body.get("confirm") is not True:
        return jsonify({"error": "Brak potwierdzenia"}), 400
    plan_file = DONE_DIR / offer_id / "plan.json"
    if not plan_file.exists():
        return jsonify({"error": "Brak planu dla tej oferty"}), 404
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    backup = plan.get("backup_images")
    if not backup:
        return jsonify({"error": "Brak backupu - plan nie byl wykonany"}), 409
    try:
        allegro_client.patch_offer_images(offer_id, backup)
    except allegro_client.AllegroError as e:
        return jsonify({"error": str(e)}), 502
    plan["rolled_back_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    plan_file.write_text(json.dumps(plan, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    audit("rollback", offer_id, {"restored_count": len(backup)})
    return jsonify({"ok": True, "restored": len(backup)})


# ---------------- IdoSell (FAZA 1 - wylacznie odczyt) ----------------

IDO_SCAN_STATE_FILE = BASE / "idosell_scan_state.json"
ido_scan_lock = threading.Lock()
ido_scan_state = {"status": "idle", "total": 0, "checked": 0, "results": {}}

if IDO_SCAN_STATE_FILE.exists():
    try:
        ido_scan_state = json.loads(
            IDO_SCAN_STATE_FILE.read_text(encoding="utf-8"))
        if ido_scan_state.get("status") == "running":
            ido_scan_state["status"] = "stopped"
    except (json.JSONDecodeError, OSError):
        pass


def save_ido_scan_state():
    try:
        IDO_SCAN_STATE_FILE.write_text(
            json.dumps(ido_scan_state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _ido_scan_page_retry(page: int, retries: int = 4) -> dict:
    for attempt in range(retries):
        try:
            return idosell_client.scan_page(page, limit=100)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(15 * (attempt + 1))


def ido_scan_worker():
    page = 0
    try:
        while True:
            with ido_scan_lock:
                if ido_scan_state["status"] != "running":
                    return
            data = _ido_scan_page_retry(page)
            products = data["products"]
            with ido_scan_lock:
                ido_scan_state["total"] = data["total"]
            if not products:
                break
            for p in products:
                while inference_busy.is_set():  # pauza gdy modele miela
                    time.sleep(2)
                with ido_scan_lock:
                    if ido_scan_state["status"] != "running":
                        return
                    already = str(p["id"]) in ido_scan_state["results"]
                if already:  # wznowienie - pomijamy sprawdzone
                    continue
                first = p["images"][0] if p["images"] else None
                entry = {"name": p["name"], "code": p["code"],
                         "image": first["thumb"] if first else None,
                         "images_count": p["images_count"], "needs": None}
                if first:
                    for attempt in range(3):
                        try:
                            thumb = idosell_client.download_image(first["thumb"])
                            flags = thumb_flags(thumb)
                            entry["needs"] = flags["needs"]
                            entry["reasons"] = flags["reasons"]
                            break
                        except Exception:
                            time.sleep(10 * (attempt + 1))
                with ido_scan_lock:
                    ido_scan_state["results"][str(p["id"])] = entry
                    ido_scan_state["checked"] = len(ido_scan_state["results"])
                    checked = ido_scan_state["checked"]
                if checked % 25 == 0:
                    save_ido_scan_state()
            page += 1
            if page >= data["pages"]:
                break
        with ido_scan_lock:
            ido_scan_state["status"] = "done"
    except Exception as e:
        with ido_scan_lock:
            ido_scan_state["status"] = "error"
            ido_scan_state["error"] = f"{e} - kliknij Skanuj aby wznowic"
    finally:
        save_ido_scan_state()


def ido_local_flags(product_id) -> dict:
    """Plakietki: czy produkt ma juz wyniki/plan lokalnie + wynik skanu +
    czy galeria zostala WYSLANA do IdoSell (executed_at bez rollbacku)."""
    out_dir = DONE_DIR / str(product_id)
    has_results = out_dir.exists() and any(out_dir.glob("*.jpg"))
    plan_file = out_dir / "plan.json"
    has_plan = plan_file.exists()
    executed = False
    if has_plan:
        try:
            pl = json.loads(plan_file.read_text(encoding="utf-8"))
            executed = bool(pl.get("executed_at")) and not pl.get("rolled_back_at")
        except (json.JSONDecodeError, OSError):
            pass
    with ido_scan_lock:
        scan = ido_scan_state["results"].get(str(product_id), {})
    return {"processed": has_results, "plan": has_plan, "executed": executed,
            "needs": scan.get("needs"), "reasons": scan.get("reasons")}


@app.get("/api/idosell/status")
def idosell_status():
    return jsonify({"configured": idosell_client.is_configured()})


@app.post("/api/idosell/scan")
def idosell_scan_start():
    fresh = request.args.get("fresh") == "1"
    with ido_scan_lock:
        if ido_scan_state.get("status") == "running":
            return jsonify({"status": "running"})
        prev = ido_scan_state.get("status")
        ido_scan_state.update(status="running", error=None)
        if fresh or prev == "done":
            ido_scan_state["results"] = {}
            ido_scan_state["checked"] = 0
            ido_scan_state["total"] = 0
        # bez fresh: wznowienie - sprawdzone produkty zostaja pominiete
    threading.Thread(target=ido_scan_worker, daemon=True).start()
    return jsonify({"status": "running"})


@app.post("/api/idosell/scan/stop")
def idosell_scan_stop():
    with ido_scan_lock:
        if ido_scan_state.get("status") == "running":
            ido_scan_state["status"] = "stopped"
    save_ido_scan_state()
    return jsonify({"status": ido_scan_state["status"]})


@app.get("/api/idosell/scan/status")
def idosell_scan_status():
    with ido_scan_lock:
        needs = sum(1 for r in ido_scan_state["results"].values()
                    if r.get("needs"))
        return jsonify({
            "status": ido_scan_state.get("status"),
            "checked": ido_scan_state.get("checked", 0),
            "total": ido_scan_state.get("total", 0),
            "needs": needs,
            "error": ido_scan_state.get("error"),
        })


@app.get("/api/idosell/scan/results")
def idosell_scan_results():
    offset = int(request.args.get("offset", 0))
    limit = int(request.args.get("limit", 20))
    with ido_scan_lock:
        rows = [{"id": pid, "name": r["name"], "code": r.get("code"),
                 "image": r["image"], "images_count": r.get("images_count")}
                for pid, r in ido_scan_state["results"].items()
                if r.get("needs")]
    page = rows[offset:offset + limit]
    for p in page:
        p.update(ido_local_flags(p["id"]))
        p["archived_tag"] = False
        p["deleted"] = False
    return jsonify({"products": page, "total": len(rows)})


def _ido_row_ui(p: dict) -> dict:
    first = p["images"][0] if p.get("images") else None
    return {
        "id": p["id"], "code": p.get("code"), "name": p.get("name"),
        "image": first["thumb"] if first else None,
        "images_count": p.get("images_count"),
        "archived_tag": p.get("archived_tag", False),
        "deleted": p.get("deleted", False),
        "category": p.get("category") or [],
        "season": p.get("season") or [],
        "series_id": p.get("series_id"),
        "series_name": p.get("series_name") or "",
    }


def _ido_fetch_rows(ids: list) -> dict:
    """Info (nazwa/kod/zdjecia) dla listy ID jednym zapytaniem (multi-ID)."""
    if not ids:
        return {}
    data = idosell_client.search_products({
        "returnProducts": "active",
        "returnElements": idosell_client.SCAN_RETURN_ELEMENTS + ["parameters", "series"],
        "productParams": [{"productId": int(i)} for i in ids],
        "resultsPage": 0, "resultsLimit": max(100, len(ids)),
    })
    out = {}
    for prod in data.get("results") or []:
        row = _ido_row_ui(idosell_client._product_row(prod))
        out[row["id"]] = row
    return out


def _ido_flagged_pids() -> set:
    """Produkty z aktualnym ostrzezeniem QA (z biezacych zadan)."""
    out = set()
    with lock:
        for jid in job_order:
            j = jobs[jid]
            if (j.get("source") == "idosell" and j.get("status") == "done"
                    and j.get("qa") and not j["qa"].get("ok")):
                out.add(extract_product_id(Path(j["name"]).stem))
    return out


def _ido_mine_index(state: str, query: str) -> list:
    """Lokalny indeks produktow ktore dotknelismy (done/ z planem IdoSell):
    (productId, stan). Filtr po stanie i po fragmencie ID."""
    flagged = _ido_flagged_pids()
    items = []
    for d in DONE_DIR.iterdir():
        if not d.is_dir() or not d.name.isdigit():
            continue
        if query and query not in d.name:
            continue
        plan_file = d / "plan.json"
        if not plan_file.exists():
            continue
        try:
            pl = json.loads(plan_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if pl.get("source") != "idosell":
            continue  # pomijamy plany Allegro
        pid = int(d.name)
        st = {
            "processed": any(d.glob("*.jpg")),
            "plan": True,
            "executed": bool(pl.get("executed_at")),
            "flagged": d.name in flagged,
        }
        if state == "processed" and not st["processed"]:
            continue
        if state == "planned" and st["executed"]:
            continue   # 'z planem' = jeszcze niewyslane
        if state == "executed" and not st["executed"]:
            continue
        if state == "flagged" and not st["flagged"]:
            continue
        items.append((pid, st))
    return items


SERIES_INDEX_FILE = BASE / "series_index.json"
SERIES_INDEX_TTL = 3600  # 1h


def get_ido_series_index(refresh: bool = False) -> dict:
    """Mapa: seriesId (str) -> {name, ids[]} dla calego katalogu (bez archiwum).
    Serwer nie filtruje po seriesId, wiec grupujemy z pelnego skanu, cache 1h.
    Seria = natywny model IdoSell (warianty kolorystyczne maja wspolny seriesId)."""
    if not refresh and SERIES_INDEX_FILE.exists():
        try:
            cached = json.loads(SERIES_INDEX_FILE.read_text(encoding="utf-8"))
            idx = cached.get("index") or {}
            # cache-first: format v2 trzyma gotowe wiersze ("products"); stary
            # (tylko "ids") ignorujemy i przebudowujemy, by przelaczanie bylo instant
            fresh = time.time() - cached.get("at", 0) < SERIES_INDEX_TTL
            v2 = not idx or "products" in next(iter(idx.values()))
            if fresh and v2:
                return idx
        except (json.JSONDecodeError, OSError, StopIteration):
            pass
    index = {}
    page = 0
    while True:
        data = idosell_client.search_products({
            "returnProducts": "active",
            # pelne dane do wyswietlenia -> serie renderujemy z cache bez live API
            "returnElements": idosell_client.SCAN_RETURN_ELEMENTS + ["parameters", "series"],
            "productParametersParams": idosell_client._param_filter(),
            "resultsPage": page, "resultsLimit": 100,
        })
        results = data.get("results") or []
        for prod in results:
            ser = prod.get("productSeries") or {}
            sid = ser.get("seriesId")
            if not sid:
                continue
            entry = index.setdefault(str(sid), {
                "name": ser.get("seriesPanelName") or "", "products": []})
            entry["products"].append(_ido_row_ui(idosell_client._product_row(prod)))
        page += 1
        if page >= data.get("resultsNumberPage", 0) or not results:
            break
    try:
        SERIES_INDEX_FILE.write_text(
            json.dumps({"at": time.time(), "index": index}, ensure_ascii=False),
            encoding="utf-8")
    except OSError:
        pass
    return index


_SERIES_PID_META = {"mtime": 0, "map": {}}


def _ido_series_pid_meta() -> dict:
    """Mapa productId(str) -> {category, season, series_id, series_name} z CACHE
    indeksu serii (NIE buduje go - tylko czyta, gdy istnieje). Dzieki temu
    /api/jobs moze podac metadane do Studio serwerowo (przezywaja odswiezenie
    strony), bez zaleznosci od idoMeta w pamieci przegladarki."""
    if not SERIES_INDEX_FILE.exists():
        return {}
    mt = SERIES_INDEX_FILE.stat().st_mtime
    if _SERIES_PID_META["mtime"] != mt:
        m = {}
        try:
            idx = (json.loads(SERIES_INDEX_FILE.read_text(encoding="utf-8"))
                   or {}).get("index") or {}
            for _sid, e in idx.items():
                for r in e.get("products") or []:
                    m[str(r.get("id"))] = {
                        "category": r.get("category") or [],
                        "season": r.get("season") or [],
                        "series_id": r.get("series_id"),
                        "series_name": r.get("series_name") or ""}
        except (json.JSONDecodeError, OSError):
            m = {}
        _SERIES_PID_META.update(mtime=mt, map=m)
    return _SERIES_PID_META["map"]


@app.get("/api/idosell/series")
def idosell_series_list():
    """Lista serii (modeli) z indeksu - do pickera 'wybierz model' w UI.
    Zwraca id, nazwe i liczbe wariantow; sortowane po nazwie."""
    try:
        idx = get_ido_series_index(refresh=request.args.get("refresh") == "1")
    except idosell_client.IdoSellError as e:
        return jsonify({"error": str(e)}), 400
    out = [{"id": sid, "name": e.get("name") or f"seria {sid}",
            "count": len(e.get("products") or [])} for sid, e in idx.items()]
    out.sort(key=lambda s: s["name"].lower())
    return jsonify({"series": out, "total": len(out)})


@app.get("/api/idosell/products")
def idosell_products():
    """Listing produktow.
    view=catalog (domyslnie): strony skanu (active bez tagu Archiwum) lub
      wyszukiwarka po ID/kodzie (query); sort id_asc|id_desc.
    view=mine: produkty obrobione lokalnie (done/ z planem IdoSell) z
      podfiltrem stanu (state) i sortowaniem."""
    query = request.args.get("query", "").strip()
    offset = int(request.args.get("offset", 0))
    limit = int(request.args.get("limit", 20))
    sort = request.args.get("sort", "id_asc")
    view = request.args.get("view", "catalog")
    state = request.args.get("state", "all")
    avail = request.args.get("avail")  # 'y' / 'n' / None
    if avail not in ("y", "n"):
        avail = None
    season = request.args.get("season", "").strip() or None  # filtr sezonu
    category = request.args.get("category", "").strip() or None  # filtr kategorii
    desc = sort == "id_desc"
    # Kategoria + KONKRETNY sezon to w API IdoSell OR (nie AND). Gdy oba ustawione,
    # serwerowo trzymamy kategorie (strict), a sezon dofiltrowujemy nizej po
    # wyciagnietym parametrze - dzieki temu wynik to twarde AND (best-effort:
    # total/paginacja przyblizone). 'bez-zimy' to disabled i laczy sie strict.
    _SPEC = ("wiosna", "lato", "jesien", "zima")
    _SEASON_PL = {"wiosna": "wiosna", "lato": "lato", "jesien": "jesień", "zima": "zima"}
    post_season = season if (category and season in _SPEC) else None
    srv_season = None if post_season else season
    series_filter = request.args.get("series", "").strip()  # seriesId = caly model
    try:
        if series_filter:
            # caly model (seria) PROSTO z cache (cache-first, instant) - dane do
            # wyswietlenia juz w indeksie, dociagamy tylko lokalne flagi (dysk).
            idx = get_ido_series_index(refresh=request.args.get("refresh") == "1")
            entry = idx.get(series_filter) or {"products": [], "name": ""}
            prods = sorted(entry["products"], key=lambda r: r["id"], reverse=desc)
            total = len(prods)
            rows = []
            for r in prods[offset:offset + limit]:
                base = dict(r)
                base.update(ido_local_flags(base["id"]))
                rows.append(base)
            allf = [ido_local_flags(p["id"]) for p in prods]
            summary = {"shown": len(prods),
                       "processed": sum(1 for f in allf if f["processed"]),
                       "executed": sum(1 for f in allf if f["executed"])}
            return jsonify({"products": rows, "total": total, "summary": summary,
                            "series_name": entry.get("name")})

        if view == "mine":
            items = _ido_mine_index(state, query)
            items.sort(key=lambda t: t[0], reverse=desc)
            total = len(items)
            page = items[offset:offset + limit]
            rowmap = _ido_fetch_rows([pid for pid, _ in page])
            rows = []
            for pid, st in page:
                base = rowmap.get(pid) or {
                    "id": pid, "code": None, "name": None, "image": None,
                    "images_count": None, "archived_tag": False, "deleted": False}
                base.update(st)
                base.update(ido_local_flags(pid))
                rows.append(base)
            return jsonify({"products": rows, "total": total})

        if query and query.isdigit():
            # ID: pelne wyszukanie (z fallbackiem do usunietych), cap 50
            data = idosell_client.find_products(query, limit=50, availability=avail,
                                                season=srv_season, category=category)
            rows = [_ido_row_ui(p) for p in data["products"]]
            rows.sort(key=lambda r: r["id"], reverse=desc)
            rows = rows[offset:offset + limit]
            total = data["total"]
        elif query:
            # nazwa/marka (np. "Bobux") lub kod - paginowane, pelny total
            page_idx = offset // limit
            res = idosell_client.search_active("text", query, page_idx, limit, avail, srv_season, category)
            if res["total"] == 0 and page_idx == 0:
                res = idosell_client.search_active("code", query, 0, limit, avail, srv_season, category)
            rows = [_ido_row_ui(p) for p in res["products"]]
            rows.sort(key=lambda r: r["id"], reverse=desc)
            total = res["total"]
        elif desc:
            total = idosell_client.scan_page(0, 1, availability=avail, season=srv_season,
                                             category=category)["total"]
            start = max(0, total - offset - limit)
            end = max(0, total - offset)
            asc = []
            if end > start:
                p0, p1 = start // limit, (end - 1) // limit
                for p in range(p0, p1 + 1):
                    asc += idosell_client.scan_page(p, limit, availability=avail,
                                                    season=srv_season, category=category,
                                                    with_attrs=True)["products"]
                window = list(reversed(asc[start - p0 * limit:end - p0 * limit]))
            else:
                window = []
            rows = [_ido_row_ui(p) for p in window]
        else:
            page = idosell_client.scan_page(offset // limit, limit, availability=avail,
                                            season=srv_season, category=category, with_attrs=True)
            rows = [_ido_row_ui(p) for p in page["products"]]
            total = page["total"]
    except idosell_client.IdoSellError as e:
        return jsonify({"error": str(e)}), 400
    if post_season:   # twarde AND kategoria + sezon (API daje OR) - dofiltruj
        want = _SEASON_PL.get(post_season, post_season)
        rows = [r for r in rows if want in (r.get("season") or [])]
    for p in rows:
        p.update(ido_local_flags(p["id"]))
    # podsumowanie biezacej listy - "czy model/seria cala zrobiona"
    summary = {
        "shown": len(rows),
        "processed": sum(1 for r in rows if r.get("processed")),
        "executed": sum(1 for r in rows if r.get("executed")),
    }
    return jsonify({"products": rows, "total": total, "summary": summary})


@app.get("/api/idosell/products/<int:product_id>/images")
def idosell_product_images(product_id):
    """Zdjecia produktu + sugestia akcji per zdjecie (jak w Allegro):
    biale tlo -> obrob, kolorowe -> prawdopodobnie stylizacja -> zostaw."""
    try:
        images = idosell_client.get_product_images(product_id)
    except idosell_client.IdoSellError as e:
        return jsonify({"error": str(e)}), 400

    # ROWNOLEGLE pobranie miniatur (~6 watkow) - sekwencyjnie 6-7 zdjec x
    # CDN = kilka sekund, pick-flow "Obrob zdjecia" wisial.
    def _suggest(img):
        try:
            thumb = idosell_client.download_image(img["thumb"])
            if analyze_thumb(thumb)["white"] < 0.40:
                return "keep"  # kolorowe tlo (stylizacja) -> zostaw
        except Exception:
            pass
        return "process"

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as ex:
        suggestions = list(ex.map(_suggest, images))  # zachowuje kolejnosc
    return jsonify({"images": images, "suggestions": suggestions})


def _reset_product_jobs(product_id) -> None:
    """Usuwa STARE zadania danego produktu przed ponowna obrobka - inaczej w
    Studio wisza duplikaty kart (kazda obrobka tworzy nowy job_id na ten sam
    plik), a zdjecia zmienione na 'keep'/'delete' zostawiaja stara wycieta
    wersje. Czysci wpisy zadan + ich pliki robocze (masks/originals) + stare
    obrobione pliki done/{pid}/{pid}_*.jpg."""
    pid = str(product_id)
    removed = []
    with lock:
        rem = [jid for jid in list(job_order)
               if jobs.get(jid) and jobs[jid].get("source") == "idosell"
               and extract_product_id(Path(jobs[jid].get("name", "")).stem) == pid]
        for jid in rem:
            j = jobs.pop(jid, None)
            if jid in job_order:
                job_order.remove(jid)
            removed.append((jid, (j or {}).get("orig")))
    for jid, orig in removed:
        try:
            if orig:
                (ORIGINALS_DIR / orig).unlink(missing_ok=True)
            (MASKS_DIR / f"{jid}.rgb.jpg").unlink(missing_ok=True)
            (MASKS_DIR / f"{jid}.a.png").unlink(missing_ok=True)
        except OSError:
            pass
    d = DONE_DIR / pid
    if d.exists():
        for f in d.glob(f"{pid}_*.jpg"):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
    if removed:
        persist_jobs()


@app.post("/api/idosell/products/<int:product_id>/process")
def idosell_product_process(product_id):
    """Wykonuje decyzje per zdjecie: 'process' -> kolejka obrobki,
    'keep'/'delete' -> tylko zapis w planie (done/{productId}/plan.json).
    NIC nie jest wysylane do IdoSell - zapis wykona faza 2 wg planu."""
    # pierwsze dotkniecie produktu: trwale archiwum oryginalow na dysk D:
    # (idempotentne - kolejne wejscia nic nie nadpisuja)
    archive = archive_originals(product_id)
    _reset_product_jobs(product_id)   # ponowna obrobka: skasuj stare zadania
    options = parse_options(request.form)
    try:
        decisions = json.loads(request.form.get("decisions", "[]"))
    except json.JSONDecodeError:
        return jsonify({"error": "Nieprawidlowy format decyzji"}), 400
    if not decisions:  # brak wyboru = obrob wszystkie
        try:
            images = idosell_client.get_product_images(product_id)
        except idosell_client.IdoSellError as e:
            return jsonify({"error": str(e)}), 400
        decisions = [{"url": i["url"], "image_id": i["id"], "slot": i["slot"],
                      "action": "process"} for i in images]

    out_dir = DONE_DIR / str(product_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    plan = {
        "product_id": product_id,
        "source": "idosell",
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        # kolejnosc listy = docelowa kolejnosc galerii; "index" = oryginalna
        # pozycja (nazwy plikow), image_id/slot -> potrzebne w fazie 2,
        # icons -> przypiecie zdjecia do slotow lista/grupa/bez tla
        "decisions": [{"index": d.get("index", i + 1),
                       "image_id": d.get("image_id"),
                       "slot": d.get("slot"),
                       "url": d["url"],
                       "action": d["action"],
                       "mirror": bool(d.get("mirror")),
                       "icons": [t for t in (d.get("icons") or [])
                                 if t in idosell_client.SETTABLE_ICON_TYPES]}
                      for i, d in enumerate(decisions)],
    }
    (out_dir / "plan.json").write_text(
        json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")

    created = []
    errors = []
    dup_entries = []   # (job_id, index, data) do wykrycia duplikatow
    for i, d in enumerate(decisions, 1):
        act = d.get("action")
        if act not in ("process", "keep"):
            continue   # delete -> tylko w planie, bez karty
        idx = d.get("index", i)
        try:
            data = idosell_client.download_image(d["url"])
        except idosell_client.IdoSellError as e:
            errors.append(f"zdjecie {idx}: {e}")
            continue
        if act == "keep":
            # zostawione -> widoczna karta (oryginal bez ciecia, bez maski)
            jid = submit_kept(product_id, idx, data, fashion=bool(d.get("fashion")))
            if jid:
                dup_entries.append((jid, idx, data))
            continue
        # mirror per zdjecie (standaryzacja kierunku noska)
        job_opts = {**options, "mirror": bool(d.get("mirror"))}
        jid = submit_job(f"{product_id}_{idx}.jpg", data, job_opts, source="idosell")
        created.append(jid)
        dup_entries.append((jid, idx, data))
    _flag_duplicates(dup_entries)   # nominacja slabszej rozdz. do usuniecia
    skipped = sum(1 for d in decisions if d.get("action") == "keep")
    to_delete = sum(1 for d in decisions if d.get("action") == "delete")
    return jsonify({"jobs": created, "errors": errors,
                    "kept": skipped, "marked_delete": to_delete,
                    "archive": archive})


def _dhash(data: bytes, size: int = 8) -> int:
    """Percepcyjny hash roznicowy (dHash) - odporny na rozmiar/kompresje,
    czuly na tresc. Bez dodatkowych zaleznosci (sam PIL)."""
    from io import BytesIO
    from PIL import Image
    im = Image.open(BytesIO(data)).convert("L").resize(
        (size + 1, size), Image.LANCZOS)
    px = list(im.getdata())
    bits = 0
    for r in range(size):
        row = r * (size + 1)
        for c in range(size):
            bits = (bits << 1) | (1 if px[row + c] < px[row + c + 1] else 0)
    return bits


def _flag_duplicates(entries, thresh: int = 8) -> int:
    """entries: lista (job_id, photo_index, data_bytes). Wykrywa bliskie
    DUPLIKATY (dHash, odleglosc Hamminga <= thresh) i flaguje te o SLABSZEJ
    rozdzielczosci: dup=True + dup_keep=index lepszego (nominacja do usuniecia).
    Wyzsza rozdzielczosc w grupie zostaje. Zwraca liczbe oflagowanych."""
    from io import BytesIO
    from PIL import Image
    items = []
    for jid, idx, data in entries:
        if not data:
            continue
        try:
            h = _dhash(data)
            with Image.open(BytesIO(data)) as im:
                area = im.size[0] * im.size[1]
        except Exception:
            continue
        items.append({"jid": jid, "idx": idx, "hash": h, "area": area})
    flagged, n, used = 0, len(items), [False] * len(items)
    for i in range(n):
        if used[i]:
            continue
        cluster = [i]
        for j in range(i + 1, n):
            if not used[j] and bin(items[i]["hash"] ^ items[j]["hash"]).count("1") <= thresh:
                cluster.append(j); used[j] = True
        used[i] = True
        if len(cluster) < 2:
            continue
        keeper = max(cluster, key=lambda k: items[k]["area"])
        keep_idx = items[keeper]["idx"]
        for k in cluster:
            if k == keeper:
                continue
            jid = items[k]["jid"]
            with lock:
                if jid in jobs:
                    jobs[jid]["dup"] = True
                    jobs[jid]["dup_keep"] = keep_idx
                    flagged += 1
    if flagged:
        persist_jobs()
    return flagged


def _archived_images(product_id):
    """Oryginaly z trwalego archiwum (D:\\idosell_backup\\{pid}) jako
    [{id, slot, url, data}] - ZRODLO PRAWDY przy ponownej obrobce. Aktualny
    IdoSell PO WYSYLCE to juz NASZE obrobione zdjecia, wiec reprocess MUSI brac
    archiwum (zrzucone przy pierwszej obrobce, przed jakimkolwiek zapisem).
    None gdy brak archiwum/dysku (wtedy fallback do aktualnego IdoSell)."""
    root = ido_backup_root()
    if root is None:
        return None
    manifest = root / str(product_id) / "_originals.json"
    if not manifest.exists():
        return None
    try:
        info = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    out = []
    for e in info.get("images", []):
        fp = root / str(product_id) / (e.get("file") or "")
        try:
            data = fp.read_bytes()
        except OSError:
            continue
        out.append({"id": e.get("id"), "slot": e.get("slot"),
                    "url": e.get("url"), "data": data})
    return out or None


def _ido_process_default(product_id: int, options: dict) -> dict:
    """Obrobka wsadowa produktu domyslnym planem: zdjecia 'Obrob' (fashion ->
    'keep'), zdjecie #1 -> ikona Listy + Grupy. ZRODLO = trwale archiwum
    ORYGINALOW (nie aktualny IdoSell - po wysylce to obrobione!). Dograne
    recznie zdjecia (extras) z poprzedniego planu sa ZACHOWANE. Zwraca {ok,...}."""
    archive_originals(product_id)   # idempotent: pierwszy zrzut = prawdziwe oryginaly
    # zachowaj dograne recznie zdjecia (extras) z poprzedniego planu - reprocess
    # ma je przepuscic ponownie, nie wyrzucic
    pf = DONE_DIR / str(product_id) / "plan.json"
    old_extras = []
    if pf.exists():
        try:
            old = json.loads(pf.read_text(encoding="utf-8"))
            old_extras = [d for d in old.get("decisions", []) if d.get("extra")]
        except (json.JSONDecodeError, OSError):
            pass
    _reset_product_jobs(product_id)   # skasuj stare zadania (karty)
    # ORYGINALY z archiwum; fallback do aktualnego IdoSell gdy brak archiwum
    images = _archived_images(product_id)
    if images is None:
        try:
            images = [{"id": i["id"], "slot": i["slot"], "url": i["url"],
                       "data": None}
                      for i in idosell_client.get_product_images(product_id)]
        except idosell_client.IdoSellError as e:
            return {"ok": False, "error": str(e), "jobs": 0}
    if not images:
        return {"ok": False, "error": "produkt bez zdjec", "jobs": 0}

    # wykryj fashion (kolorowe/zlozone tlo) -> "keep"; reszta -> "process".
    decisions, jobs_made, kept = [], 0, 0
    dup_entries = []   # (job_id, index, data) do wykrycia duplikatow
    for i, im in enumerate(images):
        data = im.get("data")
        if data is None:
            try:
                data = idosell_client.download_image(im["url"])
            except idosell_client.IdoSellError:
                data = None
        is_fashion = False
        if data:
            try:
                is_fashion = analyze_thumb(data)["white"] < 0.40
            except Exception:
                is_fashion = False
        action = "keep" if is_fashion else "process"
        decisions.append({"index": i + 1, "image_id": im["id"], "slot": im["slot"],
                          "url": im["url"], "action": action, "mirror": False,
                          "fashion": is_fashion,
                          # zdjecie #1 -> Lista + Grupa + Bez tla (auction) -
                          # "bez tla" idzie tez na Allegro, wiec auto-zaznaczamy
                          "icons": ["shop", "group", "auction"] if i == 0 else []})
        if action == "process" and data:
            jid = submit_job(f"{product_id}_{i + 1}.jpg", data,
                             {**options, "mirror": False}, source="idosell",
                             priority=False)   # BULK (masowka/feeder) -> wstrzymywany pauza
            dup_entries.append((jid, i + 1, data))
            jobs_made += 1
        elif action == "keep" and data:
            jid = submit_kept(product_id, i + 1, data, fashion=True)
            if jid:
                dup_entries.append((jid, i + 1, data))
            kept += 1
    # dolacz ZACHOWANE dograne zdjecia (extras) - z plikow w extras/
    for d in old_extras:
        ex = d.get("extra")
        if not ex or not (EXTRAS_DIR / ex).exists():
            continue
        try:
            data = (EXTRAS_DIR / ex).read_bytes()
        except OSError:
            continue
        idx = d.get("index") or (max([900] + [x["index"] for x in decisions]) + 1)
        action = d.get("action") if d.get("action") in ("process", "keep") else "process"
        decisions.append({"index": idx, "image_id": None, "slot": None,
                          "url": None, "action": action,
                          "mirror": bool(d.get("mirror")),
                          "fashion": bool(d.get("fashion")), "extra": ex,
                          "icons": d.get("icons") or []})
        if action == "keep":
            jid = submit_kept(product_id, idx, data, fashion=bool(d.get("fashion")))
            if jid:
                dup_entries.append((jid, idx, data))
            kept += 1
        else:
            jid = submit_job(f"{product_id}_{idx}.jpg", data,
                             {**options, "mirror": bool(d.get("mirror"))},
                             source="idosell")
            dup_entries.append((jid, idx, data))
            jobs_made += 1
    dups = _flag_duplicates(dup_entries)   # nominacja slabszej rozdz. do usuniecia
    out_dir = DONE_DIR / str(product_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan.json").write_text(json.dumps({
        "product_id": product_id, "source": "idosell",
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "decisions": decisions,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": bool(decisions), "jobs": jobs_made, "kept": kept,
            "dups": dups,
            "error": None if decisions else "nie pobrano zdjec"}


@app.post("/api/idosell/bulk-process")
def idosell_bulk_process():
    """Masowa obrobka wielu produktow naraz (domyslny plan per produkt).
    Body form: product_ids (JSON lista) + opcje obrobki."""
    options = parse_options(request.form)
    try:
        ids = [int(x) for x in json.loads(request.form.get("product_ids", "[]"))]
    except (json.JSONDecodeError, ValueError):
        return jsonify({"error": "Nieprawidlowa lista produktow"}), 400
    if not ids:
        return jsonify({"error": "Pusta lista produktow"}), 400
    results, total_jobs = [], 0
    for pid in ids:
        r = _ido_process_default(pid, options)
        total_jobs += r["jobs"]
        results.append({"id": pid, **r})
    ok = sum(1 for r in results if r["ok"])
    return jsonify({"results": results, "ok": ok,
                    "failed": len(results) - ok, "total_jobs": total_jobs})


@app.post("/api/idosell/products/<int:product_id>/photo-action")
def idosell_photo_action(product_id):
    """Zmiana akcji JEDNEGO zdjecia w istniejacym planie (process/keep/delete)
    po obrobce - bez przerabiania calego produktu. 'process' dokolejkowuje
    obrobke tylko tego zdjecia. Po zmianie wyslij produkt ponownie (nadpisze)."""
    options = parse_options(request.form)
    idx = request.form.get("index", type=int)
    action = request.form.get("action")
    if action not in ("process", "keep", "delete"):
        return jsonify({"error": "Nieprawidlowa akcja"}), 400
    pf = DONE_DIR / str(product_id) / "plan.json"
    if not pf.exists():
        return jsonify({"error": "Brak planu dla tego produktu"}), 404
    plan = json.loads(pf.read_text(encoding="utf-8"))
    dec = next((d for d in plan.get("decisions", [])
                if d.get("index") == idx), None)
    if dec is None:
        return jsonify({"error": "Nie ma takiego zdjecia w planie"}), 404
    dec["action"] = action
    if action == "delete":
        dec["icons"] = []   # usuniete zdjecie nie moze byc ikona
    pf.write_text(json.dumps(plan, indent=2, ensure_ascii=False),
                  encoding="utf-8")
    _remove_product_job(product_id, idx)   # skasuj stara karte tego zdjecia
    queued = False
    if action in ("process", "keep"):
        try:
            if dec.get("extra"):   # zdjecie dodane z dysku
                data = (EXTRAS_DIR / dec["extra"]).read_bytes()
            else:
                data = idosell_client.download_image(dec["url"])
        except (idosell_client.IdoSellError, OSError) as e:
            return jsonify({"error": f"Pobranie zdjecia: {e}"}), 400
        if action == "keep":
            # zostawione -> widoczna karta (oryginal bez ciecia)
            submit_kept(product_id, idx, data, fashion=bool(dec.get("fashion")))
        else:   # process -> dokolejkuj obrobke tego jednego zdjecia
            job_opts = {**options, "mirror": bool(dec.get("mirror"))}
            submit_job(f"{product_id}_{idx}.jpg", data, job_opts, source="idosell")
            queued = True
    return jsonify({"ok": True, "action": action, "queued": queued})


@app.post("/api/idosell/products/<int:product_id>/plan-curate")
def idosell_plan_curate(product_id):
    """Kuracja planu PO obrobce, bez ponownej obrobki: zmiana KOLEJNOSCI galerii
    (op=move) i przypisania IKON Lista/Grupa/Bez tla (op=icon). Zapisuje plan.json
    - wysylka honoruje kolejnosc decyzji i ikony. Ikona jest unikalna (jedno
    zdjecie na typ). Usuniete zdjecia pomijamy."""
    pf = DONE_DIR / str(product_id) / "plan.json"
    if not pf.exists():
        return jsonify({"error": "Brak planu dla tego produktu"}), 404
    plan = json.loads(pf.read_text(encoding="utf-8"))
    decs = plan.get("decisions") or []
    idx = request.form.get("index", type=int)
    pos = next((i for i, d in enumerate(decs) if d.get("index") == idx), None)
    if pos is None:
        return jsonify({"error": "Nie ma takiego zdjecia w planie"}), 404
    op = request.form.get("op")
    if op == "move":
        new = pos + (request.form.get("dir", type=int) or 0)
        if 0 <= new < len(decs):
            decs[pos], decs[new] = decs[new], decs[pos]
    elif op == "icon":
        typ = request.form.get("type")
        if typ not in idosell_client.SETTABLE_ICON_TYPES:
            return jsonify({"error": "Nieprawidlowy typ ikony"}), 400
        if decs[pos].get("action") == "delete":
            return jsonify({"error": "Usuniete zdjecie nie moze byc ikona"}), 400
        cur = decs[pos].get("icons") or []
        if typ in cur:
            decs[pos]["icons"] = [t for t in cur if t != typ]
        else:
            for d in decs:    # ikona unikalna - zdejmij z innych
                if d.get("icons"):
                    d["icons"] = [t for t in d["icons"] if t != typ]
            decs[pos]["icons"] = (decs[pos].get("icons") or []) + [typ]
    else:
        return jsonify({"error": "Nieznana operacja"}), 400
    plan["decisions"] = decs
    pf.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    return jsonify({"ok": True})


@app.post("/api/idosell/products/<int:product_id>/add-local")
def idosell_add_local(product_id):
    """Zapisuje zdjecie z dysku do extras/ - do dolaczenia do planu."""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "Brak pliku"}), 400
    data = f.read()
    ext = Path(f.filename or "img.jpg").suffix or ".jpg"
    fname = f"{product_id}_{uuid.uuid4().hex[:8]}{ext}"
    try:
        (EXTRAS_DIR / fname).write_bytes(data)
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"extra": fname, "name": f.filename})


@app.get("/extras/<path:relpath>")
def serve_extra(relpath):
    return send_from_directory(EXTRAS_DIR, relpath)


@app.post("/api/idosell/products/<int:product_id>/plan-add")
def idosell_plan_add(product_id):
    """Dodaje zdjecie z dysku (zapisane przez /add-local) do ISTNIEJACEGO
    planu jako 'process' + dokolejkowuje obrobke - bez przerabiania calego
    produktu. Po obrobce wyslij produkt ponownie (nadpisze galeria)."""
    options = parse_options(request.form)
    extra = request.form.get("extra")
    if not extra or not (EXTRAS_DIR / extra).exists():
        return jsonify({"error": "Brak pliku (extra)"}), 400
    pf = DONE_DIR / str(product_id) / "plan.json"
    if not pf.exists():
        return jsonify({"error": "Brak planu dla tego produktu"}), 404
    plan = json.loads(pf.read_text(encoding="utf-8"))
    decs = plan.get("decisions", [])
    # wysoki index (>=901) dla zdjec z dysku - nie koliduje ze slotami galerii
    new_idx = max([900] + [d.get("index", 0) for d in decs]) + 1
    decs.append({"index": new_idx, "image_id": None, "slot": None,
                 "url": None, "action": "process", "mirror": False,
                 "icons": [], "extra": extra})
    plan["decisions"] = decs
    pf.write_text(json.dumps(plan, indent=2, ensure_ascii=False),
                  encoding="utf-8")
    try:
        data = (EXTRAS_DIR / extra).read_bytes()
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    submit_job(f"{product_id}_{new_idx}.jpg", data,
               {**options, "mirror": False}, source="idosell")
    return jsonify({"ok": True, "index": new_idx})


@app.post("/api/idosell/products/<int:product_id>/add-url")
def idosell_add_url(product_id):
    """Dodaje zdjecie z LINKU (internet) do istniejacego planu jako 'process'
    + dokolejkowuje obrobke. Do uzupelniania brakujacych ujec znalezionych w
    sieci, gdy produkt ma za malo zdjec. To UZYTKOWNIK wskazuje link, wiec brak
    ryzyka zlego dopasowania. Po obrobce wyslij produkt (nadpisze galeria)."""
    from io import BytesIO
    import requests as rq
    from PIL import Image
    options = parse_options(request.form)
    url = (request.form.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "Podaj poprawny adres http(s)"}), 400
    pf = DONE_DIR / str(product_id) / "plan.json"
    if not pf.exists():
        return jsonify({"error": "Najpierw obrob ten produkt (brak planu)"}), 404
    try:
        resp = rq.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return jsonify({"error": f"Pobranie nieudane ({resp.status_code})"}), 400
        content = resp.content
        with Image.open(BytesIO(content)) as im:   # walidacja: czy to obraz
            w, h = im.size
    except Exception as e:
        return jsonify({"error": f"Nie udalo sie pobrac obrazu: {e}"}), 400
    fname = f"{product_id}_{uuid.uuid4().hex[:8]}.jpg"
    try:
        (EXTRAS_DIR / fname).write_bytes(content)
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    plan = json.loads(pf.read_text(encoding="utf-8"))
    decs = plan.get("decisions", [])
    # wysoki index (>=901) - zdjecie dodane laduje na koncu galerii (mozna
    # przesunac w Studio); nie koliduje ze slotami oryginalnej galerii
    new_idx = max([900] + [d.get("index", 0) for d in decs]) + 1
    decs.append({"index": new_idx, "image_id": None, "slot": None,
                 "url": None, "action": "process", "mirror": False,
                 "icons": [], "extra": fname})
    plan["decisions"] = decs
    pf.write_text(json.dumps(plan, indent=2, ensure_ascii=False),
                  encoding="utf-8")
    submit_job(f"{product_id}_{new_idx}.jpg", content,
               {**options, "mirror": False}, source="idosell")
    return jsonify({"ok": True, "index": new_idx, "w": w, "h": h})


# ------------- IdoSell FAZA 2: wykonanie planu (zapis do sklepu) -------------

IDO_AUDIT_FILE = BASE / "idosell_audit.jsonl"
IDO_ORIGINALS_DIR = ORIGINALS_DIR / "idosell"
LOWRES_SENT_LOG = BASE / "lowres_sent.jsonl"   # male zrodla, ktore poszly do sklepu


def ido_backup_root():
    """Katalog trwalego archiwum oryginalow (domyslnie D:\\idosell_backup).
    Zwraca None gdy dysk niedostepny - zapis wtedy nie ruszy bez archiwum."""
    cfg = idosell_client.load_config() or {}
    root = Path(cfg.get("backup_dir") or "D:\\idosell_backup")
    if root.drive and not Path(root.drive + "\\").exists():
        return None
    return root


def archive_originals(product_id: int) -> dict:
    """Trwale archiwum ORYGINALOW w pelnej rozdzielczosci (osobny dysk).
    Idempotentne: pierwszy zrzut zostaje na zawsze (manifest _originals.json
    = znacznik), kolejne wywolania niczego nie nadpisuja - to gwarantuje,
    ze nie stracimy prawdziwego oryginalu nawet po wielu zapisach."""
    root = ido_backup_root()
    if root is None:
        return {"ok": False, "reason": "Dysk backupu niedostepny (D:)"}
    target = root / str(product_id)
    manifest = target / "_originals.json"
    if manifest.exists():
        try:
            info = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            info = {}
        return {"ok": True, "already": True, "dir": str(target),
                "count": len(info.get("images", [])),
                "captured_at": info.get("captured_at")}
    try:
        images = idosell_client.get_product_images(product_id)
    except idosell_client.IdoSellError as e:
        return {"ok": False, "reason": str(e)}
    target.mkdir(parents=True, exist_ok=True)
    img_entries = []
    for img in images:
        data = idosell_client.download_image(img["url"])
        fname = img["id"] or f"{product_id}_{img['slot']}.jpg"
        (target / fname).write_bytes(data)
        img_entries.append({"id": img["id"], "slot": img["slot"],
                            "url": img["url"], "hash": img["hash"],
                            "width": img["width"], "height": img["height"],
                            "bytes": len(data), "file": fname})
    icon_entries = []
    try:
        for typ, info in idosell_client.get_product_icons(product_id).items():
            if info["exists"] and info["url"]:
                data = idosell_client.download_image(info["url"])
                ext = ".webp" if ".webp" in info["url"] else ".jpg"
                fname = f"icon_{typ}{ext}"
                (target / fname).write_bytes(data)
                icon_entries.append({"type": typ, "url": info["url"],
                                     "file": fname, "bytes": len(data)})
    except idosell_client.IdoSellError:
        pass
    manifest.write_text(json.dumps({
        "product_id": product_id,
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": "Pierwszy zrzut oryginalow - NIE nadpisywac.",
        "images": img_entries,
        "icons": icon_entries,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    ido_audit("archive_originals", product_id, {
        "images": len(img_entries), "icons": len(icon_entries),
        "dir": str(target)})
    return {"ok": True, "already": False, "dir": str(target),
            "count": len(img_entries), "captured_at": None}


def ido_audit(operation: str, product_id, details: dict):
    """Dziennik KAZDEJ operacji zapisu do IdoSell (append-only)."""
    entry = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
             "operation": operation, "product_id": product_id} | details
    try:
        with IDO_AUDIT_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


@app.get("/api/idosell/audit")
def idosell_audit_list():
    if not IDO_AUDIT_FILE.exists():
        return jsonify([])
    lines = IDO_AUDIT_FILE.read_text(encoding="utf-8").strip().splitlines()
    return jsonify([json.loads(x) for x in lines[-50:]][::-1])


@app.get("/api/idosell/lowres-log")
def idosell_lowres_log():
    """Lista zdjec z MALYM zrodlem, ktore poszly do sklepu (worklist do poprawy).
    Deduplikacja po (produkt, index) - liczy sie najnowszy wpis."""
    if not LOWRES_SENT_LOG.exists():
        return jsonify({"items": [], "total": 0})
    latest = {}
    for line in LOWRES_SENT_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        latest[(e.get("product_id"), e.get("index"))] = e
    items = sorted(latest.values(), key=lambda x: x.get("sent_at") or "",
                   reverse=True)
    return jsonify({"items": items, "total": len(items)})


def load_ido_plan(product_id: int) -> dict:
    plan_file = DONE_DIR / str(product_id) / "plan.json"
    if not plan_file.exists():
        raise ValueError("Brak planu dla tego produktu - najpierw wybierz zdjecia")
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    if plan.get("source") != "idosell":
        raise ValueError("Plan tego ID pochodzi z modulu Allegro, nie IdoSell")
    return plan


def save_ido_plan(product_id: int, plan: dict):
    (DONE_DIR / str(product_id) / "plan.json").write_text(
        json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")


def _gallery_ordered(rows, mode="fashion_second"):
    """Domyslna kolejnosc galerii: [1: glowny profil] [2: fashion/lifestyle]
    [3+: reszta wg oryginalnej kolejnosci]; delete na koniec (i tak nie ide do
    galerii). rows = decisions albo items (potrzebne pola: action, fashion).
    mode 'raw' = bez zmian. Wspolne dla podgladu/wysylki (build_ido_plan_preview)
    i pozycji w siatce Studio (gallery_pos) - zeby pokazywaly to samo."""
    if mode != "fashion_second":
        return list(rows)
    non_del = [r for r in rows if r.get("action") != "delete"]
    dels = [r for r in rows if r.get("action") == "delete"]
    fashion = [r for r in non_del if r.get("fashion")]
    nonfash = [r for r in non_del if not r.get("fashion")]
    if fashion and nonfash:
        return [nonfash[0]] + fashion + nonfash[1:] + dels
    return list(rows)


def build_ido_plan_preview(product_id: int) -> dict:
    """Sklada podglad: co zostanie wgrane / zostawione / usuniete."""
    plan = load_ido_plan(product_id)
    items = []
    ready = True
    for d in plan["decisions"]:
        item = {"index": d["index"], "action": d["action"], "url": d["url"],
                "image_id": d.get("image_id"), "slot": d.get("slot"),
                "icons": d.get("icons") or [], "extra": d.get("extra"),
                "fashion": bool(d.get("fashion"))}
        if d["action"] == "process":
            rel = f"{product_id}/{product_id}_{d['index']}.jpg"
            path = DONE_DIR / rel
            item["local"] = rel if path.exists() else None
            if item["local"]:
                item["size_kb"] = round(path.stat().st_size / 1024)
            else:
                ready = False
        items.append(item)
    items = _gallery_ordered(items, plan.get("gallery_order", "fashion_second"))
    final_count = sum(1 for d in plan["decisions"] if d["action"] != "delete")
    backup_root = ido_backup_root()
    archived = bool(backup_root and
                    (backup_root / str(product_id) / "_originals.json").exists())
    return {
        "product_id": product_id,
        "items": items,
        "ready": ready,
        "final_count": final_count,
        "executed_at": plan.get("executed_at"),
        "verified": plan.get("verified"),
        "apply_macro": idosell_client.apply_macro_setting(),
        "originals_archived": archived,
        "backup_available": backup_root is not None,
    }


@app.get("/api/idosell/products/<int:product_id>/plan")
def idosell_product_plan(product_id):
    try:
        return jsonify(build_ido_plan_preview(product_id))
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


def ido_backup_gallery(product_id: int, current: list[dict]) -> list[dict]:
    """Fizyczny backup aktualnych zdjec produktu na dysk
    (originals/idosell/{productId}/). Zwraca liste wpisow do plan.json."""
    backup_dir = IDO_ORIGINALS_DIR / str(product_id)
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = []
    for img in current:
        data = idosell_client.download_image(img["url"])
        fname = img["id"] or f"{product_id}_{img['slot']}.jpg"
        (backup_dir / fname).write_bytes(data)
        backup.append({"id": img["id"], "slot": img["slot"],
                       "priority": img["priority"], "hash": img["hash"],
                       "url": img["url"],
                       "file": f"idosell/{product_id}/{fname}"})
    return backup


def ido_backup_icons(product_id: int) -> list[dict]:
    """Backup 3 ikon produktu (lista/grupa/bez tla) na dysk. Zapamietuje
    tez, ktorych ikon NIE bylo (rollback wtedy je skasuje)."""
    backup_dir = IDO_ORIGINALS_DIR / str(product_id)
    backup_dir.mkdir(parents=True, exist_ok=True)
    state = idosell_client.get_product_icons(product_id)
    backup = []
    for typ, info in state.items():
        entry = {"type": typ, "existed": info["exists"], "file": None}
        if info["exists"] and info["url"]:
            ext = ".webp" if ".webp" in info["url"] else ".jpg"
            fname = f"icon_{typ}{ext}"
            (backup_dir / fname).write_bytes(
                idosell_client.download_image(info["url"]))
            entry["file"] = f"idosell/{product_id}/{fname}"
        backup.append(entry)
    return backup


def ido_verify_gallery(product_id: int, expected_count: int,
                       expected_icons: list | None = None) -> dict:
    """Weryfikacja GET-em po zapisie: liczba zdjec, ciaglosc slotow,
    obecnosc przypietych ikon."""
    after = idosell_client.get_product_images(product_id)
    slots = sorted(i["slot"] for i in after if i["slot"] is not None)
    ok = (len(after) == expected_count
          and slots == list(range(1, expected_count + 1)))
    out = {"ok": ok, "count": len(after), "expected": expected_count,
           "slots": slots}
    if expected_icons:
        state = idosell_client.get_product_icons(product_id)
        missing = [t for t in expected_icons if not state[t]["exists"]]
        out["icons_ok"] = not missing
        out["icons_missing"] = missing
        out["ok"] = out["ok"] and not missing
    return out


class IdoExecError(Exception):
    """Blad wykonania planu z kodem HTTP - wspolny dla pojedynczego
    i wsadowego zapisu."""
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _id_stem(image_id) -> str:
    """Rdzen image_id bez rozszerzenia. IdoSell raportuje TEN SAM slot raz jako
    '8524_4.webp', raz '8524_4.jpg' (plan ze skanu vs live GET) - porownujemy po
    slocie, nie po rozszerzeniu, inaczej 'keep' falszywie wykrywa zniknicie."""
    return str(image_id or "").rsplit(".", 1)[0]


def _log_lowres_sent(product_id: int, preview: dict, sent_at: str) -> int:
    """Loguje zdjecia z MALYM zrodlem (dluzszy bok < LOWRES_MAX_PX), ktore
    POSZLY do sklepu - zbiera sie lista 'do poprawy' (znalezc lepsze zrodlo,
    podmienic, doslac). Append-only do LOWRES_SENT_LOG."""
    from PIL import Image as _Img
    pid = str(product_id)
    entries = []
    for item in preview.get("items", []):
        if item.get("action") == "delete":
            continue
        idx = item.get("index")
        with lock:
            jid = next((j for j in job_order
                        if jobs[j].get("name") == f"{pid}_{idx}.jpg"), None)
            sw = jobs[jid].get("src_w") if jid else None
            sh = jobs[jid].get("src_h") if jid else None
            orig = jobs[jid].get("orig") if jid else None
        if sw is None and orig:   # nie policzone wczesniej - z pliku oryginalu
            try:
                with _Img.open(ORIGINALS_DIR / orig) as im:
                    sw, sh = im.size
            except Exception:
                continue
        if sw and sh and max(sw, sh) < LOWRES_MAX_PX:
            entries.append({"product_id": product_id, "index": idx,
                            "src_w": sw, "src_h": sh, "sent_at": sent_at})
    if entries:
        try:
            with LOWRES_SENT_LOG.open("a", encoding="utf-8") as f:
                for e in entries:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
        except OSError:
            pass
    return len(entries)


def _ido_execute_one(product_id: int) -> dict:
    """Rdzen zapisu planu W SKLEPIE dla jednego produktu: backup na dysk,
    PUT galerii w sloty 1..N (bez okna z pusta galeria), kasowanie
    nadmiarowych slotow, ikony, weryfikacja GET-em. Zwraca dict wyniku
    albo rzuca IdoExecError(msg, status). Wspolny dla execute i bulk."""
    try:
        preview = build_ido_plan_preview(product_id)
    except ValueError as e:
        raise IdoExecError(str(e), 404)
    if not preview["ready"]:
        raise IdoExecError("Nie wszystkie zdjecia z planu sa obrobione", 409)
    if preview["final_count"] == 0:
        raise IdoExecError("Plan usunalby wszystkie zdjecia produktu", 409)

    # OBOWIAZKOWE archiwum oryginalow na dysku D: zanim cokolwiek nadpiszemy
    archive = archive_originals(product_id)
    if not archive.get("ok"):
        raise IdoExecError(
            f"Brak archiwum oryginalow - zapis wstrzymany. "
            f"{archive.get('reason', '')}", 412)

    plan = load_ido_plan(product_id)
    try:
        # 1) swiezy stan galerii + kontrola przeterminowania planu.
        # Tylko decyzje "Zostaw" musza miec swoje zdjecie w sklepie
        # (bo bierzemy ich bajty ze sklepu); "Obrob" ma plik na dysku,
        # "Usun" i tak znika - wiec puste/zmienione zdjecia zrodlowe ich
        # nie blokuja (pozwala dosłac do recznie wyczyszczonej galerii).
        current = idosell_client.get_product_images(product_id)
        current_stems = {_id_stem(i["id"]) for i in current}
        keep_ids = {d.get("image_id") for d in plan["decisions"]
                    if d["action"] == "keep" and d.get("image_id")}
        # porownanie po slocie (bez rozszerzenia) - .webp w planie vs .jpg w sklepie
        missing = {kid for kid in keep_ids if _id_stem(kid) not in current_stems}
        if missing:
            raise IdoExecError(
                f"Zdjecia oznaczone 'Zostaw' zniknely ze sklepu "
                f"({', '.join(sorted(missing))}) - otworz produkt "
                f"i zapisz plan ponownie", 409)

        # 2) fizyczny backup aktualnych zdjec + 3 ikon na dysk
        backup = ido_backup_gallery(product_id, current)
        backup_icons = ido_backup_icons(product_id)

        # 3) finalna galeria wg kolejnosci planu (base64)
        images_b64 = []
        item_b64 = {}  # index decyzji -> base64 (do przypiec ikon)
        uploaded = 0
        for item in preview["items"]:
            if item["action"] == "delete":
                continue
            if item["action"] == "process":
                data = (DONE_DIR / item["local"]).read_bytes()
                uploaded += 1
            else:  # keep - bajty ze swiezego backupu (match po slocie, bez rozszerzenia)
                entry = next((b for b in backup
                              if _id_stem(b["id"]) == _id_stem(item["image_id"])), None)
                if entry is None:
                    raise IdoExecError(
                        f"Zdjecie {item['image_id']} (Zostaw) zniknelo "
                        f"ze sklepu - zapisz plan ponownie", 409)
                data = (ORIGINALS_DIR / entry["file"]).read_bytes()
            b64 = base64.b64encode(data).decode()
            images_b64.append(b64)
            item_b64[item["index"]] = b64

        # przypiecia ikon: te same bajty co zdjecie z galerii (tylko
        # ustawialne typy - shop pomijamy, idzie za zdjeciem #1)
        icons_b64 = {}
        for item in preview["items"]:
            if item["action"] == "delete":
                continue
            for typ in item.get("icons") or []:
                if typ in idosell_client.SETTABLE_ICON_TYPES:
                    icons_b64[typ] = item_b64[item["index"]]

        final_count = len(images_b64)
        leftover = [i["id"] for i in current
                    if i["slot"] is None or i["slot"] > final_count]
        ido_audit("execute_attempt", product_id, {
            "put_count": final_count, "uploaded": uploaded,
            "current_count": len(current), "delete_after": leftover,
            "icons": sorted(icons_b64), "apply_macro": preview["apply_macro"],
        })

        # 4-7) PUT slotow + kasowanie + ikony + WERYFIKACJA, z PONOWIENIEM gdy
        # galeria wyszla niekompletna. IdoSell czasem CICHO gubi slot (PUT bez
        # faultu, ale GET pokazuje brak) - krytyczne przy nocnym batchu, gdzie
        # nikt nie patrzy. Do 3 prob: PUT calej galerii -> weryfikacja GET-em.
        verify = None
        attempt = 0
        for attempt in range(3):
            idosell_client.put_product_images(product_id, images_b64)
            idosell_client.delete_product_images(product_id, leftover)
            if icons_b64:
                idosell_client.set_product_icons(product_id, icons_b64)
            verify = ido_verify_gallery(product_id, final_count,
                                        expected_icons=sorted(icons_b64))
            if verify.get("ok"):
                break
            if attempt < 2:
                ido_audit("execute_retry", product_id,
                          {"attempt": attempt + 1, "verify": verify})
                time.sleep(2)
        verify["attempts"] = attempt + 1
    except idosell_client.IdoSellError as e:
        ido_audit("execute_error", product_id, {"error": str(e)})
        raise IdoExecError(str(e), 502)

    plan["backup_images"] = backup
    plan["backup_icons"] = backup_icons
    plan["executed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    plan["verified"] = verify
    save_ido_plan(product_id, plan)
    lowres_sent = _log_lowres_sent(product_id, preview, plan["executed_at"])
    ido_audit("execute_plan", product_id, {
        "uploaded": uploaded,
        "kept": sum(1 for i in preview["items"] if i["action"] == "keep"),
        "deleted": len(leftover),
        "icons": sorted(icons_b64),
        "final_count": final_count,
        "backup_count": len(backup),
        "verified": verify,
        "lowres_sent": lowres_sent,
    })
    return {"ok": True, "uploaded": uploaded,
            "final_count": final_count, "verify": verify,
            "icons_set": sorted(icons_b64), "archive": archive,
            "lowres_sent": lowres_sent,
            "executed_at": plan["executed_at"]}


@app.post("/api/idosell/products/<int:product_id>/execute")
def idosell_product_execute(product_id):
    """Pojedynczy zapis planu - wymaga jawnego confirm z UI."""
    body = request.get_json(silent=True) or {}
    if body.get("confirm") is not True:
        return jsonify({"error": "Brak potwierdzenia"}), 400
    try:
        return jsonify(_ido_execute_one(product_id))
    except IdoExecError as e:
        return jsonify({"error": str(e)}), e.status


@app.post("/api/idosell/bulk-execute")
def idosell_bulk_execute():
    """Wsadowy zapis planow wielu produktow - jedno potwierdzenie obejmuje
    cala liste, ale KAZDY produkt przechodzi pelny wzorzec bezpieczenstwa
    (archiwum, backup, PUT, weryfikacja GET, audyt). Wynik per produkt."""
    body = request.get_json(silent=True) or {}
    if body.get("confirm") is not True:
        return jsonify({"error": "Brak potwierdzenia"}), 400
    try:
        ids = [int(x) for x in (body.get("product_ids") or [])]
    except (TypeError, ValueError):
        return jsonify({"error": "Nieprawidlowa lista produktow"}), 400
    if not ids:
        return jsonify({"error": "Pusta lista produktow"}), 400
    results = []
    for pid in ids:
        try:
            r = _ido_execute_one(pid)
            v = r.get("verify") or {}
            results.append({"id": pid, "ok": True, "verified": v.get("ok"),
                            "final_count": r.get("final_count")})
        except IdoExecError as e:
            results.append({"id": pid, "ok": False, "error": str(e)})
        except Exception as e:  # noqa: BLE001 - jeden produkt nie wywala calosci
            results.append({"id": pid, "ok": False, "error": str(e)})
    ok = sum(1 for r in results if r["ok"])
    return jsonify({"results": results, "ok": ok, "failed": len(results) - ok})


# ---------------- most Allegro (idosell -> bg-remover, kontrakt v0.2) ----------
# idosell NIE dotyka Allegro - tylko wola bg-removera (allegro_bridge.py). Push
# proponowany PO udanej wysylce na IdoSell (auto-prompt we froncie, decyzja usera).
def _ido_final_image_paths(product_id: int) -> list:
    """ABSOLUTNE sciezki finalnej galerii wyslanej na sklep, w kolejnosci planu
    (process -> done/, keep -> backup oryginalu). To samo zrodlo co _ido_execute_one,
    wiec galeria 1:1 ze sklepem. Wymaga wykonanego planu (keep bierze z backupu)."""
    preview = build_ido_plan_preview(product_id)        # ValueError -> brak planu
    plan = load_ido_plan(product_id) or {}
    backup = {b["id"]: b for b in (plan.get("backup_images") or [])}
    paths = []
    for item in preview["items"]:
        if item["action"] == "delete":
            continue
        if item["action"] == "process":
            p = (DONE_DIR / item["local"]).resolve()
        else:  # keep - plik z backupu oryginalow (jak w _ido_execute_one)
            entry = backup.get(item.get("image_id"))
            if not entry:
                continue
            p = (ORIGINALS_DIR / entry["file"]).resolve()
        if p.exists():
            paths.append(str(p))
    return paths


# ---- async wysylka (IdoSell -> Allegro) w tle: kolejka + magazyn wynikow.
# ADDITIVE: synchroniczne /execute, /bulk-execute, /bridge/execute zostaja jako
# rdzen/bezpiecznik. Worker per produkt robi IdoSell (pelny _ido_execute_one),
# a po sukcesie push na Allegro - oba poza krytyczna sciezka usera. ----
def _now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def persist_sends():
    with send_lock:
        snap = list(send_records.values())
    try:
        SENDS_STATE_FILE.write_text(json.dumps(snap, ensure_ascii=False),
                                    encoding="utf-8")
    except OSError:
        pass


def restore_sends():
    if not SENDS_STATE_FILE.exists():
        return
    try:
        snap = json.loads(SENDS_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    for rec in snap:
        pid = str(rec.get("product_id") or "")
        if not pid:
            continue
        # migracja: stare bledy "Brak ofert" (sprzed fix-a klasyfikacji) -> skip
        al = rec.get("allegro") or {}
        if al.get("status") == "error" and "Brak ofert" in str(al.get("error") or ""):
            rec["allegro"] = {"status": "skip", "reason": "brak ofert na Allegro"}
        if rec.get("state") in ("queued", "running"):  # przerwane restartem -> wznow
            rec["state"] = "queued"
            send_records[pid] = rec
            send_queue.put(pid)
        else:
            send_records[pid] = rec


def _send_set(pid, **kw):
    with send_lock:
        rec = send_records.get(pid) or {"product_id": pid}
        rec.update(kw); rec["ts"] = _now_str()
        send_records[pid] = rec
    persist_sends()


def enqueue_send(product_id, mode="full") -> bool:
    """Dokolejkowuje produkt do wysylki w tle. mode 'full' = IdoSell + Allegro;
    mode 'allegro' = tylko Allegro (pomija push IdoSell - dla osobnego modala)."""
    pid = str(product_id)
    with send_lock:
        rec = send_records.get(pid) or {"product_id": pid}
        rec.update({"state": "queued", "reviewed": False, "ts": _now_str(), "mode": mode,
                    "ido": {"status": "pending"}, "allegro": {"status": "pending"}})
        send_records[pid] = rec
    persist_sends()
    send_queue.put(pid)
    return True


def send_worker():
    """Per produkt: IdoSell (pelny wzorzec bezpieczenstwa) -> po sukcesie Allegro.
    Throttling na poziomie produktow; bg robi backoff 429 per wariant."""
    while True:
        pid = send_queue.get()
        if pid not in send_records:
            continue
        _send_set(pid, state="running")
        mode = send_records[pid].get("mode", "full")
        # --- etap 1: IdoSell (pomijany w trybie 'allegro') ---
        if mode == "allegro":
            with send_lock:
                send_records[pid]["ido"] = {"status": "skip", "reason": "tylko Allegro"}
            ido_ok = True
            persist_sends()
        else:
            with send_lock:
                send_records[pid]["ido"] = {"status": "sending"}
            persist_sends()
            ido_ok = False
            try:
                r = _ido_execute_one(int(pid))
                v = r.get("verify") or {}
                with send_lock:
                    send_records[pid]["ido"] = {
                        "status": "ok" if v.get("ok") else "warn",
                        "verify": v, "final_count": r.get("final_count")}
                ido_ok = True
            except Exception as e:   # IdoExecError i inne - jeden produkt nie wywala workera
                with send_lock:
                    send_records[pid]["ido"] = {"status": "error", "error": str(e)}
                ido_audit("send_ido_error", int(pid) if pid.isdigit() else 0, {"error": str(e)})
            persist_sends()
        # --- etap 2: Allegro (tylko gdy IdoSell przeszedl i most aktywny) ---
        if not ido_ok:
            with send_lock:
                send_records[pid]["allegro"] = {"status": "skip", "reason": "IdoSell nieudany"}
            _send_set(pid, state="done")
            continue
        if allegro_bridge.load_config() is None:
            with send_lock:
                send_records[pid]["allegro"] = {"status": "skip", "reason": "most nieaktywny"}
            _send_set(pid, state="done")
            time.sleep(SEND_THROTTLE_S)
            continue
        with send_lock:
            send_records[pid]["allegro"] = {"status": "sending"}
        persist_sends()
        try:
            paths = _ido_final_image_paths(int(pid))
            if not paths:
                raise RuntimeError("Brak finalnej galerii")
            res = allegro_bridge.execute(str(pid), paths)
            offers = res.get("results") or []
            ok_c = sum(1 for o in offers if o.get("ok"))
            failed = len(offers) - ok_c
            if not offers:
                status = "skip"      # produkt nie ma ofert na Allegro - to NIE blad
            else:
                status = "ok" if failed == 0 else "partial" if ok_c else "error"
            al_rec = {"status": status, "offers": offers, "ok_count": ok_c,
                      "failed_count": failed, "model": res.get("model")}
            if status == "skip":
                al_rec["reason"] = "brak ofert na Allegro"
            with send_lock:
                send_records[pid]["allegro"] = al_rec
            ido_audit("send_allegro", int(pid), {"status": status, "ok": ok_c, "failed": failed})
        except Exception as e:   # noqa: BLE001
            msg = str(e)
            if "Brak ofert" in msg:   # produkt nie ma ofert na Allegro - to NIE blad, tylko skip
                with send_lock:
                    send_records[pid]["allegro"] = {"status": "skip", "reason": "brak ofert na Allegro"}
            else:
                with send_lock:
                    send_records[pid]["allegro"] = {"status": "error", "error": msg}
                ido_audit("send_allegro_error", int(pid) if pid.isdigit() else 0, {"error": msg})
        _send_set(pid, state="done")
        time.sleep(SEND_THROTTLE_S)


restore_sends()
threading.Thread(target=send_worker, daemon=True).start()


def _send_problem(rec) -> bool:
    if rec.get("reviewed"):
        return False
    return (rec.get("ido", {}).get("status") == "error"
            or rec.get("allegro", {}).get("status") in ("error", "partial"))


@app.get("/api/sends")
def sends_list():
    """Zakladka 'Do sprawdzenia': pozycje + statystyki + sent_ids (do 'ukryj wyslane').
    Sort: problemy nieprzejrzane na wierzchu, w grupie najnowsze pierwsze."""
    with send_lock:
        items = list(send_records.values())

    def prio(r):
        if _send_problem(r):
            return 0
        if r.get("state") == "running":
            return 1
        if r.get("state") == "queued":
            return 2
        return 3
    items.sort(key=lambda r: r.get("ts") or "", reverse=True)
    items.sort(key=prio)
    stats = {
        "queued": sum(1 for r in items if r.get("state") == "queued"),
        "running": sum(1 for r in items if r.get("state") == "running"),
        "done": sum(1 for r in items if r.get("state") == "done"),
        "problems": sum(1 for r in items if _send_problem(r)),
    }
    return jsonify({"items": items, "stats": stats,
                    "sent_ids": [r.get("product_id") for r in items]})


@app.post("/api/sends/enqueue")
def sends_enqueue():
    body = request.get_json(silent=True) or {}
    pids = [str(p).strip() for p in (body.get("product_ids") or []) if str(p).strip()]
    if not pids:
        return jsonify({"error": "Brak product_ids"}), 400
    mode = body.get("mode") if body.get("mode") in ("full", "allegro") else "full"
    n = sum(1 for pid in pids if enqueue_send(pid, mode))
    return jsonify({"ok": True, "queued": n})


@app.post("/api/sends/<int:product_id>/retry")
def sends_retry(product_id):
    enqueue_send(product_id)
    return jsonify({"ok": True})


@app.post("/api/sends/<int:product_id>/dismiss")
def sends_dismiss(product_id):
    with send_lock:
        rec = send_records.get(str(product_id))
        if rec:
            rec["reviewed"] = True
    persist_sends()
    return jsonify({"ok": True})


@app.get("/api/allegro/bridge/status")
def allegro_bridge_status():
    """Czy most do bg-removera jest skonfigurowany (allegro_bridge_config.json)."""
    return jsonify({"configured": allegro_bridge.load_config() is not None})


@app.post("/api/allegro/bridge/preview")
def allegro_bridge_preview():
    """A1 (read-only): z product_ids ustal kody SERWEROWO (idosell_client), przekaz
    do bg-removera, zwroc on_allegro (z dolaczonym product_id) + skip + no_code.
    Kod rozwiazujemy z product_id, zeby nie zalezec od pamieci przegladarki."""
    body = request.get_json(silent=True) or {}
    pids = [str(p).strip() for p in (body.get("product_ids") or []) if str(p).strip()]
    if not pids:
        return jsonify({"error": "Brak product_ids"}), 400
    # bg matchuje po 'code' = IdoSell productId (= baza Allegro external.id;
    # potwierdzone empirycznie 7206/7207). productDisplayedCode to INNY identyfikator
    # (np. 7206 -> "01394-70109") i bg go NIE rozpoznaje - dlatego slemy productId.
    try:
        r = allegro_bridge.preview(pids)
    except allegro_bridge.BridgeError as e:
        return jsonify({"error": str(e)}), 502
    for o in (r.get("on_allegro") or []):
        o["product_id"] = o.get("code")   # code == productId
    return jsonify(r)


@app.post("/api/allegro/bridge/execute")
def allegro_bridge_execute():
    """A2 (zapis, wymaga confirm): zbuduj image_paths finalnej galerii produktu
    i przekaz bg-removerowi (po code). Push na WSZYSTKIE warianty (all_variants)."""
    body = request.get_json(silent=True) or {}
    if body.get("confirm") is not True:
        return jsonify({"error": "Brak potwierdzenia"}), 400
    try:
        product_id = int(body.get("product_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Brak/zly product_id"}), 400
    # bg matchuje po productId (= baza Allegro external.id), nie po productDisplayedCode
    code = str(product_id)
    try:
        paths = _ido_final_image_paths(product_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    if not paths:
        return jsonify({"error": "Brak finalnej galerii - czy produkt wyslany na IdoSell?"}), 409
    try:
        res = allegro_bridge.execute(code, paths)
    except allegro_bridge.BridgeError as e:
        ido_audit("allegro_push_error", product_id, {"code": code, "error": str(e)})
        return jsonify({"error": str(e)}), 502
    ido_audit("allegro_push", product_id, {
        "code": code, "images": len(paths),
        "results": (res or {}).get("results")})
    return jsonify(res)


@app.post("/api/allegro/bridge/execute-batch")
def allegro_bridge_execute_batch():
    """Masowy push na Allegro: z product_ids buduje items=[{code, image_paths}]
    (kod=productId, galeria z planu) i woła bg execute-batch. Dokleja product_id
    do wynikow. Powtorka = wyslij ponownie tylko status error/partial."""
    body = request.get_json(silent=True) or {}
    if body.get("confirm") is not True:
        return jsonify({"error": "Brak potwierdzenia"}), 400
    pids = [str(p).strip() for p in (body.get("product_ids") or []) if str(p).strip()]
    if not pids:
        return jsonify({"error": "Brak product_ids"}), 400
    items = []
    for pid in pids:
        try:
            paths = _ido_final_image_paths(int(pid))
        except (ValueError, TypeError):
            paths = []
        if paths:
            items.append({"code": pid, "image_paths": paths})
    if not items:
        return jsonify({"error": "Brak finalnych galerii - czy produkty wyslane na IdoSell?"}), 409
    try:
        res = allegro_bridge.execute_batch(items)
    except allegro_bridge.BridgeError as e:
        return jsonify({"error": str(e)}), 502
    for r in (res.get("results") or []):
        r["product_id"] = r.get("code")
    ido_audit("allegro_push_batch", 0,
              {"summary": res.get("summary"), "count": len(items)})
    return jsonify(res)


@app.post("/api/allegro/bridge/rollback")
def allegro_bridge_rollback():
    """Cofnij CALY push modelu na Allegro (wszystkie warianty) - przez bg."""
    body = request.get_json(silent=True) or {}
    if body.get("confirm") is not True:
        return jsonify({"error": "Brak potwierdzenia"}), 400
    try:
        product_id = int(body.get("product_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Brak/zly product_id"}), 400
    try:
        res = allegro_bridge.rollback(str(product_id))
    except allegro_bridge.BridgeError as e:
        ido_audit("allegro_rollback_error", product_id, {"error": str(e)})
        return jsonify({"error": str(e)}), 502
    ido_audit("allegro_rollback", product_id,
              {"ok": res.get("ok"), "failed": res.get("failed")})
    return jsonify(res)


def _ido_rollback_one(product_id: int) -> dict:
    """Rdzen przywrocenia galerii z fizycznego backupu na dysku dla jednego
    produktu (zdjecia + ikony, weryfikacja GET-em). Zwraca dict wyniku albo
    rzuca IdoExecError(msg, status). Wspolny dla rollback i bulk-rollback."""
    try:
        plan = load_ido_plan(product_id)
    except ValueError as e:
        raise IdoExecError(str(e), 404)
    backup = plan.get("backup_images")
    if not backup:
        raise IdoExecError("Brak backupu - plan nie byl wykonany", 409)
    try:
        images_b64 = []
        for entry in backup:
            path = ORIGINALS_DIR / entry["file"]
            if not path.exists():
                raise IdoExecError(f"Brak pliku backupu {entry['file']}", 409)
            images_b64.append(base64.b64encode(path.read_bytes()).decode())

        # ikony: byly -> przywroc z backupu; nie bylo -> skasuj nasza
        # (tylko ustawialne typy; shop zostawiamy IdoSellowi)
        icons_restore = {}
        icons_to_delete = []
        for entry in plan.get("backup_icons") or []:
            if entry["type"] not in idosell_client.SETTABLE_ICON_TYPES:
                continue
            if entry["existed"] and entry.get("file"):
                path = ORIGINALS_DIR / entry["file"]
                if not path.exists():
                    raise IdoExecError(
                        f"Brak pliku backupu ikony {entry['file']}", 409)
                icons_restore[entry["type"]] = \
                    base64.b64encode(path.read_bytes()).decode()
            elif not entry["existed"]:
                icons_to_delete.append(entry["type"])

        restored = len(images_b64)
        ido_audit("rollback_attempt", product_id, {
            "restore_count": restored,
            "icons_restore": sorted(icons_restore),
            "icons_delete": icons_to_delete,
        })
        idosell_client.put_product_images(product_id, images_b64)
        current = idosell_client.get_product_images(product_id)
        leftover = [i["id"] for i in current
                    if i["slot"] is None or i["slot"] > restored]
        idosell_client.delete_product_images(product_id, leftover)
        if icons_restore:
            idosell_client.set_product_icons(product_id, icons_restore)
        if icons_to_delete:
            state = idosell_client.get_product_icons(product_id)
            for typ in icons_to_delete:
                if state[typ]["exists"]:
                    idosell_client.delete_product_icon(product_id, typ)
        verify = ido_verify_gallery(product_id, restored,
                                    expected_icons=sorted(icons_restore))
    except idosell_client.IdoSellError as e:
        ido_audit("rollback_error", product_id, {"error": str(e)})
        raise IdoExecError(str(e), 502)
    plan["rolled_back_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    plan["verified"] = verify
    save_ido_plan(product_id, plan)
    ido_audit("rollback", product_id,
              {"restored_count": restored, "verified": verify})
    return {"ok": True, "restored": restored, "verify": verify}


@app.post("/api/idosell/products/<int:product_id>/rollback")
def idosell_product_rollback(product_id):
    """Przywraca galerie z fizycznego backupu na dysku - wymaga confirm."""
    body = request.get_json(silent=True) or {}
    if body.get("confirm") is not True:
        return jsonify({"error": "Brak potwierdzenia"}), 400
    try:
        return jsonify(_ido_rollback_one(product_id))
    except IdoExecError as e:
        return jsonify({"error": str(e)}), e.status


@app.post("/api/idosell/bulk-rollback")
def idosell_bulk_rollback():
    """Wsadowe cofniecie zapisu wielu produktow - jedno potwierdzenie obejmuje
    cala liste, ale KAZDY produkt cofa sie pelnym wzorcem (backup z dysku,
    PUT, weryfikacja GET, audyt). Wynik per produkt - bezpiecznik przy
    masowej wysylce, gdy trzeba szybko wrocic do stanu sprzed."""
    body = request.get_json(silent=True) or {}
    if body.get("confirm") is not True:
        return jsonify({"error": "Brak potwierdzenia"}), 400
    try:
        ids = [int(x) for x in (body.get("product_ids") or [])]
    except (TypeError, ValueError):
        return jsonify({"error": "Nieprawidlowa lista produktow"}), 400
    if not ids:
        return jsonify({"error": "Pusta lista produktow"}), 400
    results = []
    for pid in ids:
        try:
            r = _ido_rollback_one(pid)
            v = r.get("verify") or {}
            results.append({"id": pid, "ok": True, "verified": v.get("ok"),
                            "restored": r.get("restored")})
        except IdoExecError as e:
            results.append({"id": pid, "ok": False, "error": str(e)})
        except Exception as e:  # noqa: BLE001 - jeden produkt nie wywala calosci
            results.append({"id": pid, "ok": False, "error": str(e)})
    ok = sum(1 for r in results if r["ok"])
    return jsonify({"results": results, "ok": ok, "failed": len(results) - ok})


class QuietPollingFilter(logging.Filter):
    """Wycisza w konsoli lokalne odpytki UI (statusy co 1.5s) -
    zostaja wpisy istotne: Allegro, logowania, bledy."""
    NOISY = ("/api/jobs", "/api/allegro/scan/status",
             "/api/idosell/scan/status", "/done/", "/originals/")

    def filter(self, record):
        msg = record.getMessage()
        return not (" 200 " in msg or " 304 " in msg) or \
            not any(p in msg for p in self.NOISY)


if __name__ == "__main__":
    logging.getLogger("werkzeug").addFilter(QuietPollingFilter())
    print("idosell-remover: http://127.0.0.1:5001")
    print(f"PIN dostepu: {APP_CFG['pin']}")
    # threaded=True: dlugie zadania (np. masowa obrobka) nie blokuja UI/pollingu
    # - dostep do wspoldzielonego stanu i tak chroniony przez 'lock'
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)
