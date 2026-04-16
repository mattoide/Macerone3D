"""
Analizza output/satellite.png e per ogni punto della centerline determina
se sull'asfalto c'è davvero la linea di mezzeria bianca.

Logica: per ogni punto del tracciato proietto in pixel, calcolo il vettore
tangente in pixel-space, poi confronto la luminosità media SUL centro della
strada con quella a ±1.8 px di lato. Se on-center è significativamente
più chiaro → linea presente.

Output: output/line_marks.json  { "has_center_line": [bool, ...] }
        (stesso ordinamento di road_data.json["centerline"])

Dipendenze: pillow, numpy
    pip install pillow numpy
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

try:
    from PIL import Image
    import numpy as np
except ImportError:
    print("Serve: pip install pillow numpy", file=sys.stderr)
    sys.exit(1)

ROOT = Path(__file__).parent
OUT = ROOT / "output"
ROAD_PATH = ROOT / "road_data.json"
SAT_PATH = OUT / "satellite.png"
META_PATH = OUT / "satellite_bbox.json"

# Soglie tarate per ESRI World Imagery zoom 17 in zone montane
ON_OFF_DELTA = 8.0       # MAX(on-line) - MEAN(off-line) min per dire "linea"
LATERAL_OFF_PX = 2.5     # offset perpendicolare per il "background asfalto"
SAMPLES_ALONG = 9        # campionamenti lungo la tangente
SMOOTH_WIN = 11          # finestra moving-average sul flag finale


def main():
    if not (SAT_PATH.exists() and META_PATH.exists() and ROAD_PATH.exists()):
        print("Mancano satellite.png / satellite_bbox.json / road_data.json",
              file=sys.stderr)
        sys.exit(1)

    print("Carico satellite.png...")
    img = Image.open(SAT_PATH).convert("RGB")
    arr = np.asarray(img)
    H, W, _ = arr.shape
    gray = arr.mean(axis=2)
    print(f"  {W}x{H} px")

    meta = json.loads(META_PATH.read_text())
    bb = meta["bbox_geo"]
    lat_per_px = (bb["north"] - bb["south"]) / H
    lon_per_px = (bb["east"] - bb["west"]) / W

    cl = json.loads(ROAD_PATH.read_text())["centerline"]
    n = len(cl)
    print(f"Centerline: {n} punti")

    def latlon_to_pix(lat, lon):
        return ((lon - bb["west"]) / lon_per_px,
                (bb["north"] - lat) / lat_per_px)

    flags = []
    deltas = []
    for i in range(n):
        p = cl[i]
        # tangente in pixel-space dal punto seguente (o precedente all'ultimo)
        if i < n - 1:
            q = cl[i + 1]
        else:
            q = cl[i - 1]
        px1, py1 = latlon_to_pix(p["lat"], p["lon"])
        px2, py2 = latlon_to_pix(q["lat"], q["lon"])
        if i == n - 1:
            px1, py1, px2, py2 = px2, py2, px1, py1
        dx, dy = px2 - px1, py2 - py1
        L = math.hypot(dx, dy)
        if L < 0.1:
            flags.append(False); deltas.append(0); continue
        tx, ty = dx / L, dy / L
        nx, ny = -ty, tx

        # raccolgo on-line (sulla mezzeria, ±0.5 px laterali) e off-line (asfalto)
        on_vals = []
        off_vals = []
        for k in range(-(SAMPLES_ALONG // 2), SAMPLES_ALONG // 2 + 1):
            bx = px1 + tx * k * 0.6
            by = py1 + ty * k * 0.6
            # patch on-line: 3 pixel laterali alla mezzeria
            for off in (-0.5, 0.0, 0.5):
                ix = int(round(bx + nx * off))
                iy = int(round(by + ny * off))
                if 0 <= ix < W and 0 <= iy < H:
                    on_vals.append(gray[iy, ix])
            for off in (-LATERAL_OFF_PX, +LATERAL_OFF_PX):
                ix = int(round(bx + nx * off))
                iy = int(round(by + ny * off))
                if 0 <= ix < W and 0 <= iy < H:
                    off_vals.append(gray[iy, ix])
        if not on_vals or not off_vals:
            flags.append(False); deltas.append(0); continue
        on_max = max(on_vals)            # picco di luminosità sulla linea
        off_mean = sum(off_vals) / len(off_vals)
        delta = float(on_max - off_mean)
        deltas.append(delta)
        flags.append(bool(delta > ON_OFF_DELTA))

    # smoothing: maggioranza in finestra → riduce il rumore single-pixel
    smoothed = []
    half = SMOOTH_WIN // 2
    for i in range(n):
        i0, i1 = max(0, i - half), min(n, i + half + 1)
        window = flags[i0:i1]
        smoothed.append(sum(window) > len(window) / 2)

    n_yes = sum(smoothed)
    print(f"Linea rilevata in {n_yes}/{n} punti ({100 * n_yes / n:.0f}%)")
    out_path = OUT / "line_marks.json"
    out_path.write_text(json.dumps({
        "has_center_line": smoothed,
        "deltas": deltas,
        "params": {"on_off_delta": ON_OFF_DELTA,
                   "smooth_window": SMOOTH_WIN},
    }))
    print(f"Scritto {out_path}")


if __name__ == "__main__":
    main()
