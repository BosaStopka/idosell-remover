# -*- coding: utf-8 -*-
"""Kontaktowka galerii produktu - male miniatury z numerami slotow,
zeby wybrac ktory slot to pojedynczy profil, a ktory para.

python contact_sheet.py <productId|fraza>
"""
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).parent.parent))
import idosell_client as ic  # noqa: E402

OUT = Path(__file__).parent.parent / "samples" / "compare"
OUT.mkdir(parents=True, exist_ok=True)
arg = " ".join(sys.argv[1:])

if arg.isdigit():
    imgs = ic.get_product_images(int(arg))
    pid = arg
    name = arg
else:
    res = ic.search_active("text", arg, page=0, limit=1)
    p = res["products"][0]
    imgs = p["images"]
    pid = p["id"]
    name = p["name"]
print(f"{pid} {name}: {len(imgs)} zdjec")

T = 240
cols = min(5, len(imgs))
rows = (len(imgs) + cols - 1) // cols
sheet = Image.new("RGB", (cols * (T + 10) + 10, rows * (T + 28) + 10),
                  (245, 246, 250))
d = ImageDraw.Draw(sheet)
try:
    f = ImageFont.truetype("arialbd.ttf", 18)
except Exception:
    f = ImageFont.load_default()
for i, im in enumerate(imgs):
    data = ic.download_image(im["medium"] or im["url"])
    t = Image.open(BytesIO(data))
    if t.mode in ("RGBA", "LA", "P"):
        t = t.convert("RGBA")
        bg = Image.new("RGBA", t.size, (255, 255, 255, 255))
        bg.alpha_composite(t)
        t = bg
    t = t.convert("RGB")
    t.thumbnail((T, T), Image.LANCZOS)
    cell = Image.new("RGB", (T, T), (255, 255, 255))
    cell.paste(t, ((T - t.width) // 2, (T - t.height) // 2))
    x = 10 + (i % cols) * (T + 10)
    y = 10 + (i // cols) * (T + 28)
    sheet.paste(cell, (x, y))
    d.text((x + 4, y + T + 4), f"slot {im['slot']}", fill=(30, 34, 48), font=f)
out = OUT / f"contact_{pid}.jpg"
sheet.save(out, "JPEG", quality=88)
print("zapisano:", out)
