"""Classifica ogni frame video SS17 in base a cosa si vede ai lati strada.

Euristiche (veloci, niente ML):
- Area laterale (10-40% larghezza, 30-70% altezza): misura densita' verde scuro
  (alberi/bosco) e luminosita' (campo aperto ha cielo + verde chiaro).
- Zone strette in alto-centro (20-80% larghezza, 40-55% altezza): se ha
  blocchi beige/grigi ad alta luminanza e bassa saturazione -> edificio.
- Output: JSON con per frame:
    t_sec, video_tag, tree_density_left, tree_density_right,
    has_building_ahead, openness (0=bosco chiuso, 1=campo aperto)

Serve per modulare la densita' clutter/forest lungo la strada nel mod,
e scegliere dove piazzare farmhouse vicino.
"""
from pathlib import Path
import cv2
import json
import numpy as np
import re

REF_DIR = Path(r"C:\Users\Matto\Desktop\Macerozz\tools\beamng\landscape_refs")
OUT = REF_DIR.parent / "landscape_scenes.json"


def classify(frame_bgr: np.ndarray) -> dict:
    h, w = frame_bgr.shape[:2]
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    H = hsv[:, :, 0].astype(np.float32)
    S = hsv[:, :, 1].astype(np.float32) / 255.0
    V = hsv[:, :, 2].astype(np.float32) / 255.0

    def crop(x0f, y0f, x1f, y1f):
        return (slice(int(y0f * h), int(y1f * h)),
                slice(int(x0f * w), int(x1f * w)))

    # Verde scuro = alberi (H 35-85 OpenCV, S > 0.25, V 0.1-0.6)
    green_tree = ((H >= 30) & (H <= 90) & (S > 0.25) & (V > 0.08) & (V < 0.55))
    # Cielo (H 95-125, V > 0.6, S < 0.4)
    sky = ((H >= 95) & (H <= 130) & (V > 0.55) & (S < 0.45))
    # Beige/muro edificio (S < 0.28, V 0.4-0.85) — tetti rossi sono
    # H 0-15 con S 0.3-0.7
    wall = ((S < 0.30) & (V > 0.40) & (V < 0.85))
    roof = (((H <= 15) | (H >= 165)) & (S >= 0.25) & (V > 0.30) & (V < 0.75))

    # Area laterale sx: x in [0.00,0.30], y in [0.30,0.75]
    ls = crop(0.00, 0.30, 0.30, 0.75)
    rs = crop(0.70, 0.30, 1.00, 0.75)
    mid = crop(0.35, 0.35, 0.65, 0.60)

    tree_left = float(green_tree[ls].mean())
    tree_right = float(green_tree[rs].mean())
    sky_ratio = float(sky[ls].mean() + sky[rs].mean()) / 2.0

    # edificio frontale: blocchi con aspetto muro o tetto
    wall_mid = float(wall[mid].mean())
    roof_mid = float(roof[mid].mean())
    has_building = wall_mid > 0.25 or roof_mid > 0.08

    # openness: piu' cielo laterale e meno verde = aperto (campo/valle)
    openness = max(0.0, min(1.0, sky_ratio * 1.6 +
                              (1.0 - (tree_left + tree_right) / 2.0) * 0.3))

    return {
        "tree_left": round(tree_left, 3),
        "tree_right": round(tree_right, 3),
        "sky_lateral": round(sky_ratio, 3),
        "wall_mid": round(wall_mid, 3),
        "roof_mid": round(roof_mid, 3),
        "has_building_ahead": has_building,
        "openness": round(openness, 3),
    }


def main():
    files = sorted(REF_DIR.glob("*.jpg"))
    scenes = []
    for p in files:
        # nome: v1_t0034s.jpg / v2_t0012s.jpg
        m = re.match(r"(v\d)_t(\d{4})s\.jpg$", p.name)
        if not m:
            continue
        tag, t = m.group(1), int(m.group(2))
        frame = cv2.imread(str(p))
        if frame is None:
            continue
        info = classify(frame)
        info["video"] = tag
        info["t_sec"] = t
        scenes.append(info)
    OUT.write_text(json.dumps(scenes, indent=2), encoding="utf-8")
    # Riassunto
    n = len(scenes)
    n_build = sum(1 for s in scenes if s["has_building_ahead"])
    n_forest = sum(1 for s in scenes if (s["tree_left"] + s["tree_right"]) / 2 > 0.45)
    n_open = sum(1 for s in scenes if s["openness"] > 0.65)
    print(f"Totale {n} frame")
    print(f"  con edificio frontale:     {n_build}  ({100*n_build/n:.0f}%)")
    print(f"  bosco denso (verde > 45%): {n_forest}  ({100*n_forest/n:.0f}%)")
    print(f"  campo aperto (openness>65%):{n_open}  ({100*n_open/n:.0f}%)")
    print(f"Saved: {OUT}")


if __name__ == "__main__":
    main()
