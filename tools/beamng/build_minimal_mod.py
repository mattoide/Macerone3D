"""
Build MINIMALE della mod BeamNG: SOLO il tracciato della SS17 come mesh
statico, piazzato su un terreno piatto. Niente piu' heightmap complesso,
niente texture satellitare, niente forest, niente edifici.

Strategia:
- Estraggo dal macerone.blend la sola collezione 'Road' (12 oggetti) in OBJ
- Converto OBJ -> DAE con il mio obj_to_dae.py
- Creo un terrain 1024x1024 PIATTO a z=0
- Nel main.level.json aggiungo un TSStatic che carica la road a world (0,0,0)
  (la road ha coord locali Blender, il primo punto e' a circa (3552,-4449,72))
- SpawnSphere al primo punto della centerline, 2m sopra l'asfalto.
- Il livello si carica istantaneamente, la strada e' li', niente altro.

Output:  output/beamng/macerone3d.zip  (distribuibile)
"""
from __future__ import annotations

import json
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
TERRAIN_MATERIAL_NAME = "macerone_base_ground"
TERRAIN_MATERIAL_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

# Terrain piatto: 1024x1024 cells, 12m per cell -> 12288m quadrato,
# centrato sull'origine (position=-6144,-6144)
TER_SIZE = 1024
TER_SQUARESIZE = 12.0
TER_EXTENT = TER_SIZE * TER_SQUARESIZE


def run(desc: str, cmd: list[str]) -> None:
    print(f"\n=== [{desc}] ===")
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print(f"[{desc}] EXIT {r.returncode}")
        sys.exit(r.returncode)


# ---------------------------------------------------------------------------
# Step 1: esporta solo collezione Road dal .blend via Blender headless
# ---------------------------------------------------------------------------
BLENDER_EXPORT_SCRIPT = '''
import bpy, sys
from pathlib import Path
out_path = sys.argv[sys.argv.index("--") + 1]
# Deseleziona tutto
bpy.ops.object.select_all(action="DESELECT")
# Seleziona SOLO la collezione Road (asfalto + banchine + mezzeria + linee + tombini)
road_col = bpy.data.collections.get("Road")
if road_col is None:
    print("!! Collezione 'Road' non trovata")
    sys.exit(2)
objs = [o for o in road_col.all_objects if o.type == "MESH"]

# Solidify su TUTTI gli oggetti della Road: 0.4m verso il basso,
# cosi' BeamNG ha volume di collisione non zero-thickness e le ruote
# non penetrano l'asfalto. Offset -1 = estrude dal lato opposto alle normali.
for o in objs:
    has_solidify = any(m.type == "SOLIDIFY" for m in o.modifiers)
    if has_solidify:
        continue
    mod = o.modifiers.new(name="RoadSolidify", type="SOLIDIFY")
    mod.thickness = 0.4
    mod.offset = -1.0
    mod.use_even_offset = True
    mod.use_quality_normals = True
    print(f"  + Solidify su {o.name}")

for o in objs:
    o.select_set(True)
print(f"Selezionati {len(objs)} oggetti della collezione Road")
bpy.context.view_layer.objects.active = objs[0]
bpy.ops.wm.obj_export(
    filepath=out_path,
    export_selected_objects=True,
    apply_modifiers=True,
    forward_axis="Y",
    up_axis="Z",
    export_materials=True,
)
print(f"Scritto {out_path}")
'''


def export_road_from_blender(out_obj: Path) -> None:
    script_path = BEAMNG_OUT / "_blender_road_export.py"
    script_path.write_text(BLENDER_EXPORT_SCRIPT, encoding="utf-8")
    run("blender_export_road", [
        BLENDER_EXE, "--background", str(BLEND_FILE),
        "--python", str(script_path),
        "--", str(out_obj),
    ])
    script_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Step 2: converte OBJ in DAE con obj_to_dae.py
# ---------------------------------------------------------------------------
def convert_to_dae(obj_path: Path) -> Path:
    run("obj_to_dae", [sys.executable, str(TOOLS / "obj_to_dae.py"),
                        str(obj_path)])
    return obj_path.with_suffix(".dae")


# ---------------------------------------------------------------------------
# Step 3: terrain piatto 1024x1024 a z=0
# ---------------------------------------------------------------------------
def write_flat_terrain(level_dir: Path) -> None:
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
    print(f"Scritto {ter}  ({ter.stat().st_size} bytes)")

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
    Image.fromarray(depth, mode="L").save(level_dir / "theTerrain.ter.depth.png",
                                            optimize=True)
    print("terrain piatto + depth map scritti")


