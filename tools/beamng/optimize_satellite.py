"""
Riduce satellite.png (38 MB, 8704x11008 PNG) a una texture BeamNG-friendly
per il terrain diffuse.

Strategy:
  1. Crop sul bbox del heightmap (il satellite spesso sconfina).
  2. Resize a SIZE x SIZE (default 4096) con bicubic.
  3. Salva come JPEG quality 90 (tipicamente 2-4 MB).

BeamNG Torque3D accetta JPEG per le texture terrain via ground cover / color
layer; file piu' piccoli -> caricamento molto piu' rapido (non deve
decomprimere 38 MB di PNG).

Output:
  output/beamng/satellite_diffuse.jpg
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
SATELLITE_PNG = ROOT / "output" / "satellite.png"
SATELLITE_META = ROOT / "output" / "satellite_bbox.json"
TERRAIN_INFO = ROOT / "output" / "beamng" / "terrain_info.json"
ROAD_DATA = ROOT / "road_data.json"
OUT_JPG = ROOT / "output" / "beamng" / "satellite_diffuse.jpg"

SIZE = 4096
JPEG_QUALITY = 90

R_EARTH = 6_378_137.0


def main() -> None:
    Image.MAX_IMAGE_PIXELS = None  # il PNG e' enorme
    if not SATELLITE_PNG.exists():
        print(f"manca {SATELLITE_PNG}, salto")
        return
    meta = json.loads(SATELLITE_META.read_text(encoding="utf-8"))
    tinfo = json.loads(TERRAIN_INFO.read_text(encoding="utf-8"))
    data = json.loads(ROAD_DATA.read_text(encoding="utf-8"))

    bbox_geo = meta["bbox_geo"]
    north = bbox_geo["north"]; south = bbox_geo["south"]
    west = bbox_geo["west"]; east = bbox_geo["east"]

    # Proiezione locale (stessa di blender_build / build_heightmap)
    cl = data["centerline"]
    lat0 = sum(p["lat"] for p in cl) / len(cl)
    lon0 = sum(p["lon"] for p in cl) / len(cl)
    kx = math.cos(math.radians(lat0)) * R_EARTH
    ky = R_EARTH

    def to_local(lat: float, lon: float) -> tuple[float, float]:
        return (math.radians(lon - lon0) * kx,
                math.radians(lat - lat0) * ky)

    def from_local(x: float, y: float) -> tuple[float, float]:
        lat = math.degrees(y / ky) + lat0
        lon = math.degrees(x / kx) + lon0
        return lat, lon

    # bbox del heightmap in coord locali -> lat/lon
    hm_half = tinfo["extent_m"] / 2.0
    # corner: SW, SE, NW, NE
    sw_lat, sw_lon = from_local(-hm_half, -hm_half)
    ne_lat, ne_lon = from_local(hm_half, hm_half)

    lat_min = min(sw_lat, ne_lat)
    lat_max = max(sw_lat, ne_lat)
    lon_min = min(sw_lon, ne_lon)
    lon_max = max(sw_lon, ne_lon)

    print(f"Heightmap bbox: lat [{lat_min:.5f}, {lat_max:.5f}], "
          f"lon [{lon_min:.5f}, {lon_max:.5f}]")
    print(f"Satellite bbox: lat [{south:.5f}, {north:.5f}], "
          f"lon [{west:.5f}, {east:.5f}]")

    img = Image.open(SATELLITE_PNG)
    W, H = img.size
    print(f"Satellite png: {W}x{H} ({SATELLITE_PNG.stat().st_size // 1024 // 1024} MB)")

    # Mappa lat/lon -> pixel lineare (approssimazione ok a scale di pochi km)
    def lat_to_py(lat: float) -> float:
        return (north - lat) / (north - south) * H

    def lon_to_px(lon: float) -> float:
        return (lon - west) / (east - west) * W

    # Calcola il pixel-bbox della finestra heightmap dentro al satellite
    px_l = max(0, int(math.floor(lon_to_px(lon_min))))
    px_r = min(W, int(math.ceil(lon_to_px(lon_max))))
    py_t = max(0, int(math.floor(lat_to_py(lat_max))))
    py_b = min(H, int(math.ceil(lat_to_py(lat_min))))
    print(f"Crop satellite: ({px_l},{py_t}) -> ({px_r},{py_b}) "
          f"[{px_r-px_l} x {py_b-py_t}]")

    # Crop + eventualmente square-ify (heightmap e' quadrato)
    crop = img.crop((px_l, py_t, px_r, py_b))
    # Padding se la zona heightmap cade fuori satellite (arriva meno satellite
    # del necessario): cornice nera.
    need_w = int(round((lon_max - lon_min) / (east - west) * W))
    need_h = int(round((lat_max - lat_min) / (north - south) * H))
    side_src = max(need_w, need_h)
    square = Image.new("RGB", (side_src, side_src), (0, 0, 0))
    square.paste(crop, ((side_src - crop.width) // 2,
                         (side_src - crop.height) // 2))

    print(f"Resize -> {SIZE}x{SIZE} bicubic ...")
    resized = square.resize((SIZE, SIZE), Image.BICUBIC)

    resized.save(OUT_JPG, "JPEG", quality=JPEG_QUALITY, optimize=True,
                  progressive=True)
    size_mb = OUT_JPG.stat().st_size / 1024 / 1024
    print(f"Scritto {OUT_JPG} ({size_mb:.2f} MB)")

    # Salva anche PNG 2048x2048: il TerrainMaterial di BeamNG preferisce PNG/DDS
    # al JPEG. 2048 va bene come dimensione di texture per terrain.
    out_png = OUT_JPG.with_suffix(".png")
    resized_small = resized.resize((2048, 2048), Image.BICUBIC)
    resized_small.save(out_png, "PNG", optimize=True)
    size_mb_png = out_png.stat().st_size / 1024 / 1024
    print(f"Scritto {out_png} ({size_mb_png:.2f} MB)")


if __name__ == "__main__":
    main()
