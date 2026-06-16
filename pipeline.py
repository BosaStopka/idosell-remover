"""Silnik obrobki zdjec: BiRefNet + cien + kolory + wyostrzanie.

Uzywany przez app.py (aplikacja webowa). Parametry przekazywane per zadanie.
"""
from io import BytesIO

from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageOps

_session = None

DEFAULTS = {
    "size": 1600,            # bok kwadratu wyjsciowego
    "padding": 0.90,         # maks. udzial produktu w kadrze
    "max_upscale": 3.0,      # maks. powiekszenie malych zdjec
    "shadow": True,          # cien wlaczony (master)
    "shadow_mode": "auto",   # auto | preserve | minimal | none - patrz compose()
    "shadow_opacity": 0.24,  # krycie MINIMALNEGO cienia kontaktowego (gdy brak realnego)
    "shadow_blur": 11,       # (zachowane dla zgodnosci UI; minimal liczy blur z wysokosci)
    "shadow_detect_drop": 6.0,  # o ile pas pod podeszwa ciemniejszy od tla = realny cien
    "preserve_plate_pct": 88,   # percentyl jasnosci tla -> punkt bieli przy ZACHOWAJ
    "colors": True,          # podbicie kolorow (delikatne, naturalne)
    "saturation": 1.02,
    "contrast": 1.0,         # bez podbicia kontrastu (naturalnie)
    "sharpen": True,         # unsharp po upscale > 1.2x
    "whiten_neutral": True,  # jasne neutralne piksele (podeszwa) -> ku bieli
    "edge_feather": 1.0,     # wtopienie krawedzi maski w px
    "mirror": False,         # odbicie lustrzane (standaryzacja kierunku noska)
    # podbicie kontrastu/jasnosci TYLKO do policzenia maski - model lepiej
    # lapie niskokontrastowe fragmenty (szary element na jasnym tle);
    # finalny obraz skladany z ORYGINALNYCH pikseli, kolory bez zmian.
    # 1.0/1.0 = wylaczone. Przeniesione z idosell-remover (4576).
    "mask_contrast": 1.8,
    "mask_brightness": 0.8,
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


def object_bbox(rgba: Image.Image):
    """Ramka kadrowania po ISTOTNYCH obszarach maski - pomija pojedyncze
    piksele szumu i resztki cienia z oryginalu, ktore rozszerzaly kadr
    (but wychodzil na 60% szerokosci zamiast 90%)."""
    import numpy as np
    from scipy import ndimage
    alpha = np.array(rgba.split()[3])
    mask = alpha > 128
    if not mask.any():
        return rgba.getbbox()
    labeled, n = ndimage.label(mask)
    if n > 1:
        sizes = ndimage.sum(mask, labeled, range(1, n + 1))
        keep = sizes >= sizes.max() * 0.01
        mask = np.isin(labeled, np.nonzero(keep)[0] + 1)
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    return (int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1)


def is_full_bleed(rgba: Image.Image) -> bool:
    """Ujecie pelnokadrowe (zblizenie/detal): obiekt uciety krawedziami
    oryginalu. Takie zdjecie ma wypelnic kadr, bez 'ramki' i bez cienia."""
    import numpy as np
    a = np.array(rgba.split()[3]) > 128
    if not a.any():
        return False
    edges = [a[0].mean(), a[-1].mean(), a[:, 0].mean(), a[:, -1].mean()]
    return sum(1 for e in edges if e > 0.15) >= 2


def whiten_neutral(rgba: Image.Image) -> Image.Image:
    """Jasne NEUTRALNE piksele produktu (szara/biala podeszwa) ciagnie ku
    bieli; kolorowe (granat, czerwien) nietkniete - wysokie nasycenie je
    chroni. Tlo (alpha=0) pomijane."""
    import numpy as np
    arr = np.asarray(rgba).astype(np.float64)
    rgb, a = arr[..., :3], arr[..., 3]
    mx = rgb.max(axis=2)
    mn = rgb.min(axis=2)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0)
    sel = (a > 128) & (mx > 165) & (sat < 0.14)        # jasne + neutralne
    lift = np.clip((mx - 165) / 90.0, 0, 1)            # mocniej im jasniej
    push = (sel * lift * 0.85)[..., None]              # do 85% ku bieli
    arr[..., :3] = np.clip(rgb + (255 - rgb) * push, 0, 255)
    return Image.fromarray(arr.astype("uint8"), "RGBA")


