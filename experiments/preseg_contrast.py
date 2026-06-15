# -*- coding: utf-8 -*-
"""Eksperyment: podbicie kontrastu WEJSCIA przed segmentacja, zeby model
zlapal niskokontrastowe fragmenty (szary element na jasnym tle, 4576_1).

Kluczowe: podbity obraz sluzy TYLKO do policzenia maski (alfy); finalny
obraz skladamy z ORYGINALNYCH pikseli - kolory pozostaja wierne.

Read-only wzgledem sklepu. Wynik do samples/preseg/.
"""
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageEnhance

sys.path.insert(0, str(Path(__file__).parent.parent))
import pipeline  # noqa: E402

BASE = Path(__file__).parent.parent
OUT = BASE / "samples" / "preseg"
OUT.mkdir(parents=True, exist_ok=True)

SOURCES = {"4576_1": "10db6ae55b24.jpg"}


def load_src(data: bytes) -> Image.Image:
    src = Image.open(BytesIO(data)).convert("RGB")
    if max(src.size) > pipeline.MAX_INPUT_PX:
        r = pipeline.MAX_INPUT_PX / max(src.size)
        src = src.resize((int(src.size[0] * r), int(src.size[1] * r)),
                         Image.LANCZOS)
    return src


def mask_from(img: Image.Image) -> Image.Image:
    """Alfa policzona z (ewentualnie podbitego) obrazu - z retry na RAM."""
    import gc
    import time

    from rembg import remove
    last = None
    for delay in (0, 5, 15, 30):
        if delay:
            gc.collect()
            time.sleep(delay)
        try:
            out = remove(img, session=pipeline.get_session()).convert("RGBA")
            return out.split()[3]
        except Exception as e:
            last = e
            if "bad allocation" not in str(e):
                raise
            print(f"    bad_alloc, retry za {delay}s...", flush=True)
    raise RuntimeError(f"Za malo RAM: {last}")


def preprocess(src, contrast=1.0, brightness=1.0, color=1.0):
    img = src
    if contrast != 1.0:
        img = ImageEnhance.Contrast(img).enhance(contrast)
    if brightness != 1.0:
        img = ImageEnhance.Brightness(img).enhance(brightness)
    if color != 1.0:
        img = ImageEnhance.Color(img).enhance(color)
    return img


# warianty podbicia WEJSCIA (do maski)
VARIANTS = {
    "p0_baseline": dict(),
    "p1_contrast14": dict(contrast=1.4),
    "p2_contrast14_dark": dict(contrast=1.4, brightness=0.85),
    "p3_contrast18_dark": dict(contrast=1.8, brightness=0.8),
}

opt = {**pipeline.DEFAULTS}

# jeden wariant na uruchomienie procesu (czysta pamiec za kazdym razem):
#   python preseg_contrast.py p1_contrast14
only = sys.argv[1] if len(sys.argv) > 1 else None
todo = {only: VARIANTS[only]} if only else VARIANTS

for name, fname in SOURCES.items():
    src = load_src((BASE / "originals" / fname).read_bytes())
    for vname, params in todo.items():
        print(f"[{name}] {vname} {params}...", flush=True)
        boosted = preprocess(src, **params)
        alpha = mask_from(boosted)
        alpha.save(OUT / f"{name}_{vname}_alpha.png")
        rgba = src.convert("RGBA")
        rgba.putalpha(alpha)
        refined = pipeline.refine_edges(rgba, float(opt["edge_feather"]))
        pipeline.compose(refined, opt).save(
            OUT / f"{name}_{vname}.jpg", "JPEG", quality=95)
        print(f"  -> {name}_{vname}.jpg", flush=True)

print("Gotowe:", OUT)
