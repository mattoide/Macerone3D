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

## Importare in BeamNG.drive

1. Aprire il BeamNG World Editor
2. Importare `output/macerone.obj` come static mesh
3. Posizionare all'origine del livello
4. `output/centerline.csv` può essere usato per creare un decalRoad con l'asse strada

Per una mappa BeamNG completa servirebbe ancora: heightmap terrain, spawn point, atmospheric, manifest `main.level.json`. Non incluso in questo pipeline (per ora).

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
