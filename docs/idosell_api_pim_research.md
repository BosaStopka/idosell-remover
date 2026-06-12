# IdoSell Admin API v8 - research pod budowę własnego PIM

Data: 2026-06-12. Źródła: idosell.readme.io (OpenAPI v8, trik z .md),
indeks llms.txt (~300 endpointów), plus fakty zweryfikowane empirycznie
na koncie bosastopka.pl w trakcie budowy idosell-remover.

Cel: ocena, czy i jak na IdoSell da się oprzeć własny PIM w stylu
Akeneo/Ergonode - co API daje, czego brakuje, jak to spiąć.

---

## 1. Fundamenty API

| Aspekt | Stan |
|---|---|
| Baza | `https://www.bosastopka.pl/api/admin/v8` |
| Auth podstawowy | nagłówek `X-API-KEY` (klucz z panelu, per aplikacja) |
| Auth alternatywny | `POST /authorize/accessToken` - OAuth Bearer (Basic auth na wejściu, token ~3600 s, scope np. "admin"); dla PIM wystarczy X-API-KEY |
| Wersjonowanie | v8 w ścieżce; starsze v3/v5 wciąż żyją (stare integracje) |
| Paginacja | `resultsPage` (od 0) + `resultsLimit` (1-100); odpowiedź: `resultsNumberAll` (rekordy) i `resultsNumberPage` (LICZBA STRON) |
| Statusy | 200 OK, **207 Multi-Status** (częściowy sukces ALBO pusty wynik - patrz pułapki), 422 format, 429 rate limit |
| Rate limit | nieudokumentowany licznik; 429 wymaga throttlingu + retry z backoff (nasz klient: 0.5 s odstępu, retry x5) - do zmierzenia w praktyce przy masowych operacjach |
| Webhooki / eventy | **BRAK w API** - synchronizacja wyłącznie pollingiem (na szczęście są filtry po datach zmian, pkt 5) |
| Bulk | `POST /products/synchronization/file` - import plikowy IOF 3.0 (pkt 5) |

## 2. Mapa zasobów istotnych dla PIM

CRUD na wszystkim, co stanowi katalog:

| Zasób | Endpointy | Uwagi |
|---|---|---|
| Produkty | GET/POST/PUT/DELETE `/products/products`, `POST /products/products/search`, `/products/restore`, `/products/codeExistence` | search to główne narzędzie odczytu (GET filtruje tylko po ID!) |
| Opisy + SEO | GET/PUT `/products/descriptions` | nazwy, opis krótki/długi, **meta title/description/keywords**, opisy aukcyjne, sekcje opisów - per `langId` i per `shopId` |
| Parametry (atrybuty) | PUT/DELETE/SEARCH `/products/parameters` | sekcje → parametry → wartości; nazwy/opisy/ikony per język i sklep; 24 konteksty systemowe (kolor, stan, GHS, wagi...) |
| Kategorie własne | GET/PUT `/products/categories` | drzewo id/parent_id/priority, nazwy per język, mapowania na Allegro/eBay/WP |
| Kategorie IdoSell | `POST /products/categoriesIdosell/search` | globalna taksonomia IdoSell (odpowiednik kategorii marketplace) |
| Producenci (marki) | GET/POST/PUT/DELETE `/products/brands` + `brands/filter` | filtry nawigacyjne per marka |
| Serie | GET/PUT/DELETE `/products/series` + `series/filter` | |
| Rozmiary (warianty magazynowe) | GET/PUT/DELETE `/products/sizes`, `/sizes/sizes`, grupy rozmiarów | ceny i waga per rozmiar per sklep; `productIdBySizecode`, `productSkuByBarcode` |
| Warianty (wersje) | w modelu produktu: `productVersion` | grupa wariantów z 50+ flagami wspólności pól (cena/opis/kategoria wspólne lub nie) |
| Zdjęcia | PUT `/products/images`, POST `/products/images/delete` | per slot, base64/url, applyMacro; limit 4000x4000 |
| Załączniki | PUT `/products/attachments` | pliki audio/video/doc, typy dokumentów (energy_label, user_manual), załączniki wirtualne z limitami pobrań |
| Stany | GET/PUT `/products/stocks`, PUT `/products/stockQuantity`, GET `/products/reservations` | set/add/subtract per magazyn (stockId) per rozmiar + lokalizacje magazynowe |
| Ceny specjalne | strikethrough (przekreślone), **omnibus** (GET/PUT - dyrektywa UE!), cenniki indywidualne, grupy rabatowe | |
| Zestawy/kolekcje | `/products/bundles/*`, `/products/collections/*` | produkty złożone |
| Powiązania | `associatedProducts` w PUT products, `/products/groups/*` (grupy z produktem głównym i kolejnością) | rekomendacje, grupy wariantowe |
| Multistore | GET `/system/shopsData`, `/shops/languages`, `/shops/currencies`, `/system/config` | sklepy, języki (ISO-639-3), waluty - to są "kanały" PIM |
| Jednostki | GET/PUT `/system/units` | jednostki miary per język |
| Gwarancje | CRUD `/warranties/*` | |
| Dostawcy | `/wms/suppliers/*`, `productSizeCodeDeliverer` | |
| Aukcje | GET `/products/auctions` | odczyt powiązań produkt ↔ aukcja Allegro |
| Opinie | CRUD `/products/opinions` | recenzje produktów (do PIM raczej nie, ale jest) |

