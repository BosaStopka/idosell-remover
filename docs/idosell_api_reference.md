# IdoSell Admin API v8 - skonsolidowana referencja (dla AI)

INSTRUKCJA DLA AI, nie dla człowieka. To jest kompletna, samowystarczalna
wiedza o IdoSell Admin API v8 zebrana empirycznie na ŻYWYM koncie
(bosastopka.pl) - tak, żeby kolejna aplikacja NIE musiała robić researchu od
zera. Fakty pochodzą z działającego klienta (`idosell_client.py`) i testów na
realnych produktach. Daty w nawiasach = kiedy zweryfikowano.

Szczegółowe docsy w tym repo (uzupełniają, nie zastępują):
- `docs/idosell_api_zdjecia.md` - obrazy produktów (schematy, przykłady).
- `docs/idosell_api_pim_research.md` - model danych produktu, ceny, warianty,
  synchronizacja, budowa własnego PIM.

---

## 0. TL;DR (najważniejsze pułapki)

1. Body KAŻDEGO żądania POST/PUT jest opakowane: `{"params": <dict|list>}`.
2. Filtr po ID: `productParams: [{"productId": 334}]` - LISTA OBIEKTÓW.
   `productParams: {"productIds": [...]}` jest po cichu IGNOROWANY (zwraca cały
   katalog). To najczęstszy błąd.
3. `207 Multi-Status` NIE znaczy sukcesu - trzeba sprawdzać `errors.faultCode`
   per element. Pusty wynik search = 207 + faultCode 2 (to nie błąd).
4. Filtruj parametry WYŁĄCZNIE po ID (parameterId / parameterValueId), nigdy po
   nazwach (nazwy zwracają śmieci).
5. Serii (modelu) NIE da się filtrować serwerowo - trzeba indeks po stronie klienta.
6. "Kategoria" nie jest osobnym returnElement - to parametr "Rodzaj" (id 88).
7. Zapis zdjęć: POMIJAJ `shopId` żeby pisać GLOBALNIE; podanie shopId tworzy
   osobny zestaw per-sklep (zakładka w panelu).
8. Ikon NIE wysyłaj w tym samym PUT co galerię - znikną. Osobny PUT PO galerii.
9. Ceny per rozmiar: zapis `PUT /products/products` przez tablicę **`productSizes`** (nie `productSizesAttributes`), `priceChangeMode:"amount_set"`, `settingModificationType:"edit"`; podanie jednego rozmiaru edytuje tylko jego. Szczegóły i mapa pól: sekcja 11.
10. `faults:[]` w odpowiedzi zapisu NIE gwarantuje, że pola weszły (POS = wymuszona detaliczna, sugerowana = poziom produktu) - ZAWSZE weryfikuj GET-em.
11. `0` w polu ceny = "nie zmieniaj", NIE "ustaw zero" (wyjątek: przekreślona detaliczna = 0 czyści). Minimalnej/sugerowanej nie wyczyścisz przez API wysyłając 0. Ceny zapisuj REST-em, nie webowym importem (ten generuje błędne `shopsMask:0`).

---

## 1. Połączenie i autoryzacja

- Baza: `https://www.bosastopka.pl/api/admin/v8` (wzorzec: `https://<domena>/api/admin/v8`).
- Auth: nagłówek `X-API-KEY: <klucz>`. Dodatkowo `Content-Type: application/json`,
  `Accept: application/json`.
- Body POST/PUT: zawsze `{"params": <payload>}`. `<payload>` to dict (search,
  PUT images) ALBO lista (images/delete).
- GET: parametry w query stringu (nie w body) - patrz ikony, sekcja 6.
- Throttling: trzymaj min. ~0.5 s między żądaniami.
- Retry: na `429` czekaj wg nagłówka `Retry-After` (albo rosnący backoff), do ~5 prób.
- Kody: `200` OK, `207` Multi-Status (SPRAWDŹ per element!), `401`/`403` zły klucz,
  `429` rate limit. Inne = błąd.

Minimalny request (Python, referencyjnie):
```python
requests.request(method, base_url + path,
  headers={"X-API-KEY": key, "Content-Type": "application/json",
           "Accept": "application/json"},
  json={"params": params}, timeout=30)
```

---

