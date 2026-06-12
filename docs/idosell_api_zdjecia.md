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
Filtrowanie też po: indeksach/kodach (productIndexes, searchByCodes itd. - do potwierdzenia w kroku 0).

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
