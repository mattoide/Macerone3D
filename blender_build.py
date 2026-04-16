"""
Costruisce il Valico del Macerone in Blender partendo da road_data.json.
Versione batched: una mesh per categoria (edifici/strade/boschi/acqua) per
evitare l'overhead di Python API con migliaia di oggetti.

Esegui:
    blender --background --python blender_build.py
"""
from __future__ import annotations

import bisect
import bpy
import bmesh
import json
import math
import os
import sys
import time
from mathutils import Vector
from pathlib import Path

# stdout unbuffered per vedere il progresso con --background
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ======== Parametri ====================================================
SHOULDER_W = 0.75
MARKING_CENTER_W = 0.12
MARKING_EDGE_W = 0.12
EDGE_INSET = 0.15
ROAD_THICKNESS = 0.15
SMOOTH_WINDOW = 5
SUBDIV_PER_SEG = 3
BANK_MAX_DEG = 4.0
BANK_CURV_NORM = 0.02
GUARDRAIL_DROP = 2.5
GUARDRAIL_HEIGHT = 0.75
GUARDRAIL_OFFSET = 0.25
BUILDING_DEFAULT_H = 6.0

# Corridoio attorno alla strada: tutto ciò che cade oltre questa distanza
# dalla centerline viene scartato (terreno, edifici, boschi, acque, altre strade).
CORRIDOR_M = 120.0

# Anti-clipping strada/terreno: il DEM ha celle ~60m, quindi tra due vertici
# distanti la superficie interpolata può sporgere sopra l'asfalto. Questi
# parametri allargano il "carving" del terreno sotto la strada per evitarlo.
ROAD_EMBANKMENT = 0.35   # la strada sta "sollevata" di questi m sul DEM
CARVE_BUFFER_M = 12.0    # buffer oltre (asfalto + banchine) che viene abbassato
CARVE_DEPTH_M = 0.8      # m sotto la strada a cui portiamo i vertici del terreno
CARVE_BLEND_FACTOR = 2.2 # zona di raccordo: da carve_width/2 a carve_width/2 * factor
# =======================================================================

SCRIPT_DIR = Path(bpy.path.abspath("//")) if bpy.data.filepath else Path(__file__).parent
DATA_PATH = SCRIPT_DIR / "road_data.json"
OUT_DIR = SCRIPT_DIR / "output"
OUT_DIR.mkdir(exist_ok=True)

R_EARTH = 6_378_137.0


# ======== Proiezione ===================================================
class Projection:
    def __init__(self, lat0, lon0):
        self.lat0 = lat0
        self.lon0 = lon0
        self.kx = math.cos(math.radians(lat0)) * R_EARTH
        self.ky = R_EARTH

    def xy(self, lat, lon):
        return (math.radians(lon - self.lon0) * self.kx,
                math.radians(lat - self.lat0) * self.ky)


# ======== DEM sampler (bilineare su griglia) ===========================
class DEMSampler:
    def __init__(self, dem, proj, z_offset):
        self.rows = dem["rows"]
        self.cols = dem["cols"]
        s, w, n, e = dem["bbox"]
        self.lat_step = (n - s) / (self.rows - 1)
        self.lon_step = (e - w) / (self.cols - 1)
        self.s, self.w, self.n, self.e = s, w, n, e
        # precomputo coord locali degli angoli per conversione inversa x,y→(j,i)
        self.x_min, self.y_min = proj.xy(s, w)
        self.x_max, self.y_max = proj.xy(n, e)
        self.grid = [[z - z_offset for z in row] for row in dem["grid"]]

    def sample(self, x, y):
        # normalizzo in [0, cols-1] x [0, rows-1]
        if self.x_max == self.x_min or self.y_max == self.y_min:
            return 0.0
        fx = (x - self.x_min) / (self.x_max - self.x_min) * (self.cols - 1)
        fy = (y - self.y_min) / (self.y_max - self.y_min) * (self.rows - 1)
        fx = max(0.0, min(self.cols - 1.001, fx))
        fy = max(0.0, min(self.rows - 1.001, fy))
        i0 = int(fx); j0 = int(fy)
        i1 = i0 + 1; j1 = j0 + 1
        tx = fx - i0; ty = fy - j0
        g = self.grid
        z00 = g[j0][i0]; z10 = g[j0][i1]
        z01 = g[j1][i0]; z11 = g[j1][i1]
        return ((1 - tx) * (1 - ty) * z00 + tx * (1 - ty) * z10 +
                (1 - tx) * ty * z01 + tx * ty * z11)