def _apply_colors(final: Image.Image, opt: dict) -> Image.Image:
    if opt.get("colors"):
        if float(opt["saturation"]) != 1.0:
            final = ImageEnhance.Color(final).enhance(float(opt["saturation"]))
        if float(opt["contrast"]) != 1.0:
            final = ImageEnhance.Contrast(final).enhance(float(opt["contrast"]))
    return final


def _luma(arr):
    return 0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2]


def has_real_shadow(src_rgb: Image.Image, alpha: Image.Image, drop=6.0) -> bool:
    """Czy oryginal ma realny cien studyjny pod butem - pas tla tuz pod
    podeszwa wyraznie ciemniejszy od czystego tla. Gdy but dotyka dolu kadru
    (brak miejsca na pas) -> False (pojdzie tryb minimal)."""
    import numpy as np
    lum = _luma(np.asarray(src_rgb.convert("RGB")).astype(np.float32))
    m = np.asarray(alpha) > 128
    if not m.any():
        return False
    ys, xs = np.where(m)
    y1, x0, x1 = int(ys.max()), int(xs.min()), int(xs.max())
    h, H = int(ys.max() - ys.min() + 1), lum.shape[0]
    y_lo, y_hi = y1 + 2, min(H, y1 + 2 + max(4, int(h * 0.08)))
    bg = ~m
    if y_hi - y_lo < 3 or bg.sum() < 100:
        return False
    band = lum[y_lo:y_hi, x0:x1 + 1]
    ref = float(np.percentile(lum[bg], 70))
    return float(np.median(band)) < ref - float(drop)


def preserve_to_white(src_rgb, alpha, plate_pct=88) -> Image.Image:
    """Tlo studyjne -> biel z ZACHOWANIEM realnego cienia (z luminancji, wiec
    neutralny - bez koloru tla), na to ostry wybielony but. Pelna klatka."""
    import numpy as np
    lum = _luma(np.asarray(src_rgb.convert("RGB")).astype(np.float32))
    bg = ~(np.asarray(alpha) > 128)
    plate = max(1.0, float(np.percentile(lum[bg], plate_pct)))
    g = np.clip(lum * (255.0 / plate), 0, 255).astype("uint8")
    gi = Image.fromarray(g, "L")
    base = Image.merge("RGB", (gi, gi, gi)).convert("RGBA")
    shoe = src_rgb.convert("RGB").copy()
    shoe.putalpha(alpha)
    base.alpha_composite(whiten_neutral(shoe))
    return base.convert("RGB")


def _scale_to_pad(img, size, padding, max_upscale, sharpen):
    max_dim = int(min(size) * float(padding))
    w, h = img.size
    ratio = min(max_dim / w, max_dim / h, float(max_upscale))
    if ratio != 1:
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        if sharpen and ratio > 1.3:
            img = img.filter(
                ImageFilter.UnsharpMask(radius=1.0, percent=22, threshold=3))
    return img