Poza zakresem PIM, ale dostępne: pełne zamówienia, klienci, płatności,
zwroty/RMA, paczki/kurierzy, promocje, vouchery, menu sklepu, blog (CMS),
WMS (dokumenty magazynowe), subskrypcje.

## 3. Model danych produktu (najważniejsze ustalenia)

### Identyfikacja
- `productId` (IAI, wewnętrzny int), `productIndex` (unikalny indeks:
  IAI / kod zewnętrzny / kod producenta), `productDisplayedCode`,
  per rozmiar: `productSizeCodeExternal` / `Producer` / `Deliverer`.
- W zapisach `productIdent.productIdentType`: id | index | codeExtern |
  codeProducer - PIM może adresować produkty własnym SKU.

### Ceny - trzy poziomy
1. **Globalnie**: retail/wholesale/minimal/suggested/POS/strikethrough
   (brutto i netto), VAT, vatFree, `priceFormula` (funkcja JS!).
2. **Per sklep** (`productShopsAttributes[]`): komplet cen + profil
   (`default_prices` / `wholesale_equals_retail` / ...), ceny porównywarek,
   ceny aukcji.
3. **Per rozmiar**: ceny w `productSizes[].sites[]`, waga per rozmiar.
- `priceChangeMode`: amount_set | amount_diff | percent_diff.
- Omnibus i ceny przekreślone osobnymi endpointami.

### Treść i SEO
- `PUT /products/descriptions`: nazwa, nazwa aukcyjna, nazwa porównywarek,
  opis krótki/długi, **metaTitle/metaDescription/metaKeywords** -
  wszystko per `langId` x `shopId` (pełna macierz kanał x język).
- Sekcje opisów (`productDescriptionSections`): typy text / photo /
  video / html - strukturalny opis jak w naszych aukcjach Allegro.
- UWAGA: SEO NIE ma w PUT /products/products - tylko w descriptions.

### Atrybuty (parametry)
- Hierarchia: sekcja → parametr → wartości; per język i per sklep
  (nazwy, opisy, ikony karty/listy, opis wyszukiwania).
- `context_id` - 24 systemowe znaczenia (CONTEXT_COLOR, CONTEXT_STATE,
  wagi, GHS, dla dorosłych itd.) - mapują parametr na funkcje sklepu.
- Przypisanie do produktu: `productParametersDistinction` w PUT products
  (+ `parametersConfigurable` z modyfikatorami cen input/radio/select).
- To jest płaski słownik typu Ergonode "attributes + options" -
  bez typów danych (wszystko tekstowe), bez walidacji, bez rodzin.

