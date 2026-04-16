"""
Build COMPLETA della mod BeamNG: strada SS17 + terreno DEM reale + texture
satellite + edifici + alberi + muretti + guardrail + rocce + segnaletica.

Estende build_minimal_mod.py aggiungendo:
- Heightmap DEM (prima era terreno piatto)
- Texture satellite come TerrainMaterial
- Mesh "mondo" (tutto eccetto Road e Grass/Bushes) come TSStatic addizionale

Tutti i DAE sono Z-up nativi (niente tag Y_UP che BeamNG ignora).
Lo z_offset_blender_m dal terrain_info.json viene applicato come position
dei TSStatic, cosi' le coord Blender si allineano al heightmap world.

Output: output/beamng/macerone3d.zip
"""
from __future__ import annotations

import json
import math
import re
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
TOOLS = Path(__file__).resolve().parent
BEAMNG_OUT = ROOT / "output" / "beamng"
MOD_DIR = BEAMNG_OUT / "mod"
LEVEL_DIR = MOD_DIR / "levels" / "macerone"

BLEND_FILE = ROOT / "output" / "macerone.blend"
BLENDER_EXE = r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe"

TEMPLATE_LEVEL_JSON = TOOLS / "templates" / "main.level.json"

LEVEL_NAME = "macerone"
LEVEL_TITLE = "SS17 Valico del Macerone"
TERRAIN_MATERIAL_NAME = "macerone_ground"
TERRAIN_MATERIAL_UUID = "a1b2c3d4-9999-0000-0000-000000000099"

# Dimensioni terrain: 1024 cells a 12m -> 12288m quadrato (matcha DEM)
TER_SIZE = 1024
TER_SQUARESIZE = 12.0
TER_EXTENT = TER_SIZE * TER_SQUARESIZE

# --- Spawn tuning -----------------------------------------------------------
# Offset rispetto al primo punto centerline. "Forward" = direzione del muso.
SPAWN_FORWARD_M = 5.0      # metri avanti lungo la direzione dell'auto
SPAWN_UP_M = 1.0            # metri in alto (oltre ai 0.10 sopra asfalto)
SPAWN_TURN_RIGHT_DEG = -25.0  # gradi di rotazione a destra (negativo = sx)

# --- Filtro oggetti world intrusivi ----------------------------------------
# Triangoli del world mesh con centroide entro questa distanza dalla
# centerline vengono rimossi: alberi procedurali, bushes, rocce che sono
# finiti casualmente sull'asfalto.
ROAD_CORRIDOR_FILTER_M = 4.5

# Collezioni Blender da esportare come "world" (tutto tranne Road e roba troppo
# pesante tipo Grass/Bushes). Se una non esiste, viene saltata.
WORLD_COLLECTIONS = [
    "Buildings",
    "Walls",
    "Guardrails",
    "Trees",
    "Cypresses",
    "Rocks",
    "Signals",
    "Poles",
    "Chimneys",
    "Shrubs",
]

MODS_DIR = Path(r"C:\Users\Matto\AppData\Local\BeamNG\BeamNG.drive\current\mods")


def run(desc: str, cmd: list[str]) -> None:
    print(f"\n=== [{desc}] ===")
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print(f"[{desc}] EXIT {r.returncode}")
        sys.exit(r.returncode)


# ---------------------------------------------------------------------------
# Step 1: heightmap DEM (se manca, lo genera)
# ---------------------------------------------------------------------------
def ensure_heightmap() -> dict:
    hm = BEAMNG_OUT / "heightmap.png"
    info = BEAMNG_OUT / "terrain_info.json"
    if not hm.exists() or not info.exists():
        run("build_heightmap", [sys.executable, str(TOOLS / "build_heightmap.py")])
    return json.loads(info.read_text(encoding="utf-8"))


def infer_z_offset_blender(info: dict) -> float:
    """Inferisce il vero z_offset usato da blender_build.py per tradurre la
    centerline da metri reali a coord Blender (z_blender = real - z_offset).

    terrain_info.json.z_offset_blender_m = min(DEM bbox) = ~336m, ma
    blender_build.py in realta' usa min(centerline_recompute_z) ~ 419m.
    Differenza puo' essere 80m: se usata quella sbagliata la road finisce
    sottoterra e i "muri altissimi" sono il terreno naturale intorno.

    Strategy: per ogni centerline point (x, y, z_csv) campiono DEM(x,y) dal
    heightmap. In blender_build.py la strada z reale ~ DEM - epsilon (MIN-17),
    quindi candidate = DEM - z_csv - ROAD_EMBANKMENT(0.35). Ritorno mediana
    (robust a outlier, mesh carvato e campioni su curve).
    """
    Image.MAX_IMAGE_PIXELS = None
    im = Image.open(BEAMNG_OUT / "heightmap.png")
    hm = np.array(im, dtype=np.uint16)
    H, W = hm.shape
    elev_min = info["elevation_min_m"]
    elev_max = info["elevation_max_m"]
    max_h = elev_max - elev_min
    mpp = info["meters_per_pixel"]
    half = info["extent_m"] / 2.0
    offsets = []
    import csv as _csv
    with (ROOT / "output" / "centerline.csv").open(newline="", encoding="utf-8") as f:
        for r in _csv.DictReader(f):
            x, y, z_csv = float(r["x"]), float(r["y"]), float(r["z"])
            col = int((x + half) / mpp)
            row_px = int((half - y) / mpp)  # PNG row 0 = nord
            if not (0 <= col < W and 0 <= row_px < H):
                continue
            dem_real = elev_min + (float(hm[row_px, col]) / 65535.0) * max_h
            offsets.append(dem_real - z_csv - 0.35)
    if not offsets:
        return float(info["z_offset_blender_m"])
    return float(np.median(offsets))


# ---------------------------------------------------------------------------
# Step 2: Blender export - Road (Solidify) + World (tutto il resto)
# ---------------------------------------------------------------------------
BLENDER_EXPORT_SCRIPT = '''
import bpy, sys, json
from pathlib import Path
args = sys.argv[sys.argv.index("--") + 1:]
road_out = args[0]
world_out = args[1]
terrain_out = args[2]
world_cols_csv = args[3]
skip_names_csv = args[4] if len(args) > 4 else ""
world_cols = [c for c in world_cols_csv.split(",") if c]
skip_names = set(n for n in skip_names_csv.split(",") if n)

def select_only(objs):
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    if objs:
        bpy.context.view_layer.objects.active = objs[0]

# --- Road: Solidify sui mesh principali che formano la carreggiata. Nomi
# esatti del blend: Road (asfalto), Shoulder_L/R (banchine). NON:
# RoadStuds (catarifrangenti), Manholes (tombini), Marking* (linee),
# Patches (rappezzi), StopLines (strisce stop) che sono sottili e non
# devono sporgere.
SOLIDIFY_EXACT = {"Road", "Shoulder_L", "Shoulder_R"}
def needs_solidify(name: str) -> bool:
    return name in SOLIDIFY_EXACT

road_col = bpy.data.collections.get("Road")
if road_col is None:
    print("!! Collezione 'Road' non trovata")
    sys.exit(2)
road_objs = [o for o in road_col.all_objects if o.type == "MESH"]
solidified = 0
for o in road_objs:
    if not needs_solidify(o.name):
        continue
    if any(m.type == "SOLIDIFY" for m in o.modifiers):
        solidified += 1
        continue
    mod = o.modifiers.new(name="RoadSolidify", type="SOLIDIFY")
    mod.thickness = 0.4
    mod.offset = -1.0
    mod.use_even_offset = True
    mod.use_quality_normals = True
    solidified += 1
print(f"Solidify applicato a {solidified}/{len(road_objs)} mesh Road")
select_only(road_objs)
bpy.ops.wm.obj_export(
    filepath=road_out,
    export_selected_objects=True,
    apply_modifiers=True,
    forward_axis="Y",
    up_axis="Z",
    export_materials=True,
)
print(f"Road: {len(road_objs)} oggetti -> {road_out}")

# --- World: tutto il resto (escluse mesh nella skip_names come Delineators) ---
world_objs = []
for cname in world_cols:
    col = bpy.data.collections.get(cname)
    if col is None:
        print(f"  collezione '{cname}' non trovata, skip")
        continue
    ms = []
    for o in col.all_objects:
        if o.type != "MESH":
            continue
        if o.name in skip_names:
            print(f"  skip mesh '{o.name}' (in SKIP_MESH_NAMES)")
            continue
        ms.append(o)
    world_objs.extend(ms)
    print(f"  {cname}: {len(ms)} mesh")

if not world_objs:
    Path(world_out).write_text("# empty\\n", encoding="utf-8")
    print(f"World: 0 oggetti -> {world_out} (empty)")
else:
    select_only(world_objs)
    bpy.ops.wm.obj_export(
        filepath=world_out,
        export_selected_objects=True,
        apply_modifiers=True,
        forward_axis="Y",
        up_axis="Z",
        export_materials=True,
    )
    print(f"World: {len(world_objs)} oggetti -> {world_out}")

# --- Terrain: esporta la collection Terrain (mesh DEM carvato + Perlin) ---
terrain_col = bpy.data.collections.get("Terrain")
if terrain_col is None:
    Path(terrain_out).write_text("# empty\\n", encoding="utf-8")
    print(f"Terrain: collezione non trovata -> {terrain_out} (empty)")
else:
    terrain_objs = [o for o in terrain_col.all_objects if o.type == "MESH"]
    if not terrain_objs:
        Path(terrain_out).write_text("# empty\\n", encoding="utf-8")
    else:
        select_only(terrain_objs)
        bpy.ops.wm.obj_export(
            filepath=terrain_out,
            export_selected_objects=True,
            apply_modifiers=True,
            forward_axis="Y",
            up_axis="Z",
            export_materials=True,
        )
        print(f"Terrain: {len(terrain_objs)} oggetti -> {terrain_out}")
'''


