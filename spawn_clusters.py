from collections import defaultdict


def find_clusters(locations, radius):
    """
    Group spawn points into clusters via union-find connected components.
    Two points belong to the same cluster if their 2D (X, Y) distance <= radius.
    Z is ignored (vertical axis in Unreal Engine).
    Returns a list of cluster dicts sorted by density descending:
      {"x": float, "y": float, "z": float, "density": int}
    """
    n = len(locations)
    if n == 0:
        return []

    parent = list(range(n))
    radius_sq = radius * radius

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    for i in range(n):
        xi, yi = locations[i]["X"], locations[i]["Y"]
        for j in range(i + 1, n):
            dx = xi - locations[j]["X"]
            dy = yi - locations[j]["Y"]
            if dx * dx + dy * dy <= radius_sq:
                union(i, j)

    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)

    result = []
    for indices in groups.values():
        xs = [locations[i]["X"] for i in indices]
        ys = [locations[i]["Y"] for i in indices]
        zs = [locations[i]["Z"] for i in indices]
        result.append({
            "x": round(sum(xs) / len(xs), 2),
            "y": round(sum(ys) / len(ys), 2),
            "z": round(sum(zs) / len(zs), 2),
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
            "clusters": clusters[:2], #Just need 2 clusters for the usecase
        }

    return result
