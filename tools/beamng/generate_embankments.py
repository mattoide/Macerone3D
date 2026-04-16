"""
Genera scarpate procedurali: nastri mesh inclinati sui bordi della strada
che collegano il bordo banchina al terrain sottostante, quando la strada
e' rialzata (embankment).

Strategia:
- Per ogni centerline point, calcolo 2 vertex "bordo banchina" (4m offset
  sx/dx, z = road_z - 0.2m) e 2 vertex "piede scarpata" (10m offset sx/dx,
  z = terrain_z campionato dal mesh Terrain Blender).
- Se terrain_z < road_z - 0.5m (gap visibile), costruisco i quad triangoli
  per lato. Se gap piccolo o negativo (terrain sopra), skip (nessuna
  scarpata necessaria).

Output: macerone_embankments.obj (strisce mesh per lato).
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

SHOULDER_OFFSET_M = 4.0       # bordo esterno banchina
FOOT_OFFSET_M = 10.0           # piede scarpata
GAP_THRESHOLD_M = 0.5          # sopraelevazione minima per generare scarpata


def project_factory(lat0, lon0):
    R = 6378137.0
    kx = math.cos(math.radians(lat0)) * R
    ky = R
    def project(lat, lon):
        return (math.radians(lon - lon0) * kx,
                math.radians(lat - lat0) * ky)
    return project


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


def sample_blender_z(hm, max_h: float, z_offset: float,
                      x: float, y: float,
                      extent: float = 12288.0) -> float:
    H, W = hm.shape
    mpp = extent / W
    half = extent / 2.0
    col = int((x + half) / mpp)
    row = int((half - y) / mpp)
    col = max(0, min(W - 1, col))
    row = max(0, min(H - 1, row))
    real_z = float(hm[row, col]) / 65535.0 * max_h
    return real_z - z_offset


def main():
    cl_path = ROOT / "output" / "centerline.csv"
    info_path = ROOT / "output" / "beamng" / "terrain_info.json"
    if not (cl_path.exists() and info_path.exists()):
        print("Missing centerline.csv or terrain_info.json")
        return

    info = json.loads(info_path.read_text(encoding="utf-8"))
    elev_min = info["elevation_min_m"]
    elev_max = info["elevation_max_m"]
    max_h_orig = elev_max - elev_min
    z_offset = infer_z_offset()
    Image.MAX_IMAGE_PIXELS = None
    hm = np.array(Image.open(ROOT / "output" / "beamng" / "heightmap.png"),
                   dtype=np.uint16)

    cl = []
    with cl_path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            cl.append((float(r["x"]), float(r["y"]), float(r["z"]),
                       int(r.get("bridge", "0") or 0),
                       int(r.get("tunnel", "0") or 0)))

    verts: list[tuple[float, float, float]] = []
    faces: list[list[int]] = []

    # Per each lato (+1 sx, -1 dx) costruisco un nastro di quad
    for side in (+1, -1):
        strip_shoulder = []   # vertex index di bordo banchina
        strip_foot = []       # vertex index di piede scarpata
        valid = []            # True se quel point ha gap > threshold
        for i, (x, y, z, br, tu) in enumerate(cl):
            if i == 0 or i == len(cl) - 1:
                tx, ty = 1.0, 0.0
            else:
                dx = cl[i + 1][0] - cl[i - 1][0]
                dy = cl[i + 1][1] - cl[i - 1][1]
                d = math.hypot(dx, dy)
                if d < 1e-6:
                    tx, ty = 1.0, 0.0
                else:
                    tx, ty = dx / d, dy / d
            nx, ny = -ty, tx
            # Bordo banchina e piede
            sx = x + nx * SHOULDER_OFFSET_M * side
            sy = y + ny * SHOULDER_OFFSET_M * side
            fx = x + nx * FOOT_OFFSET_M * side
            fy = y + ny * FOOT_OFFSET_M * side
            # terrain_z al piede scarpata
            terrain_z_foot = sample_blender_z(hm, max_h_orig, z_offset, fx, fy)
            gap = (z - 0.2) - terrain_z_foot
            # Bordo banchina a z = road_z - 0.2m
            shoulder_z = z - 0.2
            foot_z = terrain_z_foot - 0.1  # leggero sink sotto il terrain
            strip_shoulder.append((sx, sy, shoulder_z))
            strip_foot.append((fx, fy, foot_z))
            valid.append(gap > GAP_THRESHOLD_M and not (br or tu))

        # Costruisci quad tra (i) e (i+1) solo se entrambi valid
        base_off = len(verts)
        for v in strip_shoulder:
            verts.append(v)
        for v in strip_foot:
            verts.append(v)
        N = len(strip_shoulder)
        for i in range(N - 1):
            if not (valid[i] and valid[i + 1]):
                continue
            a = base_off + i + 1                   # shoulder_i
            b = base_off + i + 1 + 1               # shoulder_i+1 (1-indexed OBJ)
            c = base_off + N + i + 1 + 1           # foot_i+1
            d_ = base_off + N + i + 1              # foot_i
            # Normale corretta: per side=+1 (sx) la normale dal basso verso
            # l'alto guarda in direzione nx. side_sign inverso per dx.
            if side > 0:
                faces.append([a, d_, c])
                faces.append([a, c, b])
            else:
                faces.append([a, b, c])
                faces.append([a, c, d_])

    if not faces:
        print("Nessuna scarpata da generare (strada sempre a livello terrain)")
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    obj_path = OUT_DIR / "macerone_embankments.obj"
    mtl_path = OUT_DIR / "macerone_embankments.mtl"

    lines = ["# macerone_embankments: scarpate procedurali sui bordi strada\n",
              "mtllib macerone_embankments.mtl\n"]
    for (vx, vy, vz) in verts:
        lines.append(f"v {vx:.3f} {vy:.3f} {vz:.3f}\n")
    lines.append("o Embankments\n")
    lines.append("usemtl Embankment\n")
    for idx in faces:
        lines.append(f"f {idx[0]} {idx[1]} {idx[2]}\n")
    obj_path.write_text("".join(lines), encoding="utf-8")

    mtl_path.write_text(
        "newmtl Embankment\nKd 0.40 0.45 0.28\n",  # verde-marrone terriccio
        encoding="utf-8"
    )
    print(f"Embankments: {len(faces)//2} quad generati "
          f"(scarpate dove road e' > {GAP_THRESHOLD_M}m sopra terrain)")
    print(f"Scritto {obj_path}")


if __name__ == "__main__":
    main()
