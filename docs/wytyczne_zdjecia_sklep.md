# Standaryzacja zdjęć produktów - brief do decyzji z właścicielami

Dokument roboczy do rozmowy o docelowym wyglądzie zdjęć w sklepie bosastopka.pl
przed masową obróbką ~2300 produktów. Stan: do decyzji. Data: 2026-06-16.

---

## 1. Problem: katalog jest dziś niespójny

Zbadane zdjęcia (próbka z żywego sklepu przez API):

| Produkt | Format | Tło | Rozmiar |
|---|---|---|---|
| Evacare Kaky | JPG | **szare (242,242,242)** wypalone w pliku | 1200×1200 |
| MTNG Apolo | JPG | **białe (255)** | 1600×1600 |
| Be Lenka (4767) | JPG | białe | 1200×900 (nie kwadrat) |
| Tikki | JPG | prawie białe (247) | 900×900 |
| Be Lenka (8452) | PNG | **przezroczyste** | różne |

Czyli w katalogu mamy jednocześnie: **białe, szare, prawie-białe i przezroczyste**
tła, do tego **różne rozmiary i proporcje** (kwadrat, prostokąt). Klient
przewijający sklep widzi tę niespójność - to obniża odbiór "premium".

**Cel:** wybrać JEDEN standard i sprowadzić do niego cały katalog.
Backup oryginałów (dysk D) i rollback mamy, więc operacja jest odwracalna.

### Rozszerzony przegląd (kolejne realne przykłady z API)

| ID | Produkt | Tło pliku | Rozmiar | Uwaga |
|---|---|---|---|---|
| 8522/8523/8524 | MTNG Evacare (Negro/Kaky) | **szare 242** | 1200×1200 | cała linia Evacare na szarym |
| 7083 | MTNG Apolo | białe 255 | **1600×1600** | nasza obróbka (kwadrat) |
| 7388 | Xero Z-Trail | białe 255 | **1540×1000** | białe, ale **nie kwadrat** |
| 4767 | Be Lenka | białe 255 | 1200×900 | nie kwadrat |
| 4772 | Tikki | prawie białe 247 | 900×900 | małe |
| 8452 | Be Lenka | **przezroczyste PNG** | różne | już wycięte |

Dwa dodatkowe wnioski z przeglądu:
- **Niespójność bywa W OBRĘBIE jednej linii i jednego sklepu** - ten sam model
  Evacare ma szare tło (242), a podobne sneakersy/Xero mają białe. To nie jest
  "jeden dostawca = jedno tło"; bałagan jest wymieszany.
- **Zdjęcia lifestyle/"fashion"** (np. dziecko/osoba w butach na scenie) mają
  kolorowy narożnik - automat poprawnie je wykrywa i proponuje "Zostaw"
  (nie ruszamy tła). Te zostają jako uzupełnienie galerii.

---

## 2. Dlaczego to ważne (poza estetyką)

- **Spójność = profesjonalny odbiór** i wyższa konwersja - sklep wygląda jak
  jedna przemyślana marka, nie zlepek zdjęć od różnych dostawców.
- **Allegro** (te same produkty, ~1800 ofert): główne zdjęcie na Allegro
  powinno być na **białym/neutralnym tle** (wymóg jakości ofert). Jeśli
  ustandaryzujemy pod kątem Allegro, **jedno zdjęcie obsłuży oba kanały** -
  inaczej trzeba by robić osobną wersję na Allegro (podwójna praca).
- **Szybkość sklepu / SEO** - lżejsze pliki = szybsze ładowanie = lepsze
  pozycje i Core Web Vitals.

---

## 2b. Druga warstwa problemu: PROPORCJA (kwadrat vs prostokąt)

Niespójność to nie tylko tło. Przykład **całej linii Xero**: zdjęcia są
**białe**, ale w proporcji **landscape ~1.54:1** (np. 1540×1000, 675×438) -
**NIE kwadrat**. To gryzie nawet przy białym tle:

- Siatka produktów i miniaturki w sklepie to **kwadratowe kontenery**.
- Landscape w kwadratowym slocie → sklep **dokłada pasy** (but mały, luki
  góra/dół) albo **przycina** (ucina kawałki).
