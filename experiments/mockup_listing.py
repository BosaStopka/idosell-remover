# -*- coding: utf-8 -*-
"""Makieta siatki sklepu: PRZED (zdjecia jak sa) vs PO (standaryzacja),
zeby pokazac roznice w kontekscie listingu - nie pojedynczego zdjecia.

Karty na jasnoszarym tle (jak sklep), zeby bylo widac, ktore zdjecia maja
biale/szare/przezroczyste tlo i jaka skale produktu.
"""
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).parent.parent))
import idosell_client as ic  # noqa: E402
import pipeline  # noqa: E402

OUT = Path(__file__).parent.parent / "samples" / "compare"
OUT.mkdir(parents=True, exist_ok=True)

# miks reprezentujacy balagan + czysta para Ameko Neo (navy + dino) jako
# bohaterowie "PO". Pick zrodlo: file=plik z input/ (Twoje zdjecia) ALBO
# q=text-search ALBO pid+slot (pewne pobranie po ID z katalogu).
INPUT = Path(__file__).parent.parent / "input"
PICKS = [
    {"label": "Xero (landscape biale)", "q": "Xero Scrambler Trail Low WP",
     "price": "599,00 zl"},
    {"label": "Evacare (szare 242)", "q": "Evacare Kaky", "price": "249,00 zl"},
    {"label": "Be Lenka (przezroczyste)", "q": "Be Lenka Synergy",
     "price": "419,00 zl"},
    # navy + dino: WYJATEK - gora = ujecie para/3-4, dol = profil, OBA po
    # naszej obrobce (porownanie ktory kat lepszy na glowne zdjecie).
    {"label": "Ameko Neo Navy", "top_file": "image-1781608289982.webp",
     "bottom_file": "image-1781608305661.webp",
     "name": "Ameko Barefoot Tenisowki Neo Navy", "price": "219,00 zl"},
    {"label": "Ameko Neo Dino", "top_file": "image-1781608310793.webp",
     "bottom_file": "dino_single.webp",
     "name": "Ameko Barefoot Tenisowki Neo Dino", "price": "219,00 zl"},
]

CARD_BG = (236, 238, 242)      # jasnoszare tlo karty (jak sklep)
PAGE_BG = (247, 248, 250)
CW, IMG = 300, 280             # szerokosc karty, obszar zdjecia
CARD_H = IMG + 120


def load_font(sz, bold=False):
    for name in (("arialbd.ttf" if bold else "arial.ttf"), "segoeui.ttf"):
        try:
            return ImageFont.truetype(name, sz)
        except Exception:
            pass
    return ImageFont.load_default()


def fit(img, box):
    """Wpasuj zdjecie w kwadrat box x box (zachowaj proporcje, wycentruj)."""
    im = img.convert("RGBA") if img.mode in ("RGBA", "LA", "P") else img.convert("RGB")
    if im.mode == "P":
        im = im.convert("RGBA")
    th = im.copy()
    th.thumbnail((box, box), Image.LANCZOS)
    cell = Image.new("RGBA", (box, box), (0, 0, 0, 0))
    cell.paste(th, ((box - th.width) // 2, (box - th.height) // 2),
               th if th.mode == "RGBA" else None)
    return cell


def card(img, price, name, bg=CARD_BG):
    c = Image.new("RGB", (CW, CARD_H), bg)
    d = ImageDraw.Draw(c)
    # obszar zdjecia - zdjecie na tle karty (widac biale/szare/przezr.)
    c.paste(fit(img, IMG), ((CW - IMG) // 2, 10),
            fit(img, IMG))
    y = IMG + 18
    # chip ceny
    d.rounded_rectangle([14, y, 110, y + 26], 6, fill=(255, 255, 255),
                        outline=(210, 214, 224))
    d.text((24, y + 4), price, fill=(40, 44, 60), font=load_font(15, True))
    # nazwa (2 linie)
    f = load_font(15, True)
    words, line, lines = name.split(), "", []
    for w in words:
        t = (line + " " + w).strip()
        if d.textlength(t, font=f) > CW - 28:
            lines.append(line); line = w
        else:
            line = t
    lines.append(line)
    for i, ln in enumerate(lines[:2]):
        d.text((14, y + 36 + i * 20), ln, fill=(30, 50, 80), font=f)
    return c


def grid_row(cards):
    gap = 18
    w = len(cards) * CW + (len(cards) + 1) * gap
    row = Image.new("RGB", (w, CARD_H + 2 * gap), PAGE_BG)
    x = gap
    for c in cards:
        row.paste(c, (x, gap))
        x += CW + gap
    return row


print("pobieram + obrabiam produkty...")
top_cards, bottom_cards = [], []
for pick in PICKS:
    price, name = pick["price"], pick.get("name")
    if pick.get("top_file"):  # navy/dino: gora=para/3-4, dol=profil, OBA obrobione
        top_img = pipeline.process_bytes((INPUT / pick["top_file"]).read_bytes(), {})
        bot_img = pipeline.process_bytes((INPUT / pick["bottom_file"]).read_bytes(), {})
        top_cards.append(card(top_img, price, name, (255, 255, 255)))
        bottom_cards.append(card(bot_img, price, name, (255, 255, 255)))
        print(f"  {name[:34]:34} ({pick['label']}) para/3-4 + profil")
        continue
    # reszta: text-search, gora=oryginal (jak jest), dol=po obrobce
    res = ic.search_active("text", pick["q"], page=0, limit=1)
    if not res["products"] or not res["products"][0]["images"]:
        print("  brak:", pick["q"]); continue
    p = res["products"][0]
    data, name = ic.download_image(p["images"][0]["url"]), p["name"]
    print(f"  {name[:34]:34} ({pick['label']})")
    # karta biala (sklep nie ma szarego tla) - gora pokazuje oryginal zdjecia
    top_cards.append(card(Image.open(BytesIO(data)), price, name, (255, 255, 255)))
    bottom_cards.append(card(pipeline.process_bytes(data, {}), price, name,
                             (255, 255, 255)))

rows = [
    ("GORA - jak jest teraz (navy/dino: ujecie para / 3-4)", (200, 60, 60),
     grid_row(top_cards)),
    ("DOL - po standaryzacji na jasnej karcie (navy/dino: profil)",
     (20, 150, 110), grid_row(bottom_cards)),
]
W = max(r[2].width for r in rows)
TH = 46
full = Image.new("RGB", (W, sum(TH + r[2].height for r in rows) + 20), PAGE_BG)
dd = ImageDraw.Draw(full)
tf = load_font(24, True)
y = 8
for title, color, row in rows:
    dd.text((20, y), title, fill=color, font=tf)
    full.paste(row, (0, y + TH - 10))
    y += TH + row.height
out = OUT / "mockup_listing.jpg"
full.save(out, "JPEG", quality=90)
print("zapisano:", out, full.size)