SKIP_MESH_NAMES = [
    # Delineators riabilitati: aiutano a "riempire" il bordo strada.
]


def export_from_blender(road_obj: Path, world_obj: Path,
                           terrain_obj: Path) -> None:
    script_path = BEAMNG_OUT / "_blender_full_export.py"
    script_path.write_text(BLENDER_EXPORT_SCRIPT, encoding="utf-8")
    world_cols = ",".join(WORLD_COLLECTIONS)
    skip = ",".join(SKIP_MESH_NAMES)
    run("blender_export_full", [
        BLENDER_EXE, "--background", str(BLEND_FILE),
        "--python", str(script_path),
        "--", str(road_obj), str(world_obj), str(terrain_obj), world_cols, skip,
    ])
    script_path.unlink(missing_ok=True)


def convert_to_dae(obj_path: Path) -> Path:
    run("obj_to_dae", [sys.executable, str(TOOLS / "obj_to_dae.py"),
                        str(obj_path)])
    return obj_path.with_suffix(".dae")


# ---------------------------------------------------------------------------
# Colore asfalto medio campionato dall'immagine satellite sulla centerline
# ---------------------------------------------------------------------------
def sample_asphalt_color_from_satellite() -> tuple[float, float, float]:
    """Apre output/satellite.png e campiona il colore dei pixel che cadono
    sulla centerline (lat/lon in road_data.json). Media RGB normalizzato
    [0..1]. Se qualcosa manca, fallback a grigio medio."""
    sat_png = ROOT / "output" / "satellite.png"
    bbox_json = ROOT / "output" / "satellite_bbox.json"
    road_json = ROOT / "road_data.json"
    if not (sat_png.exists() and bbox_json.exists() and road_json.exists()):
        return (0.35, 0.35, 0.35)
    Image.MAX_IMAGE_PIXELS = None
    meta = json.loads(bbox_json.read_text(encoding="utf-8"))
    bbox = meta["bbox_geo"]
    rd = json.loads(road_json.read_text(encoding="utf-8"))
    cl = rd["centerline"]
    im = Image.open(sat_png).convert("RGB")
    W, H = im.size
    arr = np.array(im)  # (H, W, 3)
    samples = []
    denom_lon = bbox["east"] - bbox["west"]
    denom_lat = bbox["north"] - bbox["south"]
    for p in cl:
        u = (p["lon"] - bbox["west"]) / denom_lon
        v = (bbox["north"] - p["lat"]) / denom_lat
        px = int(u * W)
        py = int(v * H)
        if 0 <= px < W and 0 <= py < H:
            samples.append(arr[py, px])
    if not samples:
        return (0.35, 0.35, 0.35)
    avg = np.array(samples).mean(axis=0) / 255.0
    # Leggero boost luminosita' (il satellite e' ombrato) + leggero desaturate
    r, g, b = float(avg[0]), float(avg[1]), float(avg[2])
    gray = (r + g + b) / 3.0
    # mix 70% campione + 30% gray (evita tinte verdi da vegetazione ai bordi)
    r = r * 0.7 + gray * 0.3
    g = g * 0.7 + gray * 0.3
    b = b * 0.7 + gray * 0.3
    return (min(1.0, r), min(1.0, g), min(1.0, b))


# ---------------------------------------------------------------------------
# Filtro OBJ world: rimuove triangoli dentro il corridoio road
# ---------------------------------------------------------------------------
FILTER_OBJ_NAME_KEYWORDS = (
    "TreeTrunks", "TreeCanopies",
    "RoadsideTrunks", "RoadsideCanopies", "Roadside",
    "Bushes", "Rocks", "StoneWalls",
)


def drop_world_obj_to_terrain(obj_path: Path, arr_orig: np.ndarray,
                                 arr_carved: np.ndarray,
                                 max_height: float) -> int:
    """Per ogni vertex del world OBJ, calcola la differenza tra DEM
    pre-carve e post-carve alla sua XY, poi abbassa v.z di quella delta.
    Gli oggetti appoggiati al DEM originale rimangono al nuovo terrain
    level (niente alberi/edifici fluttuanti dopo il carve).

    Ritorna numero di vertex shiftati significativamente."""
    H, W = arr_orig.shape
    half = TER_EXTENT / 2.0
    cell = TER_SQUARESIZE

    def sample_m(arr, x, y):
        col = int((x + half) / cell)
        ry = int((y + half) / cell)
        if not (0 <= col < W and 0 <= ry < H):
            return None
        return float(arr[ry, col]) / 65535.0 * max_height

    lines = obj_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    out_lines = []
    shifted = 0
    for line in lines:
        if not line.startswith("v "):
            out_lines.append(line)
            continue
        parts = line.split()
        x, y, z = float(parts[1]), float(parts[2]), float(parts[3])
        t_orig = sample_m(arr_orig, x, y)
        t_new = sample_m(arr_carved, x, y)
        if t_orig is None or t_new is None:
            out_lines.append(line)
            continue
        delta = t_new - t_orig  # <= 0 (il carve abbassa mai alza)
        if abs(delta) > 0.05:
            z += delta
            shifted += 1
        out_lines.append(f"v {x:.6f} {y:.6f} {z:.6f}\n")
    obj_path.write_text("".join(out_lines), encoding="utf-8")
    return shifted