- Efekt: na sąsiednich kafelkach buty mają **różną skalę** - kwadrat (Evacare)
  wypełnia kafelek, landscape (Xero) siedzi mały z lukami. Wygląda
  przypadkowo, mimo że oba białe.

**Wniosek:** standaryzujemy DWIE rzeczy naraz - tło ORAZ proporcję/skalę.

### Specyfikacja techniczna docelowego zdjęcia
- **Kadr: kwadrat 1:1, 1600×1600 px** (każde zdjęcie identyczny kontener).
- **Produkt: wyśrodkowany, wypełnia ~88-90%** szerokości/wysokości - dzięki
  temu na każdym kafelku but ma **tę samą skalę**.
- **Tło:** wg decyzji z pkt 3 (rekomendacja: białe).
- **Format:** JPG jakość 95 (małe pliki) - chyba że wybierzemy przezroczyste
  (wtedy PNG).
- **Cień:** wg decyzji z pkt 4.
- Pipeline robi to automatycznie: przycina do produktu → kwadrat 1600 →
  skala ~90%. Po przerobieniu Xero/Evacare/Be Lenka = ten sam kwadrat, ta
  sama skala, to samo tło → siatka jak jeden spójny katalog.

---

## 3. Decyzja główna: jakie TŁO

### A) Czyste białe (#FFFFFF) - REKOMENDOWANE
- **Plusy:** standard e-commerce (Zalando, duże sklepy); zgodne z Allegro
  (jedno zdjęcie na sklep + marketplace); ponadczasowe, niezależne od motywu
  sklepu; najmniejsze pliki (JPG); pipeline już to robi i jest sprawdzony.
- **Minusy:** jeśli sklep wyświetla zdjęcia na **szarym "stole"** (karta/tło
  strony jest szare), białe zdjęcie da delikatną "ramkę/pudełko". Rozwiązanie:
  ujednolicić też tło strony/karty na białe lub bardzo jasne (drobna zmiana
  szablonu) - wtedy biel wtapia się idealnie.

### B) Szare dopasowane do dzisiejszego stylu (~242)
- **Plusy:** od razu spójne z tą częścią zdjęć, które już są szare;
  miękki, "butikowy" klimat.
- **Minusy:** **przywiązuje zdjęcia do dzisiejszego motywu** - zmiana
  szablonu/odcienia szarości w przyszłości = znowu niespójność i ponowna
  obróbka; **niezgodne z Allegro** (marketplace chce bieli) → osobna wersja
  na Allegro; ciemniejsze tło lekko "przygasza" produkt.

### C) Przezroczyste PNG (produkt wycięty, bez tła)
- **Plusy:** produkt wkomponuje się w **dowolne** tło sklepu (szare, białe,
  baner, tryb ciemny) - maksymalna elastyczność na stronie; nowoczesne.
- **Minusy:** **Allegro/marketplace zwykle NIE przyjmują przezroczystości**
  (chcą bieli) → osobna wersja na Allegro; **większe pliki** (PNG); cień
  trzeba **wypalić jako półprzezroczysty** w pliku; przy podmianie tła sklepu
  na ciemne, wypalony jasny cień może odstawać.

> **Rekomendacja doradcy:** **A) białe** - bo jako jedyne obsługuje
> równocześnie sklep i Allegro jednym plikiem, jest standardem branży i nie
> starzeje się wraz z motywem. Jeśli właściciele chcą "miękki" wygląd jak
> teraz, najczystsze rozwiązanie to **białe zdjęcia + jasne tło strony** -
> efekt szarego "stołu" osiągamy tłem strony, a same pliki zostają białe i
> uniwersalne (i Allegro-zgodne).

---

## 4. Decyzje towarzyszące

### Cień pod produktem
- **Miękki cień kontaktowy** (mamy) - dodaje głębi, wygląda premium; na bieli
  i na Allegro akceptowalny.
- **Bez cienia** (płasko) - bardziej "katalogowo/sterylnie", łatwiej później
  podmienić tło.
- **Zachowaj realny cień z oryginału** - gdy fotograf zrobił ładny studyjny
  cień, zostawiamy go; gdy nie ma, brak (ten tryb już mamy).
