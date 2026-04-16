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

Un singolo comando genera la mod installabile (ZIP pronta) e la copia in BeamNG:

```bash
python tools/beamng/build_full_mod.py
```

Output: `output/beamng/macerone3d.zip` (~16 MB) automaticamente copiata in `C:\Users\Matto\AppData\Local\BeamNG\BeamNG.drive\current\mods\`.

### Cosa fa la pipeline `build_full_mod.py`

1. Legge il heightmap DEM pre-calcolato (`build_heightmap.py` se manca).
2. Inferisce `z_offset_blender` vero campionando il DEM lungo la centerline (il valore in `terrain_info.json` è sbagliato — vedi sotto).
3. Esporta con Blender headless:
   - **Road mesh** (collection `Road`, Solidify 0.4 m SOLO su Asphalt/Shoulder) → `macerone_road.dae`
   - **World mesh** (Buildings + Walls + Guardrails + Trees + Rocks + Signals, escluso `Delineators`) → `macerone_world.dae`
4. Filtra dall'OBJ world le face di alberi/bushes/rocce entro 3.5 m dalla centerline (procedurali finiti sull'asfalto).
5. Scrive `theTerrain.ter` con heightmap SHIFTATO in coord Blender + carve bidirezionale sul corridoio road (falloff 100 m).
6. Campiona RGB asfalto dal satellite ESRI lungo la centerline e genera texture procedurale 512×512 (grana + crepe + striature) usata come `colorMap` del material Asphalt.
7. Scrive `main.level.json` con TerrainBlock + TSStatic road/world + SpawnSphere orientata verso il primo rettilineo.
8. Zippa e copia nei mods di BeamNG.

### Tuning spawn (variabili in testa a `build_full_mod.py`)

```python
SPAWN_FORWARD_M = 5.0        # metri avanti lungo il muso
SPAWN_UP_M = 1.0             # altezza extra sopra l'asfalto
SPAWN_TURN_RIGHT_DEG = -25.0 # gradi di rotazione a destra (negativi = sinistra)
ROAD_CORRIDOR_FILTER_M = 3.5 # raggio filtro oggetti vicino alla strada
```

### Installazione manuale (se serve)

1. Copia `output/beamng/macerone3d.zip` in `Documents/BeamNG.drive/<versione>/mods/` (o lascia che lo faccia lo script).
2. Avvia BeamNG → Singleplayer → Freeroam → "SS17 Valico del Macerone".

### Scelte tecniche critiche (hard-earned)

- **DAE Z-up nativo**: BeamNG/Torque ignora il tag `<up_axis>Y_UP</up_axis>`. `tools/beamng/obj_to_dae.py` scrive sempre `Z_UP`; l'export Blender usa `forward_axis="Y", up_axis="Z"`. Con Y-up il mesh finisce a z=4000 m.
- **Muso veicolo = -Y locale**: heading corretto `atan2(dx, -dy)`. Con la formula standard il veicolo spawna voltato di 180°.
- **z_offset_blender inferito dal DEM**: `terrain_info.json.z_offset_blender_m` = `min(DEM bbox)` ≈ 336 m, ma `blender_build.py` usa `min(centerline_recompute_z)` ≈ 424 m. Diff ~88 m. `infer_z_offset_blender()` campiona il DEM lungo la centerline e prende mediana.
- **Heightmap shiftato in coord Blender**: pixel uint16 = `(real_z - z_offset_blender) / 800 * 65535`. TSStatic road/world a `(0, 0, 0)`. Evita z=500 che crea problemi fisici in BeamNG.
- **Carve heightmap bidirezionale**: blend verso `road_z - 0.8 m` con falloff lineare raggio 8 celle (96 m). Alza il DEM dove troppo basso, abbassa dove troppo alto → paesaggio segue la strada invece di essere sospeso sopra valli o sprofondato in trincea.
- **Solidify solo su Asphalt/Shoulder**: non su linee/catarifrangenti/tombini/patches (altrimenti sporgono e sbalzano il veicolo).
- **Filter corridoio per nome mesh**: rimuove face entro 3.5 m solo per `Trees*/Roadside*/Bushes/Rocks/StoneWalls`. Guardrail, Delineators (se abilitati), Signs restano.
- **TerrainMaterial path**: `/levels/macerone/art/terrains/satellite_diffuse` (senza estensione, leading `/`). `diffuseColor` di fallback (verde-grigio) presente.
- **`.ter` formato BeamNG 0.38**: version=9 (version 7 non carica più). TER_SIZE=1024 stabile.

### File scripts `tools/beamng/`

| File | Uso |
|------|-----|
| `build_full_mod.py` | **orchestrator principale** — genera la mod completa |
| `build_minimal_mod.py` | baseline "solo strada + terrain flat", utile come fallback di debug |
| `build_heightmap.py` | genera heightmap PNG16 4096² dal DEM |
| `obj_to_dae.py` | convertitore OBJ→DAE Z-up (per evitare il Collada exporter di Blender 5.x rimosso) |
| `build_mod.py`, `build_mod_skeleton.py`, `build_ter.py`, `build_roads.py`, `build_textures.py`, `optimize_satellite.py`, `blender_export.py` | script legacy della vecchia pipeline "DecalRoad", non usati dalla full — mantenuti per riferimento |

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
