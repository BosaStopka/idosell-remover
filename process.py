import re
from io import BytesIO
from pathlib import Path

import onnxruntime as ort
from PIL import Image, ImageEnhance, ImageFilter
from rembg import remove
from rembg.sessions.birefnet_general import BiRefNetSessionGeneral

INPUT_DIR = Path(__file__).parent / "input"
DONE_DIR = Path(__file__).parent / "done"
OUTPUT_SIZE = (1600, 1600)

# Delikatne podbicie wygladu (1.0 = wylaczone)
ENHANCE_SATURATION = 1.06   # +6% nasycenia kolorow
ENHANCE_CONTRAST = 1.03     # +3% kontrastu
EDGE_FEATHER = 1.0          # rozmycie krawedzi maski w px (wtopienie w tlo)

# Cien kontaktowy pod butem (tylko wersja _pimp) - styl "Sportano":
# maska buta przesunieta w dol i rozmyta, but zakrywa wiekszosc,
# zostaje waski naturalny cien wzdluz konturu podeszwy
SHADOW_OPACITY = 0.35       # max krycie cienia (0-1)
SHADOW_BLUR = 12            # rozmycie Gaussa w px
SHADOW_OFFSET_FRAC = 0.025  # przesuniecie w dol jako ulamek wysokosci buta

# Tryb porownawczy: zapisuje DWIE wersje kazdego zdjecia -
# {nazwa}.jpg (czysty birefnet) i {nazwa}_pimp.jpg (krawedzie + kolory).
# Po wyborze wariantu ustawic False (zostanie tylko wybrana sciezka).
COMPARE_MODE = True

# enable_cpu_mem_arena=False zapobiega "bad allocation" przy malej ilosci
# wolnego RAM (arena onnxruntime fragmentuje pamiec miedzy kolejnymi zdjeciami)
sess_opts = ort.SessionOptions()
sess_opts.enable_cpu_mem_arena = False
session = BiRefNetSessionGeneral("birefnet-general", sess_opts)


def extract_product_id(filename: str) -> str:
    stem = Path(filename).stem
    match = re.match(r"^(.+?)_\d+$", stem)
    return match.group(1) if match else stem


def refine_edges(rgba_image: Image.Image) -> Image.Image:
    """Zdejmij 1px obwodke resztkowego tla i delikatnie wtop krawedz."""
    r, g, b, a = rgba_image.split()
    a = a.filter(ImageFilter.MinFilter(3))
    if EDGE_FEATHER > 0:
        a = a.filter(ImageFilter.GaussianBlur(EDGE_FEATHER))
    return Image.merge("RGBA", (r, g, b, a))


def enhance_colors(image: Image.Image) -> Image.Image:
    """Subtelne odswiezenie kolorow - bez przekamywania produktu."""
    if ENHANCE_SATURATION != 1.0:
        image = ImageEnhance.Color(image).enhance(ENHANCE_SATURATION)
    if ENHANCE_CONTRAST != 1.0:
        image = ImageEnhance.Contrast(image).enhance(ENHANCE_CONTRAST)
    return image


def place_on_white(rgba_image: Image.Image, size: tuple,
                   shadow: bool = False) -> Image.Image:
    """Crop to object, center on white square canvas with 10% padding."""
    bbox = rgba_image.getbbox()
    if bbox:
        rgba_image = rgba_image.crop(bbox)

    obj_w, obj_h = rgba_image.size
    max_dim = int(min(size) * 0.90)
    # skalujemy w dol i w gore (upscale max 2x, zeby nie rozmywac malych zdjec)
    ratio = min(max_dim / obj_w, max_dim / obj_h, 2.0)
    if ratio != 1:
        rgba_image = rgba_image.resize(
            (int(obj_w * ratio), int(obj_h * ratio)), Image.LANCZOS
        )

    canvas = Image.new("RGBA", size, (255, 255, 255, 255))
    obj_w, obj_h = rgba_image.size
    obj_x = (size[0] - obj_w) // 2
    obj_y = (size[1] - obj_h) // 2

    if shadow:
        # maska buta przesunieta w dol + blur; but pasteowany na wierzchu
        # zakryje srodek, zostanie waski cien wzdluz konturu podeszwy
        offset = max(6, int(obj_h * SHADOW_OFFSET_FRAC))
        shadow_layer = Image.new("L", size, 0)
        shadow_layer.paste(rgba_image.split()[3], (obj_x, obj_y + offset))
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(SHADOW_BLUR))
        shadow_layer = shadow_layer.point(lambda v: int(v * SHADOW_OPACITY))
        black = Image.new("RGBA", size, (0, 0, 0, 255))
        black.putalpha(shadow_layer)
        canvas.alpha_composite(black)

    canvas.alpha_composite(rgba_image, (obj_x, obj_y))
    return canvas.convert("RGB")


def process_image(input_path: Path) -> Path:
    product_id = extract_product_id(input_path.name)
    output_dir = DONE_DIR / product_id
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / (input_path.stem + ".jpg")

    print(f"  Usuwanie tla (birefnet-general)...", flush=True)
    output_data = remove(input_path.read_bytes(), session=session)
    rgba = Image.open(BytesIO(output_data)).convert("RGBA")

    if COMPARE_MODE:
        plain = place_on_white(rgba, OUTPUT_SIZE)
        plain.save(output_path, "JPEG", quality=95)

        pimped = enhance_colors(
            place_on_white(refine_edges(rgba), OUTPUT_SIZE, shadow=True)
        )
        pimp_path = output_dir / (input_path.stem + "_pimp.jpg")
        pimped.save(pimp_path, "JPEG", quality=95)
        print(f"  Zapisano wersje czysta i _pimp", flush=True)
    else:
        print(f"  Wygladzanie krawedzi i umieszczanie na bialym tle...", flush=True)
        final = enhance_colors(
            place_on_white(refine_edges(rgba), OUTPUT_SIZE, shadow=True)
        )
        final.save(output_path, "JPEG", quality=95)

    return output_path


def process_all():
    extensions = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}
    images = sorted(
        (f for f in INPUT_DIR.iterdir() if f.is_file() and f.suffix.lower() in extensions),
        key=lambda f: f.name,
    )

    if not images:
        print("Brak zdjec w folderze input/")
        return

    print(f"Znaleziono {len(images)} zdjec do przetworzenia\n")

    errors = []
    for i, img_path in enumerate(images, 1):
        product_id = extract_product_id(img_path.name)
        print(f"[{i}/{len(images)}] {img_path.name} (produkt: {product_id})")
        try:
            output = process_image(img_path)
            print(f"  -> Zapisano: {output}\n")
        except Exception as e:
            errors.append(img_path.name)
            print(f"  BLAD: {e}\n")

    if errors:
        print(f"Bledy ({len(errors)}): {', '.join(errors)}")
        print("Zamknij inne programy (zwolnij RAM) i uruchom ponownie - "
              "przetworzone zdjecia mozna usunac z input/")
    print("Gotowe!")


if __name__ == "__main__":
    INPUT_DIR.mkdir(exist_ok=True)
    DONE_DIR.mkdir(exist_ok=True)
    process_all()
