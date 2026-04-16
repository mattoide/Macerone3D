"""
Crea lo scheletro della mod BeamNG.drive per il Valico del Macerone.

Struttura generata (pronta a essere zippata):

  output/beamng/mod/
    info.json                          (metadati mod)
    README_install.md                  (istruzioni installazione manuale)
    levels/
      macerone/
        info.json                      (metadati livello)
        main.level.json                (scena: sun, weather, spawn)
        preview.jpg                    (placeholder, sostituire)
        terrain/
          heightmap.png                (copiato da output/beamng/)
        roads.json                     (copiato)
        forest.json                    (copiato)
        art/
          shapes/
            buildings.dae
            guardrails.dae
            walls.dae
            props.dae
          terrains/
            satellite_diffuse.png      (copia della texture satellitare)
          materials.json               (materiali: asfalto, edificio, ecc.)
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BEAMNG_OUT = ROOT / "output" / "beamng"
MOD_DIR = BEAMNG_OUT / "mod"

MOD_NAME = "macerone3d"
LEVEL_NAME = "macerone"
LEVEL_TITLE = "SS17 Valico del Macerone"
AUTHOR = "mattoide"
VERSION = "0.1.0"
DESCRIPTION = (
    "Ricostruzione 3D del tratto di SS17 sul Valico del Macerone (Molise), "
    "circa 16.6 km. Strada, elevazione, edifici e foreste generati da OSM + "
    "EU-DEM 25m + satellite ESRI."
)


def mod_info() -> dict:
    return {
        "title": LEVEL_TITLE,
        "description": DESCRIPTION,
        "author": AUTHOR,
        "version": VERSION,
        "tag": ["level", "map", "italy", "mountain", "real-road"],
    }


def level_info() -> dict:
    return {
        "title": LEVEL_TITLE,
        "description": DESCRIPTION,
        "authors": AUTHOR,
        "country": "IT",
        "size": [12288, 12288],
        "defaultSpawnPointName": "spawn_start",
        "previews": ["preview.jpg"],
        "spawnPoints": [
            {
                "name": "spawn_start",
                "translation": [3600.0, 1800.0, 500.0],
                "rotation": [0.0, 0.0, 0.0, 1.0],
            },
            {
                "name": "spawn_summit",
                "translation": [6144.0, 6144.0, 900.0],
                "rotation": [0.0, 0.0, 0.0, 1.0],
            },
        ],
    }


def main_level_json() -> dict:
    return {
        "name": LEVEL_NAME,
        "sun": {
            "azimuth": 0.9,
            "elevation": 0.75,
            "color": [1.0, 0.95, 0.85, 1.0],
            "brightness": 2.0,
        },
        "sky": {
            "ambientColor": [0.35, 0.4, 0.5, 1.0],
            "fogDensity": 0.0001,
            "fogColor": [0.65, 0.7, 0.75, 1.0],
        },
        "physics": {"gravity": -9.81},
        "time_of_day": 0.32,
    }


def materials_json() -> dict:
    return {
        "m_asphalt_road_damaged": {
            "mapTo": "m_asphalt_road_damaged",
            "baseColorMap": "levels/macerone/art/road/asphalt_base.png",
            "normalMap": "levels/macerone/art/road/asphalt_normal.png",
            "roughnessMap": "levels/macerone/art/road/asphalt_roughness.png",
            "roughnessFactor": 0.9,
            "metalnessFactor": 0.0,
            "uvScale": [0.2, 0.2],
            "__note": "texture da fornire o riferire con .link al pacchetto 'art_common'.",
        },
        "m_asphalt_road_damaged_small": {
            "mapTo": "m_asphalt_road_damaged_small",
            "baseColor": [0.35, 0.35, 0.35, 1.0],
            "roughnessFactor": 0.95,
            "metalnessFactor": 0.0,
        },
        "m_terrain_diffuse": {
            "mapTo": "m_terrain_diffuse",
            "baseColorMap": "levels/macerone/art/terrains/satellite_diffuse.png",
            "roughnessFactor": 1.0,
            "metalnessFactor": 0.0,
            "uvScale": [1.0, 1.0],
        },
        "m_building_generic": {
            "mapTo": "m_building_generic",
            "baseColor": [0.7, 0.65, 0.55, 1.0],
            "roughnessFactor": 0.85,
            "metalnessFactor": 0.0,
        },
        "m_guardrail_steel": {
            "mapTo": "m_guardrail_steel",
            "baseColor": [0.7, 0.72, 0.75, 1.0],
            "roughnessFactor": 0.4,
            "metalnessFactor": 0.9,
        },
        "m_wall_drystone": {
            "mapTo": "m_wall_drystone",
            "baseColor": [0.55, 0.5, 0.45, 1.0],
            "roughnessFactor": 0.95,
            "metalnessFactor": 0.0,
        },
    }


def install_readme() -> str:
    return f"""# {LEVEL_TITLE} — installazione mod BeamNG.drive

## Installazione veloce (mod unpacked)

1. Copia l'intera cartella `mod/` dentro:
   `Documents/BeamNG.drive/<versione>/mods/unpacked/{MOD_NAME}/`
   (dove `<versione>` è es. `0.37`, controllare il proprio installato)

2. Riavvia BeamNG.drive.

3. Dal menu: Singleplayer → Freeroam → cerca "{LEVEL_TITLE}".

## Installazione finale (mod zippata)

1. Zippa il contenuto di `mod/` (NON la cartella `mod/` stessa):
   il file `info.json` e `levels/` devono essere alla radice dello zip.
2. Rinomina lo zip in `{MOD_NAME}.zip`.
3. Copia in `Documents/BeamNG.drive/<versione>/mods/{MOD_NAME}.zip`.

