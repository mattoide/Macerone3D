"""
Genera mesh procedurale con gli edifici OSM mancanti dal mesh Blender.

Il blender_build.py esporta solo ~116 edifici dei 191 che OSM conosce entro
250m dalla SS17 (filtro interno Blender su footprint minimo, poligoni
invalidi, ecc). Qui identifichiamo i mancanti matchando centroidi e
generiamo mesh semplici (walls estrusi + tetto piatto) per completarli.

Per ogni building OSM missing:
- Proietto coords lat/lon -> Blender local coords
- Campiono DEM heightmap per terrain_z sotto il building
- Estrudo il footprint poligono verticalmente per height (default 6m se
  mancante)
- Genero walls (quads per ogni edge) + roof (fan triangulation)

Output: output/beamng/mod/levels/macerone/art/shapes/macerone_extra_buildings.obj
Il main build_full_mod.py lo converte in DAE e lo aggiunge come TSStatic.
"""
from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "output" / "beamng" / "mod" / "levels" / "macerone" / "art" / "shapes"

MAX_DIST_M = 250.0           # solo edifici entro 250m dalla SS17
MATCH_RADIUS_M = 15.0         # edificio OSM gia' presente nel mesh se centroide <15m
DEFAULT_HEIGHT_M = 6.0        # altezza default se non specificata in OSM


def project_factory(lat0: float, lon0: float):
    R = 6378137.0
    kx = math.cos(math.radians(lat0)) * R
    ky = R
    def project(lat, lon):
        return (math.radians(lon - lon0) * kx,
                math.radians(lat - lat0) * ky)
    return project


def parse_world_buildings_centroids(world_obj: Path) -> list[tuple[float, float]]:
    """Estrae centroidi XY dei face in Buildings_Roofs (ognuno = un edificio)."""
    verts = []
    centroids = []
    current = None
    in_roofs = False
    with world_obj.open() as f:
        for line in f:
            if line.startswith("v "):
                p = line.split()
                verts.append((float(p[1]), float(p[2]), float(p[3])))
            elif line.startswith("o "):
                current = line.split(maxsplit=1)[1].strip()
                in_roofs = (current == "Buildings_Roofs")
            elif line.startswith("f ") and in_roofs:
                toks = line.split()[1:]
                idx = [int(t.split("/")[0]) - 1 for t in toks]
                xs = [verts[i][0] for i in idx if 0 <= i < len(verts)]
                ys = [verts[i][1] for i in idx if 0 <= i < len(verts)]
                if xs:
                    centroids.append((sum(xs) / len(xs), sum(ys) / len(ys)))
    return centroids


def sample_heightmap_z(hm: np.ndarray, x: float, y: float,
                        max_h: float, z_offset: float,
                        extent: float = 12288.0) -> float:
    """Campiona z world dal heightmap 4096x4096 al punto (x, y) coord Blender.
    Ritorna z reale - z_offset = z in coord Blender (come la road)."""
    H, W = hm.shape
    mpp = extent / W
    half = extent / 2
    col = int((x + half) / mpp)
    row = int((half - y) / mpp)  # PNG row 0 = nord
    col = max(0, min(W - 1, col))
    row = max(0, min(H - 1, row))
    real_z = float(hm[row, col]) / 65535.0 * max_h
    return real_z - z_offset


def infer_z_offset_from_terrain_json() -> float:
    """Carica terrain_info.json e stima z_offset (stessa logica inferita)."""
    info_path = ROOT / "output" / "beamng" / "terrain_info.json"
    if not info_path.exists():
        return 336.0
    info = json.loads(info_path.read_text(encoding="utf-8"))
    hm = np.array(Image.open(ROOT / "output" / "beamng" / "heightmap.png"),
                   dtype=np.uint16)
    H, W = hm.shape
    elev_min = info["elevation_min_m"]
    elev_max = info["elevation_max_m"]
    max_h = elev_max - elev_min
    mpp = info["meters_per_pixel"]
    half = info["extent_m"] / 2.0
    import csv as _csv
    offsets = []
    with (ROOT / "output" / "centerline.csv").open(newline="", encoding="utf-8") as f:
        for r in _csv.DictReader(f):
            x, y, z_csv = float(r["x"]), float(r["y"]), float(r["z"])
            col = int((x + half) / mpp)
            row_px = int((half - y) / mpp)
            if 0 <= col < W and 0 <= row_px < H:
                dem_real = elev_min + float(hm[row_px, col]) / 65535.0 * max_h
                offsets.append(dem_real - z_csv - 0.35)
    if offsets:
        return float(np.median(offsets))
    return 336.0


