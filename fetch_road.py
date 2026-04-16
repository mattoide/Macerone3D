"""
Scarica tutto il necessario a ricostruire il Valico del Macerone (SS17) in 3D:
  - tracciato stradale (OSRM)
  - griglia DEM SRTM 30m (terreno reale, non interpolato dalla strada)
  - tag OSM dettagliati (larghezza, corsie, superficie, limite, tunnel, ponti)
  - geometrie OSM di contorno: edifici, boschi, corsi d'acqua, altre strade, barriere

Output: road_data.json (consumato da blender_build.py)

Dipendenze: pip install requests
"""
from __future__ import annotations

import heapq
import json
import math
import sys
import time
from pathlib import Path
from typing import Iterable

import requests

# ======== Parametri ====================================================
POINT_A = (41.6258108, 14.2305322)  # SS17 16, 86080 Isernia IS
POINT_B = (41.7085882, 14.144029)   # destinazione
# Forza il percorso sulla vecchia SS17 (Valico del Macerone), escludendo la SS17var
ROAD_REFS = ["SS17"]  # OSM qui tagga senza spazio; NON includere SS17var

CENTERLINE_STEP_M = 15.0      # densificazione tracciato
TERRAIN_MARGIN_M = 400.0      # margine attorno alla road bbox
TERRAIN_STEP_M = 60.0         # risoluzione griglia DEM (m/cella)
ELEV_BATCH = 100              # Opentopodata limit
ELEV_PAUSE = 1.05             # s tra batch (free tier: 1 req/s)
OVERPASS_RETRIES = 4

OUT_PATH = Path(__file__).parent / "road_data.json"

OSRM_URL = (
    "https://router.project-osrm.org/route/v1/driving/"
    "{a_lon},{a_lat};{b_lon},{b_lat}"
    "?overview=full&geometries=geojson&annotations=nodes&steps=false"
)
OPENTOPO_URL = "https://api.opentopodata.org/v1/eudem25m"  # DEM Europa 25 m (più preciso di SRTM)
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]

R_EARTH = 6_378_137.0


# ======== Utility ======================================================
def haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_EARTH * math.asin(math.sqrt(a))