### Warianty
Dwa mechanizmy, łatwo pomylić:
1. **Rozmiary** (`productSizes`) - warianty magazynowe wewnątrz produktu
   (nasz przypadek: rozmiar buta). Własne kody, ceny, stany per magazyn.
   Zmiana grupy rozmiarów ZERUJE stany!
2. **Wersje** (`productVersion`) - grupa POWIĄZANYCH produktów
   (np. kolory tego samego modelu): versionParentId + nazwy parametru
   grupującego per język + 50+ flag `versionCommon*` decydujących,
   które pola są wspólne. To odpowiednik "product model + variants"
   z Akeneo, ale na poziomie powiązania niezależnych produktów.

### Media
- Galeria per slot (1..n) z priorytetem, 3 ikony (sklep/aukcja/grupa),
  hash MD5 zdjęcia (świetne do detekcji zmian przy syncu), załączniki
  plikowe z typami dokumentów.
- `picturesSettingApplyMacroForPictures` - panel może przetwarzać
  zdjęcia własnym makrem (skalowanie/znak wodny).

### Flagi handlowe
- nowość/bestseller/promocja/wyróżniony/specjalny (per sklep w
  hotspotach), priorytet 1-10, widoczność: globalna, per sklep
  (shopsMask bitowo: 2^(shopId-1)), eksport do porównywarek / Amazon /
  Strefy Marek Allegro per produkt.

### Audyt zmian po stronie IdoSell
- `productAddingTime`, `productModificationTime`,
  `productPriceChangedTime`, `productQuantityChangedTime` -
  fundament syncu przyrostowego.

## 4. Tworzenie danych - zależności i kolejność

POST /products/products potrafi stworzyć produkt z kompletem danych
naraz, a `settingAdding*Allowed` pozwala w locie dokładać słowniki.
Bezpieczniej jednak słowniki prowadzić jawnie:

```
1. system/shopsData, shops/languages, currencies   (odczyt kanałów)
2. products/parameters        (sekcje, parametry, wartości)
3. products/categories        (drzewo własne) + mapowanie categoriesIdosell
4. products/brands, products/series, system/units, warranties
5. sizes/sizecharts + grupy rozmiarów
6. POST products/products     (produkt + rozmiary + ceny + kategoria)
7. PUT products/descriptions  (treści + SEO per język/sklep)
8. PUT products/images        (galeria per slot)
9. PUT products/stocks        (stany per magazyn)
10. bundles/collections/associated (powiązania)
```

Ustawienia zapisu (`settings` w PUT/POST products) są kluczowe:
`settingModificationType` (add/edit/all), `settingAdding*Allowed`,
`settingsRestoreDeletedProducts`, maski usuwania opisów per sklep.
PIM powinien jeździć z `settingAdding*Allowed: "n"` (słowniki tylko
jawnie) i `settingModificationType: "edit"` przy aktualizacjach -
chroni przed przypadkowym tworzeniem śmieci.

## 5. Synchronizacja - strategia dla PIM

### Przyrostowa (polling, brak webhooków)
`POST /products/products/search` z `productDate`:
- `productDateMode`: added | modified | quantity_changed | price_changed |
  modified_and_quantity_changed (+ finished/resumed)
- `productDateBegin`/`End` w formacie **YYYY-MM-DD (granulacja dzienna!)**
- pętla co X minut: pobierz zmienione od wczoraj, porównaj
  `productModificationTime` (sekundowy) i hashe zdjęć z lokalnym stanem,
  aktualizuj różnice. Dzienna granulacja filtra = trzeba dociągać cały
  dzień i odsiewać lokalnie po timestampach.

### Masowa (initial load / pełny eksport z PIM)
`POST /products/synchronization/file` + `PUT finishUpload`:
- format **IOF 3.0** (XML IdoSell), typy: full / light / categories /
  sizes / series / guarantees / parameters
- upload w częściach (packageId, numberInPackage, base64 + MD5)
- to jest właściwa droga dla tysięcy produktów naraz; REST per produkt
  zostaje do bieżących edycji.

