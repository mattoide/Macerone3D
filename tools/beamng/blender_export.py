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
        if obj.type not in {"MESH", "EMPTY"}:
            continue
        wx, wy, wz = obj.matrix_world.translation
        # scarta alberi fuori dal terrain BeamNG
        bx = wx + dx
        by = wy + dy
        if bx < 0 or by < 0:
            continue
        rot_z = obj.rotation_euler.z if hasattr(obj, "rotation_euler") else 0.0
        sx, sy, sz = obj.scale
        avg_scale = (sx + sy + sz) / 3.0
        instances.append({
            "x": round(bx, 2),
            "y": round(by, 2),
            "z": round(wz, 2),  # elevation locale (verra' proiettato sul terrain)
            "rot_z": round(rot_z, 3),
            "scale": round(avg_scale, 3),
            "type": tree_type_for(obj.name),
        })
    payload = {
        "format": "macerone_beamng_forest_v1",
        "coordinate_space": "terrain_local (origin=SW corner, X=east, Y=north)",
        "note": "Import via BeamNG Forest Editor o script Lua. Il campo 'type' "
                "va mappato a un ForestItem definito nella mod.",
        "instance_count": len(instances),
        "instances": instances,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"forest.json: {len(instances)} istanze scritte in {out_path}")
    return len(instances)


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

    print("blender_export.py OK")


if __name__ == "__main__":
    main()
