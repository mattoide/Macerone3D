"""
Genera il file terrain binary .ter per BeamNG.drive, evitando il Terrain and
Road Importer manuale. Dopo questo script la mod si carica con il terreno
del Valico del Macerone gia' presente.

Formato .ter (reverse-engineered da levels/autotest/theTerrain.ter v0.38):
  byte 0           version (uint8)                = 7
  bytes 1..4       size (uint32 LE)
  bytes 5..5+S*S*2 heightMap (size*size uint16 LE)
  next S*S bytes   layerMap (size*size uint8, indice del materiale)
  last             numMaterials (uint32 LE) + per ognuno: (length uint8) + ASCII

heightMap va da 0 (=quota minima) a 65535 (=quota minima + maxHeight metri),
dove maxHeight e' specificato nel TerrainBlock del main.level.json.

Input:
  output/beamng/heightmap.png    (PNG 16-bit gia' prodotto da build_heightmap.py)
  output/beamng/terrain_info.json

Output:
  mod/levels/macerone/theTerrain.ter
  mod/levels/macerone/theTerrain.terrain.json
  aggiorna main.level.json del livello per puntare al ter con maxHeight giusta
"""
from __future__ import annotations

import json
import re
import struct
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
BEAMNG_OUT = ROOT / "output" / "beamng"
HEIGHTMAP_PNG = BEAMNG_OUT / "heightmap.png"
TERRAIN_INFO = BEAMNG_OUT / "terrain_info.json"
MOD_LEVEL_DIR = BEAMNG_OUT / "mod" / "levels" / "macerone"

# Resize a 1024 per stare sul safe-side (autotest stock usa 1024).
# BeamNG tollera 2048 e 4096 ma su alcune macchine il caricamento crasha.
# Con 1024 @ 12 m/pixel -> 12288 m = stessa dimensione del mondo, risoluzione
# leggermente piu' bassa (da 3 m/pixel a 12 m/pixel) -> strada comunque ok.
TER_SIZE = 1024

MATERIAL_NAME = "macerone_ground"
MATERIAL_UUID = "a1b2c3d4-9999-0000-0000-000000000099"