# ======== Corridor (spatial index su centerline) =======================
class Corridor:
    """Permette query veloci: "questo punto è entro CORRIDOR_M dalla strada?" """
    def __init__(self, cl_xyz, radius):
        self.radius = radius
        self.bres = max(50.0, radius)
        self.buckets = {}
        for i, (x, y, _z) in enumerate(cl_xyz):
            self.buckets.setdefault((int(x // self.bres), int(y // self.bres)), []).append((x, y))

    def inside(self, x, y):
        bx, by = int(x // self.bres), int(y // self.bres)
        r2 = self.radius * self.radius
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for (cx, cy) in self.buckets.get((bx + dx, by + dy), ()):
                    if (cx - x) ** 2 + (cy - y) ** 2 <= r2:
                        return True
        return False

    def any_inside(self, poly_xy):
        return any(self.inside(x, y) for x, y in poly_xy)

    def min_dist_to_road(self, x, y):
        """Distanza minima approssimata dal punto (x,y) alla centerline."""
        bx, by = int(x // self.bres), int(y // self.bres)
        best = float("inf")
        for dx in (-2, -1, 0, 1, 2):
            for dy in (-2, -1, 0, 1, 2):
                for (cx, cy) in self.buckets.get((bx + dx, by + dy), ()):
                    d2 = (cx - x) ** 2 + (cy - y) ** 2
                    if d2 < best:
                        best = d2
        return math.sqrt(best)


# ======== Scene utilities ==============================================
def clear_scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for col in (bpy.data.meshes, bpy.data.materials, bpy.data.curves, bpy.data.images):
        for item in list(col):
            col.remove(item)


def new_mesh_object(name, verts, faces, collection=None, mat=None):
    mesh = bpy.data.meshes.new(name + "_Mesh")
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    (collection or bpy.context.collection).objects.link(obj)
    if mat is not None:
        mesh.materials.append(mat)
    return obj


def ensure_collection(name):
    if name in bpy.data.collections:
        return bpy.data.collections[name]
    col = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(col)
    return col


# ======== Materials ====================================================
def mat_pbr(name, base_color, roughness=0.8, metallic=0.0, emission=None, emission_strength=0.0):
    if name in bpy.data.materials:
        return bpy.data.materials[name]
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    bsdf = m.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (*base_color, 1.0)
        bsdf.inputs["Roughness"].default_value = roughness
        bsdf.inputs["Metallic"].default_value = metallic
        if emission is not None:
            col = emission if isinstance(emission, tuple) else base_color
            if "Emission Color" in bsdf.inputs:
                bsdf.inputs["Emission Color"].default_value = (*col, 1.0)
            elif "Emission" in bsdf.inputs:
                bsdf.inputs["Emission"].default_value = (*col, 1.0)
            if "Emission Strength" in bsdf.inputs:
                bsdf.inputs["Emission Strength"].default_value = emission_strength
    # color di anteprima in viewport (utile quando apri il .blend)
    m.diffuse_color = (*base_color, 1.0)
    return m


def build_materials():
    return _build_materials_impl()


def _build_materials_impl():
    return {
        # Strada principale: asfalto scuro "vero"
        "asphalt":   mat_pbr("Asphalt",       (0.05, 0.05, 0.055), roughness=0.85),
        "shoulder":  mat_pbr("Shoulder",      (0.20, 0.18, 0.15), roughness=0.95),
        "line_w":    mat_pbr("LineWhite",     (0.92, 0.92, 0.92), roughness=0.6),
        "line_y":    mat_pbr("LineYellow",    (0.95, 0.80, 0.10), roughness=0.6),
        # Contesto (reso visibilmente diverso per distinguerlo dalla strada principale)
        "terrain":   mat_pbr("Terrain",       (0.32, 0.42, 0.18), roughness=1.0),
        "forest":    mat_pbr("Forest",        (0.10, 0.24, 0.08), roughness=1.0),
        "water":     mat_pbr("Water",         (0.08, 0.35, 0.55), roughness=0.05),
        "building":  mat_pbr("Building",      (0.80, 0.72, 0.58), roughness=0.8),
        "roof":      mat_pbr("Roof",          (0.40, 0.15, 0.10), roughness=0.85),
        "guardrail": mat_pbr("Guardrail",     (0.75, 0.75, 0.78), roughness=0.35, metallic=0.9),
        # Altre strade: grigio chiaro per non confondersi con la principale
        "otherroad": mat_pbr("OtherRoad",     (0.35, 0.35, 0.37), roughness=0.9),
        # Evidenziatore: emissivo arancione, visibilissimo
        "highlight": mat_pbr("RoadHighlight", (1.0, 0.35, 0.0),
                             roughness=0.4, emission=(1.0, 0.4, 0.0), emission_strength=8.0),
        "start":     mat_pbr("StartMarker",   (0.0, 1.0, 0.2),
                             roughness=0.4, emission=(0.0, 1.0, 0.2), emission_strength=10.0),
        "end":       mat_pbr("EndMarker",     (1.0, 0.0, 0.05),
                             roughness=0.4, emission=(1.0, 0.0, 0.05), emission_strength=10.0),
        "trunk":     mat_pbr("TreeTrunk",     (0.22, 0.14, 0.08), roughness=0.95),
        "canopy":    mat_pbr("TreeCanopy",    (0.10, 0.28, 0.08), roughness=1.0),
        "pole":      mat_pbr("Pole",          (0.18, 0.18, 0.20), roughness=0.6, metallic=0.7),
        "sign":      mat_pbr("Sign",          (0.90, 0.10, 0.10), roughness=0.5),
        "lamp":      mat_pbr("Lamp",          (1.0, 0.95, 0.70),
                             roughness=0.3, emission=(1.0, 0.95, 0.70), emission_strength=2.0),
    }


# ======== Centerline ===================================================
def centerline_xyz(points, proj):
    return [(*proj.xy(p["lat"], p["lon"]), p["ele"]) for p in points]


def smooth_centerline(cl, window):
    if window <= 1:
        return cl[:]
    half = window // 2
    n = len(cl)
    out = []
    for i in range(n):
        i0, i1 = max(0, i - half), min(n, i + half + 1)
        seg = cl[i0:i1]
        sx = sum(p[0] for p in seg) / len(seg)
        sy = sum(p[1] for p in seg) / len(seg)
        sz = sum(p[2] for p in seg) / len(seg)
        out.append((sx, sy, sz))
    out[0] = cl[0]
    out[-1] = cl[-1]
    return out


def resample_catmull(cl, subdiv):
    if subdiv <= 1:
        return cl[:]

    def tj(ti, pi, pj):
        d = math.sqrt(sum((a - b) ** 2 for a, b in zip(pi, pj)))
        return ti + math.sqrt(max(1e-9, d))

    n = len(cl)
    out = [cl[0]]
    for i in range(n - 1):
        p0 = cl[max(0, i - 1)]
        p1 = cl[i]
        p2 = cl[i + 1]
        p3 = cl[min(n - 1, i + 2)]
        t0 = 0.0
        t1 = tj(t0, p0, p1)
        t2 = tj(t1, p1, p2)
        t3 = tj(t2, p2, p3)
        if t2 == t1:
            out.append(p2); continue
        for k in range(1, subdiv + 1):
            t = t1 + (t2 - t1) * (k / subdiv)
            a1 = _lerp(p0, p1, t, t0, t1)
            a2 = _lerp(p1, p2, t, t1, t2)
            a3 = _lerp(p2, p3, t, t2, t3)
            b1 = _lerp(a1, a2, t, t0, t2)
            b2 = _lerp(a2, a3, t, t1, t3)
            c = _lerp(b1, b2, t, t1, t2)
            out.append(c)
    return out


def _lerp(p, q, t, ta, tb):
    if tb == ta:
        return p
    w = (tb - t) / (tb - ta)
    return tuple(w * pi + (1 - w) * qi for pi, qi in zip(p, q))


def tangents_and_curvature(cl):
    n = len(cl)
    tans = []
    curvs = []
    for i in range(n):
        if i == 0:
            dx = cl[1][0] - cl[0][0]; dy = cl[1][1] - cl[0][1]
        elif i == n - 1:
            dx = cl[-1][0] - cl[-2][0]; dy = cl[-1][1] - cl[-2][1]
        else:
            dx = cl[i + 1][0] - cl[i - 1][0]; dy = cl[i + 1][1] - cl[i - 1][1]
        L = math.hypot(dx, dy) or 1.0
        tans.append((dx / L, dy / L))
    for i in range(n):
        if i == 0 or i == n - 1:
            curvs.append(0.0); continue
        t0 = tans[i - 1]; t1 = tans[i + 1]
        cross = t0[0] * t1[1] - t0[1] * t1[0]
        ds = math.hypot(cl[i + 1][0] - cl[i - 1][0], cl[i + 1][1] - cl[i - 1][1]) / 2 or 1.0
        curvs.append(max(-1.0, min(1.0, cross)) / ds)
    return tans, curvs


def recompute_road_z_from_dem(cl, tans, dem, probes=(5.0, 10.0, 18.0, 28.0)):
    """Per ogni punto, z = MIN tra il DEM al punto e a una griglia di offset
    laterali/longitudinali. Serve a spingere la strada verso il FONDO della
    trincea quando il DEM (25-30 m/pixel) fa la media con le pareti laterali."""
    new = []
    for (x, y, _z), (tx, ty) in zip(cl, tans):
        nx, ny = -ty, tx
        zs = [dem.sample(x, y)]
        for r in probes:
            zs.append(dem.sample(x + nx * r, y + ny * r))
            zs.append(dem.sample(x - nx * r, y - ny * r))
            zs.append(dem.sample(x + tx * r, y + ty * r))
            zs.append(dem.sample(x - tx * r, y - ty * r))
        new.append((x, y, min(zs)))
    return new


def smooth_z_with_slope_limit(cl, max_grade=0.09, passes_ma=3, passes_slope=3):
    """Smoothing z con vincolo di pendenza massima (default 9% per strada montana).
    Uniforma i gradini artificiali del DEM e impedisce scalini impossibili."""
    if len(cl) < 3:
        return cl
    ds = [0.0]
    for i in range(1, len(cl)):
        ds.append(math.hypot(cl[i][0] - cl[i - 1][0], cl[i][1] - cl[i - 1][1]))
    z = [c[2] for c in cl]

    for _ in range(passes_ma):
        z2 = z[:]
        for i in range(1, len(z) - 1):
            z2[i] = (z[i - 1] + 2 * z[i] + z[i + 1]) / 4.0
        z = z2

    for _ in range(passes_slope):
        for i in range(1, len(z)):
            d = ds[i]
            if d <= 0: continue
            dz = z[i] - z[i - 1]
            lim = max_grade * d
            if dz > lim:   z[i] = z[i - 1] + lim
            elif dz < -lim: z[i] = z[i - 1] - lim
        for i in range(len(z) - 2, -1, -1):
            d = ds[i + 1]
            if d <= 0: continue
            dz = z[i] - z[i + 1]
            lim = max_grade * d
            if dz > lim:   z[i] = z[i + 1] + lim
            elif dz < -lim: z[i] = z[i + 1] - lim

    return [(x, y, zz) for (x, y, _), zz in zip(cl, z)]


def compute_banking(curvs):
    out = []
    for c in curvs:
        norm = max(-1.0, min(1.0, c / BANK_CURV_NORM))
        out.append(-norm * BANK_MAX_DEG)
    w = 9
    half = w // 2
    sm = []
    for i in range(len(out)):
        i0, i1 = max(0, i - half), min(len(out), i + half + 1)
        sm.append(sum(out[i0:i1]) / (i1 - i0))
    return sm


# ======== Road strips ==================================================
def offset_strip(cl, tans, banking, off_inner, off_outer, z_lift):
    verts, faces = [], []
    n = len(cl)
    for i, (x, y, z) in enumerate(cl):
        tx, ty = tans[i]
        nx, ny = -ty, tx
        roll = math.radians(banking[i])
        z_inner = z + z_lift + off_inner * math.sin(roll)
        z_outer = z + z_lift + off_outer * math.sin(roll)
        verts.append((x + nx * off_inner, y + ny * off_inner, z_inner))
        verts.append((x + nx * off_outer, y + ny * off_outer, z_outer))
    for i in range(n - 1):
        a = 2 * i; b = 2 * i + 1
        c = 2 * (i + 1) + 1; d = 2 * (i + 1)
        faces.append((a, b, c, d))
    return verts, faces


def build_road_with_lines(cl, tans, banking, curvs, width, mats, has_line):
    half = width / 2
    col = ensure_collection("Road")

    lv, lf = offset_strip(cl, tans, banking, half, half + SHOULDER_W, ROAD_THICKNESS * 0.5)
    new_mesh_object("Shoulder_L", lv, lf, col, mats["shoulder"])
    rv, rf = offset_strip(cl, tans, banking, -half - SHOULDER_W, -half, ROAD_THICKNESS * 0.5)
    new_mesh_object("Shoulder_R", rv, rf, col, mats["shoulder"])

    av, af = offset_strip(cl, tans, banking, -half, half, ROAD_THICKNESS)
    road = new_mesh_object("Road", av, af, col, mats["asphalt"])
    _add_uv_along(road, cl)

    build_center_marking(cl, tans, banking, curvs, mats, has_line=has_line)

    # Linee di bordo: solo dove c'è la mezzeria (proxy: stessa "qualità" di asfalto)
    edge_off = half - EDGE_INSET
    if has_line is None:
        elv, elf = offset_strip(cl, tans, banking,
                                edge_off - MARKING_EDGE_W / 2, edge_off + MARKING_EDGE_W / 2,
                                ROAD_THICKNESS + 0.005)
        new_mesh_object("MarkingEdge_L", elv, elf, col, mats["line_w"])
        erv, erf = offset_strip(cl, tans, banking,
                                -edge_off - MARKING_EDGE_W / 2, -edge_off + MARKING_EDGE_W / 2,
                                ROAD_THICKNESS + 0.005)
        new_mesh_object("MarkingEdge_R", erv, erf, col, mats["line_w"])
    else:
        # Emetto per intervalli "has_line" continui
        cums = [0.0]
        for i in range(1, len(cl)):
            cums.append(cums[-1] + math.hypot(cl[i][0] - cl[i - 1][0],
                                              cl[i][1] - cl[i - 1][1]))
        z_lift = ROAD_THICKNESS + 0.005
        for sign, name in ((+1, "MarkingEdge_L"), (-1, "MarkingEdge_R")):
            verts, faces = [], []
            in_run = False; d_start = 0
            for i in range(len(cl)):
                if has_line[i] and not in_run:
                    in_run = True; d_start = cums[i]
                elif not has_line[i] and in_run:
                    _emit_strip_at(verts, faces, cl, tans, cums,
                                   d_start, cums[i],
                                   MARKING_EDGE_W / 2, z_lift,
                                   lateral=sign * edge_off)
                    in_run = False
            if in_run:
                _emit_strip_at(verts, faces, cl, tans, cums,
                               d_start, cums[-1],
                               MARKING_EDGE_W / 2, z_lift,
                               lateral=sign * edge_off)
            if verts:
                new_mesh_object(name, verts, faces, col, mats["line_w"])


def build_road(cl, tans, banking, curvs, width, mats):
    half = width / 2
    col = ensure_collection("Road")

    lv, lf = offset_strip(cl, tans, banking, half, half + SHOULDER_W, ROAD_THICKNESS * 0.5)
    new_mesh_object("Shoulder_L", lv, lf, col, mats["shoulder"])
    rv, rf = offset_strip(cl, tans, banking, -half - SHOULDER_W, -half, ROAD_THICKNESS * 0.5)
    new_mesh_object("Shoulder_R", rv, rf, col, mats["shoulder"])

    av, af = offset_strip(cl, tans, banking, -half, half, ROAD_THICKNESS)
    road = new_mesh_object("Road", av, af, col, mats["asphalt"])
    _add_uv_along(road, cl)

    # Linea di mezzeria: CONTINUA in curva, TRATTEGGIATA in rettilineo
    build_center_marking(cl, tans, banking, curvs, mats)

    # Linee di bordo: continue (convenzione italiana)
    edge_off = half - EDGE_INSET
    elv, elf = offset_strip(cl, tans, banking,
                            edge_off - MARKING_EDGE_W / 2, edge_off + MARKING_EDGE_W / 2,
                            ROAD_THICKNESS + 0.005)
    new_mesh_object("MarkingEdge_L", elv, elf, col, mats["line_w"])
    erv, erf = offset_strip(cl, tans, banking,
                            -edge_off - MARKING_EDGE_W / 2, -edge_off + MARKING_EDGE_W / 2,
                            ROAD_THICKNESS + 0.005)
    new_mesh_object("MarkingEdge_R", erv, erf, col, mats["line_w"])


def _sample_at_dist(cl, tans, cums, d):
    idx = bisect.bisect_right(cums, d) - 1
    idx = max(0, min(len(cl) - 2, idx))
    seg = cums[idx + 1] - cums[idx]
    t = (d - cums[idx]) / seg if seg > 0 else 0.0
    p1 = cl[idx]; p2 = cl[idx + 1]
    x = p1[0] + (p2[0] - p1[0]) * t
    y = p1[1] + (p2[1] - p1[1]) * t
    z = p1[2] + (p2[2] - p1[2]) * t
    return (x, y, z, tans[idx][0], tans[idx][1], idx)


def _emit_strip_at(verts, faces, cl, tans, cums, d_start, d_end,
                   half_w, z_lift, sub_step=1.5, lateral=0.0):
    """Emette una mini-strip da d_start a d_end seguendo la curva.
    `lateral` = offset perpendicolare (positivo = sinistra) per le linee di bordo."""
    samples = []
    d = d_start
    while d < d_end:
        samples.append(_sample_at_dist(cl, tans, cums, d))
        d += sub_step
    samples.append(_sample_at_dist(cl, tans, cums, d_end))
    base = len(verts)
    for x, y, z, tx, ty, _ in samples:
        nx, ny = -ty, tx
        cx = x + nx * lateral
        cy = y + ny * lateral
        verts.append((cx + nx * half_w, cy + ny * half_w, z + z_lift))
        verts.append((cx - nx * half_w, cy - ny * half_w, z + z_lift))
    n = len(samples)
    for i in range(n - 1):
        a = base + 2 * i; b = base + 2 * i + 1
        c = base + 2 * (i + 1) + 1; d2 = base + 2 * (i + 1)
        faces.append((a, b, c, d2))


def build_center_marking(cl, tans, banking, curvs, mats, has_line=None,
                          dash_len=4.5, gap_len=4.5, curv_thresh=0.012):
    """Mezzeria: continua nei tratti con |curvatura| > curv_thresh
    (≈ raggio < 80 m, dove vietato sorpasso), tratteggiata altrove.
    Se `has_line` (bool per cl point) → salta le sezioni False (linea assente)."""
    if has_line is None:
        has_line = [True] * len(cl)
    cums = [0.0]
    for i in range(1, len(cl)):
        cums.append(cums[-1] + math.hypot(cl[i][0] - cl[i - 1][0],
                                          cl[i][1] - cl[i - 1][1]))
    total = cums[-1]
    half_w = MARKING_CENTER_W / 2
    z_lift = ROAD_THICKNESS + 0.005

    # Per ogni "step" lungo cl, decido tipo: no_line / curve / straight
    step = 2.0
    intervals = []  # (d_start, d_end, type)
    cur_type = None
    seg_start = 0.0
    d = 0.0
    while d <= total:
        idx = bisect.bisect_right(cums, d) - 1
        idx = max(0, min(len(cl) - 1, idx))
        if not has_line[idx]:
            new_type = "no_line"
        else:
            w = 2
            i0, i1 = max(0, idx - w), min(len(cl), idx + w + 1)
            cmax = max(abs(c) for c in curvs[i0:i1])
            new_type = "curve" if cmax > curv_thresh else "straight"
        if cur_type is None:
            cur_type = new_type
        if new_type != cur_type:
            intervals.append((seg_start, d, cur_type))
            seg_start = d
            cur_type = new_type
        d += step
    intervals.append((seg_start, total, cur_type or "straight"))

    verts, faces = [], []
    for d_start, d_end, typ in intervals:
        if typ == "no_line":
            continue
        if typ == "curve":
            _emit_strip_at(verts, faces, cl, tans, cums,
                           d_start, d_end, half_w, z_lift)
        else:
            d = d_start
            # allineo le tratteggi al multiplo di (dash+gap) per continuità visiva
            phase = d % (dash_len + gap_len)
            if phase < dash_len:
                # già dentro un dash, completalo
                first_end = min(d_end, d + (dash_len - phase))
                _emit_strip_at(verts, faces, cl, tans, cums,
                               d, first_end, half_w, z_lift)
                d = first_end + gap_len
            else:
                d += (dash_len + gap_len) - phase
            while d < d_end:
                seg_end = min(d + dash_len, d_end)
                _emit_strip_at(verts, faces, cl, tans, cums,
                               d, seg_end, half_w, z_lift)
                d = seg_end + gap_len

    if verts:
        col = ensure_collection("Road")
        new_mesh_object("MarkingCenter", verts, faces, col, mats["line_w"])
    n_curve = sum(1 for _, _, t in intervals if t == "curve")
    n_str = sum(1 for _, _, t in intervals if t == "straight")
    n_no = sum(1 for _, _, t in intervals if t == "no_line")
    log(f"  mezzeria: {n_curve} continui, {n_str} tratteggiati, {n_no} senza linea")


def _add_uv_along(obj, cl):
    mesh = obj.data
    uv = mesh.uv_layers.new(name="UVMap")
    cum = [0.0]
    for i in range(1, len(cl)):
        cum.append(cum[-1] + math.dist(cl[i], cl[i - 1]))
    for poly in mesh.polygons:
        for li in poly.loop_indices:
            vi = mesh.loops[li].vertex_index
            seg = vi // 2
            side = vi % 2
            uv.data[li].uv = (cum[seg] / 4.0, float(side))


# ======== Terrain ======================================================
def build_terrain_from_dem(dem_sampler, mats, corridor):
    """Costruisce il terreno solo nelle celle DEM che toccano il corridoio.
    Rimappa gli indici per compattare la mesh."""
    col = ensure_collection("Terrain")
    rows, cols = dem_sampler.rows, dem_sampler.cols
    all_x = [None] * cols
    all_y = [None] * rows
    for i in range(cols):
        tx = i / (cols - 1)
        all_x[i] = dem_sampler.x_min + tx * (dem_sampler.x_max - dem_sampler.x_min)
    for j in range(rows):
        ty = j / (rows - 1)
        all_y[j] = dem_sampler.y_min + ty * (dem_sampler.y_max - dem_sampler.y_min)

    # maschera di vertici in corridoio
    kept_mask = [[False] * cols for _ in range(rows)]
    # tengo vertici con distanza <= CORRIDOR_M, più una riga/colonna di "bordo"
    for j in range(rows):
        for i in range(cols):
            if corridor.inside(all_x[i], all_y[j]):
                kept_mask[j][i] = True
    # espandi di 1 cella per evitare buchi nei bordi
    expand = [[v for v in row] for row in kept_mask]
    for j in range(rows):
        for i in range(cols):
            if kept_mask[j][i]:
                for dj in (-1, 0, 1):
                    for di in (-1, 0, 1):
                        nj, ni = j + dj, i + di
                        if 0 <= nj < rows and 0 <= ni < cols:
                            expand[nj][ni] = True
    kept_mask = expand

    # rimappo indici compatti
    idx_map = [[-1] * cols for _ in range(rows)]
    verts = []
    for j in range(rows):
        for i in range(cols):
            if kept_mask[j][i]:
                idx_map[j][i] = len(verts)
                verts.append((all_x[i], all_y[j], dem_sampler.grid[j][i]))
    faces = []
    for j in range(rows - 1):
        for i in range(cols - 1):
            a, b = idx_map[j][i], idx_map[j][i + 1]
            c, d = idx_map[j + 1][i + 1], idx_map[j + 1][i]
            if a < 0 or b < 0 or c < 0 or d < 0:
                continue
            faces.append((a, b, c, d))
    return new_mesh_object("Terrain", verts, faces, col, mats["terrain"])


def carve_terrain_under_road(terrain, cl, carve_width, depth=CARVE_DEPTH_M,
                             blend_factor=CARVE_BLEND_FACTOR):
    """Abbassa i vertici del terreno entro `carve_width/2` dalla strada a
    (z_strada - depth). Oltre, blend lineare fino a `carve_width/2 * blend_factor`.
    Cruciale per evitare clipping dato che il DEM è a 60m."""
    half = carve_width / 2
    blend_outer = half * blend_factor
    bucket = {}
    bres = max(50.0, blend_outer)
    for (x, y, z) in cl:
        bucket.setdefault((int(x // bres), int(y // bres)), []).append((x, y, z))

    def nearby(x, y):
        bx, by = int(x // bres), int(y // bres)
        out = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                out.extend(bucket.get((bx + dx, by + dy), []))
        return out

    mesh = terrain.data
    for v in mesh.vertices:
        x, y, z_orig = v.co
        cand = nearby(x, y)
        if not cand:
            continue
        best = min(cand, key=lambda p: (p[0] - x) ** 2 + (p[1] - y) ** 2)
        d = math.hypot(best[0] - x, best[1] - y)
        target = best[2] - depth
        # CRUCIALE: solo abbassare. Se il terreno è già sotto la strada
        # (es. strada su rilevato/viadotto), lascialo stare.
        if z_orig <= target:
            continue
        if d <= half:
            v.co.z = target
        elif d <= blend_outer:
            t = (d - half) / (blend_outer - half)
            # blend tra z_orig e target, ma mai alzare sopra z_orig
            new_z = z_orig * t + target * (1 - t)
            v.co.z = min(z_orig, new_z)


def subdivide_terrain_near_road(terrain, cl, corridor_m=60.0, cuts=2):
    """Suddivide le facce del terreno entro corridor_m dalla strada in `cuts+1`
    per lato, creando vertici intermedi. Dopo la suddivisione, il carve potrà
    appiattire il terreno in modo denso evitando il clipping."""
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(terrain.data)
    bm.verts.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    bres = max(30.0, corridor_m)
    bucket = {}
    for (x, y, z) in cl:
        bucket.setdefault((int(x // bres), int(y // bres)), []).append((x, y))

    def near_road(x, y, r2):
        bx, by = int(x // bres), int(y // bres)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for (cx, cy) in bucket.get((bx + dx, by + dy), ()):
                    if (cx - x) ** 2 + (cy - y) ** 2 <= r2:
                        return True
        return False

    r2 = corridor_m * corridor_m
    target_edges = set()
    for f in bm.faces:
        cx = sum(v.co.x for v in f.verts) / len(f.verts)
        cy = sum(v.co.y for v in f.verts) / len(f.verts)
        if near_road(cx, cy, r2):
            for e in f.edges:
                target_edges.add(e)

    if target_edges:
        bmesh.ops.subdivide_edges(
            bm, edges=list(target_edges),
            cuts=cuts, use_grid_fill=True,
        )

    bm.to_mesh(terrain.data)
    bm.free()
    log(f"  terrain subdivide: {len(target_edges)} edge, cuts={cuts}")


def densify_terrain_near_road_unused(terrain, cl, corridor_m=40.0, step_m=6.0):
    """Aggiunge vertici 'di cucitura' a ogni punto centerline e a offset laterali,
    usando bmesh con knife/subdivide? Troppo complicato in pratica -
    qui uso un approccio diretto: per ogni punto cl, trova la faccia corrente
    del terreno e "forza" un nuovo vertice alla quota strada-depth."""
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(terrain.data)
    bm.faces.ensure_lookup_table()

    # indice faces per bucket spaziale
    bres = 60.0
    fbucket = {}
    for f in bm.faces:
        cx = sum(v.co.x for v in f.verts) / len(f.verts)
        cy = sum(v.co.y for v in f.verts) / len(f.verts)
        fbucket.setdefault((int(cx // bres), int(cy // bres)), []).append(f)

    def nearest_face(x, y):
        bx, by = int(x // bres), int(y // bres)
        cand = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                cand.extend(fbucket.get((bx + dx, by + dy), []))
        if not cand:
            return None
        def inside(f, x, y):
            verts = [(v.co.x, v.co.y) for v in f.verts]
            n = len(verts); j = n - 1; inside_ = False
            for i in range(n):
                xi, yi = verts[i]; xj, yj = verts[j]
                if ((yi > y) != (yj > y)) and \
                   (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
                    inside_ = not inside_
                j = i
            return inside_
        for f in cand:
            if inside(f, x, y):
                return f
        return None

    added = 0
    # sub-campiono la centerline al passo step_m e piazzo vertici sopra il terreno
    cum = 0.0
    last = cl[0]
    samples = [cl[0]]
    for p in cl[1:]:
        d = math.hypot(p[0] - last[0], p[1] - last[1])
        while cum + d >= step_m:
            remain = step_m - cum
            t = remain / d if d else 0
            px = last[0] + (p[0] - last[0]) * t
            py = last[1] + (p[1] - last[1]) * t
            pz = last[2] + (p[2] - last[2]) * t
            samples.append((px, py, pz))
            last = (px, py, pz)
            d -= remain
            cum = 0.0
        cum += d
        last = p
    samples.append(cl[-1])

    for (x, y, z) in samples:
        f = nearest_face(x, y)
        if f is None:
            continue
        # creo un nuovo vertice e poke face in quel punto
        try:
            new_v = bm.verts.new((x, y, z - CARVE_DEPTH_M))
            # collega il nuovo vertice a tutti i vertici della faccia (fan)
            fv = list(f.verts)
            bm.faces.remove(f)
            for i in range(len(fv)):
                a = fv[i]; b = fv[(i + 1) % len(fv)]
                try:
                    bm.faces.new([a, b, new_v])
                except ValueError:
                    pass
            added += 1
        except Exception:
            pass

    bm.to_mesh(terrain.data)
    bm.free()
    log(f"  densified terrain: +{added} vertices sotto la strada")


# ======== Guardrail ====================================================
def build_guardrails(cl, tans, banking, road_width, dem, mats):
    col = ensure_collection("Guardrails")
    half = road_width / 2 + SHOULDER_W + GUARDRAIL_OFFSET
    n = len(cl)
    expL = [False] * n
    expR = [False] * n
    probe = 4.0
    for i, (x, y, z) in enumerate(cl):
        tx, ty = tans[i]
        nx, ny = -ty, tx
        xl = x + nx * (half + probe); yl = y + ny * (half + probe)
        xr = x - nx * (half + probe); yr = y - ny * (half + probe)
        zl = dem.sample(xl, yl); zr = dem.sample(xr, yr)
        if z - zl > GUARDRAIL_DROP: expL[i] = True
        if z - zr > GUARDRAIL_DROP: expR[i] = True

    def runs(mask):
        out = []; start = None
        for i, v in enumerate(mask):
            if v and start is None:
                start = i
            elif not v and start is not None:
                if i - start >= 3:
                    out.append((start, i - 1))
                start = None
        if start is not None and n - start >= 3:
            out.append((start, n - 1))
        return out

    # batcho tutti i guardrail in una sola mesh per lato
    for side, mask in (("L", expL), ("R", expR)):
        verts = []; faces = []
        for i0, i1 in runs(mask):
            base = len(verts)
            for k, i in enumerate(range(i0, i1 + 1)):
                x, y, z = cl[i]
                tx, ty = tans[i]
                nx, ny = -ty, tx
                sign = +1 if side == "L" else -1
                ox = x + nx * (half * sign)
                oy = y + ny * (half * sign)
                verts.append((ox, oy, z))
                verts.append((ox, oy, z + GUARDRAIL_HEIGHT))
            m = i1 - i0
            for k in range(m):
                a = base + 2 * k; b = base + 2 * k + 1
                c = base + 2 * (k + 1) + 1; d = base + 2 * (k + 1)
                faces.append((a, b, c, d))
        if verts:
            new_mesh_object(f"Guardrail_{side}", verts, faces, col, mats["guardrail"])


# ======== Batched mesh helpers =========================================
def add_polygon_footprint(verts, faces, poly_xy, base_z, top_z=None):
    """Aggiunge un poligono (come pavimento) o un prisma estruso se top_z è dato."""
    if len(poly_xy) < 3:
        return
    base = len(verts)
    n = len(poly_xy)
    if top_z is None:
        for x, y in poly_xy:
            verts.append((x, y, base_z))
        faces.append(tuple(range(base, base + n)))
        return
    for x, y in poly_xy:
        verts.append((x, y, base_z))
    for x, y in poly_xy:
        verts.append((x, y, top_z))
    # pareti
    for i in range(n):
        a = base + i
        b = base + (i + 1) % n
        c = base + n + (i + 1) % n
        d = base + n + i
        faces.append((a, b, c, d))
    # tetto
    faces.append(tuple(range(base + n, base + 2 * n)))
    # pavimento (inverted)
    faces.append(tuple(range(base + n - 1, base - 1, -1)))


def build_grass_tufts(cl, tans, dem, mats, corridor,
                        spacing=3.0, lateral_min=4.0, lateral_max=80.0,
                        chance=0.45):
    """Ciuffi d'erba (piccolissime cupole verdi 0.2-0.4 m) sparsi sul terreno.
    Riempie il vuoto visivo. Molti = pieno, pochi = sparso."""
    import random
    random.seed(909)
    col = ensure_collection("Grass")
    verts, faces = [], []
    placed = 0
    for x, y, z, tx, ty, _d in walk_centerline(cl, tans, spacing):
        nx, ny = -ty, tx
        for side in (+1, -1):
            for _step in range(int((lateral_max - lateral_min) / 4.0)):
                if random.random() > chance:
                    continue
                lat = lateral_min + random.random() * (lateral_max - lateral_min)
                jx = x + nx * lat * side + tx * random.uniform(-1.5, 1.5)
                jy = y + ny * lat * side + ty * random.uniform(-1.5, 1.5)
                if not corridor.inside(jx, jy):
                    continue
                zg = dem.sample(jx, jy)
                size = 0.18 + random.random() * 0.22
                _add_tuft(verts, faces, jx, jy, zg, size)
                placed += 1
    if verts:
        new_mesh_object("Grass", verts, faces, col, mats["forest"])
    log(f"  ciuffi d'erba: {placed}")


def _add_tuft(verts, faces, x, y, z, size):
    """Mini-tetraedro verticale, low poly."""
    s = size
    base = len(verts)
    verts.extend([
        (x - s, y, z),
        (x + s, y, z),
        (x, y - s, z),
        (x, y + s, z),
        (x, y, z + s * 1.5),
    ])
    apex = base + 4
    faces.append((base + 0, base + 2, apex))
    faces.append((base + 2, base + 1, apex))
    faces.append((base + 1, base + 3, apex))
    faces.append((base + 3, base + 0, apex))


def build_cypresses_along_road(cl, tans, dem, mats, corridor,
                                spacing=22.0, lateral=14.0, chance=0.18):
    """Cipressi italiani: alberi alti e stretti, sporadici lungo strada.
    Tipici dei colli toscani/molisani — danno carattere mediterraneo."""
    import random
    random.seed(303)
    col = ensure_collection("Trees")
    vt, ft, vc, fc = [], [], [], []
    placed = 0
    for x, y, z, tx, ty, _d in walk_centerline(cl, tans, spacing):
        nx, ny = -ty, tx
        for side in (+1, -1):
            if random.random() > chance:
                continue
            lat = lateral + random.uniform(-3, 6)
            ox = x + nx * lat * side
            oy = y + ny * lat * side
            if not corridor.inside(ox, oy):
                continue
            zg = dem.sample(ox, oy)
            _add_cypress(vt, ft, vc, fc, ox, oy, zg)
            placed += 1
    if vt:
        new_mesh_object("CypressTrunks", vt, ft, col, mats["trunk"])
    if vc:
        new_mesh_object("CypressCanopies", vc, fc, col, mats["canopy"])
    log(f"  cipressi: {placed}")


def _add_cypress(verts_t, faces_t, verts_c, faces_c, x, y, z):
    """Cipresso: trunk basso + canopy fusiforme alto e stretto."""
    import random
    h = 7.0 + random.random() * 4.0  # 7-11 m
    tw = 0.18
    # trunk
    bt = len(verts_t)
    verts_t.extend([
        (x - tw, y - tw, z), (x + tw, y - tw, z),
        (x + tw, y + tw, z), (x - tw, y + tw, z),
        (x - tw, y - tw, z + 0.8), (x + tw, y - tw, z + 0.8),
        (x + tw, y + tw, z + 0.8), (x - tw, y + tw, z + 0.8),
    ])
    faces_t.extend([
        (bt + 0, bt + 1, bt + 5, bt + 4),
        (bt + 1, bt + 2, bt + 6, bt + 5),
        (bt + 2, bt + 3, bt + 7, bt + 6),
        (bt + 3, bt + 0, bt + 4, bt + 7),
    ])
    # canopy fusiforme: 6 verts (apex bottom + 4 wide at quarter + 1 apex top)
    cw = 0.85
    bc = len(verts_c)
    verts_c.extend([
        (x, y, z + 0.5),
        (x + cw, y, z + h * 0.30),
        (x, y + cw, z + h * 0.30),
        (x - cw, y, z + h * 0.30),
        (x, y - cw, z + h * 0.30),
        (x, y, z + h),
    ])
    faces_c.extend([
        (bc + 0, bc + 1, bc + 2),
        (bc + 0, bc + 2, bc + 3),
        (bc + 0, bc + 3, bc + 4),
        (bc + 0, bc + 4, bc + 1),
        (bc + 5, bc + 2, bc + 1),
        (bc + 5, bc + 3, bc + 2),
        (bc + 5, bc + 4, bc + 3),
        (bc + 5, bc + 1, bc + 4),
    ])


def build_stone_walls(cl, tans, dem, mats, corridor,
                       spacing=120.0, lateral=18.0, length=12.0,
                       chance=0.35):
    """Muretti a secco (low gray boxes) perpendicolari alla strada,
    sporadici nei campi laterali. Tipici dei paesaggi rurali italiani."""
    import random
    random.seed(606)
    col = ensure_collection("Walls")
    verts, faces = [], []
    placed = 0
    for x, y, z, tx, ty, _d in walk_centerline(cl, tans, spacing):
        nx, ny = -ty, tx
        for side in (+1, -1):
            if random.random() > chance:
                continue
            lat = lateral + random.uniform(-4, 8)
            cx = x + nx * lat * side
            cy = y + ny * lat * side
            if not corridor.inside(cx, cy):
                continue
            zg = dem.sample(cx, cy)
            # muretto orientato lungo la strada (parallelo alla tangente)
            angle = random.uniform(-0.5, 0.5)  # piccolo random
            rot_tx = tx * math.cos(angle) - ty * math.sin(angle)
            rot_ty = tx * math.sin(angle) + ty * math.cos(angle)
            _add_box_oriented(verts, faces, cx, cy, zg,
                              rot_tx, rot_ty, length, 0.4, 0.7)
            placed += 1
    if verts:
        wall_mat = mat_pbr("StoneWall", (0.55, 0.50, 0.42), roughness=0.95)
        new_mesh_object("StoneWalls", verts, faces, col, wall_mat)
    log(f"  muretti a secco: {placed}")


def _add_box_oriented(verts, faces, cx, cy, z, tx, ty, length, width, height):
    """Box orientato lungo (tx,ty), centrato in (cx, cy, z)."""
    nx, ny = -ty, tx
    hl, hw = length / 2, width / 2
    base = len(verts)
    for dz in (0, height):
        verts.extend([
            (cx + tx * hl + nx * hw, cy + ty * hl + ny * hw, z + dz),
            (cx + tx * hl - nx * hw, cy + ty * hl - ny * hw, z + dz),
            (cx - tx * hl - nx * hw, cy - ty * hl - ny * hw, z + dz),
            (cx - tx * hl + nx * hw, cy - ty * hl + ny * hw, z + dz),
        ])
    for i in range(4):
        a = base + i
        b2 = base + (i + 1) % 4
        c = base + 4 + (i + 1) % 4
        d = base + 4 + i
        faces.append((a, b2, c, d))
    faces.append((base + 4, base + 5, base + 6, base + 7))


def build_speed_signs(cl, tans, curvs, dem, mats, road_width, base_speed=70):
    """Cartelli rotondi limite velocità: 70 km/h sui rettilinei, 50 km/h su curve.
    Posizionati ogni ~1.5 km."""
    half = road_width / 2 + SHOULDER_W + 0.4
    cums = [0.0]
    for i in range(1, len(cl)):
        cums.append(cums[-1] + math.hypot(cl[i][0] - cl[i - 1][0],
                                          cl[i][1] - cl[i - 1][1]))
    col = ensure_collection("Signals")
    vp, fp = [], []   # palo
    v_red, f_red = [], []   # bordo rosso (octagono per velocità lente)
    placed = 0
    next_d = 800.0
    for i in range(len(cl)):
        if cums[i] >= next_d:
            x, y, z = cl[i]
            tx, ty = tans[i]
            nx, ny = -ty, tx
            ox = x + nx * half
            oy = y + ny * half
            _add_pole(vp, fp, ox, oy, z, w=0.06, h=2.4)
            # cartello rotondo (ottagono piatto verticale)
            cz = z + 2.4
            r = 0.4
            base = len(v_red)
            v_red.append((ox, oy, cz))  # centro
            for k in range(8):
                a = k * 2 * math.pi / 8 + math.pi / 8
                # disco verticale: piano (tx,ty,0) orizzontale + (0,0,1) verticale
                v_red.append((ox + tx * r * math.cos(a),
                              oy + ty * r * math.cos(a),
                              cz + r * math.sin(a)))
            for k in range(8):
                f_red.append((base, base + 1 + k, base + 1 + (k + 1) % 8))
            placed += 1
            next_d += 1500.0
    if vp:
        new_mesh_object("SpeedSigns_Pole", vp, fp, col, mats["pole"])
    if v_red:
        new_mesh_object("SpeedSigns_Disc", v_red, f_red, col, mats["sign"])
    log(f"  cartelli velocità: {placed}")


def build_chimneys_on_buildings(buildings, proj, dem, corridor, mats,
                                  min_road_dist=8.0):
    """Aggiunge un piccolo camino (box rettangolare) sul tetto di ogni edificio."""
    import random
    rng = random.Random(7)
    col = ensure_collection("Buildings")
    verts, faces = [], []
    cnt = 0
    for b in buildings:
        poly = [proj.xy(lat, lon) for lat, lon in b["coords"]]
        if len(poly) < 3:
            continue
        if poly[0] == poly[-1]:
            poly = poly[:-1]
        if not corridor.any_inside(poly):
            continue
        if min(corridor.min_dist_to_road(x, y) for x, y in poly) < min_road_dist:
            continue
        # 50% chance
        if rng.random() < 0.5:
            continue
        cx = sum(x for x, _ in poly) / len(poly)
        cy = sum(y for _, y in poly) / len(poly)
        # leggero offset random dal centro
        cx += rng.uniform(-1, 1)
        cy += rng.uniform(-1, 1)
        base_z = sum(dem.sample(x, y) for x, y in poly) / len(poly) - 0.1
        h_tag = b.get("height")
        if h_tag:
            height = max(3.0, float(h_tag))
        else:
            rng2 = random.Random(hash(tuple(poly)) & 0xFFFFFF)
            height = 4.0 + rng2.random() * 5.0
        cz = base_z + height + 0.4
        # camino: box 0.3x0.3x0.8
        s = 0.18
        h = 0.7 + rng.random() * 0.4
        b0 = len(verts)
        for dz in (0.0, h):
            verts.extend([
                (cx - s, cy - s, cz + dz),
                (cx + s, cy - s, cz + dz),
                (cx + s, cy + s, cz + dz),
                (cx - s, cy + s, cz + dz),
            ])
        for i in range(4):
            a = b0 + i; bb = b0 + (i + 1) % 4
            c = b0 + 4 + (i + 1) % 4; d = b0 + 4 + i
            faces.append((a, bb, c, d))
        faces.append((b0 + 4, b0 + 5, b0 + 6, b0 + 7))
        cnt += 1
    if verts:
        new_mesh_object("Chimneys", verts, faces, col, mats["roof"])
    log(f"  camini: {cnt}")


def build_bushes_in_forests(forests, proj, dem, mats, corridor, spacing=3.0,
                             chance=0.35):
    """Arbusti (cupole verdi basse) sparsi nei boschi, tra gli alberi."""
    import random
    random.seed(42)
    col = ensure_collection("Trees")
    verts, faces = [], []
    placed = 0
    for f in forests:
        poly = [proj.xy(lat, lon) for lat, lon in f["coords"]]
        if len(poly) < 3:
            continue
        if poly[0] == poly[-1]:
            poly = poly[:-1]
        if not corridor.any_inside(poly):
            continue
        xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
        x_min, x_max = min(xs), max(xs)
        y_min, y_max = min(ys), max(ys)
        x = x_min
        while x <= x_max:
            y = y_min
            while y <= y_max:
                if random.random() < chance:
                    jx = x + (random.random() - 0.5) * spacing * 0.7
                    jy = y + (random.random() - 0.5) * spacing * 0.7
                    if corridor.inside(jx, jy) and _point_in_poly(jx, jy, poly):
                        z = dem.sample(jx, jy)
                        size = 0.6 + random.random() * 0.9
                        _add_bush(verts, faces, jx, jy, z, size)
                        placed += 1
                y += spacing
            x += spacing
    if verts:
        new_mesh_object("Bushes", verts, faces, col, mats["canopy"])
    log(f"  arbusti: {placed}")


def _add_bush(verts, faces, x, y, z, size):
    """Cupola low-poly (5 vertici)."""
    s = size
    base = len(verts)
    verts.extend([
        (x - s, y - s * 0.7, z),
        (x + s, y - s * 0.7, z),
        (x + s * 0.7, y + s, z),
        (x - s * 0.7, y + s, z),
        (x, y, z + s * 1.1),
    ])
    apex = base + 4
    for i in range(4):
        faces.append((base + i, base + (i + 1) % 4, apex))


def build_wires_between_poles(cl, tans, dem, mats, corridor,
                                spacing=80.0, lateral=22.0, side=+1,
                                wire_height=8.0, sag=0.6):
    """Cavi della corrente tra pali consecutivi (catenaria approx)."""
    col = ensure_collection("Signals")
    positions = []
    for x, y, z, tx, ty, _d in walk_centerline(cl, tans, spacing):
        nx, ny = -ty, tx
        ox = x + nx * lateral * side
        oy = y + ny * lateral * side
        if not corridor.inside(ox, oy):
            continue
        zg = dem.sample(ox, oy)
        positions.append((ox, oy, zg + wire_height))
    verts, faces = [], []
    for i in range(len(positions) - 1):
        a = positions[i]; b = positions[i + 1]
        # campiono 12 punti tra a e b con sag parabolico
        N = 12
        line_idx_start = len(verts)
        for k in range(N + 1):
            t = k / N
            x = a[0] + (b[0] - a[0]) * t
            y = a[1] + (b[1] - a[1]) * t
            # catenaria semplificata
            z = a[2] + (b[2] - a[2]) * t - sag * 4 * t * (1 - t)
            # cavo come quad sottile
            verts.append((x - 0.02, y - 0.02, z))
            verts.append((x + 0.02, y + 0.02, z))
        for k in range(N):
            o = line_idx_start + 2 * k
            faces.append((o, o + 1, o + 3, o + 2))
    if verts:
        new_mesh_object("PowerWires", verts, faces, col, mats["pole"])


def add_terrain_noise(terrain, amplitude=0.6, scale=12.0):
    """Aggiunge micro-rumore (Perlin-like) al terreno per rilievo organico."""
    import math, random
    rng = random.Random(13)
    # tabella di gradienti random per noise pseudo-Perlin
    grad_table = [(rng.uniform(-1, 1), rng.uniform(-1, 1)) for _ in range(256)]
    perm = list(range(256))
    rng.shuffle(perm)

    def fade(t): return t * t * t * (t * (t * 6 - 15) + 10)

    def noise(x, y):
        xi = int(math.floor(x)) & 255
        yi = int(math.floor(y)) & 255
        xf = x - math.floor(x)
        yf = y - math.floor(y)
        u, v = fade(xf), fade(yf)
        def grad(xi, yi, xf, yf):
            g = grad_table[perm[(perm[xi] + yi) & 255]]
            return g[0] * xf + g[1] * yf
        n00 = grad(xi, yi, xf, yf)
        n10 = grad((xi + 1) & 255, yi, xf - 1, yf)
        n01 = grad(xi, (yi + 1) & 255, xf, yf - 1)
        n11 = grad((xi + 1) & 255, (yi + 1) & 255, xf - 1, yf - 1)
        return (n00 * (1 - u) + n10 * u) * (1 - v) + (n01 * (1 - u) + n11 * u) * v

    mesh = terrain.data
    for v in mesh.vertices:
        x, y, z = v.co
        n = noise(x / scale, y / scale)
        v.co.z = z + n * amplitude
    log(f"  micro-rumore terreno (ampiezza ±{amplitude}m)")


def build_buildings_batched(buildings, proj, dem, mats, corridor, min_road_dist=8.0):
    """Edifici: walls + roofs come mesh separate per usare materiali diversi.
    Variabilità di altezza pseudo-random basata sul polygon ID."""
    import random
    col = ensure_collection("Buildings")
    v_walls, f_walls = [], []
    v_roof, f_roof = [], []
    kept = 0
    skipped_close = 0
    for b in buildings:
        poly = [proj.xy(lat, lon) for lat, lon in b["coords"]]
        if len(poly) < 3:
            continue
        if poly[0] == poly[-1]:
            poly = poly[:-1]
        if not corridor.any_inside(poly):
            continue
        if min(corridor.min_dist_to_road(x, y) for x, y in poly) < min_road_dist:
            skipped_close += 1
            continue
        base_z = sum(dem.sample(x, y) for x, y in poly) / len(poly) - 0.1
        # altezza varia: tag OSM se presente, altrimenti random 4-9 m
        rng = random.Random(hash(tuple(poly)) & 0xFFFFFF)
        h_tag = b.get("height")
        if h_tag:
            height = max(3.0, float(h_tag))
        else:
            height = 4.0 + rng.random() * 5.0
        # Walls (no top face)
        n = len(poly)
        base = len(v_walls)
        for x, y in poly:
            v_walls.append((x, y, base_z))
        for x, y in poly:
            v_walls.append((x, y, base_z + height))
        for i in range(n):
            a = base + i
            b2 = base + (i + 1) % n
            c = base + n + (i + 1) % n
            d = base + n + i
            f_walls.append((a, b2, c, d))
        # Roof slightly larger and lifted (tetto inclinato semplificato:
        # alzo i vertici opposti a +0.3m per dare un'inclinazione visiva)
        roof_base = len(v_roof)
        roof_z = base_z + height
        for k, (x, y) in enumerate(poly):
            tilt = 0.4 if k < n // 2 else 0.0
            v_roof.append((x, y, roof_z + tilt))
        f_roof.append(tuple(range(roof_base, roof_base + n)))
        kept += 1
    if v_walls:
        new_mesh_object("Buildings_Walls", v_walls, f_walls, col, mats["building"])
    if v_roof:
        new_mesh_object("Buildings_Roofs", v_roof, f_roof, col, mats["roof"])
    log(f"  kept {kept}/{len(buildings)} (scartati {skipped_close} sulla strada)")


def build_forests_batched(forests, proj, dem, mats, corridor):
    col = ensure_collection("Forests")
    verts, faces = [], []
    kept = 0
    for f in forests:
        poly = [proj.xy(lat, lon) for lat, lon in f["coords"]]
        if len(poly) < 3:
            continue
        if poly[0] == poly[-1]:
            poly = poly[:-1]
        if not corridor.any_inside(poly):
            continue
        base = len(verts)
        for x, y in poly:
            verts.append((x, y, dem.sample(x, y) + 0.2))
        faces.append(tuple(range(base, base + len(poly))))
        kept += 1
    if verts:
        new_mesh_object("Forests", verts, faces, col, mats["forest"])
    log(f"  kept {kept}/{len(forests)}")


def _strip_from_polyline(points_xy, width, dem, z_offset):
    verts, faces = [], []
    n = len(points_xy)
    if n < 2:
        return verts, faces
    half = width / 2
    for i, (x, y) in enumerate(points_xy):
        if i == 0:
            dx = points_xy[1][0] - x; dy = points_xy[1][1] - y
        elif i == n - 1:
            dx = x - points_xy[-2][0]; dy = y - points_xy[-2][1]
        else:
            dx = points_xy[i + 1][0] - points_xy[i - 1][0]
            dy = points_xy[i + 1][1] - points_xy[i - 1][1]
        L = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / L, dx / L
        z = dem.sample(x, y) + z_offset
        verts.append((x + nx * half, y + ny * half, z))
        verts.append((x - nx * half, y - ny * half, z))
    for i in range(n - 1):
        a = 2 * i; b = 2 * i + 1
        c = 2 * (i + 1) + 1; d = 2 * (i + 1)
        faces.append((a, b, c, d))
    return verts, faces


def build_waterways_batched(waterways, waterbodies, proj, dem, mats, corridor):
    col = ensure_collection("Water")
    verts, faces = [], []
    kw = kb = 0
    for w in waterways:
        pts = [proj.xy(lat, lon) for lat, lon in w["coords"]]
        if not corridor.any_inside(pts):
            continue
        sv, sf = _strip_from_polyline(pts, 3.0, dem, z_offset=-0.2)
        base = len(verts)
        verts.extend(sv)
        faces.extend([(a + base, b + base, c + base, d + base) for (a, b, c, d) in sf])
        kw += 1
    for wb in waterbodies:
        poly = [proj.xy(lat, lon) for lat, lon in wb["coords"]]
        if len(poly) < 3:
            continue
        if poly[0] == poly[-1]:
            poly = poly[:-1]
        if not corridor.any_inside(poly):
            continue
        base = len(verts)
        for x, y in poly:
            verts.append((x, y, dem.sample(x, y) - 0.1))
        faces.append(tuple(range(base, base + len(poly))))
        kb += 1
    if verts:
        new_mesh_object("Water", verts, faces, col, mats["water"])
    log(f"  kept waterways={kw}/{len(waterways)}, bodies={kb}/{len(waterbodies)}")


def build_other_roads_batched(roads, proj, dem, mats, corridor,
                              overlap_dist=6.0, overlap_max_frac=0.35):
    """Disegna le altre strade del corridoio. Scarta quelle che si sovrappongono
    alla strada principale (>= overlap_max_frac dei punti entro overlap_dist m
    dalla mezzeria SS17): è la SS17 stessa che, per limiti del matching node OSM,
    è finita in 'other_roads'."""
    col = ensure_collection("OtherRoads")
    widths = {"motorway": 10.5, "trunk": 9.0, "primary": 8.0,
              "secondary": 7.0, "tertiary": 6.0, "unclassified": 5.0,
              "residential": 5.0, "service": 4.0, "track": 3.5,
              "path": 2.0, "footway": 1.5}
    verts, faces = [], []
    kept = 0
    skipped_overlap = 0
    for rd in roads:
        kind = rd.get("kind", "unclassified")
        w = widths.get(kind, 4.5)
        pts = [proj.xy(lat, lon) for lat, lon in rd["coords"]]
        if not corridor.any_inside(pts):
            continue
        # Conta quanti punti della way si sovrappongono alla SS17
        close = sum(1 for x, y in pts
                    if corridor.min_dist_to_road(x, y) < overlap_dist)
        if close >= max(1, int(len(pts) * overlap_max_frac)):
            skipped_overlap += 1
            continue
        sv, sf = _strip_from_polyline(pts, w, dem, z_offset=0.05)
        base = len(verts)
        verts.extend(sv)
        faces.extend([(a + base, b + base, c + base, d + base) for (a, b, c, d) in sf])
        kept += 1
    if verts:
        new_mesh_object("OtherRoads", verts, faces, col, mats["otherroad"])
    log(f"  kept {kept}/{len(roads)} (scartati {skipped_overlap} sovrapposti a SS17)")


# ======== Debug aids: highlight nastro + marker + camera ==============
def build_road_highlight(cl, tans, banking, mats, height=8.0, width=2.0):
    """Nastro emissivo arancione sopra la strada per individuarla a colpo d'occhio.
    Collection separata 'Debug' — puoi nasconderla dopo."""
    col = ensure_collection("Debug")
    verts, faces = [], []
    n = len(cl)
    half = width / 2
    for i, (x, y, z) in enumerate(cl):
        tx, ty = tans[i]
        nx, ny = -ty, tx
        verts.append((x + nx * half, y + ny * half, z + height))
        verts.append((x - nx * half, y - ny * half, z + height))
    for i in range(n - 1):
        a = 2 * i; b = 2 * i + 1
        c = 2 * (i + 1) + 1; d = 2 * (i + 1)
        faces.append((a, b, c, d))
    new_mesh_object("RoadHighlight", verts, faces, col, mats["highlight"])


def _cube_verts_faces(cx, cy, cz, size):
    s = size / 2
    verts = [(cx - s, cy - s, cz - s), (cx + s, cy - s, cz - s),
             (cx + s, cy + s, cz - s), (cx - s, cy + s, cz - s),
             (cx - s, cy - s, cz + s), (cx + s, cy - s, cz + s),
             (cx + s, cy + s, cz + s), (cx - s, cy + s, cz + s)]
    faces = [(0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1),
             (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)]
    return verts, faces


def build_markers(cl, mats):
    col = ensure_collection("Debug")
    x, y, z = cl[0]
    v, f = _cube_verts_faces(x, y, z + 15.0, 6.0)
    new_mesh_object("START_A", v, f, col, mats["start"])
    x, y, z = cl[-1]
    v, f = _cube_verts_faces(x, y, z + 15.0, 6.0)
    new_mesh_object("END_B", v, f, col, mats["end"])


def setup_camera(cl):
    """Camera che guarda la strada dall'alto, inquadratura complessiva."""
    xs = [c[0] for c in cl]; ys = [c[1] for c in cl]; zs = [c[2] for c in cl]
    cx = (min(xs) + max(xs)) / 2
    cy = (min(ys) + max(ys)) / 2
    cz = max(zs)
    span = max(max(xs) - min(xs), max(ys) - min(ys))
    cam_data = bpy.data.cameras.new("OverviewCam")
    cam_data.lens = 35
    cam = bpy.data.objects.new("OverviewCam", cam_data)
    cam.location = (cx, cy - span * 0.3, cz + span * 0.7)
    cam.rotation_euler = (math.radians(55), 0, 0)
    bpy.context.collection.objects.link(cam)
    bpy.context.scene.camera = cam
    return cam


# ======== Drive camera che segue la strada =============================
def build_road_curve(cl, name="RoadPath", driver_height=1.6):
    """Crea una Curve NURBS/POLY che segue la centerline, usata come path."""
    curve_data = bpy.data.curves.new(name, "CURVE")
    curve_data.dimensions = "3D"
    spline = curve_data.splines.new("POLY")
    spline.points.add(len(cl) - 1)
    for i, (x, y, z) in enumerate(cl):
        spline.points[i].co = (x, y, z + driver_height, 1.0)
    # abilita il path animation
    curve_data.use_path = True
    curve_data.path_duration = 1  # sovrascritto da animate_path
    curve_obj = bpy.data.objects.new(name, curve_data)
    bpy.context.collection.objects.link(curve_obj)
    return curve_obj


def animate_path(curve_obj, frames):
    """Piazza due keyframe LINEARI su eval_time da 0 a `frames` così la posizione
    lungo il path avanza a velocità costante (compatibile con Blender 4.4+)."""
    cd = curve_obj.data
    cd.path_duration = frames
    # imposto LINEAR come interpolazione di default per i nuovi keyframe
    prefs = bpy.context.preferences.edit
    prev = prefs.keyframe_new_interpolation_type
    prefs.keyframe_new_interpolation_type = "LINEAR"
    try:
        cd.eval_time = 0.0
        cd.keyframe_insert("eval_time", frame=1)
        cd.eval_time = float(frames)
        cd.keyframe_insert("eval_time", frame=frames + 1)
    finally:
        prefs.keyframe_new_interpolation_type = prev


def setup_drive_camera(curve_obj, cl, duration_s=60.0, fps=30):
    """Rig con Follow Path + camera in chase, pronto a premere Play."""
    scene = bpy.context.scene
    scene.render.fps = fps
    frames = int(duration_s * fps)

    animate_path(curve_obj, frames)
    scene.frame_start = 1
    scene.frame_end = frames + 1

    # Rig (empty) che segue il path, orientato tangente alla curva
    rig = bpy.data.objects.new("CarRig", None)
    rig.empty_display_type = "ARROWS"
    rig.empty_display_size = 2.0
    bpy.context.collection.objects.link(rig)
    con = rig.constraints.new("FOLLOW_PATH")
    con.target = curve_obj
    con.use_curve_follow = True
    con.forward_axis = "FORWARD_Y"
    con.up_axis = "UP_Z"

    # Camera chase: dietro di 6 m, sopra di 2.2 m, guarda avanti
    cam_data = bpy.data.cameras.new("DriveCam")
    cam_data.lens = 28
    cam_data.clip_end = 20000.0
    cam = bpy.data.objects.new("DriveCam", cam_data)
    cam.parent = rig
    cam.location = (0.0, -6.0, 2.2)
    # camera forward = -Z; rig forward = +Y → ruoto camera di +90° su X
    cam.rotation_euler = (math.radians(90), 0.0, 0.0)
    bpy.context.collection.objects.link(cam)
    scene.camera = cam  # attiva questa come camera di default

    # Vista cockpit (opzionale, senza chase): camera figlia del rig a z=1.2
    cockpit_data = bpy.data.cameras.new("CockpitCam")
    cockpit_data.lens = 35
    cockpit_data.clip_end = 20000.0
    cockpit = bpy.data.objects.new("CockpitCam", cockpit_data)
    cockpit.parent = rig
    cockpit.location = (0.0, 0.5, 1.2)
    cockpit.rotation_euler = (math.radians(90), 0.0, 0.0)
    bpy.context.collection.objects.link(cockpit)

    log(f"  drive cam: {duration_s:.0f}s @ {fps} fps ({frames} frame), "
        f"velocità media ~{len(cl) * 0:.0f} — controlla la Scena F_end={scene.frame_end}")
    return cam


# ======== Alberi (scattered + individuali) + segnaletica ==============
def _point_in_poly(x, y, poly):
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]; xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and \
           (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _add_tree(verts_t, faces_t, verts_c, faces_c, x, y, z_ground,
              trunk_h=2.2, trunk_w=0.35, canopy_h=5.5, canopy_w=2.8, seed=None):
    """Aggiunge un trunk (box) e una canopy (piramide ottaedrica) ai buffer."""
    import random
    if seed is not None:
        random.seed(seed)
    # variazione naturale
    trunk_h *= 0.8 + 0.4 * random.random()
    canopy_h *= 0.8 + 0.4 * random.random()
    canopy_w *= 0.75 + 0.5 * random.random()
    tw = trunk_w / 2
    # trunk: box
    bt = len(verts_t)
    verts_t.extend([
        (x - tw, y - tw, z_ground),
        (x + tw, y - tw, z_ground),
        (x + tw, y + tw, z_ground),
        (x - tw, y + tw, z_ground),
        (x - tw, y - tw, z_ground + trunk_h),
        (x + tw, y - tw, z_ground + trunk_h),
        (x + tw, y + tw, z_ground + trunk_h),
        (x - tw, y + tw, z_ground + trunk_h),
    ])
    faces_t.extend([
        (bt + 0, bt + 1, bt + 5, bt + 4),
        (bt + 1, bt + 2, bt + 6, bt + 5),
        (bt + 2, bt + 3, bt + 7, bt + 6),
        (bt + 3, bt + 0, bt + 4, bt + 7),
    ])
    # canopy: ottaedrico (6 verts, 8 facce)
    cw = canopy_w / 2
    cz0 = z_ground + trunk_h * 0.7
    cz1 = z_ground + trunk_h + canopy_h * 0.5
    cz2 = z_ground + trunk_h + canopy_h
    bc = len(verts_c)
    verts_c.extend([
        (x, y, cz0),           # base apex
        (x + cw, y, cz1),
        (x, y + cw, cz1),
        (x - cw, y, cz1),
        (x, y - cw, cz1),
        (x, y, cz2),           # top apex
    ])
    faces_c.extend([
        (bc + 0, bc + 1, bc + 2),
        (bc + 0, bc + 2, bc + 3),
        (bc + 0, bc + 3, bc + 4),
        (bc + 0, bc + 4, bc + 1),
        (bc + 5, bc + 2, bc + 1),
        (bc + 5, bc + 3, bc + 2),
        (bc + 5, bc + 4, bc + 3),
        (bc + 5, bc + 1, bc + 4),
    ])


def build_roadside_trees(cl, tans, dem, mats, corridor, spacing=12.0,
                          lateral_min=18.0, lateral_max=35.0):
    """Alberi sparsi lungo la strada a distanza laterale variabile.
    Realismo: filari naturali ai bordi del corridoio, non sull'asfalto."""
    import random
    random.seed(31)
    col = ensure_collection("Trees")
    vt, ft, vc, fc = [], [], [], []
    placed = 0
    for x, y, z, tx, ty, _d in walk_centerline(cl, tans, spacing):
        nx, ny = -ty, tx
        for side in (+1, -1):
            lat = lateral_min + random.random() * (lateral_max - lateral_min)
            jitter = random.uniform(-2.5, 2.5)
            ox = x + nx * lat * side + tx * jitter
            oy = y + ny * lat * side + ty * jitter
            if not corridor.inside(ox, oy):
                continue
            zg = dem.sample(ox, oy)
            _add_tree(vt, ft, vc, fc, ox, oy, zg,
                      trunk_h=2.5, canopy_h=5.5,
                      seed=hash((int(ox * 7), int(oy * 7))) & 0xFFFF)
            placed += 1
    if vt:
        new_mesh_object("RoadsideTrunks", vt, ft, col, mats["trunk"])
    if vc:
        new_mesh_object("RoadsideCanopies", vc, fc, col, mats["canopy"])
    log(f"  roadside trees: {placed}")


def build_trees_scattered(forests, proj, dem, mats, corridor, spacing=6.0):
    import random
    random.seed(17)
    col = ensure_collection("Trees")
    vt, ft, vc, fc = [], [], [], []
    total = 0
    for f in forests:
        poly = [proj.xy(lat, lon) for lat, lon in f["coords"]]
        if len(poly) < 3:
            continue
        if poly[0] == poly[-1]:
            poly = poly[:-1]
        if not corridor.any_inside(poly):
            continue
        xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
        x_min, x_max = min(xs), max(xs)
        y_min, y_max = min(ys), max(ys)
        x = x_min
        while x <= x_max:
            y = y_min
            while y <= y_max:
                jx = x + (random.random() - 0.5) * spacing * 0.6
                jy = y + (random.random() - 0.5) * spacing * 0.6
                if corridor.inside(jx, jy) and _point_in_poly(jx, jy, poly):
                    z = dem.sample(jx, jy)
                    _add_tree(vt, ft, vc, fc, jx, jy, z,
                              seed=hash((int(jx), int(jy))) & 0xFFFF)
                    total += 1
                y += spacing
            x += spacing
    if vt:
        new_mesh_object("TreeTrunks", vt, ft, col, mats["trunk"])
    if vc:
        new_mesh_object("TreeCanopies", vc, fc, col, mats["canopy"])
    log(f"  trees scattered: {total}")


def build_trees_individual(trees, proj, dem, mats, corridor):
    col = ensure_collection("Trees")
    vt, ft, vc, fc = [], [], [], []
    kept = 0
    for t in trees:
        x, y = proj.xy(t["lat"], t["lon"])
        if not corridor.inside(x, y):
            continue
        z = dem.sample(x, y)
        h = max(4.0, float(t.get("height") or 8.0))
        _add_tree(vt, ft, vc, fc, x, y, z,
                  trunk_h=h * 0.35, canopy_h=h * 0.75,
                  canopy_w=h * 0.45, trunk_w=h * 0.06,
                  seed=hash((x, y)) & 0xFFFF)
        kept += 1
    if vt:
        new_mesh_object("TreeTrunks_OSM", vt, ft, col, mats["trunk"])
    if vc:
        new_mesh_object("TreeCanopies_OSM", vc, fc, col, mats["canopy"])
    log(f"  individual OSM trees: {kept}/{len(trees)}")


def _add_pole(verts, faces, x, y, z, w=0.08, h=1.0):
    """Aggiunge un palo (box 4 facce + tetto) ai buffer."""
    r = w / 2
    b = len(verts)
    verts.extend([
        (x - r, y - r, z), (x + r, y - r, z),
        (x + r, y + r, z), (x - r, y + r, z),
        (x - r, y - r, z + h), (x + r, y - r, z + h),
        (x + r, y + r, z + h), (x - r, y + r, z + h),
    ])
    faces.extend([
        (b, b + 1, b + 5, b + 4),
        (b + 1, b + 2, b + 6, b + 5),
        (b + 2, b + 3, b + 7, b + 6),
        (b + 3, b, b + 4, b + 7),
        (b + 4, b + 5, b + 6, b + 7),
    ])


def _add_triangle_sign(verts, faces, x, y, z, tx, ty, height=2.2, size=0.7):
    """Cartello triangolare montato su palo."""
    _add_pole(verts, faces, x, y, z, w=0.06, h=height)
    # triangolo verticale orientato perpendicolare alla strada
    nx, ny = -ty, tx
    bx, by, bz = x, y, z + height
    half = size / 2
    apex_z = bz + size * 0.866
    b = len(verts)
    verts.extend([
        (bx - nx * half, by - ny * half, bz),
        (bx + nx * half, by + ny * half, bz),
        (bx, by, apex_z),
    ])
    faces.append((b, b + 1, b + 2))


def walk_centerline(cl, tans, step):
    """Itera (x, y, z, tx, ty, dist_cumulativa) ogni `step` metri lungo cl."""
    cum = 0.0
    next_d = step
    for i in range(len(cl) - 1):
        p1 = cl[i]; p2 = cl[i + 1]
        seg = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        if seg <= 0:
            continue
        while next_d <= cum + seg:
            t = (next_d - cum) / seg
            x = p1[0] + (p2[0] - p1[0]) * t
            y = p1[1] + (p2[1] - p1[1]) * t
            z = p1[2] + (p2[2] - p1[2]) * t
            tx, ty = tans[i]
            yield (x, y, z, tx, ty, next_d)
            next_d += step
        cum += seg


def build_rocks_scattered(cl, tans, dem, mats, corridor,
                           spacing=18.0, lateral_min=12.0, lateral_max=70.0,
                           rock_chance=0.30):
    """Rocce/sassi (icosaedri irregolari) sparsi sui pendii vicino alla strada."""
    import random
    random.seed(73)
    col = ensure_collection("Rocks")
    verts, faces = [], []
    placed = 0
    for x, y, z, tx, ty, _d in walk_centerline(cl, tans, spacing):
        nx, ny = -ty, tx
        for side in (+1, -1):
            if random.random() > rock_chance:
                continue
            lat = lateral_min + random.random() * (lateral_max - lateral_min)
            ox = x + nx * lat * side + random.uniform(-2, 2)
            oy = y + ny * lat * side + random.uniform(-2, 2)
            if not corridor.inside(ox, oy):
                continue
            zg = dem.sample(ox, oy)
            size = random.uniform(0.4, 1.6)
            _add_rock(verts, faces, ox, oy, zg, size, seed=hash((int(ox), int(oy))))
            placed += 1
    if verts:
        new_mesh_object("Rocks", verts, faces, col, mats["pole"])
    log(f"  rocce: {placed}")


def _add_rock(verts, faces, x, y, z, size, seed=0):
    """Octaedro distorto pseudo-casuale."""
    import random
    rng = random.Random(seed)
    base = len(verts)
    s = size
    pts = [
        ( s,  0,  0), (-s,  0,  0),
        ( 0,  s,  0), ( 0, -s,  0),
        ( 0,  0, s * 0.7),
    ]
    for px, py, pz in pts:
        verts.append((x + px + rng.uniform(-s * 0.2, s * 0.2),
                      y + py + rng.uniform(-s * 0.2, s * 0.2),
                      z + pz + rng.uniform(-s * 0.1, s * 0.1)))
    # 4 triangoli laterali (top apex con base equator)
    apex = base + 4
    for i, j in ((0, 2), (2, 1), (1, 3), (3, 0)):
        faces.append((base + i, base + j, apex))
    # base inferiore (pavimento)
    faces.append((base + 0, base + 3, base + 1, base + 2))


def build_power_poles(cl, tans, dem, mats, corridor,
                       spacing=80.0, lateral=22.0, side=+1):
    """Pali della luce in legno con cima in metallo, lato sinistro a 22m."""
    col = ensure_collection("Signals")
    vp, fp = [], []
    vc, fc = [], []  # cima/croce metallica
    cnt = 0
    for x, y, z, tx, ty, _d in walk_centerline(cl, tans, spacing):
        nx, ny = -ty, tx
        ox = x + nx * lateral * side
        oy = y + ny * lateral * side
        if not corridor.inside(ox, oy):
            continue
        zg = dem.sample(ox, oy)
        _add_pole(vp, fp, ox, oy, zg, w=0.18, h=8.5)
        # croce orizzontale in cima
        cz = zg + 8.0
        bw = 1.5
        bh = 0.08
        b = len(vc)
        vc.extend([
            (ox - tx * bw, oy - ty * bw, cz),
            (ox + tx * bw, oy + ty * bw, cz),
            (ox + tx * bw, oy + ty * bw, cz + bh),
            (ox - tx * bw, oy - ty * bw, cz + bh),
            (ox - tx * bw + nx * 0.05, oy - ty * bw + ny * 0.05, cz),
            (ox + tx * bw + nx * 0.05, oy + ty * bw + ny * 0.05, cz),
            (ox + tx * bw + nx * 0.05, oy + ty * bw + ny * 0.05, cz + bh),
            (ox - tx * bw + nx * 0.05, oy - ty * bw + ny * 0.05, cz + bh),
        ])
        fc.extend([
            (b, b + 1, b + 2, b + 3),
            (b + 4, b + 5, b + 6, b + 7),
            (b, b + 4, b + 7, b + 3),
            (b + 1, b + 5, b + 6, b + 2),
        ])
        cnt += 1
    if vp:
        new_mesh_object("PowerPoles", vp, fp, col, mats["trunk"])
    if vc:
        new_mesh_object("PowerCrosses", vc, fc, col, mats["pole"])
    log(f"  pali della luce: {cnt}")


def _add_quad_oriented(verts, faces, cx, cy, z, tx, ty, length, width):
    """Quad orientato: lunghezza lungo (tx,ty), larghezza perpendicolare."""
    nx, ny = -ty, tx
    hl, hw = length / 2, width / 2
    base = len(verts)
    verts.extend([
        (cx + tx * hl + nx * hw, cy + ty * hl + ny * hw, z),
        (cx + tx * hl - nx * hw, cy + ty * hl - ny * hw, z),
        (cx - tx * hl - nx * hw, cy - ty * hl - ny * hw, z),
        (cx - tx * hl + nx * hw, cy - ty * hl + ny * hw, z),
    ])
    faces.append((base, base + 1, base + 2, base + 3))


def _add_octagon(verts, faces, cx, cy, z, radius):
    """Ottagono piatto centrato in (cx,cy,z)."""
    base = len(verts)
    verts.append((cx, cy, z))  # centro
    for i in range(8):
        a = i * 2 * math.pi / 8
        verts.append((cx + radius * math.cos(a),
                      cy + radius * math.sin(a), z))
    for i in range(8):
        faces.append((base, base + 1 + i, base + 1 + (i + 1) % 8))


def build_road_studs(cl, tans, curvs, road_width, mats, has_line, spacing=10.0):
    """Catarifrangenti (cat's eyes): gialli su mezzeria continua,
    bianchi sui bordi. Solo nei tratti con linea presente."""
    if has_line is None:
        has_line = [True] * len(cl)
    half = road_width / 2
    edge_off = half - EDGE_INSET
    cums = [0.0]
    for i in range(1, len(cl)):
        cums.append(cums[-1] + math.hypot(cl[i][0] - cl[i - 1][0],
                                          cl[i][1] - cl[i - 1][1]))
    total = cums[-1]
    v_y, f_y = [], []   # gialli mezzeria (solo curve)
    v_w, f_w = [], []   # bianchi bordi
    d = spacing
    while d < total:
        idx = bisect.bisect_right(cums, d) - 1
        idx = max(0, min(len(cl) - 1, idx))
        if has_line[idx]:
            x, y, z, tx, ty, _ = _sample_at_dist(cl, tans, cums, d)
            zlift = z + ROAD_THICKNESS + 0.012
            # bordi
            for sign in (+1, -1):
                nx, ny = -ty, tx
                ox = x + nx * sign * edge_off
                oy = y + ny * sign * edge_off
                _add_quad_oriented(v_w, f_w, ox, oy, zlift, tx, ty, 0.18, 0.10)
            # mezzeria solo se in curva (alta curvatura → riflettori gialli)
            if abs(curvs[idx]) > 0.012:
                _add_quad_oriented(v_y, f_y, x, y, zlift, tx, ty, 0.18, 0.10)
        d += spacing
    col = ensure_collection("Road")
    if v_w:
        new_mesh_object("RoadStuds_W", v_w, f_w, col, mats["line_w"])
    if v_y:
        new_mesh_object("RoadStuds_Y", v_y, f_y, col, mats["line_y"])
    log(f"  catarifrangenti: {len(v_w)//4} bianchi, {len(v_y)//4} gialli")


def build_manholes(cl, tans, road_width, mats, spacing=110.0):
    """Tombini di ferro alternati ai lati della carreggiata."""
    half = road_width / 2
    cums = [0.0]
    for i in range(1, len(cl)):
        cums.append(cums[-1] + math.hypot(cl[i][0] - cl[i - 1][0],
                                          cl[i][1] - cl[i - 1][1]))
    verts, faces = [], []
    d = spacing
    cnt = 0
    while d < cums[-1]:
        x, y, z, tx, ty, _ = _sample_at_dist(cl, tans, cums, d)
        nx, ny = -ty, tx
        side = +1 if cnt % 2 == 0 else -1
        ox = x + nx * (half - 0.7) * side
        oy = y + ny * (half - 0.7) * side
        _add_octagon(verts, faces, ox, oy, z + ROAD_THICKNESS + 0.008, 0.30)
        cnt += 1
        d += spacing
    if verts:
        col = ensure_collection("Road")
        manhole_mat = mat_pbr("Manhole", (0.08, 0.08, 0.10),
                              roughness=0.55, metallic=0.5)
        new_mesh_object("Manholes", verts, faces, col, manhole_mat)
    log(f"  tombini: {cnt}")


def build_asphalt_patches(cl, tans, road_width, mats, count=180, seed=271):
    """Quad sottili di asfalto più scuro/chiaro per simulare rappezzi."""
    import random
    rng = random.Random(seed)
    half = road_width / 2 - 0.2
    cums = [0.0]
    for i in range(1, len(cl)):
        cums.append(cums[-1] + math.hypot(cl[i][0] - cl[i - 1][0],
                                          cl[i][1] - cl[i - 1][1]))
    total = cums[-1]
    v_dark, f_dark = [], []
    v_light, f_light = [], []
    for _ in range(count):
        d = rng.uniform(0, total)
        x, y, z, tx, ty, _ = _sample_at_dist(cl, tans, cums, d)
        nx, ny = -ty, tx
        w = rng.uniform(0.5, 2.2)
        L = rng.uniform(0.8, 3.5)
        max_off = max(0, half - w / 2)
        offset = rng.uniform(-max_off, max_off)
        cx = x + nx * offset
        cy = y + ny * offset
        zlift = z + ROAD_THICKNESS + 0.004
        if rng.random() < 0.7:
            _add_quad_oriented(v_dark, f_dark, cx, cy, zlift, tx, ty, L, w)
        else:
            _add_quad_oriented(v_light, f_light, cx, cy, zlift, tx, ty, L, w)
    col = ensure_collection("Road")
    if v_dark:
        m_dark = mat_pbr("AsphaltPatch_Dark", (0.020, 0.020, 0.024),
                         roughness=0.92)
        new_mesh_object("AsphaltPatches_Dark", v_dark, f_dark, col, m_dark)
    if v_light:
        m_light = mat_pbr("AsphaltPatch_Light", (0.085, 0.085, 0.092),
                          roughness=0.85)
        new_mesh_object("AsphaltPatches_Light", v_light, f_light, col, m_light)
    log(f"  patches asfalto: {len(v_dark)//4 + len(v_light)//4}")


def build_stop_lines(cl, tans, road_width, mats):
    """Striscia di stop bianca a inizio e fine percorso."""
    half = road_width / 2
    col = ensure_collection("Road")
    verts, faces = [], []
    for idx in (0, len(cl) - 1):
        x, y, z = cl[idx]
        tx, ty = tans[idx]
        zlift = z + ROAD_THICKNESS + 0.006
        _add_quad_oriented(verts, faces, x, y, zlift, tx, ty, 0.5, half * 1.9)
    if verts:
        new_mesh_object("StopLines", verts, faces, col, mats["line_w"])


def build_extra_signage(cl, tans, curvs, road_width, mats):
    """Delineatori, marker km, cartelli di curva."""
    col = ensure_collection("Signals")
    half = road_width / 2 + SHOULDER_W + 0.45

    # 1) Delineatori bianchi ogni 25 m su entrambi i lati
    vd, fd = [], []
    for x, y, z, tx, ty, d in walk_centerline(cl, tans, 25.0):
        nx, ny = -ty, tx
        for side in (+1, -1):
            _add_pole(vd, fd, x + nx * half * side, y + ny * half * side, z,
                      w=0.06, h=0.9)
    if vd:
        new_mesh_object("Delineators", vd, fd, col, mats["line_w"])

    # 2) Km marker (palo bianco con cima rossa) ogni 1000 m, lato destro
    vk, fk = [], []     # base bianca
    vkr, fkr = [], []   # cima rossa
    next_km = 1000.0
    for x, y, z, tx, ty, d in walk_centerline(cl, tans, 5.0):
        if d >= next_km:
            nx, ny = -ty, tx
            ox = x - nx * (half + 0.4)
            oy = y - ny * (half + 0.4)
            _add_pole(vk, fk, ox, oy, z, w=0.25, h=1.1)
            _add_pole(vkr, fkr, ox, oy, z + 1.1, w=0.25, h=0.25)
            next_km += 1000.0
    if vk:
        new_mesh_object("KmMarker_base", vk, fk, col, mats["line_w"])
    if vkr:
        new_mesh_object("KmMarker_top", vkr, fkr, col, mats["sign"])

    # 3) Cartelli triangolari di pericolo nelle curve strette
    vt, ft = [], []
    placed = 0
    cool = 0  # cooldown distanza
    cum = 0.0
    last_p = cl[0]
    for i in range(1, len(cl)):
        p = cl[i]
        cum += math.hypot(p[0] - last_p[0], p[1] - last_p[1])
        last_p = p
        if abs(curvs[i]) > 0.04 and cum > cool:
            tx, ty = tans[i]
            nx, ny = -ty, tx
            side = -1 if curvs[i] > 0 else +1  # cartello sul lato esterno
            ox = p[0] + nx * (half + 0.3) * side
            oy = p[1] + ny * (half + 0.3) * side
            _add_triangle_sign(vt, ft, ox, oy, p[2], tx, ty, height=2.0, size=0.6)
            placed += 1
            cool = cum + 80.0  # almeno 80 m di distanza tra cartelli
    if vt:
        new_mesh_object("CurveSigns", vt, ft, col, mats["sign"])
    log(f"  delineatori, {next_km/1000 - 1:.0f} km marker, {placed} cartelli curva")


def build_signals(signals, proj, dem, mats, corridor):
    """Semafori, stop, lampioni: palo + piccola testa sferica/cubica."""
    col = ensure_collection("Signals")
    vp, fp = [], []   # pali
    vs, fs = [], []   # testa segnale (rosso)
    vl, fl = [], []   # testa lampione (emissivo)
    kept = 0
    for sig in signals:
        x, y = proj.xy(sig["lat"], sig["lon"])
        if not corridor.inside(x, y):
            continue
        z = dem.sample(x, y)
        kind = sig.get("kind")
        height = 4.5 if kind == "street_lamp" else 3.0
        # palo: box stretto
        r = 0.08
        bp = len(vp)
        vp.extend([
            (x - r, y - r, z), (x + r, y - r, z),
            (x + r, y + r, z), (x - r, y + r, z),
            (x - r, y - r, z + height), (x + r, y - r, z + height),
            (x + r, y + r, z + height), (x - r, y + r, z + height),
        ])
        fp.extend([
            (bp, bp + 1, bp + 5, bp + 4),
            (bp + 1, bp + 2, bp + 6, bp + 5),
            (bp + 2, bp + 3, bp + 7, bp + 6),
            (bp + 3, bp, bp + 4, bp + 7),
            (bp + 4, bp + 5, bp + 6, bp + 7),
        ])
        # testa
        if kind == "street_lamp":
            verts_h, faces_h = vl, fl
        else:
            verts_h, faces_h = vs, fs
        hs = 0.3
        bh = len(verts_h)
        verts_h.extend([
            (x - hs, y - hs, z + height),
            (x + hs, y - hs, z + height),
            (x + hs, y + hs, z + height),
            (x - hs, y + hs, z + height),
            (x - hs, y - hs, z + height + 0.6),
            (x + hs, y - hs, z + height + 0.6),
            (x + hs, y + hs, z + height + 0.6),
            (x - hs, y + hs, z + height + 0.6),
        ])
        faces_h.extend([
            (bh, bh + 1, bh + 5, bh + 4),
            (bh + 1, bh + 2, bh + 6, bh + 5),
            (bh + 2, bh + 3, bh + 7, bh + 6),
            (bh + 3, bh, bh + 4, bh + 7),
            (bh + 4, bh + 5, bh + 6, bh + 7),
        ])
        kept += 1
    if vp: new_mesh_object("Poles", vp, fp, col, mats["pole"])
    if vs: new_mesh_object("Signs", vs, fs, col, mats["sign"])
    if vl: new_mesh_object("Lamps", vl, fl, col, mats["lamp"])
    log(f"  signals: {kept}/{len(signals)}")


# ======== Texture satellitare (se disponibile) =========================
def apply_satellite_texture(terrain, proj):
    """Se output/satellite.png esiste, applica come image texture sul terreno
    usando UV lat/lon → pixel."""
    png = OUT_DIR / "satellite.png"
    meta_path = OUT_DIR / "satellite_bbox.json"
    if not (png.exists() and meta_path.exists()):
        log("  satellite.png non trovato — uso materiale piatto (lancia fetch_satellite.py)")
        return
    meta = json.loads(meta_path.read_text())
    bb = meta["bbox_geo"]
    north, south = bb["north"], bb["south"]
    west, east = bb["west"], bb["east"]

    # UV per ogni vertice terreno (inverto la proiezione locale)
    mesh = terrain.data
    uv_layer = mesh.uv_layers.new(name="UVSat") if not mesh.uv_layers else mesh.uv_layers.active
    # calcolo UV per vertice
    import math as _m
    uvs_by_vert = {}
    for v in mesh.vertices:
        x, y, _z = v.co
        lon = _m.degrees(x / proj.kx) + proj.lon0
        lat = _m.degrees(y / proj.ky) + proj.lat0
        u = (lon - west) / (east - west)
        vv = 1.0 - (lat - south) / (north - south)  # v=0 in alto
        uvs_by_vert[v.index] = (u, vv)
    for poly in mesh.polygons:
        for li in poly.loop_indices:
            vi = mesh.loops[li].vertex_index
            uv_layer.data[li].uv = uvs_by_vert[vi]

    # Material con image texture
    mat = bpy.data.materials.new("SatelliteTerrain")
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes.get("Principled BSDF")
    tex = nt.nodes.new("ShaderNodeTexImage")
    img = bpy.data.images.load(str(png), check_existing=True)
    tex.image = img
    nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 1.0
    # rimpiazzo il materiale
    mesh.materials.clear()
    mesh.materials.append(mat)
    log(f"  satellite texture applicata ({img.size[0]}x{img.size[1]} px)")


# ======== World + export ===============================================
def setup_world_and_sun():
    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1.0
    world = scene.world or bpy.data.worlds.new("World")
    scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (0.55, 0.75, 0.95, 1.0)
        bg.inputs[1].default_value = 1.2
    light = bpy.data.lights.new("Sun", "SUN")
    light.energy = 4.0
    light.angle = math.radians(3.0)
    sun = bpy.data.objects.new("Sun", light)
    sun.rotation_euler = (math.radians(50), math.radians(20), math.radians(45))
    bpy.context.collection.objects.link(sun)


def export_obj_all(path):
    bpy.ops.object.select_all(action="SELECT")
    try:
        bpy.ops.wm.obj_export(filepath=str(path), export_selected_objects=True,
                              forward_axis="Y", up_axis="Z",
                              export_materials=True, export_uv=True, export_normals=True)
    except AttributeError:
        bpy.ops.export_scene.obj(filepath=str(path), use_selection=True,
                                 axis_forward="Y", axis_up="Z",
                                 use_materials=True, use_uvs=True, use_normals=True)


def export_centerline_csv(cl, flags, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write("x,y,z,bridge,tunnel\n")
        for (x, y, z), fl in zip(cl, flags):
            f.write(f"{x:.3f},{y:.3f},{z:.3f},{int(fl['bridge'])},{int(fl['tunnel'])}\n")


# ======== Main =========================================================
def main():
    t0 = time.time()
    if not DATA_PATH.exists():
        raise FileNotFoundError(f"Mancante: {DATA_PATH}")
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    cl_data = data["centerline"]

    lat0 = sum(p["lat"] for p in cl_data) / len(cl_data)
    lon0 = sum(p["lon"] for p in cl_data) / len(cl_data)
    proj = Projection(lat0, lon0)

    log("Clear scene & materiali")
    clear_scene()
    setup_world_and_sun()
    mats = build_materials()

    log(f"Centerline: {len(cl_data)} punti")
    cl_raw = centerline_xyz(cl_data, proj)
    cl_s = smooth_centerline(cl_raw, SMOOTH_WINDOW)
    cl = resample_catmull(cl_s, SUBDIV_PER_SEG)
    log(f"  smooth+resample -> {len(cl)}")

    z_min = min(c[2] for c in cl)
    cl = [(x, y, z - z_min) for (x, y, z) in cl]

    orig_flags = [(p.get("bridge", False), p.get("tunnel", False)) for p in cl_data]
    ratio = (len(cl_data) - 1) / max(1, (len(cl) - 1))
    flags = []
    for i in range(len(cl)):
        k = min(len(cl_data) - 1, int(round(i * ratio)))
        flags.append({"bridge": orig_flags[k][0], "tunnel": orig_flags[k][1]})

    log(f"Corridor {CORRIDOR_M} m attorno alla strada")
    corridor = Corridor(cl, CORRIDOR_M)

    # DEMSampler usa grid["grid"] già shiftato di z_min; voglio lo stesso shift
    # per la centerline, quindi lo applico al sampler con z_min originale.
    log("Terrain DEM (clipped al corridoio)")
    dem = DEMSampler(data["terrain"], proj, z_min)

    # Ricampiono la z della centerline: prendo il MIN del DEM nel punto e
    # a offset laterali/longitudinali. Spinge la strada al fondo delle trincee.
    log("Ricampionamento Z strada (min di DEM + smoothing slope-limited)")
    tans_pre, _ = tangents_and_curvature(cl)
    cl_z = recompute_road_z_from_dem(cl, tans_pre, dem)
    cl = smooth_z_with_slope_limit(cl_z, max_grade=0.09)
    # lievissimo rilevato per stare sempre sopra il terreno carvato
    cl = [(x, y, z + ROAD_EMBANKMENT) for (x, y, z) in cl]
    # ricalcolo tangenti/curvatura/banking dopo il cambio di z
    tans, curvs = tangents_and_curvature(cl)
    banking = compute_banking(curvs)

    terrain = build_terrain_from_dem(dem, mats, corridor)
    carve_width = data["road"]["width_m"] + 2 * SHOULDER_W + 2 * CARVE_BUFFER_M
    log(f"  subdividing terrain vicino alla strada")
    subdivide_terrain_near_road(terrain, cl, corridor_m=carve_width * 1.5, cuts=2)
    log(f"  carving sotto la strada (width={carve_width:.1f}m, "
        f"blend fino a {carve_width * CARVE_BLEND_FACTOR / 2:.1f}m)")
    carve_terrain_under_road(terrain, cl, carve_width)
    log(f"  micro-rumore terreno per rilievi organici")
    add_terrain_noise(terrain, amplitude=0.6, scale=14.0)
    # ri-carving leggero per pulire la zona vicina alla strada dopo il noise
    carve_terrain_under_road(terrain, cl, carve_width * 0.6,
                             depth=CARVE_DEPTH_M * 0.5,
                             blend_factor=1.6)

    log("Road + marcature + banchine")
    width = float(data["road"]["width_m"])

    # Carico (se disponibile) il rilevamento linee da satellite
    has_line_orig = None
    lm_path = OUT_DIR / "line_marks.json"
    if lm_path.exists():
        lm = json.loads(lm_path.read_text())
        has_line_orig = lm["has_center_line"]
        log(f"  line_marks.json: linea presente in {sum(has_line_orig)}/"
            f"{len(has_line_orig)} punti raw ({100 * sum(has_line_orig) / len(has_line_orig):.0f}%)")
    if has_line_orig:
        # mappa cl_data idx → cl idx tramite la stessa ratio dei flag
        ratio2 = (len(has_line_orig) - 1) / max(1, (len(cl) - 1))
        has_line_cl = [has_line_orig[min(len(has_line_orig) - 1,
                                         int(round(i * ratio2)))]
                       for i in range(len(cl))]
    else:
        has_line_cl = None

    build_road_with_lines(cl, tans, banking, curvs, width, mats, has_line_cl)

    log("Dettagli asfalto: catarifrangenti, tombini, patches, stop lines")
    build_road_studs(cl, tans, curvs, width, mats, has_line_cl)
    build_manholes(cl, tans, width, mats)
    build_asphalt_patches(cl, tans, width, mats)
    build_stop_lines(cl, tans, width, mats)

    log("Guardrail")
    build_guardrails(cl, tans, banking, width, dem, mats)

    log(f"Buildings ({len(data.get('buildings', []))})")
    build_buildings_batched(data.get("buildings", []), proj, dem, mats, corridor)

    log(f"Forests ({len(data.get('forests', []))})")
    build_forests_batched(data.get("forests", []), proj, dem, mats, corridor)

    log(f"Water ({len(data.get('waterways', []))} + {len(data.get('waterbodies', []))})")
    build_waterways_batched(data.get("waterways", []), data.get("waterbodies", []),
                            proj, dem, mats, corridor)

    log(f"OtherRoads ({len(data.get('other_roads', []))})")
    build_other_roads_batched(data.get("other_roads", []), proj, dem, mats, corridor)

    log(f"Trees individuali OSM ({len(data.get('trees', []))})")
    build_trees_individual(data.get("trees", []), proj, dem, mats, corridor)

    log(f"Trees scattered in boschi (densi)")
    build_trees_scattered(data.get("forests", []), proj, dem, mats, corridor,
                          spacing=4.0)

    log(f"Trees a margine strada")
    build_roadside_trees(cl, tans, dem, mats, corridor, spacing=14.0)

    log(f"Arbusti nei boschi")
    build_bushes_in_forests(data.get("forests", []), proj, dem, mats, corridor)

    log(f"Cipressi sporadici lungo strada")
    build_cypresses_along_road(cl, tans, dem, mats, corridor)

    log(f"Ciuffi d'erba sparsi")
    build_grass_tufts(cl, tans, dem, mats, corridor)

    log(f"Muretti a secco nei campi")
    build_stone_walls(cl, tans, dem, mats, corridor)

    log(f"Camini sui tetti")
    build_chimneys_on_buildings(data.get("buildings", []), proj, dem, corridor, mats)

    log(f"Cartelli velocità")
    build_speed_signs(cl, tans, curvs, dem, mats, width)

    log(f"Segnaletica/lampioni OSM ({len(data.get('signals', []))})")
    build_signals(data.get("signals", []), proj, dem, mats, corridor)

    log(f"Segnaletica generata (delineatori, km, cartelli curva)")
    build_extra_signage(cl, tans, curvs, width, mats)

    log(f"Rocce sparse")
    build_rocks_scattered(cl, tans, dem, mats, corridor)

    log(f"Pali della luce + cavi")
    build_power_poles(cl, tans, dem, mats, corridor)
    build_wires_between_poles(cl, tans, dem, mats, corridor)

    log("Satellite texture (se disponibile)")
    apply_satellite_texture(terrain, proj)

    log("Highlight + marker start/end + camere")
    build_road_highlight(cl, tans, banking, mats)
    build_markers(cl, mats)
    setup_camera(cl)
    road_path = build_road_curve(cl)
    setup_drive_camera(road_path, cl, duration_s=60.0, fps=30)

    log("Export")
    export_centerline_csv(cl, flags, OUT_DIR / "centerline.csv")
    export_obj_all(OUT_DIR / "macerone.obj")
    try:
        bpy.ops.wm.save_as_mainfile(filepath=str(OUT_DIR / "macerone.blend"))
    except Exception as ex:
        log(f"  .blend saltato: {ex}")

    log(f"Fatto in {time.time() - t0:.1f}s. Output in {OUT_DIR}")
    log(f"Origine locale: lat0={lat0:.6f}, lon0={lon0:.6f}, z_offset={z_min:.1f} m")


main()
