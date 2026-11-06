"""Map data: land polygons and city labels.

Tries Natural Earth 1:50m (coastlines + populated places) from GitHub and
caches it. If that can't be downloaded it falls back to the 1:110m land and
GeoNames city list shipped in plane_watch/data/.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from . import http



def _data(name: str) -> str:
    """Bundled data file (works from a folder or from inside the planewatch.pyz zip)."""
    from importlib.resources import files

    return files(__package__).joinpath("data", name).read_text(encoding="utf-8")

NE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson"
LAND_50M = f"{NE}/ne_50m_land.geojson"
PLACES_50M = f"{NE}/ne_50m_populated_places_simple.geojson"


def cache_dir() -> Path:
    d = Path(os.environ.get("PLANE_WATCH_CACHE") or Path.home() / ".cache" / "plane_watch")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cached_json(url: str, name: str):
    path = cache_dir() / name
    if path.exists():
        try:
            return json.loads(path.read_text())
        except ValueError:
            path.unlink()
    data = http.get_json(url)
    path.write_text(json.dumps(data, separators=(",", ":")))
    return data


def _rings_from_geojson(gj: dict) -> tuple[list, list]:
    outers, holes = [], []
    for feat in gj.get("features", []):
        g = feat.get("geometry") or {}
        if g.get("type") == "Polygon":
            polys = [g["coordinates"]]
        elif g.get("type") == "MultiPolygon":
            polys = g["coordinates"]
        else:
            continue
        for poly in polys:
            outers.append(poly[0])
            holes.extend(poly[1:])  # inland seas, e.g. the Caspian
    return outers, holes


@lru_cache(maxsize=1)
def land() -> tuple[list, list, str]:
    """(land rings, hole rings, attribution). Each ring is [[lon, lat], ...]."""
    if not os.environ.get("PLANE_WATCH_LOWRES"):
        try:
            outers, holes = _rings_from_geojson(_cached_json(LAND_50M, "ne_50m_land.geojson"))
            return outers, holes, "Natural Earth"
        except (http.FetchError, OSError, ValueError):
            pass
    return json.loads(_data("land_110m.json"))["rings"], [], "Natural Earth"


@lru_cache(maxsize=1)
def cities() -> list[tuple[str, float, float, int]]:
    """[(name, lat, lon, rank)] with rank = population (bigger first)."""
    if not os.environ.get("PLANE_WATCH_LOWRES"):
        try:
            gj = _cached_json(PLACES_50M, "ne_50m_populated_places_simple.geojson")
            out = []
            for f in gj.get("features", []):
                p = f.get("properties") or {}
                lon, lat = f["geometry"]["coordinates"][:2]
                out.append((p.get("name", ""), lat, lon, int(p.get("pop_max") or 0)))
            if out:
                return sorted(out, key=lambda c: -c[3])
        except (http.FetchError, OSError, ValueError, KeyError):
            pass
    data = json.loads(_data("cities.json"))
    return [tuple(c) for c in data["cities"]]
