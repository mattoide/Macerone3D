"""
Scarica un'immagine satellitare (ESRI World Imagery, gratuita, no API key)
che copre la bbox del corridoio della strada e produce:
  - output/satellite.png        (mosaico grande scala)
  - output/satellite_bbox.json  (bbox geografica del mosaico, per UV mapping)

Uso:
  python fetch_satellite.py           # usa la bbox del road_data.json corrente
  python fetch_satellite.py --zoom 17

Nota legale: ESRI World Imagery è gratuito per uso personale/educational,
attribuzione: "Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community".
Non ridistribuire i tile grezzi.

Dipendenze: requests, pillow
    pip install requests pillow
"""
from __future__ import annotations

import argparse
import io
import json
import math
import sys
import time
from pathlib import Path

import requests

try:
    from PIL import Image
except ImportError:
    print("Serve Pillow: pip install pillow", file=sys.stderr)
    sys.exit(1)

ROOT = Path(__file__).parent
DATA_PATH = ROOT / "road_data.json"
OUT_DIR = ROOT / "output"
OUT_DIR.mkdir(exist_ok=True)
TILE_CACHE = ROOT / ".tile_cache"
TILE_CACHE.mkdir(exist_ok=True)

# ESRI World Imagery (public tiles, no key)
ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
TILE_SIZE = 256

CORRIDOR_MARGIN_M = 150.0   # margine oltre il corridoio stretto


def deg2tile(lat, lon, z):
    n = 2 ** z
    xtile = int((lon + 180.0) / 360.0 * n)
    ytile = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
    return xtile, ytile


def tile2deg(x, y, z):
    n = 2 ** z
    lon = x / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * y / n)))
    return math.degrees(lat_rad), lon


def fetch_tile(x, y, z, session):
    cache = TILE_CACHE / f"esri_{z}_{x}_{y}.png"
    if cache.exists() and cache.stat().st_size > 200:
        return Image.open(cache)
    url = ESRI.format(z=z, x=x, y=y)
    for attempt in range(4):
        try:
            r = session.get(url, timeout=30,
                            headers={"User-Agent": "Macerozz-road-builder/1.0"})
            if r.status_code == 200 and len(r.content) > 200:
                cache.write_bytes(r.content)
                return Image.open(io.BytesIO(r.content))
            time.sleep(1 + attempt)
        except requests.RequestException:
            time.sleep(1 + attempt)
    raise RuntimeError(f"Impossibile scaricare tile {z}/{x}/{y}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zoom", type=int, default=17,
                    help="Zoom level (default 17 ≈ 1 m/pixel)")
    args = ap.parse_args()

    if not DATA_PATH.exists():
        print(f"Mancante {DATA_PATH}. Esegui prima fetch_road.py", file=sys.stderr)
        sys.exit(1)
    data = json.loads(DATA_PATH.read_text())

    # Calcolo bbox dal corridoio: per ogni punto centerline ± CORRIDOR_MARGIN_M
    pts = [(p["lat"], p["lon"]) for p in data["centerline"]]
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    lat0 = (min(lats) + max(lats)) / 2
    dlat = CORRIDOR_MARGIN_M / 111_320.0
    dlon = CORRIDOR_MARGIN_M / (111_320.0 * math.cos(math.radians(lat0)))
    s, w = min(lats) - dlat, min(lons) - dlon
    n, e = max(lats) + dlat, max(lons) + dlon

    z = args.zoom
    x0, y1 = deg2tile(n, w, z)
    x1, y0 = deg2tile(s, e, z)
    x_lo, x_hi = min(x0, x1), max(x0, x1)
    y_lo, y_hi = min(y0, y1), max(y0, y1)

    # Filtro: tengo solo i tile il cui centro è a <= 200 m da un punto centerline
    # Uso una griglia di bucket sui tile per velocità
    tile_set = set()
    max_dist = 250.0  # m
    for (lat, lon) in pts:
        tx, ty = deg2tile(lat, lon, z)
        # il raggio in tile: 1 tile al nostro zoom ≈ 230 m a lat 41°
        tile_size_m = 40075016.7 * math.cos(math.radians(lat)) / (2 ** z)
        r = max(1, int(max_dist / tile_size_m) + 1)
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                xx = tx + dx; yy = ty + dy
                if x_lo <= xx <= x_hi and y_lo <= yy <= y_hi:
                    tile_set.add((xx, yy))

    ntiles = len(tile_set)
    print(f"BBox: {s:.5f},{w:.5f} -> {n:.5f},{e:.5f}")
    print(f"Zoom {z}: {ntiles} tile filtrati dal corridoio "
          f"(~{ntiles * 35 / 1024:.1f} MB stimati)")

    if ntiles > 3000:
        print(f"Troppi tile ({ntiles}). Riduci zoom.", file=sys.stderr)
        sys.exit(2)

    session = requests.Session()
    W = (x_hi - x_lo + 1) * TILE_SIZE
    H = (y_hi - y_lo + 1) * TILE_SIZE
    # sfondo verde scuro dove non abbiamo dati
    img = Image.new("RGB", (W, H), (40, 50, 30))
    done = 0
    t0 = time.time()
    for (x, y) in sorted(tile_set):
        tile = fetch_tile(x, y, z, session)
        px = (x - x_lo) * TILE_SIZE
        py = (y - y_lo) * TILE_SIZE
        img.paste(tile, (px, py))
        done += 1
        if done % 25 == 0 or done == ntiles:
            print(f"  {done}/{ntiles}  ({time.time() - t0:.1f}s)")

    # Bbox geografico esatto del mosaico (top-left + bottom-right tile corners)
    tl_lat, tl_lon = tile2deg(x_lo, y_lo, z)        # NW
    br_lat, br_lon = tile2deg(x_hi + 1, y_hi + 1, z)  # SE

    out_png = OUT_DIR / "satellite.png"
    img.save(out_png, "PNG", optimize=True)
    (OUT_DIR / "satellite_bbox.json").write_text(json.dumps({
        "zoom": z,
        "tiles_x": [x_lo, x_hi],
        "tiles_y": [y_lo, y_hi],
        "bbox_geo": {"north": tl_lat, "west": tl_lon,
                     "south": br_lat, "east": br_lon},
        "size_px": [W, H],
        "attribution": "Source: Esri, Maxar, Earthstar Geographics",
    }, indent=2))
    print(f"Scritto {out_png} ({W}x{H} px, {out_png.stat().st_size / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    main()
