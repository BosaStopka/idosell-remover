# IdoSell Admin API - zdjęcia produktów (ściąga)

Źródło: idosell.readme.io (OpenAPI v8.1, pobrane 2026-06-12).
Baza: `https://www.bosastopka.pl/api/admin/v8`
Auth: nagłówek `X-API-KEY: <klucz>`

## 1. Odczyt zdjęć produktu

`POST /products/products/search`

Body (minimalne):
```json
{
  "params": {
    "returnProducts": "active",
    "returnElements": ["code", "pictures", "pictures_count"],
    "productParams": { "productIds": [9911] },
    "resultsPage": 0,
    "resultsLimit": 100
  }
}
```
### Filtrowanie (ZWERYFIKOWANE empirycznie, krok 0 - 2026-06-12)

- **Po ID**: `"productParams": [{"productId": 334}]` - lista obiektów!
  Wariant `"productParams": {"productIds": [...]}` jest po cichu IGNOROWANY
  i zwraca wszystkie produkty.
- **Po kodzie/fragmencie kodu**: `"containsCodePart": "636401"` (szuka też
  w kodzie producenta).
- **Wykluczenie tagu Archiwum**: `"productParametersParams":
  [{"productParameterIds": {"productParameterIdsDisabled": [2386]}}]`
  (2386 = wartość "Archiwum" parametru Product Tag, parameterId 993;
  id konfigurowalne w idosell_config.json jako archive_tag_value_id).
  Filtrowanie po NAZWACH parametru/wartości jest niewiarygodne (zwraca
  produkty z jakimkolwiek Product Tag) - używać wyłącznie ID.
- **`returnProducts`**: "active" / "deleted" (= archiwum IdoSell) /
  "in_trash". UWAGA: przy "deleted" filtry serwera NIE działają
  (by id zwraca 0 dla istniejących, containsCodePart jest ignorowany
  i zwraca wszystko) - trzeba przeglądać strony i filtrować po stronie
  klienta (robi to idosell_client._find_deleted).
- Pusty wynik = HTTP 207 + `errors.faultCode 2` ("zwrócono pusty wynik"),
  nie traktować jako błąd.
- `lang_data` w returnElements daje productName (pol) + opisy (ciężkie).
- Paginacja: `resultsNumberAll` (produkty), `resultsNumberPage` (LICZBA
  STRON przy danym limicie), sterowanie resultsPage/resultsLimit.

Stan sklepu (2026-06-12): 4032 active (1703 z tagiem Archiwum, 2329 bez),
769 deleted, 0 in_trash. Zakres skanu = active bez tagu = 2329.

Odpowiedź - per produkt tablica `productImages`:
- `productImageId` (string) - ID zdjęcia (potrzebne do delete)
- `productImageLargeUrl` / `productImageMediumUrl` / `productImageSmallUrl`
- `productImageLargeUrlSecond` / `...MediumUrlSecond` / `...SmallUrlSecond`
- `productImageWidth`, `productImageHeight`, `productImageSize`
- oraz `productImagesCount`

## 2. Dodawanie / edycja zdjęć

`PUT /products/images`

```json
{
  "params": {
    "productsImagesSettings": {
      "productsImagesSourceType": "base64",   // albo "url"
      "productsImagesApplyMacro": false        // true = IdoSell skaluje/przetwarza
    },
    "productsImages": [{
      "productIdent": {
        "productIdentType": "id",              // id | index | codeExtern | codeProducer
        "identValue": "9911"
      },
      "shopId": 1,
      "otherShopsForPic": [],                  // puste/brak = wszystkie sklepy
      "productImages": [{
        "productImageSource": "<base64 albo URL>",
        "productImageNumber": 1,               // numer slotu zdjęcia
        "productImagePriority": 1,             // kolejność
        "deleteProductImage": false            // true = usuń to zdjęcie (slot)
      }],
      "productIcons": [{                       // opcjonalnie ikony
        "productIconSource": "<base64/URL>",
        "productIconType": "shop",             // shop | auction | group
        "deleteProductIcon": false
      }]
    }]
  }
}
```

Uwagi:
- Edycja per slot (`productImageNumber`) - NIE podmienia całej galerii naraz;
  usuwanie konkretnego slotu przez `deleteProductImage: true`.
- `productsImagesApplyMacro` - czy IdoSell ma przetwarzać zdjęcia swoim makrem;
  dla naszych gotowych 1200x1200 prawdopodobnie `false` (zweryfikować z
  ustawieniami panelu).
- Limit wymiarów: 4000x4000 px.
- Statusy: 200 OK, 207 Multi-Status (część się nie udała!), 429 rate limit.

## 3. Kasowanie zdjęć

`POST /products/images/delete`

```json
{
  "params": [{
    "productId": 9911,
    "shopId": 1,
    "productImagesId": ["<productImageId z search>"],
    "deleteAll": false
  }]
}
```

## 4. Wersjonowanie i limity

- Aktualna wersja API: v8 (klient npm używał v3/v5 - stare; my celujemy w v8)
- 429 = Too many requests - klient musi mieć throttling + retry z backoff
- 207 Multi-Status wymaga sprawdzenia odpowiedzi per element, nie tylko HTTP code

## 5. Trik na dokumentację (readme.io renderowane JS-em)

Każda strona ma wersję markdown z pełnym OpenAPI: dopisać `.md` do URL, np.
`https://idosell.readme.io/reference/productsimagesput.md`
Indeks wszystkich stron: `https://idosell.readme.io/llms.txt`
(zwykły fetch bywa blokowany - działa przez przeglądarkę)