def triangulate_fan(poly_xy: list[tuple[float, float]]) -> list[tuple[int, int, int]]:
    """Fan triangulation da vertice 0 (ok per poligoni convessi e quasi-convessi).
    Ritorna liste di indici (a, b, c) relativi al poligono in input."""
    tris = []
    for i in range(1, len(poly_xy) - 1):
        tris.append((0, i, i + 1))
    return tris


def convex_hull_2d(pts: list[tuple[float, float]]) -> list[int]:
    """Andrew's monotone chain. Ritorna indici dei punti del convex hull in
    ordine CCW. Evita tetti 'bucati' su footprint concavi (L/U shape)."""
    if len(pts) < 3:
        return list(range(len(pts)))
    idx = sorted(range(len(pts)), key=lambda i: (pts[i][0], pts[i][1]))

    def cross(o, a, b):
        return ((pts[a][0] - pts[o][0]) * (pts[b][1] - pts[o][1])
                - (pts[a][1] - pts[o][1]) * (pts[b][0] - pts[o][0]))

    lower = []
    for i in idx:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], i) <= 0:
            lower.pop()
        lower.append(i)
    upper = []
    for i in reversed(idx):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], i) <= 0:
            upper.pop()
        upper.append(i)
    return lower[:-1] + upper[:-1]


