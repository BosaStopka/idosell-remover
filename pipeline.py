"""Silnik obrobki zdjec: BiRefNet + cien + kolory + wyostrzanie.

Uzywany przez app.py (aplikacja webowa). Parametry przekazywane per zadanie.

Scalony z bg-removerem (jakosc obrobki) + wlasne mask_contrast/mask_brightness
(podbicie wejscia do maski - odzysk niskokontrastowych fragmentow).
"""
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

_session = None

DEFAULTS = {
    "size": 1600,            # bok kwadratu wyjsciowego
    "padding": 0.90,         # maks. udzial produktu w kadrze
    "max_upscale": 3.0,      # maks. powiekszenie malych zdjec
    "shadow": True,          # miekki cien-kaluza pod produktem
    "shadow_opacity": 0.18,  # krycie cienia (widoczny, naturalny)
    "shadow_blur": 11,       # rozmycie cienia
    "colors": True,          # podbicie kolorow (delikatne, naturalne)
    "saturation": 1.02,
    "contrast": 1.0,         # bez podbicia kontrastu (naturalnie)
    "sharpen": True,         # unsharp po upscale > 1.3x
    "whiten_neutral": True,  # jasne neutralne piksele (podeszwa) -> ku bieli
    "edge_feather": 1.0,     # wtopienie krawedzi maski w px
    "mirror": False,         # odbicie lustrzane (standaryzacja kierunku noska)
    # podbicie kontrastu/jasnosci TYLKO do policzenia maski - model lepiej
    # lapie niskokontrastowe fragmenty (szary element na jasnym tle);
    # finalny obraz skladany z ORYGINALNYCH pikseli, kolory bez zmian.
    # 1.0/1.0 = wylaczone. Zweryfikowane na 4576_1 (odzyskany jezyk pisty).
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


def compose(rgba: Image.Image, opt: dict) -> Image.Image:
    """Crop do obiektu, skalowanie, cien, biale tlo, kolory."""
    size = (int(opt["size"]), int(opt["size"]))
    full_bleed = is_full_bleed(rgba)
    # zawsze kadrujemy do obiektu (tlo juz wyciete) - usuwa szare marginesy
    bbox = object_bbox(rgba)
    if bbox:
        rgba = rgba.crop(bbox)

    if opt.get("whiten_neutral", True):
        rgba = whiten_neutral(rgba)  # podeszwa -> blizej bieli, kolor nietkniety

    obj_w, obj_h = rgba.size
    if full_bleed:
        # detal: wypelnij CALY kadr (cover) - zero bialych ramek, nadmiar przyciety
        ratio = min(max(size[0] / obj_w, size[1] / obj_h),
                    float(opt["max_upscale"]))
    else:
        max_dim = int(min(size) * float(opt["padding"]))
        ratio = min(max_dim / obj_w, max_dim / obj_h, float(opt["max_upscale"]))
    if ratio != 1:
        rgba = rgba.resize(
            (int(obj_w * ratio), int(obj_h * ratio)), Image.LANCZOS)
        # bardzo delikatnie - tylko zeby odzyskac ostrosc po skalowaniu,
        # bez "chrupania" faktury
        if opt["sharpen"] and ratio > 1.3:
            rgba = rgba.filter(
                ImageFilter.UnsharpMask(radius=1.0, percent=22, threshold=3))

    canvas = Image.new("RGBA", size, (255, 255, 255, 255))
    obj_w, obj_h = rgba.size
    obj_x = (size[0] - obj_w) // 2
    obj_y = (size[1] - obj_h) // 2

    if opt["shadow"] and not full_bleed:
        # miekki cien-kaluza pod butem: sylwetka przesunieta w dol na tyle,
        # by wystawala spod buta + rozmycie + widoczne krycie
        alpha = rgba.split()[3]
        op = float(opt["shadow_opacity"])
        off = max(8, int(obj_h * 0.045))
        layer = Image.new("L", size, 0)
        layer.paste(alpha, (obj_x, obj_y + off))
        layer = layer.filter(
            ImageFilter.GaussianBlur(max(6, int(opt["shadow_blur"]) * 1.8)))
        layer = layer.point(lambda v: int(v * op))
        black = Image.new("RGBA", size, (0, 0, 0, 255))
        black.putalpha(layer)
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

# AI upscale malych zrodel (Real-ESRGAN ncnn-vulkan). Binarka opcjonalna -
# gdy jej brak, funkcja zwraca oryginal (pipeline dziala zwyklym skalowaniem).
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
    with_parts=True: dodatkowo (rgb roboczy, alpha, full_bleed) dla edytora."""
    import gc
    import time

    from rembg import remove

    options = {**DEFAULTS, **opt}
    src = Image.open(BytesIO(data)).convert("RGB")
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
    rgba = refine_edges(rgba, float(options["edge_feather"]))
    final = compose(rgba, options)
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
