"""
Apre output/macerone.blend ed esporta per BeamNG:
  - buildings.dae   (edifici OSM entro BUILD_MAX_DIST m dalla centerline)
  - guardrails.dae  (tutta la collezione Guardrails)
  - walls.dae       (muretti a secco entro WALL_MAX_DIST m)
  - props.dae       (rocce + cipressi + pali luce + cartelli entro PROP_MAX_DIST m)
  - forest.json     (istanze alberi: x, y, z, rot_z, scale, type)

Tutti i DAE sono in coordinate del terrain BeamNG (origine SW corner), cioe'
traslati di (-x_min, -y_min, 0) rispetto al Blender originale.

Uso:
  blender --background output/macerone.blend --python tools/beamng/blender_export.py

oppure orchestrato da build_mod.py (che chiama Blender con i parametri giusti).
"""
from __future__ import annotations

import csv
import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector

# --- Config -----------------------------------------------------------------
BUILD_MAX_DIST_M = 250.0
WALL_MAX_DIST_M = 80.0
PROP_MAX_DIST_M = 100.0
GUARDRAIL_ALL = True
INCLUDE_SIGNALS = True
INCLUDE_POLES = True

# Esportiamo alberi come forest instances (non come DAE massivo)
TREE_TYPE_FROM_NAME = {
    "Cipresso": "cypress",
    "Cypress": "cypress",
    "Tree_Cipresso": "cypress",
    "Shrub": "shrub",
    "Arbusto": "shrub",
    "Grass": "grass_tuft",
    "Ciuffo": "grass_tuft",
}
DEFAULT_TREE_TYPE = "tree_medium"


# --- Args parsing via "--" ---------------------------------------------------
def argv_after_dash() -> list[str]:
    if "--" in sys.argv:
        return sys.argv[sys.argv.index("--") + 1:]
    return []


def resolve_paths() -> tuple[Path, Path, Path]:
    args = argv_after_dash()
    if len(args) >= 2:
        terrain_info_path = Path(args[0])
        centerline_csv = Path(args[1])
        out_dir = Path(args[2]) if len(args) >= 3 else terrain_info_path.parent
    else:
        root = Path(bpy.data.filepath).resolve().parent.parent if bpy.data.filepath else Path.cwd()
        terrain_info_path = root / "output" / "beamng" / "terrain_info.json"
        centerline_csv = root / "output" / "centerline.csv"
        out_dir = root / "output" / "beamng"
    out_dir.mkdir(parents=True, exist_ok=True)
    return terrain_info_path, centerline_csv, out_dir


# --- Centerline + distance check --------------------------------------------
def load_centerline(csv_path: Path) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pts.append((float(row["x"]), float(row["y"])))
    return pts


