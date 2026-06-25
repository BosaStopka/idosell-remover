"""Watchdog serwera idosell (port 5001) - NIEZALEZNY od sesji Claude.

Dlaczego: nocny "nadzor" przez Claude (ScheduleWakeup) jest zawodny - zalezy
od aktywnej sesji. Ten proces uruchamiany przez Start-Process zyje po
zamknieciu terminala/sesji (tak jak app.py przezyl cala noc) i sam pilnuje.

Co robi co INTERVAL s:
  - pinguje serwer (GET /api/queue). HTTP 401/200 = serwer ZYJE (odpowiada).
    Brak odpowiedzi (connection refused / timeout) FAIL_LIMIT razy z rzedu
    -> RESTART app.py (ubija TYLKO wlasciciela portu 5001, NIE bg-remover 5000).
  - wykrywa zawieszenie: ten sam 'current' i queued nie spada przez STALL_LIMIT
    cykli przy processing=1 -> restart (zaciety job / serwer nie odpowiada na
    zadania mimo ze port zyje).
  - loguje kazdy cykl do watchdog.log.

Start (detached, przezyje sesje):
  Start-Process python -ArgumentList watchdog.py -WindowStyle Hidden
Stop: zabij proces python z watchdog.py.
"""
import hashlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
PORT = 5001
INTERVAL = 30          # s miedzy checkami
FAIL_LIMIT = 3         # tyle nieudanych z rzedu -> restart
STALL_LIMIT = 20       # tyle cykli (~10 min) bez postepu przy processing -> restart
LOG = BASE / "watchdog.log"
PY = sys.executable
DETACHED = 0x00000008  # DETACHED_PROCESS (Windows)


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def cookie():
    c = json.loads((BASE / "app_config.json").read_text(encoding="utf-8"))
    return hashlib.sha256((c["pin"] + c["cookie_secret"]).encode()).hexdigest()


def check():
    """dict z /api/queue jesli OK; {'up': True} gdy serwer odpowiada ale np.
    401/inny HTTP (tez znaczy ZYJE); None gdy nie odpowiada (down)."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/api/queue",
            headers={"Cookie": f"bs_auth_ido={cookie()}"})
        r = urllib.request.urlopen(req, timeout=10)
        return json.loads(r.read())
    except urllib.error.HTTPError:
        return {"up": True}          # odpowiedzial (np. 401) = serwer zyje
    except Exception:
        return None                  # connection refused / timeout = down


def port_pid():
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return None
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) >= 5 and parts[-1].isdigit() and "LISTENING" in ln \
                and parts[1].endswith(f":{PORT}"):
            return parts[-1]
    return None


def restart(reason):
    # ANTY-WYSCIG: jesli serwer jednak odpowiada (np. wlasnie reczny restart),
    # NIE startuj drugiej instancji (footgun: 2x app.py na 5001).
    if check() is not None:
        log(f"restart pominiety ({reason}) - serwer odpowiada")
        return
    pid = port_pid()
    if pid:
        try:
            subprocess.run(["taskkill", "/F", "/PID", pid],
                           capture_output=True, timeout=15)
        except Exception:
            pass
        time.sleep(2)
    try:
        out = open(BASE / "_server_restart.log", "a", encoding="utf-8")
        err = open(BASE / "_server_restart.log.err", "a", encoding="utf-8")
        subprocess.Popen([PY, "app.py"], cwd=str(BASE),
                         stdout=out, stderr=err, creationflags=DETACHED)
        log(f"RESTART app.py ({reason}; ubity PID {pid}) - czekam 7s")
        time.sleep(7)
    except Exception as e:
        log(f"RESTART NIEUDANY: {e}")


def main():
    log("=== watchdog START (nadzor serwera 5001) ===")
    fails = 0
    last_cur, stall = None, 0
    while True:
        d = check()
        if d is None:
            fails += 1
            log(f"BRAK ODPOWIEDZI ({fails}/{FAIL_LIMIT})")
            if fails >= FAIL_LIMIT:
                restart("serwer nie odpowiada")
                fails, last_cur, stall = 0, None, 0
        else:
            fails = 0
            if "queued" in d:
                cur = d.get("current")
                proc = d.get("processing")
                q = d.get("queued")
                if proc and cur == last_cur and cur is not None:
                    stall += 1
                else:
                    stall = 0
                last_cur = cur
                log(f"OK queued={q} proc={proc} cur={cur}"
                    + (f" STALL {stall}/{STALL_LIMIT}" if stall else ""))
                if stall >= STALL_LIMIT:
                    restart(f"zacieta obrobka na {cur}")
                    last_cur, stall = None, 0
                elif q == 0 and not proc:
                    log("kolejka PUSTA - obrobka skonczona (czuwam dalej nad serwerem)")
            else:
                log("OK serwer odpowiada (bez danych kolejki)")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