### Praktyczne tempo (zmierzone)
- search 100 produktów z pictures+lang_data: ~1-2 s/strona;
  pełny odczyt 4032 produktów: kilka minut.
- Z throttlingiem 0.5 s: ~7200 wywołań/h - wystarczy na bieżącą
  synchronizację katalogu naszej skali (4-5 tys. produktów).

## 6. Luki i pułapki (część zweryfikowana na żywym koncie)

**Zweryfikowane przez nas:**
1. Filtr po ID działa TYLKO jako `productParams: [{"productId": N}]`
   (lista obiektów); `{"productIds": [...]}` jest po cichu ignorowany
   i zwraca wszystko.
2. Przy `returnProducts: "deleted"` filtry serwera nie działają
   (by id → 0 wyników dla istniejących, containsCodePart ignorowany,
   zwraca komplet) - filtrować lokalnie.
3. Pusty wynik = HTTP 207 + faultCode 2 - nie traktować jako błąd.
4. Filtrowanie parametrów po NAZWACH niewiarygodne (zwraca produkty
   z jakimkolwiek parametrem tej grupy) - wyłącznie po ID wartości
   (`productParameterIdsEnabled/Disabled`).
5. `resultsNumberPage` = liczba stron, nie rekordów.

**Z dokumentacji / architektury:**
6. Brak webhooków - tylko polling; filtr dat z granulacją dzienną.
7. GET /products/products filtruje tylko po productIds (max 100) -
   pełnoprawne wyszukiwanie wyłącznie przez POST search.
8. Słownik atrybutów bez typów danych, walidacji, jednostek,
   wartości liczbowych/zakresów - wszystko stringi per język.
9. Brak pojęcia rodziny atrybutów (family), completeness,
   workflow/draftów, wersjonowania treści - to musi zrobić PIM.
10. Limit 100 rekordów/strona wszędzie; brak pól wybiórczych
    w GET (returnElements tylko w search).
11. Rate limit nieudokumentowany - zakładać 429 i backoff.
12. `productAuctionLongDescription` deprecated - opisy aukcji przez
    productAuctionDescriptionsData (per serwis aukcyjny).
13. Zmiana grupy rozmiarów zeruje stany magazynowe.
14. apply macro zdjęć - panel może nadpisać obróbkę PIM; trzymać false
    i zweryfikować ustawienia panelu.

## 7. Architektura własnego PIM (propozycja)

IdoSell traktujemy jako **kanał publikacji** (jeden z wielu), nie jako
źródło prawdy. Źródłem prawdy staje się PIM.

```
+----------------------------- PIM ------------------------------+
|  Katalog (DB)          Słowniki            Media (DAM)         |
|  - produkty            - atrybuty (typy!)  - oryginały         |
|  - warianty            - rodziny/szablony  - obrobione 1:1     |
|  - treści per          - kategorie (las:   - hash/wersje       |
|    kanał x język         własne + IdoSell  (zalążek: done/,    |
|  - completeness          + Allegro)         originals/)        |
|                                                                |
|  Workflow: draft -> review -> approved -> published            |
|  Historia zmian (event log per pole)                           |
+--------------------------|-------------------------------------+
                           | warstwa synchronizacji
                           | (kolejka, diff, throttling, audyt)
        +------------------+------------------+
        v                  v                  v
   IdoSell API        Allegro API        (przyszłe kanały:
   (sklep+stany)      (mamy klienta)      Amazon, porównywarki)
```

### Komponenty do zbudowania
1. **Baza katalogowa** - Postgres; atrybuty jako JSONB per produkt +
   tabela definicji atrybutów (typ: tekst/liczba/słownik/bool/media,
   lokalizowalny?, per kanał?, wymagany w rodzinie?). To przewaga nad
   IdoSell: typy i walidacja u nas.