class Corridor:
    """Grid-based spatial index per distanza rapida da centerline."""

    def __init__(self, pts: list[tuple[float, float]], cell: float = 60.0):
        self.cell = cell
        self.buckets: dict[tuple[int, int], list[tuple[float, float]]] = {}
        for x, y in pts:
            key = (int(x // cell), int(y // cell))
            self.buckets.setdefault(key, []).append((x, y))

    def dist_to_road(self, x: float, y: float, search_cells: int = 4) -> float:
        ix = int(x // self.cell)
        iy = int(y // self.cell)
        dmin2 = float("inf")
        for di in range(-search_cells, search_cells + 1):
            for dj in range(-search_cells, search_cells + 1):
                bucket = self.buckets.get((ix + di, iy + dj))
                if not bucket:
                    continue
                for px, py in bucket:
                    d2 = (px - x) ** 2 + (py - y) ** 2
                    if d2 < dmin2:
                        dmin2 = d2
        return math.sqrt(dmin2) if dmin2 != float("inf") else float("inf")


# --- Collada export helper --------------------------------------------------
def select_only(objs):
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    if objs:
        bpy.context.view_layer.objects.active = objs[0]


def translate_temp(objs, dx: float, dy: float):
    old = [(o, o.location.copy()) for o in objs]
    for o in objs:
        o.location.x += dx
        o.location.y += dy
    return old


def restore_locations(old):
    for o, loc in old:
        o.location = loc


def export_dae(path: Path) -> bool:
    # Blender 5.x: Collada core exporter e' diventato opzionale. Proviamo DAE
    # con parametri minimi; se fallisce, fallback a OBJ.
    try:
        bpy.ops.wm.collada_export(filepath=str(path), selected=True,
                                   apply_modifiers=True, triangulate=True)
        print(f"  -> scritto {path}")
        return True
    except Exception as e:
        print(f"  (DAE export fallito: {e}; fallback a OBJ)")
        try:
            obj_path = path.with_suffix(".obj")
            bpy.ops.wm.obj_export(filepath=str(obj_path),
                                   export_selected_objects=True,
                                   apply_modifiers=True,
                                   forward_axis="NEGATIVE_Z", up_axis="Y")
            print(f"  -> scritto {obj_path} (OBJ fallback)")
            return True
        except Exception as e2:
            print(f"  !! anche OBJ fallito: {e2}")
            return False


# --- Main export logic ------------------------------------------------------
def export_category(name: str, objs: list, dx: float, dy: float,
                    out_path: Path) -> int:
    if not objs:
        print(f"[{name}] nessun oggetto candidato")
        return 0
    select_only(objs)
    old = translate_temp(objs, dx, dy)
    ok = export_dae(out_path)
    restore_locations(old)
    n = len(objs) if ok else 0
    print(f"[{name}] {n} oggetti esportati")
    return n


def obj_is_within(obj, corridor: Corridor, max_dist: float) -> bool:
    wx, wy, _ = obj.matrix_world.translation
    return corridor.dist_to_road(wx, wy) <= max_dist


def tree_type_for(name: str) -> str:
    for prefix, t in TREE_TYPE_FROM_NAME.items():
        if prefix.lower() in name.lower():
            return t
    return DEFAULT_TREE_TYPE


def extract_islands_from_mesh(obj, terrain_size_m: float) -> list[dict]:
    """
    I mesh 'foresta' sono mergiati: ogni albero e' una componente connessa
    dentro il mesh. Qui estraiamo una lista di istanze {x,y,z,size} facendo
    union-find sugli edge del mesh.

    Ritorna un elenco di istanze nel sistema WORLD di Blender.
    """
    me = obj.data
    n = len(me.vertices)
    if n == 0:
        return []
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in me.edges:
        a, b = e.vertices[0], e.vertices[1]
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # raggruppa i vertici per root
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    mw = obj.matrix_world
    verts = me.vertices
    instances = []
    for vids in groups.values():
        if len(vids) < 3:
            continue
        # centroide dell'isola + altezza (z max - z min) come "size"
        xs = [verts[i].co.x for i in vids]
        ys = [verts[i].co.y for i in vids]
        zs = [verts[i].co.z for i in vids]
        cx = sum(xs) / len(xs)
        cy = sum(ys) / len(ys)
        # z base = quota del vertice piu' basso (la base dell'albero tocca terra)
        z_base = min(zs)
        height = max(zs) - z_base
        wp = mw @ Vector((cx, cy, z_base))
        instances.append({
            "x": round(wp.x, 2),
            "y": round(wp.y, 2),
            "z": round(wp.z, 2),
            "size": round(height, 2),
            "verts": len(vids),
        })
    return instances


def export_forest_json(corridor: Corridor, dx: float, dy: float,
                        out_path: Path) -> int:
    trees_col = bpy.data.collections.get("Trees")
    if not trees_col:
        print("Nessuna collezione 'Trees' trovata")
        out_path.write_text(json.dumps({"instances": []}, indent=2),
                              encoding="utf-8")
        return 0

    instances = []
    for obj in trees_col.all_objects:
        if obj.type != "MESH" or obj.data is None:
            continue
        # Il blend ha trunk + canopy come mesh separati per lo stesso albero;
        # processiamo SOLO i trunk (piu' fedeli alla base) per evitare doppioni.
        low = obj.name.lower()
        if "canopy" in low or "canopi" in low or "chioma" in low:
            print(f"  {obj.name}: skip (canopy, conta gia' col trunk)")
            continue
        obj_type = tree_type_for(obj.name)
        islands = extract_islands_from_mesh(obj, 0.0)
        kept = 0
        for inst in islands:
            bx = inst["x"] + dx
            by = inst["y"] + dy
            if bx < 0 or by < 0:
                continue
            size = inst["size"]
            # scale relativa: assumiamo un albero "tipico" alto 8 m
            avg_scale = size / 8.0 if size > 0.5 else 1.0
            instances.append({
                "x": round(bx, 2),
                "y": round(by, 2),
                "z": round(inst["z"], 2),
                "rot_z": round(hash((bx, by)) % 628 / 100.0, 3),
                "scale": round(max(0.3, min(2.5, avg_scale)), 3),
                "type": obj_type,
            })
            kept += 1
        print(f"  {obj.name}: {len(islands)} isole -> {kept} istanze "
              f"(tipo={obj_type})")

    payload = {
        "format": "macerone_beamng_forest_v1",
        "coordinate_space": "terrain_local (origin=SW corner, X=east, Y=north)",
        "note": "Import via BeamNG Forest Editor o script Lua. Il campo 'type' "
                "va mappato a un ForestItem definito nella mod.",
        "instance_count": len(instances),
        "instances": instances,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"forest.json: {len(instances)} istanze totali scritte in {out_path}")
    return len(instances)


def render_preview(out_path: Path, size: int = 512) -> bool:
    """Render 512x512 JPG della scena. Preferisce OverviewCam se c'e'."""
    scene = bpy.context.scene
    cam = None
    for obj in bpy.data.objects:
        if obj.type == "CAMERA" and "overview" in obj.name.lower():
            cam = obj
            break
    if cam is None:
        # fallback: qualunque camera
        for obj in bpy.data.objects:
            if obj.type == "CAMERA":
                cam = obj
                break
    if cam is None:
        print("Nessuna camera trovata, preview skipped")
        return False
    scene.camera = cam
    scene.render.resolution_x = size
    scene.render.resolution_y = size
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "JPEG"
    scene.render.image_settings.quality = 85
    scene.render.filepath = str(out_path)
    scene.render.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in {
        e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items
    } else "BLENDER_EEVEE"
    print(f"Rendering preview con camera '{cam.name}' engine={scene.render.engine} ...")
    try:
        bpy.ops.render.render(write_still=True)
        print(f"  -> scritto {out_path}")
        return True
    except Exception as e:
        print(f"  !! render fallito: {e}")
        return False


def main() -> None:
    terrain_info_path, centerline_csv, out_dir = resolve_paths()
    print("terrain_info :", terrain_info_path)
    print("centerline   :", centerline_csv)
    print("out_dir      :", out_dir)

    info = json.loads(terrain_info_path.read_text(encoding="utf-8"))
    x_min = info["terrain_origin_local_m"]["x"]
    y_min = info["terrain_origin_local_m"]["y"]
    dx = -x_min
    dy = -y_min
    print(f"Traslazione al sistema terrain BeamNG: dx={dx:.1f} dy={dy:.1f}")

    cl_pts = load_centerline(centerline_csv)
    corridor = Corridor(cl_pts)
    print(f"Centerline: {len(cl_pts)} punti")

    dae_dir = out_dir / "dae"
    dae_dir.mkdir(exist_ok=True)

    def collection_objs(name: str):
        col = bpy.data.collections.get(name)
        return [o for o in col.all_objects if o.type == "MESH"] if col else []

    # I mesh sono gia' mergiati in pochi oggetti per collezione; non filtriamo
    # per distanza (tanto includono naturalmente solo cio' che era nel corridoio
    # di generazione). La centerline e' comunque usata per la forest.

    export_category("Buildings", collection_objs("Buildings"), dx, dy,
                     dae_dir / "buildings.dae")
    export_category("Guardrails", collection_objs("Guardrails"), dx, dy,
                     dae_dir / "guardrails.dae")
    export_category("Walls", collection_objs("Walls"), dx, dy,
                     dae_dir / "walls.dae")
    props = collection_objs("Rocks") + collection_objs("Signals")
    export_category("Props", props, dx, dy, dae_dir / "props.dae")

    # Forest instances (alberi)
    export_forest_json(corridor, dx, dy, out_dir / "forest.json")

    # Preview render 512x512 da OverviewCam
    render_preview(out_dir / "preview.jpg", size=512)

    print("blender_export.py OK")


if __name__ == "__main__":
    main()
