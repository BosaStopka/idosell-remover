# -*- coding: utf-8 -*-
"""Eksperyment: warianty obrobki maski dla zdjec gdzie model ucina
niskokontrastowe fragmenty produktu (4576_1 - szary element przy piecie).

Read-only wzgledem sklepu: czyta surowe oryginaly z originals/, zapisuje
warianty do samples/ do wzrokowego porownania. Model uruchamiany RAZ,
warianty roznia sie tylko obrobka alfy (refine_edges).
"""
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageFilter

sys.path.insert(0, str(Path(__file__).parent.parent))
import pipeline  # noqa: E402

BASE = Path(__file__).parent.parent
OUT = BASE / "samples" / "mask"
OUT.mkdir(parents=True, exist_ok=True)

# surowe zrodla do testu (hex = job originals); 10db... = zrodlo 4576_1
SOURCES = {
    "4576_1": "10db6ae55b24.jpg",
}


def alpha_to_rgba(data: bytes) -> Image.Image:
    import gc
    import time

    from rembg import remove
    src = Image.open(BytesIO(data)).convert("RGB")
    if max(src.size) > pipeline.MAX_INPUT_PX:
        r = pipeline.MAX_INPUT_PX / max(src.size)
        src = src.resize((int(src.size[0] * r), int(src.size[1] * r)),
                         Image.LANCZOS)
    last = None
    for delay in (0, 5, 15, 30):
        if delay:
            gc.collect()
            time.sleep(delay)
        try:
            return remove(src, session=pipeline.get_session()).convert("RGBA")
        except Exception as e:
            last = e
            if "bad allocation" not in str(e):
                raise
            print(f"  bad_alloc, retry za {delay}s...", flush=True)
    raise RuntimeError(f"Za malo RAM: {last}")


# --- warianty refine_edges ---

def refine_current(rgba, feather=1.0):
    """Obecna wersja (baseline)."""
    return pipeline.refine_edges(rgba, feather)


def refine_soft(rgba, feather=1.0):
    """Lagodniejsza: bez erozji MinFilter, nizszy prog odciecia (<20),
    domkniecie maski (close) zeby zasypac male dziury/wciecia."""
    r, g, b, a = rgba.split()
    # domkniecie: dylatacja + erozja - laczy cienkie/poszarpane fragmenty
    a = a.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))
    if feather > 0:
        a = a.filter(ImageFilter.GaussianBlur(min(feather, 0.8)))
    # lagodniejsza krzywa: trzymaj slabe-ale-realne piksele (>=20)
    a = a.point(lambda v: 0 if v < 20 else 255 if v > 200 else
                int((v - 20) * 255 / 180))
    return Image.merge("RGBA", (r, g, b, a))


def refine_inclusive(rgba, feather=1.0):
    """Najbardziej zachowawcza wzgledem produktu: domkniecie + lekka
    dylatacja netto (MaxFilter raz wiecej), prog <12."""
    r, g, b, a = rgba.split()
    a = a.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))
    a = a.filter(ImageFilter.MaxFilter(3))  # netto +1px na produkt
    if feather > 0:
        a = a.filter(ImageFilter.GaussianBlur(min(feather, 0.8)))
    a = a.point(lambda v: 0 if v < 12 else 255 if v > 200 else
                int((v - 12) * 255 / 188))
    return Image.merge("RGBA", (r, g, b, a))


VARIANTS = {
    "0_current": refine_current,
    "1_soft": refine_soft,
    "2_inclusive": refine_inclusive,
}

opt = {**pipeline.DEFAULTS}

for name, fname in SOURCES.items():
    data = (BASE / "originals" / fname).read_bytes()
    print(f"[{name}] model...", flush=True)
    rgba0 = alpha_to_rgba(data)
    # podglad surowej maski (przed nasza obrobka)
    rgba0.split()[3].save(OUT / f"{name}_alpha_raw.png")
    for vname, fn in VARIANTS.items():
        refined = fn(rgba0.copy(), float(opt["edge_feather"]))
        out = pipeline.compose(refined, opt)
        out.save(OUT / f"{name}_{vname}.jpg", "JPEG", quality=95)
        print(f"  -> {name}_{vname}.jpg", flush=True)

print("Gotowe. Pliki w:", OUT)
