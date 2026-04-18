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
SPAWN_FORWARD_M = 10.0     # metri avanti LUNGO LA STRADA (non lungo muso)
SPAWN_UP_M = 1.0            # metri in alto (oltre ai 0.10 sopra asfalto)
SPAWN_TURN_RIGHT_DEG = -20.0   # gradi di rotazione fine a destra (negativo = sx)
# MASTER offset spawn rotation: tarare empiricamente. 0 = formula -Y locale,
# +90 = muso-convention +X, -90 = muso-convention -X, 180 = flip (muso +Y).
SPAWN_ROT_OFFSET_DEG = 270.0

# --- Filtro oggetti world intrusivi ----------------------------------------
# Triangoli del world mesh con centroide entro questa distanza dalla
# centerline vengono rimossi: alberi procedurali, bushes, rocce che sono
# finiti casualmente sull'asfalto.
ROAD_CORRIDOR_FILTER_M = 5.5

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


# Mapping material_name -> texture path BeamNG-absoluto (per obj_to_dae)
# Solo i material con texture reale. Building/Guardrail/etc usano solo color.
MATERIAL_TEXTURE_MAP = {
    "Asphalt": f"levels/{LEVEL_NAME}/art/road/asphalt_base.color.png",
    "AsphaltPatch_Dark": f"levels/{LEVEL_NAME}/art/road/asphalt_base.color.png",
    "AsphaltPatch_Light": f"levels/{LEVEL_NAME}/art/road/asphalt_base.color.png",
    "AsphaltWear": f"levels/{LEVEL_NAME}/art/road/asphalt_base.color.png",
    "SatelliteTerrain": f"levels/{LEVEL_NAME}/art/terrains/satellite_diffuse.color.png",
    "Terrain": f"levels/{LEVEL_NAME}/art/terrains/satellite_diffuse.color.png",
    "TerrainMat": f"levels/{LEVEL_NAME}/art/terrains/satellite_diffuse.color.png",
    "Ground": f"levels/{LEVEL_NAME}/art/terrains/satellite_diffuse.color.png",
    "TreeCanopy": f"levels/{LEVEL_NAME}/art/nature/foliage.color.png",
    "TreeFoliage": f"levels/{LEVEL_NAME}/art/nature/foliage.color.png",
    "Canopy": f"levels/{LEVEL_NAME}/art/nature/foliage.color.png",
    "TreeTrunk": f"levels/{LEVEL_NAME}/art/nature/bark.color.png",
    "TreeBark": f"levels/{LEVEL_NAME}/art/nature/bark.color.png",
    "TreeBillboard": f"levels/{LEVEL_NAME}/art/nature/tree_billboard.color.png",
    "TreeBillboard_P0": f"levels/{LEVEL_NAME}/art/nature/tree_billboard.color.png",
    "TreeBillboard_P1": f"levels/{LEVEL_NAME}/art/nature/tree_billboard.color.png",
    "TreeBillboard_P2": f"levels/{LEVEL_NAME}/art/nature/tree_billboard.color.png",
    "TreeBillboard_P3": f"levels/{LEVEL_NAME}/art/nature/tree_billboard.color.png",
    # Landmark signs
    "SignValico": f"levels/{LEVEL_NAME}/art/shapes/signs/sign_valico.color.png",
    "SignSS17": f"levels/{LEVEL_NAME}/art/shapes/signs/sign_ss17.color.png",
    "SignDirezionale": f"levels/{LEVEL_NAME}/art/shapes/signs/sign_direzionale.color.png",
    # Video landmarks: cartelli specifici + edifici iconici
    "VidSignLimit50": f"levels/{LEVEL_NAME}/art/shapes/video_landmarks/sign_limit50.color.png",
    "VidSignLimit30": f"levels/{LEVEL_NAME}/art/shapes/video_landmarks/sign_limit30.color.png",
    "VidSignWinter": f"levels/{LEVEL_NAME}/art/shapes/video_landmarks/sign_winter_tires.color.png",
    "VidSignCurveSx": f"levels/{LEVEL_NAME}/art/shapes/video_landmarks/sign_curve_left.color.png",
    "VidBldgRudere": f"levels/{LEVEL_NAME}/art/shapes/video_landmarks/bldg_rudere.color.png",
    "VidBldgTorretta": f"levels/{LEVEL_NAME}/art/shapes/video_landmarks/bldg_torretta.color.png",
    "VidBldgCasale": f"levels/{LEVEL_NAME}/art/shapes/video_landmarks/bldg_casale.color.png",
}


def inject_map_kd_in_mtl(mtl_path: Path) -> int:
    """Aggiunge riga `map_Kd <path>` dopo ogni newmtl se il nome material
    matcha MATERIAL_TEXTURE_MAP. RIMUOVE prima eventuali map_Kd preesistenti
    scritti da Blender (con path assoluti Windows che BeamNG non legge)."""
    if not mtl_path.exists():
        return 0
    lines = mtl_path.read_text(encoding="utf-8", errors="replace").splitlines()
    # Step 1: rimuove TUTTE le righe map_Kd (anche Blender default path assoluti)
    cleaned = [ln for ln in lines if not ln.strip().startswith("map_Kd ")]
    # Step 2: inject map_Kd DOPO newmtl per i material matching
    out: list[str] = []
    n_injected = 0
    for line in cleaned:
        out.append(line)
        stripped = line.strip()
        if stripped.startswith("newmtl "):
            mat_name = stripped.split(maxsplit=1)[1]
            tex = MATERIAL_TEXTURE_MAP.get(mat_name)
            if tex:
                out.append(f"map_Kd {tex}")
                n_injected += 1
    mtl_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return n_injected


def _convert_to_dae_orig(obj_path: Path) -> Path:
    run("obj_to_dae", [sys.executable, str(TOOLS / "obj_to_dae.py"),
                        str(obj_path)])
    return obj_path.with_suffix(".dae")


def convert_to_dae(obj_path: Path) -> Path:
    """Inietta map_Kd nel MTL se mancante, poi converte OBJ -> DAE.
    Il DAE risultante include library_images + texture reference, cosi'
    BeamNG auto-carica le texture senza dover leggere main.materials.json."""
    mtl_path = obj_path.with_suffix(".mtl")
    injected = inject_map_kd_in_mtl(mtl_path)
    if injected > 0:
        print(f"  map_Kd iniettato in {mtl_path.name}: {injected} material")
    return _convert_to_dae_orig(obj_path)


def shift_marking_vertices(obj_path: Path, shift_z: float = 0.03) -> int:
    """Alza di shift_z i vertex degli oggetti Marking*/StopLines nel road OBJ
    per evitare Z-fighting con la superficie Road (stesso z base). 3cm
    sufficiente a renderle sempre visibili davanti al mesh Road."""
    MARK_NAMES = ("MarkingCenter", "MarkingEdge_L", "MarkingEdge_R",
                  "StopLines", "RoadStuds_W", "RoadStuds_Y")
    lines = obj_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    out = []
    current = None
    shift_active = False
    shifted = 0
    for line in lines:
        if line.startswith("o ") or line.startswith("g "):
            current = line.split(maxsplit=1)[1].strip()
            shift_active = any(m in current for m in MARK_NAMES)
            out.append(line)
            continue
        if shift_active and line.startswith("v "):
            p = line.split()
            x, y, z = float(p[1]), float(p[2]), float(p[3])
            z += shift_z
            out.append(f"v {x:.6f} {y:.6f} {z:.6f}\n")
            shifted += 1
            continue
        out.append(line)
    obj_path.write_text("".join(out), encoding="utf-8")
    return shifted


# ---------------------------------------------------------------------------
# Colore asfalto medio campionato dall'immagine satellite sulla centerline
# ---------------------------------------------------------------------------
def sample_asphalt_color_from_satellite() -> tuple[float, float, float]:
    """Campiona la satellite ESRI sopra la centerline, filtra solo i pixel
    'asfalto' (bassa saturazione = grigi, no verdi/rossi laterali), usa
    mediana invece di media per essere robusto a outlier. Campiona anche
    un intorno 3x3 pixel per avere piu' samples."""
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
    arr = np.array(im).astype(np.float32) / 255.0
    denom_lon = bbox["east"] - bbox["west"]
    denom_lat = bbox["north"] - bbox["south"]
    collected_rgb = []
    for p in cl:
        u = (p["lon"] - bbox["west"]) / denom_lon
        v = (bbox["north"] - p["lat"]) / denom_lat
        px = int(u * W)
        py = int(v * H)
        if not (1 <= px < W - 1 and 1 <= py < H - 1):
            continue
        patch = arr[py-1:py+2, px-1:px+2].reshape(-1, 3)  # 9 pixels
        for rgb in patch:
            r, g, b = rgb
            mx, mn = max(r, g, b), min(r, g, b)
            sat = (mx - mn) / (mx + 1e-6)
            lum = (r + g + b) / 3.0
            # Asfalto: saturazione bassa (grigio) + luminosita' medio-bassa
            if sat < 0.12 and 0.20 < lum < 0.70:
                collected_rgb.append((r, g, b))
    if len(collected_rgb) < 10:
        return (0.35, 0.35, 0.35)
    a = np.array(collected_rgb)
    r = float(np.median(a[:, 0]))
    g = float(np.median(a[:, 1]))
    b = float(np.median(a[:, 2]))
    # Desatura leggermente verso gray neutro
    gray = (r + g + b) / 3.0
    r = r * 0.85 + gray * 0.15
    g = g * 0.85 + gray * 0.15
    b = b * 0.85 + gray * 0.15
    print(f"  asfalto sample filtrato: {len(collected_rgb)} px, "
          f"mediana ({r:.3f}, {g:.3f}, {b:.3f})")
    return (min(1.0, r), min(1.0, g), min(1.0, b))


# ---------------------------------------------------------------------------
# Filtro OBJ world: rimuove triangoli dentro il corridoio road
# ---------------------------------------------------------------------------
FILTER_OBJ_NAME_KEYWORDS = (
    "TreeTrunks", "TreeCanopies",
    "RoadsideTrunks", "RoadsideCanopies", "Roadside",
    "Bushes", "Rocks", "StoneWalls",
)


