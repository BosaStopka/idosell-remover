"""Odbudowa jobs_state.json z wynikow na dysku (done/).

Jednorazowe narzedzie ratunkowe: stan widoku obrobki (Studio) zostal
wyczyszczony do []. Pliki wynikowe w done/{pid}/{pid}_N.jpg sa nienaruszone.
Ten skrypt odtwarza liste zadan 'done' tak, by serwer po restarcie wczytal
je i Studio sie zapelnilo - BEZ ponownej obrobki.

Pola grupujace (pid, photo_index, series_id, ikony, pozycja w galerii) NIE
sa trzymane w stanie - serwer dolicza je na biezaco w /api/jobs z nazwy
pliku + plan.json. Wiec wystarczy poprawny name/result/source/fashion.

Uruchom przy ZATRZYMANYM serwerze idosell (inaczej nadpisze stan persistem).
"""
import json
import re
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parent
DONE_DIR = BASE / "done"
ORIGINALS_DIR = BASE / "originals"
STATE_FILE = BASE / "jobs_state.json"

# kolejnosc kluczy zgodna z PERSIST_KEYS w app.py (restore_jobs czyta to samo)
RESULT_RE = re.compile(r"^(?P<pid>.+?)_(?P<idx>\d+)\.jpg$", re.IGNORECASE)


def load_plan(pid_dir: Path):
    pf = pid_dir / "plan.json"
    if not pf.exists():
        return None
    try:
        return json.loads(pf.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def main():
    if not DONE_DIR.exists():
        print("Brak katalogu done/ - nic do odbudowy")
        return

    # backup obecnego stanu (zwykle []), zeby bylo do czego wrocic
    if STATE_FILE.exists():
        bak = STATE_FILE.with_suffix(".json.prebuild.bak")
        bak.write_text(STATE_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Backup obecnego stanu -> {bak.name}")

    rebuilt = []
    n_dirs = n_files = n_orig = 0
    src_counts = {}

    for pid_dir in sorted(DONE_DIR.iterdir(),
                          key=lambda p: (not p.name.isdigit(), p.name)):
        if not pid_dir.is_dir():
            continue
        plan = load_plan(pid_dir)
        source = (plan or {}).get("source") or "idosell"
        # mapa index -> fashion z planu
        fashion_by_idx = {}
        for d in (plan or {}).get("decisions", []) or []:
            if d.get("index") is not None:
                fashion_by_idx[int(d["index"])] = bool(d.get("fashion"))

        # zbierz pliki wynikowe {pid}_{N}.jpg (pomin icon_*, plan.json itp.)
        files = []
        for f in pid_dir.glob("*.jpg"):
            m = RESULT_RE.match(f.name)
            if m and m.group("pid") == pid_dir.name:
                files.append((int(m.group("idx")), f))
        if not files:
            continue
        files.sort(key=lambda t: t[0])
        n_dirs += 1

        for idx, f in files:
            stem = f.stem                       # np. "7202_8"
            name = f.name                       # np. "7202_8.jpg"
            result = f"{pid_dir.name}/{name}"   # zgodne z app.py: {pid}/{stem}.jpg

            # orig: backup oryginalu IdoSell, jesli istnieje (podglad przed/po)
            orig = None
            cand = ORIGINALS_DIR / "idosell" / pid_dir.name / name
            if cand.exists():
                orig = f"idosell/{pid_dir.name}/{name}"
                n_orig += 1

            fashion = fashion_by_idx.get(idx, False)
            rebuilt.append({
                "id": uuid.uuid4().hex[:12],
                "name": name,
                "status": "done",
                "result": result,
                "error": None,
                "source": source,
                "orig": orig,
                "options": {},
                "seconds": None,
                # maski sa kluczowane starym job_id (utracone) -> wylacz edytor,
                # zeby nie otwierac pustej maski. Wyniki i wysylka dzialaja.
                "editable": False,
                "qa": None,
                "kept": fashion,      # fashion zwykle zostawiane 1:1
                "fashion": fashion,
                "rev": 0,
                "dup": None,
                "dup_keep": None,
            })
            n_files += 1
            src_counts[source] = src_counts.get(source, 0) + 1

    STATE_FILE.write_text(json.dumps(rebuilt, ensure_ascii=False),
                          encoding="utf-8")
    print(f"Odbudowano stan: {n_files} zadan z {n_dirs} produktow")
    print(f"  z czego z mapowanym oryginalem (przed/po): {n_orig}")
    print(f"  zrodla: {src_counts}")
    print(f"  zapis -> {STATE_FILE.name}")


if __name__ == "__main__":
    main()
