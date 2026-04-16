"""
Genera vegetazione realistica basata sull'analisi del satellite ESRI:

1. Texture billboard albero PNG 512x512 con alpha: trunk verticale +
   canopy a cluster di blob verdi (diverse sfumature + variazione random).
2. Analizza tutto il satellite bbox in grid 15x15m, classifica ogni cella
   come "forest" se verde dominante + alta varianza (boschi densi).
3. Genera crossed billboards (2 quad perpendicolari) per ogni alberi
   piazzato nelle celle forest, con offset random + scale variabile.
4. Output: macerone_vegetation.obj (+ .dae + .mtl) con ~1000-3000 alberi
   billboard distribuiti map-wide.

Materiale Tree usa colorMap tree_billboard.png con alphaTest per
trasparenza background.
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "output" / "beamng" / "mod" / "levels" / "macerone" / "art" / "shapes"
TEX_DIR = ROOT / "output" / "beamng" / "mod" / "levels" / "macerone" / "art" / "nature"

GRID_CELL_M = 15.0             # cella analisi satellite
MAX_TREES = 3000                # limite alberi generati
FOREST_GREEN_MIN = 0.05         # green_score minimo per "forest"
FOREST_LUM_MAX = 0.50           # luminosita' massima (ombre chioma)
FOREST_STD_MIN = 0.04           # varianza minima (texture foglie)


def project_factory(lat0, lon0):
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


def generate_tree_billboard_texture(out_path: Path) -> None:
    """Texture 512x512 RGBA con albero procedurale centrato (alpha = shape)."""
    size = 512
    rng = np.random.default_rng(77)
    rgba = np.zeros((size, size, 4), dtype=np.uint8)

    # --- Trunk: rettangolo marrone al centro, dal basso fino a 60% altezza ---
    trunk_x0 = size // 2 - 10
    trunk_x1 = size // 2 + 10
    trunk_y0 = int(size * 0.60)
    trunk_y1 = size - 5
    for y in range(trunk_y0, trunk_y1):
        for x in range(trunk_x0, trunk_x1):
            # variazione colore corteccia
            v = rng.random()
            r = int(80 + v * 30)
            g = int(55 + v * 20)
            b = int(30 + v * 15)
            rgba[y, x, 0] = r
            rgba[y, x, 1] = g
            rgba[y, x, 2] = b
            rgba[y, x, 3] = 255

    # --- Canopy: cluster di blob verdi in area 80-380 ---
    canopy_cy = int(size * 0.35)
    canopy_cx = size // 2
    canopy_radius_max = size // 2 - 20
    # Prima passo: mask generale (forma canopy irregolare)
    yy, xx = np.ogrid[:size, :size]
    d_from_center = np.sqrt((yy - canopy_cy) ** 2 + (xx - canopy_cx) ** 2)
    # Shape base: cerchio con variazione fBm-like
    base_radius = size * 0.30
    # Add perimeter noise
    angle_grid = np.arctan2(yy - canopy_cy, xx - canopy_cx)
    perim_var = np.zeros((size, size), dtype=np.float32)
    for k in range(6):
        freq = 2 ** k
        amp = 1.0 / (k + 1)
        perim_var += amp * np.cos(freq * angle_grid + rng.uniform(0, 2 * np.pi))
    radius_effective = base_radius + perim_var * 20
    canopy_mask = d_from_center < radius_effective
    # Limita al 60% superiore (non coprire trunk bottom)
    canopy_mask = canopy_mask & (yy < trunk_y0 + 20)

    # Dentro la mask, applica colori verdi variati
    n_blobs = 80
    # Posizioni blob random dentro la mask
    for _ in range(n_blobs):
        bx = rng.integers(canopy_cx - canopy_radius_max, canopy_cx + canopy_radius_max)
        by = rng.integers(max(0, canopy_cy - canopy_radius_max),
                          min(size, canopy_cy + canopy_radius_max))
        if not (0 <= by < size and 0 <= bx < size):
            continue
        br = rng.integers(15, 40)
        # Colore blob: verde variato
        bg = rng.integers(80, 180)
        bb = rng.integers(20, 80)
        br_col = rng.integers(30, 90)
        for dy in range(-br, br + 1):
            for dx in range(-br, br + 1):
                xx_, yy_ = bx + dx, by + dy
                if not (0 <= yy_ < size and 0 <= xx_ < size):
                    continue
                if not canopy_mask[yy_, xx_]:
                    continue
                d2 = dx * dx + dy * dy
                if d2 > br * br:
                    continue
                falloff = 1.0 - math.sqrt(d2) / br
                if rgba[yy_, xx_, 3] < 255 * falloff:
                    rgba[yy_, xx_, 0] = int(br_col * (0.9 + rng.random() * 0.2))
                    rgba[yy_, xx_, 1] = int(bg * (0.9 + rng.random() * 0.2))
                    rgba[yy_, xx_, 2] = int(bb * (0.9 + rng.random() * 0.2))
                    rgba[yy_, xx_, 3] = int(255 * falloff)

    # Salva
    TEX_DIR.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba, mode="RGBA").save(out_path, optimize=True)
    print(f"Tree billboard texture: {out_path.relative_to(ROOT)}")


def classify_forest(patch_rgb: np.ndarray) -> bool:
    """True se il patch e' verde scuro + alta varianza = bosco."""
    flat = patch_rgb.reshape(-1, 3).astype(np.float32) / 255.0
    mean = flat.mean(axis=0)
    r, g, b = float(mean[0]), float(mean[1]), float(mean[2])
    lum = (r + g + b) / 3.0
    green_score = g - (r + b) / 2.0
    std = float(flat.std())
    return (green_score > FOREST_GREEN_MIN
            and lum < FOREST_LUM_MAX
            and std > FOREST_STD_MIN)