def carve_terrain_mesh_near_road(terrain_obj_path: Path) -> int:
    """Abbassa i vertex del mesh Terrain Blender che invadono lo spazio
    sopra la strada. Necessario perche' il mesh Blender Terrain a volte
    ha zone (tipicamente in curve strette o sulle montagne) dove i vertex
    sono sopra la road locale.

    Per ogni vertex terrain: trova nearest centerline point, calcola
    max_z permesso in funzione della distanza:
    - d < 6m: z <= road_z - 0.3m (sotto strada + banchina)
    - d < 30m: z <= road_z + 0.3m (appoggio banchina)
    - d < 80m: z <= road_z + 4m (collina dolce)
    - oltre: libero
    """
    import csv as _csv
    cl_path = ROOT / "output" / "centerline.csv"
    if not cl_path.exists():
        return 0
    cl = []
    with cl_path.open(newline="", encoding="utf-8") as f:
        for r in _csv.DictReader(f):
            cl.append((float(r["x"]), float(r["y"]), float(r["z"])))

    cell_grid = 30.0
    buckets: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
    for (x, y, z) in cl:
        buckets.setdefault((int(x // cell_grid), int(y // cell_grid)), []).append((x, y, z))

    def nearest_cl(vx: float, vy: float) -> tuple[float, float] | None:
        ix = int(vx // cell_grid); iy = int(vy // cell_grid)
        dmin = float("inf"); cz = 0.0
        for di in (-2, -1, 0, 1, 2):
            for dj in (-2, -1, 0, 1, 2):
                for (cx, cy, cz_) in buckets.get((ix + di, iy + dj), []):
                    d = (cx - vx) ** 2 + (cy - vy) ** 2
                    if d < dmin:
                        dmin = d; cz = cz_
        if dmin == float("inf"):
            return None
        return (math.sqrt(dmin), cz)

    def max_z_at_dist(d: float, road_z: float) -> float:
        # Ulteriore tightening per l'issue "terrain sopra strada":
        # - fascia piatta allargata a 12m (era 10m)
        # - clearance sotto strada aumentata da 0.3m a 0.8m (road_top
        #   e' centerline+0.35, quindi terrain a road_top-1.15 = ben sotto)
        # - scarpata a 50m (era 40m), collina a 140m (era 120m)
        if d < 12.0:
            return road_z - 0.8
        if d < 50.0:
            # lerp da road-0.8 a road+0.2 sui 38m (scarpata dolce)
            t = (d - 12.0) / 38.0
            return road_z - 0.8 + t * 1.0
        if d < 140.0:
            # lerp da road+0.2 a road+3.0m (collina gradevole)
            t = (d - 50.0) / 90.0
            return road_z + 0.2 + t * 2.8
        # Oltre 140m: cap a road+7m
        return road_z + 7.0

    lines = terrain_obj_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    out = []
    carved = 0
    for line in lines:
        if not line.startswith("v "):
            out.append(line)
            continue
        p = line.split()
        x, y, z = float(p[1]), float(p[2]), float(p[3])
        nr = nearest_cl(x, y)
        if nr is None:
            out.append(line)
            continue
        d, road_z = nr
        mz = max_z_at_dist(d, road_z)
        if z > mz:
            z = mz
            carved += 1
        out.append(f"v {x:.6f} {y:.6f} {z:.6f}\n")
    terrain_obj_path.write_text("".join(out), encoding="utf-8")
    return carved


def remove_buildings_on_road(obj_path: Path, radius_m: float = 4.0,
                               target_names: tuple[str, ...] = (
                                   "Buildings_Walls", "Buildings_Roofs",
                                   "Chimneys", "ExtraBuildings")) -> int:
    """Rimuove INTERI edifici (componenti connesse) dal mesh se un qualsiasi
    loro vertex cade entro radius_m dalla centerline. Filtra per nome
    oggetto nel OBJ (solo building-like). Ritorna numero di isole rimosse."""
    import csv as _csv
    cl_path = ROOT / "output" / "centerline.csv"
    if not cl_path.exists():
        return 0
    cell_grid = 30.0
    buckets: dict[tuple[int, int], list[tuple[float, float]]] = {}
    with cl_path.open(newline="", encoding="utf-8") as f:
        for r in _csv.DictReader(f):
            x = float(r["x"]); y = float(r["y"])
            buckets.setdefault((int(x // cell_grid), int(y // cell_grid)), []).append((x, y))
    r2 = radius_m * radius_m

    def near_road(vx: float, vy: float) -> bool:
        ix = int(vx // cell_grid); iy = int(vy // cell_grid)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for (cx, cy) in buckets.get((ix + di, iy + dj), []):
                    if (cx - vx) ** 2 + (cy - vy) ** 2 <= r2:
                        return True
        return False

    # Prima passata: legge vertex + face con track di current_obj
    lines = obj_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    wverts: list[tuple[float, float, float]] = []
    # Memorizzo per ogni face: (obj_name, vertex_indices list)
    face_info: list[tuple[int, str, list[int]]] = []  # (line_idx, name, idx)
    current_obj = "default"
    for li, line in enumerate(lines):
        if line.startswith("v "):
            p = line.split()
            wverts.append((float(p[1]), float(p[2]), float(p[3])))
        elif line.startswith("o "):
            current_obj = line.split(maxsplit=1)[1].strip()
        elif line.startswith("f "):
            toks = line.split()[1:]
            idx = [int(t.split("/")[0]) - 1 for t in toks]
            face_info.append((li, current_obj, idx))

    # Union-find solo su face degli oggetti target
    n = len(wverts)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for (_, name, idx) in face_info:
        if name not in target_names:
            continue
        for i in range(1, len(idx)):
            if 0 <= idx[0] < n and 0 <= idx[i] < n:
                union(idx[0], idx[i])

    # Raggruppa vertex per root; per ogni isola (tra quelle in target) verifica
    # se qualche vertex e' vicino alla road
    island_roots_bad: set[int] = set()
    groups: dict[int, list[int]] = {}
    for (_, name, idx) in face_info:
        if name not in target_names:
            continue
        for vi in idx:
            if 0 <= vi < n:
                groups.setdefault(find(vi), []).append(vi)

    for root, vlist in groups.items():
        for vi in vlist:
            if near_road(wverts[vi][0], wverts[vi][1]):
                island_roots_bad.add(root)
                break

    if not island_roots_bad:
        return 0

    # Riscrivi OBJ escludendo le face che toccano isole bad
    lines_to_skip = set()
    for (li, name, idx) in face_info:
        if name not in target_names:
            continue
        root = find(idx[0]) if idx and 0 <= idx[0] < n else None
        if root in island_roots_bad:
            lines_to_skip.add(li)

    out = []
    for li, line in enumerate(lines):
        if li in lines_to_skip:
            continue
        out.append(line)
    obj_path.write_text("".join(out), encoding="utf-8")
    return len(island_roots_bad)


def make_terrain_sampler(terrain_obj_path: Path):
    """Costruisce un sampler z(x,y) dal mesh terrain Blender.
    Ritorna None se il file non esiste / e' vuoto.
    Il sampler ritorna None se il punto e' fuori dal terrain.
    Utile per piazzare prop lontani dalla centerline (siepi, muretti,
    boulders) senza farli fluttuare su/affondare in terrain pendenti.
    """
    if not terrain_obj_path.exists() or terrain_obj_path.stat().st_size < 200:
        return None
    tverts: list[tuple[float, float, float]] = []
    tfaces: list[tuple[int, int, int]] = []
    with terrain_obj_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("v "):
                p = line.split()
                tverts.append((float(p[1]), float(p[2]), float(p[3])))
            elif line.startswith("f "):
                toks = line.split()[1:]
                idx = [int(t.split("/")[0]) - 1 for t in toks]
                for i in range(1, len(idx) - 1):
                    tfaces.append((idx[0], idx[i], idx[i + 1]))
    if not tfaces:
        return None
    GRID_CELL = 100.0
    grid: dict[tuple[int, int], list[int]] = {}
    face_bbox = []
    for fi, (a, b, c) in enumerate(tfaces):
        va, vb, vc = tverts[a], tverts[b], tverts[c]
        x_min = min(va[0], vb[0], vc[0])
        x_max = max(va[0], vb[0], vc[0])
        y_min = min(va[1], vb[1], vc[1])
        y_max = max(va[1], vb[1], vc[1])
        face_bbox.append((x_min, y_min, x_max, y_max))
        ix0 = int(x_min // GRID_CELL)
        ix1 = int(x_max // GRID_CELL)
        iy0 = int(y_min // GRID_CELL)
        iy1 = int(y_max // GRID_CELL)
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                grid.setdefault((ix, iy), []).append(fi)

    def sampler(x: float, y: float):
        ix = int(x // GRID_CELL)
        iy = int(y // GRID_CELL)
        best_z = None
        best_d = float("inf")
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for fi in grid.get((ix + di, iy + dj), []):
                    x0, y0, x1, y1 = face_bbox[fi]
                    if not (x0 <= x <= x1 and y0 <= y <= y1):
                        continue
                    a, b, c = tfaces[fi]
                    va, vb, vc = tverts[a], tverts[b], tverts[c]
                    denom = ((vb[1] - vc[1]) * (va[0] - vc[0])
                             + (vc[0] - vb[0]) * (va[1] - vc[1]))
                    if abs(denom) < 1e-9:
                        continue
                    l1 = ((vb[1] - vc[1]) * (x - vc[0])
                          + (vc[0] - vb[0]) * (y - vc[1])) / denom
                    l2 = ((vc[1] - va[1]) * (x - vc[0])
                          + (va[0] - vc[0]) * (y - vc[1])) / denom
                    l3 = 1.0 - l1 - l2
                    eps = -0.01
                    if l1 >= eps and l2 >= eps and l3 >= eps:
                        z = l1 * va[2] + l2 * vb[2] + l3 * vc[2]
                        return z
        for di in (-2, -1, 0, 1, 2):
            for dj in (-2, -1, 0, 1, 2):
                for fi in grid.get((ix + di, iy + dj), []):
                    for vi in tfaces[fi]:
                        v = tverts[vi]
                        d2 = (v[0] - x) ** 2 + (v[1] - y) ** 2
                        if d2 < best_d:
                            best_d = d2
                            best_z = v[2]
        return best_z

    return sampler


def drop_world_obj_to_terrain_mesh(world_obj_path: Path,
                                      terrain_obj_path: Path) -> int:
    """Drop-to-ground per-ISOLA del world OBJ sul mesh Terrain Blender.

    Il mesh Blender Terrain e' coarse (5k face, media 160m tra vertex),
    mentre gli alberi/edifici nel world mesh sono piazzati sul DEM fine.
    Gap -> alberi/edifici fluttuanti.

    Algoritmo:
    1. Parso terrain mesh in face con loro bbox XY e Z interpolation coeff.
    2. Spatial grid per nearest face lookup.
    3. Parso world mesh: estraggo isole (componenti connesse) via union-find
       sulle edges dei triangoli.
    4. Per ogni isola: base_z = min(z), centroide XY. Campiono terrain_z al
       centroide. delta = terrain_z - base_z. Shifto tutti i vertex della
       isola di delta.
    """
    # --- 1. Parse terrain mesh: lista face con vertex XY e Z ---
    tverts: list[tuple[float, float, float]] = []
    tfaces: list[tuple[int, int, int]] = []
    with terrain_obj_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("v "):
                p = line.split()
                tverts.append((float(p[1]), float(p[2]), float(p[3])))
            elif line.startswith("f "):
                toks = line.split()[1:]
                idx = [int(t.split("/")[0]) - 1 for t in toks]
                # triangoliarizza fan da idx[0]
                for i in range(1, len(idx) - 1):
                    tfaces.append((idx[0], idx[i], idx[i + 1]))
    if not tfaces:
        return 0

    # Spatial grid per face
    GRID_CELL = 100.0  # 100m per cell
    grid: dict[tuple[int, int], list[int]] = {}
    face_bbox = []
    for fi, (a, b, c) in enumerate(tfaces):
        va, vb, vc = tverts[a], tverts[b], tverts[c]
        x_min = min(va[0], vb[0], vc[0])
        x_max = max(va[0], vb[0], vc[0])
        y_min = min(va[1], vb[1], vc[1])
        y_max = max(va[1], vb[1], vc[1])
        face_bbox.append((x_min, y_min, x_max, y_max))
        ix0 = int(x_min // GRID_CELL)
        ix1 = int(x_max // GRID_CELL)
        iy0 = int(y_min // GRID_CELL)
        iy1 = int(y_max // GRID_CELL)
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                grid.setdefault((ix, iy), []).append(fi)

    def sample_terrain_z(x: float, y: float) -> float | None:
        """Ritorna z del terrain mesh al punto (x, y) via bary-interpolation
        nella face che lo contiene. None se fuori terrain."""
        ix = int(x // GRID_CELL)
        iy = int(y // GRID_CELL)
        best_z = None
        best_d = float("inf")
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for fi in grid.get((ix + di, iy + dj), []):
                    x0, y0, x1, y1 = face_bbox[fi]
                    if not (x0 <= x <= x1 and y0 <= y <= y1):
                        continue
                    a, b, c = tfaces[fi]
                    va, vb, vc = tverts[a], tverts[b], tverts[c]
                    # barycentric
                    denom = ((vb[1] - vc[1]) * (va[0] - vc[0])
                             + (vc[0] - vb[0]) * (va[1] - vc[1]))
                    if abs(denom) < 1e-9:
                        continue
                    l1 = ((vb[1] - vc[1]) * (x - vc[0])
                          + (vc[0] - vb[0]) * (y - vc[1])) / denom
                    l2 = ((vc[1] - va[1]) * (x - vc[0])
                          + (va[0] - vc[0]) * (y - vc[1])) / denom
                    l3 = 1.0 - l1 - l2
                    eps = -0.01
                    if l1 >= eps and l2 >= eps and l3 >= eps:
                        z = l1 * va[2] + l2 * vb[2] + l3 * vc[2]
                        return z
        # Fallback: nearest vertex
        for di in (-2, -1, 0, 1, 2):
            for dj in (-2, -1, 0, 1, 2):
                for fi in grid.get((ix + di, iy + dj), []):
                    for vi in tfaces[fi]:
                        v = tverts[vi]
                        d2 = (v[0] - x) ** 2 + (v[1] - y) ** 2
                        if d2 < best_d:
                            best_d = d2
                            best_z = v[2]
        return best_z

    # --- 2. Parse world mesh con vertex + face ---
    wverts: list[tuple[float, float, float]] = []
    wedges: list[tuple[int, int]] = []
    world_lines = world_obj_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    vi_offset = 0
    for line in world_lines:
        if line.startswith("v "):
            p = line.split()
            wverts.append((float(p[1]), float(p[2]), float(p[3])))
        elif line.startswith("f "):
            toks = line.split()[1:]
            idx = [int(t.split("/")[0]) - 1 for t in toks]
            for i in range(1, len(idx)):
                wedges.append((idx[0], idx[i]))

    # --- 3. Union-find isole ---
    n = len(wverts)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for (a, b) in wedges:
        if 0 <= a < n and 0 <= b < n:
            union(a, b)

    # Raggruppa per root
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    # --- 4. Drop per isola ---
    # Regole conservative:
    # - Oggetti ESTESI (extent > 30m) NON droppati: sono guardrail/walls/
    #   wires/powercrosses che seguono la topografia lungo la strada; il
    #   centroide non rappresenta bene la loro base.
    # - Shift SOLO verso il BASSO (delta <= 0): gli oggetti nel blend sono
    #   al DEM fine; il terrain mesh esportato e' al piu' uguale o piu'
    #   basso (carvato). Delta positivo = errore di sampling -> skip.
    # - |delta| <= 5m: cap conservativo per evitare salti enormi.
    shifts = [0.0] * n
    n_isles = 0
    n_shifted_isles = 0
    n_skip_extended = 0
    for root, vlist in groups.items():
        if len(vlist) < 3:
            continue
        n_isles += 1
        xs = [wverts[i][0] for i in vlist]
        ys = [wverts[i][1] for i in vlist]
        zs = [wverts[i][2] for i in vlist]
        x_range = max(xs) - min(xs)
        y_range = max(ys) - min(ys)
        if max(x_range, y_range) > 30.0:
            n_skip_extended += 1
            continue  # oggetto esteso, non droppare
        cx = sum(xs) / len(xs)
        cy = sum(ys) / len(ys)
        base_z = min(zs)
        tz = sample_terrain_z(cx, cy)
        if tz is None:
            continue
        delta = tz - base_z
        # Abbassamento normale + upshift moderato per edifici compatti che
        # sono sotto il terrain mesh coarse (es. base a 70m, terrain mesh
        # a 72m perche' il mesh ha vertici interpolati al ribasso).
        extent = max(x_range, y_range)
        if delta > -0.1:
            # delta positivo = terrain sopra base. Accetto solo per oggetti
            # compatti (edifici/alberi singoli) con delta piccolo.
            if extent < 15.0 and 0.1 < delta < 3.0:
                pass  # upshift OK
            else:
                continue
        elif delta < -15.0:
            continue  # downshift troppo grande, probabile errore sample
        for vi in vlist:
            shifts[vi] = delta
        n_shifted_isles += 1

    # --- 5. Riscrivi OBJ ---
    out_lines: list[str] = []
    vi = 0
    for line in world_lines:
        if line.startswith("v "):
            p = line.split()
            x, y, z = float(p[1]), float(p[2]), float(p[3])
            z += shifts[vi]
            out_lines.append(f"v {x:.6f} {y:.6f} {z:.6f}\n")
            vi += 1
        else:
            out_lines.append(line)
    world_obj_path.write_text("".join(out_lines), encoding="utf-8")
    print(f"  drop-to-ground: {n_shifted_isles}/{n_isles} isole spostate "
          f"({n_skip_extended} skip extese guardrail/wires/walls)")
    return n_shifted_isles


def strip_building_objects_from_world_obj(obj_path: Path) -> int:
    """Rimuove dal world OBJ TUTTI i blocchi `o <name>` che sono:
    (a) edifici procedurali Blender (Building/Walls/Roofs/Chimneys/ExtraBuildings)
    (b) alberi e cespugli procedurali Blender (TreeTrunks/TreeCanopies/
        RoadsideTrunks/RoadsideCanopies/CypressTrunks/CypressCanopies/Bushes)

    Sono tutti sostituiti da asset vanilla italy piazzati via Forest system
    (farmhouse/shed/ind_bld per edifici; scraggly/olive/cypress/holm_oak/
    maritime_pine/cork_oak/fluffy_bush/generibush/holm_oak_bush per vegetazione).

    Mantiene intatti: Guardrails, StoneWalls, Rocks, Signs, KmMarker,
    Delineators, PowerPoles, PowerCrosses, PowerWires, terrain.

    Ritorna: numero di blocchi rimossi.
    """
    REMOVE_KEYWORDS = (
        # edifici procedurali
        "Building", "Buildings_Walls", "Buildings_Roofs",
        "Chimney", "Chimneys", "ExtraBuildings", "Roof",
        # alberi/bushes procedurali Blender (duplicano Forest)
        "TreeTrunks", "TreeCanopies",
        "RoadsideTrunks", "RoadsideCanopies",
        "CypressTrunks", "CypressCanopies",
        "Bushes",
    )
    # Whitelist: se il nome contiene una keyword qui, NON rimuovere,
    # anche se matcha una keyword di rimozione.
    KEEP_EVEN_IF_MATCH = ("StoneWall", "stonewall", "RetainingWall",
                            "Guardrail", "Sign", "Marker", "Delineator",
                            "Pole", "Terrain", "terrain", "PowerCross",
                            "PowerWire", "PowerPole", "Rock")

    def is_removable(name: str) -> bool:
        kept = any(k in name for k in KEEP_EVEN_IF_MATCH)
        if kept:
            return False
        return any(k in name for k in REMOVE_KEYWORDS)

    out_lines: list[str] = []
    removed_blocks = 0
    removing = False
    with obj_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("o ") or line.startswith("g "):
                name = line.split(maxsplit=1)[1].strip()
                if is_removable(name):
                    removing = True
                    removed_blocks += 1
                    continue
                removing = False
                out_lines.append(line)
                continue
            if removing:
                # Skip f/s/usemtl/vt/vn lines of this block. Keep v lines?
                # Le 'v' sono vertex globali: rimuoverle invaliderebbe gli
                # indici delle altre face. Le teniamo (vertici orfani = OK).
                if line.startswith("v "):
                    out_lines.append(line)
                continue
            out_lines.append(line)

    obj_path.write_text("".join(out_lines), encoding="utf-8")
    return removed_blocks


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
    """Embankment bidirezionale attorno alla centerline: il terreno viene
    forzato a seguire la quota strada in una fascia stretta.

    Problema precedente: carve solo come CAP (terreno mai alzato, solo
    abbassato). Dove il DEM era piu' basso della road (tratti su rilevato
    stradale) il terreno restava 1-2m sotto la road -> "scogliera" visibile.

    Nuova logica: per ogni cella entro 96m da almeno un punto centerline,
    uso la centerline PIU' VICINA per quella cella e forzo:
      - dist <= 10m (dentro carreggiata + banchina): terrain = road_z - 0.30
      - 10 < dist <= 40m (scarpata dolce): blend lineare da road_z - 0.30
        verso natural_terrain, con clamp [road_z - 2, road_z + 0.5]
      - 40 < dist <= 96m (fuori carve): natural terrain invariato
    Cosi' la strada e' sempre 30 cm sopra il terreno adiacente, poi la
    scarpata si fonde dolcemente col DEM entro 40m.
    """
    import csv as _csv
    cl_path = ROOT / "output" / "centerline.csv"
    if not cl_path.exists():
        return 0
    H, W = arr.shape
    half = TER_EXTENT / 2.0
    cell = TER_SQUARESIZE

    # Leggo tutti i punti centerline
    pts_xy = []
    pts_z = []
    with cl_path.open(newline="", encoding="utf-8") as f:
        for row in _csv.DictReader(f):
            x = float(row["x"]); y = float(row["y"]); zb = float(row["z"])
            rz = zb if target_is_blender_z else zb + z_offset_blender
            pts_xy.append((x, y))
            pts_z.append(rz)
    if not pts_xy:
        return 0
    pts_xy_arr = np.array(pts_xy, dtype=np.float32)
    pts_z_arr = np.array(pts_z, dtype=np.float32)

    # Bounding box dei centerline per evitare di scannerizzare tutta la map
    xs = pts_xy_arr[:, 0]
    ys = pts_xy_arr[:, 1]
    R_m = 96.0
    x_min = xs.min() - R_m; x_max = xs.max() + R_m
    y_min = ys.min() - R_m; y_max = ys.max() + R_m
    c_lo = max(0, int((x_min + half) / cell))
    c_hi = min(W, int((x_max + half) / cell) + 1)
    r_lo = max(0, int((y_min + half) / cell))
    r_hi = min(H, int((y_max + half) / cell) + 1)
    if c_lo >= c_hi or r_lo >= r_hi:
        return 0

    # Per ogni cella nella bbox: trova centerline piu' vicino (+distanza)
    # via scan kernel per ogni centerline.
    R_cells = int(np.ceil(R_m / cell))
    nearest_d2 = np.full((H, W), np.inf, dtype=np.float32)
    nearest_rz = np.zeros((H, W), dtype=np.float32)

    for i, ((px, py), pz) in enumerate(zip(pts_xy, pts_z)):
        col = int((px + half) / cell)
        ry = int((py + half) / cell)
        r0 = max(0, ry - R_cells); r1 = min(H, ry + R_cells + 1)
        c0 = max(0, col - R_cells); c1 = min(W, col + R_cells + 1)
        if r0 >= r1 or c0 >= c1:
            continue
        rows_arr = (np.arange(r0, r1) + 0.5) * cell - half
        cols_arr = (np.arange(c0, c1) + 0.5) * cell - half
        dx = cols_arr[None, :] - px
        dy = rows_arr[:, None] - py
        d2 = (dx * dx + dy * dy).astype(np.float32)
        sub = nearest_d2[r0:r1, c0:c1]
        mask = d2 < sub
        nearest_d2[r0:r1, c0:c1] = np.where(mask, d2, sub)
        nearest_rz_sub = nearest_rz[r0:r1, c0:c1]
        nearest_rz[r0:r1, c0:c1] = np.where(mask, pz, nearest_rz_sub)

    # Converti arr a metri (blender_z)
    arr_m = arr.astype(np.float32) * (max_height / 65535.0)
    dist_m = np.sqrt(np.clip(nearest_d2, 0, None))

    # CALIBRAZIONE EMPIRICA dalla mesh Blender (misurata sul .dae):
    # road mesh top = centerline_csv_z + ~0.35m. E' l'effetto del Solidify
    # con offset=-1 che sposta il guscio 0.4m in +normal direction (quindi
    # il piano di guida finale e' ~35cm sopra la centerline CSV).
    ROAD_TOP_OFS = 0.35   # m, road_surface = centerline_z + 0.35
    # CLEARANCE abbassata da 0.80 a 0.25: utente chiede strada meno
    # sopraelevata, "attaccata al paesaggio" ma mai sotto. 25cm garantisce
    # che la superficie stradale sia SEMPRE 10cm sopra terreno
    # (road_top - terrain = ROAD_TOP_OFS - CLEARANCE + noise_quant = 0.35 -
    # 0.25 + ~ 0.02 = 0.12m) evitando che il terreno emerga dall'asfalto.
    CLEARANCE = 0.25

    # Quota target per il terreno: road_surface - clearance = centerline + 0.05
    target_flat = nearest_rz + ROAD_TOP_OFS - CLEARANCE

    # Fasce d'effetto:
    # - d <= 4m (carreggiata+banchina): forza terrain a target_flat
    # - 4 < d <= 30m: blend lineare a DEM naturale (scarpata dolce)
    # - 30 < d <= 80: solo cap soft road+2m (evita pareti alte)
    # - d > 80: invariato
    # Prima avevo D1=10m che creava una fascia piatta di 20m totali (10m per
    # lato) -> sembrava strada larghissima di 3 corsie.
    D1 = 6.0    # m: fascia piana (era 4m: margine piu' ampio per evitare
                # heightmap sopra strada in curve/vertex dove la celle
                # quantizzata puo' sforare la road mesh)
    D2 = 35.0   # m: fine blend
    D3 = 90.0   # m: fine carve leggera

    out = arr_m.copy()

    # fascia 1: force
    m1 = dist_m <= D1
    out = np.where(m1, target_flat, out)

    # fascia 2: blend
    m2 = (dist_m > D1) & (dist_m <= D2)
    t = np.clip((dist_m - D1) / (D2 - D1), 0.0, 1.0)
    blended = target_flat * (1 - t) + arr_m * t
    # soft clamp: terrain max = road_top (mai emergere sopra strada)
    # road_top = centerline + ROAD_TOP_OFS = nearest_rz + 0.35
    # - min road - 4 (scarpata giu')
    blended = np.clip(blended, nearest_rz - 4.0, nearest_rz + ROAD_TOP_OFS - 0.05)
    out = np.where(m2, blended, out)

    # fascia 3: solo cap soft (evita muri alti vicino scarpata)
    # Ridotto da +2.0 a +1.5 per coerenza col carve mesh Blender.
    m3 = (dist_m > D2) & (dist_m <= D3)
    cap_soft = nearest_rz + 1.5
    out = np.where(m3 & (arr_m > cap_soft), cap_soft, out)
    # fascia 4 (nuova): d > D3 -> cap duro road+8m per evitare pareti
    # altissime di terrain che invadono il primo piano.
    m4 = dist_m > D3
    cap_far = nearest_rz + 8.0
    out = np.where(m4 & (arr_m > cap_far), cap_far, out)

    # Converti back a u16
    out_u16 = np.clip(out / max_height * 65535.0, 0, 65535).astype(np.uint16)
    changed = int((out_u16 != arr).sum())
    arr[:] = out_u16
    return changed


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
                     asphalt_color_map: str | None = None,
                     terrain_color_map: str | None = None,
                     foliage_color_map: str | None = None,
                     bark_color_map: str | None = None,
                     asphalt_normal_map: str | None = None,
                     bark_normal_map: str | None = None,
                     stonewall_normal_map: str | None = None) -> None:
    # TerrainMaterial con:
    # - diffuseMap = texture satellite (colore macro su scala del tile 12288m)
    # - detailMap = texture erba/terriccio procedurale che ripete ogni 10m
    #   per avere grana close-range (altrimenti da vicino tutto uniforme).
    # Path SENZA leading "/" e CON estensione .png (il wiki dice dipende
    # dalla versione — BeamNG 0.38 vuole path con estensione in TerrainMat).
    # TerrainMaterial: formato classico (v1) come autotest.zip ufficiale.
    # DEVE avere diffuseMap + detailMap + macroMap + normalMap tutti settati
    # altrimenti il TerrainCellMaterial logga "missing texture" per ogni cella
    # e il terreno rimane nero. Verifica: levels/autotest/art/terrain/main.
    # materials.json — la sua Asphalt TerrainMaterial ha tutti e quattro.
    # Path senza extension: BeamNG engine prova .dds, .png, .jpg in ordine
    # (nostri PNG terminano .color.png, quindi riferimento base + ".color").
    base_sat = f"levels/{LEVEL_NAME}/art/terrains/satellite_diffuse.color"
    base_detail = f"levels/{LEVEL_NAME}/art/terrains/detail_grass.color"
    base_macro = f"levels/{LEVEL_NAME}/art/terrains/macro_grass.color"
    base_nrm = f"levels/{LEVEL_NAME}/art/terrains/detail_grass_nrm.color"
    terrain_mat_dir = level_dir / "art" / "terrain"
    terrain_mat_dir.mkdir(parents=True, exist_ok=True)
    terrain_materials = {
        f"{TERRAIN_MATERIAL_NAME}-{TERRAIN_MATERIAL_UUID}": {
            "name": f"{TERRAIN_MATERIAL_NAME}-{TERRAIN_MATERIAL_UUID}",
            "internalName": TERRAIN_MATERIAL_NAME,
            "class": "TerrainMaterial",
            "persistentId": TERRAIN_MATERIAL_UUID,
            # Base diffuse = satellite full-map (1 tile = 12288m)
            "diffuseMap": base_sat,
            "diffuseSize": 12288,
            # Detail = grana fine close-range. Strength basso (0.28) cosi' la
            # satellite (con le sue patch verdi/gialle/marroni) resta visibile
            # al posto di un verde grass uniforme che copriva tutto.
            "detailMap": base_detail,
            "detailSize": 6,
            "detailStrength": 0.20,
            "detailDistance": 50,
            # Macro = variazione scala media. Strength MODERATO: troppo alto
            # e si vedeva un verde-neon uniforme perche' macro_grass era
            # verde saturo. Ora macro_grass ha palette hay/soil/green mista,
            # strength piu' basso lascia passare il satellite (che ha gia'
            # la varianza di colore corretta).
            "macroMap": base_macro,
            "macroSize": 280,
            "macroStrength": 0.35,
            "macroDistance": 1200,
            # Normal = piccola perturbazione superficie
            "normalMap": base_nrm,
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
        ("AsphaltWear", [a_r * 0.78, a_g * 0.78, a_b * 0.78]),
        ("Shoulder", shoulder_rgb),
        ("LineWhite", [0.93, 0.93, 0.92]),
        ("LineYellow", [0.92, 0.80, 0.18]),
        ("Manhole", [0.14, 0.13, 0.12]),
        # WORLD (dal blend: Building, Guardrail, Pole, Roof, Sign, StoneWall,
        # TreeCanopy, TreeTrunk)
        # NB: tutti i colori dei materiali senza texture sono "diffuseColor" e
        # vengono moltiplicati per l'illuminazione. Con il sole forte di
        # BeamNG (exposure=13) i beige chiari diventano quasi bianchi -> tengo
        # i valori piu' bassi per non perdere la tinta.
        ("Building", [0.58, 0.52, 0.42]),    # beige italiano smorzato
        ("Roof", [0.48, 0.22, 0.15]),         # terracotta sgranata
        ("StoneWall", [0.42, 0.38, 0.32]),    # pietra grigio-beige scura
        ("TreeCanopy", [0.20, 0.35, 0.16]),   # verde foglia scuro
        ("TreeTrunk", [0.32, 0.22, 0.14]),    # corteccia marrone
        ("Guardrail", [0.72, 0.74, 0.78]),    # metallo chiaro
        ("Pole", [0.55, 0.55, 0.55]),         # metallo scuro
        ("Sign", [0.92, 0.92, 0.92]),         # bianco cartello
        # Terrain mesh Blender: se colorMap non carica, fallback verde vivo
        # appenninico (prato estivo, non beige-verde tipo ESRI zoom)
        ("SatelliteTerrain", [0.35, 0.52, 0.25]),
        ("Terrain", [0.35, 0.52, 0.25]),
        ("TerrainMat", [0.35, 0.52, 0.25]),
        ("Ground", [0.35, 0.52, 0.25]),
        # Roadside procedural clutter
        ("Rock", [0.55, 0.52, 0.46]),
        ("BushGreen", [0.26, 0.40, 0.20]),
        ("Parapet", [0.62, 0.58, 0.52]),       # cemento parapetti ponte
        ("BollardMat", [0.82, 0.82, 0.80]),    # paletto bianco-grigio
        ("CableWire", [0.08, 0.08, 0.08]),     # cavo elettrico scuro
        ("TreeBark", [0.30, 0.21, 0.13]),      # corteccia marrone
        ("TreeFoliage", [0.18, 0.32, 0.14]),   # chioma verde scuro
        ("Embankment", [0.40, 0.45, 0.28]),    # scarpata verde-terriccio
        ("TreeBillboard", [0.90, 0.90, 0.90]), # billboard PNG+alpha (legacy)
        # 4 varianti palette tinta usate da generate_vegetation per variazione
        ("TreeBillboard_P0", [0.95, 1.00, 0.92]),  # caldo
        ("TreeBillboard_P1", [0.82, 0.92, 0.80]),  # verde scuro
        ("TreeBillboard_P2", [1.05, 1.08, 0.95]),  # verde vivo
        ("TreeBillboard_P3", [0.92, 1.02, 1.00]),  # verde umido
        # Road details (generate_road_details)
        ("AsphaltPatchDarkNew", [0.06, 0.06, 0.07]),  # bitume nuovo nero
        ("AsphaltPatchLightNew", [0.35, 0.34, 0.32]),  # asfalto scolorito
        ("ChevronPole", [0.85, 0.85, 0.85]),           # palo chevron bianco
        ("ChevronSign", [0.95, 0.82, 0.10]),           # cartello giallo
        # Landmark signs: VALICO / SS17 / direzionale / edicola
        ("SignValico", [1.0, 1.0, 1.0]),     # texture sign_valico.color.png
        ("SignSS17", [1.0, 1.0, 1.0]),       # texture sign_ss17.color.png
        ("SignDirezionale", [1.0, 1.0, 1.0]),  # sign_direzionale.color.png
        ("SignPole", [0.42, 0.42, 0.44]),    # palo metallico scuro
        ("EdicolaStone", [0.52, 0.48, 0.42]),  # stele pietra beige
        # Video landmarks: cartelli esatti + edifici iconici + balle + delineatori
        ("VidSignLimit50", [1.0, 1.0, 1.0]),
        ("VidSignLimit30", [1.0, 1.0, 1.0]),
        ("VidSignWinter", [1.0, 1.0, 1.0]),
        ("VidSignCurveSx", [1.0, 1.0, 1.0]),
        ("VidSignPole", [0.42, 0.42, 0.44]),
        ("VidBldgRudere", [1.0, 1.0, 1.0]),
        ("VidBldgTorretta", [1.0, 1.0, 1.0]),
        ("VidBldgCasale", [1.0, 1.0, 1.0]),
        ("VidHay", [0.78, 0.68, 0.42]),       # paglia giallo-marrone
        ("VidDelineator", [0.92, 0.92, 0.88]),  # palo bianco-rosso (fallback)
        # Fallback generici
        ("default", [0.55, 0.55, 0.55]),
        ("DefaultMat", [0.55, 0.55, 0.55]),
    ]
    import hashlib as _h
    mats = {}
    for name, rgb in entries:
        # BeamNG 0.38: Material classic. ATTENZIONE: BeamNG MOLTIPLICA
        # diffuseColor * colorMap texel. Se metti diffuseColor [0.14,0.14,0.14]
        # su un asfalto PNG mean ~0.15, il render finale e' 0.02 (NERO PECE).
        # Per questo: se c'e' colorMap, diffuseColor diventa [1,1,1,1] (identita'
        # moltiplicativa). Il rgb semantico resta come fallback solo quando la
        # texture NON e' impostata.
        stage0 = {
            "diffuseColor": [*rgb, 1.0],
            "specularPower": 1,
            "useAnisotropic": True,
        }
        is_billboard = (name == "TreeBillboard" or name.startswith("TreeBillboard_"))
        has_color_map = False
        if is_billboard:
            # Format v1.5 (PBR shader): baseColorMap + opacityMap SEPARATI.
            # Critico: lo shader V1.5 campiona opacityMap.r (canale rosso),
            # NON il canale alpha. Quindi:
            # - baseColorMap = RGB della foglia (ignorata alpha)
            # - opacityMap = grayscale separato dove R=alpha originale
            # Fonte: shaders/common/material/shadergen/defaultMat.hlsl:
            #   opacityMapSample = sampleMaterialTex(...).x
            # e levels/italy/art/shapes/trees/trees_italy/main.materials.json
            # usa t_cork_oak_o.data.png (grayscale separato).
            base_tb = f"levels/{LEVEL_NAME}/art/nature/tree_billboard.color.png"
            opac_tb = f"levels/{LEVEL_NAME}/art/nature/tree_billboard_opacity.data.png"
            stage0["baseColorMap"] = base_tb
            stage0["opacityMap"] = opac_tb
            has_color_map = True
        if asphalt_color_map and name in ("Asphalt", "AsphaltPatch_Dark",
                                             "AsphaltPatch_Light",
                                             "AsphaltWear"):
            stage0["colorMap"] = asphalt_color_map
            has_color_map = True
        if terrain_color_map and name in ("SatelliteTerrain", "Terrain",
                                             "TerrainMat", "Ground"):
            stage0["colorMap"] = terrain_color_map
            has_color_map = True
        if foliage_color_map and name in ("TreeCanopy", "TreeFoliage",
                                             "Canopy"):
            stage0["colorMap"] = foliage_color_map
            has_color_map = True
        if bark_color_map and name in ("TreeTrunk", "TreeBark"):
            stage0["colorMap"] = bark_color_map
            has_color_map = True
        # Landmark signs texture mapping
        _signs_tex = {
            "SignValico": "sign_valico",
            "SignSS17": "sign_ss17",
            "SignDirezionale": "sign_direzionale",
        }
        if name in _signs_tex:
            stage0["colorMap"] = (f"levels/{LEVEL_NAME}/art/shapes/signs/"
                                  f"{_signs_tex[name]}.color.png")
            has_color_map = True
        # Video landmarks texture mapping (cartelli + edifici)
        _vid_tex = {
            "VidSignLimit50": "sign_limit50",
            "VidSignLimit30": "sign_limit30",
            "VidSignWinter": "sign_winter_tires",
            "VidSignCurveSx": "sign_curve_left",
            "VidBldgRudere": "bldg_rudere",
            "VidBldgTorretta": "bldg_torretta",
            "VidBldgCasale": "bldg_casale",
        }
        if name in _vid_tex:
            stage0["colorMap"] = (f"levels/{LEVEL_NAME}/art/shapes/"
                                  f"video_landmarks/"
                                  f"{_vid_tex[name]}.color.png")
            has_color_map = True
        # FIX NERO: quando c'e' un colorMap, il diffuseColor DEVE essere
        # neutro [1,1,1,1] altrimenti BeamNG moltiplica e incupisce.
        if has_color_map:
            stage0["diffuseColor"] = [1.0, 1.0, 1.0, 1.0]
        # Billboard v1.5: strip V0 fields perche' la loro presenza fa il
        # loader materials fallback a V0 shader (dove opacityMap non e'
        # onorata come in italy vanilla). V1.5 Stage deve contenere SOLO
        # i campi nuovi: baseColorMap, opacityMap, baseColorFactor, etc.
        if is_billboard:
            stage0.pop("diffuseColor", None)
            stage0.pop("specularPower", None)
            stage0.pop("useAnisotropic", None)
            stage0["baseColorFactor"] = [rgb[0], rgb[1], rgb[2], 1.0]
        _ = (asphalt_normal_map, bark_normal_map, stonewall_normal_map)
        pid = _h.md5(name.encode()).hexdigest()
        pid = (f"{pid[0:8]}-{pid[8:12]}-{pid[12:16]}-"
               f"{pid[16:20]}-{pid[20:32]}")
        mat_entry = {
            "name": name,
            "mapTo": name,
            "class": "Material",
            "persistentId": pid,
            "Stages": [stage0, {}, {}, {}],
            "materialTag0": "beamng",
            "materialTag1": "environment",
        }
        # Billboard: alpha test via PBR shader V1.5 (stesso setup italy vanilla).
        # KEY: "version": 1.5 -> usa shader V1 (defaultMat.hlsl) che supporta
        # alpha test correttamente via opacityMap separata. V0 non discardava
        # pixel alpha<ref -> billboard appariva come rettangoli solidi.
        if is_billboard:
            mat_entry["version"] = 1.5
            mat_entry["alphaTest"] = True
            mat_entry["alphaRef"] = 68
            mat_entry["doubleSided"] = True
            mat_entry["materialTag1"] = "vegetation"
            mat_entry["annotation"] = "NATURE"
        else:
            mat_entry["translucentBlendOp"] = "None"
        # Cartelli stradali e facciate edifici: usare shader V1.5 (PBR).
        # V0 default in alcune build BeamNG rende il panel NERO quando
        # la texture ha estensione .color.png -> .color.dds non inclusa
        # nei path recognized. Con V1.5 + baseColorMap la lookup e' esplicita
        # e il materialTag "structures" evita fallback a shader terrain.
        _sign_or_bldg = (name in ("SignValico", "SignSS17", "SignDirezionale",
                                       "VidSignLimit50", "VidSignLimit30",
                                       "VidSignWinter", "VidSignCurveSx",
                                       "VidBldgRudere", "VidBldgTorretta",
                                       "VidBldgCasale"))
        if _sign_or_bldg:
            mat_entry["version"] = 1.5
            mat_entry["alphaTest"] = False
            mat_entry["doubleSided"] = True
            mat_entry["materialTag1"] = "structures"
            # Con V1.5 il campo key e' baseColorMap non colorMap
            if "colorMap" in stage0:
                stage0["baseColorMap"] = stage0.pop("colorMap")
            stage0["baseColorFactor"] = [1.0, 1.0, 1.0, 1.0]
            stage0.pop("diffuseColor", None)
        mats[name] = mat_entry
    # BeamNG 0.38 scansiona TUTTI i main.materials.json del mod. Scriviamo
    # in entrambi i path (parent level + art/shapes/) per garantire che
    # venga letto in ogni caso (BeamNG fa merge dei material trovati).
    shapes_mats_dir = level_dir / "art" / "shapes"
    shapes_mats_dir.mkdir(parents=True, exist_ok=True)
    json_str = json.dumps(mats, indent=2)
    (shapes_mats_dir / "main.materials.json").write_text(json_str, encoding="utf-8")
    (level_dir / "main.materials.json").write_text(json_str, encoding="utf-8")
    print(f"materials scritti: 1 TerrainMaterial + {len(mats)} Material "
          f"(levels/ + art/shapes/)")


# ---------------------------------------------------------------------------
# Step 5: copia satellite texture nel mod
# ---------------------------------------------------------------------------
def _fbm_noise(size: int, octaves: int, seed: int,
                 start_freq: int = 8) -> np.ndarray:
    """Fractal Brownian Motion noise: somma di ottave di Perlin-like noise.
    Risultato continuo, organic, senza pattern riconoscibili quando tiled."""
    g = np.random.default_rng(seed)
    out = np.zeros((size, size), np.float32)
    amp = 1.0
    freq = start_freq
    for _ in range(octaves):
        base = g.normal(0, 1, (freq, freq)).astype(np.float32)
        layer_img = Image.fromarray(base, mode="F").resize(
            (size, size), Image.BICUBIC
        )
        out += np.array(layer_img, dtype=np.float32) * amp
        amp *= 0.5
        freq *= 2
    out = (out - out.mean()) / (out.std() + 1e-6)
    return out


def generate_asphalt_texture(level_dir: Path,
                               base_rgb: tuple[float, float, float]) -> str:
    """Asfalto 1024x1024 realistico basato su frame GoPro SS17 reali.

    Riferimenti reali (video GX010576/577):
    - Colore base: grigio medio cool ~RGB(125,128,132) = 0.49,0.50,0.52
      (NON nero-asfalto scuro: quella e' l'idea sbagliata di chi vede
      catrame fresco; l'asfalto invecchiato sotto sole e' chiaro)
    - Aggregato fine "salt-and-pepper": bitumen scuro + pietrisco chiaro
      visibile a pochi metri, pattern ad alta frequenza
    - Variazione tonale macro sottile (fBm low-freq, range ~+/-5%)
    - Tracce ruote longitudinali: due bande orizzontali leggermente piu'
      scure (UV u=lungo strada, v=0..1 attraverso corsia). Posizione:
      v in [0.30, 0.40] e [0.60, 0.70].
    - Crepa longitudinale centrale (caratteristica italiana): linea scura
      sottile a v~0.50, giunto di pavimentazione
    - Micro-crepe casuali sporadiche, non cracks drammatici
    """
    size = 1024
    rng = np.random.default_rng(42)

    # ---- AGGREGATO SALT-AND-PEPPER (grana fine 3-8mm) ----
    # Ad alta frequenza: ogni pixel = una "pietra" del pietrisco.
    # deviazione maggiore del vecchio (0.055 -> 0.075) per rendere
    # visibile il pattern.
    fine_grit = rng.normal(0.0, 0.075, (size, size)).astype(np.float32)

    # ---- PIETRISCO CHIARO RARO (granelli chiari pronunciati) ----
    spk = rng.random((size, size), dtype=np.float32)
    # ~0.5% pixels molto chiari (pietrisco calcareo exposto)
    very_light_grit = np.where(spk < 0.005, 0.22, 0.0)
    # ~1% pixels medio-chiari
    light_grit = np.where((spk >= 0.005) & (spk < 0.015), 0.10, 0.0)
    # ~0.3% pixels bitumen nero
    dark_grit = np.where(spk > 0.997, -0.15, 0.0)

    # ---- VARIAZIONE TONALE MACRO (fBm high-freq, quasi-grain) ----
    # Frequenza alta (12): rompe tiling senza creare cloud visibili.
    # Emulazione di variazioni micro-locali nell'aggregato / bitumen.
    tone_macro = _fbm_noise(size, octaves=3, seed=42, start_freq=12)
    tone_macro = (tone_macro - 0.5) * 0.028  # +/- 1.4% tonale

    # ---- TRACCE RUOTE LONGITUDINALI (bande orizzontali) ----
    # UV layout road mesh: u=lungo strada, v=0..1 attraverso corsia.
    # Quindi nella TEXTURE: asse x (orizzontale) = travel, asse y =
    # larghezza. Wheel tracks = bande orizzontali centrate a y=0.35 e
    # y=0.65. Darker by -0.03, larghezza ~15% con soft falloff.
    yy = np.linspace(0.0, 1.0, size, dtype=np.float32)[:, None]
    track1 = np.exp(-((yy - 0.35) ** 2) / (2 * 0.055 ** 2))
    track2 = np.exp(-((yy - 0.65) ** 2) / (2 * 0.055 ** 2))
    wheel_tracks = -(track1 + track2) * 0.055  # sottile, -5.5% al centro
    # broadcast a 2D
    wheel_tracks = np.broadcast_to(wheel_tracks, (size, size)).copy()
    # Aggiungi micro-noise ai bordi traccia per non-uniformita'
    track_noise = _fbm_noise(size, octaves=3, seed=71, start_freq=6)
    wheel_tracks = wheel_tracks * (0.7 + (track_noise - 0.5) * 0.6)

    # ---- CREPA LONGITUDINALE CENTRALE (giunto) ----
    # Linea scura sottile a y~0.50, wobble laterale per naturale.
    crack_center = np.zeros((size, size), dtype=np.float32)
    wobble = _fbm_noise(size, octaves=3, seed=11, start_freq=4)
    for x in range(size):
        y_base = int(0.5 * size)
        # wobble +/- 6 px
        off = int((wobble[0, x] - 0.5) * 12)
        y = y_base + off
        if 0 <= y < size:
            crack_center[y, x] = -0.12
            if y + 1 < size:
                crack_center[y + 1, x] = -0.06
            if y - 1 >= 0:
                crack_center[y - 1, x] = -0.06

    # ---- MICRO-CREPE CASUALI (sporadiche, angolate, sottili) ----
    cracks_micro = np.zeros((size, size), dtype=np.float32)
    for _ in range(12):
        cx = int(rng.integers(0, size))
        cy = int(rng.integers(0, size))
        length = int(rng.integers(15, 60))
        # angolo bias sul longitudinale (travel direction) con jitter
        angle = rng.choice([0.0, math.pi]) + rng.normal(0, 0.5)
        dx = math.cos(angle)
        dy = math.sin(angle)
        for t in range(length):
            xx = int(cx + dx * t + rng.normal(0, 1.2))
            yy_px = int(cy + dy * t + rng.normal(0, 1.2))
            if 0 <= xx < size and 0 <= yy_px < size:
                cracks_micro[yy_px, xx] = -0.04

    # ---- PATCH RATTOPPO SCURO (DISATTIVATO per SS17) ----
    # Le immagini GoPro SS17 mostrano asfalto in condizioni decenti,
    # NESSUN rattoppo evidente. Rattoppi dark creavano macchie tipo
    # ombra irrealistica. Set a zero ma lascio infra per futuro tuning.
    patch = np.zeros((size, size), dtype=np.float32)

    # ---- COMPOSIZIONE ----
    delta = (
        fine_grit
        + very_light_grit
        + light_grit
        + dark_grit
        + tone_macro
        + wheel_tracks
        + crack_center
        + cracks_micro
        + patch
    )

    r, g, b = base_rgb
    # Clip range allargato: 0.20-0.80 (era 0.04-0.55 = troppo scuro).
    R = np.clip(r + delta, 0.20, 0.80)
    G = np.clip(g + delta, 0.20, 0.80)
    B = np.clip(b + delta * 1.02, 0.22, 0.82)  # leggera cool bias
    img = np.stack([R, G, B], axis=-1)

    # ---- SEGNALETICA ORIZZONTALE (dal video SS17) ----
    # UV road mesh: x (buffer) = travel (u), y (buffer) = width (v).
    # Linee laterali bianche CONTINUE a v~0.02 e v~0.98.
    # Linea centrale bianca TRATTEGGIATA a v~0.50 (2 cicli dash+gap
    # nella tile per robustezza a tile_len vario).
    # Bianco usurato ~ RGB (240, 238, 232) — non bianco puro (asfalto
    # invecchiato, polvere).
    white_r, white_g, white_b = 0.94, 0.93, 0.91
    lat_w = max(3, int(0.011 * size))   # ~1.1% larghezza (~12px @ 1024)
    cent_w = max(3, int(0.009 * size))  # ~0.9% larghezza (~9px @ 1024)
    mask = np.zeros((size, size), dtype=np.float32)
    # Laterale SX (y piccolo, v~0.02)
    y0 = int(0.018 * size); y1 = y0 + lat_w
    mask[y0:y1, :] = 1.0
    # Laterale DX (y grande, v~0.98)
    y0 = int(0.980 * size) - lat_w; y1 = y0 + lat_w
    mask[y0:y1, :] = 1.0
    # Centrale tratteggiata: 2 cicli dash(25%)+gap(25%)+dash(25%)+gap(25%)
    y_mid = int(0.500 * size)
    y0 = y_mid - cent_w // 2; y1 = y0 + cent_w
    for (u_a, u_b) in [(0.03, 0.23), (0.53, 0.73)]:
        x0 = int(u_a * size); x1 = int(u_b * size)
        mask[y0:y1, x0:x1] = 1.0
    # Smorzamento bordi (antialias 1-pixel) per evitare step duri
    # Semplice: dilato il mask di 1 pixel con valore 0.5
    mask_soft = mask.copy()
    mask_soft[1:, :] = np.maximum(mask_soft[1:, :], mask[:-1, :] * 0.5)
    mask_soft[:-1, :] = np.maximum(mask_soft[:-1, :], mask[1:, :] * 0.5)
    mask_soft[:, 1:] = np.maximum(mask_soft[:, 1:], mask[:, :-1] * 0.5)
    mask_soft[:, :-1] = np.maximum(mask_soft[:, :-1], mask[:, 1:] * 0.5)
    mask = np.minimum(mask_soft, 1.0)
    # Jitter tonale sulla linea bianca (usurato, non piatto)
    line_noise = rng.normal(0.0, 0.025, (size, size)).astype(np.float32)
    wR = np.clip(white_r + line_noise, 0.80, 1.00)
    wG = np.clip(white_g + line_noise, 0.80, 0.99)
    wB = np.clip(white_b + line_noise * 0.9, 0.78, 0.97)
    m = mask[..., None]
    img = img * (1.0 - m) + np.stack([wR, wG, wB], axis=-1) * m
    img_u8 = (img * 255.0).astype(np.uint8)

    tex_dir = level_dir / "art" / "road"
    tex_dir.mkdir(parents=True, exist_ok=True)
    out = tex_dir / "asphalt_base.png"
    Image.fromarray(img_u8).save(out, optimize=True)
    rel = f"levels/{LEVEL_NAME}/art/road/asphalt_base.png"
    # Stats per debug
    mean_rgb = img_u8.reshape(-1, 3).mean(axis=0).astype(int).tolist()
    print(f"Asfalto texture {size}x{size} (GoPro-ref): "
          f"{out.relative_to(MOD_DIR)}  mean RGB={mean_rgb}")
    return rel


def generate_terrain_grass_texture(level_dir: Path) -> str:
    """Texture erba 1024x1024 per il material Terrain (mesh Blender):
    base verde variata + chiazze terriccio + fiori gialli/bianchi rari.
    Applicata come colorMap al material Terrain sul mesh Blender carvato."""
    size = 1024
    rng = np.random.default_rng(17)

    # Base verde variata (fBm multi-scala, colore dominante)
    green_var = _fbm_noise(size, octaves=4, seed=17, start_freq=4)
    brown_var = _fbm_noise(size, octaves=3, seed=23, start_freq=3)

    # Macchie di terriccio/terra nuda (low-freq)
    soil_mask = _fbm_noise(size, octaves=3, seed=31, start_freq=5)
    soil_factor = np.clip((soil_mask - 0.5) * 0.8, 0.0, 0.4)  # 0..0.4

    # Fiori gialli sparsi (rari, spot)
    spk = rng.random((size, size), dtype=np.float32)
    yellow_flowers = spk > 0.996
    white_flowers = spk < 0.002

    # Base colore: verde erba naturale + variazione
    R = 0.32 + green_var * 0.08 + brown_var * 0.04 + soil_factor * 0.25
    G = 0.42 + green_var * 0.10 - soil_factor * 0.10
    B = 0.22 + green_var * 0.04 + brown_var * 0.02 - soil_factor * 0.05

    # Sovrapponi fiori
    R = np.where(yellow_flowers, 0.92, R)
    G = np.where(yellow_flowers, 0.80, G)
    B = np.where(yellow_flowers, 0.15, B)
    R = np.where(white_flowers, 0.95, R)
    G = np.where(white_flowers, 0.93, G)
    B = np.where(white_flowers, 0.88, B)

    R = np.clip(R, 0.10, 0.95)
    G = np.clip(G, 0.15, 0.95)
    B = np.clip(B, 0.08, 0.90)
    img = np.stack([R, G, B], axis=-1)
    img_u8 = (img * 255.0).astype(np.uint8)

    tex_dir = level_dir / "art" / "terrains"
    tex_dir.mkdir(parents=True, exist_ok=True)
    out = tex_dir / "terrain_grass.png"
    Image.fromarray(img_u8).save(out, optimize=True)
    rel = f"levels/{LEVEL_NAME}/art/terrains/terrain_grass.png"
    print(f"Terrain grass texture {size}x{size}: {out.relative_to(MOD_DIR)}")
    return rel


def generate_foliage_texture(level_dir: Path) -> str:
    """Texture foliage 512x512 procedurale: verde scuro base + cluster di
    foglie chiare (luce filtrata) + cluster scuri (ombre interne chioma).
    Tile senza pattern grazie a fBm multi-scala."""
    size = 512
    rng = np.random.default_rng(13)
    # Base scura organica
    base = _fbm_noise(size, octaves=4, seed=13, start_freq=4) * 0.12
    # Pattern foglie: puntini chiari densi (simula luce tra foglie)
    light_leaves = rng.random((size, size), dtype=np.float32)
    light_mask = np.where(light_leaves > 0.85, (light_leaves - 0.85) / 0.15, 0.0)
    # Puntini scuri (ombre tra cluster)
    dark_mask = np.where(light_leaves < 0.08, (0.08 - light_leaves) / 0.08, 0.0)
    # Composizione
    delta = base
    R = np.clip(0.18 + delta + light_mask * 0.25 - dark_mask * 0.10, 0.04, 0.80)
    G = np.clip(0.35 + delta + light_mask * 0.35 - dark_mask * 0.15, 0.08, 0.90)
    B = np.clip(0.14 + delta + light_mask * 0.20 - dark_mask * 0.08, 0.03, 0.60)
    img = np.stack([R, G, B], axis=-1)
    img_u8 = (img * 255.0).astype(np.uint8)
    tex_dir = level_dir / "art" / "nature"
    tex_dir.mkdir(parents=True, exist_ok=True)
    out = tex_dir / "foliage.png"
    Image.fromarray(img_u8).save(out, optimize=True)
    print(f"Foliage texture {size}x{size}: {out.relative_to(MOD_DIR)}")
    return f"levels/{LEVEL_NAME}/art/nature/foliage.png"


def generate_bark_texture(level_dir: Path) -> str:
    """Texture corteccia 256x256: marrone con striature verticali."""
    size = 256
    rng = np.random.default_rng(29)
    # Striature verticali (fBm orizz freq bassa, vert freq alta)
    stripe_noise = _fbm_noise(size, octaves=3, seed=29, start_freq=2)
    # Noise fine per dettagli
    fine = rng.normal(0, 0.05, (size, size)).astype(np.float32)
    delta = stripe_noise * 0.15 + fine
    R = np.clip(0.30 + delta, 0.15, 0.55)
    G = np.clip(0.21 + delta * 0.8, 0.10, 0.40)
    B = np.clip(0.13 + delta * 0.5, 0.05, 0.28)
    img = np.stack([R, G, B], axis=-1)
    img_u8 = (img * 255.0).astype(np.uint8)
    tex_dir = level_dir / "art" / "nature"
    tex_dir.mkdir(parents=True, exist_ok=True)
    out = tex_dir / "bark.png"
    Image.fromarray(img_u8).save(out, optimize=True)
    return f"levels/{LEVEL_NAME}/art/nature/bark.png"


def _encode_dxt1(rgb: np.ndarray) -> bytes:
    """DXT1 block compression (no alpha). rgb: HxWx3 uint8, H/W multipli di 4.
    BeamNG 0.38 DDS reader accetta SOLO DXT1/DXT5 compressed; uncompressed
    e' rifiutato con 'only RGB formats are supported'. Questo encoder e'
    veloce (vectorized numpy) e semplice: ogni block 4x4 usa min/max del
    bounding box come endpoints (qualita' accettabile per texture game)."""
    H, W = rgb.shape[:2]
    assert H % 4 == 0 and W % 4 == 0
    # Reshape in blocks: (bH, 4, bW, 4, 3) -> (bH*bW, 16, 3)
    bH, bW = H // 4, W // 4
    blocks = rgb.reshape(bH, 4, bW, 4, 3).transpose(0, 2, 1, 3, 4).reshape(bH * bW, 16, 3)
    blocks_i = blocks.astype(np.int32)

    # Endpoints: min and max colors per block
    c_max = blocks_i.max(axis=1)  # (N, 3)
    c_min = blocks_i.min(axis=1)  # (N, 3)

    # Convert endpoints to RGB565
    def to_565(c):
        return (((c[:, 0] >> 3) & 0x1F) << 11) | (((c[:, 1] >> 2) & 0x3F) << 5) | ((c[:, 2] >> 3) & 0x1F)

    c0_565 = to_565(c_max)
    c1_565 = to_565(c_min)
    # Force c0 > c1 for 4-color mode (no 1-bit alpha)
    swap = c0_565 <= c1_565
    # Bump c0 by 1 in 565 where they're equal (uniform block)
    equal = c0_565 == c1_565
    c0_565 = np.where(equal, np.minimum(c0_565 + 1, 0xFFFF), c0_565)
    swap = c0_565 < c1_565
    # If after bump c0 still <= c1, swap (shouldn't happen)
    tmp = np.where(swap, c0_565, 0)
    c0_565 = np.where(swap, c1_565, c0_565)
    c1_565 = np.where(swap, tmp, c1_565)

    # Decode 565 back to 8-bit RGB for palette
    def from_565(v):
        r = ((v >> 11) & 0x1F); r = (r << 3) | (r >> 2)
        g = ((v >> 5) & 0x3F); g = (g << 2) | (g >> 4)
        b = (v & 0x1F); b = (b << 3) | (b >> 2)
        return np.stack([r, g, b], axis=-1).astype(np.int32)

    col0 = from_565(c0_565)  # (N, 3)
    col1 = from_565(c1_565)  # (N, 3)
    col2 = (2 * col0 + col1) // 3
    col3 = (col0 + 2 * col1) // 3
    # Palette: (N, 4, 3)
    palette = np.stack([col0, col1, col2, col3], axis=1)

    # For each pixel in block, find nearest palette index
    # blocks_i: (N, 16, 3), palette: (N, 4, 3)
    # diffs: (N, 16, 4)
    diffs = np.linalg.norm(
        blocks_i[:, :, None, :] - palette[:, None, :, :], axis=3
    )
    indices = np.argmin(diffs, axis=2).astype(np.uint32)  # (N, 16)

    # DXT1 pixel order: row-major within block (0..3 row0, 4..7 row1, etc).
    # Pack 16 indices (2-bit each) into uint32 little-endian
    bits = np.zeros(len(blocks), dtype=np.uint32)
    for i in range(16):
        bits |= (indices[:, i] & 0x3) << (2 * i)

    # Pack output: uint16 c0, uint16 c1, uint32 bits = 8 bytes per block
    out = np.zeros((len(blocks), 8), dtype=np.uint8)
    out[:, 0] = c0_565 & 0xFF
    out[:, 1] = (c0_565 >> 8) & 0xFF
    out[:, 2] = c1_565 & 0xFF
    out[:, 3] = (c1_565 >> 8) & 0xFF
    out[:, 4] = bits & 0xFF
    out[:, 5] = (bits >> 8) & 0xFF
    out[:, 6] = (bits >> 16) & 0xFF
    out[:, 7] = (bits >> 24) & 0xFF
    return out.tobytes()


def save_dds_dxt1(path: Path, rgb: np.ndarray) -> None:
    """DDS compressed DXT1 (fourCC DXT1). BeamNG 0.38 lo accetta al 100%.
    H/W vengono pad a multiplo di 4 se necessario."""
    if rgb.ndim == 3 and rgb.shape[2] == 4:
        rgb = rgb[:, :, :3]
    rgb = rgb.astype(np.uint8)
    H, W = rgb.shape[:2]
    # Pad to multiple of 4
    pH = (H + 3) & ~3
    pW = (W + 3) & ~3
    if (pH, pW) != (H, W):
        padded = np.zeros((pH, pW, 3), dtype=np.uint8)
        padded[:H, :W] = rgb
        rgb = padded
        H, W = pH, pW

    block_data = _encode_dxt1(rgb)

    header = bytearray()
    header += b"DDS "
    header += struct.pack("<I", 124)
    header += struct.pack("<I", 0x00081007)      # CAPS|HEIGHT|WIDTH|PIXELFORMAT|LINEARSIZE
    header += struct.pack("<I", H)
    header += struct.pack("<I", W)
    header += struct.pack("<I", len(block_data)) # linearSize
    header += struct.pack("<I", 0)                # depth
    header += struct.pack("<I", 0)                # mipMapCount (no mips)
    header += b"\x00" * 44
    # Pixel format: DXT1 (fourCC)
    header += struct.pack("<I", 32)
    header += struct.pack("<I", 0x04)            # DDPF_FOURCC
    header += b"DXT1"                             # fourCC
    header += struct.pack("<I", 0)                # rgbBitCount
    header += struct.pack("<I", 0)
    header += struct.pack("<I", 0)
    header += struct.pack("<I", 0)
    header += struct.pack("<I", 0)
    header += struct.pack("<I", 0x1000)          # caps: TEXTURE
    header += struct.pack("<I", 0)
    header += struct.pack("<I", 0)
    header += struct.pack("<I", 0)
    header += struct.pack("<I", 0)

    with path.open("wb") as f:
        f.write(header)
        f.write(block_data)


def save_dds_rgb24(path: Path, rgb: np.ndarray) -> None:
    """Scrive un DDS uncompressed X8R8G8B8 (32-bit, alpha ignored).
    BeamNG 0.38 DDSFile reader RIFIUTA 24-bit RGB e RGBA con ALPHAPIXELS;
    accetta solo 32-bit con ALPHAPIXELS=0 (flag 0x40), treated as RGB only.
    rgb: ndarray HxWx{3,4} uint8."""
    if rgb.ndim != 3 or rgb.shape[2] not in (3, 4):
        raise ValueError(f"Unsupported shape {rgb.shape}")
    if rgb.shape[2] == 4:
        rgb = rgb[:, :, :3]
    rgb = rgb.astype(np.uint8)
    H, W = rgb.shape[:2]
    # BGRX pixel order (32-bit, X=0 padding)
    pad = np.zeros((H, W, 1), dtype=np.uint8)
    bgrx = np.concatenate([rgb[:, :, 2:3], rgb[:, :, 1:2], rgb[:, :, 0:1], pad], axis=2)

    header = bytearray()
    header += b"DDS "
    header += struct.pack("<I", 124)
    header += struct.pack("<I", 0x0000100F)      # CAPS|HEIGHT|WIDTH|PIXELFORMAT|PITCH
    header += struct.pack("<I", H)
    header += struct.pack("<I", W)
    header += struct.pack("<I", W * 4)           # pitch (32-bit)
    header += struct.pack("<I", 0)
    header += struct.pack("<I", 0)
    header += b"\x00" * 44
    # Pixel format: RGB only (0x40) NO ALPHAPIXELS, 32-bit X8R8G8B8
    header += struct.pack("<I", 32)
    header += struct.pack("<I", 0x40)            # RGB only (no ALPHAPIXELS bit)
    header += struct.pack("<I", 0)
    header += struct.pack("<I", 32)              # rgbBitCount: 32
    header += struct.pack("<I", 0x00FF0000)      # R mask
    header += struct.pack("<I", 0x0000FF00)      # G mask
    header += struct.pack("<I", 0x000000FF)      # B mask
    header += struct.pack("<I", 0x00000000)      # A mask = 0 (X channel)
    header += struct.pack("<I", 0x00001000)      # caps: TEXTURE
    header += struct.pack("<I", 0)
    header += struct.pack("<I", 0)
    header += struct.pack("<I", 0)
    header += struct.pack("<I", 0)

    with path.open("wb") as f:
        f.write(header)
        f.write(bgrx.tobytes())


def convert_png_to_dds(png_path: Path, dds_path: Path | None = None) -> Path:
    """Converte PNG a DDS DXT1 (compressed). BeamNG 0.38 accetta solo
    DXT1/DXT5; uncompressed e' rifiutato."""
    if dds_path is None:
        dds_path = png_path.with_suffix(".dds")
    Image.MAX_IMAGE_PIXELS = None
    im = Image.open(png_path).convert("RGB")
    arr = np.array(im, dtype=np.uint8)
    save_dds_dxt1(dds_path, arr)
    return dds_path


def _height_to_normal_rgb(h: np.ndarray, strength: float = 1.0) -> np.ndarray:
    """Converte heightmap float in normal map tangent-space RGB 0..255.
    R/G = gradiente x/y rinormalizzato in [0,1], B = componente Z (1=flat)."""
    dx = np.zeros_like(h)
    dy = np.zeros_like(h)
    dx[:, 1:-1] = (h[:, 2:] - h[:, :-2]) * 0.5 * strength
    dy[1:-1, :] = (h[2:, :] - h[:-2, :]) * 0.5 * strength
    nx = -dx
    ny = -dy
    nz = np.ones_like(h)
    mag = np.sqrt(nx * nx + ny * ny + nz * nz) + 1e-8
    nx /= mag
    ny /= mag
    nz /= mag
    r = ((nx * 0.5 + 0.5) * 255).clip(0, 255).astype(np.uint8)
    g = ((ny * 0.5 + 0.5) * 255).clip(0, 255).astype(np.uint8)
    b = ((nz * 0.5 + 0.5) * 255).clip(0, 255).astype(np.uint8)
    return np.stack([r, g, b], axis=-1)


def generate_asphalt_normal(level_dir: Path) -> str:
    """Normal map asfalto 1024x1024: grana fine + sporadici sassolini.
    Strength bassa — asfalto e' relativamente liscio."""
    size = 1024
    rng = np.random.default_rng(43)
    # base: noise fine gaussiano (grana)
    h = rng.normal(0.0, 0.10, (size, size)).astype(np.float32)
    # sparsi sassolini (piccolo rilievo)
    spk = rng.random((size, size), dtype=np.float32)
    stones = np.where(spk > 0.995, 0.40, 0.0)
    h = h + stones
    # leggero smoothing (sassolini con bordi sfumati)
    h[1:-1, 1:-1] = (h[1:-1, 1:-1] + h[:-2, 1:-1] + h[2:, 1:-1]
                       + h[1:-1, :-2] + h[1:-1, 2:]) / 5.0
    nrm = _height_to_normal_rgb(h, strength=1.2)
    out = level_dir / "art" / "road" / "asphalt_nrm.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(nrm).save(out, optimize=True)
    rel = f"levels/{LEVEL_NAME}/art/road/asphalt_nrm.png"
    print(f"Asphalt normal map {size}x{size}: {out.relative_to(MOD_DIR)}")
    return rel


def generate_bark_normal(level_dir: Path) -> str:
    """Normal map corteccia 256x256: striature verticali marcate (rilievo)."""
    size = 256
    rng = np.random.default_rng(31)
    stripe = _fbm_noise(size, octaves=3, seed=31, start_freq=2)
    fine = rng.normal(0, 0.08, (size, size)).astype(np.float32)
    # solchi verticali: enfatizzo l'asse Y (ridurre dy sulla horizontal
    # quasi a zero → rileva solo dx)
    h = stripe * 1.2 + fine * 0.3
    nrm = _height_to_normal_rgb(h, strength=2.5)
    out = level_dir / "art" / "nature" / "bark_nrm.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(nrm).save(out, optimize=True)
    rel = f"levels/{LEVEL_NAME}/art/nature/bark_nrm.png"
    print(f"Bark normal map {size}x{size}: {out.relative_to(MOD_DIR)}")
    return rel


def generate_stonewall_normal(level_dir: Path) -> str:
    """Normal map muretto 512x512: pattern pietre irregolari stile muretto
    a secco appenninico. Costruisco una height con blob random e fuga scura."""
    size = 512
    rng = np.random.default_rng(47)
    h = np.zeros((size, size), dtype=np.float32)
    # Piazza ~200 blob di dimensione variabile come "pietre"
    n_stones = 240
    for _ in range(n_stones):
        cx = int(rng.integers(0, size))
        cy = int(rng.integers(0, size))
        rx = int(rng.integers(12, 36))
        ry = int(rng.integers(10, 28))
        base_h = float(rng.uniform(0.5, 1.0))
        yy, xx = np.ogrid[:size, :size]
        d = ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2
        mask = d < 1.0
        falloff = np.clip(1.0 - np.sqrt(np.maximum(d, 0.0)), 0.0, 1.0)
        h = np.maximum(h, np.where(mask, base_h * falloff, 0.0))
    # Aggiungi fuga (malta) leggermente incavata tra le pietre
    fine = rng.normal(0, 0.05, (size, size)).astype(np.float32)
    h = h + fine
    nrm = _height_to_normal_rgb(h, strength=3.0)
    out = level_dir / "art" / "nature" / "stonewall_nrm.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(nrm).save(out, optimize=True)
    rel = f"levels/{LEVEL_NAME}/art/nature/stonewall_nrm.png"
    print(f"Stonewall normal map {size}x{size}: {out.relative_to(MOD_DIR)}")
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
    n2 = rng.normal(0.0, 0.04, (size, size)).astype(np.float32)
    # Patch larghe per spezzare uniformita' (fBm lowfreq)
    patch = _fbm_noise(size, octaves=3, seed=7, start_freq=4) * 0.18
    delta = n1 * 0.6 + n2 * 0.4 + patch
    # BASE NEUTRA (grigio-terroso). Evita bias verde altrimenti detail e
    # macro amplificano il verde gia' saturo del satellite -> neon effect.
    # Il ROLE del detail e' dare grana + micro-variazione, NON tingere.
    R = np.clip(0.50 + delta * 0.7, 0.25, 0.85)
    G = np.clip(0.48 + delta * 0.7, 0.22, 0.80)
    B = np.clip(0.40 + delta * 0.6, 0.18, 0.70)
    img = np.stack([R, G, B], axis=-1)
    img_u8 = (img * 255.0).astype(np.uint8)
    tex_dir = level_dir / "art" / "terrains"
    tex_dir.mkdir(parents=True, exist_ok=True)
    out = tex_dir / "detail_grass.png"
    Image.fromarray(img_u8).save(out, optimize=True)
    print(f"Terrain detailMap {size}x{size}: {out.relative_to(MOD_DIR)}")
    return f"levels/{LEVEL_NAME}/art/terrains/detail_grass.png"


def generate_terrain_normal_texture(level_dir: Path) -> str:
    """NormalMap per il TerrainMaterial: piccola perturbazione aleatoria
    (erba/terreno poco accidentato). Senza normalMap, BeamNG 0.38 logga
    'Material X is missing texture' dal TerrainCellMaterial e non renderizza.
    """
    size = 512
    rng = np.random.default_rng(13)
    h = rng.normal(0.0, 0.5, (size, size)).astype(np.float32)
    # Smoothing leggero (box blur 3x3)
    hs = (h + np.roll(h, 1, 0) + np.roll(h, -1, 0)
          + np.roll(h, 1, 1) + np.roll(h, -1, 1)) / 5.0
    nrm = _height_to_normal_rgb(hs, strength=0.8)
    tex_dir = level_dir / "art" / "terrains"
    tex_dir.mkdir(parents=True, exist_ok=True)
    out = tex_dir / "detail_grass_nrm.png"
    Image.fromarray(nrm).save(out, optimize=True)
    print(f"Terrain normalMap {size}x{size}: {out.relative_to(MOD_DIR)}")
    return f"levels/{LEVEL_NAME}/art/terrains/detail_grass_nrm.png"


def generate_terrain_macro_texture(level_dir: Path) -> str:
    """MacroMap per il TerrainMaterial: variazione di tinta su scala media
    (~500m) che si sovrappone al satellite. Mitiga la 'piattezza' del
    satellite a distanze medie. Tiled a macroSize metri.
    """
    size = 256
    rng = np.random.default_rng(19)
    # Usa 3 mappe fBm per determinare classi di terreno: verde / hay / terra
    w_green = _fbm_noise(size, octaves=4, seed=19, start_freq=4)
    w_hay   = _fbm_noise(size, octaves=4, seed=23, start_freq=4)
    w_soil  = _fbm_noise(size, octaves=4, seed=29, start_freq=4)
    # Normalizza su [0,1] e somma
    w_stack = np.stack([w_green, w_hay, w_soil], axis=-1)
    w_stack = (w_stack - w_stack.min()) / (w_stack.max() - w_stack.min() + 1e-6)
    # Softmax-ish per scegliere classe dominante (ma con blending)
    w_stack = w_stack ** 2.0
    w_stack /= (w_stack.sum(axis=-1, keepdims=True) + 1e-6)
    # Palette Italia centrale Aprile (hillside SS17 Molise):
    # - verde erba fresca: 45% tinto in G ma MODERATO (R=0.42, G=0.52, B=0.30)
    # - hay/paglia/erba secca: 30% (R=0.58, G=0.54, B=0.32)
    # - terra/sterrato: 25% (R=0.48, G=0.40, B=0.28)
    P_GREEN = np.array([0.44, 0.50, 0.30], np.float32)
    P_HAY   = np.array([0.60, 0.54, 0.32], np.float32)
    P_SOIL  = np.array([0.48, 0.40, 0.28], np.float32)
    rgb = (w_stack[..., 0:1] * P_GREEN
           + w_stack[..., 1:2] * P_HAY
           + w_stack[..., 2:3] * P_SOIL)
    # Aggiungi micro-noise per rompere banding
    micro = rng.normal(0.0, 0.025, (size, size, 1)).astype(np.float32)
    rgb = np.clip(rgb + micro, 0.15, 0.75)
    img_u8 = (rgb * 255.0).astype(np.uint8)
    tex_dir = level_dir / "art" / "terrains"
    tex_dir.mkdir(parents=True, exist_ok=True)
    out = tex_dir / "macro_grass.png"
    Image.fromarray(img_u8).save(out, optimize=True)
    print(f"Terrain macroMap {size}x{size}: {out.relative_to(MOD_DIR)}")
    return f"levels/{LEVEL_NAME}/art/terrains/macro_grass.png"


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


def _load_font(size: int):
    """Font TrueType con fallback progressivo. Preferisce arial sans-serif."""
    from PIL import ImageFont
    candidates = [
        "arialbd.ttf", "arial.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
    ]
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _nearest_pow2(n: int) -> int:
    """Restituisce la potenza di 2 piu' vicina a n (>= 16 <= 2048)."""
    import math as _math
    if n < 16:
        return 16
    if n > 2048:
        return 2048
    lo = 1 << int(_math.log2(n))
    hi = lo << 1
    return lo if (n - lo) < (hi - n) else hi


def save_pow2(img, out_path, optimize: bool = True):
    """Salva un PIL Image forzando dimensioni POW2 per Texture Cooker BeamNG.
    Il log BeamNG mostra 'skip cooking, texture is smaller than 16x16 or not
    power of 2' per PNG con dimensioni non POW2 -> texture non caricata ->
    mesh renderizzato completamente nero.
    """
    from PIL import Image as _Im
    w, h = img.size
    w2 = _nearest_pow2(w); h2 = _nearest_pow2(h)
    if (w, h) != (w2, h2):
        img = img.resize((w2, h2), _Im.LANCZOS)
    img.save(out_path, optimize=optimize)


def generate_landmark_signs(level_dir: Path,
                              terrain_z_sampler=None) -> Path | None:
    """Landmark iconici SS17 Macerone per immersione:
    - Cartello marrone 'VALICO DEL MACERONE m 881' al passo
    - Scudo blu 'SS17' al punto di spawn
    - Direzionale 'ISERNIA 15 / CASTEL DI SANGRO 32' in prossimita' spawn
    - Edicola votiva (stele in pietra con croce) vicino al valico

    Output:
      levels/macerone/art/shapes/signs/sign_valico.color.png
      levels/macerone/art/shapes/signs/sign_ss17.color.png
      levels/macerone/art/shapes/signs/sign_direzionale.color.png
      levels/macerone/art/shapes/macerone_signs.obj (+ .mtl)

    Ritorna il Path dell'OBJ (o None se non ci sono dati centerline).
    """
    from PIL import Image, ImageDraw
    import csv as _csv

    cl_path = ROOT / "output" / "centerline.csv"
    if not cl_path.exists():
        return None
    with cl_path.open(newline="", encoding="utf-8") as f:
        cl = [(float(r["x"]), float(r["y"]), float(r["z"]))
                for r in _csv.DictReader(f)]
    if len(cl) < 50:
        return None

    signs_dir = level_dir / "art" / "shapes" / "signs"
    signs_dir.mkdir(parents=True, exist_ok=True)

    # ---- Generator texture PNG ----
    # 1) Valico del Macerone — pannello marrone stile cartello turistico IT
    # Disegno a risoluzione alta "comoda" poi resize a POW2 al save.
    # BeamNG Texture Cooker rifiuta dimensioni non power-of-2 (log:
    # "skip cooking, texture is smaller than 16x16 or not power of 2").
    W, H = 1280, 640
    img = Image.new("RGB", (W, H), (98, 54, 26))  # marrone turistico
    d = ImageDraw.Draw(img)
    # Bordo bianco stile CdS italiana
    b = 14
    d.rectangle([b, b, W - b - 1, H - b - 1], outline=(250, 250, 245), width=8)
    # Mountain icon: triangolino bianco in alto
    mtn_cx = W // 2
    mtn_cy = 120
    d.polygon([(mtn_cx - 90, mtn_cy + 50), (mtn_cx, mtn_cy - 40),
                  (mtn_cx - 15, mtn_cy + 5), (mtn_cx + 30, mtn_cy - 20),
                  (mtn_cx + 90, mtn_cy + 50)],
                 fill=(250, 250, 245))
    f_valico = _load_font(88)
    f_alt = _load_font(72)
    text1 = "VALICO DEL MACERONE"
    tb = d.textbbox((0, 0), text1, font=f_valico)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    d.text(((W - tw) // 2, 230), text1, font=f_valico, fill=(250, 250, 245))
    text2 = "m  881"
    tb2 = d.textbbox((0, 0), text2, font=f_alt)
    tw2 = tb2[2] - tb2[0]
    d.text(((W - tw2) // 2, 420), text2, font=f_alt, fill=(250, 250, 245))
    valico_png = signs_dir / "sign_valico.color.png"
    save_pow2(img, valico_png)

    # 2) Scudo SS17 — bianco su blu, stile strada statale italiana
    W2, H2 = 640, 800
    img2 = Image.new("RGB", (W2, H2), (245, 245, 245))
    d2 = ImageDraw.Draw(img2)
    # Pannello blu con bordo bianco + inner rim blu scuro
    d2.rectangle([20, 40, W2 - 20, H2 - 60], fill=(20, 65, 150),
                    outline=(250, 250, 245), width=10)
    d2.rectangle([46, 66, W2 - 46, H2 - 86], outline=(10, 35, 90), width=4)
    f_head = _load_font(54)
    head = "STRADA STATALE"
    tb_h = d2.textbbox((0, 0), head, font=f_head)
    tw_h = tb_h[2] - tb_h[0]
    d2.text(((W2 - tw_h) // 2, 110), head, font=f_head, fill=(250, 250, 245))
    f_ss = _load_font(240)
    ss = "SS 17"
    tb_ss = d2.textbbox((0, 0), ss, font=f_ss)
    tw_ss = tb_ss[2] - tb_ss[0]
    d2.text(((W2 - tw_ss) // 2, 280), ss, font=f_ss, fill=(250, 250, 245))
    f_foot = _load_font(40)
    foot = "Appennino Abruzzese"
    tb_f = d2.textbbox((0, 0), foot, font=f_foot)
    tw_f = tb_f[2] - tb_f[0]
    d2.text(((W2 - tw_f) // 2, 620), foot, font=f_foot, fill=(250, 250, 245))
    ss17_png = signs_dir / "sign_ss17.color.png"
    save_pow2(img2, ss17_png)

    # 3) Direzionale — pannello bianco con frecce verso destinazioni
    W3, H3 = 1536, 640
    img3 = Image.new("RGB", (W3, H3), (248, 246, 240))
    d3 = ImageDraw.Draw(img3)
    d3.rectangle([14, 14, W3 - 15, H3 - 15], outline=(30, 30, 30), width=6)
    f_city = _load_font(96)
    f_km = _load_font(72)
    # Riga 1: ISERNIA  15 →
    row1_y = 90
    d3.text((90, row1_y), "ISERNIA", font=f_city, fill=(20, 20, 20))
    d3.text((700, row1_y + 12), "15 km", font=f_km, fill=(20, 20, 20))
    # Freccia destra
    ax0 = 1050; ay = row1_y + 60
    d3.polygon([(ax0, ay - 36), (ax0 + 200, ay - 36), (ax0 + 200, ay - 70),
                   (ax0 + 360, ay), (ax0 + 200, ay + 70), (ax0 + 200, ay + 36),
                   (ax0, ay + 36)], fill=(20, 20, 20))
    # Riga 2: ← CASTEL DI SANGRO  32
    row2_y = 340
    bx1 = 90; by = row2_y + 60
    d3.polygon([(bx1 + 360, by - 36), (bx1 + 160, by - 36),
                   (bx1 + 160, by - 70), (bx1, by),
                   (bx1 + 160, by + 70), (bx1 + 160, by + 36),
                   (bx1 + 360, by + 36)], fill=(20, 20, 20))
    d3.text((510, row2_y), "CASTEL DI SANGRO", font=f_city, fill=(20, 20, 20))
    d3.text((510, row2_y + 110), "32 km", font=f_km, fill=(20, 20, 20))
    dir_png = signs_dir / "sign_direzionale.color.png"
    save_pow2(img3, dir_png)

    print(f"  sign textures: valico + SS17 + direzionale in "
          f"levels/{LEVEL_NAME}/art/shapes/signs/")

    # ---- Posizioni sui punti chiave della centerline ----
    def tangent_at(i):
        i0 = max(0, i - 2); i1 = min(len(cl) - 1, i + 2)
        dx = cl[i1][0] - cl[i0][0]
        dy = cl[i1][1] - cl[i0][1]
        n = math.hypot(dx, dy)
        return (dx / n, dy / n) if n > 1e-6 else (1.0, 0.0)

    def g_z(x, y):
        if terrain_z_sampler is not None:
            z = terrain_z_sampler(x, y)
            if z is not None:
                return z
        # fallback: cerca z della centerline vicina
        best = float("inf"); zq = 0.0
        for (xx, yy, zz) in cl[::4]:
            d2 = (xx - x) ** 2 + (yy - y) ** 2
            if d2 < best:
                best = d2; zq = zz
        return zq

    # Summit = punto max z della centerline
    max_i = max(range(len(cl)), key=lambda i: cl[i][2])
    # Evito i primi/ultimi 20 punti (spesso sono clip boundary)
    if max_i < 20 or max_i > len(cl) - 20:
        candidates = cl[20:-20]
        max_i = 20 + max(range(len(candidates)),
                           key=lambda i: candidates[i][2])

    # Spawn sign — qualche decina di metri dal punto iniziale
    ss17_i = 25

    # Direzionale — poco dopo SS17 shield, sullo stesso lato
    dir_i = 60

    # Per ogni sign: side = +1 (sinistra nella direzione di marcia)
    # o -1 (destra). In Italia si guida a destra, cartelli sulla destra.
    def place_right(i: int, offset_m: float):
        x0, y0, z0 = cl[i]
        tx, ty = tangent_at(i)
        # normale destra (verso destra nella direzione di marcia): (ty, -tx)
        nx, ny = ty, -tx
        x = x0 + nx * offset_m
        y = y0 + ny * offset_m
        z = g_z(x, y)
        # heading del pannello: face normale = -normale (verso strada)
        # cioe' (-ty, tx)
        heading = math.atan2(-ny, -nx)  # = atan2(tx, -ty)
        return (x, y, z, heading, tx, ty)

    valico_pos = place_right(max_i, 5.0)
    # Edicola molto vicina al valico, 2m piu' in la'
    edicola_pos = place_right(max_i + 4, 3.2)
    ss17_pos = place_right(ss17_i, 5.0)
    dir_pos = place_right(dir_i, 5.0)

    print(f"  signs pos: Valico idx {max_i} z={cl[max_i][2]:.1f}m, "
          f"SS17 idx {ss17_i}, Direzionale idx {dir_i}")

    # ---- OBJ builder ----
    verts: list[tuple[float, float, float]] = []
    uvs: list[tuple[float, float]] = []
    groups: dict[str, list[tuple[str, list[list[tuple[int, int]]]]]] = {}
    # Formato: groups[group_name] = [(material, [face [(v_idx, vt_idx)]])]

    def add_group(name: str, material: str):
        groups.setdefault(name, []).append((material, []))

    def add_face(name: str, face: list[tuple[int, int]]):
        groups[name][-1][1].append(face)

    def add_v(x, y, z):
        verts.append((x, y, z))
        return len(verts)  # 1-based

    def add_vt(u, v):
        uvs.append((u, v))
        return len(uvs)  # 1-based

    def add_panel(group, material, cx, cy, cz, heading, w, h, z_bot=None,
                    z_top=None):
        """Panel frontale orientato a heading (face normal). Double-sided
        (aggiungiamo entrambe le facce con winding opposto per essere visibile
        da entrambi i lati). La texture e' mappata UV 0..1 sulla faccia
        frontale; dietro si vede speculare ma non ci importa.
        Panel dims: w orizzontale (perpendicolare a heading), h verticale."""
        if z_bot is None:
            z_bot = cz
        if z_top is None:
            z_top = cz + h
        # tangent (perpendicolare a heading): rotazione -90deg da heading
        tx = -math.sin(heading); ty = math.cos(heading)
        hw = w * 0.5
        # 4 angoli: BL, BR, TR, TL (da davanti)
        bl = add_v(cx - tx * hw, cy - ty * hw, z_bot)
        br = add_v(cx + tx * hw, cy + ty * hw, z_bot)
        tr = add_v(cx + tx * hw, cy + ty * hw, z_top)
        tl = add_v(cx - tx * hw, cy - ty * hw, z_top)
        uv_bl = add_vt(0.0, 0.0)
        uv_br = add_vt(1.0, 0.0)
        uv_tr = add_vt(1.0, 1.0)
        uv_tl = add_vt(0.0, 1.0)
        add_group(group, material)
        # Front face (CCW vista da davanti = da heading direction)
        add_face(group, [(bl, uv_bl), (br, uv_br), (tr, uv_tr)])
        add_face(group, [(bl, uv_bl), (tr, uv_tr), (tl, uv_tl)])
        # Back face (CCW vista dal retro)
        add_face(group, [(bl, uv_bl), (tr, uv_tr), (br, uv_br)])
        add_face(group, [(bl, uv_bl), (tl, uv_tl), (tr, uv_tr)])

    def add_pole(group, material, cx, cy, cz, height, radius=0.06):
        """Pole esagonale cx,cy,cz base, altezza height, raggio radius."""
        add_group(group, material)
        n = 6
        ring_bot = []
        ring_top = []
        for k in range(n):
            a = (2 * math.pi * k) / n
            x = cx + math.cos(a) * radius
            y = cy + math.sin(a) * radius
            ring_bot.append(add_v(x, y, cz))
            ring_top.append(add_v(x, y, cz + height))
        uvb = add_vt(0.0, 0.0); uvt = add_vt(0.0, 1.0)
        for k in range(n):
            k1 = (k + 1) % n
            add_face(group, [(ring_bot[k], uvb), (ring_bot[k1], uvb),
                               (ring_top[k1], uvt)])
            add_face(group, [(ring_bot[k], uvb), (ring_top[k1], uvt),
                               (ring_top[k], uvt)])

    def add_stone_box(group, material, cx, cy, cz, lx, ly, lz):
        """Parallelepipedo semplice (stele) lx,ly,lz centrato su (cx,cy),
        base a cz."""
        add_group(group, material)
        hx = lx * 0.5; hy = ly * 0.5
        # 8 vertici
        vs = []
        for (sx, sy) in ((-hx, -hy), (+hx, -hy), (+hx, +hy), (-hx, +hy)):
            vs.append(add_v(cx + sx, cy + sy, cz))         # bot
        for (sx, sy) in ((-hx, -hy), (+hx, -hy), (+hx, +hy), (-hx, +hy)):
            vs.append(add_v(cx + sx, cy + sy, cz + lz))    # top
        uv00 = add_vt(0.0, 0.0); uv10 = add_vt(1.0, 0.0)
        uv11 = add_vt(1.0, 1.0); uv01 = add_vt(0.0, 1.0)
        # 4 lati (front=Y-, right=X+, back=Y+, left=X-) + top
        sides = [
            (0, 1, 5, 4),  # Y-
            (1, 2, 6, 5),  # X+
            (2, 3, 7, 6),  # Y+
            (3, 0, 4, 7),  # X-
            (4, 5, 6, 7),  # top
        ]
        for (a, b, c, d_) in sides:
            add_face(group, [(vs[a], uv00), (vs[b], uv10), (vs[c], uv11)])
            add_face(group, [(vs[a], uv00), (vs[c], uv11), (vs[d_], uv01)])

    # --- Valico del Macerone sign ---
    (vx, vy, vz, vh, vtx, vty) = valico_pos
    # Pole 2.4m
    POLE_H = 2.4
    PANEL_H = 1.2
    PANEL_W = 2.4
    add_pole("ValicoPole", "SignPole", vx, vy, vz, POLE_H + PANEL_H, 0.07)
    add_panel("ValicoPanel", "SignValico",
                vx, vy, vz, vh, PANEL_W, PANEL_H,
                z_bot=vz + POLE_H, z_top=vz + POLE_H + PANEL_H)

    # --- Edicola votiva (stele di pietra con croce) ---
    (ex, ey, ez, eh, etx, ety) = edicola_pos
    STELE_H = 1.6
    STELE_W = 0.55
    STELE_D = 0.45
    add_stone_box("Edicola", "EdicolaStone",
                    ex, ey, ez, STELE_W, STELE_D, STELE_H)
    # Croce sopra: barra verticale + barra orizzontale
    CROSS_H = 0.45
    CROSS_W = 0.30
    # Asta verticale
    add_stone_box("EdicolaCross", "EdicolaStone",
                    ex, ey, ez + STELE_H, 0.08, 0.08, CROSS_H)
    # Braccio orizzontale (25% dall'alto della croce)
    add_stone_box("EdicolaCross", "EdicolaStone",
                    ex, ey, ez + STELE_H + CROSS_H * 0.55,
                    CROSS_W, 0.08, 0.08)

    # --- Scudo SS17 ---
    (sx, sy, sz, sh, stx, sty) = ss17_pos
    add_pole("SS17Pole", "SignPole", sx, sy, sz, POLE_H + 1.4, 0.07)
    add_panel("SS17Panel", "SignSS17",
                sx, sy, sz, sh, 1.4, 1.4,
                z_bot=sz + POLE_H, z_top=sz + POLE_H + 1.4)

    # --- Direzionale ---
    (dx_, dy_, dz_, dh_, dtx_, dty_) = dir_pos
    add_pole("DirPole", "SignPole", dx_, dy_, dz_, POLE_H + 1.4, 0.08)
    add_panel("DirPanel", "SignDirezionale",
                dx_, dy_, dz_, dh_, 3.6, 1.5,
                z_bot=dz_ + POLE_H, z_top=dz_ + POLE_H + 1.5)

    # ---- Scrivi OBJ + MTL ----
    obj_path = level_dir / "art" / "shapes" / "macerone_signs.obj"
    mtl_path = obj_path.with_suffix(".mtl")
    lines = [f"mtllib {mtl_path.name}\n"]
    for (x, y, z) in verts:
        lines.append(f"v {x:.4f} {y:.4f} {z:.4f}\n")
    for (u, v) in uvs:
        lines.append(f"vt {u:.4f} {v:.4f}\n")
    for gname, subs in groups.items():
        lines.append(f"g {gname}\n")
        for (mat, faces) in subs:
            lines.append(f"usemtl {mat}\n")
            for face in faces:
                face_str = " ".join(f"{vi}/{ti}" for (vi, ti) in face)
                lines.append(f"f {face_str}\n")
    obj_path.write_text("".join(lines), encoding="utf-8")

    # MTL con map_Kd (Texture Cooker BeamNG legge .color.png e produce DDS)
    mtl_lines = []
    def _mtl(mat, kd, map_kd=None):
        mtl_lines.append(f"newmtl {mat}\n")
        mtl_lines.append(f"Kd {kd[0]:.3f} {kd[1]:.3f} {kd[2]:.3f}\n")
        mtl_lines.append("Ka 0 0 0\nKs 0 0 0\nNs 1\nillum 1\n")
        if map_kd:
            mtl_lines.append(f"map_Kd {map_kd}\n")
        mtl_lines.append("\n")
    _mtl("SignValico", (1.0, 1.0, 1.0),
          f"../signs/{valico_png.name}")
    _mtl("SignSS17", (1.0, 1.0, 1.0),
          f"../signs/{ss17_png.name}")
    _mtl("SignDirezionale", (1.0, 1.0, 1.0),
          f"../signs/{dir_png.name}")
    _mtl("SignPole", (0.42, 0.42, 0.44))
    _mtl("EdicolaStone", (0.52, 0.48, 0.42))
    mtl_path.write_text("".join(mtl_lines), encoding="utf-8")

    print(f"  landmark signs -> {obj_path.relative_to(MOD_DIR)} "
          f"({len(verts)} v, {sum(len(sub[1]) for subs in groups.values() for sub in subs)} f)")
    return obj_path


def generate_video_landmarks(level_dir: Path,
                              terrain_z_sampler=None) -> Path | None:
    """Landmark specifici osservati nella registrazione StreetView SS17.

    Frame riferimento ogni 1s, velocita' StreetView assumida 13.9 m/s (50 km/h).
    Posizioni calcolate per cumulative distance lungo centerline.

    Elementi piazzati:
    - Cartelli con TESTO ESATTO dal video:
      * "50 km/h" (circolare rosso/bianco) @ t9s
      * "PNEUMATICI INVERNALI 15/11 - 15/04" (blu rettangolare) @ t39s
      * "30 km/h" (circolare rosso/bianco) @ t82s
      * "CURVA SX" (triangolo rosso/bianco) @ t91s
    - Edifici iconici (box procedurali con facciata mattone rosso):
      * Rudere colombaia tonda 3 piani @ t28s, SX 50m
      * Casa-torre colombaia quadrata 3 piani @ t33s, SX 50m
      * Casale con cipressi e tetto mattone @ t103s, SX 150m
    - Balle di fieno cilindriche in campo DX @ t88-90s
    - Delineatori bianco-rossi extra sulle curve @ t85-95s
    """
    from PIL import Image, ImageDraw
    import csv as _csv

    cl_path = ROOT / "output" / "centerline.csv"
    if not cl_path.exists():
        return None
    with cl_path.open(newline="", encoding="utf-8") as f:
        cl = [(float(r["x"]), float(r["y"]), float(r["z"]))
                for r in _csv.DictReader(f)]
    if len(cl) < 50:
        return None

    # ---- Cumulative length lungo centerline ----
    cum = [0.0]
    for i in range(1, len(cl)):
        dx_ = cl[i][0] - cl[i-1][0]; dy_ = cl[i][1] - cl[i-1][1]
        cum.append(cum[-1] + math.hypot(dx_, dy_))
    total_len = cum[-1]
    SV_SPEED = 13.9  # m/s

    def idx_at_t(t_sec: float) -> int:
        d = min(SV_SPEED * t_sec, total_len - 1.0)
        import bisect
        return min(max(0, bisect.bisect_left(cum, d)), len(cl) - 1)

    signs_dir = level_dir / "art" / "shapes" / "signs"
    signs_dir.mkdir(parents=True, exist_ok=True)
    tex_dir = level_dir / "art" / "shapes" / "video_landmarks"
    tex_dir.mkdir(parents=True, exist_ok=True)

    # ---- TEXTURE CARTELLI dal video ----
    # 1) Limite velocita' 50 km/h (circolare rosso-bianco europeo)
    def make_speed_limit(num_text: str, out_path: Path):
        W = H = 512
        img = Image.new("RGB", (W, H), (250, 248, 240))  # sfondo bianco-panel
        d = ImageDraw.Draw(img)
        cx, cy = W // 2, H // 2
        R_out = 220
        R_in = 170
        # Anello rosso
        d.ellipse([cx - R_out, cy - R_out, cx + R_out, cy + R_out],
                   fill=(195, 30, 30))
        # Bianco interno
        d.ellipse([cx - R_in, cy - R_in, cx + R_in, cy + R_in],
                   fill=(248, 246, 240))
        f_num = _load_font(220)
        tb = d.textbbox((0, 0), num_text, font=f_num)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        d.text((cx - tw // 2 - tb[0], cy - th // 2 - tb[1] - 8),
                num_text, font=f_num, fill=(20, 20, 20))
        save_pow2(img, out_path)

    limit50_png = tex_dir / "sign_limit50.color.png"
    limit30_png = tex_dir / "sign_limit30.color.png"
    make_speed_limit("50", limit50_png)
    make_speed_limit("30", limit30_png)

    # 2) Pneumatici invernali (blu rettangolare con pittogramma)
    W2, H2 = 640, 800
    img2 = Image.new("RGB", (W2, H2), (248, 246, 240))
    d2 = ImageDraw.Draw(img2)
    # Pannello blu
    d2.rectangle([20, 30, W2 - 20, H2 - 30], fill=(22, 70, 160),
                  outline=(248, 246, 240), width=10)
    # Pittogramma pneumatico (cerchio scuro con battistrada a V)
    t_cx, t_cy, t_r = W2 // 2, 220, 140
    d2.ellipse([t_cx - t_r, t_cy - t_r, t_cx + t_r, t_cy + t_r],
                fill=(30, 30, 30))
    d2.ellipse([t_cx - t_r + 28, t_cy - t_r + 28,
                 t_cx + t_r - 28, t_cy + t_r - 28], fill=(55, 55, 55))
    # Battistrada 4 scanalature a V
    for k in range(-1, 2):
        off = k * 42
        d2.polygon([(t_cx - 70 + off, t_cy - 70),
                     (t_cx + off, t_cy - 10),
                     (t_cx + 70 + off, t_cy - 70),
                     (t_cx + off, t_cy + 50)], fill=(30, 30, 30))
    # Testo sotto pittogramma — piu' righe
    f_line = _load_font(44)
    lines = ["OBBLIGO", "PNEUMATICI INVERNALI",
             "O CATENE A BORDO",
             "DAL 15 NOV AL 15 APR"]
    y = 420
    for ln in lines:
        tb = d2.textbbox((0, 0), ln, font=f_line)
        tw = tb[2] - tb[0]
        d2.text(((W2 - tw) // 2, y), ln, font=f_line,
                 fill=(248, 246, 240))
        y += 62
    winter_png = tex_dir / "sign_winter_tires.color.png"
    save_pow2(img2, winter_png)

    # 3) Triangolo pericolo CURVA SX (rosso/bianco con freccia)
    W3 = H3 = 640
    img3 = Image.new("RGB", (W3, H3), (248, 246, 240))
    d3 = ImageDraw.Draw(img3)
    # Triangolo equilatero apex in alto
    apex = (W3 // 2, 40)
    bl = (60, H3 - 60)
    br = (W3 - 60, H3 - 60)
    d3.polygon([apex, bl, br], fill=(195, 30, 30))
    inner_inset = 36
    apex_in = (W3 // 2, 40 + inner_inset + 6)
    bl_in = (60 + inner_inset, H3 - 60 - int(inner_inset * 0.55))
    br_in = (W3 - 60 - inner_inset, H3 - 60 - int(inner_inset * 0.55))
    d3.polygon([apex_in, bl_in, br_in], fill=(248, 246, 240))
    # Simbolo curva a sinistra: linea che parte in basso destra verso alto sinistra con curve
    # semplificato: freccia ad arco
    arrow_pts = []
    import math as _m
    for k in range(22):
        t = k / 21.0
        # arco: parte da (0.75, 0.80) a (0.20, 0.35) via controllo (0.35, 0.75)
        u = 1 - t
        px = u * u * 0.78 + 2 * u * t * 0.30 + t * t * 0.20
        py = u * u * 0.80 + 2 * u * t * 0.72 + t * t * 0.38
        arrow_pts.append((W3 * px, H3 * py))
    # spessore
    for w in range(-6, 7):
        for (px, py) in arrow_pts:
            d3.ellipse([px - 8, py - 8 + w * 0.5,
                         px + 8, py + 8 + w * 0.5], fill=(20, 20, 20))
    # punta freccia in cima
    tip = arrow_pts[-1]
    d3.polygon([(tip[0] - 45, tip[1] + 30), (tip[0] + 20, tip[1] - 35),
                 (tip[0] + 45, tip[1] + 5)], fill=(20, 20, 20))
    curve_png = tex_dir / "sign_curve_left.color.png"
    save_pow2(img3, curve_png)

    # ---- TEXTURE EDIFICI ICONICI (facciate mattone rosso) ----
    def make_brick_facade(out_path: Path, W=1024, H=768,
                            floors=3, windows_per_floor=4,
                            has_cancellata=True,
                            has_round_dovecote=False,
                            has_square_dovecote=False,
                            windows_boarded=False,
                            roof_color=(120, 60, 48)):
        """Facciata mattone rosso con finestre e tetto. Proporzioni:
        - bottom 70% = muro mattone rosso
        - top 25% = tetto coppi
        - ultimo 5% = cielo (trasparente via alpha/sky)
        """
        img = Image.new("RGB", (W, H), (135, 160, 180))  # sfondo cielo
        d = ImageDraw.Draw(img)
        # Tetto zone
        roof_bot_y = int(0.12 * H)
        wall_bot_y = int(0.96 * H)
        wall_top_y = roof_bot_y
        # Tetto coppi - pannello trapezoidale
        d.polygon([(0, roof_bot_y), (W, roof_bot_y),
                    (W - 40, wall_top_y - 3), (40, wall_top_y - 3)],
                   fill=roof_color)
        # Muro mattone rosso: base color + rumore
        import numpy as _np
        rng = _np.random.default_rng(13)
        wall_h = wall_bot_y - wall_top_y
        base = _np.array([142, 60, 48], _np.uint8)
        noise = rng.normal(0, 10, (wall_h, W, 3)).astype(_np.int16)
        wall = _np.clip(base + noise, 40, 220).astype(_np.uint8)
        # Linee orizzontali corsi di mattoni ogni ~8 px (scuro)
        for y in range(0, wall_h, 8):
            wall[y:y+1, :, :] = _np.clip(wall[y:y+1, :, :] * 0.78, 0, 255)
        # Stagger verticale: ogni corso sposta giunti
        # (omesso per semplicita', pattern orizzontale sufficiente)
        wall_img = Image.fromarray(wall)
        img.paste(wall_img, (0, wall_top_y))
        # Finestre rettangolari regolari per piano
        margin_x = int(0.08 * W)
        win_w = int((W - 2 * margin_x) / (windows_per_floor * 1.8))
        gap_x = (W - 2 * margin_x - win_w * windows_per_floor) // max(1, (windows_per_floor - 1))
        floor_h = wall_h // (floors + 1)
        win_h = int(floor_h * 0.55)
        for fl in range(floors):
            win_y = wall_top_y + floor_h // 2 + fl * floor_h
            for wi in range(windows_per_floor):
                wx = margin_x + wi * (win_w + gap_x)
                if windows_boarded and fl >= 1:
                    # finestre murate: grigio-marrone scuro
                    d.rectangle([wx, win_y, wx + win_w, win_y + win_h],
                                 fill=(80, 65, 52))
                    d.rectangle([wx, win_y, wx + win_w, win_y + win_h],
                                 outline=(35, 25, 20), width=3)
                else:
                    # vetro scuro con telaio
                    d.rectangle([wx, win_y, wx + win_w, win_y + win_h],
                                 fill=(40, 45, 55))
                    d.rectangle([wx, win_y, wx + win_w, win_y + win_h],
                                 outline=(210, 205, 195), width=3)
                    # croce telaio
                    mx = wx + win_w // 2
                    my = win_y + win_h // 2
                    d.line([(mx, win_y + 3), (mx, win_y + win_h - 3)],
                            fill=(210, 205, 195), width=2)
                    d.line([(wx + 3, my), (wx + win_w - 3, my)],
                            fill=(210, 205, 195), width=2)
        # Porta centrale piano terra (doppia battente scura)
        door_w = int(win_w * 1.4)
        door_h = int(floor_h * 0.88)
        door_x = W // 2 - door_w // 2
        door_y = wall_bot_y - door_h
        d.rectangle([door_x, door_y, door_x + door_w, door_y + door_h],
                     fill=(55, 35, 22))
        d.rectangle([door_x, door_y, door_x + door_w, door_y + door_h],
                     outline=(140, 90, 55), width=4)
        # Cancellata ferro battuto (banda sottile in basso, scura con sbarre)
        if has_cancellata:
            gate_y = wall_bot_y - 6
            d.rectangle([0, gate_y, W, H - 1], fill=(32, 30, 30))
            # sbarre verticali
            for bx in range(8, W, 22):
                d.line([(bx, gate_y + 2), (bx, H - 2)],
                        fill=(60, 58, 56), width=2)
        # Colombaia tonda (riportata sul colmo tetto)
        if has_round_dovecote:
            dov_cx = W // 2
            dov_cy = int(0.05 * H)
            dov_r = int(0.045 * H)
            d.ellipse([dov_cx - dov_r, dov_cy - dov_r,
                        dov_cx + dov_r, dov_cy + dov_r],
                       fill=(130, 55, 42))
            # Apertura
            d.ellipse([dov_cx - dov_r // 3, dov_cy - dov_r // 3,
                        dov_cx + dov_r // 3, dov_cy + dov_r // 3],
                       fill=(25, 20, 18))
        if has_square_dovecote:
            # Torre quadrata sul colmo: rettangolo con 10 aperture griglia 5x2
            tower_w = int(0.22 * W); tower_h = int(0.11 * H)
            tx = W // 2 - tower_w // 2
            ty = int(0.01 * H)
            d.rectangle([tx, ty, tx + tower_w, ty + tower_h],
                         fill=(138, 58, 46), outline=(85, 38, 30), width=3)
            # Tetto piccolo sulla torre
            d.polygon([(tx - 8, ty + 2), (tx + tower_w + 8, ty + 2),
                        (tx + tower_w + 18, ty - 10), (tx - 18, ty - 10)],
                       fill=(110, 55, 45))
            # 10 aperture 5x2
            cell_w = tower_w // 5
            cell_h = tower_h // 2
            for row in range(2):
                for col in range(5):
                    ox = tx + col * cell_w + cell_w // 4
                    oy = ty + row * cell_h + cell_h // 4
                    d.rectangle([ox, oy, ox + cell_w // 2, oy + cell_h // 2],
                                 fill=(20, 18, 16))
        save_pow2(img, out_path)

    rudere_png = tex_dir / "bldg_rudere.color.png"
    torretta_png = tex_dir / "bldg_torretta.color.png"
    casale_png = tex_dir / "bldg_casale.color.png"
    make_brick_facade(rudere_png, floors=3, windows_per_floor=4,
                       has_cancellata=True, has_round_dovecote=True,
                       windows_boarded=True,
                       roof_color=(110, 58, 46))
    make_brick_facade(torretta_png, floors=3, windows_per_floor=5,
                       has_cancellata=True, has_square_dovecote=True,
                       windows_boarded=False,
                       roof_color=(125, 65, 52))
    make_brick_facade(casale_png, floors=2, windows_per_floor=6,
                       has_cancellata=False, has_round_dovecote=False,
                       windows_boarded=False,
                       roof_color=(135, 75, 58))

    print(f"  video textures: signs/*.png + bldg/*.png in "
          f"levels/{LEVEL_NAME}/art/shapes/video_landmarks/")

    # ---- Helpers ----
    def tangent_at(i):
        i0 = max(0, i - 2); i1 = min(len(cl) - 1, i + 2)
        dx = cl[i1][0] - cl[i0][0]
        dy = cl[i1][1] - cl[i0][1]
        n = math.hypot(dx, dy)
        return (dx / n, dy / n) if n > 1e-6 else (1.0, 0.0)

    def g_z(x, y):
        if terrain_z_sampler is not None:
            z = terrain_z_sampler(x, y)
            if z is not None:
                return z
        best = float("inf"); zq = 0.0
        for (xx, yy, zz) in cl[::4]:
            d2 = (xx - x) ** 2 + (yy - y) ** 2
            if d2 < best:
                best = d2; zq = zz
        return zq

    def place_side(i: int, offset_m: float, side: int = 1):
        """side=+1 destra, -1 sinistra. Heading verso la strada."""
        x0, y0, _z0 = cl[i]
        tx, ty = tangent_at(i)
        nx, ny = ty * side, -tx * side  # destra: (ty, -tx)
        x = x0 + nx * offset_m
        y = y0 + ny * offset_m
        z = g_z(x, y)
        heading = math.atan2(-ny, -nx)
        return (x, y, z, heading, tx, ty)

    # ---- OBJ builder condiviso ----
    verts: list[tuple[float, float, float]] = []
    uvs: list[tuple[float, float]] = []
    groups: dict[str, list] = {}

    def add_group(name, material):
        groups.setdefault(name, []).append((material, []))

    def add_face(name, face):
        groups[name][-1][1].append(face)

    def add_v(x, y, z):
        verts.append((x, y, z)); return len(verts)

    def add_vt(u, v):
        uvs.append((u, v)); return len(uvs)

    def add_panel_quad(group, material, cx, cy, cz, heading, w, h,
                         z_bot=None, z_top=None):
        if z_bot is None: z_bot = cz
        if z_top is None: z_top = cz + h
        tx = -math.sin(heading); ty = math.cos(heading)
        hw = w * 0.5
        bl = add_v(cx - tx * hw, cy - ty * hw, z_bot)
        br = add_v(cx + tx * hw, cy + ty * hw, z_bot)
        tr = add_v(cx + tx * hw, cy + ty * hw, z_top)
        tl = add_v(cx - tx * hw, cy - ty * hw, z_top)
        u00 = add_vt(0.0, 0.0); u10 = add_vt(1.0, 0.0)
        u11 = add_vt(1.0, 1.0); u01 = add_vt(0.0, 1.0)
        add_group(group, material)
        add_face(group, [(bl, u00), (br, u10), (tr, u11)])
        add_face(group, [(bl, u00), (tr, u11), (tl, u01)])
        # backface
        add_face(group, [(bl, u00), (tr, u11), (br, u10)])
        add_face(group, [(bl, u00), (tl, u01), (tr, u11)])

    def add_pole_hex(group, material, cx, cy, cz, height, radius=0.06):
        add_group(group, material)
        n = 6
        rb = []; rt = []
        for k in range(n):
            a = (2 * math.pi * k) / n
            x = cx + math.cos(a) * radius
            y = cy + math.sin(a) * radius
            rb.append(add_v(x, y, cz))
            rt.append(add_v(x, y, cz + height))
        uvb = add_vt(0, 0); uvt = add_vt(0, 1)
        for k in range(n):
            k1 = (k + 1) % n
            add_face(group, [(rb[k], uvb), (rb[k1], uvb), (rt[k1], uvt)])
            add_face(group, [(rb[k], uvb), (rt[k1], uvt), (rt[k], uvt)])

    def add_cylinder(group, material, cx, cy, cz, radius, height,
                       axis="z", uv_tile=1.0):
        """Cilindro generico asse z (vertical) o x (horizontal per balla
        fieno). Bassa-poly (8 lati)."""
        add_group(group, material)
        n = 10
        if axis == "z":
            rb = []; rt = []
            for k in range(n):
                a = (2 * math.pi * k) / n
                x = cx + math.cos(a) * radius
                y = cy + math.sin(a) * radius
                rb.append(add_v(x, y, cz))
                rt.append(add_v(x, y, cz + height))
            # lato
            for k in range(n):
                k1 = (k + 1) % n
                u0 = add_vt(k / n, 0); u1 = add_vt((k+1) / n, 0)
                v0 = add_vt(k / n, 1); v1 = add_vt((k+1) / n, 1)
                add_face(group, [(rb[k], u0), (rb[k1], u1), (rt[k1], v1)])
                add_face(group, [(rb[k], u0), (rt[k1], v1), (rt[k], v0)])
        else:  # axis == "x" balla fieno sdraiata, asse lungo y_tangent
            # semplificazione: asse Y mondo, lunghezza = height, raggio = radius
            rb = []; rt = []
            for k in range(n):
                a = (2 * math.pi * k) / n
                y = cy + math.cos(a) * radius
                z = cz + radius + math.sin(a) * radius
                rb.append(add_v(cx - height * 0.5, y, z))
                rt.append(add_v(cx + height * 0.5, y, z))
            for k in range(n):
                k1 = (k + 1) % n
                u0 = add_vt(k / n, 0); u1 = add_vt((k+1) / n, 0)
                v0 = add_vt(k / n, uv_tile); v1 = add_vt((k+1) / n, uv_tile)
                add_face(group, [(rb[k], u0), (rb[k1], u1), (rt[k1], v1)])
                add_face(group, [(rb[k], u0), (rt[k1], v1), (rt[k], v0)])
            # tappi
            for (ring, x_cap) in [(rb, cx - height * 0.5),
                                     (rt, cx + height * 0.5)]:
                center_uv = add_vt(0.5, 0.5)
                c_idx = add_v(x_cap, cy, cz + radius)
                for k in range(n):
                    k1 = (k + 1) % n
                    add_face(group, [(c_idx, center_uv),
                                        (ring[k], center_uv),
                                        (ring[k1], center_uv)])

    def add_building_billboard(group, material, cx, cy, cz,
                                 heading, width, height):
        """Facciata edificio come pannello alto (billboard orientato).
        Face verso la strada con heading, retro non renderizzato (solo front).
        Per edifici lontani 50-150m, il billboard e' sufficiente."""
        # Front-only panel (2 triangoli), leggermente spesso per gestire
        # ombra
        tx = -math.sin(heading); ty = math.cos(heading)
        hw = width * 0.5
        bl = add_v(cx - tx * hw, cy - ty * hw, cz)
        br = add_v(cx + tx * hw, cy + ty * hw, cz)
        tr = add_v(cx + tx * hw, cy + ty * hw, cz + height)
        tl = add_v(cx - tx * hw, cy - ty * hw, cz + height)
        u00 = add_vt(0.0, 0.0); u10 = add_vt(1.0, 0.0)
        u11 = add_vt(1.0, 1.0); u01 = add_vt(0.0, 1.0)
        add_group(group, material)
        add_face(group, [(bl, u00), (br, u10), (tr, u11)])
        add_face(group, [(bl, u00), (tr, u11), (tl, u01)])
        # backface visibile (con stessa texture; accettabile per billboard)
        add_face(group, [(bl, u00), (tr, u11), (br, u10)])
        add_face(group, [(bl, u00), (tl, u01), (tr, u11)])

    # ---- PIAZZAMENTO CARTELLI video (t_sec dal video) ----
    POLE_R_SMALL = 0.055  # leggermente piu' visibile (era 0.045)
    POLE_H_STD = 2.4
    # Cartelli: sovradimensionati rispetto al reale (60-90cm) per essere
    # leggibili a distanza di guida. 1.4m disc, 1.5m triangolo, 1.2x1.5 rettang.
    DISC_SIZE = 1.40  # cartello circolare 140cm diametro (leggibile a 60m)
    TRI_SIZE = 1.50   # triangolo 150cm lato
    PANEL_WINTER_W = 1.20  # cartello pneumatici invernali 120x150cm
    PANEL_WINTER_H = 1.50

    t_signs = [
        (9,  "limit50",  "Limit50",  DISC_SIZE, DISC_SIZE, 1, 4.5),
        (39, "winter",   "Winter",   PANEL_WINTER_W, PANEL_WINTER_H, 1, 5.0),
        (82, "limit30",  "Limit30",  DISC_SIZE, DISC_SIZE, 1, 4.5),
        (91, "curveSx",  "CurveSx",  TRI_SIZE, TRI_SIZE, 1, 4.5),
    ]
    for (t_sec, mat_key, group_name, pw, ph, side, off) in t_signs:
        idx = idx_at_t(t_sec)
        (px, py, pz, heading, _tx, _ty) = place_side(idx, off, side=side)
        mat_map = {"limit50": "VidSignLimit50",
                   "winter": "VidSignWinter",
                   "limit30": "VidSignLimit30",
                   "curveSx": "VidSignCurveSx"}
        mat_name = mat_map[mat_key]
        add_pole_hex(f"{group_name}Pole", "VidSignPole",
                      px, py, pz, POLE_H_STD + ph, POLE_R_SMALL)
        add_panel_quad(f"{group_name}Panel", mat_name,
                        px, py, pz, heading, pw, ph,
                        z_bot=pz + POLE_H_STD,
                        z_top=pz + POLE_H_STD + ph)

    # ---- EDIFICI ICONICI (billboard facciate) ----
    # Rudere colombaia tonda - t28s, SX ~50m da asfalto
    rud_idx = idx_at_t(28)
    (rx, ry, rz, rh, _, _) = place_side(rud_idx, 50.0, side=-1)
    add_building_billboard("RudereColombaia", "VidBldgRudere",
                            rx, ry, rz, rh, width=18.0, height=11.0)
    # Casa-torre colombaia quadrata - t33s, SX ~50m
    tor_idx = idx_at_t(33)
    (tx_, ty_, tz_, th_, _, _) = place_side(tor_idx, 50.0, side=-1)
    add_building_billboard("CasaTorretta", "VidBldgTorretta",
                            tx_, ty_, tz_, th_, width=22.0, height=13.0)
    # Casale con cipressi - t103s, SX ~150m
    cas_idx = idx_at_t(103)
    (cx_, cy_, cz_, ch_, _, _) = place_side(cas_idx, 150.0, side=-1)
    add_building_billboard("CasaleCipressi", "VidBldgCasale",
                            cx_, cy_, cz_, ch_, width=26.0, height=10.0)

    # ---- BALLE DI FIENO (t88-90s DX campo arativo) ----
    import numpy as _np
    rng_h = _np.random.default_rng(7)
    count_hay = 0
    for t_sec in (86.0, 88.0, 89.5, 90.5, 92.0):
        hi = idx_at_t(t_sec)
        # offset casuale 25-60m a DX in campo, perpendicolare alla strada
        for k in range(3):
            off = 28.0 + float(rng_h.uniform(0, 30))
            (hx, hy, hz, hh, _, _) = place_side(hi + k * 2, off, side=+1)
            # Balla cilindrica sdraiata, asse ~ tangente strada, raggio 0.65m, lunghezza 1.3m
            add_cylinder("HayBale", "VidHay",
                          hx, hy, hz, radius=0.65, height=1.35,
                          axis="x", uv_tile=2.0)
            count_hay += 1

    # ---- DELINEATORI BIANCO-ROSSI EXTRA (t85-95s curve) ----
    count_del = 0
    for t_sec in range(85, 96):
        di = idx_at_t(float(t_sec))
        # 2 delineatori (SX e DX) con offset standard
        for side_d in (-1, +1):
            (dx_d, dy_d, dz_d, _, _, _) = place_side(di, 3.2, side=side_d)
            add_pole_hex(f"DelinExtra_{t_sec}_{side_d}",
                          "VidDelineator",
                          dx_d, dy_d, dz_d, 1.05, 0.035)
            count_del += 1

    print(f"  video landmarks: 4 cartelli + 3 edifici + {count_hay} "
          f"balle fieno + {count_del} delineatori")

    # ---- SCRITTURA OBJ + MTL ----
    obj_path = level_dir / "art" / "shapes" / "macerone_video.obj"
    mtl_path = obj_path.with_suffix(".mtl")
    lines = [f"mtllib {mtl_path.name}\n"]
    for (x, y, z) in verts:
        lines.append(f"v {x:.4f} {y:.4f} {z:.4f}\n")
    for (u, v) in uvs:
        lines.append(f"vt {u:.4f} {v:.4f}\n")
    for gname, subs in groups.items():
        lines.append(f"g {gname}\n")
        for (mat, faces) in subs:
            lines.append(f"usemtl {mat}\n")
            for face in faces:
                face_str = " ".join(f"{vi}/{ti}" for (vi, ti) in face)
                lines.append(f"f {face_str}\n")
    obj_path.write_text("".join(lines), encoding="utf-8")

    mtl_lines = []
    def _mtl(mat, kd, map_kd=None):
        mtl_lines.append(f"newmtl {mat}\n")
        mtl_lines.append(f"Kd {kd[0]:.3f} {kd[1]:.3f} {kd[2]:.3f}\n")
        mtl_lines.append("Ka 0 0 0\nKs 0 0 0\nNs 1\nillum 1\n")
        if map_kd:
            mtl_lines.append(f"map_Kd {map_kd}\n")
        mtl_lines.append("\n")
    _mtl("VidSignLimit50",  (1, 1, 1), f"video_landmarks/{limit50_png.name}")
    _mtl("VidSignLimit30",  (1, 1, 1), f"video_landmarks/{limit30_png.name}")
    _mtl("VidSignWinter",   (1, 1, 1), f"video_landmarks/{winter_png.name}")
    _mtl("VidSignCurveSx",  (1, 1, 1), f"video_landmarks/{curve_png.name}")
    _mtl("VidSignPole",     (0.42, 0.42, 0.44))
    _mtl("VidBldgRudere",   (1, 1, 1), f"video_landmarks/{rudere_png.name}")
    _mtl("VidBldgTorretta", (1, 1, 1), f"video_landmarks/{torretta_png.name}")
    _mtl("VidBldgCasale",   (1, 1, 1), f"video_landmarks/{casale_png.name}")
    _mtl("VidHay",          (0.78, 0.68, 0.42))
    _mtl("VidDelineator",   (0.92, 0.92, 0.88))
    mtl_path.write_text("".join(mtl_lines), encoding="utf-8")

    print(f"  video landmarks -> {obj_path.relative_to(MOD_DIR)} "
          f"({len(verts)} v, {sum(len(sub[1]) for subs in groups.values() for sub in subs)} f)")
    return obj_path


def generate_roadside_clutter(level_dir: Path,
                                  terrain_z_sampler=None) -> Path | None:
    """Clutter bordo strada condizionato su OSM + classificazione satellite:
    - bridge/tunnel: skip clutter (parapetti aggiunti separati)
    - Dist building < 30m: zona abitata, siepi regolari
    - Dentro poligono foresta OSM: alberi bassi extra
    - Satellite class "paved" (guardrail/muretto visibile): NO bush/rock,
      eventualmente un piccolo muretto procedurale
    - Satellite class "tree" (bosco): alberi procedurali extra
    - Satellite class "grass": clutter leggero standard
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

    # Ricerca z dal centerline piu' vicino (step 4 per perf)
    def nearest_z(x_q: float, y_q: float) -> float:
        dmin = float("inf"); zq = 0.0
        for i in range(0, len(cl), 4):
            d2 = (cl[i][0] - x_q) ** 2 + (cl[i][1] - y_q) ** 2
            if d2 < dmin:
                dmin = d2; zq = cl[i][2]
        return zq

    # Per prop piazzati lontano dalla centerline (siepi, muretti,
    # edifici OSM, boulders) usiamo il terrain_z reale dal mesh Blender
    # se disponibile, con fallback alla centerline. Evita prop fluttuanti
    # o affondati sulle scarpate/pendii.
    def ground_z(x_q: float, y_q: float) -> float:
        if terrain_z_sampler is not None:
            z_t = terrain_z_sampler(x_q, y_q)
            if z_t is not None:
                return z_t
        return nearest_z(x_q, y_q)

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

    # Carica classificazione satellite (road_conditions.json) se disponibile
    conditions_path = ROOT / "output" / "road_conditions.json"
    cond_by_idx: dict[int, dict] = {}
    if conditions_path.exists():
        payload = json.loads(conditions_path.read_text(encoding="utf-8"))
        for p in payload.get("points", []):
            cond_by_idx[p["index"]] = p
        print(f"  road_conditions.json: {len(cond_by_idx)} points classificati")

    rng = np.random.default_rng(1234)
    shapes_dir = level_dir / "art" / "shapes"
    shapes_dir.mkdir(parents=True, exist_ok=True)
    obj_path = shapes_dir / "macerone_roadside.obj"
    mtl_path = shapes_dir / "macerone_roadside.mtl"

    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[list[int], str]] = []
    # UV support: per face abbiamo una lista opzionale di indici vt.
    # Se None la face viene emessa come "f v v v" (senza UV). Se presente
    # come "f v/vt v/vt v/vt".
    uvs: list[tuple[float, float]] = []
    face_uvs: list[list[int] | None] = []
    # Posizioni alberi (x,y,z,height,angle_rad,context). Verranno scritte
    # come Forest4 json + managedItemData per rendering nativo BeamNG con
    # alpha test funzionante + LOD + instancing. Context e' un tag usato
    # per scegliere il tipo di albero vanilla (olive, oak, pine, ecc.).
    tree_positions: list[tuple[float, float, float, float, float, str]] = []

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
        """Prima generava tetraedro verde procedurale (i rombi visibili in
        gioco). Ora accumula semplicemente una posizione con contesto "bush":
        il Forest system la renderizzera' con fluffy_bush o generibush vanilla
        italy (mesh reali con alpha corretta). size -> altezza target.
        """
        angle = float(rng.uniform(0.0, 2 * math.pi))
        # size era il raggio base del tetraedro, l'altezza era size*1.5.
        # Mantengo la stessa altezza visiva mappando size -> height.
        h_target = max(0.8, size * 1.5)
        tree_positions.append((cx, cy, cz, h_target, angle, "bush", None, None))

    def add_tree(cx: float, cy: float, cz: float, height: float = 5.0,
                   context: str = "mixed",
                   specific_species: str | None = None,
                   angle: float | None = None,
                   scale_override: float | None = None):
        """Accumula posizione per il Forest system.
        - context: 'forest'/'orchard'/'roadside'/'mixed'/'bush'/'farmhouse'
          -> seleziona specie da MIX
        - specific_species: se valorizzato, forza quella specie (usato per
          OSM buildings dove vogliamo asset+orientation precisi)
        - angle: radianti; se None, random
        - scale_override: se valorizzato, bypassa il calcolo height/h0
        """
        if angle is None:
            angle = float(rng.uniform(0.0, 2 * math.pi))
        tree_positions.append((cx, cy, cz, height, angle, context,
                                 specific_species, scale_override))

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

    # Cammina lungo la centerline con step ~5m (prima 12): AMBIENTE PIENO.
    # I video GoPro SS17 mostrano filari densi + cespugli + olivi ovunque,
    # mai "vuoto verde piatto". Triplichiamo la densita' clutter.
    step_m = 5.0
    acc = 0.0
    last_x, last_y = cl[0][0], cl[0][1]
    side = 1
    count_rock = 0
    count_bush = 0
    count_tree_near = 0
    count_pole = 0
    count_skipped_bridge = 0

    # ---- Carica landscape_scenes.json (video GoPro -> density hint) ----
    # classify_landscape_scenes.py ha generato tree_left/right, openness per
    # 86 frame. Li mappo ad arc_length lineare su centerline (v1 = prima
    # meta', v2 = seconda meta'). Durante PASS 1 uso la scene piu' vicina
    # per modulare densita' alberi/cespugli: zone VIDEO openness alta
    # -> meno alberi (campo aperto), VIDEO tree_density alta -> piu' alberi.
    cl_arc = [0.0]
    for i in range(1, len(cl)):
        cl_arc.append(cl_arc[-1] + math.hypot(
            cl[i][0] - cl[i-1][0], cl[i][1] - cl[i-1][1]))
    total_len_clarc = cl_arc[-1] if cl_arc else 1.0

    scene_data: list[tuple[float, float, float]] = []  # (s_m, tree_lr, open)
    scenes_json = TOOLS / "landscape_scenes.json"
    if scenes_json.exists():
        try:
            raw_scenes = json.loads(scenes_json.read_text(encoding="utf-8"))
            v1_scenes = [s for s in raw_scenes if s["video"] == "v1"]
            v2_scenes = [s for s in raw_scenes if s["video"] == "v2"]
            v1_tmin = min(s["t_sec"] for s in v1_scenes) if v1_scenes else 34
            v1_tmax = max(s["t_sec"] for s in v1_scenes) if v1_scenes else 310
            v2_tmax = max(s["t_sec"] for s in v2_scenes) if v2_scenes else 228
            total_t = (v1_tmax - v1_tmin) + v2_tmax
            for s in raw_scenes:
                if s["video"] == "v1":
                    t_norm = (s["t_sec"] - v1_tmin) / max(total_t, 1)
                else:
                    t_norm = ((v1_tmax - v1_tmin) + s["t_sec"]) / max(total_t, 1)
                s_m = t_norm * total_len_clarc
                tree_lr = (s["tree_left"] + s["tree_right"]) / 2.0
                scene_data.append((s_m, tree_lr, s["openness"]))
            scene_data.sort()
            print(f"  landscape_scenes caricate: {len(scene_data)} frame "
                  f"mappati su {total_len_clarc:.0f}m centerline")
        except Exception as e:
            print(f"  WARN landscape_scenes.json: {e}")

    def scene_at_arc(s_q: float) -> tuple[float, float]:
        """(tree_lr_density, openness) scena video piu' vicina ad arc_length."""
        if not scene_data:
            return 0.3, 0.5
        # Binary search O(log n)
        lo, hi = 0, len(scene_data) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if scene_data[mid][0] < s_q:
                lo = mid + 1
            else:
                hi = mid
        cand = [scene_data[lo]]
        if lo > 0:
            cand.append(scene_data[lo - 1])
        best = min(cand, key=lambda e: abs(e[0] - s_q))
        return best[1], best[2]

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

    cl_idx = 0
    for (x, y, z, br, tu) in cl[1:]:
        cl_idx += 1
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

        dist_b = dist_to_nearest_building(x, y)
        is_forested = in_forest(x, y)
        cond = cond_by_idx.get(cl_idx, {})
        sat_left = cond.get("left_near", "grass")
        sat_right = cond.get("right_near", "grass")
        # Video scene hint: modulazione densita'.
        # tree_lr > 0.15 = bosco/filari densi nel video
        # openness > 0.75 = campo aperto nel video
        scene_tree_lr, scene_open = scene_at_arc(
            cl_arc[cl_idx] if cl_idx < len(cl_arc) else 0.0)

        def place_for_side(side_sign: int, sat_class: str, offset_min: float,
                             offset_max: float):
            """Piazza clutter su un lato (+1 sx, -1 dx) in base alla classe
            satellite e al contesto (zona abitata/forestata).
            offset_min/max: range distanza dal centro strada (allargato
            per riempire il paesaggio in profondita', non solo sul ciglio).
            """
            nonlocal count_rock, count_bush, count_tree_near
            # Se satellite vede "paved" -> skip clutter naturale (gia' c'e'
            # guardrail/muretto/banchina). Niente cespugli selvaggi.
            if sat_class == "paved":
                return
            # Clearance minima dalla centerline (road half + canopy margin):
            # road ~4m half + 1.5m chioma = 5.5m. Alzo qui offset_min se
            # il chiamante lo ha passato basso (retro-compat con chiamate
            # 3.5m che causavano alberi IN CARREGGIATA).
            MIN_OFFSET_FROM_AXIS = 5.5
            offset_eff_min = max(offset_min, MIN_OFFSET_FROM_AXIS)
            offset_eff_max = max(offset_max, offset_eff_min + 1.0)
            offset_abs = rng.uniform(offset_eff_min, offset_eff_max)
            # Riduco sigma del noise da 0.5 -> 0.3 cosi' il 3sigma non
            # spinge mai piu' di 0.9m dentro la carreggiata.
            ox = x + nx * offset_abs * side_sign + rng.normal(0, 0.3)
            oy = y + ny * offset_abs * side_sign + rng.normal(0, 0.3)
            oz = z - 0.1
            # Safety HARD: se per qualsiasi motivo il punto e' finito
            # entro 4.5m dall'asse (approx bordo asfalto), skip.
            dx_safety = ox - x; dy_safety = oy - y
            if (dx_safety * dx_safety + dy_safety * dy_safety) < 4.5 * 4.5:
                return
            if dist_b < 30.0:
                # Zona abitata: siepi + qualche cespuglio vario
                add_bush(ox, oy, oz, rng.uniform(0.35, 0.65))
                count_bush += 1
            elif sat_class == "tree" or is_forested:
                # Bosco: 50/30/20 cespugli/alberi/sassi per ambiente denso
                r01 = rng.random()
                if r01 < 0.50:
                    add_bush(ox, oy, oz, rng.uniform(0.50, 1.10))
                    count_bush += 1
                elif r01 < 0.80:
                    add_tree(ox, oy, oz, height=rng.uniform(3.5, 7.0),
                              context="forest")
                    count_tree_near += 1
                else:
                    add_rock(ox, oy, oz, rng.uniform(0.30, 0.60))
                    count_rock += 1
            else:
                # Grass/open: misto cespugli+sassi+qualche albero sparso
                r01 = rng.random()
                if r01 < 0.55:
                    add_bush(ox, oy, oz, rng.uniform(0.30, 0.70))
                    count_bush += 1
                elif r01 < 0.80:
                    add_rock(ox, oy, oz, rng.uniform(0.25, 0.55))
                    count_rock += 1
                else:
                    # Olivo/albero isolato nel prato
                    add_tree(ox, oy, oz, height=rng.uniform(2.8, 4.5),
                              context="orchard")
                    count_tree_near += 1

        # Modula densita' clutter in base al VIDEO: se il frame GoPro
        # piu' vicino e' "openness" >0.75 (campo aperto), riduco i passaggi
        # e sovrascrivo OSM forest (che puo' essere obsoleto). Se tree_lr
        # alto, aumento densita' per riflettere bosco video reale.
        if scene_open > 0.80:
            near_passes = 1
            mid_passes = 1
            # Override: in zona VIDEO-open, ignora forest OSM se presente
            if is_forested or sat_left == "tree" or sat_right == "tree":
                is_forested = False
                if sat_left == "tree": sat_left = "grass"
                if sat_right == "tree": sat_right = "grass"
        elif scene_open > 0.65:
            near_passes = 2
            mid_passes = 2
        elif scene_tree_lr > 0.18:
            near_passes = 4    # bosco denso nel video -> 4 passate (+33%)
            mid_passes = 3
        else:
            near_passes = 3    # default
            mid_passes = 2
        # Fascia vicina (3.5-8m): clutter denso ciglio strada
        for _ in range(near_passes):
            place_for_side(+1, sat_left,  3.5, 8.0)
            place_for_side(-1, sat_right, 3.5, 8.0)
        # Fascia media (8-18m): alberi/cespugli rari per profondita' scena
        for _ in range(mid_passes):
            place_for_side(+1, sat_left,  8.0, 18.0)
            place_for_side(-1, sat_right, 8.0, 18.0)
        # Zone tree (bosco): +4 alberi veri tronco+chioma per lato (prima 2)
        # Offset minimo 7m per lasciare spazio a canopia (era 5m -> alberi
        # con chioma 3m finivano a 2m dall'asse = sopra strada).
        if sat_left == "tree" or is_forested:
            for _ in range(4):
                offset = rng.uniform(7.0, 22.0)
                ox = x + nx * offset + rng.normal(0, 0.8)
                oy = y + ny * offset + rng.normal(0, 0.8)
                # Safety: skip se finito troppo vicino all'asse
                dd2 = (ox - x) ** 2 + (oy - y) ** 2
                if dd2 < 6.0 * 6.0:
                    continue
                oz = z - 0.15
                add_tree(ox, oy, oz, height=rng.uniform(5.0, 9.0),
                          context="forest")
                count_tree_near += 1
        if sat_right == "tree" or is_forested:
            for _ in range(4):
                offset = rng.uniform(-22.0, -7.0)
                ox = x + nx * offset + rng.normal(0, 0.8)
                oy = y + ny * offset + rng.normal(0, 0.8)
                dd2 = (ox - x) ** 2 + (oy - y) ** 2
                if dd2 < 6.0 * 6.0:
                    continue
                oz = z - 0.15
                add_tree(ox, oy, oz, height=rng.uniform(5.0, 9.0),
                          context="forest")
                count_tree_near += 1

    # ---- PASS 2: bosco distante (riempi forest polygons OSM) ----
    # Alberi sparsi nell'area interna di ogni forest polygon, NON solo
    # sul ciglio strada. Popola le colline visibili in fondo, in modo
    # che il paesaggio non sembri un prato vuoto infinito.
    # Usiamo gli stessi forests_bbox gia' calcolati.
    # Densita' media: ~1 albero ogni 14m x 14m = ~50 trees / ha.
    # Skip se troppo vicino alla strada (< 25m) per non doppiare con
    # il pass 1 sopra. Skip buildings polygon (non alberi in casa).
    def dist_to_road_axis(x_q: float, y_q: float) -> float:
        """Distanza euclidea 2D al segmento centerline piu' vicino."""
        dmin2 = float("inf")
        # sampling ogni 4-5 punti per velocita'
        for i in range(0, len(cl), 4):
            cx_, cy_ = cl[i][0], cl[i][1]
            d2 = (cx_ - x_q) ** 2 + (cy_ - y_q) ** 2
            if d2 < dmin2:
                dmin2 = d2
        return math.sqrt(dmin2)

    count_forest_trees = 0
    forest_grid = 14.0
    for (x0, y0, x1, y1) in forests_bbox:
        # Limita bbox molto grandi (> 400m lato) per non generare migliaia
        # di alberi e sfondare la memoria OBJ.
        w = min(x1 - x0, 400.0)
        h = min(y1 - y0, 400.0)
        x_end = x0 + w
        y_end = y0 + h
        xv = x0
        while xv < x_end:
            yv = y0
            while yv < y_end:
                # Jitter stocastico nella cella
                fx = xv + rng.uniform(0, forest_grid) + rng.normal(0, 1.5)
                fy = yv + rng.uniform(0, forest_grid) + rng.normal(0, 1.5)
                # Skip se troppo vicino alla strada (gia' popolato)
                if dist_to_road_axis(fx, fy) < 25.0:
                    yv += forest_grid
                    continue
                # Skip se dentro/vicino a un edificio
                if dist_to_nearest_building(fx, fy) < 8.0:
                    yv += forest_grid
                    continue
                # Z: usa la z della centerline piu' vicina come approssimazione
                best_dz = float("inf"); fz = 0.0
                for i in range(0, len(cl), 8):
                    d2 = (cl[i][0] - fx) ** 2 + (cl[i][1] - fy) ** 2
                    if d2 < best_dz:
                        best_dz = d2; fz = cl[i][2]
                # Skip 30% random per non saturare + varia size
                if rng.random() < 0.30:
                    yv += forest_grid
                    continue
                add_tree(fx, fy, fz - 0.15, height=rng.uniform(4.5, 9.5),
                          context="forest")
                count_forest_trees += 1
                yv += forest_grid
            xv += forest_grid

    # ---- PASS 2.5: casolari rurali lungo strada ----
    # Ogni ~180m sulla centerline: se il lato e' aperto (grass, no forest,
    # no building OSM vicino entro 50m), piazzo un farmhouse o shed a
    # 22-40m dal centerline. Crea l'effetto "casa sparsa sulla collina"
    # tipico dei video SS17.
    count_farmhouse = 0
    farm_step_m = 180.0
    farm_acc = 0.0
    last_fx, last_fy = cl[0][0], cl[0][1]
    farm_side = +1
    for (x, y, z, br, tu) in cl[1:]:
        dx_f = x - last_fx; dy_f = y - last_fy
        last_fx, last_fy = x, y
        d_f = math.hypot(dx_f, dy_f)
        farm_acc += d_f
        if farm_acc < farm_step_m:
            continue
        farm_acc = 0.0
        if br or tu:
            continue
        # Non piazzare in bosco fitto (farmhouse e' edificio, non sta nel bosco)
        if in_forest(x, y):
            continue
        # Non piazzare se c'e' gia' un edificio OSM entro 50m (doppione)
        if dist_to_nearest_building(x, y) < 50.0:
            continue
        nxf, nyf = -dy_f / d_f, dx_f / d_f
        offset = rng.uniform(22.0, 38.0) * farm_side
        fhx = x + nxf * offset + rng.normal(0, 1.5)
        fhy = y + nyf * offset + rng.normal(0, 1.5)
        # Z dal terrain reale (fallback centerline) — casolare sparso e'
        # 22-38m dalla strada, il terrain puo' essere significativamente
        # sopra/sotto la centerline.
        fhz = ground_z(fhx, fhy) - 0.25
        # add_tree con context farmhouse -> Forest lo piazzera' come farmhouse
        add_tree(fhx, fhy, fhz, height=7.5, context="farmhouse")
        count_farmhouse += 1
        farm_side = -farm_side

    # ---- PASS 2.6: OSM buildings -> asset italy vanilla ----
    # Sostituisce TUTTI gli edifici procedurali (cubi grigi) con farmhouse /
    # shed / ind_bld italy scelti in base alla dimensione del poligono OSM.
    # L'angolo dell'asset e' allineato all'asse principale del poligono (PCA)
    # cosi' le case sono orientate "come in realta'".
    # ASSET_BUILDINGS e ASSET_AREA sono definiti in write_forest_system (SPECIES).
    ASSET_AREA_OSM = {
        "shed_c": 20, "shed_d": 20, "shed_a": 25, "shed_e": 25,
        "shed_b": 30, "shed_f": 30,
        "ind_bld_8x8": 64,
        "farmhouse_d": 80, "farmhouse_e": 90, "farmhouse_g": 95,
        "farmhouse_h": 110, "farmhouse_i": 110,
        "ind_bld_12x10": 120, "farmhouse_b": 130, "farmhouse_j": 130,
        "farmhouse_a": 140, "farmhouse_c": 140, "ind_bld_12x12": 144,
        "farmhouse_f": 150,
        "ind_bld_12x15": 180, "ind_bld_12x20": 240,
    }
    ASSET_H0_OSM = {
        "shed_c": 3.5, "shed_d": 3.5, "shed_a": 4.0, "shed_e": 3.5,
        "shed_b": 4.0, "shed_f": 3.5,
        "ind_bld_8x8": 5.0,
        "farmhouse_d": 7.5, "farmhouse_e": 7.0, "farmhouse_g": 7.5,
        "farmhouse_h": 7.5, "farmhouse_i": 7.5,
        "ind_bld_12x10": 6.0, "farmhouse_b": 8.0, "farmhouse_j": 8.0,
        "farmhouse_a": 8.0, "farmhouse_c": 8.0, "ind_bld_12x12": 6.0,
        "farmhouse_f": 8.5,
        "ind_bld_12x15": 6.5, "ind_bld_12x20": 7.0,
    }
    # Pre-sort per matching efficiente per area
    _sorted_assets = sorted(ASSET_AREA_OSM.items(), key=lambda kv: kv[1])

    def pick_building_asset(poly_area: float, aspect_ratio: float) -> str:
        """Sceglie asset italy con area piu' vicina, preferisce shed se molto
        piccolo, ind_bld se molto allungato/grande, farmhouse per il resto."""
        if poly_area < 35:
            # tettoia / casolare piccolo
            candidates = [k for k, v in _sorted_assets if 15 <= v <= 45]
        elif poly_area < 70:
            candidates = [k for k, v in _sorted_assets if 30 <= v <= 85]
        elif poly_area < 160:
            # Casa tipica. Se molto allungato -> ind_bld o farmhouse lungo
            if aspect_ratio > 2.0:
                candidates = [k for k, v in _sorted_assets
                               if 80 <= v <= 180 and ("ind_bld" in k or k in ("farmhouse_f",))]
                if not candidates:
                    candidates = [k for k, v in _sorted_assets if 80 <= v <= 180]
            else:
                candidates = [k for k, v in _sorted_assets
                               if 80 <= v <= 180 and k.startswith("farmhouse")]
        else:
            # Grande: ind_bld o farmhouse_f (il piu' grande)
            candidates = [k for k, v in _sorted_assets
                           if v >= 140 and ("ind_bld" in k or k == "farmhouse_f")]
        if not candidates:
            candidates = [kv[0] for kv in _sorted_assets]
        return candidates[rng.integers(0, len(candidates))]

    def polygon_area_and_pca(coords_xy: list[tuple[float, float]]):
        """Ritorna (area_m2, centroid_xy, angle_rad, aspect_ratio)."""
        n = len(coords_xy)
        if n < 3:
            return 0.0, (0.0, 0.0), 0.0, 1.0
        # Shoelace area + centroide pesato
        area2 = 0.0
        cx_s = 0.0
        cy_s = 0.0
        for i in range(n):
            x0_, y0_ = coords_xy[i]
            x1_, y1_ = coords_xy[(i + 1) % n]
            cross = x0_ * y1_ - x1_ * y0_
            area2 += cross
            cx_s += (x0_ + x1_) * cross
            cy_s += (y0_ + y1_) * cross
        area = abs(area2) * 0.5
        if area < 1e-3:
            # fallback: media aritmetica
            cx_m = sum(p[0] for p in coords_xy) / n
            cy_m = sum(p[1] for p in coords_xy) / n
            return area, (cx_m, cy_m), 0.0, 1.0
        cx_c = cx_s / (3.0 * area2)
        cy_c = cy_s / (3.0 * area2)
        # PCA: covariance sui punti centrati
        sxx = 0.0; syy = 0.0; sxy = 0.0
        for (px_, py_) in coords_xy:
            dx_ = px_ - cx_c
            dy_ = py_ - cy_c
            sxx += dx_ * dx_
            syy += dy_ * dy_
            sxy += dx_ * dy_
        # Autovalori/autovettori 2x2
        trace = sxx + syy
        det = sxx * syy - sxy * sxy
        disc = max(0.0, trace * trace * 0.25 - det)
        lam1 = trace * 0.5 + math.sqrt(disc)
        lam2 = trace * 0.5 - math.sqrt(disc)
        # Autovettore di lam1
        if abs(sxy) > 1e-6:
            ang = math.atan2(lam1 - sxx, sxy)
        else:
            ang = 0.0 if sxx >= syy else math.pi * 0.5
        ratio = math.sqrt(max(lam1 / max(lam2, 1e-6), 1.0))
        return area, (cx_c, cy_c), ang, ratio

    # Ricerca punto piu' vicino SUL segmento di centerline + distanza
    # (per push perpendicolare corretto anche tra due vertici).
    def nearest_cl_point(x_q: float, y_q: float):
        dmin = float("inf")
        bpx = x_q; bpy = y_q; bpz = 0.0
        for i in range(len(cl) - 1):
            x0_ = cl[i][0]; y0_ = cl[i][1]; z0_ = cl[i][2]
            x1_ = cl[i + 1][0]; y1_ = cl[i + 1][1]; z1_ = cl[i + 1][2]
            dx_ = x1_ - x0_; dy_ = y1_ - y0_
            seg2 = dx_ * dx_ + dy_ * dy_
            if seg2 < 1e-6:
                continue
            t = ((x_q - x0_) * dx_ + (y_q - y0_) * dy_) / seg2
            if t < 0.0:
                t = 0.0
            elif t > 1.0:
                t = 1.0
            px_ = x0_ + t * dx_
            py_ = y0_ + t * dy_
            pz_ = z0_ + t * (z1_ - z0_)
            d2 = (px_ - x_q) ** 2 + (py_ - y_q) ** 2
            if d2 < dmin:
                dmin = d2; bpx = px_; bpy = py_; bpz = pz_
        return (bpx, bpy, bpz, math.sqrt(dmin))

    # Distanza minima punto-qualsiasi -> asse strada, usando distanza
    # PUNTO-SEGMENTO (non solo punto-vertice) cosi' non c'e' gap tra i
    # vertici della centerline che lasci passare angoli edificio.
    def min_dist_to_centerline(x_q: float, y_q: float) -> float:
        dmin2 = float("inf")
        for i in range(len(cl) - 1):
            x0_ = cl[i][0]; y0_ = cl[i][1]
            x1_ = cl[i + 1][0]; y1_ = cl[i + 1][1]
            dx_ = x1_ - x0_; dy_ = y1_ - y0_
            seg2 = dx_ * dx_ + dy_ * dy_
            if seg2 < 1e-6:
                continue
            t = ((x_q - x0_) * dx_ + (y_q - y0_) * dy_) / seg2
            if t < 0.0:
                t = 0.0
            elif t > 1.0:
                t = 1.0
            px_ = x0_ + t * dx_
            py_ = y0_ + t * dy_
            d2 = (px_ - x_q) * (px_ - x_q) + (py_ - y_q) * (py_ - y_q)
            if d2 < dmin2:
                dmin2 = d2
        return math.sqrt(dmin2)

    # Distanza asse strada -> muro asfalto: ~4m corsia + 0.5m banchina +
    # 2.5m margine sicurezza extra = 7.0m. Il raggio dell'asset
    # (mezza diagonale) si aggiunge sopra cio'.
    ROAD_EDGE_PLUS_MARGIN = 7.0
    # Distanza MINIMA ammessa da qualsiasi angolo del footprint al piu'
    # vicino SEGMENTO della centerline (non solo vertici) -- se un angolo
    # cade piu' vicino, l'edificio e' sulla strada -> skip o push.
    CORNER_MIN_TO_AXIS = 7.0

    # Dimensioni reali (L, W) dei DAE italy -- servono a calcolare il
    # footprint ruotato invece di usare sqrt(area/2). Il primo valore e'
    # la dimensione lungo il lato LUNGO (si suppone allineato a ang_b
    # tramite PCA del poligono OSM).
    # NB: per gli ind_bld_* i numeri includono il piazzale/cortile di
    # cemento che e' integrato nel DAE (warehouse industriali hanno loading
    # yard ~6-8m oltre il muro). Per evitare che il piazzale invada la
    # strada, dichiariamo L/W piu' grandi del muro fisico.
    ASSET_DIMS = {
        "shed_a": (6.0, 4.0), "shed_b": (7.0, 4.5),
        "shed_c": (5.0, 4.0), "shed_d": (5.5, 3.8),
        "shed_e": (5.5, 4.5), "shed_f": (7.5, 4.0),
        # ind_bld_* : L,W = wall + 8m per lato di cortile integrato
        "ind_bld_8x8":   (8.0 + 16.0,  8.0 + 16.0),
        "ind_bld_12x10": (12.0 + 16.0, 10.0 + 16.0),
        "ind_bld_12x12": (12.0 + 16.0, 12.0 + 16.0),
        "ind_bld_12x15": (15.0 + 16.0, 12.0 + 16.0),
        "ind_bld_12x20": (20.0 + 16.0, 12.0 + 16.0),
        "farmhouse_a": (14.0, 10.0), "farmhouse_b": (13.0, 10.0),
        "farmhouse_c": (14.0, 10.0), "farmhouse_d": (11.0, 7.5),
        "farmhouse_e": (12.0, 7.5), "farmhouse_f": (15.0, 10.0),
        "farmhouse_g": (12.5, 7.5), "farmhouse_h": (13.0, 8.5),
        "farmhouse_i": (13.0, 8.5), "farmhouse_j": (13.0, 10.0),
    }

    def asset_half_diag(species_name: str) -> float:
        """Mezza-diagonale (metri) dell'asset piazzato. Per ind_bld_*
        include il cortile integrato, cosi' la clearance tiene conto
        del piazzale che esce oltre i muri."""
        dims = ASSET_DIMS.get(species_name)
        if dims is not None:
            L, W = dims
            return math.hypot(L, W) * 0.5
        a = ASSET_AREA_OSM.get(species_name, 80.0)
        return math.sqrt(max(a, 1.0) * 0.5)

    def rotated_corners(cx_: float, cy_: float, L: float, W: float,
                         ang: float):
        """4 angoli di un rettangolo L x W centrato in (cx,cy), ruotato
        di ang radianti (asse lungo L allineato a (cos ang, sin ang))."""
        hl = L * 0.5
        hw = W * 0.5
        ca = math.cos(ang); sa = math.sin(ang)
        pts = []
        for (dl, dw) in ((+hl, +hw), (+hl, -hw), (-hl, +hw), (-hl, -hw)):
            x_ = cx_ + dl * ca - dw * sa
            y_ = cy_ + dl * sa + dw * ca
            pts.append((x_, y_))
        return pts

    def footprint_clear_of_road(cx_: float, cy_: float, species: str,
                                  ang: float) -> bool:
        """True se tutti e 4 gli angoli ruotati dell'asset (incluso
        cortile integrato per ind_bld_*) sono abbastanza lontani dal
        piu' vicino SEGMENTO della centerline. Campiona anche punti
        lungo i 4 lati del rettangolo cosi' e' impossibile che un
        segmento intero del footprint "tagli" la strada tra due vertici."""
        dims = ASSET_DIMS.get(species)
        if dims is None:
            hd = asset_half_diag(species)
            dims = (hd * 2.0, hd * 2.0)
        L, W = dims
        corners = rotated_corners(cx_, cy_, L + 1.0, W + 1.0, ang)
        # Check vertici
        for (x_, y_) in corners:
            if min_dist_to_centerline(x_, y_) < CORNER_MIN_TO_AXIS:
                return False
        # Check midpoints dei 4 lati del footprint (caso angolo lontano
        # ma lato che sfiora la strada).
        for i in range(4):
            a = corners[i]
            b = corners[(i + 1) % 4]
            mx_ = (a[0] + b[0]) * 0.5
            my_ = (a[1] + b[1]) * 0.5
            if min_dist_to_centerline(mx_, my_) < CORNER_MIN_TO_AXIS:
                return False
        return True

    count_osm_buildings = 0
    count_osm_skip_tiny = 0
    count_osm_skip_near_road = 0
    count_osm_skip_corner_in_road = 0
    count_osm_pushed = 0
    for b in rd.get("buildings", []):
        cds = b.get("coords", [])
        if len(cds) < 3:
            continue
        coords_xy = [project(c[0], c[1]) for c in cds]
        area, (cx_b, cy_b), ang_b, asp = polygon_area_and_pca(coords_xy)
        if area < 8.0:
            count_osm_skip_tiny += 1
            continue

        species_pick = pick_building_asset(area, asp)
        half_diag = asset_half_diag(species_pick)
        # Clearance richiesta: dalla centerline al centroide dell'asset,
        # l'asset deve stare completamente fuori dalla carreggiata.
        required_clearance = ROAD_EDGE_PLUS_MARGIN + half_diag

        (pnx, pny, pnz, d_to_cl) = nearest_cl_point(cx_b, cy_b)

        # Se il centroide e' dentro la carreggiata (< 2.5m dall'asse)
        # e' quasi certamente OSM bugged (poligono mal mappato o la
        # strada ricostruita taglia l'edificio). Skip: non pushiamo,
        # perche' non sappiamo su che lato buttarlo senza ambiguita'.
        if d_to_cl < 2.5:
            count_osm_skip_near_road += 1
            continue

        # Se il centroide e' fuori dalla carreggiata ma entro la
        # clearance richiesta -> PUSH perpendicolare alla centerline
        # per spingere l'asset fuori dalla strada, mantenendo la
        # sua posizione OSM "dal lato giusto".
        if d_to_cl < required_clearance:
            dx = cx_b - pnx
            dy = cy_b - pny
            norm = math.hypot(dx, dy)
            if norm < 1e-3:
                # centroide troppo vicino al punto strada per decidere lato
                count_osm_skip_near_road += 1
                continue
            ux = dx / norm
            uy = dy / norm
            cx_b = pnx + ux * required_clearance
            cy_b = pny + uy * required_clearance
            count_osm_pushed += 1

        # Verifica footprint ruotato: controlla che nessun angolo
        # dell'asset (gia' ruotato di ang_b) cada sulla carreggiata.
        # Se entrambe le orientazioni (ang e ang+90) hanno almeno un
        # angolo sulla strada, skip. Altrimenti usa quella che
        # funziona (preferenza per ang_b = PCA poligono).
        final_angle = ang_b
        ok_ang = footprint_clear_of_road(cx_b, cy_b, species_pick, ang_b)
        if not ok_ang:
            ang_alt = ang_b + math.pi * 0.5
            if footprint_clear_of_road(cx_b, cy_b, species_pick, ang_alt):
                final_angle = ang_alt
            else:
                # Provo a spingere ulteriormente l'edificio lontano
                # dalla strada finche' gli angoli escono dalla carreggiata.
                # 8 tentativi fino a +20m dal posto originale (passo 2.5m).
                # Ricalcolo pn a ogni step cosi' la perpendicolare resta
                # corretta anche se il punto piu' vicino cambia.
                saved = False
                for step in range(1, 9):
                    push_dist = step * 2.5
                    dx = cx_b - pnx; dy = cy_b - pny
                    norm = math.hypot(dx, dy)
                    if norm < 1e-3:
                        break
                    ux = dx / norm; uy = dy / norm
                    cx_try = cx_b + ux * push_dist
                    cy_try = cy_b + uy * push_dist
                    if footprint_clear_of_road(cx_try, cy_try,
                                                 species_pick, ang_b):
                        cx_b, cy_b = cx_try, cy_try
                        final_angle = ang_b
                        count_osm_pushed += 1
                        saved = True
                        break
                    if footprint_clear_of_road(cx_try, cy_try,
                                                 species_pick,
                                                 ang_b + math.pi * 0.5):
                        cx_b, cy_b = cx_try, cy_try
                        final_angle = ang_b + math.pi * 0.5
                        count_osm_pushed += 1
                        saved = True
                        break
                if not saved:
                    count_osm_skip_corner_in_road += 1
                    continue

        h_target = ASSET_H0_OSM.get(species_pick, 6.0)
        # Scale esatto 1.0 (gli asset italy sono gia' nella giusta scala).
        # Usiamo scale_override=1.0 cosi' evitiamo ridimensionamenti basati
        # su h_target che sfigurerebbero i DAE.
        # Z dal terrain reale (se sampler disponibile) cosi' l'edificio
        # appoggia sul terreno invece di fluttuare o affondare.
        cz_b = ground_z(cx_b, cy_b) - 0.3
        add_tree(cx_b, cy_b, cz_b, height=h_target,
                  context="osm_building",
                  specific_species=species_pick,
                  angle=final_angle,
                  scale_override=1.0)
        count_osm_buildings += 1
    print(f"  OSM buildings -> asset italy: {count_osm_buildings} "
          f"(skip {count_osm_skip_tiny} tiny, "
          f"{count_osm_skip_near_road} sulla strada, "
          f"{count_osm_skip_corner_in_road} corner-in-road, "
          f"{count_osm_pushed} pushed lontani da asse)")

    # ---- PASS 2.8: cypress hedges + stone walls vicino a edifici OSM ----
    # Quando c'e' un edificio OSM, il real-world italiano spesso ha:
    # - siepe di cipressi (cypress_hedge_3m) 15-25m davanti lato strada
    # - muretto a secco (italy_wall_stone_short) intorno al giardino
    # - recinzione metallica lungo lato campo
    count_hedge = 0
    count_wall = 0
    count_fence = 0
    for b in rd.get("buildings", []):
        cds = b.get("coords", [])
        if len(cds) < 3:
            continue
        coords_xy = [project(c[0], c[1]) for c in cds]
        area, (cx_b, cy_b), ang_b, asp = polygon_area_and_pca(coords_xy)
        if area < 30:
            continue  # skip tiny + shed (non hanno siepi)
        # 50% di probabilita' di avere una siepe di cipressi davanti
        (pnx, pny, _, d_to_cl) = nearest_cl_point(cx_b, cy_b)
        if d_to_cl > 8.0 and d_to_cl < 80.0 and rng.random() < 0.5:
            # direzione centroide→strada (per mettere siepe tra casa e strada)
            dx_cs = pnx - cx_b
            dy_cs = pny - cy_b
            norm_cs = math.hypot(dx_cs, dy_cs)
            if norm_cs > 1e-3:
                ux = dx_cs / norm_cs
                uy = dy_cs / norm_cs
                # perpendicolare (per aliniare la siepe parallelamente alla strada)
                pxh = -uy; pyh = ux
                # posiziona siepe a 40-60% del tragitto casa→strada
                t_along = rng.uniform(0.40, 0.60)
                hx0 = cx_b + ux * d_to_cl * t_along
                hy0 = cy_b + uy * d_to_cl * t_along
                # hedge lungo 3m, piazzo 2-4 segmenti consecutivi per filare
                n_seg = rng.integers(2, 5)
                for k in range(n_seg):
                    ofs = (k - (n_seg - 1) / 2) * 3.0
                    hx = hx0 + pxh * ofs
                    hy = hy0 + pyh * ofs
                    hz = ground_z(hx, hy) - 0.15
                    # verifica clearance dalla strada (siepe non in carreggiata)
                    (_, _, _, d_h) = nearest_cl_point(hx, hy)
                    if d_h < 6.0:
                        continue
                    hedge_ang = math.atan2(pyh, pxh)
                    add_tree(hx, hy, hz, height=3.0,
                              context="prop",
                              specific_species="cypress_hedge_3m",
                              angle=hedge_ang,
                              scale_override=1.0)
                    count_hedge += 1
        # 30% probabilita' di muretto a secco laterale (confine orto/giardino)
        if area > 60 and rng.random() < 0.3:
            # piazzo 3-5 segmenti muretto a secco perpendicolari alla strada
            n_wall = rng.integers(3, 6)
            wall_ang = ang_b + math.pi / 2
            wx_dir = math.cos(wall_ang)
            wy_dir = math.sin(wall_ang)
            # punto di partenza: lato del poligono lontano dalla strada
            dx_cs = cx_b - pnx
            dy_cs = cy_b - pny
            norm_cs = math.hypot(dx_cs, dy_cs)
            if norm_cs > 1e-3:
                ux_far = dx_cs / norm_cs
                uy_far = dy_cs / norm_cs
                wx0 = cx_b + ux_far * 8.0
                wy0 = cy_b + uy_far * 8.0
                for k in range(n_wall):
                    ofs = k * 2.5
                    wx = wx0 + wx_dir * ofs
                    wy = wy0 + wy_dir * ofs
                    (_, _, _, d_w) = nearest_cl_point(wx, wy)
                    if d_w < 7.0:
                        break  # stop filare se entra in zona strada
                    wz = ground_z(wx, wy) - 0.15
                    species_wall = picker.choice([
                        "italy_wall_stone_short", "italy_wall_stone_individual",
                        "italy_rock_wall",
                    ]) if False else (
                        "italy_wall_stone_short" if rng.random() < 0.6
                        else "italy_rock_wall"
                    )
                    add_tree(wx, wy, wz, height=1.0,
                              context="prop",
                              specific_species=species_wall,
                              angle=wall_ang,
                              scale_override=1.0)
                    count_wall += 1
    print(f"  props italy dopo edifici OSM: {count_hedge} siepi cipressi, "
          f"{count_wall} muretti a secco, {count_fence} recinzioni")

    # ---- PASS 2.9: boulders sparsi in zone forest (scarpate mountain pass) ----
    # Piazza italy_rockface_boulder_* in cluster sparsi dentro forest polygons,
    # a distanza > 15m dalla strada per non invadere carreggiata.
    count_boulder = 0
    boulder_grid = 45.0  # meno densi dei tree
    boulder_species = ["italy_boulder_1", "italy_boulder_2", "italy_boulder_3",
                        "italy_rockface_small_1", "italy_rockface_small_2",
                        "italy_rockface_small_3"]
    for (x0, y0, x1, y1) in forests_bbox[:40]:  # primi 40 bosco (perf)
        w = min(x1 - x0, 300.0)
        h = min(y1 - y0, 300.0)
        x_end = x0 + w; y_end = y0 + h
        xv = x0
        while xv < x_end:
            yv = y0
            while yv < y_end:
                fx = xv + rng.uniform(0, boulder_grid)
                fy = yv + rng.uniform(0, boulder_grid)
                yv += boulder_grid
                if not in_forest(fx, fy):
                    continue
                # skip se troppo vicino a edifici OSM
                if dist_to_nearest_building(fx, fy) < 15.0:
                    continue
                (_, _, _, d_r) = nearest_cl_point(fx, fy)
                if d_r < 15.0:
                    continue
                # 50% skip per evitare over-saturation
                if rng.random() < 0.5:
                    continue
                fz = ground_z(fx, fy) - 0.25
                species_b = boulder_species[
                    rng.integers(0, len(boulder_species))]
                add_tree(fx, fy, fz, height=1.5,
                          context="prop",
                          specific_species=species_b,
                          angle=rng.uniform(0, 2 * math.pi),
                          scale_override=rng.uniform(0.7, 1.3))
                count_boulder += 1
            xv += boulder_grid
    print(f"  boulders italy nelle forest: {count_boulder}")

    # ---- PASS 3: pali elettrici lungo strada ----
    # OSM non ha power_lines qui, ma una SS provinciale in Molise ha pali
    # dell'elettrico o del telefono ogni ~35-40m. Piazzo un palo
    # cilindrico alternato lato sx/dx a 5.5m dal centro strada.
    def add_power_pole(cx: float, cy: float, cz: float, height: float = 8.5):
        """Palo cilindrico sottile r=0.12, height=8.5m, material Parapet."""
        r = 0.12
        n = 6
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

    # Pali elettrici: collezioni pure le posizioni per disegnare poi i cavi
    # sospesi tra consecutivi sullo stesso lato (catenaria approssimata con
    # 3 segmenti: 10% dip al centro, tipico reale ~0.5-1m su span di 40m).
    pole_positions: list[tuple[float, float, float, int]] = []  # (x, y, z_base, side)
    POLE_HEIGHT = 8.5  # deve combaciare con add_power_pole
    POLE_TOP_OFF = 0.3  # i cavi partono ~30cm sotto la cima
    pole_side = +1
    pole_step_m = 38.0
    pole_acc = 0.0
    last_px, last_py = cl[0][0], cl[0][1]
    for (x, y, z, br, tu) in cl[1:]:
        dx_p = x - last_px; dy_p = y - last_py
        last_px, last_py = x, y
        d_p = math.hypot(dx_p, dy_p)
        pole_acc += d_p
        if pole_acc < pole_step_m:
            continue
        pole_acc = 0.0
        if br or tu:
            continue
        nxp, nyp = -dy_p / d_p, dx_p / d_p
        off = 5.5 * pole_side
        px = x + nxp * off
        py = y + nyp * off
        add_power_pole(px, py, z - 0.1)
        pole_positions.append((px, py, z - 0.1, pole_side))
        count_pole += 1
        pole_side = -pole_side

    # Cavi sospesi tra pali consecutivi dello STESSO lato. La SS17 provinciale
    # ha tipicamente 2 cavi: uno alto (fase) e uno ~60cm sotto (neutro/MT).
    # Modello come prisma rettangolare molto sottile (0.04x0.04m) con una
    # leggera curva in 4 segmenti (catenaria discreta) per dare realismo.
    def add_cable(x0, y0, z0, x1, y1, z1, segs: int = 4,
                   sag: float = 0.6, radius: float = 0.02):
        """Cavo sottile tra due punti con catenaria approssimata in `segs`
        segmenti quadrati. Usa materiale CableWire (scuro)."""
        # Parabola discreta: z(t) = (1-t)*z0 + t*z1 - 4*sag*t*(1-t)
        pts: list[tuple[float, float, float]] = []
        for k in range(segs + 1):
            t = k / segs
            xk = (1 - t) * x0 + t * x1
            yk = (1 - t) * y0 + t * y1
            zk = (1 - t) * z0 + t * z1 - 4.0 * sag * t * (1 - t)
            pts.append((xk, yk, zk))
        # normale perpendicolare al cavo in piano XY (per orientare sezione)
        dx_c = x1 - x0; dy_c = y1 - y0
        dh = math.hypot(dx_c, dy_c)
        if dh < 1e-3:
            return
        nx_c = -dy_c / dh; ny_c = dx_c / dh
        r = radius
        # sezione quadrata 4 vertici per nodo (offset in orizz + vert)
        def nodes(px: float, py: float, pz: float) -> list[tuple[float, float, float]]:
            return [
                (px + nx_c * r, py + ny_c * r, pz + r),
                (px - nx_c * r, py - ny_c * r, pz + r),
                (px - nx_c * r, py - ny_c * r, pz - r),
                (px + nx_c * r, py + ny_c * r, pz - r),
            ]
        for k in range(segs):
            pA = pts[k]; pB = pts[k + 1]
            a_nodes = nodes(*pA)
            b_nodes = nodes(*pB)
            base = len(verts) + 1
            for nd in a_nodes + b_nodes:
                verts.append(nd)
            # 4 lati del prisma (a0,a1,b1,b0), (a1,a2,b2,b1), ...
            for s in range(4):
                a0 = base + s
                a1 = base + (s + 1) % 4
                b0 = base + 4 + s
                b1 = base + 4 + (s + 1) % 4
                faces.append(([a0, a1, b1], "CableWire"))
                faces.append(([a0, b1, b0], "CableWire"))

    count_cable = 0
    # Costruisco mapping side -> lista ordinata di poli (per side 1 e -1)
    for side in (+1, -1):
        ps = [p for p in pole_positions if p[3] == side]
        # I poli sono gia' in ordine di avanzamento lungo la strada.
        # Un salto dev'essere <= pole_step_m * 2.5 per essere considerato
        # consecutivo (evita saltare ponti con cavi che tagliano dentro).
        MAX_SPAN = pole_step_m * 2.2 + 10.0
        for i in range(len(ps) - 1):
            x0, y0, z0, _ = ps[i]
            x1, y1, z1, _ = ps[i + 1]
            span = math.hypot(x1 - x0, y1 - y0)
            if span > MAX_SPAN:
                continue
            # z di attacco al palo = z_base + POLE_HEIGHT - POLE_TOP_OFF
            za_top = z0 + POLE_HEIGHT - POLE_TOP_OFF
            zb_top = z1 + POLE_HEIGHT - POLE_TOP_OFF
            # 2 cavi: quello alto (a cima palo) e quello piu' basso (-0.6m)
            add_cable(x0, y0, za_top, x1, y1, zb_top, sag=0.5)
            add_cable(x0, y0, za_top - 0.6, x1, y1, zb_top - 0.6, sag=0.55)
            count_cable += 2

    # Parapetti sui ponti: segui la centerline punto per punto (evita
    # muri dritti che tagliano la strada nei ponti in curva).
    count_parapet = 0
    for (a, b) in bridge_segments:
        for i in range(a, b):
            x0, y0, z0 = cl[i][0], cl[i][1], cl[i][2]
            x1, y1, z1 = cl[i + 1][0], cl[i + 1][1], cl[i + 1][2]
            dx = x1 - x0; dy = y1 - y0
            d = math.hypot(dx, dy)
            if d < 0.5:
                continue
            nx, ny = -dy / d, dx / d
            add_parapet_segment(x0, y0, z0, x1, y1, z1, (nx, ny))
            add_parapet_segment(x0, y0, z0, x1, y1, z1, (-nx, -ny))
            count_parapet += 2

    # Muretti protettivi sulle CURVE (lato esterno). SS17 in Molise ha
    # muretti basso in pietra + delineatori bianco-rosso sulle curve
    # con raggio <~ 120m. Rilevo curvatura comparando tangente a i e a i+30m
    # (cross positivo = sinistra, negativo = destra). Soglia 6 deg (piu'
    # inclusivo di prima: copre anche curve ampie).
    CURVE_WINDOW_M = 30.0
    CURVE_DEG = 6.0
    # cumulative length lungo cl (per avanzare di CURVE_WINDOW_M indice-per-indice)
    cl_cum = [0.0]
    for i in range(1, len(cl)):
        cl_cum.append(cl_cum[-1] + math.hypot(
            cl[i][0] - cl[i - 1][0], cl[i][1] - cl[i - 1][1]))

    def tangent_cl(i):
        if i <= 0:
            i = 1
        if i >= len(cl) - 1:
            i = len(cl) - 2
        dx = cl[i + 1][0] - cl[i - 1][0]
        dy = cl[i + 1][1] - cl[i - 1][1]
        d = math.hypot(dx, dy) or 1.0
        return dx / d, dy / d

    def idx_ahead(i, m):
        target = cl_cum[i] + m
        for j in range(i, len(cl_cum)):
            if cl_cum[j] >= target:
                return j
        return len(cl_cum) - 1

    count_curve_wall = 0
    for i in range(1, len(cl) - 1):
        # skip su ponti/tunnel (gia' parapetti / no protezione)
        if cl[i][3] or cl[i][4]:
            continue
        if any(a <= i < b for (a, b) in bridge_segments):
            continue
        j = idx_ahead(i, CURVE_WINDOW_M)
        if j <= i + 1:
            continue
        tx0, ty0 = tangent_cl(i)
        tx1, ty1 = tangent_cl(j)
        cross = tx0 * ty1 - ty0 * tx1
        dot = tx0 * tx1 + ty0 * ty1
        ang = math.degrees(math.atan2(cross, dot))
        if abs(ang) < CURVE_DEG:
            continue
        # segmento i -> i+1
        x0, y0, z0 = cl[i][0], cl[i][1], cl[i][2]
        x1, y1, z1 = cl[i + 1][0], cl[i + 1][1], cl[i + 1][2]
        dx = x1 - x0; dy = y1 - y0
        dn = math.hypot(dx, dy)
        if dn < 0.5:
            continue
        # lato esterno: opposto alla direzione di curva. Normale SX = (-dy,dx)/d
        if cross > 0:
            # curva a sinistra -> esterno e' DESTRA
            nx, ny = dy / dn, -dx / dn
        else:
            # curva a destra -> esterno e' SINISTRA
            nx, ny = -dy / dn, dx / dn
        add_parapet_segment(x0, y0, z0, x1, y1, z1, (nx, ny))
        count_curve_wall += 1

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

    # Scrivi OBJ con supporto UV per le face che le hanno (billboards alberi).
    # Pad face_uvs a len(faces) con None (le face senza UV usano formato
    # "f a b c", le face con UV usano "f a/ta b/tb c/tc").
    while len(face_uvs) < len(faces):
        face_uvs.append(None)
    lines = [
        "# macerone_roadside: procedurale sassi+cespugli+alberi (billboards)\n",
        "mtllib macerone_roadside.mtl\n",
    ]
    for (vx, vy, vz) in verts:
        lines.append(f"v {vx:.3f} {vy:.3f} {vz:.3f}\n")
    for (uu, vv) in uvs:
        lines.append(f"vt {uu:.4f} {vv:.4f}\n")
    current_mat = None
    lines.append("o Roadside\n")
    for (idx, mat), uv_idx in zip(faces, face_uvs):
        if mat != current_mat:
            lines.append(f"usemtl {mat}\n")
            current_mat = mat
        if uv_idx is None:
            lines.append(f"f {idx[0]} {idx[1]} {idx[2]}\n")
        else:
            lines.append(
                f"f {idx[0]}/{uv_idx[0]} {idx[1]}/{uv_idx[1]} "
                f"{idx[2]}/{uv_idx[2]}\n"
            )
    obj_path.write_text("".join(lines), encoding="utf-8")

    # Material roadside: Rock, BushGreen (saturazione ridotta), Parapet,
    # BollardMat, + TreeBillboard_P0..P3 con map_Kd alpha texture (stessa
    # texture usata da generate_vegetation.py).
    mtl_lines = [
        "newmtl Rock\nKd 0.52 0.50 0.45\n\n",
        # BushGreen: meno saturo, piu' oliva-terroso (era 0.28 0.40 0.22)
        "newmtl BushGreen\nKd 0.38 0.42 0.28\n\n",
        "newmtl Parapet\nKd 0.62 0.58 0.52\n\n",
        "newmtl BollardMat\nKd 0.80 0.80 0.78\n\n",
        "newmtl CableWire\nKd 0.10 0.10 0.10\nKa 0 0 0\nKs 0 0 0\n\n",
    ]
    # 4 palette di tinta per i billboards (stessi valori di generate_vegetation.py)
    tint_palettes = [
        (0.95, 1.00, 0.92),
        (0.82, 0.92, 0.80),
        (1.05, 1.08, 0.95),
        (0.92, 1.02, 1.00),
    ]
    for pal_i, (r, g, b) in enumerate(tint_palettes):
        name = f"TreeBillboard_P{pal_i}"
        mtl_lines.append(f"newmtl {name}\n")
        mtl_lines.append("map_Kd art/nature/tree_billboard.png\n")
        mtl_lines.append("map_d art/nature/tree_billboard.png\n")
        mtl_lines.append(f"Kd {r:.3f} {g:.3f} {b:.3f}\n")
        mtl_lines.append("d 1.0\nillum 1\n\n")
    mtl_path.write_text("".join(mtl_lines), encoding="utf-8")
    print(f"Roadside clutter: {count_rock} pietre + {count_bush} cespugli + "
          f"{count_tree_near} alberi(road) + {count_forest_trees} alberi(bosco) + "
          f"{count_farmhouse} farmhouse(sparse) + "
          f"{count_osm_buildings} OSM-buildings-italy + "
          f"{count_pole} pali (skip {count_skipped_bridge} ponti), "
          f"{count_cable} cavi, "
          f"{count_parapet} parapetti, {count_curve_wall} muretti curva, "
          f"{count_bollard} bollard -> "
          f"{obj_path.relative_to(MOD_DIR)}")
    print(f"  tree_positions accumulati: {len(tree_positions)} (per Forest system)")
    # Scrivi il Forest system nativo BeamNG usando tree_positions
    write_forest_system(level_dir, tree_positions)
    return obj_path


def write_italy_tree_materials(level_dir: Path) -> None:
    """Copia i main.materials.json italy (trees + buildings) nel mod.

    Senza questi materiali, i DAE referenziati da ForestItemData non trovano
    match per i nomi materiali (es. "cork_oak", "holm_oak", "olive", "fluffy",
    "italy_bld_old_bricks", "italy_bld_roof_tiles") e BeamNG applica il
    fallback "no texture" → colore arancione/rosa.

    I file contengono path texture assoluti (/levels/italy/.../*.png)
    che il gioco risolve dal content/levels/italy.zip — quindi NON servono
    copie delle texture nel mod, solo dei materials.json.

    Fonti (tools/beamng/italy_extracted/*.materials.json):
      - trees_italy.materials.json  -> levels/.../trees/trees_italy/
      - buildings.materials.json    -> levels/.../buildings/
    Estratte una tantum da D:/Giochi/.../content/levels/italy.zip.
    """
    copied = 0
    for src_name, dst_rel in (
        ("trees_italy.materials.json",
             "art/shapes/trees/trees_italy/main.materials.json"),
        ("buildings.materials.json",
             "art/shapes/buildings/main.materials.json"),
        ("rocks.materials.json",
             "art/shapes/rocks/main.materials.json"),
    ):
        src = TOOLS / "italy_extracted" / src_name
        if not src.exists():
            print(f"  WARN: {src} non trovato - asset italy renderizzeranno "
                  f"con fallback arancione.")
            continue
        dst = level_dir / dst_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
            n_mats = len(data) if isinstance(data, dict) else 0
        except Exception:
            n_mats = -1
        print(f"  Italy materials ({src_name}): {n_mats} mat -> "
              f"{dst.relative_to(MOD_DIR)}")
        copied += 1
    if copied == 0:
        print("  WARN: nessun materiale italy copiato.")


def write_forest_system(level_dir: Path,
                         tree_positions: list[tuple[float, float, float,
                                                      float, float, str]]) -> None:
    """Scrive:
    - levels/macerone/art/forest/managedItemData.json (ForestItemData entries)
    - levels/macerone/forest/<type>.forest4.json (posizioni istanze)

    Usa DAE alberi vanilla italy (shapeFile reference cross-level).
    BeamNG autoLoad Forest entity raccoglie automaticamente tutti i file
    forest4.json nel directory levels/<level>/forest/ quando c'e' un
    entity class Forest in main.level.json.
    """
    if not tree_positions:
        return
    import uuid as _uuid
    forest_dir = level_dir / "forest"
    forest_dir.mkdir(parents=True, exist_ok=True)
    art_forest_dir = level_dir / "art" / "forest"
    art_forest_dir.mkdir(parents=True, exist_ok=True)

    # === Specie disponibili (path DAE relativi a content/levels/italy.zip) ===
    # Sono risolte automaticamente perche' italy.zip e' caricato dal gioco.
    # Scaled_range sono altezze reali dell'asset a scale=1.0 (approx), usati
    # per derivare scale dal target height requested.
    SPECIES = {
        # name: (shapeFile, asset_height_at_scale1)
        "olive_tree":        ("levels/italy/art/shapes/trees/trees_italy/olive.dae",               5.0),
        "holm_oak_test":     ("levels/italy/art/shapes/trees/trees_italy/holm_oak.dae",            9.0),
        "holm_oak_bush":     ("levels/italy/art/shapes/trees/trees_italy/holm_oak_bush.dae",       4.0),
        "cypress_tree":      ("levels/italy/art/shapes/trees/trees_italy/cypress_tree.dae",       12.0),
        "maritime_pine_tree": ("levels/italy/art/shapes/trees/trees_italy/maritime_pine.dae",     10.0),
        "scraggly_tree":     ("levels/italy/art/shapes/trees/trees_italy/scraggly.dae",            6.0),
        "scraggly_tree_2":   ("levels/italy/art/shapes/trees/trees_italy/scraggly_2.dae",          5.0),
        "cork_oak_medium":   ("levels/italy/art/shapes/trees/trees_italy/cork_oak_medium.dae",     7.0),
        "fluffy_bush":       ("levels/italy/art/shapes/trees/trees_italy/fluffy_bush.dae",         2.5),
        "generibush":        ("levels/italy/art/shapes/trees/trees_italy/generibush.dae",          1.8),
        # Farmhouse italy — case rurali complete (2-3 piani, ~7-10m).
        "farmhouse_a":       ("levels/italy/art/shapes/buildings/italy_bld_farmhouse_a.DAE",       8.0),
        "farmhouse_b":       ("levels/italy/art/shapes/buildings/italy_bld_farmhouse_b.DAE",       8.0),
        "farmhouse_c":       ("levels/italy/art/shapes/buildings/italy_bld_farmhouse_c.DAE",       8.0),
        "farmhouse_d":       ("levels/italy/art/shapes/buildings/italy_bld_farmhouse_d.DAE",       7.5),
        "farmhouse_e":       ("levels/italy/art/shapes/buildings/italy_bld_farmhouse_e.DAE",       7.0),
        "farmhouse_f":       ("levels/italy/art/shapes/buildings/italy_bld_farmhouse_f.DAE",       8.5),
        "farmhouse_g":       ("levels/italy/art/shapes/buildings/italy_bld_farmhouse_g.DAE",       7.5),
        "farmhouse_h":       ("levels/italy/art/shapes/buildings/italy_bld_farmhouse_h.DAE",       7.5),
        "farmhouse_i":       ("levels/italy/art/shapes/buildings/italy_bld_farmhouse_i.DAE",       7.5),
        "farmhouse_j":       ("levels/italy/art/shapes/buildings/italy_bld_farmhouse_j.DAE",       8.0),
        # Shed — tettoie / capanne 1 piano (~3-4m, ~30 m²)
        "shed_a":            ("levels/italy/art/shapes/buildings/italy_bld_shed_a.DAE",            4.0),
        "shed_b":            ("levels/italy/art/shapes/buildings/italy_bld_shed_b.DAE",            4.0),
        "shed_c":            ("levels/italy/art/shapes/buildings/italy_bld_shed_c.DAE",            3.5),
        "shed_d":            ("levels/italy/art/shapes/buildings/italy_bld_shed_d.DAE",            3.5),
        "shed_e":            ("levels/italy/art/shapes/buildings/italy_bld_shed_e.DAE",            3.5),
        "shed_f":            ("levels/italy/art/shapes/buildings/italy_bld_shed_f.DAE",            3.5),
        # Ind_bld — capannoni industriali / commerciali piccoli
        # Il suffisso indica le dimensioni reali in metri (AxB).
        "ind_bld_8x8":       ("levels/italy/art/shapes/buildings/ind_bld_8x8.dae",                5.0),
        "ind_bld_12x10":     ("levels/italy/art/shapes/buildings/ind_bld_12x10.dae",              6.0),
        "ind_bld_12x12":     ("levels/italy/art/shapes/buildings/ind_bld_12x12.dae",              6.0),
        "ind_bld_12x15":     ("levels/italy/art/shapes/buildings/ind_bld_12x15.dae",              6.5),
        "ind_bld_12x20":     ("levels/italy/art/shapes/buildings/ind_bld_12x20.dae",              7.0),
        # --- Props paesaggio italy (realismo countryside Molise) ---
        # Muretti a secco: confini campi, scarpate, lati strada
        "italy_wall_stone":            ("levels/italy/art/shapes/buildings/italy_wall_stone.dae",            1.2),
        "italy_wall_stone_short":      ("levels/italy/art/shapes/buildings/italy_wall_stone_short.dae",      0.9),
        "italy_wall_stone_individual": ("levels/italy/art/shapes/buildings/italy_wall_stone_individual.dae", 0.8),
        "italy_rock_wall":             ("levels/italy/art/shapes/buildings/italy_rock_wall_a.dae",           1.2),
        # Massi (rockface) — sparsi in zone bosco e scarpate
        "italy_boulder_1": ("levels/italy/art/shapes/rocks/italy_rockface_boulder_1.dae", 2.2),
        "italy_boulder_2": ("levels/italy/art/shapes/rocks/italy_rockface_boulder_2.dae", 2.0),
        "italy_boulder_3": ("levels/italy/art/shapes/rocks/italy_rockface_boulder_3.dae", 1.8),
        "italy_rockface_small_1": ("levels/italy/art/shapes/rocks/italy_rockface_small_1.dae", 0.9),
        "italy_rockface_small_2": ("levels/italy/art/shapes/rocks/italy_rockface_small_2.dae", 0.8),
        "italy_rockface_small_3": ("levels/italy/art/shapes/rocks/italy_rockface_small_3.dae", 0.7),
        # Siepe di cipressi — tipici filari 3m davanti case/giardini
        "cypress_hedge_3m": ("levels/italy/art/shapes/trees/trees_italy/cypress_hedge_3m.dae",  3.0),
        # Recinzione rete metallica 6m — perimetro case, campi
        "italy_fence_mesh_6m": ("levels/italy/art/shapes/buildings/italy_fence_mesh_6m.dae", 1.8),
        # Vite da uva (filare) — rari nel Molise montano ma presenti
        "grape_vine":       ("levels/italy/art/shapes/trees/trees_italy/grape_vine.dae",       1.5),
        "grape_vine_group": ("levels/italy/art/shapes/trees/trees_italy/grape_vine_group.dae", 1.5),
    }

    # Area asset in m² (centrata sul footprint del DAE a scale=1), usata per
    # match col polygon OSM. Valori stimati dalle dimensioni nominali.
    ASSET_AREA = {
        "shed_a": 25, "shed_b": 30, "shed_c": 20, "shed_d": 20, "shed_e": 25, "shed_f": 30,
        "farmhouse_d": 80, "farmhouse_e": 90, "farmhouse_g": 95,
        "farmhouse_a": 140, "farmhouse_b": 130, "farmhouse_c": 140,
        "farmhouse_f": 150, "farmhouse_h": 110, "farmhouse_i": 110, "farmhouse_j": 130,
        "ind_bld_8x8": 64, "ind_bld_12x10": 120, "ind_bld_12x12": 144,
        "ind_bld_12x15": 180, "ind_bld_12x20": 240,
    }
    BUILDING_ASSETS = list(ASSET_AREA.keys())

    # === Raggruppa per contesto -> mix di specie ===
    # Molise/Appennino interno: bosco misto querce + pini, filari di olivi,
    # cipressi sparsi ai confini campi.
    MIX = {
        "forest":   ["holm_oak_test", "cork_oak_medium", "scraggly_tree",
                       "scraggly_tree_2", "holm_oak_bush", "maritime_pine_tree",
                       "cypress_tree"],  # cipressi anche in bosco (caratteristici SS17)
        # orchard: olivi dominanti + cipressi frequenti (filari campi)
        "orchard":  ["olive_tree", "olive_tree", "olive_tree",
                       "cypress_tree", "cypress_tree",  # 2x cipressi
                       "scraggly_tree_2"],
        # roadside: cipressi dominanti (il look classico SS17 italiana),
        # olivi, cespugli di accompagnamento
        "roadside": ["cypress_tree", "cypress_tree",  # 2x cipressi
                       "olive_tree", "olive_tree",
                       "holm_oak_bush", "fluffy_bush"],
        "mixed":    ["holm_oak_test", "olive_tree", "scraggly_tree",
                       "cypress_tree", "cypress_tree",  # 2x cipressi
                       "holm_oak_bush"],
        # bush: SOLO cespugli bassi (niente alberi alti) per sostituire
        # i vecchi tetraedri verdi. Peso maggiore su fluffy_bush che e'
        # piu' visualmente pieno di generibush.
        "bush":     ["fluffy_bush", "fluffy_bush", "fluffy_bush",
                       "generibush", "holm_oak_bush"],
        # farmhouse: casale rurale italiano (context usato per piazzamento
        # sparso lungo strada, con shed piccoli misti). NON ruotato come
        # albero (gli edifici hanno orientamento fissato dall'angle random).
        "farmhouse": ["farmhouse_a", "farmhouse_b", "farmhouse_c",
                        "farmhouse_d", "farmhouse_e", "farmhouse_f",
                        "farmhouse_g", "farmhouse_h", "farmhouse_i",
                        "farmhouse_j",
                        "shed_a", "shed_b", "shed_c"],
        # osm_building: fallback se specific_species per un polygon OSM non e'
        # valido (non dovrebbe mai succedere dato che pick_building_asset
        # ritorna sempre un nome in SPECIES).
        "osm_building": ["farmhouse_a", "farmhouse_c", "farmhouse_f",
                           "ind_bld_8x8", "ind_bld_12x10"],
    }
    import random as _r
    picker = _r.Random(5678)

    # === managedItemData.json ===
    # Le farmhouse/shed sono edifici statici: no wind, collidable=true,
    # annotation non "NATURE". Gli alberi hanno wind + collisione off.
    # Classifico per rendering/collision:
    # - SOLID_OBJECTS + rigid: edifici, muretti a secco, massi, recinzioni
    # - NATURE (no collision, wind): alberi, cespugli, siepi cipressi, vigne
    SOLID_PREFIX = ("farmhouse_", "shed_", "ind_bld_",
                    "italy_wall_", "italy_rock_wall",
                    "italy_boulder_", "italy_rockface_",
                    "italy_fence_")
    mid = {}
    for name, (shape_path, _h0) in SPECIES.items():
        pid = str(_uuid.UUID(bytes=name.encode().ljust(16, b'\0')[:16]))
        is_solid = any(name.startswith(p) for p in SOLID_PREFIX)
        if is_solid:
            # Muretti/massi/recinzioni collidabili ma massa ridotta rispetto
            # agli edifici per permettere fisica realistica (puoi rompere un
            # muretto a secco con l'auto, non un casolare).
            mass_val = 50000.0 if (name.startswith("farmhouse_") or
                                   name.startswith("shed_") or
                                   name.startswith("ind_bld_")) else 2000.0
            mid[name] = {
                "name": name,
                "internalName": f"{name}_int",
                "class": "ForestItemData",
                "persistentId": pid,
                "annotation": "SOLID_OBJECTS",
                "shapeFile": f"/{shape_path}",
                "collidable": True,
                "branchAmp": 0.0,
                "detailAmp": 0.0,
                "detailFreq": 0.0,
                "mass": mass_val,
                "rigid": True,
                "trunkBendScale": 0.0,
                "windScale": 0.0,
            }
        else:
            mid[name] = {
                "name": name,
                "internalName": f"{name}_int",
                "class": "ForestItemData",
                "persistentId": pid,
                "annotation": "NATURE",
                "shapeFile": f"/{shape_path}",
                "collidable": False,
                "branchAmp": 0.05,
                "detailAmp": 0.1,
                "detailFreq": 0.3,
                "mass": 200.0,
                "rigid": False,
                "trunkBendScale": 0.02,
                "windScale": 0.15,
            }
    mid_path = art_forest_dir / "managedItemData.json"
    mid_path.write_text(json.dumps(mid, indent=2), encoding="utf-8")
    print(f"  Forest managedItemData: {len(mid)} specie -> {mid_path.relative_to(MOD_DIR)}")

    # Copia i materials.json dei trees_italy nel mod — senza questi le mesh
    # renderizzano col fallback "no texture" (arancione / rosa).
    # I path texture sono assoluti (/levels/italy/...) quindi risolvono sempre.
    write_italy_tree_materials(level_dir)

    # === Distribuisci trees nelle specie e scrivi forest4.json ===
    # Raggruppa per type selezionato
    by_type: dict[str, list[str]] = {}
    for entry in tree_positions:
        # Tuple legacy a 6 elementi supportata con defaults:
        if len(entry) == 6:
            tx, ty, tz, h_target, ang, ctx = entry
            specific_species = None
            scale_override = None
        else:
            (tx, ty, tz, h_target, ang, ctx,
             specific_species, scale_override) = entry
        if specific_species is not None and specific_species in SPECIES:
            species_name = specific_species
        else:
            mix = MIX.get(ctx, MIX["mixed"])
            species_name = picker.choice(mix)
        _shape, h0 = SPECIES[species_name]
        if scale_override is not None:
            scale = scale_override
        else:
            # Scale = altezza_voluta / altezza_asset * jitter
            scale = max(0.5, min(1.8, (h_target / max(h0, 0.1)) * picker.uniform(0.85, 1.15)))
        ca = math.cos(ang); sa = math.sin(ang)
        # rotationMatrix 3x3 row-major (rotazione attorno Z):
        # [ ca -sa  0
        #   sa  ca  0
        #    0   0  1 ]
        rot = [ca, -sa, 0.0,
                 sa,  ca, 0.0,
                 0.0, 0.0, 1.0]
        item = {
            "ctxid": 0,
            "pos": [round(tx, 3), round(ty, 3), round(tz, 3)],
            "rotationMatrix": [round(v, 6) for v in rot],
            "scale": round(scale, 3),
            "type": species_name,
        }
        by_type.setdefault(species_name, []).append(json.dumps(item))

    # === Scrivi un forest4.json per ciascuna specie usata ===
    total_written = 0
    for species_name, items in by_type.items():
        fpath = forest_dir / f"{species_name}.forest4.json"
        # Format e' JSONL: un oggetto per riga
        fpath.write_text("\n".join(items) + "\n", encoding="utf-8")
        total_written += len(items)
        print(f"  Forest4 {species_name}: {len(items)} istanze -> "
              f"{fpath.relative_to(MOD_DIR)}")
    print(f"  Forest totale: {total_written} alberi in {len(by_type)} specie")


def copy_satellite_texture(level_dir: Path) -> None:
    """Post-processa la satellite prima di copiarla:
    - ~50% dei pixel e' nero (maschera poligonale fuori dal buffer strada).
      Senza fill, il TerrainBlock e' per meta' nero, compensato solo dal
      detail_grass a distanza ravvicinata -> effetto verde piatto.
    - Riempiamo i pixel neri con un bleed dai vicini non-neri + noise multi-
      scala di colore 'campagna italiana' (verdi diversi, marroni, gialli
      fieno) per avere variazione cromatica visibile.
    - Brighten gamma leggero cosi' la satellite non sparisce sotto il detail.
    """
    src = BEAMNG_OUT / "satellite_diffuse.png"
    if not src.exists():
        src = ROOT / "output" / "satellite.png"
    dst_dir = level_dir / "art" / "terrains"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / "satellite_diffuse.png"

    from PIL import Image, ImageFilter
    img = Image.open(src).convert("RGB")
    arr = np.array(img, dtype=np.float32)
    h, w = arr.shape[:2]

    # 1) Mask dei pixel validi (non-neri)
    mask = (arr.sum(axis=2) > 15).astype(np.float32)

    # 2) Fill noise = palette campagna italiana multi-scala
    rng = np.random.default_rng(777)
    def _multiscale(size_h: int, size_w: int, seed: int) -> np.ndarray:
        g = np.random.default_rng(seed)
        accum = np.zeros((size_h, size_w), np.float32)
        amp = 1.0
        f = 4
        while f <= 128:
            low_h, low_w = max(1, size_h // (256 // f)), max(1, size_w // (256 // f))
            lo = g.random((low_h, low_w)).astype(np.float32)
            up = np.array(Image.fromarray((lo * 255).astype(np.uint8)).resize(
                (size_w, size_h), Image.BICUBIC), dtype=np.float32) / 255.0
            accum += up * amp
            amp *= 0.55
            f *= 2
        mn, mx = accum.min(), accum.max()
        if mx - mn > 1e-6:
            accum = (accum - mn) / (mx - mn)
        return accum

    # 3 ottave di noise: vegetazione (verde-verde), stagionale (verde-giallo),
    # suolo (marrone). Combino per avere patchwork di campi.
    veg = _multiscale(h, w, 101)
    seas = _multiscale(h, w, 202)
    soil = _multiscale(h, w, 303)

    # Palette: verde scuro foresta, verde prato, giallo fieno, marrone arato.
    # Verdi SPOSTATI verso oliva/oliva-chiaro per evitare effetto neon-fluo
    # tipico di quando G >> R+B. Italia centrale Aprile = verde tenue con
    # molte patch di fieno secco e terra visibile.
    P_FOREST = np.array([58, 72, 42], np.float32)      # oliva scuro (meno G dominance)
    P_GRASS  = np.array([102, 112, 68], np.float32)    # verde-oliva tenue (era [92,120,58])
    P_HAY    = np.array([165, 150, 92], np.float32)    # fieno (inalterato)
    P_SOIL   = np.array([130, 100, 68], np.float32)    # marrone arato

    fill = np.zeros_like(arr)
    # Mix pesato: riduciamo peso grass, aumentiamo hay+soil per patchwork Italia
    w_forest = np.clip(1.0 - veg, 0, 1) * 0.55
    w_grass  = veg * 0.75                              # era 0.90
    w_hay    = np.clip(seas - 0.40, 0, 1) * 1.50       # soglia + peso piu' alti
    w_soil   = np.clip(soil - 0.55, 0, 1) * 1.20       # piu' terra visibile
    tot = w_forest + w_grass + w_hay + w_soil + 1e-6
    for i, (P, wt) in enumerate([
        (P_FOREST, w_forest), (P_GRASS, w_grass),
        (P_HAY, w_hay), (P_SOIL, w_soil)
    ]):
        for c in range(3):
            fill[:, :, c] += P[c] * wt / tot

    # Jitter per ogni pixel
    jitter = (rng.random((h, w, 3)).astype(np.float32) - 0.5) * 18
    fill = np.clip(fill + jitter, 0, 255)

    # 3) Dove la satellite e' valida la teniamo (ma brighten 1.15x), dove nera
    #    usiamo il fill procedurale. Sui bordi blendiamo con una dilate soft.
    mask_img = Image.fromarray((mask * 255).astype(np.uint8))
    mask_blur = np.array(mask_img.filter(ImageFilter.GaussianBlur(radius=4)),
                           dtype=np.float32) / 255.0
    # Brighten ridotto da 1.18 a 1.05: il satellite e' gia' OK di base,
    # boost alto rendeva tutto green-fluo a distanza.
    sat_bright = np.clip(arr * 1.05, 0, 255)
    out = sat_bright * mask_blur[..., None] + fill * (1.0 - mask_blur[..., None])

    # 4) Saturazione -8% (riduce neon), gamma leggera 0.95
    out_u8 = np.clip(out, 0, 255).astype(np.uint8)
    hsv = Image.fromarray(out_u8).convert("HSV")
    hsv_arr = np.array(hsv, dtype=np.float32)
    # DESATURA per eliminare effetto verde fluo
    hsv_arr[:, :, 1] = np.clip(hsv_arr[:, :, 1] * 0.78, 0, 255)
    hsv_arr[:, :, 2] = np.clip(np.power(hsv_arr[:, :, 2] / 255.0, 0.95) * 255.0, 0, 255)
    out_final = Image.fromarray(hsv_arr.astype(np.uint8), mode="HSV").convert("RGB")
    out_final.save(dst, "PNG", optimize=True)

    final_arr = np.array(out_final)
    print(f"Satellite (fix+enriched) -> {dst.relative_to(MOD_DIR)}  "
          f"({dst.stat().st_size // 1024} KB, mean RGB="
          f"{final_arr.mean(axis=(0,1)).round(0).astype(int).tolist()})")


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
    """Muso veicolo BeamNG = -Y locale -> heading = atan2(dx, -dy).
    Media i vettori di direzione tra p1 e i CL points nel range 20-80m per
    avere una direzione robusta al rumore locale della centerline (piccole
    curve dei primi tornanti rovinano una singola misura)."""
    import csv as _csv
    cl = ROOT / "output" / "centerline.csv"
    with cl.open(newline="", encoding="utf-8") as f:
        rows = list(_csv.DictReader(f))
    p1 = (float(rows[0]["x"]), float(rows[0]["y"]))
    sum_dx = 0.0
    sum_dy = 0.0
    count = 0
    for r in rows[1:]:
        px, py = float(r["x"]), float(r["y"])
        d = math.hypot(px - p1[0], py - p1[1])
        if 20.0 <= d <= 80.0:
            sum_dx += (px - p1[0]) / d
            sum_dy += (py - p1[1]) / d
            count += 1
    if count == 0:
        p2 = (float(rows[1]["x"]), float(rows[1]["y"]))
        sum_dx = p2[0] - p1[0]
        sum_dy = p2[1] - p1[1]
    # Formula base (muso = -Y locale, standard memoria storica): atan2(dx, -dy)
    base_heading = math.atan2(sum_dx, -sum_dy)
    # Applica offset tunabile in gradi
    return base_heading + math.radians(SPAWN_ROT_OFFSET_DEG)


def heading_to_quat(h: float) -> tuple[float, float, float, float]:
    return (0.0, 0.0, math.sin(h / 2.0), math.cos(h / 2.0))


# ---------------------------------------------------------------------------
# Step 7: main.level.json + info.json
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Helper: River BeamNG nativi da waterways OSM + DecalRoad di usura + Bookmark
# ---------------------------------------------------------------------------
def make_dem_sampler_blender(info: dict, z_offset_blender: float):
    """Ritorna sample(x,y) -> z_blender = DEM_real - z_offset_blender."""
    Image.MAX_IMAGE_PIXELS = None
    hm = np.array(Image.open(BEAMNG_OUT / "heightmap.png"), dtype=np.uint16)
    H, W = hm.shape
    elev_min = info["elevation_min_m"]
    elev_max = info["elevation_max_m"]
    max_h = elev_max - elev_min
    mpp = info["meters_per_pixel"]
    half = info["extent_m"] / 2.0

    def sample(x: float, y: float) -> float:
        col = int((x + half) / mpp)
        row = int((half - y) / mpp)
        col = max(0, min(W - 1, col))
        row = max(0, min(H - 1, row))
        dem_real = elev_min + (float(hm[row, col]) / 65535.0) * max_h
        return dem_real - z_offset_blender

    return sample


def build_river_blocks_from_waterways(waterways, project, dem_z) -> list[str]:
    """Converti waterways OSM in River BeamNG nativi (width/depth da kind)."""
    if not waterways:
        return []
    cl_pts: list[tuple[float, float]] = []
    try:
        import csv as _csv
        with (ROOT / "output" / "centerline.csv").open(newline="", encoding="utf-8") as f:
            for r in _csv.DictReader(f):
                cl_pts.append((float(r["x"]), float(r["y"])))
    except FileNotFoundError:
        pass

    def min_dist_to_cl(x: float, y: float) -> float:
        if not cl_pts:
            return 1e9
        dmin = 1e18
        for (cx, cy) in cl_pts:
            d = (cx - x) ** 2 + (cy - y) ** 2
            if d < dmin:
                dmin = d
        return math.sqrt(dmin)

    blocks: list[str] = []
    kept = 0
    for w in waterways:
        coords = w.get("coords") or []
        if len(coords) < 2:
            continue
        kind = w.get("kind") or "stream"
        width = 6.0 if kind == "river" else (3.5 if kind == "canal" else 2.5)
        depth = 1.4

        proj_pts = []
        for (lat, lon) in coords:
            x, y = project(lat, lon)
            proj_pts.append((x, y))
        # Skip segmenti interamente distanti >800m dalla strada (off-map vero)
        if cl_pts and min(min_dist_to_cl(x, y) for (x, y) in proj_pts) > 800.0:
            continue

        node_strs = []
        for (x, y) in proj_pts:
            z = dem_z(x, y) - 0.5
            node_strs.append(
                f"[ {x:.3f}, {y:.3f}, {z:.3f}, {width}, {depth}, 0, 0, 1 ]"
            )
        fx, fy = proj_pts[0]
        fz = dem_z(fx, fy) - 0.5
        nodes_str = ",\n          ".join(node_strs)
        blocks.append(
            '{\n'
            '  "class" : "River",\n'
            f'  "position" : [ {fx:.3f}, {fy:.3f}, {fz:.3f} ],\n'
            '  "baseColor" : [ 86, 95, 80, 225 ],\n'
            '  "cubemap" : "DefaultSkyCubemap",\n'
            '  "density" : 5000,\n'
            '  "depthGradientTex" : "core/art/water/depthcolor_ramp.png",\n'
            '  "foamTex" : "core/art/water/foam.dds",\n'
            '  "fullReflect" : false,\n'
            '  "overallRippleMagnitude" : 0.35,\n'
            '  "overallWaveMagnitude" : 0.12,\n'
            '  "rippleTex" : "core/art/water/ripple.dds",\n'
            '  "specularPower" : 80,\n'
            '  "waterFogDensity" : 0.7,\n'
            '  "wetDarkening" : 0.5,\n'
            '  "Ripples (texture animation)" : [\n'
            '    { "rippleDir" : [ 0, 1 ], "rippleSpeed" : -0.015, "rippleTexScale" : [ 5, 5 ] },\n'
            '    { "rippleDir" : [ 0.7, 0.7 ], "rippleSpeed" : 0.02, "rippleTexScale" : [ 8, 8 ] }\n'
            '  ],\n'
            '  "Waves (vertex undulation)" : [\n'
            '    { "waveDir" : [ 0, 1 ], "waveMagnitude" : 0.08, "waveSpeed" : 1 }\n'
            '  ],\n'
            '  "Foam" : [ {}, {} ],\n'
            f'  "nodes" : [\n          {nodes_str}\n        ]\n'
            '}'
        )
        kept += 1
    print(f"  River generati da waterways OSM: {kept}/{len(waterways)}")
    return blocks


def build_decalroad_wear_blocks() -> list[str]:
    """DecalRoad di usura (AsphaltWear) sulla centerline reale: aggiunge
    variazione visiva alla strada, nascosta tra le linee. Non fa da strada
    vera (c'e' gia' la mesh road), serve solo come layer di sporco/bitume."""
    import csv as _csv
    try:
        with (ROOT / "output" / "centerline.csv").open(newline="", encoding="utf-8") as f:
            pts = [(float(r["x"]), float(r["y"]), float(r["z"]))
                   for r in _csv.DictReader(f)]
    except FileNotFoundError:
        return []
    if len(pts) < 2:
        return []
    STEP = max(1, len(pts) // 120)
    sub = pts[::STEP]
    if len(sub) < 2:
        return []
    nodes = [f"[ {x:.3f}, {y:.3f}, {z + 0.02:.3f}, 7 ]" for (x, y, z) in sub]
    fx, fy, fz = sub[0]
    nodes_str = ",\n          ".join(nodes)
    return [(
        '{\n'
        '  "class" : "DecalRoad",\n'
        '  "name" : "road_wear",\n'
        f'  "position" : [ {fx:.3f}, {fy:.3f}, {fz + 0.02:.3f} ],\n'
        '  "Material" : "AsphaltWear",\n'
        '  "renderPriority" : 8,\n'
        '  "startEndFade" : [ 80, 80 ],\n'
        '  "textureLength" : 40,\n'
        f'  "nodes" : [\n          {nodes_str}\n        ]\n'
        '}'
    )]


def build_camera_bookmark_blocks(spawn_xyz, spawn_heading) -> list[str]:
    sx, sy, sz = spawn_xyz
    fx_v = math.sin(spawn_heading)
    fy_v = -math.cos(spawn_heading)
    bx = sx - 30 * fx_v
    by = sy - 30 * fy_v
    bz = sz + 14
    heading_deg = math.degrees(spawn_heading)
    return [(
        '{\n'
        '  "class" : "CameraBookmark",\n'
        '  "name" : "start_overview",\n'
        f'  "position" : [ {bx:.3f}, {by:.3f}, {bz:.3f} ],\n'
        f'  "rotation" : [ 0, 0, 1, {heading_deg:.2f} ],\n'
        '  "dataBlock" : "CameraBookmarkMarker",\n'
        '  "internalName" : "NewCamera_0",\n'
        '  "isAIControlled" : "0",\n'
        '  "mode" : "Override"\n'
        '}'
    )]


def inject_into_simgroup(tpl: str, sg_name: str, blocks: list[str]) -> str:
    """Inietta blocchi JSON dentro un SimGroup con il nome dato, prendendo
    come target il suo childs vuoto []. Torna tpl invariato se non matcha."""
    if not blocks:
        return tpl
    body = ",\n        ".join(blocks)
    pat = (r'(\{\s*"class"\s*:\s*"SimGroup"\s*,\s*"name"\s*:\s*"'
           + re.escape(sg_name)
           + r'"\s*,\s*(?:"enabled"\s*:\s*"[^"]+"\s*,\s*)?"childs"\s*:\s*\[)(\s*)(\])')

    def repl(m):
        return m.group(1) + "\n        " + body + "\n      " + m.group(3)

    new = re.sub(pat, repl, tpl, count=1, flags=re.S)
    if new == tpl:
        print(f"  WARN: inject_into_simgroup('{sg_name}') pattern non matchato")
    return new


def write_level_json(level_dir: Path,
                      road_shape_rel: str,
                      world_shape_rel: str | None,
                      roadside_shape_rel: str | None,
                      terrain_shape_rel: str | None,
                      extra_buildings_shape_rel: str | None,
                      road_details_shape_rel: str | None,
                      embankments_shape_rel: str | None,
                      vegetation_shape_rel: str | None,
                      spawn_xyz: tuple[float, float, float],
                      spawn_heading: float,
                      max_height: float,
                      elev_min: float,
                      z_offset_blender: float,
                      waterways: list | None = None,
                      dem_sampler=None,
                      project_fn=None,
                      signs_shape_rel: str | None = None,
                      video_shape_rel: str | None = None) -> None:
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
    if extra_buildings_shape_rel is not None:
        tsstatics.append(
            (
                "macerone_extra_buildings_mesh",
                f"levels/{LEVEL_NAME}/{extra_buildings_shape_rel}",
            )
        )
    if road_details_shape_rel is not None:
        tsstatics.append(
            (
                "macerone_road_details_mesh",
                f"levels/{LEVEL_NAME}/{road_details_shape_rel}",
            )
        )
    if embankments_shape_rel is not None:
        tsstatics.append(
            (
                "macerone_embankments_mesh",
                f"levels/{LEVEL_NAME}/{embankments_shape_rel}",
            )
        )
    if vegetation_shape_rel is not None:
        tsstatics.append(
            (
                "macerone_vegetation_mesh",
                f"levels/{LEVEL_NAME}/{vegetation_shape_rel}",
            )
        )
    if signs_shape_rel is not None:
        tsstatics.append(
            (
                "macerone_signs_mesh",
                f"levels/{LEVEL_NAME}/{signs_shape_rel}",
            )
        )
    if video_shape_rel is not None:
        tsstatics.append(
            (
                "macerone_video_mesh",
                f"levels/{LEVEL_NAME}/{video_shape_rel}",
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

    # Forest entity: abilita il rendering Forest4 nativo. BeamNG autoLoad
    # tutti i file levels/macerone/forest/*.forest4.json a creazione.
    # Richiede managedItemData.json in levels/macerone/art/forest/.
    forest_entity = (
        '{\n'
        '  "class" : "Forest",\n'
        '  "name" : "theForest",\n'
        '  "persistentId" : "b0000001-0000-4000-8000-000000000001",\n'
        '  "lodReflectScalar" : 0\n'
        '}'
    )

    def inject_after_terrain(m):
        return (m.group(0) + ",\n        "
                  + ",\n        ".join(tsstatic_blocks)
                  + ",\n        " + forest_entity)

    tpl = re.sub(r'\{\s*"class"\s*:\s*"TerrainBlock".*?\}', inject_after_terrain,
                   tpl, count=1, flags=re.S)

    # --- Inject River nativi da waterways OSM reali ---
    if waterways and project_fn is not None and dem_sampler is not None:
        river_blocks = build_river_blocks_from_waterways(
            waterways, project_fn, dem_sampler)
        tpl = inject_into_simgroup(tpl, "Water", river_blocks)

    # DecalRoad wear rimosso: il Material custom "AsphaltWear" copriva le
    # line markings su alcune build. Lasciamo la road texture ai markings
    # MarkingCenter/Edge gia' presenti nel mesh Blender.

    # --- Inject CameraBookmark centrata sullo spawn ---
    bm_blocks = build_camera_bookmark_blocks(spawn_xyz, spawn_heading)
    tpl = inject_into_simgroup(tpl, "CameraBookmarks", bm_blocks)

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

    # 3. OBJ -> DAE (prima alzo le linee di 3cm contro Z-fighting col Road)
    shifted_markings = shift_marking_vertices(road_obj, shift_z=0.03)
    print(f"  markings/roadstuds alzati di 3cm: {shifted_markings} vertici")
    road_dae = convert_to_dae(road_obj)
    road_rel = road_dae.relative_to(LEVEL_DIR).as_posix()

    # PRIMA: carve mesh terrain (poi sara' usato da drop world)
    terrain_has_content = terrain_obj.exists() and terrain_obj.stat().st_size > 200
    terrain_rel = None
    if terrain_has_content:
        carved_terrain = carve_terrain_mesh_near_road(terrain_obj)
        print(f"  terrain mesh carve: {carved_terrain} vertex abbassati "
              f"(invadevano sopra la strada)")

    world_has_content = world_obj.exists() and world_obj.stat().st_size > 200
    world_rel = None
    if world_has_content:
        # STRIP edifici procedurali Blender (Building/Buildings_Walls/Roofs/
        # Chimneys/ExtraBuildings): sono sostituiti dagli asset vanilla
        # italy piazzati via Forest system (PASS 2.6 in generate_roadside_clutter).
        stripped = strip_building_objects_from_world_obj(world_obj)
        print(f"  stripped {stripped} blocchi edificio procedurale dal world mesh")
        removed = filter_world_obj_near_road(world_obj, ROAD_CORRIDOR_FILTER_M)
        print(f"  filter corridoio {ROAD_CORRIDOR_FILTER_M}m: "
              f"rimosse {removed} face dal world mesh")
        # Rimuove edifici che intrudono sulla strada (building INTERO,
        # non solo face). Raggio 4m dal centerline. Dopo il strip sopra
        # questo e' quasi no-op (gli edifici sono gia' via), ma lo lasciamo
        # per gli extra building polygons generati altrove.
        removed_b = remove_buildings_on_road(world_obj, radius_m=4.0)
        print(f"  rimossi {removed_b} edifici world che invadevano la strada")
        # Drop-to-ground usa il terrain mesh CARVATO come riferimento
        if terrain_has_content:
            drop_world_obj_to_terrain_mesh(world_obj, terrain_obj)
        obj_counts = {}
        current = None
        with world_obj.open() as f:
            for line in f:
                if line.startswith("o "):
                    current = line.split(maxsplit=1)[1].strip()
                    obj_counts[current] = 0
                elif line.startswith("f ") and current:
                    obj_counts[current] += 1
        interesting = {"Guardrail_L", "Guardrail_R", "CurveSigns",
                       "SpeedSigns_Disc", "SpeedSigns_Pole",
                       "KmMarker_top", "KmMarker_base", "StoneWalls",
                       "Delineators", "PowerPoles", "Chimneys"}
        print("  world mesh elementi visibili:")
        for name in sorted(obj_counts):
            if name in interesting and obj_counts[name] > 0:
                print(f"    {name}: {obj_counts[name]} face")
        world_dae = convert_to_dae(world_obj)
        world_rel = world_dae.relative_to(LEVEL_DIR).as_posix()

    # NOTA: il Blender terrain OBJ resta solo come riferimento interno per
    # drop_world_obj_to_terrain_mesh (allineamento z di edifici/scarpate).
    # NON viene esportato come DAE ne' aggiunto come TSStatic: il terreno
    # vero e proprio e' ora il native TerrainBlock (.ter) sotto.
    # Motivo: le TSStatic mesh con texture da material Material non rendono
    # correttamente la texture satellite (sempre nero). Il TerrainBlock nativo
    # usa TerrainMaterial + diffuseMap e funziona come nei livelli ufficiali.
    terrain_rel = None

    # 4. Terrain .ter: DEM reale con heightmap, allineato alle coord Blender
    # (z_offset_blender shiftato), cosi' il road mesh in coord Blender (z~72m)
    # combacia con il terreno nativo. La heightmap viene anche carvata sotto
    # la strada per evitare z-fighting.
    max_height, elev_min, z_offset_blender = write_dem_terrain(
        LEVEL_DIR, info, z_offset_blender)

    # 5. Materiali + texture asfalto + satellite terrain + foliage/bark
    # Asfalto: uso colore realistico fisso invece del sample satellite che
    # dava verde-grigio (ESRI vede l'asfalto illuminato dall'alto come chiaro).
    # Asfalto vero appenninico: grigio scuro leggermente caldo.
    # Base RGB da campionamento frame GoPro GX010576/577 (SS17 reale):
    # asfalto invecchiato sotto sole, pietrisco chiaro esposto.
    # Medio grigio cool ~(0.49, 0.50, 0.52). NON 0.18: era nero-catrame
    # fresco, irrealistico per strada provinciale esposta da anni.
    asphalt_rgb = (0.49, 0.50, 0.52)
    print(f"asfalto RGB base (GoPro-ref): "
          f"({asphalt_rgb[0]:.3f}, {asphalt_rgb[1]:.3f}, {asphalt_rgb[2]:.3f})")
    generate_asphalt_texture(LEVEL_DIR, asphalt_rgb)
    foliage_map = generate_foliage_texture(LEVEL_DIR)
    bark_map = generate_bark_texture(LEVEL_DIR)
    asphalt_nrm = generate_asphalt_normal(LEVEL_DIR)
    bark_nrm = generate_bark_normal(LEVEL_DIR)
    stonewall_nrm = generate_stonewall_normal(LEVEL_DIR)
    generate_terrain_detail_texture(LEVEL_DIR)
    generate_terrain_normal_texture(LEVEL_DIR)
    generate_terrain_macro_texture(LEVEL_DIR)
    copy_satellite_texture(LEVEL_DIR)
    # BeamNG 0.38 Texture Cooker: converte auto PNG -> DDS BC7 sRGB al primo
    # load, ma SOLO se i PNG hanno suffix `.color.png`. Il materials.json
    # deve puntare al path `.color.png`; BeamNG legge automaticamente il DDS
    # convertito se disponibile.
    # (Fonte: https://documentation.beamng.com/modding/materials/texture_cooker/)
    print("Rinomino PNG in *.color.png per Texture Cooker BeamNG...")
    rename_pairs = [
        ("art/road/asphalt_base.png", "art/road/asphalt_base.color.png"),
        ("art/terrains/satellite_diffuse.png", "art/terrains/satellite_diffuse.color.png"),
        ("art/terrains/detail_grass.png", "art/terrains/detail_grass.color.png"),
        ("art/terrains/detail_grass_nrm.png", "art/terrains/detail_grass_nrm.color.png"),
        ("art/terrains/macro_grass.png", "art/terrains/macro_grass.color.png"),
        ("art/nature/foliage.png", "art/nature/foliage.color.png"),
        ("art/nature/bark.png", "art/nature/bark.color.png"),
    ]
    for src, dst in rename_pairs:
        sp = LEVEL_DIR / src
        dp = LEVEL_DIR / dst
        if sp.exists():
            if dp.exists():
                dp.unlink()
            sp.rename(dp)
            print(f"  {src} -> {dst}")
    # I FILE fisici hanno suffix .color.png (Texture Cooker input).
    # I material PUNTANO direttamente al .color.png (output del Texture Cooker
    # che esiste nel virtual FS di BeamNG post-conversione).
    foliage_map = foliage_map.replace(".png", ".color.png").lstrip("/")
    bark_map = bark_map.replace(".png", ".color.png").lstrip("/")
    asphalt_map = f"levels/{LEVEL_NAME}/art/road/asphalt_base.color.png"
    terrain_map = f"levels/{LEVEL_NAME}/art/terrains/satellite_diffuse.color.png"
    write_materials(LEVEL_DIR, asphalt_rgb,
                     asphalt_color_map=asphalt_map,
                     terrain_color_map=terrain_map,
                     foliage_color_map=foliage_map,
                     bark_color_map=bark_map,
                     asphalt_normal_map=asphalt_nrm,
                     bark_normal_map=bark_nrm,
                     stonewall_normal_map=stonewall_nrm)

    # 5b. Roadside clutter procedurale (sassi + ciuffi ai bordi strada)
    # Passo il sampler terrain cosi' prop lontani dalla strada (hedges,
    # muretti, edifici OSM, boulders) sampleranno Z dal terreno reale
    # invece della centerline -> niente piu' hedge fluttuanti.
    terrain_z_sampler = make_terrain_sampler(terrain_obj) if terrain_has_content else None
    roadside_obj = generate_roadside_clutter(LEVEL_DIR, terrain_z_sampler)
    roadside_rel = None
    if roadside_obj is not None:
        roadside_dae = convert_to_dae(roadside_obj)
        roadside_rel = roadside_dae.relative_to(LEVEL_DIR).as_posix()

    # 5b2. Landmark signs (VALICO / SS17 / direzionale / edicola votiva)
    signs_obj = generate_landmark_signs(LEVEL_DIR, terrain_z_sampler)
    signs_rel = None
    if signs_obj is not None:
        signs_dae = convert_to_dae(signs_obj)
        signs_rel = signs_dae.relative_to(LEVEL_DIR).as_posix()

    # 5b3. Video landmarks (cartelli esatti dal video + edifici iconici +
    # balle fieno + delineatori extra)
    video_obj = generate_video_landmarks(LEVEL_DIR, terrain_z_sampler)
    video_rel = None
    if video_obj is not None:
        video_dae = convert_to_dae(video_obj)
        video_rel = video_dae.relative_to(LEVEL_DIR).as_posix()

    # 5c. Edifici OSM — DISATTIVATO.
    # Prima: generate_extra_buildings.py creava cubi procedurali per i
    # polygon OSM che Blender filtrava. Ora TUTTI gli OSM building polygons
    # sono piazzati come asset vanilla italy (farmhouse/shed/ind_bld) via
    # Forest system nel Pass 2.6 di generate_roadside_clutter.
    extra_buildings_rel = None
    # Rimuovi DAE/OBJ precedenti per non farli rientrare nel zip
    for stale in ("macerone_extra_buildings.obj",
                  "macerone_extra_buildings.dae",
                  "macerone_extra_buildings.mtl"):
        fp = LEVEL_DIR / "art" / "shapes" / stale
        if fp.exists():
            fp.unlink()
            print(f"  removed stale {stale}")

    # 5d. Dettagli realistici su asfalto: patches bitume + chevrons tornanti
    road_details_rel = None
    run("generate_road_details",
        [sys.executable, str(TOOLS / "generate_road_details.py")])
    road_details_obj = LEVEL_DIR / "art" / "shapes" / "macerone_road_details.obj"
    if road_details_obj.exists() and road_details_obj.stat().st_size > 200:
        road_details_dae = convert_to_dae(road_details_obj)
        road_details_rel = road_details_dae.relative_to(LEVEL_DIR).as_posix()

    # 5e0. Vegetazione map-wide — DISATTIVATO.
    # I crossed billboards (due quad perpendicolari con texture albero
    # procedurale) apparivano in gioco come RAGGI VERDI / rombi a X quando
    # l'alpha test falliva. Il Forest system con DAE vanilla italy (holm_oak,
    # olive, cypress, scraggly, fluffy_bush ecc.) copre gia' l'intera mappa
    # con alberi reali, alpha test funzionante e LOD — quindi questo pass
    # e' ridondante oltre che problematico.
    vegetation_rel = None
    # Rimuovi eventuale DAE precedente per non farlo rientrare nel zip.
    for stale in ("macerone_vegetation.obj", "macerone_vegetation.dae",
                  "macerone_vegetation.mtl"):
        fp = LEVEL_DIR / "art" / "shapes" / stale
        if fp.exists():
            fp.unlink()
            print(f"  removed stale {stale}")

    # 5e. Scarpate procedurali (riempiono il gap strada-terreno rialzato)
    embankments_rel = None
    run("generate_embankments",
        [sys.executable, str(TOOLS / "generate_embankments.py")])
    embankments_obj = LEVEL_DIR / "art" / "shapes" / "macerone_embankments.obj"
    if embankments_obj.exists() and embankments_obj.stat().st_size > 200:
        if terrain_has_content:
            drop_world_obj_to_terrain_mesh(embankments_obj, terrain_obj)
        embankments_dae = convert_to_dae(embankments_obj)
        embankments_rel = embankments_dae.relative_to(LEVEL_DIR).as_posix()

    # 6. Spawn: DECOUPLE posizione (lungo strada reale) da heading (muso).
    # road_dx/road_dy = direzione normalizzata della strada (da centerline).
    # SPAWN_FORWARD_M sposta lungo la strada (NON lungo il muso veicolo).
    sx, sy, _sz = read_first_centerline_point()
    top_z = road_top_z_at(road_obj, sx, sy, radius=3.0)
    # Calcola direzione strada media (stessa logica di read_spawn_heading
    # ma ritorna il vettore invece dell'angolo)
    import csv as _csv
    _cl_rows = list(_csv.DictReader(
        (ROOT / "output" / "centerline.csv").open(newline="", encoding="utf-8")))
    _p1 = (float(_cl_rows[0]["x"]), float(_cl_rows[0]["y"]))
    _sdx, _sdy, _cnt = 0.0, 0.0, 0
    for _r in _cl_rows[1:]:
        _px, _py = float(_r["x"]), float(_r["y"])
        _d = math.hypot(_px - _p1[0], _py - _p1[1])
        if 20.0 <= _d <= 80.0:
            _sdx += (_px - _p1[0]) / _d
            _sdy += (_py - _p1[1]) / _d
            _cnt += 1
    if _cnt == 0:
        _sdx = float(_cl_rows[1]["x"]) - _p1[0]
        _sdy = float(_cl_rows[1]["y"]) - _p1[1]
    _mag = math.hypot(_sdx, _sdy)
    road_dx = _sdx / _mag if _mag > 0 else 0.0
    road_dy = _sdy / _mag if _mag > 0 else 1.0

    # Posizione: sposta AVANTI lungo la strada reale
    sx2 = sx + SPAWN_FORWARD_M * road_dx
    sy2 = sy + SPAWN_FORWARD_M * road_dy
    sz2 = top_z + 0.10 + SPAWN_UP_M
    spawn = (sx2, sy2, sz2)

    # Heading: direzione muso (con offset + turn_right)
    heading = read_spawn_heading()  # include SPAWN_ROT_OFFSET_DEG
    heading -= math.radians(SPAWN_TURN_RIGHT_DEG)
    print(f"road dir: ({road_dx:.3f}, {road_dy:.3f})  spawn: "
          f"({sx2:.2f}, {sy2:.2f}, {sz2:.3f})")
    print(f"spawn heading (tuned): {math.degrees(heading):.1f} deg "
          f"(forward={SPAWN_FORWARD_M}m up={SPAWN_UP_M}m "
          f"rot_offset={SPAWN_ROT_OFFSET_DEG} turn_right={SPAWN_TURN_RIGHT_DEG})")

    # 7. main.level.json + info.json
    # Prepara waterways OSM reali + dem sampler per River nativi BeamNG
    try:
        project_fn, rd_all = _project_factory_from_road_data()
        waterways_osm = rd_all.get("waterways", []) or []
    except Exception as _e:
        print(f"  WARN preparing waterways: {_e}")
        project_fn = None
        waterways_osm = []
    dem_sampler = make_dem_sampler_blender(info, z_offset_blender)

    write_level_json(LEVEL_DIR, road_rel, world_rel, roadside_rel,
                      terrain_rel, extra_buildings_rel, road_details_rel,
                      embankments_rel, vegetation_rel, spawn, heading,
                      max_height, elev_min, z_offset_blender,
                      waterways=waterways_osm,
                      dem_sampler=dem_sampler,
                      project_fn=project_fn,
                      signs_shape_rel=signs_rel,
                      video_shape_rel=video_rel)
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