# ---------------------------------------------------------------------------
# Step 4: materiali (terrain base + road)
# ---------------------------------------------------------------------------
def write_materials(level_dir: Path) -> None:
    # Terrain: un material grigio scuro semplice (no texture)
    terrain_mat_dir = level_dir / "art" / "terrain"
    terrain_mat_dir.mkdir(parents=True, exist_ok=True)
    terrain_materials = {
        f"{TERRAIN_MATERIAL_NAME}-{TERRAIN_MATERIAL_UUID}": {
            "internalName": TERRAIN_MATERIAL_NAME,
            "class": "TerrainMaterial",
            "persistentId": TERRAIN_MATERIAL_UUID,
            "diffuseColor": [0.35, 0.4, 0.3, 1.0],
            "diffuseSize": 256,
            "groundmodelName": "GRASS",
        }
    }
    (terrain_mat_dir / "main.materials.json").write_text(
        json.dumps(terrain_materials, indent=2), encoding="utf-8"
    )

    # Road materials: nomi che l'OBJ ha nel .mtl; mappo a colori
    # semplici perche' l'obj_to_dae mette solo diffuse color.
    # Nel dubbio creiamo entry generic che coprano i casi comuni della Road.
    road_materials = {}
    generic_entries = [
        ("Asphalt", [0.22, 0.22, 0.22]),
        ("AsphaltDark", [0.18, 0.18, 0.18]),
        ("Shoulder", [0.35, 0.32, 0.28]),
        ("LineWhite", [0.92, 0.92, 0.92]),
        ("LineYellow", [0.9, 0.8, 0.25]),
        ("default", [0.22, 0.22, 0.22]),
        ("DefaultMat", [0.22, 0.22, 0.22]),
    ]
    for name, rgb in generic_entries:
        road_materials[name] = {
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
        json.dumps(road_materials, indent=2), encoding="utf-8"
    )
    print("materials scritti")


# ---------------------------------------------------------------------------
# Step 5: info.json + main.level.json (da template, patch TerrainBlock +
#         TSStatic + SpawnSphere)
# ---------------------------------------------------------------------------
def read_first_centerline_point() -> tuple[float, float, float]:
    """Primo punto della centerline in coord locali Blender (x, y, z)."""
    import csv as _csv
    cl = ROOT / "output" / "centerline.csv"
    with cl.open(newline="", encoding="utf-8") as f:
        row = next(_csv.DictReader(f))
        return float(row["x"]), float(row["y"]), float(row["z"])


def road_top_z_at(obj_path: Path, cx: float, cy: float,
                    radius: float = 3.0) -> float:
    """Scanna l'OBJ e ritorna la z max dei vertici entro `radius` m da (cx,cy).
    La road e' Z-up, quindi questa e' la superficie superiore dell'asfalto al
    punto di spawn.
    """
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
        raise RuntimeError(
            f"Nessun vertice road entro {radius}m da ({cx},{cy})"
        )
    return best


def read_spawn_heading() -> float:
    """Heading (rad) dalla direzione P1->P_lookahead della centerline.

    Prende un punto ~15 m avanti per mediare curvature iniziali.
    Convenzione BeamNG: muso veicolo = -Y locale, quindi per orientare
    verso (dx, dy) serve ruotare -Y su (dx, dy) = ruotare +Y su (-dx,-dy),
    cioe' heading = atan2(dx, -dy).
    """
    import csv as _csv
    import math
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
    """Rotazione yaw (Z-axis) in quaternion BeamNG (qx, qy, qz, qw)."""
    import math
    return (0.0, 0.0, math.sin(h / 2.0), math.cos(h / 2.0))