## Primo import nel World Editor (una tantum)

Questa mod fornisce il heightmap e la strada in forma "importer-friendly".
Al primo avvio del livello bisogna:

1. Aprire il World Editor (F11).
2. `Tools → Terrain and Road Importer`.
3. Settare:
   - Heightmap PNG: `levels/macerone/terrain/heightmap.png`
   - Height Scale: **1200** m (corrisponde a `terrain_info.json.terrain_height_scale_m`)
   - Meters Per Pixel: **3.0**
   - Roads JSON: `levels/macerone/roads.json`
4. Click Import. Il tool creera' il terrain e piazzera' i DecalRoad terraformando
   il terreno sotto la SS17 e le strade secondarie.
5. Salva il livello (File → Save). Dal salvataggio successivo non serve piu' reimportare.

## Forest / alberi

Il file `levels/macerone/forest.json` contiene le istanze degli alberi (posizioni,
tipo). Dato che il formato `.forest4.json` di BeamNG e' interno al Forest Editor,
lo usa uno script Lua della mod in fase di onload del livello (vedi
`levels/macerone/scripts/populate_forest.lua` se presente) oppure si importano
manualmente con il Forest Editor.

## File manuali da aggiungere

- `preview.jpg` (512x512) — attualmente placeholder. Fai un render dalla
  OverviewCam del .blend originale, esporta come JPG e sostituisci.
- texture PBR di road/terrain — i materiali referenziano path tipici BeamNG; se
  mancano, il tool di default usera' colori fallback.

## Formato mesh

Se Blender 5.x ha esportato in .obj anziche' .dae (Collada exporter rimosso
dai core addons), sono nella stessa cartella con estensione `.obj`. Opzioni:

a) Installa Blender 4.4 LTS in parallelo, apri il blend, esporta in Collada.
b) Usa l'import OBJ del World Editor (Tools -> Import Mesh) e salva come
   static shape.
c) Converti .obj -> .dae con `assimp` (`assimp export buildings.obj buildings.dae`).

## Note

- Origine del terrain: corner sud-ovest = (0, 0). La SS17 parte attorno a
  (3600, 1800) e finisce attorno a (6200, 8800) circa.
- Progetto generato da: https://github.com/{AUTHOR}/Macerone3D
- Licenza texture satellitari: ESRI World Imagery — verifica termini d'uso
  per distribuzione.
"""


def copy_if_exists(src: Path, dst: Path) -> bool:
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        print(f"  cp {src.name} -> {dst.relative_to(MOD_DIR)}")
        return True
    print(f"  skip (manca {src.name})")
    return False


def main() -> None:
    if MOD_DIR.exists():
        shutil.rmtree(MOD_DIR)
    MOD_DIR.mkdir(parents=True)

    level_dir = MOD_DIR / "levels" / LEVEL_NAME
    (level_dir / "terrain").mkdir(parents=True, exist_ok=True)
    (level_dir / "art" / "shapes").mkdir(parents=True, exist_ok=True)
    (level_dir / "art" / "terrains").mkdir(parents=True, exist_ok=True)

    # Metadata
    (MOD_DIR / "info.json").write_text(
        json.dumps(mod_info(), indent=2), encoding="utf-8"
    )
    (MOD_DIR / "README_install.md").write_text(install_readme(), encoding="utf-8")
    (level_dir / "info.json").write_text(
        json.dumps(level_info(), indent=2), encoding="utf-8"
    )
    (level_dir / "main.level.json").write_text(
        json.dumps(main_level_json(), indent=2), encoding="utf-8"
    )
    (level_dir / "art" / "materials.json").write_text(
        json.dumps(materials_json(), indent=2), encoding="utf-8"
    )

    # Copy artifacts prodotti dagli altri script
    print("Copio artifacts...")
    copy_if_exists(BEAMNG_OUT / "heightmap.png",
                    level_dir / "terrain" / "heightmap.png")
    copy_if_exists(BEAMNG_OUT / "terrain_info.json",
                    level_dir / "terrain" / "terrain_info.json")
    copy_if_exists(BEAMNG_OUT / "roads.json", level_dir / "roads.json")
    copy_if_exists(BEAMNG_OUT / "forest.json", level_dir / "forest.json")
    for basename in ("buildings", "guardrails", "walls", "props"):
        # Prova .dae (preferito da BeamNG); fallback a .obj se il Collada
        # exporter non era disponibile (Blender 5.x).
        dae_src = BEAMNG_OUT / "dae" / f"{basename}.dae"
        obj_src = BEAMNG_OUT / "dae" / f"{basename}.obj"
        dst_dir = level_dir / "art" / "shapes"
        if dae_src.exists():
            copy_if_exists(dae_src, dst_dir / f"{basename}.dae")
        elif obj_src.exists():
            copy_if_exists(obj_src, dst_dir / f"{basename}.obj")
            # copia anche il .mtl se c'e'
            mtl_src = obj_src.with_suffix(".mtl")
            if mtl_src.exists():
                copy_if_exists(mtl_src, dst_dir / f"{basename}.mtl")
    copy_if_exists(ROOT / "output" / "satellite.png",
                    level_dir / "art" / "terrains" / "satellite_diffuse.png")

    # preview.jpg placeholder: 1x1 pixel grigio (da sostituire manualmente)
    placeholder = level_dir / "preview.jpg"
    if not placeholder.exists():
        from PIL import Image
        Image.new("RGB", (512, 512), (120, 120, 120)).save(placeholder, "JPEG")
        print(f"  creato placeholder {placeholder.relative_to(MOD_DIR)}")

    print(f"Mod skeleton pronta in {MOD_DIR}")


if __name__ == "__main__":
    main()
