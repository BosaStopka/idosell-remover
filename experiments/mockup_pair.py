# -*- coding: utf-8 -*-
"""Makieta siatki: ujecie POJEDYNCZE (3/4, ze sklepu) vs PARA (Twoje zdjecia
z folderu input/). Standaryzowane na kwadrat bialy, w ukladzie kart sklepu.

Zdjecia pary wrzuc do: input/  (dowolne JPG/PNG). Skrypt je przerobi i
zestawi z pojedynczymi ze sklepu w jednej siatce.

python mockup_pair.py
"""
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
import idosell_client as ic  # noqa: E402
import pipeline  # noqa: E402

OUT = ROOT / "samples" / "compare"
OUT.mkdir(parents=True, exist_ok=True)
INPUT = ROOT / "input"

# pojedyncze 3/4 ze sklepu: (productId, slot_3/4, etykieta, cena)
SINGLES = [
    (7246, 3, "Ameko dino", "219,00 zl"),
    (7272, 1, "Ameko Lilac", "219,00 zl"),
]

CW, IMG = 300, 280
CARD_H = IMG + 90
CARD_BG = (255, 255, 255)
PAGE_BG = (247, 248, 250)


def font(sz, b=False):
    try:
        return ImageFont.truetype("arialbd.ttf" if b else "arial.ttf", sz)
    except Exception:
        return ImageFont.load_default()


def card(std_img, name, price):
    c = Image.new("RGB", (CW, CARD_H), CARD_BG)
    th = std_img.convert("RGB"); th.thumbnail((IMG, IMG), Image.LANCZOS)
    c.paste(th, ((CW - th.width) // 2, 8))
    d = ImageDraw.Draw(c)
    d.rounded_rectangle([12, IMG + 14, 104, IMG + 40], 6, fill=(245, 246, 250),
                        outline=(214, 218, 228))
    d.text((20, IMG + 18), price, fill=(40, 44, 60), font=font(14, True))
    d.text((12, IMG + 48), name[:30], fill=(30, 50, 80), font=font(14, True))
    return c


def grid_row(cards):
    gap = 16
    w = max(1, len(cards)) * (CW + gap) + gap
    row = Image.new("RGB", (w, CARD_H + gap), PAGE_BG)
    x = gap
    for c in cards:
        row.paste(c, (x, 0)); x += CW + gap
    return row


single_cards = []
for pid, slot, label, price in SINGLES:
    imgs = ic.get_product_images(pid)
    im = next((i for i in imgs if i["slot"] == slot), imgs[0])
    std = pipeline.process_bytes(ic.download_image(im["url"]), {})
    single_cards.append(card(std, label + " (3/4)", price))
    print(f"single: {pid} {label} slot {slot}")

pair_files = sorted([p for p in INPUT.glob("*")
                     if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")])
pair_cards = []
for f in pair_files:
    std = pipeline.process_bytes(f.read_bytes(), {})
    pair_cards.append(card(std, f.stem[:26] + " (para)", ""))
    print("para:", f.name)

rows = [("POJEDYNCZY - 3/4 kat (ze sklepu, standaryzowane)", (20, 120, 170),
         grid_row(single_cards))]
if pair_cards:
    rows.append(("PARA - Twoje zdjecia (standaryzowane)", (20, 150, 110),
                 grid_row(pair_cards)))
else:
    print("\n>>> Brak zdjec w input/ - wrzuc pliki pary do:", INPUT)

W = max(r[2].width for r in rows)
TH = 44
full = Image.new("RGB", (W, sum(TH + r[2].height for r in rows) + 16), PAGE_BG)
dd = ImageDraw.Draw(full)
y = 8
for title, color, row in rows:
    dd.text((16, y), title, fill=color, font=font(22, True))
    full.paste(row, (0, y + TH - 8))
    y += TH + row.height
out = OUT / "mockup_pair.jpg"
full.save(out, "JPEG", quality=90)
print("zapisano:", out, full.size)