2. **Słownik atrybutów z mapowaniem** - każdy atrybut PIM ma mapę:
   → parametr IdoSell (id), → parametr Allegro (id), → pole opisu.
   Mapowanie kategorii: drzewo PIM → categoryId IdoSell → kategoria
   Allegro (już dziś IdoSell trzyma allegro_category_id).
3. **Rodziny produktów** (np. "buty barefoot dziecięce") - zestaw
   wymaganych atrybutów → liczenie completeness per produkt per kanał
   (czego brakuje do publikacji gdzie).
4. **DAM** - rozszerzenie obecnych done/originals: deduplikacja po
   hashu (IdoSell daje MD5), warianty per kanał (1600 sklep, kadr
   Allegro), pipeline obróbki już istnieje (BiRefNet).
5. **Sync engine** - per kanał adapter (idosell_client już jest
   zalążkiem); diff stanu lokalnego vs zdalnego (timestampy + hashe),
   kolejka zapisów z throttlingiem, dwukierunkowość TYLKO dla pól,
   których PIM nie jest właścicielem (stany, ceny zakupu - one płyną
   Z IdoSell DO PIM, treści płyną Z PIM DO IdoSell). Jasny podział
   własności pól = brak wojen o nadpisywanie.
6. **UI** - tabela produktów z edycją atrybutów, completeness,
   podgląd per kanał; wzorzec: obecna zakładka IdoSell.
7. **Audyt i bezpieczeństwo** - jak w idosell-remover: append-only
   dziennik każdej publikacji, backup przed zapisem, weryfikacja
   GET-em po zapisie, dwustopniowe potwierdzenie masowych operacji.

### Czego NIE budować (IdoSell to już robi)
- magazyn/rezerwacje/lokalizacje (WMS IdoSell), zamówienia, klienci,
  omnibus/cenniki, render sklepu, wystawianie na Allegro z poziomu
  panelu (idzie przez powiązania produkt-aukcja).

## 8. Proponowany roadmap MVP

1. **Read-only mirror** - pełny zrzut katalogu do lokalnej bazy
   (search + descriptions + parameters + categories + brands/series +
   sizes), sync przyrostowy pollingiem. Od razu wartość: szybkie
   wyszukiwanie/raporty bez limitów API.
2. **Słownik atrybutów + mapowania** - definicje typów nad surowymi
   parametrami IdoSell, rodziny, completeness (raport braków).
3. **Edycja treści w PIM** - opisy/SEO/atrybuty per język/sklep,
   publikacja przez PUT descriptions/parameters/products
   (z naszym wzorcem: podgląd payloadu → potwierdzenie → audyt →
   weryfikacja GET-em).
4. **DAM** - spięcie istniejącego pipeline zdjęć z katalogiem.
5. **Drugi kanał** - publikacja Allegro bezpośrednio (klient już jest)
   albo przez IdoSell - decyzja po zmierzeniu, czego brakuje w
   powiązaniach produkt-aukcja.
6. (opcjonalnie) **Import dostawców** - IOF/CSV od dystrybutorów
   do PIM zamiast ręcznego zakładania kartotek.

## 9. Wnioski

API IdoSell jest WYSTARCZAJĄCE do roli kanału publikacji pod własnym
PIM: pełny CRUD katalogu, treści i SEO per język x sklep, słownik
parametrów, media, stany, bulk import IOF, znaczniki czasu zmian.
Krytyczne braki względem gotowych PIM (typowane atrybuty, rodziny,
completeness, workflow, wersjonowanie, webhooki) są dokładnie tym,
co własny PIM ma wnosić - i nic w API tego nie blokuje.

Największe ryzyka projektowe: (a) nieudokumentowany rate limit przy
masowych zapisach, (b) dzienna granulacja filtrów dat przy pollingu,
(c) rozjazd dwóch mechanizmów wariantów (rozmiary vs wersje) - model
PIM musi to odwzorować od pierwszego dnia, (d) niespodzianki w API
jak te z pkt 6 - każdy nowy endpoint testować na żywym koncie zanim
wejdzie do automatu (sprawdzony sposób: krok 0 jak w tym projekcie).
