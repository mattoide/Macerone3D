"""
Genera heightmap PNG 16-bit 4096x4096 per BeamNG a partire da road_data.json.

Strategy:
- prende la griglia DEM EU-DEM 25m esistente (168x134 @ 60m = 10.08 x 8.04 km)
  gia' campionata da fetch_road.py lungo e attorno alla centerline
- upsample bicubico a 4096x4096 @ 3 m/pixel = 12288 m (12.3 km) quadrato,
  centrato sull'origine locale (centroide della centerline)
- zone fuori dal DEM vengono riempite con l'elevazione mediana (plateau smussato)
- pixel 0 -> ELEV_MIN m, pixel 65535 -> ELEV_MAX m

Output:
  output/beamng/heightmap.png          (PNG 16-bit greyscale)
  output/beamng/terrain_info.json      (scala, origine, min/max elev)

Il heightmap e' compatibile con il "Terrain and Road Importer" di BeamNG.drive
(World Editor -> Tools -> Terrain and Road Importer).
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
from PIL import Image

HEIGHTMAP_SIZE = 4096
METERS_PER_PIXEL = 3.0
ELEV_MIN_M = 0.0
ELEV_MAX_M = 1200.0

R_EARTH = 6_378_137.0

ROOT = Path(__file__).resolve().parents[2]
ROAD_DATA = ROOT / "road_data.json"
OUT_DIR = ROOT / "output" / "beamng"


def project_factory(lat0: float, lon0: float):
    kx = math.cos(math.radians(lat0)) * R_EARTH
    ky = R_EARTH

    def project(lat: float, lon: float) -> tuple[float, float]:
        return (math.radians(lon - lon0) * kx,
                math.radians(lat - lat0) * ky)

    return project, kx, ky


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data = json.loads(ROAD_DATA.read_text(encoding="utf-8"))
    terrain = data["terrain"]
    rows = int(terrain["rows"])
    cols = int(terrain["cols"])
    lat_s, lon_w, lat_n, lon_e = terrain["bbox"]
    grid = np.array(terrain["grid"], dtype=np.float32)
    assert grid.shape == (rows, cols), grid.shape

    cl = data["centerline"]
    lat0 = sum(p["lat"] for p in cl) / len(cl)
    lon0 = sum(p["lon"] for p in cl) / len(cl)
    project, _, _ = project_factory(lat0, lon0)

    sw_x, sw_y = project(lat_s, lon_w)
    ne_x, ne_y = project(lat_n, lon_e)
    print(f"DEM bbox locale: SW=({sw_x:.1f}, {sw_y:.1f}) NE=({ne_x:.1f}, {ne_y:.1f})")
    print(f"DEM esteso: {ne_x - sw_x:.0f} m x {ne_y - sw_y:.0f} m "
          f"(grid {rows}x{cols} @ {terrain['step_m']} m)")

    hm_half = HEIGHTMAP_SIZE * METERS_PER_PIXEL / 2.0
    x_min, x_max = -hm_half, hm_half
    y_min, y_max = -hm_half, hm_half
    print(f"Heightmap BeamNG: {HEIGHTMAP_SIZE}x{HEIGHTMAP_SIZE} @ "
          f"{METERS_PER_PIXEL} m/pixel = {2*hm_half:.0f} m quadrato")

    # Flip grid verticalmente: in fetch_road.py grid[0] corrisponde a lat_s (sud),
    # ma nel PNG la row 0 e' il top (nord). Quindi flippiamo.
    grid_nfirst = np.flipud(grid)

    # Upsample bicubico del DEM alla risoluzione target (solo la porzione che copre).
    dem_x0_px = int(round((sw_x - x_min) / METERS_PER_PIXEL))
    dem_x1_px = int(round((ne_x - x_min) / METERS_PER_PIXEL))
    dem_y0_px = int(round((y_max - ne_y) / METERS_PER_PIXEL))  # top (nord) row
    dem_y1_px = int(round((y_max - sw_y) / METERS_PER_PIXEL))  # bottom (sud) row
    dem_w = max(1, dem_x1_px - dem_x0_px)
    dem_h = max(1, dem_y1_px - dem_y0_px)
    print(f"DEM mappato in pixel target: ({dem_x0_px},{dem_y0_px}) "
          f"-> ({dem_x1_px},{dem_y1_px}) [{dem_w} x {dem_h}]")

    dem_img = Image.fromarray(grid_nfirst.astype(np.float32), mode="F")
    dem_upsampled = np.array(dem_img.resize((dem_w, dem_h), Image.BICUBIC),
                              dtype=np.float32)

    median_elev = float(np.median(grid))
    hm = np.full((HEIGHTMAP_SIZE, HEIGHTMAP_SIZE), median_elev, dtype=np.float32)

    y0c = max(0, dem_y0_px); y1c = min(HEIGHTMAP_SIZE, dem_y1_px)
    x0c = max(0, dem_x0_px); x1c = min(HEIGHTMAP_SIZE, dem_x1_px)
    dy0 = y0c - dem_y0_px; dy1 = dy0 + (y1c - y0c)
    dx0 = x0c - dem_x0_px; dx1 = dx0 + (x1c - x0c)
    hm[y0c:y1c, x0c:x1c] = dem_upsampled[dy0:dy1, dx0:dx1]

    # Feather: dissolvenza ai bordi del bbox DEM per evitare scalini nel plateau
    feather_px = 64
    for margin in range(1, feather_px + 1):
        alpha = margin / (feather_px + 1)
        # top
        row = dem_y0_px - margin
        if 0 <= row < HEIGHTMAP_SIZE:
            hm[row, x0c:x1c] = hm[row, x0c:x1c] * (1 - alpha) + median_elev * alpha
        # bottom
        row = dem_y1_px + margin - 1
        if 0 <= row < HEIGHTMAP_SIZE:
            hm[row, x0c:x1c] = hm[row, x0c:x1c] * (1 - alpha) + median_elev * alpha
        # left
        col = dem_x0_px - margin
        if 0 <= col < HEIGHTMAP_SIZE:
            hm[y0c:y1c, col] = hm[y0c:y1c, col] * (1 - alpha) + median_elev * alpha
        # right
        col = dem_x1_px + margin - 1
        if 0 <= col < HEIGHTMAP_SIZE:
            hm[y0c:y1c, col] = hm[y0c:y1c, col] * (1 - alpha) + median_elev * alpha

    real_min = float(hm.min())
    real_max = float(hm.max())
    print(f"Elevazione nel heightmap: min={real_min:.1f} m max={real_max:.1f} m")
    if real_min < ELEV_MIN_M or real_max > ELEV_MAX_M:
        print(f"ATTENZIONE: elevazioni fuori da [{ELEV_MIN_M}, {ELEV_MAX_M}] m!")

    hm_norm = np.clip((hm - ELEV_MIN_M) / (ELEV_MAX_M - ELEV_MIN_M), 0.0, 1.0)
    hm_u16 = (hm_norm * 65535.0 + 0.5).astype(np.uint16)

    out_png = OUT_DIR / "heightmap.png"
    Image.fromarray(hm_u16, mode="I;16").save(out_png, compress_level=0)
    print(f"Scritto {out_png}  shape={hm_u16.shape}  dtype={hm_u16.dtype}")

    info = {
        "heightmap_png": "heightmap.png",
        "size_px": HEIGHTMAP_SIZE,
        "meters_per_pixel": METERS_PER_PIXEL,
        "extent_m": HEIGHTMAP_SIZE * METERS_PER_PIXEL,
        "elevation_min_m": ELEV_MIN_M,
        "elevation_max_m": ELEV_MAX_M,
        "terrain_height_scale_m": ELEV_MAX_M - ELEV_MIN_M,
        "projection_origin_geo": {"lat": lat0, "lon": lon0},
        "terrain_origin_local_m": {"x": x_min, "y": y_min},
        "beamng_import_hints": {
            "tool": "World Editor -> Tools -> Terrain and Road Importer",
            "height_scale_m": ELEV_MAX_M - ELEV_MIN_M,
            "meters_per_pixel": METERS_PER_PIXEL,
            "square_size_m": HEIGHTMAP_SIZE * METERS_PER_PIXEL,
            "heightmap_format": "PNG 16-bit greyscale non-compressed",
        },
    }
    (OUT_DIR / "terrain_info.json").write_text(
        json.dumps(info, indent=2), encoding="utf-8"
    )
    print(f"Scritto {OUT_DIR / 'terrain_info.json'}")


if __name__ == "__main__":
    main()
