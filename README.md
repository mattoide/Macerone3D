# Macerozz — Valico del Macerone (SS17) per BeamNG.drive

Ricostruzione 3D automatica del tratto di **SS17** che attraversa il **Valico del Macerone** (Molise), partendo solo dalle coordinate GPS. Output: file `.blend` e `.obj` importabili in BeamNG.drive.

## Endpoint del tracciato

- **Punto A:** `41.6258108, 14.2305322` — SS17 16, 86080 Isernia IS
- **Punto B:** `41.7085882, 14.144029`
- **Percorso:** vecchia SS17 via Valico del Macerone (NON la bypass SS17var)
- **Distanza:** ~16.6 km, dislivello ~565 m

## Pipeline

```
coordinate GPS
    │
    ▼
fetch_road.py         ─→  road_data.json        (tracciato OSM, DEM 25m, layer OSM)
    │
    ▼
fetch_satellite.py    ─→  output/satellite.png  (ESRI World Imagery zoom 17)
                          output/satellite_bbox.json
    │
    ▼
detect_lines.py       ─→  output/line_marks.json (dove c'è la linea bianca sul satellite)
    │
    ▼
blender_build.py      ─→  output/macerone.blend
                          output/macerone.obj + .mtl
                          output/centerline.csv
```

## Setup

```bash
pip install requests pillow numpy
```

Blender 5.1 installato in `C:\Program Files\Blender Foundation\Blender 5.1\blender.exe` (o equivalente).

## Uso

### 1. Scarica i dati strada (lento, ~5 min — fa query OSM + ~22000 punti DEM)

```bash
python fetch_road.py
```

Output: `road_data.json` (~1.4 MB).

### 2. Scarica la texture satellitare (~35 sec)

```bash
python fetch_satellite.py --zoom 17
```

Output: `output/satellite.png` (~38 MB) + `output/satellite_bbox.json`.

### 3. Rileva le linee bianche dal satellite (~5 sec)

```bash
python detect_lines.py
```

Output: `output/line_marks.json`.

### 4. Costruisci la scena in Blender (~3 sec)

```bash
"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe" --background --python blender_build.py
```

Output: `output/macerone.blend`, `output/macerone.obj`, `output/centerline.csv`.

### 5. Apri il risultato

```bash
"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe" output/macerone.blend
```