def densify(points, step_m):
    out = [points[0]]
    for i in range(1, len(points)):
        lat1, lon1 = points[i - 1]
        lat2, lon2 = points[i]
        d = haversine_m(lat1, lon1, lat2, lon2)
        n = max(1, int(d // step_m))
        for k in range(1, n + 1):
            t = k / n
            out.append((lat1 + (lat2 - lat1) * t, lon1 + (lon2 - lon1) * t))
    return out


def bbox_of(points, margin_m):
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    lat0 = (min(lats) + max(lats)) / 2
    dlat = margin_m / 111_320.0
    dlon = margin_m / (111_320.0 * math.cos(math.radians(lat0)))
    return (min(lats) - dlat, min(lons) - dlon, max(lats) + dlat, max(lons) + dlon)


# ======== Router OSM vincolato a ref specifici =========================
def osm_graph_route(a, b, refs):
    """Scarica le way OSM con ref in `refs` in una bbox attorno a (a,b),
    costruisce un grafo e ritorna il percorso minimo da A a B percorrendo
    SOLO quelle way. Torna (points[(lat,lon)], distance_m, node_ids)."""
    s = min(a[0], b[0]) - 0.06
    n = max(a[0], b[0]) + 0.06
    w = min(a[1], b[1]) - 0.06
    e = max(a[1], b[1]) + 0.06
    ref_filter = "|".join(refs)
    query = f"""
    [out:json][timeout:180];
    way[highway][ref~"^({ref_filter})$"]({s},{w},{n},{e});
    (._; >;);
    out body;
    """
    print(f"Query Overpass ways con ref={refs}...")
    js = overpass(query)

    nodes = {}   # id -> (lat, lon)
    adj = {}     # id -> [(neighbor, cost_m)]
    n_ways = 0
    for el in js.get("elements", []):
        if el["type"] == "node":
            nodes[el["id"]] = (el["lat"], el["lon"])
    for el in js.get("elements", []):
        if el["type"] == "way":
            n_ways += 1
            nd = el.get("nodes", [])
            for i in range(len(nd) - 1):
                u, v = nd[i], nd[i + 1]
                if u not in nodes or v not in nodes:
                    continue
                d = haversine_m(*nodes[u], *nodes[v])
                adj.setdefault(u, []).append((v, d))
                adj.setdefault(v, []).append((u, d))
    print(f"  {n_ways} way, {len(nodes)} node")
    if not adj:
        raise RuntimeError(f"Nessuna way trovata con ref={refs} nella bbox")

    def nearest(pt):
        best = None; bd2 = float("inf")
        for nid, (lat, lon) in nodes.items():
            d2 = (lat - pt[0]) ** 2 + (lon - pt[1]) ** 2
            if d2 < bd2:
                bd2 = d2; best = nid
        return best, math.sqrt(bd2) * 111_320.0

    start, dA = nearest(a)
    end, dB = nearest(b)
    print(f"  nearest node A: {start} a {dA:.0f} m,  B: {end} a {dB:.0f} m")

    # Dijkstra
    dist = {start: 0.0}
    prev = {}
    pq = [(0.0, start)]
    while pq:
        d, u = heapq.heappop(pq)
        if u == end:
            break
        if d > dist.get(u, float("inf")):
            continue
        for v, w_ in adj.get(u, []):
            nd = d + w_
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    if end != start and end not in prev:
        raise RuntimeError(f"Nessun percorso connesso su ref={refs}")

    path_ids = []
    u = end
    while u != start:
        path_ids.append(u)
        u = prev[u]
    path_ids.append(start)
    path_ids.reverse()
    pts = [nodes[nid] for nid in path_ids]
    # prepend A e append B se distanti >30m dal primo/ultimo nodo
    if dA > 30: pts = [a] + pts
    if dB > 30: pts = pts + [b]
    return pts, dist.get(end, 0.0), path_ids


# ======== Opentopodata (elevazioni) ====================================
def elevations_points(points) -> list[float]:
    out: list[float] = []
    for i in range(0, len(points), ELEV_BATCH):
        chunk = points[i:i + ELEV_BATCH]
        locs = "|".join(f"{lat},{lon}" for lat, lon in chunk)
        resp = _elev_post(locs)
        for res in resp["results"]:
            out.append(float(res["elevation"]) if res["elevation"] is not None else 0.0)
        print(f"  elevazioni: {len(out)}/{len(points)}")
        if i + ELEV_BATCH < len(points):
            time.sleep(ELEV_PAUSE)
    return out


def _elev_post(locs: str):
    for attempt in range(4):
        try:
            r = requests.post(OPENTOPO_URL, data={"locations": locs}, timeout=120)
            if r.status_code == 429:
                time.sleep(5 + attempt * 3)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as ex:
            if attempt == 3:
                raise
            print(f"    retry ({ex})", file=sys.stderr)
            time.sleep(2 + attempt * 2)
    raise RuntimeError("unreachable")


def elevations_grid(bbox, step_m):
    s, w, n, e = bbox
    lat0 = (s + n) / 2
    dlat = step_m / 111_320.0
    dlon = step_m / (111_320.0 * math.cos(math.radians(lat0)))
    nrows = max(2, int((n - s) / dlat) + 1)
    ncols = max(2, int((e - w) / dlon) + 1)
    print(f"  griglia DEM: {nrows} x {ncols} = {nrows * ncols} punti "
          f"(~{nrows * ncols / ELEV_BATCH:.0f} richieste, ~{nrows * ncols / ELEV_BATCH * ELEV_PAUSE:.0f} s)")
    pts: list[tuple[float, float]] = []
    for j in range(nrows):
        for i in range(ncols):
            pts.append((s + j * dlat, w + i * dlon))
    elevs = elevations_points(pts)
    grid = [elevs[j * ncols:(j + 1) * ncols] for j in range(nrows)]
    return {
        "bbox": [s, w, n, e],
        "rows": nrows,
        "cols": ncols,
        "step_m": step_m,
        "grid": grid,
    }


# ======== Overpass =====================================================
def overpass(query: str):
    last_err = None
    for mirror in OVERPASS_MIRRORS:
        for attempt in range(OVERPASS_RETRIES):
            try:
                r = requests.post(mirror, data={"data": query}, timeout=180)
                if r.status_code in (429, 504, 502):
                    time.sleep(4 + attempt * 3)
                    continue
                r.raise_for_status()
                return r.json()
            except requests.RequestException as ex:
                last_err = ex
                time.sleep(2 + attempt * 2)
        print(f"  mirror {mirror} fallito, provo il prossimo", file=sys.stderr)
    raise RuntimeError(f"Overpass unreachable: {last_err}")


def fetch_osm_layers(bbox):
    s, w, n, e = bbox
    b = f"{s},{w},{n},{e}"
    query = f"""
    [out:json][timeout:180];
    (
      way[highway]({b});
      way[building]({b});
      way[landuse=forest]({b});
      way[natural=wood]({b});
      way[waterway]({b});
      way[natural=water]({b});
      way[barrier]({b});
      way[bridge=yes]({b});
      way[tunnel=yes]({b});
      node[natural=tree]({b});
      node[highway=traffic_signals]({b});
      node[highway=street_lamp]({b});
      node[highway=stop]({b});
      node[barrier]({b});
    );
    out body geom tags;
    """
    return overpass(query)


def _parse_float(v, default):
    try:
        return float(str(v).split()[0])
    except (TypeError, ValueError, IndexError):
        return default


def classify_osm(osm_json, route_nodes: set[int]):
    """Organizza le way/node OSM in categorie e identifica quelle percorse dal tracciato."""
    out = {
        "route_ways": [],
        "bridges_tunnels": [],
        "buildings": [],
        "forests": [],
        "waterways": [],
        "waterbodies": [],
        "barriers": [],
        "other_roads": [],
        "trees": [],          # (lat, lon) individuali
        "signals": [],        # semafori, stop, street_lamp (lat, lon, kind)
        "node_barriers": [],  # barriere puntuali (bollardi, cancelli)
    }
    for el in osm_json.get("elements", []):
        t = el.get("type")
        tags = el.get("tags", {}) or {}
        if t == "node":
            lat, lon = el.get("lat"), el.get("lon")
            if lat is None:
                continue
            if tags.get("natural") == "tree":
                out["trees"].append({"lat": lat, "lon": lon,
                                     "height": _parse_float(tags.get("height"), 8.0)})
            elif tags.get("highway") in ("traffic_signals", "stop", "street_lamp"):
                out["signals"].append({"lat": lat, "lon": lon, "kind": tags["highway"]})
            elif tags.get("barrier"):
                out["node_barriers"].append({"lat": lat, "lon": lon, "kind": tags["barrier"]})
            continue
        if t != "way":
            continue
        tags = el.get("tags", {}) or {}
        geom = el.get("geometry") or []
        coords = [(g["lat"], g["lon"]) for g in geom]
        if not coords:
            continue
        nodes = set(el.get("nodes") or [])

        # ways attraversate dalla route (condividono almeno 2 nodi consecutivi)
        if tags.get("highway") and nodes & route_nodes:
            out["route_ways"].append({
                "id": el["id"],
                "tags": {k: tags.get(k) for k in
                         ("highway", "name", "ref", "width", "lanes",
                          "surface", "maxspeed", "bridge", "tunnel", "oneway")},
                "nodes": list(nodes & route_nodes),
                "coords": coords,
            })

        if tags.get("bridge") == "yes" or tags.get("tunnel") == "yes":
            out["bridges_tunnels"].append({
                "id": el["id"],
                "kind": "bridge" if tags.get("bridge") == "yes" else "tunnel",
                "coords": coords,
            })

        if tags.get("building"):
            try:
                levels = float(tags.get("building:levels") or 2)
            except ValueError:
                levels = 2.0
            try:
                height = float(str(tags.get("height") or "").split()[0])
            except (ValueError, IndexError):
                height = levels * 3.0
            out["buildings"].append({"coords": coords, "height": height})

        if tags.get("landuse") == "forest" or tags.get("natural") == "wood":
            out["forests"].append({"coords": coords})

        if tags.get("waterway"):
            out["waterways"].append({"kind": tags["waterway"], "coords": coords})

        if tags.get("natural") == "water":
            out["waterbodies"].append({"coords": coords})

        if tags.get("barrier"):
            out["barriers"].append({"kind": tags["barrier"], "coords": coords})

        if tags.get("highway") and not (nodes & route_nodes):
            out["other_roads"].append({
                "kind": tags["highway"],
                "coords": coords,
            })

    return out


def summarize_route_tags(route_ways):
    from collections import Counter

    def most_common(key, default=None):
        vals = [w["tags"].get(key) for w in route_ways if w["tags"].get(key)]
        return Counter(vals).most_common(1)[0][0] if vals else default

    highway = most_common("highway", "secondary")
    name = most_common("name")
    ref = most_common("ref")
    lanes = most_common("lanes", "2")
    surface = most_common("surface", "asphalt")
    maxspeed = most_common("maxspeed")

    width_raw = most_common("width")
    try:
        width_m = float(width_raw) if width_raw else None
    except ValueError:
        width_m = None
    if width_m is None:
        try:
            width_m = int(str(lanes).split(";")[0]) * 3.5
        except (TypeError, ValueError):
            width_m = {"motorway": 10.5, "trunk": 9.0, "primary": 8.0,
                       "secondary": 7.0, "tertiary": 6.5,
                       "unclassified": 5.5, "residential": 5.0}.get(highway, 7.0)

    return {
        "highway": highway,
        "name": name, "ref": ref,
        "lanes": lanes,
        "surface": surface,
        "maxspeed": maxspeed,
        "width_m": round(width_m, 2),
    }


def flag_bridges_tunnels(centerline, bridges_tunnels, tol_m=15.0):
    """Per ogni punto della centerline marca bridge/tunnel se cade a <tol_m da un segmento flagged."""
    flags = [{"bridge": False, "tunnel": False} for _ in centerline]
    for bt in bridges_tunnels:
        coords = bt["coords"]
        for i in range(len(coords) - 1):
            a = coords[i]
            b = coords[i + 1]
            for k, p in enumerate(centerline):
                if _point_seg_dist_m(p, a, b) <= tol_m:
                    flags[k][bt["kind"]] = True
    return flags


def _point_seg_dist_m(p, a, b):
    """Distanza approx in metri da p al segmento a-b (tutti lat/lon)."""
    # proiezione locale centrata su p
    lat0 = p[0]
    mx = lambda lon: math.radians(lon) * R_EARTH * math.cos(math.radians(lat0))
    my = lambda lat: math.radians(lat) * R_EARTH
    px, py = mx(p[1]), my(p[0])
    ax, ay = mx(a[1]), my(a[0])
    bx, by = mx(b[1]), my(b[0])
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    qx, qy = ax + t * dx, ay + t * dy
    return math.hypot(px - qx, py - qy)


# ======== Main =========================================================
def main():
    print(f"Routing OSM (ref={ROAD_REFS}) tra {POINT_A} e {POINT_B}...")
    raw, dist_m, node_ids = osm_graph_route(POINT_A, POINT_B, ROAD_REFS)
    dur_s = dist_m / (50_000 / 3600)  # stima 50 km/h di media sul passo
    print(f"  {len(raw)} punti, {dist_m:.0f} m, ~{dur_s / 60:.1f} min (stima), "
          f"{len(node_ids)} node id")

    print(f"Densificazione tracciato a {CENTERLINE_STEP_M} m...")
    centerline = densify(raw, CENTERLINE_STEP_M)
    print(f"  {len(centerline)} punti")

    bbox_road = bbox_of(centerline, TERRAIN_MARGIN_M)
    print(f"BBox terreno: {bbox_road}")

    print("Recupero OSM (Overpass) — strade, edifici, boschi, ponti, tunnel, ecc...")
    osm_raw = {"elements": []}
    try:
        osm_raw = fetch_osm_layers(bbox_road)
        print(f"  {len(osm_raw.get('elements', []))} elementi OSM")
    except Exception as ex:
        print(f"  ERRORE Overpass: {ex} — proseguo senza layer OSM", file=sys.stderr)

    layers = classify_osm(osm_raw, set(node_ids))
    print(f"  route_ways={len(layers['route_ways'])}, "
          f"bridges/tunnels={len(layers['bridges_tunnels'])}, "
          f"buildings={len(layers['buildings'])}, "
          f"forests={len(layers['forests'])}, "
          f"waterways={len(layers['waterways'])}, "
          f"waterbodies={len(layers['waterbodies'])}, "
          f"barriers={len(layers['barriers'])}, "
          f"other_roads={len(layers['other_roads'])}, "
          f"trees={len(layers['trees'])}, "
          f"signals={len(layers['signals'])}, "
          f"node_barriers={len(layers['node_barriers'])}")

    road_meta = summarize_route_tags(layers["route_ways"])
    print(f"  strada: {road_meta['ref'] or road_meta['highway']} "
          f"'{road_meta['name']}' — {road_meta['width_m']} m, {road_meta['lanes']} corsie, "
          f"{road_meta['surface']}, limite {road_meta['maxspeed']}")

    flags = flag_bridges_tunnels(centerline, layers["bridges_tunnels"])

    print("Elevazioni centerline (SRTM 30m)...")
    cl_ele = elevations_points(centerline)

    print("Elevazioni griglia DEM terreno...")
    dem = elevations_grid(bbox_road, TERRAIN_STEP_M)

    data = {
        "source": {"point_a": POINT_A, "point_b": POINT_B},
        "distance_m": dist_m,
        "duration_s": dur_s,
        "road": road_meta,
        "centerline": [
            {"lat": lat, "lon": lon, "ele": ele,
             "bridge": f["bridge"], "tunnel": f["tunnel"]}
            for (lat, lon), ele, f in zip(centerline, cl_ele, flags)
        ],
        "terrain": dem,
        "buildings": layers["buildings"],
        "forests": layers["forests"],
        "waterways": layers["waterways"],
        "waterbodies": layers["waterbodies"],
        "barriers": layers["barriers"],
        "other_roads": layers["other_roads"],
        "trees": layers["trees"],
        "signals": layers["signals"],
        "node_barriers": layers["node_barriers"],
    }
    OUT_PATH.write_text(json.dumps(data))
    kb = OUT_PATH.stat().st_size / 1024
    print(f"Scritto {OUT_PATH} ({kb:.0f} KB)")


if __name__ == "__main__":
    main()
