# -*- coding: utf-8 -*-
"""Roznica dwoch masek alfa - gdzie i ile pikseli przybylo/ubylo."""
from pathlib import Path

import numpy as np
from PIL import Image

BASE = Path(__file__).parent.parent / "samples" / "preseg"
a = np.asarray(Image.open(BASE / "4576_1_p0_baseline_alpha.png").convert("L"), float)
b = np.asarray(Image.open(BASE / "4576_1_p3_contrast18_dark_alpha.png").convert("L"), float)

base_on = a > 128
p3_on = b > 128
added = p3_on & ~base_on   # p3 ma produkt, baseline nie (odzyskane)
removed = base_on & ~p3_on  # baseline mial, p3 nie (stracone)

print(f"piksele produktu baseline: {base_on.sum():,}")
print(f"piksele produktu p3:       {p3_on.sum():,}")
print(f"ODZYSKANE przez p3 (+):    {added.sum():,}")
print(f"STRACONE przez p3 (-):     {removed.sum():,}")

# wizualizacja roznicy: zielony = odzyskane, czerwony = stracone
h, w = a.shape
vis = np.zeros((h, w, 3), np.uint8)
vis[base_on & p3_on] = (80, 80, 80)   # wspolne - szare
vis[added] = (0, 230, 0)              # odzyskane - zielone
vis[removed] = (230, 0, 0)            # stracone - czerwone
Image.fromarray(vis).save(BASE / "4576_1_diff_p0_p3.png")
print("Mapa roznicy -> 4576_1_diff_p0_p3.png")
