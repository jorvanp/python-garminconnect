#!/usr/bin/env python3
"""
Genera todos los íconos y assets del logo para Sento Run.

Uso:
    .venv/bin/python generate_icons.py <ruta_al_logo.png>

Ejemplo:
    .venv/bin/python generate_icons.py logo_original.png

El script genera en /static/:
    favicon.ico            — 16/32/48px multi-size para pestaña del navegador
    apple-touch-icon.png   — 180×180 para iOS
    icon-192.png           — 192×192 para Android / PWA
    icon-512.png           — 512×512 para splash screen PWA
    logo.png               — 400px ancho, logo completo para navbar y login
    logo-icon.png          — 200×200 recortado cuadrado, solo para navbar compacta
    og-image.png           — 1200×630 para Open Graph (redes sociales)
"""
import sys
from pathlib import Path
from PIL import Image

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)


def make_square(img: Image.Image, size: int) -> Image.Image:
    """Resize manteniendo aspect ratio, luego centra sobre fondo transparente cuadrado."""
    img = img.convert("RGBA")
    img.thumbnail((size, size), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    offset = ((size - img.width) // 2, (size - img.height) // 2)
    canvas.paste(img, offset, img)
    return canvas


def make_og_image(img: Image.Image) -> Image.Image:
    """1200×630 con fondo oscuro centrado para Open Graph."""
    bg = Image.new("RGBA", (1200, 630), (10, 10, 20, 255))
    logo = img.convert("RGBA")
    logo.thumbnail((400, 400), Image.LANCZOS)
    offset = ((1200 - logo.width) // 2, (630 - logo.height) // 2)
    bg.paste(logo, offset, logo)
    return bg


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    src = Path(sys.argv[1])
    if not src.exists():
        print(f"❌ No se encontró: {src}")
        sys.exit(1)

    img = Image.open(src)
    print(f"✅ Logo cargado: {img.size[0]}×{img.size[1]}px, modo {img.mode}")

    # favicon.ico — multi-size
    ico_path = STATIC_DIR / "favicon.ico"
    sizes = [16, 32, 48]
    icons = [make_square(img, s) for s in sizes]
    icons[0].save(ico_path, format="ICO", sizes=[(s, s) for s in sizes],
                  append_images=icons[1:])
    print(f"  ✓ favicon.ico          ({', '.join(str(s) for s in sizes)}px)")

    # PNG icons
    for name, size in [
        ("apple-touch-icon.png", 180),
        ("icon-192.png", 192),
        ("icon-512.png", 512),
        ("logo-icon.png", 200),
    ]:
        out = make_square(img, size)
        out.save(STATIC_DIR / name, format="PNG", optimize=True)
        print(f"  ✓ {name:<26} ({size}×{size}px)")

    # logo.png — ancho 400px manteniendo proporción
    logo_full = img.convert("RGBA")
    ratio = 400 / logo_full.width
    new_h = int(logo_full.height * ratio)
    logo_full = logo_full.resize((400, new_h), Image.LANCZOS)
    logo_full.save(STATIC_DIR / "logo.png", format="PNG", optimize=True)
    print(f"  ✓ logo.png                      (400×{new_h}px)")

    # og-image.png
    og = make_og_image(img)
    og.convert("RGB").save(STATIC_DIR / "og-image.png", format="PNG", optimize=True)
    print(f"  ✓ og-image.png                  (1200×630px)")

    print(f"\n🎉 Todos los assets generados en: {STATIC_DIR.resolve()}")


if __name__ == "__main__":
    main()