## 2. Odczyt produktów - `POST /products/products/search`

Payload (dict) - pola:
- `returnProducts`: `"active"` | `"deleted"` (= archiwum IdoSell, usunięte) | `"in_trash"`.
- `returnElements`: lista, co zwrócić (patrz niżej).
- `resultsPage` (0-based), `resultsLimit` (max 100).
- Filtry (patrz 2.1).

Odpowiedź:
- `results`: lista produktów.
- `resultsNumberAll`: łączna liczba produktów (pełny total, nie cap).
- `resultsNumberPage`: LICZBA STRON przy danym limicie (nie rozmiar strony!).

### 2.1. Filtry (ZWERYFIKOWANE empirycznie)

- Po ID: `"productParams": [{"productId": 334}]` (LISTA OBIEKTÓW).
  `{"productIds": [...]}` jest IGNOROWANY → zwraca wszystko.
- Po fragmencie kodu: `"containsCodePart": "636401"` (szuka też w kodzie producenta).
- Po nazwie/opisie: `"containsText": "Bobux"` (marka/model w nazwie + opis).
- Dostępność magazynowa: `"productIsAvailable": "y"` | `"n"`.
- Parametry (tag/kategoria/sezon) - WYŁĄCZNIE po ID, w bloku:
  ```json
  "productParametersParams": [{
    "productParameterIds": {
      "productParameterIdsDisabled": [2386],
      "productParameterIdsEnabled": [74]
    }
  }]
  ```
  - `...Disabled`: produkt NIE MA tej wartości (twardo, AND - wyklucza).
  - `...Enabled`: produkt MA tę wartość. UWAGA: wartości z RÓŻNYCH parametrów
    w jednej liście enabled łączą się jak **OR, nie AND** (np. enabled
    [lato 74, kapcie 90] zwróci letnie LUB kapcie, nie "letnie kapcie").
    Rozbicie na osobne bloki też nie daje AND. Jeśli potrzebujesz AND wielu
    parametrów - dofiltruj po stronie klienta z `parameters` w wyniku.
  - Filtrowanie po NAZWACH parametrów jest niewiarygodne - tylko ID.
- `returnProducts: "deleted"`: filtry serwera NIE działają (by-id zwraca 0,
  containsCodePart ignorowany). Trzeba przeglądać strony i filtrować u klienta.
- Pusty wynik: HTTP 207 + `errors.faultCode == 2` ("pusty wynik") - to NIE błąd.

### 2.2. returnElements (zweryfikowane nazwy)

Działają: `"code"`, `"pictures"`, `"pictures_count"`, `"lang_data"`,
`"parameters"`, `"series"`, `"icon"` (tylko ikona shop).
NIE działają / ignorowane: `"category"`, `"categories"`, `"groups"`, `"group"`
(zwracane bez efektu - kategorii szukaj w parametrze "Rodzaj", patrz 4).

### 2.3. Pola produktu w wyniku

- `productId` (int), `productDisplayedCode` (string; **bywa pusty** np. Ameko).
- Nazwa: `productDescriptionsLangData` → element z `langId == "pol"` → `productName`.
- `productImages` (lista, gdy `pictures`): patrz 5.
- `productImagesCount`.
- `productParameters` (gdy `parameters`): lista `{parameterId, parameterType,
  parameterDescriptionsLangData[], parameterValues[]}`; każda wartość:
  `{parameterValueId, parameterValueDescriptionsLangData[] (→ langId "pol" →
  parameterValueName)}`.
- `productSeries` (gdy `series`): `{seriesId, seriesPanelName,
  seriesDescriptionsLangData[]}` - natywny MODEL/seria (warianty kolorystyczne
  współdzielą `seriesId`). NIE filtrowalne serwerowo - patrz 2.4.

### 2.4. Seria (model) - brak filtra serwerowego