def infer_z_offset() -> float:
    info = json.loads((ROOT / "output" / "beamng" / "terrain_info.json").read_text(encoding="utf-8"))
    Image.MAX_IMAGE_PIXELS = None
    hm = np.array(Image.open(ROOT / "output" / "beamng" / "heightmap.png"),
                   dtype=np.uint16)
    H, W = hm.shape
    elev_min = info["elevation_min_m"]
    elev_max = info["elevation_max_m"]
    max_h = elev_max - elev_min
    mpp = info["meters_per_pixel"]
    half = info["extent_m"] / 2.0
    offsets = []
    with (ROOT / "output" / "centerline.csv").open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            x, y, z_csv = float(r["x"]), float(r["y"]), float(r["z"])
            col = int((x + half) / mpp)
            row_px = int((half - y) / mpp)
            if 0 <= col < W and 0 <= row_px < H:
                dem_real = elev_min + float(hm[row_px, col]) / 65535.0 * max_h
                offsets.append(dem_real - z_csv - 0.35)
    return float(np.median(offsets)) if offsets else 336.0


def sample_blender_z(hm, max_h, z_offset, x, y, extent=12288.0) -> float:
    H, W = hm.shape
    mpp = extent / W
    half = extent / 2
    col = int((x + half) / mpp)
    row = int((half - y) / mpp)
    col = max(0, min(W - 1, col))
    row = max(0, min(H - 1, row))
    real_z = float(hm[row, col]) / 65535.0 * max_h
    return real_z - z_offset


