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
    # Formato identico a levels/autotest/info.json per massima compatibilita'
    # BeamNG 0.38. Lo spawnPoint si definisce dentro main.level.json con classe
    # SpawnSphere (non qui).
    return {
        "title": LEVEL_TITLE,
        "description": DESCRIPTION,
        "authors": AUTHOR,
        "previews": ["main_preview.png"],
        "size": [12288, 12288],
        "biome": "temperate",
        "roads": "few",
        "suitablefor": "Freeroam",
        "features": "hills",
        "isAuxiliary": False,
        "supportsTraffic": False,
        "supportsTimeOfDay": True,
    }


def materials_json() -> dict:
    # Formato BeamNG 0.38: ogni materiale DEVE avere "class": "Material" e
    # "Stages" (4 stages dove di solito solo [0] e' popolato).
    def mat(name: str, map_to: str, color_map_rel: str | None = None,
            base_color_rgb: tuple[float, float, float] = (0.6, 0.6, 0.6),
            pid: str = "00000000-0000-0000-0000-000000000000") -> dict:
        stage0: dict = {}
        if color_map_rel:
            stage0["colorMap"] = f"/levels/{LEVEL_NAME}/{color_map_rel}"
        else:
            stage0["diffuseColor"] = [*base_color_rgb, 1.0]
        return {
            "name": name,
            "mapTo": map_to,
            "class": "Material",
            "persistentId": pid,
            "Stages": [stage0, {}, {}, {}],
            "materialTag0": "Miscellaneous",
        }

    return {
        "m_asphalt_road_damaged": mat(
            "m_asphalt_road_damaged", "m_asphalt_road_damaged",
            "art/road/asphalt_base",
            pid="a1b2c3d4-0001-0000-0000-000000000001",
        ),
        "m_terrain_diffuse": mat(
            "m_terrain_diffuse", "m_terrain_diffuse",
            "art/terrains/satellite_diffuse",
            pid="a1b2c3d4-0002-0000-0000-000000000002",
        ),
        "m_building_generic": mat(
            "m_building_generic", "m_building_generic",
            None, (0.70, 0.65, 0.55),
            pid="a1b2c3d4-0003-0000-0000-000000000003",
        ),
        "m_guardrail_steel": mat(
            "m_guardrail_steel", "m_guardrail_steel",
            None, (0.70, 0.72, 0.75),
            pid="a1b2c3d4-0004-0000-0000-000000000004",
        ),
        "m_wall_drystone": mat(
            "m_wall_drystone", "m_wall_drystone",
            None, (0.55, 0.50, 0.45),
            pid="a1b2c3d4-0005-0000-0000-000000000005",
        ),
    }


def decals_json() -> dict:
    return {
        "header": {
            "name": "DecalData File",
            "comments": "// generato da Macerone3D",
            "version": 1,
        },
        "instances": {},
    }


def map_json() -> dict:
    return {"segments": {}}


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