def main() -> None:
    if not HEIGHTMAP_PNG.exists():
        print(f"manca {HEIGHTMAP_PNG}, lancia prima build_heightmap.py")
        return
    if not TERRAIN_INFO.exists():
        print(f"manca {TERRAIN_INFO}")
        return

    info = json.loads(TERRAIN_INFO.read_text(encoding="utf-8"))
    source_size = info["size_px"]
    mpp_source = info["meters_per_pixel"]
    elev_min = info["elevation_min_m"]
    elev_max = info["elevation_max_m"]
    max_height = float(elev_max - elev_min)
    extent_m = info["extent_m"]

    # Carica heightmap PNG16 come uint16 array
    Image.MAX_IMAGE_PIXELS = None
    im = Image.open(HEIGHTMAP_PNG)
    arr = np.array(im, dtype=np.uint16)
    if arr.shape != (source_size, source_size):
        print(f"heightmap ha shape {arr.shape}, atteso ({source_size},{source_size})")
        return
    print(f"Input heightmap: {source_size}x{source_size} @ {mpp_source} m/pixel "
          f"(elev {elev_min}-{elev_max} m)")

    # Resize a TER_SIZE x TER_SIZE via PIL (mean-approximating bilinear)
    if TER_SIZE != source_size:
        im_resized = im.resize((TER_SIZE, TER_SIZE), Image.BILINEAR)
        arr = np.array(im_resized, dtype=np.uint16)
        print(f"Downsampled a {TER_SIZE}x{TER_SIZE}")

    # Nel PNG row 0 = nord. In un terrain Torque3D (origine SW), row 0 corrisponde
    # al bordo SUD. Flippo verticalmente.
    arr = np.flipud(arr)

    # Layer map: tutto 0 = primo materiale
    layer = np.zeros((TER_SIZE, TER_SIZE), dtype=np.uint8)

    # --- Scrivi .ter ---
    out_ter = MOD_LEVEL_DIR / "theTerrain.ter"
    MOD_LEVEL_DIR.mkdir(parents=True, exist_ok=True)
    with out_ter.open("wb") as f:
        # version 9 = formato BeamNG 0.38. Layout identico a version 7
        # (version 7 genera warning "outdated" e fallisce il caricamento).
        f.write(struct.pack("<B", 9))             # version
        f.write(struct.pack("<I", TER_SIZE))      # size
        f.write(arr.tobytes(order="C"))            # heightMap uint16 LE row-major
        f.write(layer.tobytes(order="C"))          # layerMap uint8
        # materialNames: uint32 count + [uint8 len + ascii]*
        names = [MATERIAL_NAME]
        f.write(struct.pack("<I", len(names)))
        for n in names:
            nb = n.encode("ascii")
            f.write(struct.pack("<B", len(nb)))
            f.write(nb)
    print(f"Scritto {out_ter}  ({out_ter.stat().st_size} bytes)")

    # --- Scrivi theTerrain.terrain.json ---
    hm_size_cells = TER_SIZE * TER_SIZE
    terrain_json = {
        "binaryFormat": "version(char), size(unsigned int), heightMap(heightMapSize * heightMapItemSize), layerMap (layerMapSize * layerMapItemSize), materialNames",
        "datafile": f"levels/macerone/theTerrain.ter",
        "heightMapItemSize": 2,
        "heightMapSize": hm_size_cells,
        "heightmapImage": "levels/macerone/theTerrain.terrainheightmap.png",
        "layerMapItemSize": 1,
        "layerMapSize": hm_size_cells,
        "materials": [MATERIAL_NAME],
        "size": TER_SIZE,
    }
    (MOD_LEVEL_DIR / "theTerrain.terrain.json").write_text(
        json.dumps(terrain_json, indent=2), encoding="utf-8"
    )
    print(f"Scritto theTerrain.terrain.json")

    # --- Modifica main.level.json per mettere TerrainBlock giusto ---
    mlj = MOD_LEVEL_DIR / "main.level.json"
    if not mlj.exists():
        print(f"manca {mlj}, rilancia build_mod_skeleton.py prima")
        return
    text = mlj.read_text(encoding="utf-8")
    # Sostituisci il path del .ter (era puntato ad autotest dal template)
    text = text.replace("levels/autotest/theTerrain.ter",
                         "levels/macerone/theTerrain.ter")
    # squareSize del terrain e maxHeight devono corrispondere al nostro .ter
    # squareSize = m per cella. extent 12288m / 1024 celle = 12 m/cella.
    square_size = extent_m / TER_SIZE
    # regex sul blocco TerrainBlock
    def patch_terrainblock(m):
        block = m.group(0)
        # maxHeight
        block = re.sub(r'"maxHeight"\s*:\s*[\d\.e\-\+]+',
                        f'"maxHeight" : {max_height}', block)
        # squareSize: aggiungi se manca, modifica se esiste
        if '"squareSize"' in block:
            block = re.sub(r'"squareSize"\s*:\s*[\d\.e\-\+]+',
                            f'"squareSize" : {square_size}', block)
        else:
            block = block.replace('"maxHeight"',
                                    f'"squareSize" : {square_size},\n          "maxHeight"')
        # position: centra il terrain sull'origine
        half = extent_m / 2.0
        block = re.sub(r'"position"\s*:\s*\[[^\]]+\]',
                        f'"position" : [ {-half}, {-half}, {elev_min} ]',
                        block)
        return block

    text = re.sub(
        r'\{\s*"class"\s*:\s*"TerrainBlock".*?\}',
        patch_terrainblock, text, count=1, flags=re.S,
    )
    print(f"Patched TerrainBlock in main.level.json "
          f"(maxHeight={max_height}, squareSize={square_size:.2f}, "
          f"position=[{-extent_m/2}, {-extent_m/2}, {elev_min}])")

    # Patcha TUTTE le SpawnSphere per metterle sopra l'inizio della SS17.
    # Il primo punto della centerline nel sistema Blender e' circa (3552, -4449)
    # con z=72 (relativa a z_offset=elev_min). World z = elev_min + 72 + buffer.
    cl_csv = ROOT / "output" / "centerline.csv"
    # Il centerline.csv ha z relative all'elev min del DEM: z_real = csv.z + offset
    z_offset = float(info.get("z_offset_blender_m", 0.0))
    spawn_x = 0.0; spawn_y = 0.0; spawn_z = elev_max + 50.0  # fallback
    if cl_csv.exists():
        import csv as _csv
        with cl_csv.open(newline="", encoding="utf-8") as fcsv:
            row = next(_csv.DictReader(fcsv))
            spawn_x = float(row["x"])
            spawn_y = float(row["y"])
            # quota world BeamNG = quota reale = csv.z + z_offset_blender + 3m
            spawn_z = float(row["z"]) + z_offset + 3.0
    print(f"Spawn point: ({spawn_x:.1f}, {spawn_y:.1f}, {spawn_z:.1f})  "
          f"[z_offset={z_offset}]")

    pos_str = f'"position" : [ {spawn_x}, {spawn_y}, {spawn_z} ]'

    def patch_spawnsphere(m):
        block = m.group(0)
        if '"position"' in block:
            block = re.sub(r'"position"\s*:\s*\[[^\]]+\]', pos_str, block)
        else:
            # AGGIUNGI position dopo "class":"SpawnSphere"
            block = block.replace(
                '"class" : "SpawnSphere"',
                f'"class" : "SpawnSphere",\n          "name" : "spawn_start",\n          {pos_str}',
            )
        return block

    text = re.sub(
        r'\{\s*"class"\s*:\s*"SpawnSphere".*?\}',
        patch_spawnsphere, text, flags=re.S,
    )
    mlj.write_text(text, encoding="utf-8")
    print(f"Patched SpawnSphere(s) in main.level.json: pos=({spawn_x:.0f}, "
          f"{spawn_y:.0f}, {spawn_z:.1f})")

    # Aggiungi spawnPoints al info.json livello come fallback extra
    info_file = MOD_LEVEL_DIR / "info.json"
    if info_file.exists():
        lvl_info = json.loads(info_file.read_text(encoding="utf-8"))
        lvl_info["spawnPoints"] = [
            {
                "translation": [spawn_x, spawn_y, spawn_z],
                "rot": [0, 0, 0, 1],
                "objectname": "spawn_start",
            }
        ]
        lvl_info["defaultSpawnPointName"] = "spawn_start"
        info_file.write_text(json.dumps(lvl_info, indent=2), encoding="utf-8")
        print(f"Aggiornato info.json con spawnPoints")

    # --- Crea art/terrain/main.materials.json con TerrainMaterial custom ---
    # Il material del TERRAIN deve avere class=TerrainMaterial (non Material),
    # internalName matching il nome nel .ter, e groundmodelName per la fisica.
    # Path: levels/<name>/art/terrain/main.materials.json (singolare, NON terrains).
    terrain_mat_dir = MOD_LEVEL_DIR / "art" / "terrain"
    terrain_mat_dir.mkdir(parents=True, exist_ok=True)
    terrain_materials = {
        f"{MATERIAL_NAME}-{MATERIAL_UUID}": {
            "internalName": MATERIAL_NAME,
            "class": "TerrainMaterial",
            "persistentId": MATERIAL_UUID,
            "diffuseMap": "levels/macerone/art/terrains/satellite_diffuse",
            "diffuseSize": 12288,   # spazio coperto in metri dalla singola texture
            "detailMap": "levels/macerone/art/terrain/asphalt_base",
            "detailSize": 4,
            "detailStrength": 0.3,
            "detailDistance": 80,
            "groundmodelName": "ASPHALT",
        }
    }
    (terrain_mat_dir / "main.materials.json").write_text(
        json.dumps(terrain_materials, indent=2), encoding="utf-8"
    )
    print(f"Scritto art/terrain/main.materials.json con TerrainMaterial "
          f"'{MATERIAL_NAME}' (groundmodel=ASPHALT)")

    # Rimuovi l'eventuale material omonimo obsoleto dalla root main.materials.json
    root_materials_file = MOD_LEVEL_DIR / "main.materials.json"
    if root_materials_file.exists():
        root_mats = json.loads(root_materials_file.read_text(encoding="utf-8"))
        for k in ("macerone_satellite", MATERIAL_NAME):
            root_mats.pop(k, None)
        root_materials_file.write_text(
            json.dumps(root_mats, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
