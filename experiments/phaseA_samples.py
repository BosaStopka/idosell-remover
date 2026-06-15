# -*- coding: utf-8 -*-
"""Faza A: probki scalonego pipeline (object_bbox, full-bleed/cover,
whiten_neutral, nowy cien, mirror) + zachowany mask_contrast.

Pobiera zdjecie ze sklepu (read-only) i przepuszcza nowym pipeline.
Jeden obraz na uruchomienie (RAM):
  python phaseA_samples.py <productId> <slot> [mirror]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import idosell_client as ic  # noqa: E402
import pipeline  # noqa: E402

BASE = Path(__file__).parent.parent
OUT = BASE / "samples" / "phaseA"
OUT.mkdir(parents=True, exist_ok=True)

pid = int(sys.argv[1])
slot = int(sys.argv[2])
mirror = len(sys.argv) > 3 and sys.argv[3] == "mirror"

images = ic.get_product_images(pid)
img = next((i for i in images if i["slot"] == slot), images[slot - 1])
data = ic.download_image(img["url"])

# oryginal (wejscie) do porownania
(OUT / f"{pid}_{slot}_orig.jpg").write_bytes(data)
out = pipeline.process_bytes(data, {"mirror": mirror})
suffix = "_mirror" if mirror else "_new"
out.save(OUT / f"{pid}_{slot}{suffix}.jpg", "JPEG", quality=95)
print(f"  {pid}_{slot}: orig {img['width']}x{img['height']} -> {out.size} mirror={mirror}")
