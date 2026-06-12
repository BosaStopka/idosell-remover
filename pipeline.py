"""Silnik obrobki zdjec: BiRefNet + cien + kolory + wyostrzanie.

Uzywany przez app.py (aplikacja webowa). Parametry przekazywane per zadanie.
"""
from io import BytesIO

from PIL import Image, ImageChops, ImageEnhance, ImageFilter

_session = None

DEFAULTS = {
    "size": 1600,            # bok kwadratu wyjsciowego
    "padding": 0.90,         # maks. udzial produktu w kadrze
    "max_upscale": 3.0,      # maks. powiekszenie malych zdjec
    "shadow": True,          # cien dwuwarstwowy
    "shadow_opacity": 0.20,  # krycie cienia kontaktowego (ambient = 1/2)
    "shadow_blur": 8,        # rozmycie cienia kontaktowego (ambient = ~4x)
    "colors": True,          # podbicie kolorow
    "saturation": 1.04,
    "contrast": 1.02,
    "sharpen": True,         # unsharp po upscale > 1.2x
    "edge_feather": 1.0,     # wtopienie krawedzi maski w px
}


def get_session():
    """Lazy-load modelu (ok. 1 GB RAM); enable_cpu_mem_arena=False
    zapobiega 'bad allocation' przy malej ilosci wolnego RAM."""
    global _session
    if _session is None:
        import onnxruntime as ort
        from rembg.sessions.birefnet_general import BiRefNetSessionGeneral
        opts = ort.SessionOptions()
        opts.enable_cpu_mem_arena = False
        _session = BiRefNetSessionGeneral("birefnet-general", opts)
    return _session


def refine_edges(rgba: Image.Image, feather: float) -> Image.Image:
    """Zdejmij 1px obwodke tla, wygladz i UTWARDZ krawedz.

    Samo rozmycie (stara wersja) zmiekczalo widocznie gore cholewki.
    Utwardzenie krzywa alfa: piksele <40 -> 0, >200 -> 255, posrednie
    rozciagniete - krawedz gladka, ale ostra jak w oryginale.
    """
    r, g, b, a = rgba.split()
    a = a.filter(ImageFilter.MinFilter(3))
    if feather > 0:
        a = a.filter(ImageFilter.GaussianBlur(min(feather, 0.8)))
    a = a.point(lambda v: 0 if v < 40 else 255 if v > 200 else
                int((v - 40) * 255 / 160))
    return Image.merge("RGBA", (r, g, b, a))


def _shadow_layer(alpha, size, obj_x, obj_y, obj_h, offset_frac, blur, opacity):
    offset = max(4, int(obj_h * offset_frac))
    layer = Image.new("L", size, 0)
    layer.paste(alpha, (obj_x, obj_y + offset))
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    return layer.point(lambda v: int(v * opacity))


def compose(rgba: Image.Image, opt: dict) -> Image.Image:
    """Crop do obiektu, skalowanie, cien, biale tlo, kolory."""
    size = (int(opt["size"]), int(opt["size"]))
    bbox = rgba.getbbox()
    if bbox:
        rgba = rgba.crop(bbox)

    obj_w, obj_h = rgba.size
    max_dim = int(min(size) * float(opt["padding"]))
    ratio = min(max_dim / obj_w, max_dim / obj_h, float(opt["max_upscale"]))
    if ratio != 1:
        rgba = rgba.resize(
            (int(obj_w * ratio), int(obj_h * ratio)), Image.LANCZOS)
        # delikatnie - mocniejszy unsharp "przepala" dzianine i jasne kolory
        if opt["sharpen"] and ratio > 1.3:
            rgba = rgba.filter(
                ImageFilter.UnsharpMask(radius=1.5, percent=60, threshold=3))

    canvas = Image.new("RGBA", size, (255, 255, 255, 255))
    obj_w, obj_h = rgba.size
    obj_x = (size[0] - obj_w) // 2
    obj_y = (size[1] - obj_h) // 2

    if opt["shadow"]:
        alpha = rgba.split()[3]
        op = float(opt["shadow_opacity"])
        bl = int(opt["shadow_blur"])
        contact = _shadow_layer(alpha, size, obj_x, obj_y, obj_h,
                                0.015, bl, op)
        ambient = _shadow_layer(alpha, size, obj_x, obj_y, obj_h,
                                0.04, bl * 4, op * 0.5)
        combined = ImageChops.lighter(ambient, contact)
        black = Image.new("RGBA", size, (0, 0, 0, 255))
        black.putalpha(combined)
        canvas.alpha_composite(black)

    canvas.alpha_composite(rgba, (obj_x, obj_y))
    final = canvas.convert("RGB")

    if opt["colors"]:
        if float(opt["saturation"]) != 1.0:
            final = ImageEnhance.Color(final).enhance(float(opt["saturation"]))
        if float(opt["contrast"]) != 1.0:
            final = ImageEnhance.Contrast(final).enhance(float(opt["contrast"]))
    return final


MAX_INPUT_PX = 2048  # wieksze wejscia zmniejszamy (model i tak liczy na 1024)


def process_bytes(data: bytes, opt: dict) -> Image.Image:
    """Pelny pipeline: bajty zdjecia -> gotowy obraz PIL."""
    import gc
    import time

    from rembg import remove

    options = {**DEFAULTS, **opt}
    src = Image.open(BytesIO(data)).convert("RGB")
    if max(src.size) > MAX_INPUT_PX:
        ratio = MAX_INPUT_PX / max(src.size)
        src = src.resize(
            (int(src.size[0] * ratio), int(src.size[1] * ratio)), Image.LANCZOS)

    # "bad allocation" przy malej ilosci wolnego RAM bywa przejsciowe -
    # gc + pauza i ponowna proba; odstepy rosnace (5s, 15s)
    last_err = None
    for attempt, delay in enumerate((0, 5, 15)):
        if delay:
            gc.collect()
            time.sleep(delay)
        try:
            rgba = remove(src, session=get_session()).convert("RGBA")
            break
        except Exception as e:
            last_err = e
            if "bad allocation" not in str(e):
                raise
    else:
        raise RuntimeError(
            f"Za malo wolnego RAM (3 proby): {last_err}. "
            "Zamknij inne programy i sprobuj ponownie.")

    rgba = refine_edges(rgba, float(options["edge_feather"]))
    return compose(rgba, options)
