# -*- coding: utf-8 -*-
"""Porownanie ujecia glownego: profil vs 3/4 (oba pojedyncze, standaryzowane)
na realnych produktach ze sklepu. Material do decyzji single-profil vs kat.
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

# (productId, slot_profil, slot_3/4, etykieta)
ITEMS = [
    (7246, 1, 3, "Ameko dino"),
    (7272, 2, 1, "Ameko Lilac"),
]
CW, IMG = 300, 280
CARD_H = 280 + 60


def font(sz, b=False):
    try:
        return ImageFont.truetype("arialbd.ttf" if b else "arial.ttf", sz)
    except Exception:
        return ImageFont.load_default()


def std_card(data, label, bg=(255, 255, 255)):
    out = pipeline.process_bytes(data, {})
    c = Image.new("RGB", (CW, CARD_H), bg)
    th = out.convert("RGB"); th.thumbnail((IMG, IMG), Image.LANCZOS)
    c.paste(th, ((CW - th.width) // 2, 10))
    d = ImageDraw.Draw(c)
    d.text((12, IMG + 22), label, fill=(30, 40, 60), font=font(16, True))
    return c


def slot_data(imgs, slot):
    im = next((i for i in imgs if i["slot"] == slot), imgs[0])
    return ic.download_image(im["url"])


cols = []
for pid, sp, sa, lab in ITEMS:
    imgs = ic.get_product_images(pid)
    print(f"{pid} {lab}: profil slot {sp}, 3/4 slot {sa}")
    cols.append((std_card(slot_data(imgs, sp), f"{lab} - PROFIL"),
                 std_card(slot_data(imgs, sa), f"{lab} - 3/4 (kat)")))

gap = 16
W = len(cols) * (CW + gap) + gap
H = 2 * (CARD_H + gap) + 50
full = Image.new("RGB", (W, H), (247, 248, 250))
dd = ImageDraw.Draw(full)
dd.text((16, 10), "GLOWNE UJECIE: profil (gora) vs 3/4 kat (dol) - oba "
        "standaryzowane na kwadrat bialy", fill=(30, 34, 48), font=font(20, True))
for ci, (top, bot) in enumerate(cols):
    x = gap + ci * (CW + gap)
    full.paste(top, (x, 50))
    full.paste(bot, (x, 50 + CARD_H + gap))
out = OUT / "compare_angle.jpg"
full.save(out, "JPEG", quality=90)
print("zapisano:", out, full.size)