- *Rekomendacja:* subtelny cień zostawić - podnosi odbiór, nie szkodzi Allegro.

### Format pliku
- Białe/szare → **JPG** (jakość 95, małe pliki). Przezroczyste → PNG (duże).
- *Rekomendacja:* JPG, jeśli wybierzemy białe/szare.

### Rozmiar i proporcje
- *Rekomendacja:* **kwadrat 1600×1600**, produkt wypełnia ~85-90% kadru,
  wyśrodkowany. Kwadrat = równe, spójne kafelki w siatce i zgodność z Allegro.
  (Dziś część zdjęć jest prostokątna 1200×900 itp. - to też źródło bałaganu.)

### Makro zdjęć w panelu IdoSell - DO SPRAWDZENIA PRZEZ ADMINA
- IdoSell może mieć włączone własne **makro** przetwarzające wgrywane zdjęcia
  (skalowanie, dodanie tła, znak wodny, ramka). My wgrywamy "surowo"
  (apply_macro = false), ale jeśli panel ma globalne makro, **może zmienić
  nasz efekt po wgraniu**. Trzeba potwierdzić ustawienia zdjęć w panelu
  (Administracja → ustawienia zdjęć/galerii) zanim pójdzie masówka.

### Tło samej STRONY/karty produktu (pytanie do właścicieli)
- Kluczowa interakcja: tło PLIKU vs tło STRONY. Najlepiej, gdy oba grają.
  Czysty profesjonalny zestaw: **białe pliki + białe/bardzo jasne tło strony**.
  Jeśli strona ma zostać szara - patrz opcja B lub C, albo białe pliki na
  jasnym tle (rekomendacja w pkt 3).

---

## 5. Zakres masowej obróbki
- **Wszystko** → 100% spójność (przerabiamy też już-białe pod jeden standard
  rozmiaru/cienia). Więcej zapisów, ale katalog idealnie równy.
- **Tylko niezgodne** → zostawiamy zdjęcia już w docelowym formacie, ruszamy
  szare/małe/przezroczyste/prostokątne. Mniej pracy.
- *Rekomendacja:* skoro problemem jest właśnie niespójność - dążyć do jednego
  standardu; zdjęcia już zgodne z wyborem (np. białe 1600² jeśli wybierzemy
  białe) pomijać automatycznie.

---

## 6. Co gwarantujemy technicznie (dla spokoju właścicieli)
- **Backup oryginałów** w pełnej rozdzielczości na osobnym dysku przed każdą
  zmianą - nic nie ginie.
- **Rollback** jednym kliknięciem per produkt.
- **Weryfikacja po zapisie** (sprawdzamy, czy galeria w sklepie zgadza się
  z tym, co wysłaliśmy) + dziennik każdej operacji.
- **Pierwszy zapis tylko na produkcie testowym/ukrytym** - zatwierdzacie efekt
  zanim ruszy masówka.
- Ręczny **edytor maski** i wyrównanie barw - poprawki tam, gdzie automat
  nie da rady.

---

## 7. Pytania do ustalenia z właścicielami (checklist)
1. **Tło docelowe:** białe / szare / przezroczyste? (rekomendacja: białe)
2. **Tło strony/karty produktu:** zostaje szare czy ujednolicamy na jasne?
3. **Cień:** subtelny zostaje czy płasko bez cienia?
4. **Rozmiar:** akceptacja kwadratu 1600×1600, produkt ~90%?
5. **Allegro:** czy te zdjęcia mają iść też na Allegro? (jeśli tak - mocny
   argument za bielą)
6. **Makro w panelu IdoSell:** admin potwierdza ustawienia zdjęć (czy panel
   sam nie przerabia wgrywanych plików).
7. **Zakres:** cały katalog czy tylko niezgodne?
8. **Zdjęcia "fashion" (np. dziecko w butach):** zostawiamy jak są (bez
   obróbki tła) - potwierdzić.

Po decyzjach: zrobię **próbki tego samego produktu w wybranym wariancie**
(białe / szare / przezroczyste + wariant cienia), wrzucimy na produkt
testowy, obejrzycie na żywo w sklepie i dopniemy detale, zanim ruszy masówka.
