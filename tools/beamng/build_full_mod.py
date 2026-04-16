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
world_cols = [c for c in world_cols_csv.split(",") if c]

def select_only(objs):
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    if objs:
        bpy.context.view_layer.objects.active = objs[0]

# --- Road (con Solidify 0.4m sotto) ---
road_col = bpy.data.collections.get("Road")
if road_col is None:
    print("!! Collezione 'Road' non trovata")
    sys.exit(2)
road_objs = [o for o in road_col.all_objects if o.type == "MESH"]
for o in road_objs:
    if any(m.type == "SOLIDIFY" for m in o.modifiers):
        continue
    mod = o.modifiers.new(name="RoadSolidify", type="SOLIDIFY")
    mod.thickness = 0.4
    mod.offset = -1.0
    mod.use_even_offset = True
    mod.use_quality_normals = True
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

# --- World: tutto il resto (eccetto Road, Grass, Bushes e terrain Blender) ---
world_objs = []
for cname in world_cols:
    col = bpy.data.collections.get(cname)
    if col is None:
        print(f"  collezione '{cname}' non trovata, skip")
        continue
    ms = [o for o in col.all_objects if o.type == "MESH"]
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


def export_from_blender(road_obj: Path, world_obj: Path) -> None:
    script_path = BEAMNG_OUT / "_blender_full_export.py"
    script_path.write_text(BLENDER_EXPORT_SCRIPT, encoding="utf-8")
    world_cols = ",".join(WORLD_COLLECTIONS)
    run("blender_export_full", [
        BLENDER_EXE, "--background", str(BLEND_FILE),
        "--python", str(script_path),
        "--", str(road_obj), str(world_obj), world_cols,
    ])
    script_path.unlink(missing_ok=True)


def convert_to_dae(obj_path: Path) -> Path:
    run("obj_to_dae", [sys.executable, str(TOOLS / "obj_to_dae.py"),
                        str(obj_path)])
    return obj_path.with_suffix(".dae")


# ---------------------------------------------------------------------------
# Step 3: .ter binary dal heightmap DEM + terrain.json
# ---------------------------------------------------------------------------
def carve_heightmap_under_road(arr: np.ndarray, elev_min: float,
                                 max_height: float, z_offset_blender: float,
                                 target_is_blender_z: bool = False) -> int:
    """Abbassa dolcemente il heightmap DEM verso la quota strada. Usa coord
    REALI DEM: per ogni centerline point target = (real_road_z - 2m) / maxH.

    Se `target_is_blender_z=True` il target e' in coord Blender (road_z =
    z_blender), altrimenti real (road_z = z_blender + z_offset_blender).
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
            target = road_z - elev_min - 2.0
            if target < 0:
                target = 0.0
            tgt_u16 = min(65535.0, target / max_height * 65535.0)

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
            # blended = sub*(1-a) + target*a; carve solo se < sub
            blended = sub * (1.0 - a) + tgt_u16 * a
            new = np.minimum(sub, blended)
            changed = int((new < sub).sum())
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
def write_materials(level_dir: Path) -> None:
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
    entries = [
        # Road
        ("Asphalt", [0.10, 0.10, 0.10]),
        ("AsphaltPatch_Dark", [0.06, 0.06, 0.06]),
        ("AsphaltPatch_Light", [0.14, 0.14, 0.14]),
        ("Shoulder", [0.35, 0.32, 0.28]),
        ("Shoulder_L", [0.35, 0.32, 0.28]),
        ("Shoulder_R", [0.35, 0.32, 0.28]),
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
        mats[name] = {
            "name": name,
            "mapTo": name,
            "class": "Material",
            "Stages": [
                {"diffuseColor": [*rgb, 1.0]},
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
        world_dae = convert_to_dae(world_obj)
        world_rel = world_dae.relative_to(LEVEL_DIR).as_posix()

    # 4. Terrain .ter dal DEM (usa z_offset_blender inferito)
    max_height, elev_min, z_offset_blender = write_dem_terrain(
        LEVEL_DIR, info, z_offset_blender
    )

    # 5. Materiali + satellite texture
    write_materials(LEVEL_DIR)
    copy_satellite_texture(LEVEL_DIR)

    # 6. Spawn (10cm sopra top asfalto, coord Blender native come il minimal)
    sx, sy, _sz = read_first_centerline_point()
    top_z = road_top_z_at(road_obj, sx, sy, radius=3.0)
    spawn = (sx, sy, top_z + 0.10)
    heading = read_spawn_heading()
    print(f"road top z (Blender): {top_z:.3f}  ->  spawn z: {spawn[2]:.3f}")
    print(f"spawn heading: {math.degrees(heading):.1f} deg")

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
