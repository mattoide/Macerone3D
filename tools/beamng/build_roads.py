"""
Genera il file roads.json per il Terrain and Road Importer di BeamNG.drive.

Input:
  road_data.json   (centerline + other_roads in lat/lon, prodotto da fetch_road.py)
  output/beamng/terrain_info.json  (origine e scala del terrain BeamNG)

Output:
  output/beamng/roads.json

Il file contiene una lista di strade, ciascuna con nodi ordinati {x, y, z, width}
in coordinate locali del terrain BeamNG (origine al corner SW, X=est, Y=nord).
Z e' volutamente 0: il Terrain Importer deforma il terrain per fare combaciare
la strada con l'elevazione (lo fa lui).

Formato JSON (best-guess basato sulla doc ufficiale):
{
  "roads": [
    {
      "name": "SS17",
      "material": "m_asphalt_road_damaged",
      "nodes": [{"x": ..., "y": ..., "z": 0, "width": 7.5}, ...]
    }, ...
  ]
}
"""
from __future__ import annotations

import json
import math
from pathlib import Path

R_EARTH = 6_378_137.0

ROOT = Path(__file__).resolve().parents[2]
ROAD_DATA = ROOT / "road_data.json"
TERRAIN_INFO = ROOT / "output" / "beamng" / "terrain_info.json"
OUT_FILE = ROOT / "output" / "beamng" / "roads.json"

SS17_WIDTH_M = 7.5
OTHER_ROAD_WIDTH_DEFAULT = 5.0
MAIN_MATERIAL = "m_asphalt_road_damaged"
SIDE_MATERIAL = "m_asphalt_road_damaged_small"

# Se la centerline e' troppo densa (ogni 15 m), sfoltiamo un po' per non avere
# decine di migliaia di nodi.
DECIMATE_MAIN_STEP_M = 8.0
DECIMATE_OTHER_STEP_M = 15.0

# Filtro: scarta other_roads esterne al terrain BeamNG
OTHER_ROAD_MIN_POINTS = 4


def project_factory(lat0: float, lon0: float):
    kx = math.cos(math.radians(lat0)) * R_EARTH
    ky = R_EARTH

    def project(lat: float, lon: float) -> tuple[float, float]:
        return (math.radians(lon - lon0) * kx,
                math.radians(lat - lat0) * ky)

    return project


def decimate_by_distance(pts: list[tuple[float, float]],
                         min_step_m: float) -> list[tuple[float, float]]:
    if len(pts) <= 2:
        return pts
    out = [pts[0]]
    for p in pts[1:-1]:
        dx = p[0] - out[-1][0]
        dy = p[1] - out[-1][1]
        if dx * dx + dy * dy >= min_step_m * min_step_m:
            out.append(p)
    out.append(pts[-1])
    return out


def in_terrain(x: float, y: float, x_min: float, y_min: float,
               size_m: float) -> bool:
    return 0 <= (x - x_min) <= size_m and 0 <= (y - y_min) <= size_m


def main() -> None:
    data = json.loads(ROAD_DATA.read_text(encoding="utf-8"))
    tinfo = json.loads(TERRAIN_INFO.read_text(encoding="utf-8"))

    size_m = tinfo["extent_m"]
    x_min = tinfo["terrain_origin_local_m"]["x"]
    y_min = tinfo["terrain_origin_local_m"]["y"]
    lat0 = tinfo["projection_origin_geo"]["lat"]
    lon0 = tinfo["projection_origin_geo"]["lon"]
    project = project_factory(lat0, lon0)

    roads: list[dict] = []

    # Main road: SS17
    cl = data["centerline"]
    main_pts: list[tuple[float, float]] = []
    for p in cl:
        lx, ly = project(p["lat"], p["lon"])
        # Trasla nel sistema terrain BeamNG (origin = SW corner)
        bx = lx - x_min
        by = ly - y_min
        main_pts.append((bx, by))
    main_pts = decimate_by_distance(main_pts, DECIMATE_MAIN_STEP_M)
    road_width = float(data.get("road", {}).get("width_m") or SS17_WIDTH_M)
    roads.append({
        "name": "SS17",
        "material": MAIN_MATERIAL,
        "nodes": [
            {"x": round(x, 3), "y": round(y, 3), "z": 0.0,
             "width": round(road_width, 2)}
            for (x, y) in main_pts
        ],
    })
    print(f"SS17: {len(main_pts)} nodi  width={road_width} m")

    # Other roads (strade secondarie da OSM)
    keep = 0
    skip_outside = 0
    skip_short = 0
    for idx, way in enumerate(data.get("other_roads", [])):
        coords = way.get("coords") or []
        if len(coords) < OTHER_ROAD_MIN_POINTS:
            skip_short += 1
            continue
        pts: list[tuple[float, float]] = []
        for lat, lon in coords:
            lx, ly = project(lat, lon)
            pts.append((lx - x_min, ly - y_min))
        pts = [(x, y) for (x, y) in pts if in_terrain(x, y, 0, 0, size_m)]
        if len(pts) < OTHER_ROAD_MIN_POINTS:
            skip_outside += 1
            continue
        pts = decimate_by_distance(pts, DECIMATE_OTHER_STEP_M)
        width = way.get("width_m") or OTHER_ROAD_WIDTH_DEFAULT
        roads.append({
            "name": way.get("name") or f"secondary_{idx}",
            "material": SIDE_MATERIAL,
            "nodes": [
                {"x": round(x, 3), "y": round(y, 3), "z": 0.0,
                 "width": round(float(width), 2)}
                for (x, y) in pts
            ],
        })
        keep += 1
    print(f"other_roads: {keep} tenute, {skip_short} scartate (troppo corte), "
          f"{skip_outside} fuori terrain")

    payload = {
        "format": "beamng_terrain_road_importer_v1",
        "coordinate_space": "terrain_local (origin=SW corner, X=east, Y=north, Z unused)",
        "terrain_size_m": size_m,
        "roads": roads,
    }
    OUT_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Scritto {OUT_FILE}  ({len(roads)} strade totali)")


if __name__ == "__main__":
    main()
