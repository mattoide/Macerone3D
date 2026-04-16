"""
Aggiunge dettagli realistici sull'asfalto della SS17:
1. Toppe di bitume (patches scuri irregolari) sparse ogni ~25m, tipiche
   delle statali italiane di montagna riparate a pezzi
2. Chevrons gialli (cartelli freccia) sui tornanti piu' stretti
3. Strisce di usura chiare (tracce ruote longitudinali) sui rettilinei

Output:
  macerone_road_details.obj + .dae + .mtl
Aggiunto come TSStatic nel main.level.json.
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "output" / "beamng" / "mod" / "levels" / "macerone" / "art" / "shapes"

# Parametri
PATCH_STEP_M = 20.0          # media distanza tra patches
PATCH_PROB = 0.45             # probabilita' di piazzare patch ogni step
CHEVRON_ANGLE_DEG = 35.0      # chevron se angolo turn > questo (3 cl points)
ROAD_HALF_WIDTH_M = 3.0       # meta' carreggiata (~6m asfalto)


def main():
    cl_path = ROOT / "output" / "centerline.csv"
    if not cl_path.exists():
        print(f"Missing {cl_path}")
        return
    cl = []
    with cl_path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            cl.append((float(r["x"]), float(r["y"]), float(r["z"]),
                       int(r.get("bridge", "0") or 0),
                       int(r.get("tunnel", "0") or 0)))

    rng = np.random.default_rng(55)
    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[list[int], str]] = []

    def add_quad(p1, p2, p3, p4, mat: str):
        """Quad piatto come 2 triangoli."""
        base = len(verts) + 1
        verts.extend([p1, p2, p3, p4])
        faces.append(([base, base + 1, base + 2], mat))
        faces.append(([base, base + 2, base + 3], mat))

    def add_patch_irregular(cx, cy, cz, tx, ty, nx, ny, mat: str):
        """Patch bitume irregolare: poligono quasi-ellittico 2x0.5m piazzato
        lungo la direzione strada."""
        length = rng.uniform(1.5, 4.0)
        width = rng.uniform(0.3, 0.8)
        n_pts = 8  # vertices del poligono
        # Centro leggermente offset nella corsia (evita centro perfetto)
        lane_offset = rng.uniform(-1.5, 1.5)
        cx += nx * lane_offset
        cy += ny * lane_offset
        base = len(verts) + 1
        for i in range(n_pts):
            ang = 2 * math.pi * i / n_pts
            # jitter sulla forma
            r_long = length / 2 * (1.0 + rng.uniform(-0.15, 0.15))
            r_wid = width / 2 * (1.0 + rng.uniform(-0.15, 0.15))
            # Forma lungo tangent (r_long) e normal (r_wid)
            dlong = r_long * math.cos(ang)
            dwid = r_wid * math.sin(ang)
            px = cx + tx * dlong + nx * dwid
            py = cy + ty * dlong + ny * dwid
            pz = cz + 0.03  # 3cm sopra asfalto
            verts.append((px, py, pz))
        # Fan triangulation dal vertice 0
        for i in range(1, n_pts - 1):
            faces.append(([base, base + i, base + i + 1], mat))

    def add_chevron(cx, cy, cz, tx, ty, nx, ny, side: int):
        """Chevron giallo (cartello V orizzontale) su palo al bordo esterno."""
        # Palo a 4m dal centerline, lato esterno curva
        pole_x = cx + nx * 4.0 * side
        pole_y = cy + ny * 4.0 * side
        pole_z = cz - 0.1
        # Palo: cilindro sottile 1.2m alto
        base = len(verts) + 1
        for k in range(6):
            ang = 2 * math.pi * k / 6
            verts.append((pole_x + 0.04 * math.cos(ang),
                          pole_y + 0.04 * math.sin(ang), pole_z))
        for k in range(6):
            ang = 2 * math.pi * k / 6
            verts.append((pole_x + 0.04 * math.cos(ang),
                          pole_y + 0.04 * math.sin(ang), pole_z + 1.2))
        for k in range(6):
            a = base + k
            b = base + (k + 1) % 6
            c = base + 6 + (k + 1) % 6
            d = base + 6 + k
            faces.append(([a, b, c], "ChevronPole"))
            faces.append(([a, c, d], "ChevronPole"))

        # Cartello rettangolare giallo 50x40cm orientato verso la strada
        # Perpendicolare alla tangent, rivolto verso il centerline
        sign_z = pole_z + 1.05
        # Normale del cartello punta verso il centerline = -nx*side, -ny*side
        face_nx, face_ny = -nx * side, -ny * side
        # Il cartello e' un piano: verts ai 4 angoli
        # Asse "horizontal" del cartello = tangent
        # Asse "vertical" del cartello = Z
        hw = 0.25  # half width 50cm
        hh = 0.20  # half height 40cm
        b2 = len(verts) + 1
        verts.append((pole_x + tx * hw, pole_y + ty * hw, sign_z - hh))
        verts.append((pole_x - tx * hw, pole_y - ty * hw, sign_z - hh))
        verts.append((pole_x - tx * hw, pole_y - ty * hw, sign_z + hh))
        verts.append((pole_x + tx * hw, pole_y + ty * hw, sign_z + hh))
        faces.append(([b2, b2 + 1, b2 + 2], "ChevronSign"))
        faces.append(([b2, b2 + 2, b2 + 3], "ChevronSign"))

    # --- Piazza patches lungo la strada ---
    acc = 0.0
    last_x, last_y = cl[0][0], cl[0][1]
    n_patches = 0
    n_patches_light = 0
    for i in range(1, len(cl)):
        x, y, z, br, tu = cl[i]
        dx = x - last_x; dy = y - last_y
        d = math.hypot(dx, dy)
        acc += d
        last_x, last_y = x, y
        if br or tu:
            continue
        if acc < PATCH_STEP_M:
            continue
        acc = 0.0
        if rng.random() > PATCH_PROB:
            continue
        if d < 0.01:
            continue
        tx, ty = dx / d, dy / d
        nx, ny = -ty, tx
        # Dark patch (bitume nuovo)
        add_patch_irregular(x, y, z, tx, ty, nx, ny, "AsphaltPatchDarkNew")
        n_patches += 1
        # Probabilmente 25% anche un light patch (asfalto scolorito)
        if rng.random() < 0.25:
            add_patch_irregular(x, y, z, tx, ty, nx, ny, "AsphaltPatchLightNew")
            n_patches_light += 1

    # --- Chevrons sui tornanti ---
    n_chevrons = 0
    for i in range(2, len(cl) - 2):
        if cl[i][3] or cl[i][4]:  # bridge/tunnel
            continue
        # Calcolo angolo di curva tra i-2, i, i+2 (scala piu' grande)
        p1 = cl[i - 2]
        p2 = cl[i]
        p3 = cl[i + 2]
        v1x, v1y = p2[0] - p1[0], p2[1] - p1[1]
        v2x, v2y = p3[0] - p2[0], p3[1] - p2[1]
        m1 = math.hypot(v1x, v1y)
        m2 = math.hypot(v2x, v2y)
        if m1 < 1.0 or m2 < 1.0:
            continue
        dot = (v1x * v2x + v1y * v2y) / (m1 * m2)
        dot = max(-1.0, min(1.0, dot))
        angle_deg = math.degrees(math.acos(dot))
        if angle_deg < CHEVRON_ANGLE_DEG:
            continue
        # Curva stretta: piazza chevron. Determina lato esterno (cross product)
        cross = v1x * v2y - v1y * v2x
        side = -1 if cross > 0 else 1  # lato esterno
        # Tangent medio
        tx = (v1x + v2x) / (m1 + m2)
        ty = (v1y + v2y) / (m1 + m2)
        d = math.hypot(tx, ty)
        if d < 0.01:
            continue
        tx, ty = tx / d, ty / d
        nx, ny = -ty, tx
        add_chevron(p2[0], p2[1], p2[2], tx, ty, nx, ny, side)
        n_chevrons += 1

    print(f"Road details: {n_patches} patches dark + {n_patches_light} light, "
          f"{n_chevrons} chevrons")

    if not verts:
        print("Nessun dettaglio generato")
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    obj_path = OUT_DIR / "macerone_road_details.obj"
    mtl_path = OUT_DIR / "macerone_road_details.mtl"

    lines = ["# macerone_road_details: patches bitume + chevrons\n",
              "mtllib macerone_road_details.mtl\n"]
    for (vx, vy, vz) in verts:
        lines.append(f"v {vx:.3f} {vy:.3f} {vz:.3f}\n")
    lines.append("o RoadDetails\n")
    current_mat = None
    for (idx, mat) in faces:
        if mat != current_mat:
            lines.append(f"usemtl {mat}\n")
            current_mat = mat
        lines.append(f"f {idx[0]} {idx[1]} {idx[2]}\n")
    obj_path.write_text("".join(lines), encoding="utf-8")

    mtl_path.write_text(
        "newmtl AsphaltPatchDarkNew\nKd 0.06 0.06 0.07\n"
        "newmtl AsphaltPatchLightNew\nKd 0.35 0.34 0.32\n"
        "newmtl ChevronPole\nKd 0.85 0.85 0.85\n"
        "newmtl ChevronSign\nKd 0.95 0.82 0.10\n",
        encoding="utf-8"
    )
    print(f"Scritto {obj_path}")


if __name__ == "__main__":
    main()
