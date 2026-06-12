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
from queue import Queue

from flask import (Flask, jsonify, make_response, request, send_file,
                   send_from_directory)

import allegro_client
import idosell_client
import pipeline

BASE = Path(__file__).parent
DONE_DIR = BASE / "done"
DONE_DIR.mkdir(exist_ok=True)
ORIGINALS_DIR = BASE / "originals"
ORIGINALS_DIR.mkdir(exist_ok=True)
APP_CONFIG_FILE = BASE / "app_config.json"
JOBS_STATE_FILE = BASE / "jobs_state.json"

app = Flask(__name__, static_folder="static", static_url_path="/static")

jobs = {}          # job_id -> dict(status, name, result, error, ...)
job_order = []     # kolejnosc dodania
queue = Queue()
lock = threading.Lock()
inference_busy = threading.Event()  # gdy ustawione, skan tla pauzuje

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
    return request.cookies.get("bs_auth") == _auth_cookie_value()


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
    resp.set_cookie("bs_auth", _auth_cookie_value(), httponly=True,
                    samesite="Strict", max_age=30 * 24 * 3600)
    return resp


# ---------------- obrobka ----------------

def extract_product_id(stem: str) -> str:
    match = re.match(r"^(.+?)_\d+$", stem)
    return match.group(1) if match else stem


PERSIST_KEYS = ("id", "name", "status", "result", "error", "source",
                "orig", "options", "seconds")


def persist_jobs():
    with lock:
        snapshot = [{k: jobs[jid].get(k) for k in PERSIST_KEYS}
                    for jid in job_order]
    try:
        JOBS_STATE_FILE.write_text(
            json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def submit_job(name: str, data: bytes, options: dict, source: str = "upload") -> str:
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
            "data": data, "options": options, "result": None,
            "error": None, "source": source, "orig": orig_file,
        }
        job_order.append(job_id)
    queue.put(job_id)
    persist_jobs()
    return job_id


def worker():
    while True:
        job_id = queue.get()
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
            img = pipeline.process_bytes(data, job["options"])
            stem = Path(job["name"]).stem
            pid = extract_product_id(stem)
            out_dir = DONE_DIR / pid
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / (stem + ".jpg")
            img.save(out_path, "JPEG", quality=95)
            with lock:
                job["status"] = "done"
                job["result"] = f"{pid}/{stem}.jpg"
                job["seconds"] = round(time.time() - t0, 1)
                job["data"] = None
        except Exception as e:
            with lock:
                job["status"] = "error"
                job["error"] = str(e)
                job["data"] = None
        finally:
            if queue.empty():
                inference_busy.clear()  # kolejka pusta - skan moze wrocic
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


@app.get("/api/jobs")
def api_jobs():
    with lock:
        out = []
        for jid in job_order:
            j = jobs[jid]
            out.append({k: j.get(k) for k in
                        ("id", "name", "status", "result", "error",
                         "source", "orig", "seconds")})
    return jsonify(out)


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
        job.update(status="queued", error=None, result=None,
                   seconds=None, options=options, data=None)
    queue.put(job_id)
    persist_jobs()
    return jsonify({"job": job_id})


@app.get("/done/<path:relpath>")
def serve_done(relpath):
    return send_from_directory(DONE_DIR, relpath)


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
    arr = np.asarray(Image.open(BytesIO(data)).convert("RGB"))
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
    """Plakietki: czy produkt ma juz wyniki/plan lokalnie + wynik skanu."""
    out_dir = DONE_DIR / str(product_id)
    has_results = out_dir.exists() and any(out_dir.glob("*.jpg"))
    has_plan = (out_dir / "plan.json").exists()
    with ido_scan_lock:
        scan = ido_scan_state["results"].get(str(product_id), {})
    return {"processed": has_results, "plan": has_plan,
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
    }


@app.get("/api/idosell/products")
def idosell_products():
    """Listing: bez query - kolejne strony skanu (active bez tagu Archiwum);
    z query - wyszukiwarka ID/kod (takze otagowane i usuniete)."""
    query = request.args.get("query", "").strip()
    offset = int(request.args.get("offset", 0))
    limit = int(request.args.get("limit", 20))
    try:
        if query:
            data = idosell_client.find_products(query, limit=50)
            rows = [_ido_row_ui(p) for p in data["products"]][offset:offset + limit]
            total = data["total"]
        else:
            page = idosell_client.scan_page(offset // limit, limit)
            rows = [_ido_row_ui(p) for p in page["products"]]
            total = page["total"]
    except idosell_client.IdoSellError as e:
        return jsonify({"error": str(e)}), 400
    for p in rows:
        p.update(ido_local_flags(p["id"]))
    return jsonify({"products": rows, "total": total})


@app.get("/api/idosell/products/<int:product_id>/images")
def idosell_product_images(product_id):
    """Zdjecia produktu + sugestia akcji per zdjecie (jak w Allegro):
    biale tlo -> obrob, kolorowe -> prawdopodobnie stylizacja -> zostaw."""
    try:
        images = idosell_client.get_product_images(product_id)
    except idosell_client.IdoSellError as e:
        return jsonify({"error": str(e)}), 400
    suggestions = []
    for img in images:
        suggest = "process"
        try:
            thumb = idosell_client.download_image(img["thumb"])
            if analyze_thumb(thumb)["white"] < 0.40:
                suggest = "keep"
        except Exception:
            pass
        suggestions.append(suggest)
    return jsonify({"images": images, "suggestions": suggestions})


@app.post("/api/idosell/products/<int:product_id>/process")
def idosell_product_process(product_id):
    """Wykonuje decyzje per zdjecie: 'process' -> kolejka obrobki,
    'keep'/'delete' -> tylko zapis w planie (done/{productId}/plan.json).
    NIC nie jest wysylane do IdoSell - zapis wykona faza 2 wg planu."""
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
        # pozycja (nazwy plikow), image_id/slot -> potrzebne w fazie 2
        "decisions": [{"index": d.get("index", i + 1),
                       "image_id": d.get("image_id"),
                       "slot": d.get("slot"),
                       "url": d["url"],
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
            data = idosell_client.download_image(d["url"])
        except idosell_client.IdoSellError as e:
            errors.append(f"zdjecie {idx}: {e}")
            continue
        created.append(submit_job(
            f"{product_id}_{idx}.jpg", data, options, source="idosell"))
    skipped = sum(1 for d in decisions if d.get("action") == "keep")
    to_delete = sum(1 for d in decisions if d.get("action") == "delete")
    return jsonify({"jobs": created, "errors": errors,
                    "kept": skipped, "marked_delete": to_delete})


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
    app.run(host="127.0.0.1", port=5001, debug=False)