def main():
    road_data = json.loads((ROOT / "road_data.json").read_text(encoding="utf-8"))
    buildings_osm = road_data.get("buildings", [])
    cl_geo = road_data["centerline"]
    lat0 = sum(p["lat"] for p in cl_geo) / len(cl_geo)
    lon0 = sum(p["lon"] for p in cl_geo) / len(cl_geo)
    project = project_factory(lat0, lon0)

    # Centerline in Blender coords
    cl_xy = [project(p["lat"], p["lon"]) for p in cl_geo]
    cell_grid = 50.0
    cl_buckets: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for (x, y) in cl_xy:
        cl_buckets.setdefault((int(x // cell_grid), int(y // cell_grid)), []).append((x, y))

    def dist_to_cl(x: float, y: float) -> float:
        ix = int(x // cell_grid); iy = int(y // cell_grid)
        dmin = float("inf")
        for di in range(-5, 6):
            for dj in range(-5, 6):
                for (cx, cy) in cl_buckets.get((ix + di, iy + dj), []):
                    d = (cx - x) ** 2 + (cy - y) ** 2
                    if d < dmin:
                        dmin = d
        return math.sqrt(dmin) if dmin != float("inf") else 1e9

    # Edifici gia' esportati dal Blender (centroidi)
    world_obj = OUT_DIR / "macerone_world.obj"
    if not world_obj.exists():
        print(f"Missing {world_obj}, rilancia build_full_mod.py prima")
        return
    existing_centroids = parse_world_buildings_centroids(world_obj)
    print(f"Edifici gia' nel mesh Blender: {len(existing_centroids)}")

    # Heightmap per sampling z
    hm_path = ROOT / "output" / "beamng" / "heightmap.png"
    info_path = ROOT / "output" / "beamng" / "terrain_info.json"
    Image.MAX_IMAGE_PIXELS = None
    hm = np.array(Image.open(hm_path), dtype=np.uint16)
    info = json.loads(info_path.read_text(encoding="utf-8"))
    elev_min = info["elevation_min_m"]
    elev_max = info["elevation_max_m"]
    max_h = elev_max - elev_min
    z_offset = infer_z_offset_from_terrain_json()
    print(f"z_offset inferito: {z_offset:.2f}m")

    # Spatial grid per match con existing
    match_buckets: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for (cx, cy) in existing_centroids:
        match_buckets.setdefault(
            (int(cx // MATCH_RADIUS_M), int(cy // MATCH_RADIUS_M)), []
        ).append((cx, cy))

    def is_already_present(cx: float, cy: float) -> bool:
        ix = int(cx // MATCH_RADIUS_M); iy = int(cy // MATCH_RADIUS_M)
        r2 = MATCH_RADIUS_M ** 2
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for (ex, ey) in match_buckets.get((ix + di, iy + dj), []):
                    if (ex - cx) ** 2 + (ey - cy) ** 2 < r2:
                        return True
        return False

    # Genera mesh per ogni edificio missing
    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[list[int], str]] = []

    added = 0
    skipped_too_far = 0
    skipped_already = 0
    skipped_invalid = 0
    for b in buildings_osm:
        coords = b.get("coords", [])
        if len(coords) < 3:
            skipped_invalid += 1
            continue
        poly_xy = [project(c[0], c[1]) for c in coords]
        # Rimuovi duplicato ultimo = primo (se chiuso)
        if poly_xy[0] == poly_xy[-1]:
            poly_xy = poly_xy[:-1]
        if len(poly_xy) < 3:
            skipped_invalid += 1
            continue
        cx = sum(p[0] for p in poly_xy) / len(poly_xy)
        cy = sum(p[1] for p in poly_xy) / len(poly_xy)
        d = dist_to_cl(cx, cy)
        if d > MAX_DIST_M:
            skipped_too_far += 1
            continue
        if is_already_present(cx, cy):
            skipped_already += 1
            continue

        height = float(b.get("height") or DEFAULT_HEIGHT_M)
        if height < 2.0:
            height = DEFAULT_HEIGHT_M

        # z del terreno sotto il building
        base_z = sample_heightmap_z(hm, cx, cy, max_h, z_offset)
        # Sink ~20cm nel terreno per evitare gap visibili
        base_z -= 0.2

        # Base verts + top verts
        base_idx_start = len(verts) + 1  # 1-indexed in OBJ
        for (px, py) in poly_xy:
            verts.append((px, py, base_z))
        for (px, py) in poly_xy:
            verts.append((px, py, base_z + height))
        n = len(poly_xy)

        # Walls: per ogni edge (i, i+1), quad (i, i+1, top_i+1, top_i)
        for i in range(n):
            a = base_idx_start + i
            b = base_idx_start + (i + 1) % n
            c = base_idx_start + n + (i + 1) % n
            d_ = base_idx_start + n + i
            # 2 triangoli
            faces.append(([a, b, c], "Building"))
            faces.append(([a, c, d_], "Building"))

        # Tetto: convex hull sui top verts (chiude l'edificio anche su
        # footprint concavi a L/U; tetti semplici bastano a 250m).
        top_start = base_idx_start + n
        hull = convex_hull_2d(poly_xy)
        for i in range(1, len(hull) - 1):
            a_t = top_start + hull[0]
            b_t = top_start + hull[i]
            c_t = top_start + hull[i + 1]
            faces.append(([a_t, b_t, c_t], "Roof"))
        added += 1

    print(f"\nEdifici OSM missing aggiunti: {added}")
    print(f"  gia' presenti:  {skipped_already}")
    print(f"  >250m dalla SS17: {skipped_too_far}")
    print(f"  poligono invalido: {skipped_invalid}")

    if not verts:
        print("Nessun edificio extra da aggiungere")
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    obj_path = OUT_DIR / "macerone_extra_buildings.obj"
    mtl_path = OUT_DIR / "macerone_extra_buildings.mtl"

    lines = ["# macerone_extra_buildings: edifici OSM non generati da Blender\n",
              "mtllib macerone_extra_buildings.mtl\n"]
    for (vx, vy, vz) in verts:
        lines.append(f"v {vx:.3f} {vy:.3f} {vz:.3f}\n")
    lines.append("o ExtraBuildings\n")
    current_mat = None
    for (idx, mat) in faces:
        if mat != current_mat:
            lines.append(f"usemtl {mat}\n")
            current_mat = mat
        lines.append(f"f {idx[0]} {idx[1]} {idx[2]}\n")
    obj_path.write_text("".join(lines), encoding="utf-8")

    mtl_path.write_text(
        "newmtl Building\nKd 0.82 0.76 0.62\n"
        "newmtl Roof\nKd 0.62 0.32 0.22\n",
        encoding="utf-8"
    )
    print(f"Scritto {obj_path}")


if __name__ == "__main__":
    main()