def write_level_json(level_dir: Path, road_shape_rel: str,
                     spawn_xyz: tuple[float, float, float],
                     spawn_heading: float) -> None:
    qx, qy, qz, qw = heading_to_quat(spawn_heading)
    info = {
        "title": LEVEL_TITLE,
        "description": "Tracciato SS17 Valico del Macerone (Molise), mesh + terrain flat",
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

    # Copia il template autotest main.level.json
    tpl = TEMPLATE_LEVEL_JSON.read_text(encoding="utf-8")

    # Patch TerrainBlock: datafile + position + maxHeight=100 (tanto e' piatto) + squareSize
    def patch_tb(m):
        block = m.group(0)
        block = re.sub(r'"terrainFile"\s*:\s*"[^"]+"',
                        f'"terrainFile" : "levels/{LEVEL_NAME}/theTerrain.ter"',
                        block)
        block = re.sub(r'"maxHeight"\s*:\s*[\d\.e\-\+]+',
                        '"maxHeight" : 100.0', block)
        half = TER_EXTENT / 2.0
        block = re.sub(r'"position"\s*:\s*\[[^\]]+\]',
                        f'"position" : [ {-half}, {-half}, 0.0 ]', block)
        if '"squareSize"' not in block:
            block = block.replace('"maxHeight"',
                                    f'"squareSize" : {TER_SQUARESIZE},\n          "maxHeight"')
        else:
            block = re.sub(r'"squareSize"\s*:\s*[\d\.e\-\+]+',
                            f'"squareSize" : {TER_SQUARESIZE}', block)
        return block

    tpl = re.sub(r'\{\s*"class"\s*:\s*"TerrainBlock".*?\}', patch_tb, tpl,
                   count=1, flags=re.S)

    # Patch SpawnSphere: aggiungi position+name+rotation (axis-angle, Z-axis)
    sx, sy, sz = spawn_xyz
    import math
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

    # Inject TSStatic della strada come child del MissionGroup PRIMA della parentesi
    # piu' esterna. Metto il TSStatic dentro Level_objects.
    tsstatic_json = (
        '{\n'
        '  "class" : "TSStatic",\n'
        '  "name" : "macerone_road_mesh",\n'
        '  "position" : [ 0, 0, 0 ],\n'
        '  "allowPlayerStep" : "1",\n'
        '  "collisionType" : "Visible Mesh Final",\n'
        '  "decalType" : "Visible Mesh Final",\n'
        f'  "shapeName" : "levels/{LEVEL_NAME}/{road_shape_rel}"\n'
        '}'
    )

    # Trovo il primo SimGroup "Level_objects" e inject il TSStatic alla fine
    # dei childs. Semplice marker: aggiungo dopo il TerrainBlock.
    def inject_after_terrain(m):
        return m.group(0) + ",\n        " + tsstatic_json

    tpl = re.sub(r'\{\s*"class"\s*:\s*"TerrainBlock".*?\}', inject_after_terrain,
                   tpl, count=1, flags=re.S)

    (level_dir / "main.level.json").write_text(tpl, encoding="utf-8")
    print(f"main.level.json scritto (TSStatic road @ (0,0,0), spawn @ {spawn_xyz})")


def write_empty_jsons(level_dir: Path) -> None:
    (level_dir / "main.decals.json").write_text(
        json.dumps({"header": {"name": "DecalData File", "version": 1},
                     "instances": {}}, indent=2), encoding="utf-8"
    )
    (level_dir / "map.json").write_text(
        json.dumps({"segments": {}}, indent=2), encoding="utf-8"
    )


def write_preview(level_dir: Path) -> None:
    # preview.jpg dal satellite se c'e', altrimenti placeholder
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
    print("=== BUILD MINIMAL MOD ===\n")
    if MOD_DIR.exists():
        shutil.rmtree(MOD_DIR)
    LEVEL_DIR.mkdir(parents=True, exist_ok=True)
    shapes_dir = LEVEL_DIR / "art" / "shapes"
    shapes_dir.mkdir(parents=True, exist_ok=True)

    # 1. export road obj
    road_obj = shapes_dir / "macerone_road.obj"
    export_road_from_blender(road_obj)

    # 2. convert to DAE
    road_dae = convert_to_dae(road_obj)
    road_rel = road_dae.relative_to(LEVEL_DIR).as_posix()
    print(f"road shape: {road_rel}")

    # 3. terrain flat
    write_flat_terrain(LEVEL_DIR)

    # 4. materials
    write_materials(LEVEL_DIR)

    # 5. spawn from centerline
    sx, sy, _sz = read_first_centerline_point()
    # Prendo la z REALE del top asfalto dall'OBJ esportato (post-Solidify),
    # cosi' lo spawn sta esattamente 10 cm sopra la superficie.
    top_z = road_top_z_at(road_obj, sx, sy, radius=3.0)
    spawn = (sx, sy, top_z + 0.10)
    heading = read_spawn_heading()
    import math
    print(f"road top z @ spawn: {top_z:.3f}  ->  spawn z: {spawn[2]:.3f}")
    print(f"spawn heading: {math.degrees(heading):.1f} deg")

    # 6. level.json
    write_level_json(LEVEL_DIR, road_rel, spawn, heading)
    write_empty_jsons(LEVEL_DIR)
    write_preview(LEVEL_DIR)

    # 7. mod info.json (metadata)
    (MOD_DIR / "info.json").write_text(json.dumps({
        "title": LEVEL_TITLE,
        "description": "SS17 Valico del Macerone - minimal build",
        "author": "mattoide",
        "version": "0.2.0",
        "tag": ["level", "map", "italy", "real-road"],
    }, indent=2), encoding="utf-8")

    # 8. zip
    zp = zip_mod()
    print(f"\nZip: {zp}  ({zp.stat().st_size // 1024} KB)")

    # 9. copy nella cartella mods
    dst = Path(r"C:\Users\Matto\AppData\Local\BeamNG\BeamNG.drive\current\mods") / "macerone3d.zip"
    if dst.parent.exists():
        shutil.copy2(zp, dst)
        print(f"Copiato in {dst}")


if __name__ == "__main__":
    main()
