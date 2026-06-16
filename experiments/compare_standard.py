# -*- coding: utf-8 -*-
"""Porownanie standaryzacji na jednym produkcie: oryginal vs warianty
docelowe (kwadrat 1600, rozne tla/cien). Material pogladowy do decyzji.

python compare_standard.py <productId> [slot]
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

pid = int(sys.argv[1])
slot = int(sys.argv[2]) if len(sys.argv) > 2 else 1

imgs = ic.get_product_images(pid)
im = next((i for i in imgs if i["slot"] == slot), imgs[0])
data = ic.download_image(im["url"])
orig = Image.open(BytesIO(data)).convert("RGB")
print(f"produkt {pid} slot {im['slot']}: oryginal {orig.size}")

# model RAZ -> alpha + src; reszta wariantow bez modelu (compose_from / kompozycja)
final_white_shadow, src_rgb, alpha, full_bleed = pipeline.process_bytes(
    data, {}, with_parts=True)


def square_on(bg_rgba, size=1600, pad=0.90):
    """Kwadrat z produktem na zadanym tle (RGBA) - przyciecie+skala jak pipeline."""
    rgba = src_rgb.convert("RGBA")
    rgba.putalpha(alpha)
    bbox = pipeline.object_bbox(rgba)
    rgba = rgba.crop(bbox)
    rgba = pipeline.whiten_neutral(rgba)
    w, h = rgba.size
    md = int(size * pad)
    r = min(md / w, md / h, 3.0)
    rgba = rgba.resize((int(w * r), int(h * r)), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), bg_rgba)
    ow, oh = rgba.size
    canvas.alpha_composite(rgba, ((size - ow) // 2, (size - oh) // 2))
    return canvas


white_no_shadow = pipeline.compose_from(src_rgb, alpha, {"shadow": False})
grey = square_on((242, 242, 242, 255)).convert("RGB")
transp = square_on((0, 0, 0, 0))  # RGBA - przezroczyste

# transparent na szachownicy (zeby bylo widac przezroczystosc)
checker = Image.new("RGB", (1600, 1600), (255, 255, 255))
d = ImageDraw.Draw(checker)
for y in range(0, 1600, 80):
    for x in range(0, 1600, 80):
        if (x // 80 + y // 80) % 2:
            d.rectangle([x, y, x + 80, y + 80], fill=(228, 230, 236))
checker.paste(transp, (0, 0), transp)

panels = [
    ("ORYGINAL (jak jest)", orig),
    ("Kwadrat BIALE + cien", final_white_shadow),
    ("Kwadrat BIALE bez cienia", white_no_shadow),
    ("Kwadrat SZARE 242", grey),
    ("Kwadrat PRZEZROCZYSTE", checker),
]

# strip pozioma, kazdy panel 520px, podpis nad
P = 520
PAD = 16
LBL = 34
strip = Image.new("RGB", (len(panels) * (P + PAD) + PAD,
                          P + LBL + 2 * PAD), (245, 246, 250))
dr = ImageDraw.Draw(strip)
try:
    font = ImageFont.truetype("arial.ttf", 18)
except Exception:
    font = ImageFont.load_default()
x = PAD
for label, img in panels:
    thumb = img.convert("RGB").copy()
    thumb.thumbnail((P, P), Image.LANCZOS)
    # tlo panelu szare zeby widac biale zdjecie
    cell = Image.new("RGB", (P, P), (255, 255, 255))
    cell.paste(thumb, ((P - thumb.width) // 2, (P - thumb.height) // 2))
    strip.paste(cell, (x, LBL + PAD))
    dr.text((x, PAD), label, fill=(30, 34, 48), font=font)
    dr.rectangle([x, LBL + PAD, x + P, LBL + PAD + P], outline=(210, 214, 224))
    x += P + PAD

out = OUT / f"compare_{pid}_{im['slot']}.jpg"
strip.save(out, "JPEG", quality=92)
print("zapisano:", out, strip.size)
