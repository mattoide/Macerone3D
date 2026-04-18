"""
Genera vegetazione realistica basata sull'analisi del satellite ESRI:

1. Texture SINGOLA 512x512 RGBA con albero procedurale centrato (alpha=shape).
2. Analizza satellite in grid 15x15m -> celle "forest" = green_score alto +
   luminosita' bassa + varianza alta.
3. Per ogni cella forest piazza 1-3 crossed billboards. Ogni albero ha:
     scala ∈ [0.75, 1.50]
     rotazione Z ∈ [0°, 90°] (la croce ruota: da angoli diversi non si
       vede la griglia)
     tinta variabile via 4 palette (caldo/scuro/pieno-giorno/umido)
     altezza variabile (alberi giovani/maturi mescolati)
4. Output: macerone_vegetation.obj (+ .dae + .mtl). 4 material groups
   TreeBillboard_P0..P3 con stesso colorMap ma Kd diverso.

Materiale TreeBillboard usa colorMap tree_billboard.png con alphaTest
per trasparenza background.
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

GRID_CELL_M = 15.0
MAX_TREES = 3000
FOREST_GREEN_MIN = 0.05
FOREST_LUM_MAX = 0.50
FOREST_STD_MIN = 0.04

# 4 palette di tinta: moltiplicazione RGB su Kd, stesso colorMap.
TINT_PALETTES = [
    (0.95, 1.00, 0.92),  # caldo (lieve secco/autunno)
    (0.82, 0.92, 0.80),  # verde scuro profondo (conifera)
    (1.05, 1.08, 0.95),  # verde vivo pieno giorno
    (0.92, 1.02, 1.00),  # verde umido (piu' freddo)
]


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
    """Texture 512x512 RGBA albero procedurale centrato (alpha = shape)."""
    size = 512
    rng = np.random.default_rng(77)
    rgba = np.zeros((size, size, 4), dtype=np.uint8)

    # --- Trunk ---
    trunk_x0 = size // 2 - 10
    trunk_x1 = size // 2 + 10
    trunk_y0 = int(size * 0.60)
    trunk_y1 = size - 5
    for y in range(trunk_y0, trunk_y1):
        for x in range(trunk_x0, trunk_x1):
            v = rng.random()
            rgba[y, x, 0] = int(80 + v * 30)
            rgba[y, x, 1] = int(55 + v * 20)
            rgba[y, x, 2] = int(30 + v * 15)
            rgba[y, x, 3] = 255

    # --- Canopy: cluster di blob verdi ---
    canopy_cy = int(size * 0.35)
    canopy_cx = size // 2
    canopy_radius_max = size // 2 - 20
    yy, xx = np.ogrid[:size, :size]
    d_from_center = np.sqrt((yy - canopy_cy) ** 2 + (xx - canopy_cx) ** 2)
    base_radius = size * 0.30
    angle_grid = np.arctan2(yy - canopy_cy, xx - canopy_cx)
    perim_var = np.zeros((size, size), dtype=np.float32)
    for k in range(6):
        freq = 2 ** k
        amp = 1.0 / (k + 1)
        perim_var += amp * np.cos(freq * angle_grid + rng.uniform(0, 2 * np.pi))
    radius_effective = base_radius + perim_var * 20
    canopy_mask = d_from_center < radius_effective
    canopy_mask = canopy_mask & (yy < trunk_y0 + 20)

    n_blobs = 80
    for _ in range(n_blobs):
        bx = rng.integers(canopy_cx - canopy_radius_max, canopy_cx + canopy_radius_max)
        by = rng.integers(max(0, canopy_cy - canopy_radius_max),
                          min(size, canopy_cy + canopy_radius_max))
        if not (0 <= by < size and 0 <= bx < size):
            continue
        br = rng.integers(15, 40)
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

    TEX_DIR.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba).save(out_path, optimize=True)
    print(f"Tree billboard texture: {out_path.relative_to(ROOT)}")
    # BeamNG V1.5 PBR shader legge opacityMap.r, NON .a. Quindi serve
    # una texture separata opacity grayscale (alpha -> R channel).
    # Suffix .data.png = linear (non sRGB), come usa italy vanilla.
    alpha_ch = rgba[:, :, 3]
    opacity_gray = np.stack([alpha_ch, alpha_ch, alpha_ch], axis=-1)
    opacity_path = out_path.parent / "tree_billboard_opacity.data.png"
    Image.fromarray(opacity_gray).save(opacity_path, optimize=True)
    print(f"Tree billboard opacity map: {opacity_path.relative_to(ROOT)}")


def classify_forest(patch_rgb: np.ndarray) -> bool:
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

    # Nome con convenzione BeamNG 0.38 Texture Cooker (*.color.png)
    tex_out = TEX_DIR / "tree_billboard.color.png"
    generate_tree_billboard_texture(tex_out)

    Image.MAX_IMAGE_PIXELS = None
    sat = np.array(Image.open(sat_path).convert("RGB"))
    H_img, W_img, _ = sat.shape
    bbox = json.loads(bbox_path.read_text(encoding="utf-8"))["bbox_geo"]
    rd = json.loads(road_data_path.read_text(encoding="utf-8"))
    cl_geo = rd["centerline"]
    lat0 = sum(p["lat"] for p in cl_geo) / len(cl_geo)
    lon0 = sum(p["lon"] for p in cl_geo) / len(cl_geo)
    project, inverse = project_factory(lat0, lon0)

    cl_x = []
    cl_y = []
    with (ROOT / "output" / "centerline.csv").open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            cl_x.append(float(r["x"]))
            cl_y.append(float(r["y"]))
    bbox_m = (min(cl_x) - 200, min(cl_y) - 200,
              max(cl_x) + 200, max(cl_y) + 200)

    cl_cell = 20.0
    cl_buckets: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for (x, y) in zip(cl_x, cl_y):
        cl_buckets.setdefault((int(x // cl_cell), int(y // cl_cell)), []).append((x, y))
    MIN_DIST_CL = 6.0

    def dist_to_cl(x, y):
        ix = int(x // cl_cell)
        iy = int(y // cl_cell)
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

    info = json.loads(info_path.read_text(encoding="utf-8"))
    elev_min = info["elevation_min_m"]
    elev_max = info["elevation_max_m"]
    max_h = elev_max - elev_min
    z_offset = infer_z_offset()
    hm = np.array(Image.open(ROOT / "output" / "beamng" / "heightmap.png"),
                   dtype=np.uint16)

    rng = np.random.default_rng(2024)
    trees = []  # (x, y, z, scale, angle_deg, palette_idx)
    x0, y0, x1, y1 = bbox_m
    nx_cells = int((x1 - x0) / GRID_CELL_M)
    ny_cells = int((y1 - y0) / GRID_CELL_M)
    print(f"Scan grid {nx_cells}x{ny_cells} celle da {GRID_CELL_M}m "
          f"sul bbox {int(x1-x0)}x{int(y1-y0)}m")

    for iy in range(ny_cells):
        for ix in range(nx_cells):
            cx = x0 + (ix + 0.5) * GRID_CELL_M
            cy = y0 + (iy + 0.5) * GRID_CELL_M
            if dist_to_cl(cx, cy) < MIN_DIST_CL:
                continue
            px, py = pix_from_xy(cx, cy)
            if not (3 <= px < W_img - 3 and 3 <= py < H_img - 3):
                continue
            patch = sat[py - 2:py + 3, px - 2:px + 3]
            if not classify_forest(patch):
                continue
            n_in_cell = int(rng.integers(1, 4))
            for _ in range(n_in_cell):
                ox = cx + rng.uniform(-GRID_CELL_M / 2, GRID_CELL_M / 2)
                oy = cy + rng.uniform(-GRID_CELL_M / 2, GRID_CELL_M / 2)
                if dist_to_cl(ox, oy) < MIN_DIST_CL:
                    continue
                oz = sample_blender_z(hm, max_h, z_offset, ox, oy) - 0.2
                scale = float(rng.uniform(0.75, 1.50))
                angle_deg = float(rng.uniform(0.0, 90.0))
                palette_idx = int(rng.integers(0, len(TINT_PALETTES)))
                trees.append((ox, oy, oz, scale, angle_deg, palette_idx))
                if len(trees) >= MAX_TREES:
                    break
            if len(trees) >= MAX_TREES:
                break
        if len(trees) >= MAX_TREES:
            break

    print(f"Alberi piazzati: {len(trees)}")
    if not trees:
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    obj_path = OUT_DIR / "macerone_vegetation.obj"
    mtl_path = OUT_DIR / "macerone_vegetation.mtl"

    TREE_W = 4.5
    TREE_H_BASE = 7.5

    lines = ["# macerone_vegetation: crossed billboards (4 palette tinta)\n",
              "mtllib macerone_vegetation.mtl\n"]
    verts: list[str] = []
    uvs: list[str] = []
    group_faces: dict[str, list[str]] = {}
    v_idx = 1
    vt_idx = 1

    uv_corners = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]

    for (x, y, z, sc, angle_deg, pal) in trees:
        mat_name = f"TreeBillboard_P{pal}"
        # Altezza base variabile per rompere la monotonia
        h_tree = TREE_H_BASE * sc
        w2 = TREE_W * sc / 2
        a = math.radians(angle_deg)
        ca = math.cos(a)
        sa = math.sin(a)
        # Quad 1: lungo (ca, sa)
        q1 = [
            (x - w2 * ca, y - w2 * sa, z),
            (x + w2 * ca, y + w2 * sa, z),
            (x + w2 * ca, y + w2 * sa, z + h_tree),
            (x - w2 * ca, y - w2 * sa, z + h_tree),
        ]
        # Quad 2: perpendicolare (-sa, ca)
        q2 = [
            (x + w2 * sa, y - w2 * ca, z),
            (x - w2 * sa, y + w2 * ca, z),
            (x - w2 * sa, y + w2 * ca, z + h_tree),
            (x + w2 * sa, y - w2 * ca, z + h_tree),
        ]
        for p in q1:
            verts.append(f"v {p[0]:.3f} {p[1]:.3f} {p[2]:.3f}\n")
        for p in q2:
            verts.append(f"v {p[0]:.3f} {p[1]:.3f} {p[2]:.3f}\n")
        for (u, v) in uv_corners:
            uvs.append(f"vt {u:.4f} {v:.4f}\n")
        for (u, v) in uv_corners:
            uvs.append(f"vt {u:.4f} {v:.4f}\n")

        faces = group_faces.setdefault(mat_name, [])

        def emit(a_, b_, c_):
            faces.append(f"f {a_}/{a_} {b_}/{b_} {c_}/{c_}\n")

        emit(v_idx + 0, v_idx + 1, v_idx + 2)
        emit(v_idx + 0, v_idx + 2, v_idx + 3)
        emit(v_idx + 4, v_idx + 5, v_idx + 6)
        emit(v_idx + 4, v_idx + 6, v_idx + 7)
        v_idx += 8
        vt_idx += 8

    lines.extend(verts)
    lines.extend(uvs)
    lines.append("o Vegetation\n")
    for mat_name, faces in group_faces.items():
        lines.append(f"usemtl {mat_name}\n")
        lines.extend(faces)
    obj_path.write_text("".join(lines), encoding="utf-8")

    mtl_lines = []
    for pal_i, tint in enumerate(TINT_PALETTES):
        name = f"TreeBillboard_P{pal_i}"
        r, g, b = tint
        mtl_lines.append(f"newmtl {name}\n")
        mtl_lines.append("map_Kd art/nature/tree_billboard.png\n")
        mtl_lines.append("map_d art/nature/tree_billboard.png\n")
        mtl_lines.append(f"Kd {r:.3f} {g:.3f} {b:.3f}\n")
        mtl_lines.append("d 1.0\nillum 1\n\n")
    mtl_path.write_text("".join(mtl_lines), encoding="utf-8")
    print(f"Scritto {obj_path} ({len(group_faces)} material groups)")


if __name__ == "__main__":
    main()
