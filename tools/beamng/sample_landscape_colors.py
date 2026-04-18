"""Campiona colori paesaggio dai frame GoPro per riferimento texture.

Estrae regioni specifiche (erba primo piano, collina media, cielo, ecc.)
e calcola mediana RGB — piu' robusta di media con outlier.
"""
from pathlib import Path
import numpy as np
from PIL import Image

REF_DIR = Path(r"C:\Users\Matto\Desktop\Macerozz\tools\beamng\asphalt_refs")

# (file, regione label, (x0, y0, x1, y1) normalized 0..1)
SAMPLES = [
    # v1_late_90s: erba vicina (destra), olivi (centro collina), cielo
    ("v1_late_90s.jpg",  "erba_vicina_giallo", (0.58, 0.52, 0.90, 0.60)),
    ("v1_late_90s.jpg",  "erba_vicina_verde",  (0.00, 0.52, 0.40, 0.60)),
    ("v1_late_90s.jpg",  "collina_bosco",      (0.25, 0.42, 0.55, 0.48)),
    ("v1_late_90s.jpg",  "campo_paese",        (0.30, 0.46, 0.60, 0.50)),
    ("v1_late_90s.jpg",  "edifici_valle",      (0.25, 0.48, 0.45, 0.50)),
    ("v1_late_90s.jpg",  "cielo_basso",        (0.20, 0.20, 0.60, 0.30)),
    # v1_straight_mid: vegetation dx, campo sx, collina fondo
    ("v1_straight_mid.jpg", "cespugli_dx",     (0.70, 0.30, 0.95, 0.42)),
    ("v1_straight_mid.jpg", "prato_sx",        (0.02, 0.28, 0.28, 0.42)),
    ("v1_straight_mid.jpg", "alberi_sfondo",   (0.30, 0.22, 0.65, 0.32)),
    # v1_late_120s: prato verde chiaro
    ("v1_late_120s.jpg", "prato_primavera",    (0.65, 0.34, 0.95, 0.42)),
    # v2_late: erba + fiori gialli/bianchi, albero solitario, collina bosco
    ("v2_late.jpg", "prato_fiori",             (0.72, 0.34, 0.98, 0.44)),
    ("v2_late.jpg", "albero_solitario_canopy", (0.70, 0.22, 0.92, 0.30)),
    ("v2_late.jpg", "collina_lontana",         (0.10, 0.28, 0.70, 0.34)),
    # v1_late_180s: bosco denso
    ("v1_late_180s.jpg", "bosco_denso",        (0.05, 0.18, 0.40, 0.32)),
]


def sample_region(img: np.ndarray, x0, y0, x1, y1) -> tuple[int, int, int]:
    h, w = img.shape[:2]
    X0, Y0 = int(x0 * w), int(y0 * h)
    X1, Y1 = int(x1 * w), int(y1 * h)
    crop = img[Y0:Y1, X0:X1].reshape(-1, 3)
    # mediana per robustezza
    r = int(np.median(crop[:, 0]))
    g = int(np.median(crop[:, 1]))
    b = int(np.median(crop[:, 2]))
    return (r, g, b)


def main():
    print(f"{'file':<25} {'regione':<25} {'RGB':<16} {'hex':<8}")
    print("-" * 80)
    for f, label, box in SAMPLES:
        p = REF_DIR / f
        if not p.exists():
            print(f"MISSING: {f}")
            continue
        img = np.array(Image.open(p).convert("RGB"))
        r, g, b = sample_region(img, *box)
        hex_s = f"#{r:02x}{g:02x}{b:02x}"
        print(f"{f:<25} {label:<25} ({r:>3},{g:>3},{b:>3})  {hex_s}")


if __name__ == "__main__":
    main()
