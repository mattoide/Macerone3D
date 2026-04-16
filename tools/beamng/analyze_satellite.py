"""
Analizza la satellite ESRI zoom 17 lungo la centerline SS17 per classificare
i bordi strada in 3 categorie (sx e dx separati):

- "paved": guardrail, muretto, banchina ampia (grigio/beige, bassa saturazione)
- "grass": vegetazione bassa / campo (verde uniforme, media luminosita')
- "tree":  vegetazione alta / bosco (verde scuro, alta varianza da ombre)

Output: output/road_conditions.json con una classificazione per ogni
centerline point. Usato da build_full_mod.py per condizionare:
- Dove c'e' "paved" sul bordo -> skip clutter procedurale (c'e' gia'
  guardrail/muretto), eventualmente aggiungere StoneWall procedurale.
- Dove c'e' "grass" -> clutter leggero.
- Dove c'e' "tree" -> alberi extra procedurali.

Uso:
  python tools/beamng/analyze_satellite.py
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]


def project_factory(lat0: float, lon0: float):
    R = 6378137.0
    kx = math.cos(math.radians(lat0)) * R
    ky = R

    def project(lat, lon):
        return (math.radians(lon - lon0) * kx,
                math.radians(lat - lat0) * ky)

    def inverse(x, y):
        lat = math.degrees(y / ky) + lat0
        lon = math.degrees(x / kx) + lon0
        return (lat, lon)

    return project, inverse


def pix_from_latlon(lat: float, lon: float, bbox: dict,
                      W: int, H: int) -> tuple[int, int]:
    u = (lon - bbox["west"]) / (bbox["east"] - bbox["west"])
    v = (bbox["north"] - lat) / (bbox["north"] - bbox["south"])
    return (int(u * W), int(v * H))


def classify_patch(patch_rgb: np.ndarray) -> dict:
    """Ritorna features base del patch: mean_rgb, saturation, hue_green_score,
    std (varianza). Usato poi per la classe."""
    flat = patch_rgb.reshape(-1, 3).astype(np.float32) / 255.0
    mean = flat.mean(axis=0)
    r, g, b = float(mean[0]), float(mean[1]), float(mean[2])
    mx = max(r, g, b)
    mn = min(r, g, b)
    sat = (mx - mn) / (mx + 1e-6)
    lum = (r + g + b) / 3.0
    # hue_green: quanto il verde domina su rosso e blu
    green_score = g - (r + b) / 2.0  # >0 = tende al verde
    std = float(flat.std())
    return {
        "r": r, "g": g, "b": b,
        "sat": sat, "lum": lum,
        "green_score": green_score,
        "std": std,
    }


def classify_side(feats: dict) -> str:
    """Classifica il bordo basandosi sulle features del patch.
    Ritorna: "paved" | "grass" | "tree" | "unknown"."""
    # Paved: saturazione bassa (grigio), luminosita' medio-alta
    if feats["sat"] < 0.10 and feats["lum"] > 0.35:
        return "paved"
    # Tree: verde + luminosita' bassa + alta varianza (ombre chioma)
    if feats["green_score"] > 0.03 and feats["lum"] < 0.40 and feats["std"] > 0.10:
        return "tree"
    # Grass: verde + luminosita' media + varianza bassa/media
    if feats["green_score"] > 0.02 and feats["lum"] > 0.30:
        return "grass"
    # Fallback: se il verde e' marginale ma saturazione alta
    if feats["green_score"] > 0.0:
        return "grass"
    return "unknown"


def main():
    sat_path = ROOT / "output" / "satellite.png"
    bbox_path = ROOT / "output" / "satellite_bbox.json"
    road_data_path = ROOT / "road_data.json"
    cl_csv_path = ROOT / "output" / "centerline.csv"
    for p in (sat_path, bbox_path, road_data_path, cl_csv_path):
        if not p.exists():
            print(f"Missing: {p}")
            return

    Image.MAX_IMAGE_PIXELS = None
    im = Image.open(sat_path).convert("RGB")
    W, H = im.size
    sat = np.asarray(im)
    bbox = json.loads(bbox_path.read_text(encoding="utf-8"))["bbox_geo"]
    print(f"Satellite: {W}x{H} (zoom 17 ESRI World Imagery)")

    rd = json.loads(road_data_path.read_text(encoding="utf-8"))
    cl_geo = rd["centerline"]
    lat0 = sum(p["lat"] for p in cl_geo) / len(cl_geo)
    lon0 = sum(p["lon"] for p in cl_geo) / len(cl_geo)
    project, inverse = project_factory(lat0, lon0)

    # Centerline in coord locali Blender (x, y)
    cl_xy = []
    with cl_csv_path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            cl_xy.append((float(r["x"]), float(r["y"]), float(r["z"])))
    print(f"Centerline points: {len(cl_xy)}")

    # Parametri zone laterali (in metri da centerline):
    ROAD_HALF_M = 3.0      # strada <3m centrale, skip
    NEAR_MIN_M = 4.0       # primo sample a 4m (oltre banchina)
    NEAR_MAX_M = 7.0       # fino a 7m
    FAR_MIN_M = 8.0        # zona "far" 8..13m
    FAR_MAX_M = 13.0
    PATCH_PX = 3           # patch 3x3 sulla satellite

    results = []
    counts = {"paved": 0, "grass": 0, "tree": 0, "unknown": 0}

    for i, (x, y, z) in enumerate(cl_xy):
        # Direzione tangente: usa cl[i-1] e cl[i+1]
        i_prev = max(0, i - 1)
        i_next = min(len(cl_xy) - 1, i + 1)
        dx = cl_xy[i_next][0] - cl_xy[i_prev][0]
        dy = cl_xy[i_next][1] - cl_xy[i_prev][1]
        d = math.hypot(dx, dy)
        if d < 1e-6:
            continue
        tx, ty = dx / d, dy / d
        # Normale (perpendicolare): ruota 90° a sx
        nx, ny = -ty, tx

        def sample_at(off_m: float) -> dict | None:
            sx = x + nx * off_m
            sy = y + ny * off_m
            lat, lon = inverse(sx, sy)
            px, py = pix_from_latlon(lat, lon, bbox, W, H)
            half = PATCH_PX // 2
            if not (half <= px < W - half and half <= py < H - half):
                return None
            patch = sat[py - half:py + half + 1, px - half:px + half + 1]
            return classify_patch(patch)

        def classify_zone(offsets: list[float]) -> str:
            # Aggrega samples e classifica con la mediana
            all_feats = []
            for off in offsets:
                f = sample_at(off)
                if f is not None:
                    all_feats.append(f)
            if not all_feats:
                return "unknown"
            median_feats = {
                k: float(np.median([af[k] for af in all_feats]))
                for k in all_feats[0]
            }
            return classify_side(median_feats)

        # Lato sinistro: samples negativi (normale punta a sx = +)
        # In questa convenzione nx ruotato 90° è "sinistra" guardando avanti
        near_offsets = [NEAR_MIN_M, (NEAR_MIN_M + NEAR_MAX_M) / 2, NEAR_MAX_M]
        far_offsets = [FAR_MIN_M, (FAR_MIN_M + FAR_MAX_M) / 2, FAR_MAX_M]
        left_near = classify_zone(near_offsets)
        left_far = classify_zone(far_offsets)
        right_near = classify_zone([-o for o in near_offsets])
        right_far = classify_zone([-o for o in far_offsets])

        for c in (left_near, left_far, right_near, right_far):
            counts[c] = counts.get(c, 0) + 1

        results.append({
            "index": i,
            "left_near": left_near,
            "left_far": left_far,
            "right_near": right_near,
            "right_far": right_far,
        })

    out_path = ROOT / "output" / "road_conditions.json"
    payload = {
        "format": "macerone_road_conditions_v1",
        "zones": {
            "near_m": [NEAR_MIN_M, NEAR_MAX_M],
            "far_m": [FAR_MIN_M, FAR_MAX_M],
        },
        "classes": ["paved", "grass", "tree", "unknown"],
        "summary": counts,
        "points": results,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    total = sum(counts.values())
    print(f"\nClassificazione {len(results)} centerline points x 4 zone "
          f"= {total} samples")
    for k, v in counts.items():
        pct = 100 * v / total if total else 0
        print(f"  {k:<10} {v:>6}  ({pct:5.1f}%)")
    print(f"\nOutput: {out_path}")


if __name__ == "__main__":
    main()
