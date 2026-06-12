# idosell-remover - Zdjecia produktow IdoSell na bialym tle

Kopia projektu bg-remover (stan 2026-06-12) przerabiana pod IdoSell.
Oryginal (bg-remover, modul Allegro) zostaje nietkniety i dziala dalej.

## Cel
Narzedzie do masowej poprawy zdjec produktow w sklepie bosastopka.pl
(IdoSell): pobranie zdjec produktu przez API, wyciecie tla (BiRefNet),
bialy kadr 1:1 + cien, wgranie z powrotem do IdoSell - z pelna kontrola
uzytkownika (plan per zdjecie, dwustopniowe potwierdzenie, backup, rollback).

## Stan poczatkowy (po kopii)
- Silnik obrobki: pipeline.py (birefnet-general, dziala bez zmian)
- Aplikacja webowa: app.py na porcie 5001 (PIN, kolejka, ZIP - dziala)
- Modul Allegro: zostawiony tymczasowo, nieaktywny (brak configow w repo),
  sluzy jako wzorzec dla modulu IdoSell; docelowo do wyciecia
- Modul IdoSell: DO ZBUDOWANIA (plan ponizej)

## Plan budowy modulu IdoSell

### Krok 0 - test read-only (najpierw!)
Skrypt testowy: GET 1 produktu przez products/products/search z kluczem
z idosell_config.json. Potwierdza format odpowiedzi (productImages,
productImageId, URL-e) zanim powstanie wlasciwy klient.

### Faza 1 - odczyt i obrobka
- idosell_client.py: search produktow (ID/SKU/kod), lista zdjec, download
- Zakladka IdoSell w UI: filtry, auto-skan tla (heurystyka naroznikow
  jak w bg-remover), plan per zdjecie (Obrob/Zostaw/Usun),
  done/{productId}/plan.json
- Throttling + retry (429), obsluga 207 Multi-Status

### Faza 2 - zapis (wylacznie przez UI)
- Przed zapisem: fizyczny backup aktualnych zdjec produktu na dysk
  (originals/idosell/{productId}/ + lista w plan.json)
- PUT /products/images (base64, per slot productImageNumber);
  usuwanie POST /products/images/delete po productImageId
- Dwustopniowe potwierdzenie (podglad payloadu + checkbox + przycisk)
- Po zapisie: weryfikacja GET-em (liczba/kolejnosc zdjec), alert przy
  rozbieznosci; rollback z backupu; audyt idosell_audit.jsonl
- Pierwszy zapis: tylko produkt testowy/ukryty
- Sprawdzic productsImagesApplyMacro vs ustawienia panelu

## Szczegoly API: docs/idosell_api_zdjecia.md

## Silnik obrobki (bez zmian wzgledem bg-remover)
1. rembg + birefnet-general -> RGBA z czysta maska
2. refine_edges (utwardzenie krawedzi), crop do bbox
3. Skalowanie do max 90% canvasu (domyslnie 1600x1600), cien dwuwarstwowy
4. JPG quality 95 do done/{ID}/

Pamiec: enable_cpu_mem_arena=False (15 GB RAM, malo wolnego);
retry przy "bad allocation"; wejscia >2048px zmniejszane.

## Struktura
```
idosell-remover\
  app.py              <- serwer Flask (port 5001)
  pipeline.py         <- silnik obrobki
  idosell_client.py   <- klient IdoSell API (do napisania)
  allegro_client.py   <- wzorzec (do wyciecia po zbudowaniu IdoSell)
  process.py          <- tryb wsadowy CLI
  static\index.html   <- UI (jeden plik)
  docs\idosell_api_zdjecia.md
  idosell_config.json <- klucz API (poza gitem!)
  input\ done\ originals\  <- dane robocze (poza gitem)
```

## Zaleznosci Python
rembg (>=2.0.75), onnxruntime, Pillow, numpy, flask, requests
