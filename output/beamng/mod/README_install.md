# SS17 Valico del Macerone — installazione mod BeamNG.drive

## Installazione veloce (mod unpacked)

1. Copia l'intera cartella `mod/` dentro:
   `Documents/BeamNG.drive/<versione>/mods/unpacked/macerone3d/`
   (dove `<versione>` è es. `0.37`, controllare il proprio installato)

2. Riavvia BeamNG.drive.

3. Dal menu: Singleplayer → Freeroam → cerca "SS17 Valico del Macerone".

## Installazione finale (mod zippata)

1. Zippa il contenuto di `mod/` (NON la cartella `mod/` stessa):
   il file `info.json` e `levels/` devono essere alla radice dello zip.
2. Rinomina lo zip in `macerone3d.zip`.
3. Copia in `Documents/BeamNG.drive/<versione>/mods/macerone3d.zip`.

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
- Progetto generato da: https://github.com/mattoide/Macerone3D
- Licenza texture satellitari: ESRI World Imagery — verifica termini d'uso
  per distribuzione.