def main():
    sat_path = ROOT / "output" / "satellite.png"
    bbox_path = ROOT / "output" / "satellite_bbox.json"
    road_data_path = ROOT / "road_data.json"
    info_path = ROOT / "output" / "beamng" / "terrain_info.json"

    for p in (sat_path, bbox_path, road_data_path, info_path):
        if not p.exists():
            print(f"Missing: {p}")
            return

    # 1. Genera texture billboard
    tex_out = TEX_DIR / "tree_billboard.png"
    generate_tree_billboard_texture(tex_out)

    # 2. Analizza satellite per trovare celle "forest"
    Image.MAX_IMAGE_PIXELS = None
    sat = np.array(Image.open(sat_path).convert("RGB"))
    H_img, W_img, _ = sat.shape
    bbox = json.loads(bbox_path.read_text(encoding="utf-8"))["bbox_geo"]
    rd = json.loads(road_data_path.read_text(encoding="utf-8"))
    cl_geo = rd["centerline"]
    lat0 = sum(p["lat"] for p in cl_geo) / len(cl_geo)
    lon0 = sum(p["lon"] for p in cl_geo) / len(cl_geo)
    project, inverse = project_factory(lat0, lon0)

    # Bbox centerline in coord Blender
    cl_x = []
    cl_y = []
    with (ROOT / "output" / "centerline.csv").open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            cl_x.append(float(r["x"]))
            cl_y.append(float(r["y"]))
    bbox_m = (min(cl_x) - 200, min(cl_y) - 200,
              max(cl_x) + 200, max(cl_y) + 200)

    # Spatial grid centerline per filtro (niente alberi entro 6m dalla strada)
    cl_cell = 20.0
    cl_buckets: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for (x, y) in zip(cl_x, cl_y):
        cl_buckets.setdefault((int(x // cl_cell), int(y // cl_cell)), []).append((x, y))
    MIN_DIST_CL = 6.0

    def dist_to_cl(x, y):
        ix = int(x // cl_cell); iy = int(y // cl_cell)
        dmin = float("inf")
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for (cx, cy) in cl_buckets.get((ix + di, iy + dj), []):
                    d = (cx - x) ** 2 + (cy - y) ** 2
                    if d < dmin:
                        dmin = d
        return math.sqrt(dmin) if dmin != float("inf") else 1e9

    def pix_from_xy(x, y):
        lat, lon = inverse(x, y)
        u = (lon - bbox["west"]) / (bbox["east"] - bbox["west"])
        v = (bbox["north"] - lat) / (bbox["north"] - bbox["south"])
        return (int(u * W_img), int(v * H_img))

    # Heightmap per z sampling
    info = json.loads(info_path.read_text(encoding="utf-8"))
    elev_min = info["elevation_min_m"]
    elev_max = info["elevation_max_m"]
    max_h = elev_max - elev_min
    z_offset = infer_z_offset()
    hm = np.array(Image.open(ROOT / "output" / "beamng" / "heightmap.png"),
                   dtype=np.uint16)

    # Scan grid celle
    rng = np.random.default_rng(2024)
    trees = []  # list di (x, y, z, scale)
    x0, y0, x1, y1 = bbox_m
    nx_cells = int((x1 - x0) / GRID_CELL_M)
    ny_cells = int((y1 - y0) / GRID_CELL_M)
    print(f"Scan grid {nx_cells}x{ny_cells} celle da {GRID_CELL_M}m "
          f"sul bbox {int(x1-x0)}x{int(y1-y0)}m")

    for iy in range(ny_cells):
        for ix in range(nx_cells):
            cx = x0 + (ix + 0.5) * GRID_CELL_M
            cy = y0 + (iy + 0.5) * GRID_CELL_M
            # Skip se troppo vicino alla strada
            if dist_to_cl(cx, cy) < MIN_DIST_CL:
                continue
            # Sample satellite 5x5 pixel al centro cell
            px, py = pix_from_xy(cx, cy)
            if not (3 <= px < W_img - 3 and 3 <= py < H_img - 3):
                continue
            patch = sat[py - 2:py + 3, px - 2:px + 3]
            if not classify_forest(patch):
                continue
            # Piazza 2-4 alberi in questa cella
            n_in_cell = rng.integers(1, 4)
            for _ in range(n_in_cell):
                ox = cx + rng.uniform(-GRID_CELL_M / 2, GRID_CELL_M / 2)
                oy = cy + rng.uniform(-GRID_CELL_M / 2, GRID_CELL_M / 2)
                if dist_to_cl(ox, oy) < MIN_DIST_CL:
                    continue
                oz = sample_blender_z(hm, max_h, z_offset, ox, oy) - 0.2
                scale = rng.uniform(0.8, 1.4)
                trees.append((ox, oy, oz, scale))
                if len(trees) >= MAX_TREES:
                    break
            if len(trees) >= MAX_TREES:
                break
        if len(trees) >= MAX_TREES:
            break

    print(f"Alberi piazzati: {len(trees)}")
    if not trees:
        return

    # 3. Genera OBJ crossed billboards
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    obj_path = OUT_DIR / "macerone_vegetation.obj"
    mtl_path = OUT_DIR / "macerone_vegetation.mtl"

    TREE_W = 4.0   # larghezza billboard
    TREE_H = 7.0   # altezza

    lines = ["# macerone_vegetation: crossed billboards albero\n",
              "mtllib macerone_vegetation.mtl\n"]
    # Vertex + UV
    verts: list[str] = []
    uvs: list[str] = []
    faces: list[str] = []
    v_idx = 1
    vt_idx = 1
    # 4 UV fissi per ogni quad
    uv_corners = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    for (x, y, z, sc) in trees:
        w2 = TREE_W * sc / 2
        h = TREE_H * sc
        # Quad 1: piano XZ (along X)
        q1 = [
            (x - w2, y, z),
            (x + w2, y, z),
            (x + w2, y, z + h),
            (x - w2, y, z + h),
        ]
        # Quad 2: piano YZ (along Y) perpendicolare
        q2 = [
            (x, y - w2, z),
            (x, y + w2, z),
            (x, y + w2, z + h),
            (x, y - w2, z + h),
        ]
        # Scrivi vertex
        for p in q1:
            verts.append(f"v {p[0]:.3f} {p[1]:.3f} {p[2]:.3f}\n")
        for p in q2:
            verts.append(f"v {p[0]:.3f} {p[1]:.3f} {p[2]:.3f}\n")
        for (u, v) in uv_corners:
            uvs.append(f"vt {u:.4f} {v:.4f}\n")
        for (u, v) in uv_corners:
            uvs.append(f"vt {u:.4f} {v:.4f}\n")
        # Faces (2 tri per quad, 4 tri totali)
        # Tri format: v/vt/vn (no vn)
        def emit(a, b, c):
            faces.append(
                f"f {a}/{a} {b}/{b} {c}/{c}\n"
            )
        # Q1: v_idx..v_idx+3
        emit(v_idx + 0, v_idx + 1, v_idx + 2)
        emit(v_idx + 0, v_idx + 2, v_idx + 3)
        # Q2: v_idx+4..v_idx+7
        emit(v_idx + 4, v_idx + 5, v_idx + 6)
        emit(v_idx + 4, v_idx + 6, v_idx + 7)
        v_idx += 8
        vt_idx += 8

    lines.extend(verts)
    lines.extend(uvs)
    lines.append("o Vegetation\n")
    lines.append("usemtl TreeBillboard\n")
    lines.extend(faces)
    obj_path.write_text("".join(lines), encoding="utf-8")

    mtl_path.write_text(
        "newmtl TreeBillboard\n"
        f"map_Kd art/nature/tree_billboard.png\n"
        "map_d art/nature/tree_billboard.png\n"
        "Kd 0.90 0.90 0.90\n"
        "d 1.0\n"
        "illum 1\n",
        encoding="utf-8"
    )
    print(f"Scritto {obj_path}")


if __name__ == "__main__":
    main()
