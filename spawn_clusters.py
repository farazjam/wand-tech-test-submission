import math
from collections import defaultdict, deque

# ---------------------------------------------------------------------------
# DBSCAN parameters for Palworld spawn data.
#
# eps   – max distance (cm) between two spawn points to be considered
#         neighbours.  Within an island the spawn grid is typically 45–65 m;
#         water gaps between islands are 500 m+.  100 m (10 000 cm) sits
#         comfortably in the middle and separates islands cleanly.
#         Tune upward if a single island gets split; tune downward if two
#         islands get merged.
#
# min_samples – a point needs at least this many neighbours (itself included)
#         to be a core point.  4 (= 3 real neighbours) prevents isolated
#         coastal points from acting as bridges between islands.
# ---------------------------------------------------------------------------
DEFAULT_EPS = 10_000   # cm
DEFAULT_MIN_SAMPLES = 4


def _dbscan(coords, eps_sq, min_samples):
    """
    Pure-Python DBSCAN on 2-D (X, Y) coordinates.

    Only *core points* (>= min_samples neighbours within eps, self included)
    can expand a cluster.  This stops sparse bridge points on island coasts
    from chaining two separate islands into one cluster.

    Returns a list of integer labels (one per input point):
        -1  => noise / isolated point
         0+ => cluster id
    """
    n = len(coords)

    # Build neighbour lists in a single O(n²) pass
    neighbors = [[i] for i in range(n)]     # each point neighbours itself
    for i in range(n):
        xi, yi = coords[i]
        for j in range(i + 1, n):
            dx = xi - coords[j][0]
            dy = yi - coords[j][1]
            if dx * dx + dy * dy <= eps_sq:
                neighbors[i].append(j)
                neighbors[j].append(i)

    labels = [-2] * n                       # -2 = unvisited
    cid = 0

    for i in range(n):
        if labels[i] != -2:
            continue
        if len(neighbors[i]) < min_samples:
            labels[i] = -1                  # noise
            continue

        # Grow a new cluster from this core point
        labels[i] = cid
        seeds = deque(neighbors[i])
        while seeds:
            j = seeds.popleft()
            if labels[j] == -1:
                labels[j] = cid             # noise -> border point
            if labels[j] != -2:
                continue
            labels[j] = cid
            if len(neighbors[j]) >= min_samples:
                seeds.extend(neighbors[j])
        cid += 1

    return labels


def find_clusters(locations, radius,
                  eps=DEFAULT_EPS, min_samples=DEFAULT_MIN_SAMPLES):
    """
    Cluster spawn points with DBSCAN and return one summary dict per cluster.

    Coordinate system (Unreal Engine / Palworld):
        X, Y  - horizontal plane (used for clustering)
        Z     - vertical / up axis (ignored for distance, averaged for output)
        units - centimetres

    Each returned dict contains:
        "x", "y", "z" - centroid (mean position) of all points in the cluster
        "density"      - number of spawn points in the cluster

    Noise points (isolated spawns that belong to no dense group) are dropped.
    If every point is labelled noise (very sparse pal), all points are treated
    as one cluster so no data is silently lost.

    Results are sorted by density descending.
    """
    n = len(locations)
    if n == 0:
        return []

    if n == 1:
        loc = locations[0]
        return [{"x": loc["X"], "y": loc["Y"], "z": loc["Z"], "density": 1}]

    # X/Y only - Z is the vertical axis in Unreal Engine
    coords = [(loc["X"], loc["Y"]) for loc in locations]
    labels = _dbscan(coords, eps * eps, min_samples)

    # Fallback: if everything is noise, treat all points as one cluster
    if all(lbl == -1 for lbl in labels):
        labels = [0] * n

    groups = defaultdict(list)
    for i, lbl in enumerate(labels):
        if lbl >= 0:
            groups[lbl].append(i)

    result = []
    for indices in groups.values():
        # Centroid: mean X/Y/Z of all points in the cluster
        cx = sum(locations[i]["X"] for i in indices) / len(indices)
        cy = sum(locations[i]["Y"] for i in indices) / len(indices)
        cz = sum(locations[i]["Z"] for i in indices) / len(indices)

        result.append({
            "x": round(cx, 2),
            "y": round(cy, 2),
            "z": round(cz, 2),
            "density": len(indices),
        })

    result.sort(key=lambda c: c["density"], reverse=True)
    return result


def build_spawn_locations(pal_row, radius):
    """
    Given one entry from DT_PaldexDistributionData Rows (e.g. rows["SheepBall"]),
    return the spawnLocations dict ready to embed in PalsOutput.
    Returns None if the pal has no entry in the distribution data.
    """
    if pal_row is None:
        return None

    result = {}
    for time_key, out_key in (("dayTimeLocations", "dayTime"), ("nightTimeLocations", "nightTime")):
        block = pal_row.get(time_key) or {}
        locs = block.get("locations") or []
        clusters = find_clusters(locs, radius)
        result[out_key] = {
            "totalSpawns": len(locs),
            "clusters": clusters [:2],   # keep only the two densest clusters per time of day
        }

    return result