**Importante:** nel viewport premi `Z → Material Preview` (o clicca l'icona sfera a scacchi in alto a destra) per vedere la texture satellite e i materiali. In "Solid" vedi solo colori piatti.

Premi `Spacebar` per vedere la DriveCam percorrere la strada.

## Contenuto della scena

### Dati reali (OSM + DEM + satellite)
- Tracciato SS17 (nodi OSM via `ref="SS17"`, Dijkstra)
- Elevazione EU-DEM 25 m (Opentopodata)
- Perimetri OSM: edifici, boschi, corsi d'acqua, ponti/tunnel
- Texture terreno: ESRI World Imagery zoom 17
- Presenza/assenza linea mezzeria: rilevata dal satellite

### Dettagli procedurali (non posizioni reali)
- Alberi individuali nelle foreste (perimetro reale, distribuzione no)
- Cipressi, arbusti, ciuffi d'erba, rocce, muretti a secco
- Pali della luce + cavi catenaria
- Segnaletica verticale (delineatori, km marker, cartelli curva, cartelli velocità)
- Camini sui tetti
- Rappezzi asfalto, tombini, catarifrangenti

## Parametri principali (in `blender_build.py`)

```python
CORRIDOR_M = 120.0        # larghezza del corridoio attorno alla strada
CARVE_BUFFER_M = 12.0     # buffer carving terreno sotto la strada
CARVE_DEPTH_M = 0.8       # profondità del carving
CARVE_BLEND_FACTOR = 2.2  # ampiezza zona di raccordo
ROAD_EMBANKMENT = 0.35    # la strada sta sollevata di questi m sul DEM
SMOOTH_WINDOW = 5         # smoothing tracciato
SUBDIV_PER_SEG = 3        # resampling Catmull-Rom
BANK_MAX_DEG = 4.0        # banking massimo nelle curve
```

Cambi su queste costanti → rilancia solo `blender_build.py` (3 sec).

## Struttura oggetti in Blender

Organizzati in collection (Outliner):

- **Road** — asfalto, banchine, mezzeria, linee bordo, catarifrangenti, tombini, patches, stop lines
- **Terrain** — mesh DEM + carving + Perlin noise + texture satellite
- **Buildings** — walls, roofs, camini
- **Trees** — alberi boschi, roadside, cipressi, arbusti
- **Grass** — 92k+ ciuffi d'erba
- **Rocks** — sassi ottaedrali
- **Walls** — muretti a secco
- **Water** — fiumi/laghi da OSM
- **OtherRoads** — strade secondarie OSM
- **Signals** — segnaletica, delineatori, pali luce, cavi
- **Guardrails** — automatici sui tratti esposti
- **Debug** — nastro evidenziatore arancione + marker start/end (disattiva prima dell'export a BeamNG)

## Build mod BeamNG.drive

Un singolo comando genera la mod installabile e la copia in BeamNG:

```bash
python tools/beamng/build_full_mod.py
```

Output: `output/beamng/macerone3d.zip` automaticamente copiato in `C:\Users\Matto\AppData\Local\BeamNG\BeamNG.drive\current\mods\`. Build time: ~30 sec.

### Cosa genera: 8 TSStatic mesh nel level

| # | TSStatic | Contenuto |
|---|----------|-----------|
| 1 | `macerone_road_mesh` | Road Blender (asfalto, banchine, linee, catarifrangenti, tombini, patches) con Solidify 40 cm su {Road, Shoulder_L, Shoulder_R} |
| 2 | `macerone_world_mesh` | Edifici (Buildings_Walls + _Roofs + Chimneys), Guardrail L/R, Signs (Curve+Speed+KmMarker), StoneWalls, Delineators, PowerWires/Poles/Crosses, Trees (chioma+tronco), Rocks, Bushes, Cypresses |
| 3 | `macerone_roadside_mesh` | Procedurale: 1800+ pietre + 2000+ cespugli + alberi veri tronco/chioma condizionati da satellite + parapetti ponte + bollards |
| 4 | `macerone_terrain_mesh` | Mesh Terrain Blender carvato (5213 face) con satellite texture come colorMap via UV lat/lon |
| 5 | `macerone_extra_buildings_mesh` | 70 edifici OSM mancanti dal Blender (walls + roofs fan-triangulated) |
| 6 | `macerone_road_details_mesh` | 296 toppe bitume dark + 74 light + 5 chevrons sui tornanti |
| 7 | `macerone_embankments_mesh` | 2925 quad scarpata tra bordo banchina e terrain sottostante |
| 8 | `macerone_vegetation_mesh` | 3000 crossed billboards albero da analisi satellite map-wide |

TerrainBlock BeamNG: `.ter` flat a −30 m (fallback richiesto da BeamNG, non visibile).

### Tool ausiliari invocati da `build_full_mod.py`

| File | Ruolo |
|------|-------|
| `obj_to_dae.py` | convertitore OBJ→DAE Z-up (Blender 5.x ha rimosso Collada exporter) |
| `build_heightmap.py` | heightmap PNG16 4096² dal DEM EU-DEM 25 m |
| `analyze_satellite.py` | classifica bordi (paved/grass/tree) via satellite ESRI zoom 17, output `road_conditions.json` |
| `generate_extra_buildings.py` | edifici OSM non presenti nel mesh Blender → procedural mesh |
| `generate_road_details.py` | toppe bitume irregolari + chevrons gialli sui tornanti |
| `generate_embankments.py` | scarpate procedurali dove strada è sopraelevata sul terrain |
| `generate_vegetation.py` | crossed billboards albero map-wide, PNG RGBA con alpha procedurale |
| `mapillary_sample.py` | PoC street-view (Mapillary zero copertura sulla SS17; KartaView richiede OAuth) — non usato |
| `build_minimal_mod.py` | baseline strada-sola flat (debug/fallback) |
| `build_heightmap.py, build_ter.py, build_roads.py, build_mod.py, build_mod_skeleton.py, build_textures.py, optimize_satellite.py, blender_export.py` | script legacy della vecchia pipeline "DecalRoad", conservati per riferimento |

### Tuning parameters (in testa a `build_full_mod.py`)

```python
SPAWN_FORWARD_M = 5.0           # spawn avanti lungo il muso
SPAWN_UP_M = 1.0                 # altezza extra spawn
SPAWN_TURN_RIGHT_DEG = -25.0    # rotazione spawn (negativo = sinistra)
ROAD_CORRIDOR_FILTER_M = 5.5    # filter face alberi/bushes entro
WORLD_COLLECTIONS = [...]       # collezioni blender esportate
SKIP_MESH_NAMES = []             # mesh per-nome da escludere
```

### Scelte tecniche critiche (hard-earned, vedi memoria dettagliata)

- **DAE Z-up nativo**: BeamNG/Torque ignora `<up_axis>Y_UP</up_axis>`. `obj_to_dae.py` scrive sempre `Z_UP`; export Blender con `forward_axis="Y", up_axis="Z"`. Con Y-up mesh finisce a z=4000 m.
- **Muso veicolo = −Y locale**: heading `atan2(dx, −dy)`. Con formula standard il veicolo spawna voltato 180°.
- **z_offset_blender inferito**: `terrain_info.json` dà `min(DEM bbox) ≈ 336 m`, ma `blender_build.py` usa `min(centerline_recompute_z) ≈ 424 m`. Diff ~88 m. `infer_z_offset_blender()` campiona DEM lungo cl e prende mediana.
- **Coord Blender native**: TSStatic tutti a `(0, 0, 0)`. Spawn a z ~73.75 (non 497). A z alti BeamNG physics ha stranezze.
- **Solidify SOLO su nomi esatti** `{"Road", "Shoulder_L", "Shoulder_R"}`: linee/catarifrangenti/tombini/patches restano piatti.
- **Markings +3 cm post-export**: Z-fighting col mesh Road rendeva le linee invisibili.
- **Terrain mesh Blender come riferimento**: `macerone_terrain_mesh` (oggetto `SatelliteTerrain` nel blend) è il vero ground. Il `.ter` è piatto a −30 m fallback. Evita mismatch tra heightmap DEM fine e terrain mesh coarse.
- **Drop-to-ground per-isola**: union-find sulle edges del world mesh. Skip oggetti estesi (bbox >30 m). Downshift se `−15 < delta < −0.1`, upshift compatti se `extent<15 m AND 0.1<delta<3`.
- **Carve terrain mesh post-export**: abbassa vertex del mesh Terrain che sbucano sopra la road (falloff d<6→road−0.3, d<30→road+0.3, d<80→road+4).
- **Remove buildings on road**: union-find sugli oggetti Building. Se qualsiasi vertex isola <4 m da cl → rimuovi intero edificio.
- **Satellite analysis**: 10% paved, 87% grass, 0.5% tree. Classificazione via HSV+varianza condiziona clutter/vegetation.
- **Asfalto colore**: 1526 pixel filtrati (sat<0.12, lum 0.20-0.70), mediana RGB (0.605, 0.623, 0.570). Texture procedurale minimalista (no pattern, solo grana fine).
- **Nomi material nel blend** (match esatto richiesto):
  - Road mesh: `Asphalt, AsphaltPatch_Dark, AsphaltPatch_Light, Shoulder, LineWhite, LineYellow, Manhole`
  - World: `Building, Guardrail, LineWhite, Pole, Roof, Sign, StoneWall, TreeCanopy, TreeTrunk` (singolare!)
  - Terrain: `SatelliteTerrain` (non "Terrain")
- **Parapetti ponte per-segmento**: itera ogni coppia consecutiva di centerline points (non linea dritta start↔end) altrimenti attraversa ponti in curva.
- **TreeBillboard alphaTest=True, alphaRef=100**: BeamNG maschera lo sfondo trasparente del PNG.
- **`.ter` v9** (non v7, fallisce BeamNG 0.38). TER_SIZE=1024 stabile.

### Street view (provato, NON funziona sulla SS17)

- **Mapillary**: token gratis, ma zero copertura sull'area.
- **KartaView**: API pubblica restringe a OAuth complesso.
- **Google Street View**: coperto ma costa ~2.3$/build.
- → Fallback su **analisi satellite ESRI zoom 17** (~1 m/pixel), sufficiente per classificare paved/tree/grass per zone laterali.

## Limitazioni note

1. **OSRM sceglieva la SS17var (bypass moderna)**: risolto con Dijkstra vincolato su `ref="SS17"`.
2. **OSM in zona rurale non ha tree/signal individuali**: generati proceduralmente.
3. **Street View non scaricabile gratuitamente**: usata solo immagine satellitare.
4. **DEM 25 m ha risoluzione limitata in trincea**: `recompute_road_z_from_dem` prende MIN di 17 campioni per stimare il fondo della trincea.
5. **Subdivide terrain cuts=2** fa terreno mesh pesante ma evita clipping strada.

## File generati

```
road_data.json              (1.4 MB)  — dati strada + DEM + OSM layers
output/
├── satellite.png          (38 MB)   — mosaico ESRI
├── satellite_bbox.json              — bbox geografico del mosaico
├── line_marks.json                  — per punto: linea bianca presente?
├── macerone.blend         (17 MB)   — scena Blender completa
├── macerone.obj           (64 MB)   — mesh export
├── macerone.mtl                     — materiali
└── centerline.csv                   — asse strada per decalRoad BeamNG
```

## Troubleshooting

**Routing segue la bypass SS17var:** controllare che `ROAD_REFS = ["SS17"]` (senza spazio) in `fetch_road.py`.

**Strada sparisce sotto la montagna:** aumentare `CARVE_BUFFER_M` e `CARVE_DEPTH_M` in `blender_build.py`, o ridurre `max_grade` in `smooth_z_with_slope_limit`.

**Muri verticali ai lati strada:** bug risolto — il carve ora solo ABBASSA il terreno, non lo alza mai.

**Texture satellite invisibile:** modalità viewport = Solid invece di Material Preview. `Z → Material Preview`.

**Blender troppo lento:** 92k ciuffi d'erba sono pesanti. Nell'Outliner, disattiva la collection "Grass" per il viewport (non per il render).

**Overpass timeout:** il codice ha una lista di mirror con retry. Se tutti falliscono, riprova più tardi (Overpass ha load variabile).