def compose(rgba: Image.Image, opt: dict, src_rgb: Image.Image = None) -> Image.Image:
    """Skladanie na biale tlo z cieniem. Tryby (opt['shadow_mode']):
      - full-bleed (detal): cover, bez cienia (jak dotad),
      - ZACHOWAJ: gdy oryginal ma realny cien -> tlo->biel z jego zachowaniem,
      - MINIMAL: brak realnego cienia -> cienka linia kontaktu pod podeszwa.
    src_rgb (oryginal sprzed wyciecia) potrzebny do trybu ZACHOWAJ; bez niego
    (np. edytor maski) zawsze MINIMAL."""
    size = (int(opt["size"]), int(opt["size"]))
    full_bleed = is_full_bleed(rgba)
    alpha_full = rgba.split()[3]
    shadow_on = bool(opt.get("shadow", True))
    mode = opt.get("shadow_mode", "auto")

    # ---- FULL-BLEED (zblizenie/detal): tlo->biel z ZACHOWANIEM kadrowania ----
    # Stary cover wycinal buta i skalowal bbox - na ukosnym zblizeniu rogi bboxa
    # byly przezroczyste -> bialy naroznik ("ramka"). Teraz bielimy tlo w miejscu
    # (preserve_to_white) i cover-skalujemy CALY kadr (nieprzezroczysty: biel+but),
    # wiec but zostaje przy krawedziach jak w oryginale, bez ramki.
    if full_bleed:
        if src_rgb is not None:
            pimg = preserve_to_white(
                src_rgb, alpha_full, int(opt.get("preserve_plate_pct", 88)))
        else:  # edytor maski: zloz wyciety but na bieli (brak oryginalu tla)
            pimg = Image.new("RGB", rgba.size, (255, 255, 255))
            pimg.paste(rgba.convert("RGB"), (0, 0), alpha_full)
        w, h = pimg.size
        ratio = min(max(size[0] / w, size[1] / h), float(opt["max_upscale"]))
        if ratio != 1:
            pimg = pimg.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
            if opt["sharpen"] and ratio > 1.3:
                pimg = pimg.filter(
                    ImageFilter.UnsharpMask(radius=1.0, percent=22, threshold=3))
        canvas = Image.new("RGB", size, (255, 255, 255))
        nw, nh = pimg.size
        canvas.paste(pimg, ((size[0] - nw) // 2, (size[1] - nh) // 2))
        return _apply_colors(canvas, opt)

    # ---- ZACHOWAJ: realny cien z oryginalu ----
    want_preserve = shadow_on and src_rgb is not None and mode in ("auto", "preserve")
    if want_preserve and (mode == "preserve" or has_real_shadow(
            src_rgb, alpha_full, opt.get("shadow_detect_drop", 6.0))):
        import numpy as np
        pimg = preserve_to_white(
            src_rgb, alpha_full, int(opt.get("preserve_plate_pct", 88)))
        a = np.asarray(alpha_full) > 128
        ys, xs = np.where(a)
        x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
        w, h = x1 - x0 + 1, y1 - y0 + 1
        # skala PO SAMYM BUCIE (jak minimal) - rozmiar buta spojny w katalogu;
        # realny cien zostaje w obrazie i laduje w dolnym/bocznym marginesie.
        max_dim = int(min(size) * float(opt["padding"]))
        ratio = min(max_dim / w, max_dim / h, float(opt["max_upscale"]))
        if ratio != 1:
            pimg = pimg.resize((int(pimg.width * ratio),
                                int(pimg.height * ratio)), Image.LANCZOS)
            if opt["sharpen"] and ratio > 1.3:
                pimg = pimg.filter(
                    ImageFilter.UnsharpMask(radius=1.0, percent=22, threshold=3))
        # wysrodkuj BBOX BUTA w kadrze (jak minimal); biale tlo pimg = niewidoczne
        scx = (x0 + x1 + 1) / 2 * ratio
        scy = (y0 + y1 + 1) / 2 * ratio
        canvas = Image.new("RGB", size, (255, 255, 255))
        canvas.paste(pimg, (int(size[0] / 2 - scx), int(size[1] / 2 - scy)))
        return _apply_colors(canvas, opt)

    # ---- MINIMAL: wytnij + (opcjonalnie) cienka linia kontaktu ----
    bbox = object_bbox(rgba)
    if bbox:
        rgba = rgba.crop(bbox)
    if opt.get("whiten_neutral", True):
        rgba = whiten_neutral(rgba)
    rgba = _scale_to_pad(rgba, size, opt["padding"], opt["max_upscale"],
                         opt["sharpen"])
    canvas = Image.new("RGBA", size, (255, 255, 255, 255))
    ow, oh = rgba.size
    ox, oy = (size[0] - ow) // 2, (size[1] - oh) // 2
    if shadow_on and mode != "none":
        # cienka linia kontaktu: sylwetka zsunieta minimalnie + maly blur
        alpha = rgba.split()[3]
        op = float(opt["shadow_opacity"])
        off = max(3, int(oh * 0.012))
        blur = max(5, int(oh * 0.011))
        layer = Image.new("L", size, 0)
        layer.paste(alpha, (ox, oy + off))
        layer = layer.filter(ImageFilter.GaussianBlur(blur))
        layer = layer.point(lambda v: int(v * op))
        black = Image.new("RGBA", size, (0, 0, 0, 255))
        black.putalpha(layer)
        canvas.alpha_composite(black)
    canvas.alpha_composite(rgba, (ox, oy))
    return _apply_colors(canvas.convert("RGB"), opt)


MAX_INPUT_PX = 2048  # wieksze wejscia zmniejszamy (model i tak liczy na 1024)

# AI upscale malych zrodel (Real-ESRGAN ncnn-vulkan, dziala na AMD przez Vulkan)
from pathlib import Path

REALESRGAN_EXE = Path(__file__).parent / "tools" / "realesrgan" / \
    "realesrgan-ncnn-vulkan.exe"
AI_UPSCALE_BELOW = 800   # zrodla mniejsze niz tyle px -> AI upscale 4x


def ai_upscale(img: Image.Image) -> Image.Image:
    """Inteligentne powiekszenie 4x malych zrodel - odtwarza fakture
    zamiast rozmywac (LANCZOS przy 4-5x daje papke). Gdy brak narzedzia
    lub blad - zwraca oryginal (pipeline dziala dalej zwyklym skalowaniem)."""
    if not REALESRGAN_EXE.exists():
        return img
    import subprocess
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "in.png"
            out = Path(td) / "out.png"
            img.save(inp)
            subprocess.run(
                [str(REALESRGAN_EXE), "-i", str(inp), "-o", str(out),
                 "-n", "realesrgan-x4plus"],
                check=True, capture_output=True, timeout=300)
            up = Image.open(out).convert("RGB").copy()
            # Real-ESRGAN przeostrza fakture (mesh, splot) - mocno mieszamy
            # z gladkim LANCZOS, zeby detal byl naturalny, nie "chrupiacy"
            lanczos = img.resize(up.size, Image.LANCZOS)
            return Image.blend(up, lanczos, 0.6)
    except Exception:
        return img


def process_bytes(data: bytes, opt: dict, with_parts: bool = False):
    """Pelny pipeline: bajty zdjecia -> gotowy obraz PIL.
    with_parts=True: dodatkowo (rgba robocze, full_bleed) dla edytora."""
    import gc
    import time

    from rembg import remove

    options = {**DEFAULTS, **opt}
    _src = Image.open(BytesIO(data))
    if _src.mode in ("RGBA", "LA", "P"):
        # przezroczyste PNG (produkt juz wyciety) -> kompozycja na biel,
        # inaczej convert("RGB") da CZARNE tlo i model/cien zglupieja
        _src = _src.convert("RGBA")
        _bg = Image.new("RGBA", _src.size, (255, 255, 255, 255))
        _bg.alpha_composite(_src)
        src = _bg.convert("RGB")
    else:
        src = _src.convert("RGB")
    if options.get("mirror"):
        src = ImageOps.mirror(src)  # odbicie - standaryzacja kierunku noska
    if max(src.size) < AI_UPSCALE_BELOW:
        src = ai_upscale(src)
    if max(src.size) > MAX_INPUT_PX:
        ratio = MAX_INPUT_PX / max(src.size)
        src = src.resize(
            (int(src.size[0] * ratio), int(src.size[1] * ratio)), Image.LANCZOS)

    # obraz do SEGMENTACJI: podbity kontrast/jasnosc (lepsza maska na
    # niskokontrastowych fragmentach); finalny obraz - ORYGINALNE piksele
    mc, mb = float(options["mask_contrast"]), float(options["mask_brightness"])
    seg_src = src
    if mc != 1.0:
        seg_src = ImageEnhance.Contrast(seg_src).enhance(mc)
    if mb != 1.0:
        seg_src = ImageEnhance.Brightness(seg_src).enhance(mb)

    # "bad allocation" przy malej ilosci wolnego RAM bywa przejsciowe -
    # gc + pauza i ponowna proba; odstepy rosnace (5s, 15s)
    last_err = None
    for attempt, delay in enumerate((0, 5, 15)):
        if delay:
            gc.collect()
            time.sleep(delay)
        try:
            seg = remove(seg_src, session=get_session()).convert("RGBA")
            break
        except Exception as e:
            last_err = e
            if "bad allocation" not in str(e):
                raise
    else:
        raise RuntimeError(
            f"Za malo wolnego RAM (3 proby): {last_err}. "
            "Zamknij inne programy i sprobuj ponownie.")

    # alfa z podbitego obrazu, piksele z ORYGINALU
    rgba = src.convert("RGBA")
    rgba.putalpha(seg.split()[3])
    full_bleed = is_full_bleed(rgba)
    # ZAWSZE wycinamy tlo (wczesniej full-bleed zostawial szare tlo +
    # biale ramki). Przy detalu po prostu wypelniamy kadr (padding 1.0).
    rgba = refine_edges(rgba, float(options["edge_feather"]))
    # src (oryginal sprzed wyciecia) -> tryb ZACHOWAJ moze odzyskac realny cien
    final = compose(rgba, options, src_rgb=src)
    if with_parts:
        # czesci robocze dla edytora maski: PRAWDZIWY oryginal (src) jako rgb
        # (rembg zeruje tlo na czarno - 'Przywroc' odslanialoby czern) +
        # maska (alpha). src i alpha sa tych samych wymiarow.
        return final, src.convert("RGB"), rgba.split()[3], full_bleed
    return final


def compose_from(rgb: Image.Image, alpha: Image.Image, opt: dict) -> Image.Image:
    """Rekompozycja z recznie poprawiona maska - bez inferencji (sekundy)."""
    options = {**DEFAULTS, **opt}
    rgba = rgb.convert("RGB").copy()
    rgba.putalpha(alpha.convert("L"))
    return compose(rgba, options)
