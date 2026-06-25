# -*- coding: utf-8 -*-
"""Czysta logika kolejnosci galerii - wydzielona z app.py, zeby byla
testowalna BEZ importu app.py (app.py przy imporcie startuje watki workerow
i restore_sends -> realne wysylki; testy nie moga go importowac).

Wspolne dla podgladu/wysylki (build_ido_plan_preview) i pozycji w siatce
Studio (gallery_pos) - zeby pokazywaly to samo.
"""


def gallery_ordered(rows, mode="fashion_second"):
    """Domyslna kolejnosc galerii: [1: glowny profil/shop-ikona] [2: JEDNO
    lifestyle (hak)] [3+: zdjecia towaru] [potem: pozostale lifestyle] [delete na
    koniec]. rows = decisions albo items (potrzebne pola: action, fashion).
    mode 'raw' = bez zmian."""
    if mode != "fashion_second":
        return list(rows)
    non_del = [r for r in rows if r.get("action") != "delete"]
    dels = [r for r in rows if r.get("action") == "delete"]
    # zdjecie z ikona 'shop' (miniaturka IdoSell, przycisk Lista) -> POZYCJA 1.
    # Allegro bierze pozycje 1 jako miniaturke, wiec miniaturka sklepu = pierwsze
    # zdjecie na obu. Domyslnie shop jest na #1, wiec to bez zmian; rozni sie tylko
    # gdy user przeniosl Liste na inne zdjecie. Brak shop -> stara logika.
    lead = next((r for r in non_del if "shop" in (r.get("icons") or [])), None)
    if lead is None:
        lead = next((r for r in non_del if not r.get("fashion")), None)
    if lead is None:
        return list(rows)   # same lifestyle / brak nie-fashion i shop -> bez zmian
    rest = [r for r in non_del if r is not lead]
    fashion = [r for r in rest if r.get("fashion")]
    nonfash = [r for r in rest if not r.get("fashion")]
    # pozycja 2 = TYLKO JEDNO lifestyle (hak); pozostale lifestyle na KONIEC, zeby
    # zdjecia TOWARU szly wczesniej. Wczesniej wszystkie lifestyle ladowaly na
    # 2,3,4... i spychaly zdjecia produktu dalej.
    return [lead] + fashion[:1] + nonfash + fashion[1:] + dels
