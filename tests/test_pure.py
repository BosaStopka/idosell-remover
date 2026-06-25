# -*- coding: utf-8 -*-
"""Behawioralne testy czystej logiki wydzielonej z app.py/feeder.py
(priorytet kolejki, kolejnosc galerii, drip-feed feedera). Importuja TYLKO
czyste moduly (gallery/queueing/feeder) - NIE app.py, bo app.py przy imporcie
startuje watki workerow i restore_sends (realne wysylki). To wlasnie testy,
ktorych brakowalo - statyczna walidacja nie lapala regresji zachowania.

Uruchom: python -m pytest tests/ -q
"""
import sys
from pathlib import Path
from queue import Queue

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ---------- kolejnosc galerii (gallery.gallery_ordered) ----------
def test_gallery_shop_leads_then_one_fashion_then_rest():
    import gallery
    rows = [
        {"id": 1, "fashion": False, "icons": []},
        {"id": 2, "fashion": True, "icons": []},               # lifestyle
        {"id": 3, "fashion": False, "icons": ["shop"]},        # miniaturka -> #1
        {"id": 4, "fashion": False, "icons": []},
        {"id": 5, "fashion": True, "icons": []},               # 2. lifestyle -> koniec
    ]
    ids = [r["id"] for r in gallery.gallery_ordered(rows)]
    assert ids == [3, 2, 1, 4, 5], "shop->1, jedno fashion->2, reszta towaru, 2. fashion na koniec"


def test_gallery_no_shop_uses_first_nonfashion_as_lead():
    import gallery
    rows = [
        {"id": 1, "fashion": True, "icons": []},
        {"id": 2, "fashion": False, "icons": []},   # pierwsze nie-fashion -> lead
        {"id": 3, "fashion": False, "icons": []},
    ]
    assert [r["id"] for r in gallery.gallery_ordered(rows)] == [2, 1, 3]


def test_gallery_delete_last_and_raw_is_noop():
    import gallery
    rows = [
        {"id": 1, "fashion": False, "icons": ["shop"]},
        {"id": 2, "fashion": False, "icons": [], "action": "delete"},
        {"id": 3, "fashion": True, "icons": []},
    ]
    assert [r["id"] for r in gallery.gallery_ordered(rows)] == [1, 3, 2]
    assert gallery.gallery_ordered(rows, mode="raw") == rows   # raw = bez zmian


def test_gallery_all_fashion_is_noop():
    import gallery
    rows = [{"id": 1, "fashion": True, "icons": []},
            {"id": 2, "fashion": True, "icons": []}]
    assert [r["id"] for r in gallery.gallery_ordered(rows)] == [1, 2]


# ---------- priorytet kolejki (queueing.next_job) ----------
def test_queue_priority_first_even_when_paused():
    import queueing
    pq, bq = Queue(), Queue()
    pq.put("P1"); bq.put("B1")
    assert queueing.next_job(pq, bq, paused=True) == "P1"   # interaktywne tez przy pauzie
    # priorytet pusty + pauza -> None, bulk NIETKNIETY
    assert queueing.next_job(pq, bq, paused=True, timeout=0.05) is None
    assert bq.qsize() == 1


def test_queue_priority_before_bulk_when_running():
    import queueing
    pq, bq = Queue(), Queue()
    pq.put("P1"); bq.put("B1")
    assert queueing.next_job(pq, bq, paused=False) == "P1"
    assert bq.qsize() == 1   # bulk czeka dopoki priorytet ma cokolwiek


def test_queue_bulk_runs_only_when_not_paused():
    import queueing
    pq, bq = Queue(), Queue()
    bq.put("B1")
    assert queueing.next_job(pq, bq, paused=False, timeout=0.2) == "B1"


def test_queue_none_when_empty_not_paused():
    import queueing
    assert queueing.next_job(Queue(), Queue(), paused=False, timeout=0.05) is None


# ---------- utrwalenie pauzy miedzy restartami (#3) ----------
def test_pause_roundtrip(tmp_path):
    import queueing
    p = tmp_path / "_pause_state.json"
    assert queueing.read_pause(p) is False        # brak pliku = nie-pauza
    queueing.write_pause(p, True)
    assert queueing.read_pause(p) is True          # utrwalona pauza przezywa "restart"
    queueing.write_pause(p, False)
    assert queueing.read_pause(p) is False


def test_pause_read_corrupted_is_false(tmp_path):
    import queueing
    p = tmp_path / "_pause_state.json"
    p.write_text("{nie-json", encoding="utf-8")
    assert queueing.read_pause(p) is False          # uszkodzony plik = nie-pauza (bezpiecznie)


# ---------- drip-feed feedera (feeder.*) ----------
def test_feeder_should_feed_band():
    import feeder
    assert feeder.should_feed(None) is False                       # serwer nie odpowiada
    assert feeder.should_feed({"paused": True, "queued": 0}) is False   # pauza
    assert feeder.should_feed({"queued": 200}, low=120) is False   # >= LOW = jeszcze duzo
    assert feeder.should_feed({"queued": 50}, low=120) is True     # ponizej LOW = dosyp


def test_feeder_take_chunk_skips_active():
    import feeder
    chunk, cur = feeder.take_chunk([1, 2, 3, 4, 5], 0, act={"2", "3"}, chunk_size=2)
    assert chunk == [1, 4]   # 2,3 pominiete (active)
    assert cur == 4


def test_feeder_select_candidates_filters_and_dedupes():
    import feeder
    by_season = {
        "lato": [{"id": 1}, {"id": 2, "processed": True}, {"id": 3, "executed": True}],
        "wiosna": [{"id": 4}, {"id": 1}],   # 4 active, 1 duplikat
        "jesien": [{"id": 5}],
    }
    assert feeder.select_candidates(by_season, act={"4"}) == [1, 5]
