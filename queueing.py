# -*- coding: utf-8 -*-
"""Czysta logika wyboru zadania dla workera - wydzielona z app.py, zeby byla
testowalna bez importu app.py (app.py przy imporcie startuje watki/realne
wysylki). Dziala na dowolnych obiektach Queue (queue.Queue)."""
import json
from pathlib import Path
from queue import Empty


def next_job(priority_q, bulk_q, paused, timeout=1):
    """Wybor nastepnego zadania:
      - PRIORYTET (interaktywne: upload/Ponow/Obrob/dodaj) ZAWSZE pierwszy,
        tez przy pauzie,
      - BULK (masowka/feeder) tylko gdy NIE pauza.
    Zwraca job_id albo None (None => worker ma odczekac i sprobowac ponownie).
    Bez busy-spinu: gdy nie-pauza i brak priorytetu, czeka do `timeout` s na bulk.
    """
    try:
        return priority_q.get_nowait()
    except Empty:
        pass
    if paused:
        return None
    try:
        return bulk_q.get(timeout=timeout)
    except Empty:
        return None


def write_pause(path, paused):
    """Utrwal stan recznej pauzy na dysk. Bez wyjatkow w gore (best-effort)."""
    try:
        Path(path).write_text(json.dumps({"paused": bool(paused)}),
                              encoding="utf-8")
    except OSError:
        pass


def read_pause(path) -> bool:
    """Odczytaj utrwalony stan pauzy (domyslnie False: brak pliku/uszkodzony =
    nie-pauza). Pozwala przezyc restart bez wznawiania bulku wbrew userowi."""
    try:
        return bool(json.loads(Path(path).read_text(encoding="utf-8")).get("paused"))
    except (OSError, json.JSONDecodeError, ValueError):
        return False
