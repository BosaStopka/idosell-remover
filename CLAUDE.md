# CLAUDE.md - procedury projektu idosell-remover

Projekt to kopia bg-remover przerabiana pod IdoSell. Modul Allegro
zostal w kodzie tymczasowo (nieaktywny bez configow) - docelowo do wyciecia.

## Zasady pracy

1. **Pytaj przed zmianami** - najpierw propozycja i akceptacja, potem edycja.
   Przy eksperymentach generuj warianty do porownania (sample), nie nadpisuj
   jedynej wersji.
2. **Zapis do IdoSell tylko przez UI** - zadnych PUT/POST do produktow ze
   skryptow. Kazda zmiana zdjec produktu przechodzi przez dwustopniowe
   potwierdzenie w aplikacji (podglad payloadu + checkbox + przycisk).
   Przed kazdym zapisem: fizyczny backup aktualnych zdjec na dysk
   (originals/idosell/{productId}/), po zapisie: weryfikacja GET-em.
3. **Testuj przed oddaniem** - po kazdej zmianie serwera: restart + testy
   curl (bramka PIN 401/403, endpointy, happy path). Po zmianie pipeline:
   sample na zdjeciach z input/. Pierwszy zapis do IdoSell wylacznie na
   produkcie testowym/ukrytym.
4. **Interpunkcja** - zwykly myslnik `-`, nigdy polpauza/pauza (globalna
   zasada uzytkownika).

## Git

- Osobne repo (kopia bg-remover z 2026-06-12), galaz `main`, jednoosobowo
- Commit po kazdej zakonczonej funkcji/poprawce, opis po polsku
- **NIGDY nie commitowac**: idosell_config.json, allegro_config.json,
  allegro_token.json, app_config.json, jobs_state.json, scan_state.json,
  idosell_audit.jsonl, done/, input/, originals/ (wszystko w .gitignore)

## Architektura - gdzie co zmieniac

- Parametry obrobki (cien, kolory, rozmiar): `pipeline.py` DEFAULTS
- Endpointy / kolejka / skan / audyt: `app.py` (port 5001!)
- IdoSell API (auth X-API-KEY, search, upload, delete): `idosell_client.py`
  (do napisania; wzorzec: allegro_client.py)
- Dokumentacja API IdoSell: `docs/idosell_api_zdjecia.md` (v8, schematy)
- UI: `static/index.html` (jeden plik, vanilla JS)
- Tryb wsadowy CLI: `process.py` (niezalezny od aplikacji)

## IdoSell - kluczowe fakty

- Baza: https://www.bosastopka.pl/api/admin/v8, klucz w naglowku X-API-KEY,
  klucz lokalnie w idosell_config.json (poza gitem)
- Odczyt: POST /products/products/search z returnElements ["pictures"]
- Zapis: PUT /products/images (per slot productImageNumber, base64 lub url);
  kasowanie: POST /products/images/delete (po productImageId)
- 207 Multi-Status = czesciowy sukces, sprawdzac odpowiedz per zdjecie
- 429 = rate limit, klient musi miec throttling + retry z backoff
- productsImagesApplyMacro: sprawdzic ustawienia panelu zanim cokolwiek
  wgramy (IdoSell moze przetwarzac zdjecia swoim makrem)

## Pamiec srodowiska

- Windows 11, 15 GB RAM (czesto malo wolnego!) - BiRefNet wymaga
  `enable_cpu_mem_arena=False`, retry przy "bad allocation"
- PowerShell 5.1: brak `&&`, uwaga na cudzyslowy w `python -c`
  (lepiej heredoc w git-bash albo plik)
- Restart serwera: zabij proces python z app.py, odpal nowy w tle
- Port 5001 (bg-remover zajmuje 5000 - moga dzialac rownolegle)

## Kontekst biznesowy

- Sklep bosastopka.pl (IdoSell), te same produkty co ~1800 ofert Allegro
- SKU: czlon przed pierwszym `-` = model, sufiks = wariant rozmiarowy
- Dokumentacja IdoSell readme.io renderowana JS-em: dopisac `.md` do URL
  strony, by dostac markdown z pelnym OpenAPI (dziala przez przegladarke)
