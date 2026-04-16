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
ROAD_CORRIDOR_FILTER_M = 3.5

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
world_cols_csv = args[2]
skip_names_csv = args[3] if len(args) > 3 else ""
world_cols = [c for c in world_cols_csv.split(",") if c]
skip_names = set(n for n in skip_names_csv.split(",") if n)

def select_only(objs):
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    if objs:
        bpy.context.view_layer.objects.active = objs[0]

# --- Road: Solidify SOLO su Asphalt e Shoulder, non su elementi sottili
# (linee, catarifrangenti, tombini, patches) che non devono sporgere dalla
# superficie.
SOLIDIFY_KEYWORDS = ("Asphalt", "Shoulder")
def needs_solidify(name: str) -> bool:
    if "Patch" in name:  # AsphaltPatches va a filo con l'asfalto, no solidify
        return False
    return any(k in name for k in SOLIDIFY_KEYWORDS)

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
    # Scrivi un OBJ vuoto minimale cosi' il main script capisce che non c'e' mondo
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
'''


SKIP_MESH_NAMES = [
    "Delineators",  # paletti bianchi bassi al bordo strada, non servono
]


def export_from_blender(road_obj: Path, world_obj: Path) -> None:
    script_path = BEAMNG_OUT / "_blender_full_export.py"
    script_path.write_text(BLENDER_EXPORT_SCRIPT, encoding="utf-8")
    world_cols = ",".join(WORLD_COLLECTIONS)
    skip = ",".join(SKIP_MESH_NAMES)
    run("blender_export_full", [
        BLENDER_EXE, "--background", str(BLEND_FILE),
        "--python", str(script_path),
        "--", str(road_obj), str(world_obj), world_cols, skip,
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

    # Precompute kernel: distanza normalizzata da (0,0)
    R = 8  # raggio in celle, ~96m
    drs, dcs = np.meshgrid(np.arange(-R, R + 1), np.arange(-R, R + 1), indexing="ij")
    dist = np.sqrt(drs * drs + dcs * dcs)
    mask_in = dist <= R
    # alpha: 1 al centro, 0 al bordo (falloff lineare)
    alpha = np.where(mask_in, 1.0 - dist / R, 0.0).astype(np.float32)

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
            # Bounds in metri (sempre sotto road): carve a -1m, fill a -3m
            upper_m = max(0.0, road_z - elev_min - 1.0)
            lower_m = max(0.0, road_z - elev_min - 3.0)
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
            # Bounds che si rilassano al bordo: al centro (a=1) upper/lower
            # stretti, al bordo (a=0) infinitamente rilassati → no effect.
            # +30000 u16 ~= +550m, sufficiente per neutralizzare.
            relax = (1.0 - a) * 30000.0
            upper_bound = upper_u16 + relax
            lower_bound = np.maximum(0.0, lower_u16 - relax)
            new = np.clip(sub, lower_bound, upper_bound)
            changed = int((np.abs(new - sub) > 0.5).sum())
            carved += changed
            arr_f[r0c:r1c, c0c:c1c] = new

    arr[:] = np.clip(arr_f, 0, 65535).astype(np.uint16)
    return carved


def write_dem_terrain(level_dir: Path, info: dict,
                       z_offset_blender: float) -> tuple[float, float, float]:
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

    # Carve sotto la strada: target e' in coord Blender (identico alla road).
    carved = carve_heightmap_under_road(arr, 0.0, max_height_shifted,
                                          z_offset_blender,
                                          target_is_blender_z=True)
    print(f"  heightmap carve: abbassate {carved} celle sotto la centerline")

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
    # TerrainMaterial con diffuse = texture satellite.
    # BeamNG cerca il file provando estensioni .dds .png .jpg quindi il path
    # va scritto SENZA estensione. Il leading "/" e' consigliato (path
    # assoluto dalla root della mod). diffuseSize = metri di tiling in world
    # space: 12288 = una sola ripetizione sul terrain intero.
    terrain_mat_dir = level_dir / "art" / "terrain"
    terrain_mat_dir.mkdir(parents=True, exist_ok=True)
    terrain_materials = {
        f"{TERRAIN_MATERIAL_NAME}-{TERRAIN_MATERIAL_UUID}": {
            "internalName": TERRAIN_MATERIAL_NAME,
            "class": "TerrainMaterial",
            "persistentId": TERRAIN_MATERIAL_UUID,
            "diffuseMap": f"/levels/{LEVEL_NAME}/art/terrains/satellite_diffuse",
            "diffuseColor": [0.45, 0.50, 0.38, 1.0],
            "diffuseSize": 12288,
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
    asphalt_dark = [a_r * 0.7, a_g * 0.7, a_b * 0.7]
    asphalt_light = [min(1.0, a_r * 1.25), min(1.0, a_g * 1.25), min(1.0, a_b * 1.25)]
    shoulder_rgb = [min(1.0, a_r * 1.05), min(1.0, a_g * 1.02), min(1.0, a_b * 0.95)]
    entries = [
        # Road: colori campionati dal satellite ESRI lungo la centerline
        ("Asphalt", [a_r, a_g, a_b]),
        ("AsphaltPatch_Dark", asphalt_dark),
        ("AsphaltPatch_Light", asphalt_light),
        ("Shoulder", shoulder_rgb),
        ("Shoulder_L", shoulder_rgb),
        ("Shoulder_R", shoulder_rgb),
        ("LineWhite", [0.92, 0.92, 0.92]),
        ("LineYellow", [0.88, 0.78, 0.20]),
        ("Catarifrangente", [0.85, 0.80, 0.15]),
        ("Catarifrangenti", [0.85, 0.80, 0.15]),
        ("StopLines", [0.92, 0.92, 0.92]),
        ("Tombino", [0.20, 0.18, 0.17]),
        # World
        ("Building", [0.78, 0.72, 0.60]),
        ("BuildingWall", [0.78, 0.72, 0.60]),
        ("Roof", [0.55, 0.30, 0.22]),
        ("Chimney", [0.55, 0.30, 0.22]),
        ("Wall", [0.58, 0.52, 0.45]),
        ("WallDryStone", [0.58, 0.52, 0.45]),
        ("Guardrail", [0.70, 0.72, 0.75]),
        ("Tree", [0.22, 0.35, 0.18]),
        ("TreeTrunk", [0.28, 0.20, 0.12]),
        ("Canopy", [0.22, 0.35, 0.18]),
        ("Cypress", [0.18, 0.30, 0.15]),
        ("Shrub", [0.30, 0.40, 0.22]),
        ("Rock", [0.52, 0.50, 0.45]),
        ("Pole", [0.55, 0.55, 0.55]),
        ("Sign", [0.85, 0.85, 0.85]),
        ("SignalPost", [0.55, 0.55, 0.55]),
        ("default", [0.60, 0.60, 0.60]),
        ("DefaultMat", [0.60, 0.60, 0.60]),
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
    """Genera PNG 512x512 procedurale per l'asfalto: grana fine + piccole
    striature longitudinali + leggera variazione macchie. Colore di base
    dal satellite. Ritorna path relativo al level_dir."""
    size = 512
    rng = np.random.default_rng(42)
    # grana (rumore gaussiano stretto) + granelli (noise salt)
    grain = rng.normal(0.0, 0.05, (size, size)).astype(np.float32)
    pepper = rng.random((size, size), dtype=np.float32)
    dark_spots = np.where(pepper > 0.98, -0.10, 0.0)
    light_spots = np.where(pepper < 0.015, 0.08, 0.0)
    # Striature longitudinali (direzione Y): piccole variazioni di luminosita'
    strip_base = rng.normal(0.0, 0.04, (size,)).astype(np.float32)
    strip = np.tile(strip_base[None, :], (size, 1))  # varia in X, uniforme in Y
    # Crepe: linee scure casuali orizzontali fini
    cracks = np.zeros((size, size), dtype=np.float32)
    for _ in range(30):
        y = rng.integers(0, size)
        x0 = rng.integers(0, size // 2)
        x1 = rng.integers(size // 2, size)
        cracks[y, x0:x1] = -0.12
        if y + 1 < size:
            cracks[y + 1, x0:x1] = -0.06

    delta = grain + dark_spots + light_spots + strip + cracks
    r, g, b = base_rgb
    R = np.clip(r + delta, 0.0, 1.0)
    G = np.clip(g + delta, 0.0, 1.0)
    B = np.clip(b + delta, 0.0, 1.0)
    img = np.stack([R, G, B], axis=-1)
    img_u8 = (img * 255.0).astype(np.uint8)

    tex_dir = level_dir / "art" / "road"
    tex_dir.mkdir(parents=True, exist_ok=True)
    out = tex_dir / "asphalt_base.png"
    Image.fromarray(img_u8, mode="RGB").save(out, optimize=True)
    rel = f"/levels/{LEVEL_NAME}/art/road/asphalt_base"
    print(f"Asfalto texture procedurale: {out.relative_to(MOD_DIR)}")
    return rel


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

    # 2. Blender export: road (con Solidify) + world (tutto il resto)
    road_obj = shapes_dir / "macerone_road.obj"
    world_obj = shapes_dir / "macerone_world.obj"
    export_from_blender(road_obj, world_obj)

    # 3. OBJ -> DAE
    road_dae = convert_to_dae(road_obj)
    road_rel = road_dae.relative_to(LEVEL_DIR).as_posix()

    world_has_content = world_obj.exists() and world_obj.stat().st_size > 200
    world_rel = None
    if world_has_content:
        # Filtro oggetti world che invadono il corridoio strada (alberi/rocce
        # procedurali finiti per sbaglio sull'asfalto, causa bump fisici).
        removed = filter_world_obj_near_road(world_obj, ROAD_CORRIDOR_FILTER_M)
        print(f"  filter corridoio {ROAD_CORRIDOR_FILTER_M}m: "
              f"rimosse {removed} face dal world mesh")
        world_dae = convert_to_dae(world_obj)
        world_rel = world_dae.relative_to(LEVEL_DIR).as_posix()

    # 4. Terrain .ter dal DEM (usa z_offset_blender inferito)
    max_height, elev_min, z_offset_blender = write_dem_terrain(
        LEVEL_DIR, info, z_offset_blender
    )

    # 5. Materiali + satellite texture + asfalto procedurale
    asphalt_rgb = sample_asphalt_color_from_satellite()
    print(f"asfalto RGB campionato: "
          f"({asphalt_rgb[0]:.3f}, {asphalt_rgb[1]:.3f}, {asphalt_rgb[2]:.3f})")
    asphalt_map = generate_asphalt_texture(LEVEL_DIR, asphalt_rgb)
    write_materials(LEVEL_DIR, asphalt_rgb, asphalt_color_map=asphalt_map)
    copy_satellite_texture(LEVEL_DIR)

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
    write_level_json(LEVEL_DIR, road_rel, world_rel, spawn, heading,
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
