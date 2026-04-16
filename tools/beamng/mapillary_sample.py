"""
PoC Mapillary: scarica alcune immagini street-view free lungo la SS17.

Setup (una tantum, ~1 minuto):
  1. Vai su https://www.mapillary.com/dashboard/developers
  2. Crea un "client app" (qualsiasi nome)
  3. Copia il "Client Token" (formato MLY|xxxxxxxxxxx)
  4. Export nella shell:
       set MAPILLARY_TOKEN=MLY|xxxxxxxxxxx      (Windows cmd)
       $env:MAPILLARY_TOKEN="MLY|xxxxxxxxxxx"   (PowerShell)

Poi lancia:
  python tools/beamng/mapillary_sample.py --samples 10

Output:
  output/mapillary/images/*.jpg          (immagini scaricate)
  output/mapillary/sample_meta.json      (id, lat/lon, timestamp, angle)

Il token e' anche configurabile via arg: --token MLY|xxx
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "output" / "mapillary"
IMG_DIR = OUT_DIR / "images"


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default=os.environ.get("MAPILLARY_TOKEN", ""))
    ap.add_argument("--samples", type=int, default=10,
                    help="Numero di immagini da scaricare (sparse lungo SS17)")
    ap.add_argument("--bbox-margin-m", type=float, default=50.0,
                    help="Margine in metri attorno al bbox centerline")
    return ap.parse_args()


def compute_bbox_from_centerline() -> tuple[float, float, float, float]:
    """Ritorna (lon_sw, lat_sw, lon_ne, lat_ne) del bbox della centerline
    da road_data.json."""
    rd = json.loads((ROOT / "road_data.json").read_text(encoding="utf-8"))
    cl = rd["centerline"]
    lats = [p["lat"] for p in cl]
    lons = [p["lon"] for p in cl]
    return (min(lons), min(lats), max(lons), max(lats))


def pick_sample_points(n: int) -> list[dict]:
    """Punti sparsi lungo la centerline (1 ogni n-esimo) con lat/lon."""
    rd = json.loads((ROOT / "road_data.json").read_text(encoding="utf-8"))
    cl = rd["centerline"]
    if n >= len(cl):
        return cl
    step = max(1, len(cl) // n)
    return [cl[i] for i in range(0, len(cl), step)][:n]


def main():
    args = parse_args()
    if not args.token:
        print("ERROR: serve un Mapillary access token.")
        print("Vedi le istruzioni in testa al file. In breve:")
        print("  1. https://www.mapillary.com/dashboard/developers")
        print("  2. Crea app -> copia 'Client Token'")
        print("  3. set MAPILLARY_TOKEN=MLY|... (o passa --token)")
        sys.exit(1)

    try:
        import requests
    except ImportError:
        print("ERROR: pip install requests")
        sys.exit(1)

    IMG_DIR.mkdir(parents=True, exist_ok=True)

    # Bounding box esteso della SS17
    lon_sw, lat_sw, lon_ne, lat_ne = compute_bbox_from_centerline()
    print(f"BBOX centerline: lon[{lon_sw:.4f}..{lon_ne:.4f}] "
          f"lat[{lat_sw:.4f}..{lat_ne:.4f}]")

    sample_points = pick_sample_points(args.samples)
    print(f"Punti campione (sparsi lungo la strada): {len(sample_points)}")

    # Per ogni punto campione, query Mapillary images entro 50m e prendi
    # la piu' vicina. Endpoint: https://graph.mapillary.com/images
    # Docs: https://www.mapillary.com/developer/api-documentation
    results = []
    for idx, cp in enumerate(sample_points):
        lat, lon = cp["lat"], cp["lon"]
        # Bbox piccolo intorno al punto (~100m)
        d_lat = 100.0 / 111000.0  # ~metri per grado lat
        d_lon = d_lat / max(0.1, math.cos(math.radians(lat)))
        bbox = f"{lon - d_lon},{lat - d_lat},{lon + d_lon},{lat + d_lat}"
        url = "https://graph.mapillary.com/images"
        params = {
            "access_token": args.token,
            "bbox": bbox,
            "fields": ("id,computed_geometry,thumb_1024_url,captured_at,"
                       "compass_angle,camera_type"),
            "limit": 10,
        }
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code != 200:
                print(f"  [{idx}] HTTP {r.status_code}: {r.text[:200]}")
                continue
            data = r.json().get("data", [])
            if not data:
                print(f"  [{idx}] no images near ({lat:.5f}, {lon:.5f})")
                continue
            # Prendi la piu' vicina al centerline point
            def dist2(im):
                geom = im.get("computed_geometry", {})
                coords = geom.get("coordinates", [0, 0])
                return (coords[1] - lat) ** 2 + (coords[0] - lon) ** 2
            best = min(data, key=dist2)
            url_img = best.get("thumb_1024_url")
            if not url_img:
                continue
            ir = requests.get(url_img, timeout=30)
            if ir.status_code == 200:
                fname = f"cl{idx:03d}_{best['id']}.jpg"
                (IMG_DIR / fname).write_bytes(ir.content)
                results.append({
                    "cl_index": idx,
                    "cl_lat": lat, "cl_lon": lon,
                    "img_id": best["id"],
                    "img_lat": best["computed_geometry"]["coordinates"][1],
                    "img_lon": best["computed_geometry"]["coordinates"][0],
                    "compass_angle": best.get("compass_angle"),
                    "captured_at": best.get("captured_at"),
                    "filename": fname,
                    "size_kb": len(ir.content) // 1024,
                })
                print(f"  [{idx}] OK {fname} ({len(ir.content)//1024} KB)")
            else:
                print(f"  [{idx}] img HTTP {ir.status_code}")
        except Exception as e:
            print(f"  [{idx}] ERROR: {e}")

    (OUT_DIR / "sample_meta.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8"
    )
    print(f"\nScaricate {len(results)}/{len(sample_points)} immagini")
    print(f"Metadata: {OUT_DIR / 'sample_meta.json'}")


if __name__ == "__main__":
    main()