`seriesId` wiąże warianty kolorystyczne modelu. Sprawdzone 6 wariantów filtra
(`seriesIds`, `productParams.seriesId`, `productSeriesParams`, top-level
`seriesId`/`productSeriesId`, `searchSeries`) - WSZYSTKIE ignorowane (zwracają
cały katalog). Aby grupować po serii: zbuduj indeks (jeden pełny skan z
`returnElements:["code","series"]`, mapuj `seriesId → [produkty]`, cache'uj).

---

## 3. Parametry produktu - znane ID (konto bosastopka.pl)

Filtruj zawsze po tych ID (nie po nazwach):

- Product Tag - `parameterId 993`. Wartość "Archiwum" = `2386`
  (konfigurowalne; produkty z tym tagiem pomijamy w skanie).
- "Rodzaj" (typ obuwia ≈ kategoria) - `parameterId 88`. Wartości:
  buty sportowe `435`, kapcie `90`, buty zimowe `99`, buty przejściowe `800`,
  sandały `93`, pierwsze buty `2294`, kalosze `196`, sneakersy `1000`.
- "Pora roku" (sezon) - `parameterId 38`. Wartości:
  wiosna `39`, lato `74`, jesień `40`, zima `98`.

(ID wartości są stałe per konto; nowy parametr/wartość → odczytaj raz z
`returnElements:["parameters"]` i zmapuj.)

---

## 4. Odczyt zdjęć

Z search (`returnElements:["pictures","pictures_count"]`), per produkt
`productImages[]`:
- `productImageId` (string) - ID do kasowania. Koduje NUMER SLOTU:
  `"334_2.jpg"` → slot 2 (część po `_`, przed `.`).
- `productImageLargeUrl` / `...MediumUrl` / `...SmallUrl` (+ `...Second` warianty).
- `productImageWidth`, `productImageHeight`, `productImageSize`,
  `productImagePriority`, `productImageHash`.

Zdjęcia pobierasz zwykłym GET-em po URL (publiczne CDN).

---

## 5. Zapis zdjęć - `PUT /products/images`

Payload (dict):
```json
{
  "productsImagesSettings": {
    "productsImagesSourceType": "base64",        // albo "url"
    "productsImagesApplyMacro": false             // true = IdoSell skaluje/przetwarza
  },
  "productsImages": [{
    "productIdent": { "productIdentType": "id", "identValue": "9911" },
    "productImages": [{
      "productImageSource": "<base64 lub URL>",
      "productImageNumber": 1,                     // NUMER SLOTU (1..N)
      "productImagePriority": 1,                   // kolejność w galerii
      "deleteProductImage": false
    }]
  }]
}
```
Reguły (ZWERYFIKOWANE):
- `productIdentType`: `id` | `index` | `codeExtern` | `codeProducer`.
- **GLOBALNIE vs PER-SKLEP**: POMIJAJ `shopId`/`otherShopsForPic` → zapis
  globalny ("wszystkie sklepy"). Podanie `shopId` (np. 1) tworzy ODDZIELNY
  zestaw zdjęć per sklep (osobna zakładka w panelu obok globalnego) - zwykle
  niepożądane.
- Edycja PER SLOT (`productImageNumber`) - nie podmienia całej galerii naraz.
  Żeby "wgrać galerię N zdjęć" → wstaw sloty 1..N. Slotów powyżej N to NIE
  kasuje - rób osobny delete (sekcja 7).
- `productsImagesApplyMacro: false` = wgrywamy gotowe kadry 1:1 (sprawdź
  ustawienia panelu, by makro nie psuło naszych zdjęć).
- Limit wymiarów: 4000x4000 px.
- Odpowiedź 207 → sprawdź `errors.faultCode` per element (`productImageNumber`).

---

## 6. Ikony produktu (3 sloty nad galerią)

Typy (`productIconType`, ZWERYFIKOWANE):
- `"shop"` - "Zdjęcie na liście towarów" (główna miniatura). Odczyt: `productIcon`.
- `"group"` - "Zdjęcie dla towaru w grupie". Odczyt: `productGroupIcon`.
- `"auction"` - "Zdjęcie bez tła". Odczyt: `productAuctionIcon`.

Zapis: ten sam `PUT /products/images`, ale tablica `productIcons` zamiast/obok
`productImages`:
```json
"productIcons": [{ "productIconSource": "<base64>", "productIconType": "shop",
                   "deleteProductIcon": false }]
```
Kasowanie ikony: `"deleteProductIcon": true` (bez source).

**KRYTYCZNE**: ikon NIE wysyłaj w tym samym wywołaniu co galerię. IdoSell
deduplikuje ikonę identyczną ze zdjęciem galerii do REFERENCJI na plik slotu;
równoczesna podmiana slotów unieważnia referencję i ikona znika. Kolejność:
najpierw PUT galerii, POTEM osobny PUT ikon.

Odczyt 3 ikon: search zna tylko `icon` (shop). Wszystkie 3 →
`GET /products/products?productIds=N` (query string!). Pola: dla każdego
`productIcon`/`productAuctionIcon`/`productGroupIcon` node z
`{field}Exists == "y"` i `{field}LargeUrl`. URL-e auction/group przychodzą BEZ
domeny (np. `hpeciai/...`) - doklej origin sklepu (`https://<domena>/`).

---

## 7. Kasowanie zdjęć - `POST /products/images/delete`

Payload to LISTA (opakowana w `{"params": [...]}`):
```json
[{
  "productId": 9911,
  "productImagesId": ["334_5.jpg", "334_6.jpg"],   // productImageId z search
  "deleteAll": false
  // opcjonalnie "shopId": 1 (pomijaj dla globalnego)
}]
```
207 → sprawdź faults per element.

---

## 8. Wzorzec bezpiecznego zapisu (OBOWIĄZKOWY)

Każda operacja zmieniająca dane w sklepie MUSI:
1. Pełny BACKUP stanu przed zapisem (zdjęcia + ikony; lokalnie, per produkt).
   Aktualny stan pobierz świeżym GET/search tuż przed.
2. Zapis: PUT galerii (sloty 1..N) → delete nadmiarowych slotów (>N) →
   osobny PUT ikon.
3. WERYFIKACJA GET-em po zapisie (liczba zdjęć + ikony zgodne z planem).
4. ROLLBACK 1:1 z backupu (PUT starej listy) - na żądanie.
5. Dziennik append-only każdej operacji.
6. 207 zawsze rozbieraj per element (`_collect_faults`: rekurencyjnie szukaj
   `errors.faultCode != 0`, raportuj `productImageNumber` slotu).

---

## 9. Inne zasoby (poza zdjęciami) i model danych

Pełny research (ceny 3-poziomowe, warianty/rozmiary, treść/SEO, flagi handlowe,
audyt, strategia synchronizacji bez webhooków, tempo) jest w
`docs/idosell_api_pim_research.md`. Najważniejsze fakty:
- Jeden `productId` = produkt ze WSZYSTKIMI wariantami rozmiarowymi (rozmiary to
  nie osobne ID). Warianty KOLORYSTYCZNE to osobne `productId` powiązane `seriesId`.
- Brak webhooków - synchronizacja przez polling.
- SKU/kod bywa pusty - nie polegać wyłącznie na kodzie do identyfikacji.

---

## 10. Trik na oficjalną dokumentację (readme.io renderowane JS-em)

Każda strona ma wersję markdown z pełnym OpenAPI: dopisz `.md` do URL, np.
`https://idosell.readme.io/reference/productsimagesput.md`.
Indeks wszystkich stron: `https://idosell.readme.io/llms.txt`.
(Zwykły fetch bywa blokowany - działa przez przeglądarkę / z User-Agent.)

---

## 11. Ceny - odczyt i zapis per rozmiar (zweryfikowane empirycznie 2026-06-18 na prod. 4278/8423)

Kontekst konta: 1 sklep (shopId 1), rozmiary = warianty wewnątrz produktu, ceny różne per rozmiar.

### 11.1. Odczyt - `GET /products/products?productIds=N` (query string!)
Zwraca pełny produkt; ceny w 3 miejscach:
- **top-level** `productRetailPrice` / `...Wholesale` / `...Minimal` / `...AutomaticCalculation` / `...Pos` / `productStrikethroughRetailPrice` / `...Wholesale` + `productSuggestedPrice` = wiersz "Wszystkie". Przy aktywnych cenach per rozmiar jest **POCHODNĄ rozmiarów** (np. retail = najniższy rozmiar) - NIE ustawia się go wprost (wartości wysłane na top-level są ignorowane, oprócz suggested).
- **`productSizesAttributes[]`** = warstwa per rozmiar (globalna): te same pola cenowe per size. Rozmiar dziedziczący cenę domyślną może nie mieć wpisu cenowego (tylko waga).
- **`productShopsAttributes[]`** = per sklep: ceny produktowe + `productPricesConfig` + `productSuggestedPrice` + `productShopSizesAttributes[]` (per sklep per rozmiar).
- `shopsMask` na produkcie = **1** (1 sklep). To POPRAWNE na REST. `shopsMask:0` z webowego importu (panel PIM > Import/aktualizacja) to BUG narzędzia BETA - przez webowy import ceny per rozmiar NIE zapisują się. **Ceny zapisuj przez REST, nie przez webowy import.**

### 11.2. Zapis per rozmiar - `PUT /products/products`
```json
{"params":{"settings":{"settingModificationType":"edit"},
 "products":[{"productId":4278,"priceChangeMode":"amount_set",
   "productSizes":[{"sizeId":"52","sizePanelName":"S",
     "productRetailPrice":89.90,"productStrikethroughRetailPrice":119.90}]}]}}
```
- Klucz zapisu: **`productSizes`** (NIE `productSizesAttributes` - to nazwa z odczytu). Rozmiar po `sizeId` (+`sizePanelName`). Produkt po `productId`/`productIndex`/codeExtern/codeProducer.
- `priceChangeMode`: `amount_set` | `amount_diff` | `percent_diff`.
- **CHIRURGICZNY**: podanie jednego rozmiaru zmienia TYLKO ten rozmiar (reszta nietknięta). Zmiana per-size propaguje do warstwy per-sklep automatycznie.

### 11.3. Mapa pól (co działa per rozmiar)
Settable per rozmiar (w `productSizes[]`):
- `productRetailPrice` (Detaliczna)
- `productWholesalePrice` (Hurtowa) - jej ustawienie przełącza `productPricesConfig` na `wholesale_notequals_retail`
- `productMinimalPrice` (Minimalna)
- `productAutomaticCalculationPrice` (Do obliczeń automatycznych - używane pod Allegro)
- `productStrikethroughRetailPrice` (Przekreślona detaliczna) - AKTYWUJE się samą wartością >0 (brak osobnej flagi mimo toggla "Przekreślona" w panelu); =0 czyści przecenę
- `productStrikethroughWholesalePrice` (Przekreślona hurtowa)

NIE per rozmiar:
- `productPosPrice` (POS/stacjonarna) - wymuszona = detaliczna (`pos_equals_retail`); inna wartość ignorowana, ląduje = retail
- `productSuggestedPrice` (Sugerowana) - poziom produktu/sklepu (nie ma w productSizesAttributes); ustaw w obiekcie produktu, propaguje do per-sklep dla wszystkich rozmiarów

### 11.4. PUŁAPKI ZAPISU (krytyczne, zweryfikowane)
- **`faults: []` ≠ "zapisało się to, co chciałem".** POS/suggested mają własne reguły i bywają ignorowane mimo pustych faults. ZAWSZE weryfikuj GET-em po zapisie i porównuj z zamiarem.
- **`0` = "nie zmieniaj", NIE "ustaw zero"** dla większości pól cenowych: wysłanie `productWholesalePrice`/`productMinimalPrice`/`productSuggestedPrice` = 0 NIE wyzeruje pola (zostaje poprzednia wartość). WYJĄTEK: `productStrikethroughRetailPrice` = 0 czyści przekreśloną. Konsekwencja: minimalnej i sugerowanej NIE da się wyczyścić przez API wysyłając 0 - trzeba panelu albo innego mechanizmu (do ustalenia). Rollback buduj z NIEZEROWYCH oryginalnych wartości.
- **Hurtowa rządzona przez `productPricesConfig`**: ustawienie hurtowej przełącza na `wholesale_notequals_retail`; aby wrócić do "hurtowa=detaliczna" wyślij `productShopsAttributes:[{"shopId":1,"productPricesConfig":"wholesale_equals_retail"}]` (hurtowa zacznie podążać za detaliczną).
- VAT: net = brutto/1.23 (2 miejsca). Pola `*Net` można podać lub zostawić systemowi.
- Omnibus (najniższa cena 30 dni) IdoSell śledzi automatycznie (węzeł `omnibus_price_retail` w eksporcie IOF).

### 11.5. Wzorzec bezpiecznego zapisu cen (OBOWIĄZKOWY, jak sekcja 8)
1. Backup pełnego GET produktu PRZED zapisem (lokalnie, per produkt).
2. PUT (`productSizes[]`).
3. Weryfikacja GET PO - porównaj KAŻDE pole z zamiarem (nie ufaj `faults`).
4. Rollback 1:1 z backupu - PUT oryginalnych (NIEZEROWYCH) wartości per rozmiar + ewentualnie `productPricesConfig`. Pamiętaj o pkt 11.4 (0 nie czyści).
5. Pierwszy test/rozwój: produkt z tagiem Archiwum (param 993 = wartość 2386) ORAZ **zerowym stanem**. UWAGA: sam tag Archiwum NIE ukrywa ze sklepu (`productIsVisible:y`, `avail_mgmt:stock`) - dopiero 0 szt. = niekupowalny = bezpieczny cel.

---

## 12. Flagi handlowe, menu, kategoria - proces outletowy (PUT /products/products, zweryfikowane 2026-06-18 na 4278/7047)

Tagi (parametr 993 "Product Tag" / eng "Outlet"): wartość **994 = "Outlet"**, **2386 = "Archiwum"**, 2412 = "Buty". Filtr produktów outletu: `productParameterIdsEnabled:[994]`.

### 12.1. Produkt wyróżniony/specjalny/promocja/przecena -> `productHotspotsZones` (per sklep)
```json
"productHotspotsZones":[{"productHotspotIsEnabled":true,"shopId":1,
  "productIsDistinguished":true,   // "Produkt wyróżniony"
  "productIsSpecial":false,        // "Produkt specjalny"
  "productIsPromotion":false,      // "Promocja"
  "productIsDiscount":false}]      // "Przecena"
```
UWAGA: top-level `productDistinguished`/`productSpecial` (z `promoteItemEnabled`) to CO INNEGO - liczy się `productHotspotsZones[].productIsDistinguished`. Panel: Marketing i SEO > Rabaty i promocje.

### 12.2. Przypisanie do menu (np. węzeł Outlet) -> `productMenuItems`
```json
"productMenuItems":[{"shopId":1,"menuId":1,
  "productMenuOperation":"add_product",
  "menuItemTextId":"Outlet\\Buty dziecięce"}]
```
- Operacje ZWERYFIKOWANE: **`add_product`** (dodaj do węzła), **`delete_product`** (usuń z węzła). Inne wartości (`delete`/`remove`/`remove_product`) są CICHO IGNOROWANE (faults:[], brak zmiany) - klasyczny wzorzec "nieznana wartość = no-op".
- Adresowanie po **`menuItemTextId`** (ścieżka tekstowa, np. `"Outlet\\Buty dziecięce"`) - NIE trzeba numerycznego ID. Chirurgiczne: rusza tylko wskazany węzeł, reszta przypisań produktu nietknięta.
- Odczyt obecnych przypisań: `productMenu[]` (menuItemId + menuItemDescriptionsLangData.menuItemTextId per lang).
- Priorytet w węźle: `productPriorityInMenuNodes` (numeryczny `productMenuNodeId` + `productPriority` + `productMenuTreeId`).
- Węzły Outlet (sklep 1): `"Outlet\\Buty dziecięce"` (=menuItemId 255, potwierdzony); wg panelu też Buty damskie/męskie, Kalosze, Kapcie, Sandały (dokładne textId do potwierdzenia per typ - czytaj `productMenu` produktu już tam przypisanego).
- Endpoint drzewa menu: `GET /menu/menu` istnieje, ale param sklepu do dobicia (shopId=1 zwracał faultCode 2 "Sklep nie istnieje"). Do samego przypisania NIEPOTRZEBNY - wystarczy `menuItemTextId`.

### 12.3. Kategoria -> `categoryId` / `categoryIdoSellId`
```json
"categoryId": 1214553949,        // kategoria własna
"categoryIdoSellId": 5962        // taksonomia IdoSell
```

### 12.4. Cały proces outletowy = JEDEN PUT /products/products
cena detaliczna + przekreślona (`productSizes[]`) + wyróżniony (`productHotspotsZones`) + menu Outlet (`productMenuItems`) + kategoria (`categoryId`) można złożyć w jedno wywołanie per produkt. Zawsze: backup -> PUT -> weryfikacja GET (nie ufaj faults) -> rollback gotowy.
