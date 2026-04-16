"""
Convertitore OBJ -> DAE (Collada 1.4.1) minimale per mesh statici.

Supporta:
  - vertici (v), normali (vn), UV (vt)
  - facce come triangoli o quad (converte quad in 2 triangoli)
  - multi-material (usemtl -> libreria materials base)
  - gruppi (g) -> multi-nodi Collada
  - asse Y_UP (compatibile con l'export OBJ di Blender con up=Y, forward=-Z)

Non supporta: skeleton, animazioni, texture (solo colori base), polygon > 4 vert.

Uso CLI:
  python tools/beamng/obj_to_dae.py input.obj [input2.obj ...]

Produce input.dae accanto al .obj. Se il .dae esiste viene sovrascritto.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape


# ---------------------------------------------------------------------------
# OBJ parser (solo statica)
# ---------------------------------------------------------------------------
def parse_obj(path: Path):
    verts: list[tuple[float, float, float]] = []
    normals: list[tuple[float, float, float]] = []
    uvs: list[tuple[float, float]] = []
    # mesh_groups[group_name] = list of (material_name, list_of_triangles)
    # ogni triangolo: list of 3 tuple (v_idx, vt_idx or None, vn_idx or None)
    groups: dict[str, dict[str, list]] = {}
    current_group = "default"
    current_material = "default"

    def ensure(g, m):
        groups.setdefault(g, {}).setdefault(m, [])

    ensure(current_group, current_material)

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("v "):
                parts = line.split()
                verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
            elif line.startswith("vn "):
                parts = line.split()
                normals.append((float(parts[1]), float(parts[2]), float(parts[3])))
            elif line.startswith("vt "):
                parts = line.split()
                uvs.append((float(parts[1]), float(parts[2]) if len(parts) > 2 else 0.0))
            elif line.startswith("g ") or line.startswith("o "):
                current_group = line.split(maxsplit=1)[1]
                ensure(current_group, current_material)
            elif line.startswith("usemtl "):
                current_material = line.split(maxsplit=1)[1]
                ensure(current_group, current_material)
            elif line.startswith("f "):
                # f v[/vt[/vn]] ... (almeno 3, puo' essere quad o ngon)
                tokens = line.split()[1:]
                polygon = []
                for tk in tokens:
                    parts = tk.split("/")
                    v_idx = int(parts[0]) - 1
                    vt_idx = int(parts[1]) - 1 if len(parts) > 1 and parts[1] else None
                    vn_idx = int(parts[2]) - 1 if len(parts) > 2 and parts[2] else None
                    polygon.append((v_idx, vt_idx, vn_idx))
                # triangolarizza con fan da vertice 0
                for i in range(1, len(polygon) - 1):
                    tri = [polygon[0], polygon[i], polygon[i + 1]]
                    groups[current_group][current_material].append(tri)

    return verts, normals, uvs, groups


# ---------------------------------------------------------------------------
# Parser MTL (solo colori diffusi + nomi)
# ---------------------------------------------------------------------------
def parse_mtl(path: Path) -> dict:
    mats: dict[str, dict] = {}
    if not path.exists():
        return mats
    current = None
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("newmtl "):
                current = line.split(maxsplit=1)[1]
                mats[current] = {"Kd": (0.8, 0.8, 0.8)}
            elif current and line.startswith("Kd "):
                parts = line.split()
                mats[current]["Kd"] = (
                    float(parts[1]), float(parts[2]), float(parts[3]))
            elif current and line.startswith("Ks "):
                parts = line.split()
                mats[current]["Ks"] = (
                    float(parts[1]), float(parts[2]), float(parts[3]))
            elif current and line.startswith("Ns "):
                mats[current]["Ns"] = float(line.split()[1])
    return mats


# ---------------------------------------------------------------------------
# Collada emitter
# ---------------------------------------------------------------------------
DATE_ISO = "2026-04-16T10:00:00"
SANITIZE = re.compile(r"[^A-Za-z0-9_]")


def sid(name: str) -> str:
    s = SANITIZE.sub("_", name)
    if not s or s[0].isdigit():
        s = "_" + s
    return s


def f3(tpl) -> str:
    return " ".join(f"{v:.6f}" for v in tpl)


def write_dae(obj_path: Path, dae_path: Path | None = None) -> Path:
    verts, normals, uvs, groups = parse_obj(obj_path)
    mtl_path = obj_path.with_suffix(".mtl")
    mats = parse_mtl(mtl_path)

    # assicurati che tutti i materiali usati esistano nel dict
    all_mats = set()
    for g in groups.values():
        all_mats.update(g.keys())
    for m in all_mats:
        mats.setdefault(m, {"Kd": (0.8, 0.8, 0.8)})

    dae_path = dae_path or obj_path.with_suffix(".dae")

    # --- float arrays globali
    verts_flat = " ".join(f"{v[0]:.6f} {v[1]:.6f} {v[2]:.6f}" for v in verts)
    norms_flat = " ".join(f"{n[0]:.6f} {n[1]:.6f} {n[2]:.6f}" for n in normals)
    uvs_flat = " ".join(f"{u[0]:.6f} {u[1]:.6f}" for u in uvs)

    geom_id = sid(obj_path.stem) + "_geom"

    # costruisci i blocchi <triangles> per ciascun (gruppo, materiale)
    triangle_blocks = []
    # per Collada: le triangles vanno dentro UN solo <mesh>, ognuno con <p>
    # ordinato [v, vn, vt] per ciascun vertice del triangolo.
    for g_name, g_mats in groups.items():
        for m_name, tris in g_mats.items():
            if not tris:
                continue
            inputs = [
                '<input semantic="VERTEX" source="#{g}_vertices" offset="0"/>'.format(g=geom_id),
            ]
            stride = 1
            if normals and any(t[0][2] is not None for t in tris):
                inputs.append(f'<input semantic="NORMAL" source="#{geom_id}_normals" offset="{stride}"/>')
                stride += 1
            if uvs and any(t[0][1] is not None for t in tris):
                inputs.append(f'<input semantic="TEXCOORD" source="#{geom_id}_uvs" offset="{stride}" set="0"/>')
                stride += 1

            indices = []
            for tri in tris:
                for (v, vt, vn) in tri:
                    indices.append(str(v))
                    if stride >= 2:
                        indices.append(str(vn if vn is not None else 0))
                    if stride >= 3:
                        indices.append(str(vt if vt is not None else 0))
            triangle_blocks.append(
                f'<triangles material="{escape(sid(m_name))}" count="{len(tris)}">\n'
                + "\n".join(inputs) + "\n"
                + f'<p>{" ".join(indices)}</p>\n'
                + "</triangles>"
            )

    # assembla mesh
    sources = [
        f'''<source id="{geom_id}_positions">
  <float_array id="{geom_id}_positions_arr" count="{len(verts)*3}">{verts_flat}</float_array>
  <technique_common>
    <accessor source="#{geom_id}_positions_arr" count="{len(verts)}" stride="3">
      <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
    </accessor>
  </technique_common>
</source>''',
    ]
    if normals:
        sources.append(f'''<source id="{geom_id}_normals">
  <float_array id="{geom_id}_normals_arr" count="{len(normals)*3}">{norms_flat}</float_array>
  <technique_common>
    <accessor source="#{geom_id}_normals_arr" count="{len(normals)}" stride="3">
      <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
    </accessor>
  </technique_common>
</source>''')
    if uvs:
        sources.append(f'''<source id="{geom_id}_uvs">
  <float_array id="{geom_id}_uvs_arr" count="{len(uvs)*2}">{uvs_flat}</float_array>
  <technique_common>
    <accessor source="#{geom_id}_uvs_arr" count="{len(uvs)}" stride="2">
      <param name="S" type="float"/><param name="T" type="float"/>
    </accessor>
  </technique_common>
</source>''')

    mesh_xml = f'''<mesh>
  {"".join(sources)}
  <vertices id="{geom_id}_vertices">
    <input semantic="POSITION" source="#{geom_id}_positions"/>
  </vertices>
  {"".join(triangle_blocks)}
</mesh>'''

    # library_effects + library_materials
    effects_xml = []
    materials_xml = []
    instance_materials = []
    for m_name, attrs in mats.items():
        msid = sid(m_name)
        kd = attrs.get("Kd", (0.8, 0.8, 0.8))
        effects_xml.append(f'''<effect id="{msid}_fx">
  <profile_COMMON>
    <technique sid="common">
      <phong>
        <diffuse><color sid="diffuse">{kd[0]:.4f} {kd[1]:.4f} {kd[2]:.4f} 1</color></diffuse>
        <specular><color sid="specular">0.1 0.1 0.1 1</color></specular>
        <shininess><float sid="shininess">20</float></shininess>
      </phong>
    </technique>
  </profile_COMMON>
</effect>''')
        materials_xml.append(
            f'<material id="{msid}" name="{escape(m_name)}">\n'
            f'  <instance_effect url="#{msid}_fx"/>\n'
            f'</material>'
        )
        instance_materials.append(
            f'<instance_material symbol="{msid}" target="#{msid}"/>'
        )

    # assembla COLLADA
    doc = f'''<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset>
    <contributor><authoring_tool>macerone3d obj_to_dae.py</authoring_tool></contributor>
    <created>{DATE_ISO}</created>
    <modified>{DATE_ISO}</modified>
    <unit name="meter" meter="1"/>
    <up_axis>Z_UP</up_axis>
  </asset>
  <library_effects>
    {"".join(effects_xml)}
  </library_effects>
  <library_materials>
    {"".join(materials_xml)}
  </library_materials>
  <library_geometries>
    <geometry id="{geom_id}" name="{escape(obj_path.stem)}">
      {mesh_xml}
    </geometry>
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="scene1" name="scene1">
      <node id="node1" name="{escape(obj_path.stem)}">
        <instance_geometry url="#{geom_id}">
          <bind_material>
            <technique_common>
              {"".join(instance_materials)}
            </technique_common>
          </bind_material>
        </instance_geometry>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene>
    <instance_visual_scene url="#scene1"/>
  </scene>
</COLLADA>
'''
    dae_path.write_text(doc, encoding="utf-8")
    return dae_path


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    for p in sys.argv[1:]:
        obj = Path(p)
        if not obj.exists():
            print(f"Salto {obj}: non esiste")
            continue
        dae = write_dae(obj)
        n_verts = 0
        n_tris = 0
        with obj.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("v "):
                    n_verts += 1
                elif line.startswith("f "):
                    n_tris += 1
        print(f"{obj.name} ({n_verts} v, {n_tris} f) -> {dae.name}")


if __name__ == "__main__":
    main()