def build_preview_from_satellite(dst: Path, size: int = 512) -> bool:
    """
    Genera preview.jpg ritagliando il satellite ESRI sul bbox della centerline.
    Piu' affidabile e significativo di un render Blender headless.
    """
    from PIL import Image, ImageDraw
    satellite = ROOT / "output" / "satellite.png"
    satellite_meta = ROOT / "output" / "satellite_bbox.json"
    centerline = ROOT / "output" / "centerline.csv"
    road_data = ROOT / "road_data.json"
    if not satellite.exists() or not satellite_meta.exists():
        Image.new("RGB", (size, size), (120, 120, 120)).save(dst, "JPEG")
        print(f"  preview placeholder (manca satellite): {dst.name}")
        return False

    meta = json.loads(satellite_meta.read_text(encoding="utf-8"))
    bbox = meta.get("bbox_geo", {})
    north = bbox.get("north")
    south = bbox.get("south")
    west = bbox.get("west")
    east = bbox.get("east")
    if None in (north, south, west, east):
        Image.new("RGB", (size, size), (120, 120, 120)).save(dst, "JPEG")
        print(f"  preview placeholder (bbox_geo incompleto)")
        return False

    # bbox della centerline in lat/lon (da road_data.json)
    data = json.loads(road_data.read_text(encoding="utf-8"))
    cl = data["centerline"]
    lat_min = min(p["lat"] for p in cl)
    lat_max = max(p["lat"] for p in cl)
    lon_min = min(p["lon"] for p in cl)
    lon_max = max(p["lon"] for p in cl)
    # piccolo margine
    pad_lat = (lat_max - lat_min) * 0.05
    pad_lon = (lon_max - lon_min) * 0.05
    lat_min -= pad_lat; lat_max += pad_lat
    lon_min -= pad_lon; lon_max += pad_lon

    img = Image.open(satellite).convert("RGB")
    W, H = img.size
    # mappa lat/lon -> pixel (ESRI tile zoom 17, Web Mercator)
    # ma l'immagine e' salvata in proiezione Mercator con bbox in lat/lon,
    # quindi uso lat/lon diretti per crop (approx lineare a piccole scale).
    def to_px(lat, lon):
        u = (lon - west) / (east - west) * W
        v = (north - lat) / (north - south) * H
        return u, v

    x0, y0 = to_px(lat_max, lon_min)
    x1, y1 = to_px(lat_min, lon_max)
    x0 = max(0, int(x0)); y0 = max(0, int(y0))
    x1 = min(W, int(x1)); y1 = min(H, int(y1))
    crop = img.crop((x0, y0, x1, y1))

    # square-ify con padding nero sopra/sotto
    w, h = crop.size
    side = max(w, h)
    squared = Image.new("RGB", (side, side), (0, 0, 0))
    squared.paste(crop, ((side - w) // 2, (side - h) // 2))
    squared = squared.resize((size, size), Image.LANCZOS)

    # disegna la centerline in rosso sopra
    draw = ImageDraw.Draw(squared)
    cl_px = []
    for p in cl:
        u, v = to_px(p["lat"], p["lon"])
        # in coordinate squared (centrato nel side)
        u -= x0; v -= y0
        u += (side - w) / 2.0
        v += (side - h) / 2.0
        u = u * size / side
        v = v * size / side
        cl_px.append((u, v))
    if len(cl_px) >= 2:
        draw.line(cl_px, fill=(255, 40, 40), width=3)

    if dst.suffix.lower() == ".png":
        squared.save(dst, "PNG", optimize=True)
    else:
        squared.save(dst, "JPEG", quality=88)
    print(f"  preview generata da satellite + centerline: {dst.name}")
    return True


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

    # main.level.json: usiamo il template estratto da levels/autotest (BeamNG
    # stock). Il file contiene la scena minima valida (SimGroup/LevelInfo/
    # ScatterSky/CloudLayer/...). Modifichiamo solo pochi campi se serve.
    template_level = Path(__file__).resolve().parent / "templates" / "main.level.json"
    if template_level.exists():
        (level_dir / "main.level.json").write_text(
            template_level.read_text(encoding="utf-8"), encoding="utf-8"
        )
        print(f"  main.level.json: template autotest copiato")
    else:
        print(f"  ATTENZIONE: manca {template_level}, livello senza scena")

    # main.materials.json nella root del livello (formato BeamNG 0.38)
    (level_dir / "main.materials.json").write_text(
        json.dumps(materials_json(), indent=2), encoding="utf-8"
    )
    (level_dir / "main.decals.json").write_text(
        json.dumps(decals_json(), indent=2), encoding="utf-8"
    )
    (level_dir / "map.json").write_text(
        json.dumps(map_json(), indent=2), encoding="utf-8"
    )

    # Copy artifacts prodotti dagli altri script. Heightmap/roads/forest vanno
    # in 'import_data/' (file custom nostri che BeamNG non deve cercare di
    # interpretare al load del livello -- saranno usati solo dal Terrain and
    # Road Importer, lanciato manualmente via F11).
    print("Copio artifacts...")
    import_dir = level_dir / "import_data"
    import_dir.mkdir(exist_ok=True)
    copy_if_exists(BEAMNG_OUT / "heightmap.png", import_dir / "heightmap.png")
    copy_if_exists(BEAMNG_OUT / "terrain_info.json",
                    import_dir / "terrain_info.json")
    copy_if_exists(BEAMNG_OUT / "roads.json", import_dir / "roads.json")
    copy_if_exists(BEAMNG_OUT / "forest.json", import_dir / "forest.json")
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
    # Satellite diffuse: preferisco PNG (BeamNG TerrainMaterial vuole PNG/DDS).
    # Se manca, JPEG in fallback.
    satellite_png = BEAMNG_OUT / "satellite_diffuse.png"
    satellite_jpg = BEAMNG_OUT / "satellite_diffuse.jpg"
    terr_dir = level_dir / "art" / "terrains"
    if satellite_png.exists():
        copy_if_exists(satellite_png, terr_dir / "satellite_diffuse.png")
    if satellite_jpg.exists():
        copy_if_exists(satellite_jpg, terr_dir / "satellite_diffuse.jpg")
    if not (satellite_png.exists() or satellite_jpg.exists()):
        copy_if_exists(ROOT / "output" / "satellite.png",
                        terr_dir / "satellite_diffuse.png")

    # Preview per il menu BeamNG. v0.38+ cerca 'main_preview.png', le vecchie
    # guide online riferiscono 'preview.jpg'. Creiamo entrambi per sicurezza.
    build_preview_from_satellite(level_dir / "main_preview.png")
    build_preview_from_satellite(level_dir / "preview.jpg")

    print(f"Mod skeleton pronta in {MOD_DIR}")


if __name__ == "__main__":
    main()