def filter_world_obj_near_road(obj_path: Path, radius_m: float) -> int:
    """Rimuove dal .obj le face DI CERTI OGGETTI (alberi/rocce/bushes) i cui
    centroidi XY stanno entro radius_m dalla centerline. Oggetti come
    Guardrails/Delineators/Signs restano intatti anche se vicini. Filtra
    per nome mesh via blocchi `o <nome>` nel OBJ."""
    import csv as _csv
    cl_path = ROOT / "output" / "centerline.csv"
    if not cl_path.exists():
        return 0
    r2 = radius_m * radius_m
    # Spatial grid per dist check veloce
    cell = 30.0
    buckets: dict[tuple[int, int], list[tuple[float, float]]] = {}
    with cl_path.open(newline="", encoding="utf-8") as f:
        for r in _csv.DictReader(f):
            x = float(r["x"]); y = float(r["y"])
            buckets.setdefault((int(x // cell), int(y // cell)), []).append((x, y))

    def near_road(cx: float, cy: float) -> bool:
        ix = int(cx // cell); iy = int(cy // cell)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for (px, py) in buckets.get((ix + di, iy + dj), []):
                    if (px - cx) ** 2 + (py - cy) ** 2 <= r2:
                        return True
        return False

    # 1. Leggi tutti i vertici (indici OBJ iniziano da 1)
    verts: list[tuple[float, float, float]] = []
    with obj_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("v "):
                p = line.split()
                verts.append((float(p[1]), float(p[2]), float(p[3])))

    # 2. Riscrivi l'OBJ filtrando le faces solo degli oggetti filterabili
    out_lines: list[str] = []
    removed = 0
    current_obj = "default"
    filter_active = False
    with obj_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("o ") or line.startswith("g "):
                current_obj = line.split(maxsplit=1)[1].strip()
                filter_active = any(k in current_obj for k in FILTER_OBJ_NAME_KEYWORDS)
                out_lines.append(line)
                continue
            if not filter_active or not line.startswith("f "):
                out_lines.append(line)
                continue
            tokens = line.split()[1:]
            coords = []
            for tk in tokens:
                try:
                    vi = int(tk.split("/")[0]) - 1
                except Exception:
                    vi = -1
                if 0 <= vi < len(verts):
                    coords.append(verts[vi])
            if not coords:
                out_lines.append(line)
                continue
            cx = sum(c[0] for c in coords) / len(coords)
            cy = sum(c[1] for c in coords) / len(coords)
            if near_road(cx, cy):
                removed += 1
                continue
            out_lines.append(line)

    obj_path.write_text("".join(out_lines), encoding="utf-8")
    return removed


# ---------------------------------------------------------------------------
# Step 3: .ter binary dal heightmap DEM + terrain.json
# ---------------------------------------------------------------------------
def carve_heightmap_under_road(arr: np.ndarray, elev_min: float,
                                 max_height: float, z_offset_blender: float,
                                 target_is_blender_z: bool = False) -> int:
    """Forza il terreno vicino alla strada in una fascia [road-3m, road-1m]:
    - Upper bound (carve): terreno mai piu' alto di road_z-1m
    - Lower bound (fill): terreno mai piu' basso di road_z-3m
    La strada resta SEMPRE visibile 1m sopra il terreno.

    Falloff lineare raggio 8 celle (~100m): al centro bounds stretti, al
    bordo bounds rilassati (nessun effetto). Previene il caso precedente
    dove il blend bidirezionale con multipli centerline points conver-
    geva alla quota del punto PIU' ALTO nel corridoio, seppellendo la road.
    """
    import csv as _csv
    cl_path = ROOT / "output" / "centerline.csv"
    if not cl_path.exists():
        return 0
    H, W = arr.shape
    half = TER_EXTENT / 2.0
    cell = TER_SQUARESIZE

    # Precompute kernel: plateau 4m attorno alla centerline (carreggiata +
    # banchina), poi falloff lineare fino a ~96m. Cosi' la banchina appoggia
    # al terreno invece di stare in cima a un "muro".
    R = 8  # raggio totale in celle (96m)
    cell = TER_SQUARESIZE
    plateau_m = 12.0  # raggio piatto: almeno 1 cella attorno al centerline
    drs, dcs = np.meshgrid(np.arange(-R, R + 1), np.arange(-R, R + 1), indexing="ij")
    dist_cells = np.sqrt(drs * drs + dcs * dcs)
    dist_m = dist_cells * cell
    R_m = R * cell
    alpha = np.clip(
        1.0 - np.maximum(0.0, dist_m - plateau_m) / (R_m - plateau_m),
        0.0, 1.0,
    ).astype(np.float32)

    # Lavoriamo in float32 per il blend
    arr_f = arr.astype(np.float32)
    carved = 0

    with cl_path.open(newline="", encoding="utf-8") as f:
        for row in _csv.DictReader(f):
            x = float(row["x"])
            y = float(row["y"])
            zb = float(row["z"])
            if target_is_blender_z:
                road_z = zb
            else:
                road_z = zb + z_offset_blender
            # Bounds: terreno 10-40 cm sotto la centerline (appiccicato sotto
            # la strada). Road_top = centerline + 0.35 (embankment), quindi
            # gap visibile 45-75 cm, mai intersezione col Solidify 40cm.
            upper_m = max(0.0, road_z - elev_min - 0.1)
            lower_m = max(0.0, road_z - elev_min - 0.4)
            upper_u16 = min(65535.0, upper_m / max_height * 65535.0)
            lower_u16 = min(65535.0, lower_m / max_height * 65535.0)

            col = int((x + half) / cell)
            ry = int((y + half) / cell)
            r0, r1 = ry - R, ry + R + 1
            c0, c1 = col - R, col + R + 1
            # clip ai bordi
            kr0 = max(0, -r0); r0c = max(0, r0); r1c = min(H, r1)
            kc0 = max(0, -c0); c0c = max(0, c0); c1c = min(W, c1)
            if r0c >= r1c or c0c >= c1c:
                continue
            kr1 = kr0 + (r1c - r0c)
            kc1 = kc0 + (c1c - c0c)

            sub = arr_f[r0c:r1c, c0c:c1c]
            a = alpha[kr0:kr1, kc0:kc1]
            # Solo MIN-carve: terrain mai alzato, solo abbassato al max
            # road+0.4m dove supera. Rimossa ogni logica di lower_bound
            # che alzava il terreno e causava "erba sopra strada" quando
            # il plateau di un centerline alto overlappava con celle
            # gia' al livello road locale piu' basso.
            upper_hard_u16 = upper_u16 + (0.5 / max_height * 65535.0)
            in_kernel = a > 0
            # Dove in_kernel: min(sub, upper_hard). Fuori: sub invariato.
            cap = np.where(in_kernel, upper_hard_u16, 1e9).astype(np.float32)
            new = np.minimum(sub, cap)
            changed = int((new < sub).sum())
            carved += changed
            arr_f[r0c:r1c, c0c:c1c] = new

    arr[:] = np.clip(arr_f, 0, 65535).astype(np.uint16)
    return carved


def write_flat_fallback_terrain(level_dir: Path) -> tuple[float, float, float]:
    """Terrain piatto molto basso (-30m) come fallback SOTTO il mesh Blender
    terrain. BeamNG richiede sempre un TerrainBlock, ma l'utente vede SOLO
    il mesh Blender carvato che sta sopra."""
    level_dir.mkdir(parents=True, exist_ok=True)
    arr = np.zeros((TER_SIZE, TER_SIZE), dtype=np.uint16)
    layer = np.zeros((TER_SIZE, TER_SIZE), dtype=np.uint8)
    ter = level_dir / "theTerrain.ter"
    with ter.open("wb") as f:
        f.write(struct.pack("<B", 9))
        f.write(struct.pack("<I", TER_SIZE))
        f.write(arr.tobytes())
        f.write(layer.tobytes())
        names = [TERRAIN_MATERIAL_NAME]
        f.write(struct.pack("<I", len(names)))
        for n in names:
            nb = n.encode("ascii")
            f.write(struct.pack("<B", len(nb)))
            f.write(nb)
    terrain_json = {
        "datafile": f"levels/{LEVEL_NAME}/theTerrain.ter",
        "heightMapItemSize": 2,
        "heightMapSize": TER_SIZE * TER_SIZE,
        "heightmapImage": f"levels/{LEVEL_NAME}/theTerrain.terrainheightmap.png",
        "layerMapItemSize": 1,
        "layerMapSize": TER_SIZE * TER_SIZE,
        "materials": [TERRAIN_MATERIAL_NAME],
        "size": TER_SIZE,
    }
    (level_dir / "theTerrain.terrain.json").write_text(
        json.dumps(terrain_json, indent=2), encoding="utf-8"
    )
    depth = np.zeros((TER_SIZE, TER_SIZE), dtype=np.uint8)
    Image.fromarray(depth).save(level_dir / "theTerrain.ter.depth.png", optimize=True)
    # elev_min = -30m: il TerrainBlock sta 30m sotto la scena. maxHeight piccolo.
    return 10.0, -30.0, 0.0


def write_dem_terrain(level_dir: Path, info: dict,
                       z_offset_blender: float,
                       out_arrays: dict | None = None) -> tuple[float, float, float]:
    """Scrive theTerrain.ter SHIFTATO in coord Blender: ogni pixel uint16
    rappresenta (real_z - z_offset_blender), cosi' il terreno si allinea
    alla road che ha vertici in coord Blender (z~72 invece di z~496).
    Mantenere Z piccoli (<1000) evita stranezze fisiche di BeamNG a quote
    molto alte e tiene spawn identico al minimal che funzionava.

    Ritorna (max_height_output, terrain_z_position, z_offset_blender).
    """
    hm_png = BEAMNG_OUT / "heightmap.png"
    Image.MAX_IMAGE_PIXELS = None
    im = Image.open(hm_png)
    source_size = info["size_px"]
    elev_min_orig = float(info["elevation_min_m"])
    elev_max_orig = float(info["elevation_max_m"])
    max_height_orig = elev_max_orig - elev_min_orig

    # Downsample a TER_SIZE
    if TER_SIZE != source_size:
        im = im.resize((TER_SIZE, TER_SIZE), Image.BILINEAR)
    arr_orig = np.array(im, dtype=np.uint16)
    # Nel PNG row 0 = nord; in Torque3D terrain row 0 = sud.
    arr_orig = np.flipud(arr_orig)

    # Converto da uint16 a real elevation, shiftato in coord Blender.
    real_z = elev_min_orig + (arr_orig.astype(np.float32) / 65535.0) * max_height_orig
    blender_z = real_z - z_offset_blender

    # Il nuovo range: clip su [0, max_height_shifted]. z_blender puo' essere
    # negativo (aree sotto z_offset) - le portiamo a 0 (perdita trascurabile
    # lontano dalla strada). Il range utile sulla strada e' 0..~700m.
    max_height_shifted = 800.0  # copre range centerline (0..600) con margine
    blender_z_clipped = np.clip(blender_z, 0.0, max_height_shifted)
    arr = (blender_z_clipped / max_height_shifted * 65535.0).astype(np.uint16)
    # Salva copia pre-carve per il drop-to-ground del world mesh
    arr_orig = arr.copy()

    # Carve sotto la strada: target e' in coord Blender (identico alla road).
    carved = carve_heightmap_under_road(arr, 0.0, max_height_shifted,
                                          z_offset_blender,
                                          target_is_blender_z=True)
    print(f"  heightmap carve: abbassate {carved} celle sotto la centerline")

    if out_arrays is not None:
        out_arrays["arr_orig"] = arr_orig
        out_arrays["arr_carved"] = arr
        out_arrays["max_height"] = max_height_shifted

    layer = np.zeros((TER_SIZE, TER_SIZE), dtype=np.uint8)

    ter = level_dir / "theTerrain.ter"
    with ter.open("wb") as f:
        f.write(struct.pack("<B", 9))             # version
        f.write(struct.pack("<I", TER_SIZE))       # size
        f.write(arr.tobytes(order="C"))
        f.write(layer.tobytes(order="C"))
        names = [TERRAIN_MATERIAL_NAME]
        f.write(struct.pack("<I", len(names)))
        for n in names:
            nb = n.encode("ascii")
            f.write(struct.pack("<B", len(nb)))
            f.write(nb)
    print(f"Scritto {ter}  ({ter.stat().st_size} bytes)  "
          f"maxHeight={max_height_shifted} (shifted)  "
          f"z_offset_blender={z_offset_blender:.2f}")

    terrain_json = {
        "datafile": f"levels/{LEVEL_NAME}/theTerrain.ter",
        "heightMapItemSize": 2,
        "heightMapSize": TER_SIZE * TER_SIZE,
        "heightmapImage": f"levels/{LEVEL_NAME}/theTerrain.terrainheightmap.png",
        "layerMapItemSize": 1,
        "layerMapSize": TER_SIZE * TER_SIZE,
        "materials": [TERRAIN_MATERIAL_NAME],
        "size": TER_SIZE,
    }
    (level_dir / "theTerrain.terrain.json").write_text(
        json.dumps(terrain_json, indent=2), encoding="utf-8"
    )

    depth = np.zeros((TER_SIZE, TER_SIZE), dtype=np.uint8)
    Image.fromarray(depth).save(level_dir / "theTerrain.ter.depth.png",
                                  optimize=True)
    # Il terrain e' ora in coord Blender: maxHeight_shifted e elev_min=0.
    return max_height_shifted, 0.0, z_offset_blender


# ---------------------------------------------------------------------------
# Step 4: materiali (terrain con texture satellite + road/world generic)
# ---------------------------------------------------------------------------
def write_materials(level_dir: Path, asphalt_rgb: tuple[float, float, float],
                     asphalt_color_map: str | None = None) -> None:
    # TerrainMaterial con:
    # - diffuseMap = texture satellite (colore macro su scala del tile 12288m)
    # - detailMap = texture erba/terriccio procedurale che ripete ogni 10m
    #   per avere grana close-range (altrimenti da vicino tutto uniforme).
    # Path SENZA leading "/" e CON estensione .png (il wiki dice dipende
    # dalla versione — BeamNG 0.38 vuole path con estensione in TerrainMat).
    terrain_mat_dir = level_dir / "art" / "terrain"
    terrain_mat_dir.mkdir(parents=True, exist_ok=True)
    terrain_materials = {
        f"{TERRAIN_MATERIAL_NAME}-{TERRAIN_MATERIAL_UUID}": {
            "internalName": TERRAIN_MATERIAL_NAME,
            "class": "TerrainMaterial",
            "persistentId": TERRAIN_MATERIAL_UUID,
            "diffuseMap": f"levels/{LEVEL_NAME}/art/terrains/satellite_diffuse.png",
            "diffuseColor": [0.38, 0.48, 0.30, 1.0],
            "diffuseSize": 12288,
            "detailMap": f"levels/{LEVEL_NAME}/art/terrains/detail_grass.png",
            "detailSize": 60,
            "detailStrength": 0.30,
            "groundmodelName": "GRASS",
        }
    }
    (terrain_mat_dir / "main.materials.json").write_text(
        json.dumps(terrain_materials, indent=2), encoding="utf-8"
    )

    # Materiali generici per Road + World. L'obj_to_dae mette solo diffuseColor
    # per ogni material del .mtl; qui creiamo entries compatibili con i nomi
    # che compaiono tipicamente nei .mtl di Blender.
    a_r, a_g, a_b = asphalt_rgb
    # Asfalto base_color material (fallback se texture non carica): grigio
    # medio 0.80x del sample satellite.
    a_r *= 0.80; a_g *= 0.80; a_b *= 0.80
    asphalt_dark = [a_r * 0.7, a_g * 0.7, a_b * 0.7]
    asphalt_light = [min(1.0, a_r * 1.25), min(1.0, a_g * 1.25), min(1.0, a_b * 1.25)]
    shoulder_rgb = [min(1.0, a_r * 1.15), min(1.0, a_g * 1.12), min(1.0, a_b * 1.05)]
    entries = [
        # ROAD (dal blend: Asphalt, AsphaltPatch_*, Shoulder, LineWhite,
        # LineYellow, Manhole)
        ("Asphalt", [a_r, a_g, a_b]),
        ("AsphaltPatch_Dark", asphalt_dark),
        ("AsphaltPatch_Light", asphalt_light),
        ("Shoulder", shoulder_rgb),
        ("LineWhite", [0.93, 0.93, 0.92]),
        ("LineYellow", [0.92, 0.80, 0.18]),
        ("Manhole", [0.14, 0.13, 0.12]),
        # WORLD (dal blend: Building, Guardrail, Pole, Roof, Sign, StoneWall,
        # TreeCanopy, TreeTrunk)
        ("Building", [0.82, 0.76, 0.62]),    # beige caldo toscano
        ("Roof", [0.62, 0.32, 0.22]),         # terracotta italiana
        ("StoneWall", [0.55, 0.50, 0.42]),    # pietra grigio-beige
        ("TreeCanopy", [0.20, 0.35, 0.16]),   # verde foglia scuro
        ("TreeTrunk", [0.32, 0.22, 0.14]),    # corteccia marrone
        ("Guardrail", [0.72, 0.74, 0.78]),    # metallo chiaro
        ("Pole", [0.55, 0.55, 0.55]),         # metallo scuro
        ("Sign", [0.92, 0.92, 0.92]),         # bianco cartello
        # Terrain mesh Blender (collection "Terrain")
        ("Terrain", [0.42, 0.50, 0.32]),          # verde-grigio base
        ("TerrainMat", [0.42, 0.50, 0.32]),
        ("Ground", [0.42, 0.50, 0.32]),
        # Roadside procedural clutter
        ("Rock", [0.55, 0.52, 0.46]),
        ("BushGreen", [0.26, 0.40, 0.20]),
        ("Parapet", [0.62, 0.58, 0.52]),       # cemento parapetti ponte
        ("BollardMat", [0.82, 0.82, 0.80]),    # paletto bianco-grigio
        # Fallback generici
        ("default", [0.55, 0.55, 0.55]),
        ("DefaultMat", [0.55, 0.55, 0.55]),
    ]
    mats = {}
    for name, rgb in entries:
        stage0 = {"diffuseColor": [*rgb, 1.0]}
        # Texture procedurale solo sui materiali asfalto+patches (non linee/segnali)
        if asphalt_color_map and name in ("Asphalt", "AsphaltPatch_Dark",
                                             "AsphaltPatch_Light"):
            stage0["colorMap"] = asphalt_color_map
        mats[name] = {
            "name": name,
            "mapTo": name,
            "class": "Material",
            "Stages": [
                stage0,
                {}, {}, {},
            ],
            "materialTag0": "Miscellaneous",
        }
    (level_dir / "main.materials.json").write_text(
        json.dumps(mats, indent=2), encoding="utf-8"
    )
    print(f"materials scritti: 1 TerrainMaterial + {len(mats)} Material")


# ---------------------------------------------------------------------------
# Step 5: copia satellite texture nel mod
# ---------------------------------------------------------------------------
def generate_asphalt_texture(level_dir: Path,
                               base_rgb: tuple[float, float, float]) -> str:
    """Genera PNG 1024x1024 procedurale per l'asfalto piu' realistica:
    grana fine, crepe a ragnatela, giunti trasversali (espansione), chiazze
    di usura chiare/scure, niente striature longitudinali artificiali.
    Base color piu' scuro del satellite (che e' sovraesposto in full sun).
    """
    size = 1024
    rng = np.random.default_rng(42)

    # --- base: grana tri-scale (grande, media, fine) ---
    # noise grande (macchie a 64px = zone di finitura)
    big = rng.normal(0.0, 1.0, (size // 16, size // 16)).astype(np.float32)
    big_img = Image.fromarray(big, mode="F").resize((size, size), Image.BICUBIC)
    big = np.array(big_img, dtype=np.float32) * 0.05
    # noise medio (32px)
    med = rng.normal(0.0, 1.0, (size // 8, size // 8)).astype(np.float32)
    med_img = Image.fromarray(med, mode="F").resize((size, size), Image.BICUBIC)
    med = np.array(med_img, dtype=np.float32) * 0.04
    # grana fine
    fine = rng.normal(0.0, 0.035, (size, size)).astype(np.float32)

    # Granelli pietra nel bitume (salt & pepper)
    spk = rng.random((size, size), dtype=np.float32)
    dark_grit = np.where(spk > 0.985, -0.15, 0.0)
    light_grit = np.where(spk < 0.010, 0.10, 0.0)

    # Crepe a ragnatela: linee sottili random
    cracks = np.zeros((size, size), dtype=np.float32)
    for _ in range(60):
        y = rng.integers(0, size)
        x0 = rng.integers(0, size - 40)
        length = rng.integers(20, 200)
        drift = rng.integers(-2, 3)
        for i in range(length):
            xi = x0 + i
            yi = y + i * drift // 30
            if 0 <= xi < size and 0 <= yi < size:
                cracks[yi, xi] = -0.18
        # Alcune verticali
        if rng.random() < 0.4:
            x = rng.integers(0, size)
            y0 = rng.integers(0, size - 40)
            length = rng.integers(20, 150)
            for i in range(length):
                yi = y0 + i
                if 0 <= yi < size:
                    cracks[yi, x] = -0.16

    # Niente giunti trasversali regolari (creano pattern "piastrelle" quando
    # la texture si tile sulla road mesh). La grana + crepe random bastano.
    joints = 0.0

    # Macchia d'olio scura occasionale (grandi pozze)
    oil = np.zeros((size, size), dtype=np.float32)
    for _ in range(4):
        cx = rng.integers(size // 8, size - size // 8)
        cy = rng.integers(size // 8, size - size // 8)
        r = rng.integers(20, 60)
        yy, xx = np.ogrid[:size, :size]
        d2 = (yy - cy) ** 2 + (xx - cx) ** 2
        oil += np.where(d2 < r * r, -0.08 * (1.0 - d2 / (r * r)), 0.0).astype(np.float32)

    delta = big + med + fine + dark_grit + light_grit + cracks + joints + oil

    # Base color: il satellite ha RGB ~0.60 (sovraesposto). Un asfalto reale
    # di una SS italiana e' grigio medio (circa 0.45-0.50).
    r, g, b = base_rgb
    base_scale = 0.80  # asfalto grigio medio (non troppo scuro)
    r = r * base_scale
    g = g * base_scale
    b = b * base_scale

    R = np.clip(r + delta, 0.02, 1.0)
    G = np.clip(g + delta, 0.02, 1.0)
    B = np.clip(b + delta * 1.02, 0.02, 1.0)
    img = np.stack([R, G, B], axis=-1)
    img_u8 = (img * 255.0).astype(np.uint8)

    tex_dir = level_dir / "art" / "road"
    tex_dir.mkdir(parents=True, exist_ok=True)
    out = tex_dir / "asphalt_base.png"
    Image.fromarray(img_u8).save(out, optimize=True)
    rel = f"levels/{LEVEL_NAME}/art/road/asphalt_base.png"
    print(f"Asfalto texture {size}x{size}: {out.relative_to(MOD_DIR)}")
    return rel


def generate_terrain_detail_texture(level_dir: Path) -> str:
    """DetailMap per il TerrainMaterial: grana fine puramente noise, senza
    elementi riconoscibili (altrimenti il tiling a tile-size si vede).
    Tiled a 60m -> ripetizione meno evidente.
    """
    size = 512
    rng = np.random.default_rng(7)
    # Noise high-frequency senza blob/macchie grandi (evita pattern tiled)
    n1 = rng.normal(0.0, 0.09, (size, size)).astype(np.float32)
    # tiny noise (fine grain)
    n2 = rng.normal(0.0, 0.04, (size, size)).astype(np.float32)
    delta = n1 * 0.6 + n2 * 0.4
    # base verde-grigio uniforme (fa solo grana)
    R = np.clip(0.38 + delta * 0.8, 0.15, 0.75)
    G = np.clip(0.44 + delta * 0.9, 0.18, 0.80)
    B = np.clip(0.30 + delta * 0.7, 0.12, 0.65)
    img = np.stack([R, G, B], axis=-1)
    img_u8 = (img * 255.0).astype(np.uint8)
    tex_dir = level_dir / "art" / "terrains"
    tex_dir.mkdir(parents=True, exist_ok=True)
    out = tex_dir / "detail_grass.png"
    Image.fromarray(img_u8).save(out, optimize=True)
    print(f"Terrain detailMap {size}x{size}: {out.relative_to(MOD_DIR)}")
    return f"levels/{LEVEL_NAME}/art/terrains/detail_grass.png"


def _project_factory_from_road_data():
    """Ritorna (project(lat,lon)->(x,y), road_data_dict, bridges_flags) usando
    lat0/lon0 dal centroide centerline (come blender_build.py)."""
    rd = json.loads((ROOT / "road_data.json").read_text(encoding="utf-8"))
    cl = rd["centerline"]
    lat0 = sum(p["lat"] for p in cl) / len(cl)
    lon0 = sum(p["lon"] for p in cl) / len(cl)
    R = 6378137.0
    kx = math.cos(math.radians(lat0)) * R
    ky = R
    def project(lat, lon):
        return (math.radians(lon - lon0) * kx, math.radians(lat - lat0) * ky)
    return project, rd


def generate_roadside_clutter(level_dir: Path) -> Path | None:
    """Genera OBJ con sassi/cespugli al bordo strada, condizionato su tag
    OSM del road_data.json:
    - bridge=true: skip clutter (parapetti aggiunti separati)
    - dist building < 60m: zona abitata, densita' alta cespugli (siepi)
    - punto dentro foresta OSM: aggiunge ciuffi bassi extra
    Oltre al clutter naturale ogni 18m alternato sx/dx.

    Aggiunge inoltre parapetti semplici sui segmenti di ponte e paletti
    singoli nei 6 node_barriers OSM.
    """
    import csv as _csv
    cl_path = ROOT / "output" / "centerline.csv"
    if not cl_path.exists():
        return None
    with cl_path.open(newline="", encoding="utf-8") as f:
        cl = [(float(r["x"]), float(r["y"]), float(r["z"]),
                 int(r.get("bridge", "0") or 0), int(r.get("tunnel", "0") or 0))
                for r in _csv.DictReader(f)]
    if len(cl) < 10:
        return None

    project, rd = _project_factory_from_road_data()

    # Proietto buildings a coord Blender + spatial grid per distance-to-building
    b_cell = 50.0
    b_buckets: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for b in rd.get("buildings", []):
        coords = b.get("coords", [])
        if not coords:
            continue
        # centroide del poligono
        xs_ys = [project(c[0], c[1]) for c in coords]
        cx = sum(p[0] for p in xs_ys) / len(xs_ys)
        cy = sum(p[1] for p in xs_ys) / len(xs_ys)
        b_buckets.setdefault((int(cx // b_cell), int(cy // b_cell)), []).append((cx, cy))

    def dist_to_nearest_building(x: float, y: float) -> float:
        ix = int(x // b_cell); iy = int(y // b_cell)
        dmin2 = float("inf")
        for di in (-2, -1, 0, 1, 2):
            for dj in (-2, -1, 0, 1, 2):
                for (bx, by) in b_buckets.get((ix + di, iy + dj), []):
                    d2 = (bx - x) ** 2 + (by - y) ** 2
                    if d2 < dmin2:
                        dmin2 = d2
        return math.sqrt(dmin2) if dmin2 != float("inf") else 1e9

    # Proietto forests polygons (solo bbox per check rapido "dentro o vicino")
    forests_bbox: list[tuple[float, float, float, float]] = []
    for f in rd.get("forests", []):
        coords = f.get("coords", [])
        if not coords:
            continue
        xs_ys = [project(c[0], c[1]) for c in coords]
        xs = [p[0] for p in xs_ys]
        ys = [p[1] for p in xs_ys]
        forests_bbox.append((min(xs), min(ys), max(xs), max(ys)))

    def in_forest(x: float, y: float, margin: float = 5.0) -> bool:
        for (x0, y0, x1, y1) in forests_bbox:
            if x0 - margin <= x <= x1 + margin and y0 - margin <= y <= y1 + margin:
                return True
        return False

    rng = np.random.default_rng(1234)
    shapes_dir = level_dir / "art" / "shapes"
    shapes_dir.mkdir(parents=True, exist_ok=True)
    obj_path = shapes_dir / "macerone_roadside.obj"
    mtl_path = shapes_dir / "macerone_roadside.mtl"

    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[list[int], str]] = []

    def add_rock(cx: float, cy: float, cz: float, size: float):
        # icosaedro-like grezzo: 8 vertici piramide doppia
        h = size
        s = size * 0.8
        top = (cx + rng.normal(0, 0.1 * s), cy + rng.normal(0, 0.1 * s), cz + h)
        bot = (cx, cy, cz)
        ring = []
        for k in range(5):
            ang = 2 * math.pi * k / 5 + rng.uniform(0, 0.5)
            rr = s * rng.uniform(0.7, 1.1)
            ring.append((cx + rr * math.cos(ang),
                          cy + rr * math.sin(ang),
                          cz + h * 0.35 * rng.uniform(0.6, 1.0)))
        base = len(verts) + 1
        verts.append(top); verts.append(bot); verts.extend(ring)
        # top faces
        for k in range(5):
            a = base + 0  # top
            b = base + 2 + k
            c = base + 2 + (k + 1) % 5
            faces.append(([a, b, c], "Rock"))
        for k in range(5):
            a = base + 1  # bottom
            b = base + 2 + (k + 1) % 5
            c = base + 2 + k
            faces.append(([a, b, c], "Rock"))

    def add_bush(cx: float, cy: float, cz: float, size: float):
        # piramide triangolare (tetraedro) verdino
        h = size * 1.5
        s = size
        base_pts = []
        for k in range(4):
            ang = 2 * math.pi * k / 4 + rng.uniform(0, 0.4)
            base_pts.append((cx + s * math.cos(ang),
                              cy + s * math.sin(ang), cz))
        top = (cx + rng.normal(0, s * 0.2),
                cy + rng.normal(0, s * 0.2), cz + h)
        base = len(verts) + 1
        verts.extend(base_pts)
        verts.append(top)
        for k in range(4):
            a = base + k
            b = base + (k + 1) % 4
            c = base + 4  # top
            faces.append(([a, b, c], "BushGreen"))
        # base quad (due tri)
        faces.append(([base + 0, base + 2, base + 1], "BushGreen"))
        faces.append(([base + 0, base + 3, base + 2], "BushGreen"))

    def add_parapet_segment(x0, y0, z0, x1, y1, z1, side_normal):
        """Muretto basso 80cm lungo il bordo, da (x0,y0,z0) a (x1,y1,z1)
        con offset side_normal (3.5m dal centerline)."""
        nx, ny = side_normal
        off = 3.5
        h = 0.8
        thick = 0.15
        # 4 vertici base + 4 top
        base = len(verts) + 1
        for (xa, ya, za) in ((x0, y0, z0), (x1, y1, z1)):
            ox = xa + nx * off
            oy = ya + ny * off
            # outer edge
            verts.append((ox + nx * thick, oy + ny * thick, za))
            # inner edge
            verts.append((ox - nx * thick, oy - ny * thick, za))
        # top
        for (xa, ya, za) in ((x0, y0, z0), (x1, y1, z1)):
            ox = xa + nx * off
            oy = ya + ny * off
            verts.append((ox + nx * thick, oy + ny * thick, za + h))
            verts.append((ox - nx * thick, oy - ny * thick, za + h))
        # 8 verts: base[0..3], top[4..7]
        # outer side (quad 0,2,6,4)
        faces.append(([base + 0, base + 2, base + 6], "Parapet"))
        faces.append(([base + 0, base + 6, base + 4], "Parapet"))
        # inner side (1,5,7,3)
        faces.append(([base + 1, base + 5, base + 7], "Parapet"))
        faces.append(([base + 1, base + 7, base + 3], "Parapet"))
        # top (4,5,7,6)
        faces.append(([base + 4, base + 5, base + 7], "Parapet"))
        faces.append(([base + 4, base + 7, base + 6], "Parapet"))
        # ends (0,1,5,4)
        faces.append(([base + 0, base + 1, base + 5], "Parapet"))
        faces.append(([base + 0, base + 5, base + 4], "Parapet"))
        faces.append(([base + 2, base + 3, base + 7], "Parapet"))
        faces.append(([base + 2, base + 7, base + 6], "Parapet"))

    def add_bollard(cx: float, cy: float, cz: float, height: float = 1.0):
        """Paletto cilindrico stilizzato: ottagono."""
        r = 0.08
        n = 8
        base = len(verts) + 1
        for k in range(n):
            ang = 2 * math.pi * k / n
            verts.append((cx + r * math.cos(ang), cy + r * math.sin(ang), cz))
        for k in range(n):
            ang = 2 * math.pi * k / n
            verts.append((cx + r * math.cos(ang), cy + r * math.sin(ang), cz + height))
        for k in range(n):
            a = base + k
            b = base + (k + 1) % n
            c = base + n + (k + 1) % n
            d = base + n + k
            faces.append(([a, b, c], "BollardMat"))
            faces.append(([a, c, d], "BollardMat"))

    # Cammina lungo la centerline con step ~18m per clutter naturale
    step_m = 18.0
    acc = 0.0
    last_x, last_y = cl[0][0], cl[0][1]
    side = 1
    count_rock = 0
    count_bush = 0
    count_skipped_bridge = 0

    # Raccogli range di punti ponte per parapetti
    bridge_segments: list[tuple[int, int]] = []
    i_start = None
    for i, (_, _, _, br, _) in enumerate(cl):
        if br and i_start is None:
            i_start = i
        elif not br and i_start is not None:
            bridge_segments.append((i_start, i - 1))
            i_start = None
    if i_start is not None:
        bridge_segments.append((i_start, len(cl) - 1))

    for (x, y, z, br, tu) in cl[1:]:
        dx = x - last_x; dy = y - last_y
        d = math.hypot(dx, dy)
        acc += d
        last_x, last_y = x, y
        if acc < step_m:
            continue
        acc = 0.0
        if br or tu:
            count_skipped_bridge += 1
            continue
        if d < 0.01:
            continue
        nx, ny = -dy / d, dx / d

        # Condizionamento su vicinanza building/foresta
        dist_b = dist_to_nearest_building(x, y)
        is_forested = in_forest(x, y)
        if dist_b < 30.0:
            # Zona abitata: siepi/cespugli regolari (no sassi selvaggi)
            density = 4  # quattro oggetti per step
            for _ in range(density):
                offset = rng.uniform(3.5, 5.0) * side
                ox = x + nx * offset + rng.normal(0, 0.2)
                oy = y + ny * offset + rng.normal(0, 0.2)
                oz = z - 0.05
                add_bush(ox, oy, oz, rng.uniform(0.35, 0.60))
                count_bush += 1
                side *= -1
        elif is_forested:
            # Zona foresta: piu' cespugli, pietre grandi
            density = 3
            for _ in range(density):
                offset = rng.uniform(3.5, 6.5) * side
                ox = x + nx * offset + rng.normal(0, 0.3)
                oy = y + ny * offset + rng.normal(0, 0.3)
                oz = z - 0.1
                if rng.random() < 0.7:
                    add_bush(ox, oy, oz, rng.uniform(0.40, 0.75))
                    count_bush += 1
                else:
                    add_rock(ox, oy, oz, rng.uniform(0.30, 0.60))
                    count_rock += 1
                side *= -1
        else:
            # Zona aperta: clutter leggero originale
            for _ in range(2):
                offset = rng.uniform(3.5, 6.0) * side
                ox = x + nx * offset + rng.normal(0, 0.3)
                oy = y + ny * offset + rng.normal(0, 0.3)
                oz = z - 0.1
                if rng.random() < 0.55:
                    add_rock(ox, oy, oz, rng.uniform(0.25, 0.55))
                    count_rock += 1
                else:
                    add_bush(ox, oy, oz, rng.uniform(0.30, 0.60))
                    count_bush += 1
                side *= -1

    # Parapetti sui ponti: una striscia per lato
    count_parapet = 0
    for (a, b) in bridge_segments:
        if b - a < 1:
            continue
        x0, y0, z0 = cl[a][0], cl[a][1], cl[a][2]
        x1, y1, z1 = cl[b][0], cl[b][1], cl[b][2]
        dx = x1 - x0; dy = y1 - y0
        d = math.hypot(dx, dy)
        if d < 1.0:
            continue
        nx, ny = -dy / d, dx / d
        add_parapet_segment(x0, y0, z0, x1, y1, z1, (nx, ny))
        add_parapet_segment(x0, y0, z0, x1, y1, z1, (-nx, -ny))
        count_parapet += 2

    # Node barriers (bollard/gate) OSM
    count_bollard = 0
    for nb in rd.get("node_barriers", []):
        try:
            bx, by = project(nb["lat"], nb["lon"])
        except Exception:
            continue
        # Prendi z dalla centerline piu' vicina
        dmin = float("inf"); bz = 0.0
        for (cx, cy, cz, _, _) in cl:
            d2 = (cx - bx) ** 2 + (cy - by) ** 2
            if d2 < dmin:
                dmin = d2; bz = cz
        if dmin > 50 * 50:
            continue
        add_bollard(bx, by, bz - 0.05, height=1.0)
        count_bollard += 1

    if not verts:
        return None

    # Scrivi OBJ
    lines = [
        "# macerone_roadside: procedurale sassi+cespugli al bordo\n",
        "mtllib macerone_roadside.mtl\n",
    ]
    for (vx, vy, vz) in verts:
        lines.append(f"v {vx:.3f} {vy:.3f} {vz:.3f}\n")
    current_mat = None
    lines.append("o Roadside\n")
    for (idx, mat) in faces:
        if mat != current_mat:
            lines.append(f"usemtl {mat}\n")
            current_mat = mat
        lines.append(f"f {idx[0]} {idx[1]} {idx[2]}\n")
    obj_path.write_text("".join(lines), encoding="utf-8")

    mtl_lines = [
        "newmtl Rock\nKd 0.52 0.50 0.45\n",
        "newmtl BushGreen\nKd 0.28 0.40 0.22\n",
        "newmtl Parapet\nKd 0.62 0.58 0.52\n",
        "newmtl BollardMat\nKd 0.80 0.80 0.78\n",
    ]
    mtl_path.write_text("".join(mtl_lines), encoding="utf-8")
    print(f"Roadside clutter: {count_rock} pietre + {count_bush} cespugli "
          f"(skip su {count_skipped_bridge} ponti), {count_parapet} parapetti, "
          f"{count_bollard} bollard -> {obj_path.relative_to(MOD_DIR)}")
    return obj_path


def copy_satellite_texture(level_dir: Path) -> None:
    src = BEAMNG_OUT / "satellite_diffuse.png"
    if not src.exists():
        src = ROOT / "output" / "satellite.png"
    dst_dir = level_dir / "art" / "terrains"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / "satellite_diffuse.png"
    shutil.copy2(src, dst)
    print(f"Satellite texture -> {dst.relative_to(MOD_DIR)}  ({dst.stat().st_size // 1024} KB)")


# ---------------------------------------------------------------------------
# Step 6: spawn + heading (da centerline)
# ---------------------------------------------------------------------------
def read_first_centerline_point() -> tuple[float, float, float]:
    import csv as _csv
    cl = ROOT / "output" / "centerline.csv"
    with cl.open(newline="", encoding="utf-8") as f:
        row = next(_csv.DictReader(f))
        return float(row["x"]), float(row["y"]), float(row["z"])


def road_top_z_at(obj_path: Path, cx: float, cy: float,
                    radius: float = 3.0) -> float:
    r2 = radius * radius
    best = None
    with obj_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.startswith("v "):
                continue
            parts = line.split()
            x, y, z = float(parts[1]), float(parts[2]), float(parts[3])
            if (x - cx) * (x - cx) + (y - cy) * (y - cy) > r2:
                continue
            if best is None or z > best:
                best = z
    if best is None:
        raise RuntimeError(f"Nessun vertice road entro {radius}m da ({cx},{cy})")
    return best


def read_spawn_heading() -> float:
    """Muso veicolo BeamNG = -Y locale -> heading = atan2(dx, -dy)."""
    import csv as _csv
    cl = ROOT / "output" / "centerline.csv"
    with cl.open(newline="", encoding="utf-8") as f:
        rows = list(_csv.DictReader(f))
    p1 = (float(rows[0]["x"]), float(rows[0]["y"]))
    target = p1
    for r in rows[1:]:
        px, py = float(r["x"]), float(r["y"])
        if math.hypot(px - p1[0], py - p1[1]) >= 15.0:
            target = (px, py)
            break
    dx = target[0] - p1[0]
    dy = target[1] - p1[1]
    return math.atan2(dx, -dy)


def heading_to_quat(h: float) -> tuple[float, float, float, float]:
    return (0.0, 0.0, math.sin(h / 2.0), math.cos(h / 2.0))


# ---------------------------------------------------------------------------
# Step 7: main.level.json + info.json
# ---------------------------------------------------------------------------
def write_level_json(level_dir: Path,
                      road_shape_rel: str,
                      world_shape_rel: str | None,
                      roadside_shape_rel: str | None,
                      terrain_shape_rel: str | None,
                      spawn_xyz: tuple[float, float, float],
                      spawn_heading: float,
                      max_height: float,
                      elev_min: float,
                      z_offset_blender: float) -> None:
    qx, qy, qz, qw = heading_to_quat(spawn_heading)
    info = {
        "title": LEVEL_TITLE,
        "description": "Ricostruzione 3D SS17 Valico del Macerone (Molise)",
        "authors": "mattoide",
        "previews": ["main_preview.png"],
        "size": [int(TER_EXTENT), int(TER_EXTENT)],
        "biome": "temperate",
        "roads": "few",
        "suitablefor": "Freeroam",
        "features": "hills",
        "isAuxiliary": False,
        "supportsTraffic": False,
        "supportsTimeOfDay": True,
        "defaultSpawnPointName": "spawn_start",
        "spawnPoints": [
            {
                "translation": list(spawn_xyz),
                "rot": [qx, qy, qz, qw],
                "objectname": "spawn_start",
            }
        ],
    }
    (level_dir / "info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")

    tpl = TEMPLATE_LEVEL_JSON.read_text(encoding="utf-8")

    # --- TerrainBlock: heightmap DEM, centrato sull'origine, z=elev_min ---
    def patch_tb(m):
        block = m.group(0)
        block = re.sub(r'"terrainFile"\s*:\s*"[^"]+"',
                        f'"terrainFile" : "levels/{LEVEL_NAME}/theTerrain.ter"',
                        block)
        block = re.sub(r'"maxHeight"\s*:\s*[\d\.e\-\+]+',
                        f'"maxHeight" : {max_height}', block)
        half = TER_EXTENT / 2.0
        block = re.sub(r'"position"\s*:\s*\[[^\]]+\]',
                        f'"position" : [ {-half}, {-half}, {elev_min} ]', block)
        if '"squareSize"' not in block:
            block = block.replace('"maxHeight"',
                                    f'"squareSize" : {TER_SQUARESIZE},\n          "maxHeight"')
        else:
            block = re.sub(r'"squareSize"\s*:\s*[\d\.e\-\+]+',
                            f'"squareSize" : {TER_SQUARESIZE}', block)
        return block

    tpl = re.sub(r'\{\s*"class"\s*:\s*"TerrainBlock".*?\}', patch_tb, tpl,
                   count=1, flags=re.S)

    # --- SpawnSphere ---
    sx, sy, sz = spawn_xyz
    heading_deg = math.degrees(spawn_heading)
    rot_str = f'[ 0, 0, 1, {heading_deg} ]'
    def patch_ss(m):
        block = m.group(0)
        if '"position"' in block:
            block = re.sub(r'"position"\s*:\s*\[[^\]]+\]',
                            f'"position" : [ {sx}, {sy}, {sz} ]', block)
        else:
            block = block.replace(
                '"class" : "SpawnSphere"',
                f'"class" : "SpawnSphere",\n          "name" : "spawn_start",\n'
                f'          "position" : [ {sx}, {sy}, {sz} ]',
            )
        if '"rotation"' in block:
            block = re.sub(r'"rotation"\s*:\s*\[[^\]]+\]',
                            f'"rotation" : {rot_str}', block)
        else:
            block = block.replace(
                '"class" : "SpawnSphere"',
                f'"class" : "SpawnSphere",\n          "rotation" : {rot_str}',
            )
        if '"name"' not in block:
            block = block.replace('"class" : "SpawnSphere"',
                                    '"class" : "SpawnSphere",\n          "name" : "spawn_start"')
        return block
    tpl = re.sub(r'\{\s*"class"\s*:\s*"SpawnSphere".*?\}', patch_ss, tpl, flags=re.S)

    # --- TSStatic Road + World @ (0, 0, 0) ---
    # Il terrain e' gia' shiftato in coord Blender nel heightmap, quindi le
    # mesh con z~72 stanno sopra il terreno shiftato senza offset.
    tsstatics = [
        (
            "macerone_road_mesh",
            f"levels/{LEVEL_NAME}/{road_shape_rel}",
        )
    ]
    if world_shape_rel is not None:
        tsstatics.append(
            (
                "macerone_world_mesh",
                f"levels/{LEVEL_NAME}/{world_shape_rel}",
            )
        )
    if roadside_shape_rel is not None:
        tsstatics.append(
            (
                "macerone_roadside_mesh",
                f"levels/{LEVEL_NAME}/{roadside_shape_rel}",
            )
        )
    if terrain_shape_rel is not None:
        tsstatics.append(
            (
                "macerone_terrain_mesh",
                f"levels/{LEVEL_NAME}/{terrain_shape_rel}",
            )
        )

    tsstatic_blocks = []
    for name, shape in tsstatics:
        tsstatic_blocks.append(
            '{\n'
            '  "class" : "TSStatic",\n'
            f'  "name" : "{name}",\n'
            '  "position" : [ 0, 0, 0 ],\n'
            '  "allowPlayerStep" : "1",\n'
            '  "collisionType" : "Visible Mesh Final",\n'
            '  "decalType" : "Visible Mesh Final",\n'
            f'  "shapeName" : "{shape}"\n'
            '}'
        )

    def inject_after_terrain(m):
        return m.group(0) + ",\n        " + ",\n        ".join(tsstatic_blocks)

    tpl = re.sub(r'\{\s*"class"\s*:\s*"TerrainBlock".*?\}', inject_after_terrain,
                   tpl, count=1, flags=re.S)

    (level_dir / "main.level.json").write_text(tpl, encoding="utf-8")
    print(f"main.level.json scritto (road+world TSStatic @ (0,0,0), "
          f"spawn @ {spawn_xyz}, heading={heading_deg:.1f} deg)")


def write_empty_jsons(level_dir: Path) -> None:
    (level_dir / "main.decals.json").write_text(
        json.dumps({"header": {"name": "DecalData File", "version": 1},
                     "instances": {}}, indent=2), encoding="utf-8"
    )
    (level_dir / "map.json").write_text(
        json.dumps({"segments": {}}, indent=2), encoding="utf-8"
    )


def write_preview(level_dir: Path) -> None:
    src = BEAMNG_OUT / "preview.jpg"
    if src.exists():
        shutil.copy2(src, level_dir / "main_preview.png")
        shutil.copy2(src, level_dir / "preview.jpg")
    else:
        Image.new("RGB", (512, 512), (80, 100, 80)).save(level_dir / "main_preview.png")
        Image.new("RGB", (512, 512), (80, 100, 80)).save(level_dir / "preview.jpg")


def zip_mod() -> Path:
    zp = BEAMNG_OUT / "macerone3d.zip"
    if zp.exists():
        zp.unlink()
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for p in MOD_DIR.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(MOD_DIR))
    return zp


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def main() -> None:
    print("=== BUILD FULL MOD ===\n")
    if MOD_DIR.exists():
        shutil.rmtree(MOD_DIR)
    LEVEL_DIR.mkdir(parents=True, exist_ok=True)
    shapes_dir = LEVEL_DIR / "art" / "shapes"
    shapes_dir.mkdir(parents=True, exist_ok=True)

    # 1. Heightmap
    info = ensure_heightmap()

    # 1b. Inferisco il vero z_offset_blender (diverso da terrain_info.json)
    z_offset_blender = infer_z_offset_blender(info)
    print(f"z_offset_blender inferito: {z_offset_blender:.2f}m "
          f"(terrain_info diceva {info['z_offset_blender_m']:.2f}m, diff="
          f"{z_offset_blender - info['z_offset_blender_m']:.2f}m)")

    # 2. Blender export: road (Solidify) + world + terrain mesh
    road_obj = shapes_dir / "macerone_road.obj"
    world_obj = shapes_dir / "macerone_world.obj"
    terrain_obj = shapes_dir / "macerone_terrain.obj"
    export_from_blender(road_obj, world_obj, terrain_obj)

    # 3. OBJ -> DAE
    road_dae = convert_to_dae(road_obj)
    road_rel = road_dae.relative_to(LEVEL_DIR).as_posix()

    world_has_content = world_obj.exists() and world_obj.stat().st_size > 200
    world_rel = None
    if world_has_content:
        removed = filter_world_obj_near_road(world_obj, ROAD_CORRIDOR_FILTER_M)
        print(f"  filter corridoio {ROAD_CORRIDOR_FILTER_M}m: "
              f"rimosse {removed} face dal world mesh")
        world_dae = convert_to_dae(world_obj)
        world_rel = world_dae.relative_to(LEVEL_DIR).as_posix()

    terrain_has_content = terrain_obj.exists() and terrain_obj.stat().st_size > 200
    terrain_rel = None
    if terrain_has_content:
        terrain_dae = convert_to_dae(terrain_obj)
        terrain_rel = terrain_dae.relative_to(LEVEL_DIR).as_posix()
        print(f"terrain mesh (Blender carved DEM + noise): {terrain_rel}")

    # 4. Terrain .ter: piatto a -30m, fa solo da fallback fuori dal mesh
    # Blender (BeamNG richiede sempre un TerrainBlock). Mesh Blender sopra.
    max_height, elev_min, z_offset_blender = write_flat_fallback_terrain(LEVEL_DIR)

    # 5. Materiali + satellite texture + asfalto procedurale + detail grass
    asphalt_rgb = sample_asphalt_color_from_satellite()
    print(f"asfalto RGB campionato: "
          f"({asphalt_rgb[0]:.3f}, {asphalt_rgb[1]:.3f}, {asphalt_rgb[2]:.3f})")
    generate_asphalt_texture(LEVEL_DIR, asphalt_rgb)
    generate_terrain_detail_texture(LEVEL_DIR)
    # Material colorMap BeamNG: path relativo senza leading / e CON estensione.
    asphalt_map = f"levels/{LEVEL_NAME}/art/road/asphalt_base.png"
    write_materials(LEVEL_DIR, asphalt_rgb, asphalt_color_map=asphalt_map)
    copy_satellite_texture(LEVEL_DIR)

    # 5b. Roadside clutter procedurale (sassi + ciuffi ai bordi strada)
    roadside_obj = generate_roadside_clutter(LEVEL_DIR)
    roadside_rel = None
    if roadside_obj is not None:
        roadside_dae = convert_to_dae(roadside_obj)
        roadside_rel = roadside_dae.relative_to(LEVEL_DIR).as_posix()

    # 6. Spawn con tuning offset (forward/up/turn_right dai parametri globali)
    sx, sy, _sz = read_first_centerline_point()
    top_z = road_top_z_at(road_obj, sx, sy, radius=3.0)
    heading = read_spawn_heading()
    # Applica tuning: avanti lungo il muso, su lungo Z, ruota a dx
    heading -= math.radians(SPAWN_TURN_RIGHT_DEG)
    # Forward dell'auto in world coord (muso = -Y locale ruotato di heading)
    fwd_x = math.sin(heading)
    fwd_y = -math.cos(heading)
    sx2 = sx + SPAWN_FORWARD_M * fwd_x
    sy2 = sy + SPAWN_FORWARD_M * fwd_y
    sz2 = top_z + 0.10 + SPAWN_UP_M
    spawn = (sx2, sy2, sz2)
    print(f"road top z (Blender): {top_z:.3f}  spawn: ({sx2:.2f}, {sy2:.2f}, {sz2:.3f})")
    print(f"spawn heading (tuned): {math.degrees(heading):.1f} deg "
          f"(forward={SPAWN_FORWARD_M}m up={SPAWN_UP_M}m turn_right={SPAWN_TURN_RIGHT_DEG}deg)")

    # 7. main.level.json + info.json
    write_level_json(LEVEL_DIR, road_rel, world_rel, roadside_rel,
                      terrain_rel, spawn, heading,
                      max_height, elev_min, z_offset_blender)
    write_empty_jsons(LEVEL_DIR)
    write_preview(LEVEL_DIR)

    # 8. mod info.json
    (MOD_DIR / "info.json").write_text(json.dumps({
        "title": LEVEL_TITLE,
        "description": "SS17 Valico del Macerone - full build (DEM + satellite + world)",
        "author": "mattoide",
        "version": "0.3.0",
        "tag": ["level", "map", "italy", "real-road", "mountain"],
    }, indent=2), encoding="utf-8")

    # 9. Zip
    zp = zip_mod()
    print(f"\nZip: {zp}  ({zp.stat().st_size // 1024} KB)")

    # 10. Copy nei mods BeamNG
    if MODS_DIR.exists():
        shutil.copy2(zp, MODS_DIR / "macerone3d.zip")
        print(f"Copiato in {MODS_DIR / 'macerone3d.zip'}")


if __name__ == "__main__":
    main()
