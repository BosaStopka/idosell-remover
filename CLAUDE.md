# CLAUDE.md - procedury projektu idosell-remover

Projekt to kopia bg-remover przerabiana pod IdoSell. Modul Allegro
zostal w kodzie tymczasowo (nieaktywny bez configow) - docelowo do wyciecia.

## Integracja Allegro <-> sklep (wspolny plik z bg-remover)
Przy zadaniach dot. wypychania zdjec TEZ na Allegro przy pushu na sklep:
NA POCZATKU przeczytaj i aktualizuj wspolny plik
`C:\Users\Dell\WSPOLPRACA_allegro.md` (kontrakt API + skrzynka + dziennik,
dzielone z bg-remover). **Architektura: idosell WOLA bg-remover (HTTP, port
5000) - NIE robi Allegro sam** (lokalny modul Allegro zostaje nieaktywny).
W skrzynce jest prosba `-> dla [ido]` o Twoja opinie na temat kontraktu -
odpowiedz wpisem `[ido]` w tym pliku. Pelna instrukcja:
`C:\Users\Dell\INSTRUKCJA_ALLEGRO_PUSH_idosell.md`.

## Zasady pracy

1. **Pytaj przed zmianami** - najpierw propozycja i akceptacja, potem edycja.
   Przy eksperymentach generuj warianty do porownania (sample), nie nadpisuj
   jedynej wersji.
2. **Zapis do IdoSell tylko przez UI** - zadnych PUT/POST do produktow ze
   skryptow. Kazda zmiana zdjec produktu przechodzi przez dwustopniowe
   potwierdzenie w aplikacji (podglad payloadu + checkbox + przycisk).
   Przed kazdym zapisem: fizyczny backup aktualnych zdjec na dysk
   (originals/idosell/{productId}/), po zapisie: weryfikacja GET-em.
3. **Testuj przed oddaniem** - od 2026-06-25 jest pytest:
   **`python -m pytest tests/`** uruchamiac PRZED oddaniem kazdej zmiany
   (lapie m.in. bilans backtickow/klamr JS = czarne tlo, filtry /api/jobs,
   rejestracje endpointow, czyste funkcje pipeline/affenzahn). Po zmianie
   serwera: restart + testy. Po zmianie pipeline: sample na zdjeciach z
   input/. Pierwszy zapis do IdoSell wylacznie na produkcie testowym/ukrytym.
4. **Interpunkcja** - zwykly myslnik `-`, nigdy polpauza/pauza (globalna
   zasada uzytkownika).
5. **UI jak profesjonalny designer** - kazda zmiana UI (static/index.html)
   ma byc zrobiona jak profesjonalny designer UI/UX i SPOJNA z reszta apki:
   ten sam dark theme i tokeny (--accent #7c6cff, --accent2 #19e3d6, --radius,
   Segoe UI), jednolity zestaw ikon (Tabler), spojna hierarchia (primary vs
   ghost), odstepy i wzorce komponentow. Przy wiekszych przeprojektowaniach
   najpierw makieta do akceptacji. Po edycji index.html walidacja bilansu
   backtickow/klamr (hot-path render moze zblankowac strone). Spojnosc >
   pospiech.

## Wzorzec bezpieczenstwa zapisu (OBOWIAZKOWY)

Przeniesione z bg-remover - tam brak backupu opisu raz spowodowal
nieodwracalna utrate tresci. Kazda operacja zmieniajaca dane na IdoSell MUSI:
1. **Backup PELNEGO stanu PRZED zapisem** - wszystko co modyfikujemy
   (zdjecia + CALY opis: tekst i zdjecia), zapis lokalny per produkt.
2. **Rollback przywracajacy stan 1:1** z backupu, jednym klikiem w UI
   (zweryfikowane w bg-remover: wraca dokladnie do oryginalu).
3. **Backup oddzielny per produkt** - rollback niezalezny dla kazdego.
4. **Dziennik append-only** - kazdy zapis logowany.
5. Po recznej zmianie na platformie nie uzywac rollbacku (cofa do starego
   backupu); kolejny zapis przez aplikacje czyta dane na zywo i nadpisuje
   backup swiezym stanem.

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
