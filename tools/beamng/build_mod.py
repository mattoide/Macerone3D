"""
Orchestrator: genera l'intera mod BeamNG.drive a partire da
  road_data.json   (OSM + DEM + tag strada)
  output/macerone.blend   (scena Blender del progetto)
  output/centerline.csv   (centerline in coord. locali)

Esegue in sequenza:
  1. build_heightmap.py   -> heightmap.png + terrain_info.json
  2. build_roads.py       -> roads.json
  3. blender_export.py    -> .dae selettivi + forest.json   (via Blender)
  4. build_mod_skeleton.py-> struttura mod/ finale

Uso:
  python tools/beamng/build_mod.py [--skip-blender] [--blender PATH]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = Path(__file__).resolve().parent
DEFAULT_BLENDER = r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe"


def run(step: str, cmd: list[str]) -> None:
    print(f"\n=== [{step}] {' '.join(cmd)} ===")
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print(f"Step '{step}' fallito (exit {r.returncode})")
        sys.exit(r.returncode)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-blender", action="store_true",
                    help="Salta lo step Blender (utile per iterazioni rapide sul resto)")
    ap.add_argument("--blender", default=DEFAULT_BLENDER,
                    help="Path a blender.exe")
    args = ap.parse_args()

    py = sys.executable

    run("heightmap", [py, str(TOOLS / "build_heightmap.py")])
    run("roads", [py, str(TOOLS / "build_roads.py")])

    if args.skip_blender:
        print("\n(skip-blender) DAE e forest.json NON rigenerati: uso quelli "
              "esistenti se presenti")
    else:
        blend = ROOT / "output" / "macerone.blend"
        if not blend.exists():
            print(f"ATTENZIONE: {blend} non esiste. Esegui blender_build.py prima.")
            sys.exit(1)
        info = ROOT / "output" / "beamng" / "terrain_info.json"
        cl = ROOT / "output" / "centerline.csv"
        out = ROOT / "output" / "beamng"
        run("blender_export", [
            args.blender, "--background", str(blend),
            "--python", str(TOOLS / "blender_export.py"),
            "--", str(info), str(cl), str(out),
        ])

    run("mod_skeleton", [py, str(TOOLS / "build_mod_skeleton.py")])

    print("\n=== BUILD MOD OK ===")
    print(f"Mod pronta in: {ROOT / 'output' / 'beamng' / 'mod'}")
    print("Leggi output/beamng/mod/README_install.md per installarla in BeamNG.drive.")


if __name__ == "__main__":
    main()
