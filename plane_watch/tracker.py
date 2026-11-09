"""Record military aircraft over days and work out where they go.

Every check, the app hands this module every military aircraft the feeds
returned (plus watched types like the V-22 Osprey and H-60). Positions go into
a small SQLite database (data/tracks.sqlite, kept for 8 days by default). From
that it builds:

- flights: a position trail split wherever there's a long gap or a landing,
  with the nearest airfield at each end,
- tables: busiest airfields, most common types and operators, per aircraft,
- the points for the military heatmap,
- the last known position of each watched-type aircraft.
"""

from __future__ import annotations

import csv
import math
import sqlite3
import time
from functools import lru_cache
from pathlib import Path

from . import geo
from .models import Aircraft

MIN_GAP_S = 120  # store at most one position per aircraft every 2 minutes (keeps a week of the military feed small)
FLIGHT_GAP_S = 30 * 60  # a gap this long starts a new flight
AIRFIELD_RADIUS_KM = 25  # a flight "starts/ends at" an airfield this close to its first/last point
LOW_ALT_FT = 6000  # ...and only if it was low there (otherwise it appeared/disappeared en route)

# ICAO type designators to keep an eye on (last position over the last 24 h)
WATCH_TYPES = {
    "V22": "V-22 Osprey",
    "H60": "H-60 Black Hawk / Seahawk (incl. MH-60)",
}


@lru_cache(maxsize=1)
def _airfield_grid() -> dict:
    """Airports bucketed by whole degree, for fast nearest-airfield lookups."""
    try:
        import airportsdata
    except ImportError:
        return {}
    grid: dict[tuple[int, int], list] = {}
    for a in airportsdata.load("ICAO").values():
        if a.get("lat") is None:
            continue
        grid.setdefault((math.floor(a["lat"]), math.floor(a["lon"])), []).append(a)
    return grid


_MAJOR_WORDS = ("air base", "afb", "nas ", "naval", "mcas", "army", "air force", "airfield", "field", "international", "base")


def _is_major(a: dict) -> bool:
    name = (a.get("name") or "").lower() + " "
    return bool(a.get("iata")) or any(w in name for w in _MAJOR_WORDS)


def nearest_airfield(lat: float, lon: float, radius_km: float = AIRFIELD_RADIUS_KM) -> tuple[dict | None, float]:
    """Nearest airport within radius_km. A real base or airport wins over a closer private strip
    if it's at most 15 km further away."""
    grid = _airfield_grid()
    best, best_d = None, radius_km
    major, major_d = None, radius_km
    la, lo = math.floor(lat), math.floor(lon)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            for a in grid.get((la + dy, (lo + dx + 180) % 360 - 180), []):
                d = geo.haversine_km(lat, lon, a["lat"], a["lon"])
                if d < best_d:
                    best, best_d = a, d
                if d < major_d and _is_major(a):
                    major, major_d = a, d
    if major is not None and major_d <= best_d + 15:
        return major, major_d
    return best, best_d


class Store:
    def __init__(self, path: Path, keep_days: float = 8):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=60)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS pos (hex TEXT, ts REAL, lat REAL, lon REAL, alt INTEGER, gs REAL, trk REAL);
            CREATE INDEX IF NOT EXISTS pos_hex_ts ON pos (hex, ts);
            CREATE INDEX IF NOT EXISTS pos_ts ON pos (ts);
            CREATE TABLE IF NOT EXISTS ac (
                hex TEXT PRIMARY KEY, callsign TEXT, reg TEXT, type TEXT, descr TEXT, operator TEXT, military INTEGER,
                first_seen REAL, last_seen REAL, last_lat REAL, last_lon REAL, last_alt INTEGER, last_trk REAL, last_gs REAL);
        """)
        self.keep_days = keep_days
        self._last: dict[str, tuple[float, float, float]] = {}  # hex -> (ts, lat, lon) of the last stored point
        for h, ts, lat, lon in self.db.execute("SELECT hex, last_seen, last_lat, last_lon FROM ac"):
            if lat is not None:
                self._last[h] = (ts, lat, lon)
        self._last_prune = 0.0

    def record(self, aircraft: list[Aircraft], now: float | None = None) -> int:
        """Store positions (thinned). Returns how many were written."""
        now = now or time.time()
        rows, acs = [], []
        for ac in aircraft:
            if ac.lat is None or ac.hex.startswith("~"):
                continue
            ts = ac.seen_ts or now
            last = self._last.get(ac.hex)
            if last and ts - last[0] < MIN_GAP_S:
                continue
            self._last[ac.hex] = (ts, ac.lat, ac.lon)
            rows.append((ac.hex, ts, ac.lat, ac.lon, 0 if ac.on_ground else ac.alt_ft, ac.speed_kt, ac.heading))
            acs.append((ac.hex, ac.callsign, ac.registration, ac.type_code, ac.description, ac.operator, int(ac.military),
                        ts, ts, ac.lat, ac.lon, 0 if ac.on_ground else ac.alt_ft, ac.heading, ac.speed_kt))
        with self.db:
            self.db.executemany("INSERT INTO pos VALUES (?,?,?,?,?,?,?)", rows)
            self.db.executemany("""
                INSERT INTO ac VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(hex) DO UPDATE SET
                    callsign = COALESCE(NULLIF(excluded.callsign, ''), ac.callsign),
                    reg = COALESCE(NULLIF(excluded.reg, ''), ac.reg),
                    type = COALESCE(NULLIF(excluded.type, ''), ac.type),
                    descr = COALESCE(NULLIF(excluded.descr, ''), ac.descr),
                    operator = COALESCE(NULLIF(excluded.operator, ''), ac.operator),
                    military = MAX(ac.military, excluded.military),
                    last_seen = excluded.last_seen, last_lat = excluded.last_lat, last_lon = excluded.last_lon,
                    last_alt = excluded.last_alt, last_trk = excluded.last_trk, last_gs = excluded.last_gs
            """, acs)
        if now - self._last_prune > 3600:
            self.prune(now)
        return len(rows)

    def add_history(self, ac: Aircraft, points: list, since: float) -> int:
        """Fill in an aircraft's past positions (from a trace download), thinned like live ones.
        Points already recorded live in that time span are kept; the trace only fills the gaps."""
        if not points:
            return 0
        have = [r[0] for r in self.db.execute("SELECT ts FROM pos WHERE hex = ? AND ts >= ?", (ac.hex, since))]
        have_set = sorted(have)
        rows, last_ts = [], None
        import bisect

        for p in points:
            if last_ts is not None and p.ts - last_ts < MIN_GAP_S:
                continue
            i = bisect.bisect_left(have_set, p.ts - MIN_GAP_S / 2)
            if i < len(have_set) and abs(have_set[i] - p.ts) < MIN_GAP_S / 2:
                continue  # already have a live point here
            rows.append((ac.hex, p.ts, p.lat, p.lon, p.alt_ft, None, None))
            last_ts = p.ts
        first, last = points[0], points[-1]
        with self.db:
            self.db.executemany("INSERT INTO pos VALUES (?,?,?,?,?,?,?)", rows)
            self.db.execute("""
                INSERT INTO ac VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(hex) DO UPDATE SET
                    callsign = COALESCE(NULLIF(excluded.callsign, ''), ac.callsign),
                    reg = COALESCE(NULLIF(excluded.reg, ''), ac.reg),
                    type = COALESCE(NULLIF(excluded.type, ''), ac.type),
                    descr = COALESCE(NULLIF(excluded.descr, ''), ac.descr),
                    operator = COALESCE(NULLIF(excluded.operator, ''), ac.operator),
                    military = MAX(ac.military, excluded.military),
                    first_seen = MIN(ac.first_seen, excluded.first_seen)
            """, (ac.hex, ac.callsign, ac.registration, ac.type_code, ac.description, ac.operator, int(ac.military),
                  first.ts, last.ts, last.lat, last.lon, last.alt_ft, ac.heading, ac.speed_kt))
        return len(rows)

    def prune(self, now: float | None = None):
        cutoff = (now or time.time()) - self.keep_days * 86400
        with self.db:
            self.db.execute("DELETE FROM pos WHERE ts < ?", (cutoff,))
            self.db.execute("DELETE FROM ac WHERE last_seen < ?", (cutoff,))
        self._last_prune = now or time.time()

    # ------------------------------------------------------------ queries

    def points(self, since: float, military_only: bool = True, cell_deg: float = 0.1) -> list[tuple[float, float, int]]:
        """(lon, lat, count) per cell_deg grid cell: the heatmap input, binned inside SQLite."""
        q = f"SELECT ROUND(p.lon / {cell_deg}) * {cell_deg}, ROUND(p.lat / {cell_deg}) * {cell_deg}, COUNT(*) FROM pos p"
        if military_only:
            q += " JOIN ac ON ac.hex = p.hex AND ac.military = 1"
        return self.db.execute(q + " WHERE p.ts >= ? GROUP BY 1, 2", (since,)).fetchall()

    def segment_chunks(self, since: float, chunk: int = 200_000):
        """Yield numpy arrays of [lon, lat, prev_lon, prev_lat, seconds_since_prev] for military positions:
        consecutive points of the same aircraft, so the heatmap can draw paths instead of dots."""
        import numpy as np

        q = """SELECT p.lon, p.lat, LAG(p.lon) OVER w, LAG(p.lat) OVER w, p.ts - LAG(p.ts) OVER w
               FROM pos p JOIN ac ON ac.hex = p.hex AND ac.military = 1
               WHERE p.ts >= ?
               WINDOW w AS (PARTITION BY p.hex ORDER BY p.ts)"""
        try:
            cur = self.db.execute(q, (since,))
        except sqlite3.OperationalError:  # very old SQLite without window functions: the heatmap uses dots instead
            return
        while True:
            rows = cur.fetchmany(chunk)
            if not rows:
                return
            yield np.array(rows, dtype=float)

    def iter_tracks(self, since: float, hex_code: str | None = None):
        """Yield (hex, [(ts, lat, lon, alt), ...]) one aircraft at a time (streams; doesn't load a week into memory)."""
        from itertools import groupby

        if hex_code:
            rows = self.db.execute("SELECT hex, ts, lat, lon, alt FROM pos WHERE ts >= ? AND hex = ? ORDER BY ts", (since, hex_code))
        else:
            rows = self.db.execute("SELECT hex, ts, lat, lon, alt FROM pos WHERE ts >= ? ORDER BY hex, ts", (since,))
        for h, grp in groupby(rows, key=lambda r: r[0]):
            yield h, [r[1:] for r in grp]

    def aircraft_info(self) -> dict[str, dict]:
        cols = ["hex", "callsign", "reg", "type", "descr", "operator", "military", "first_seen", "last_seen",
                "last_lat", "last_lon", "last_alt", "last_trk", "last_gs"]
        return {r[0]: dict(zip(cols, r)) for r in self.db.execute(f"SELECT {', '.join(cols)} FROM ac")}

    def tracks(self, since: float, hexes: list[str] | None = None) -> dict[str, list[tuple]]:
        """hex -> [(ts, lat, lon, alt), ...] in time order."""
        if hexes is not None:
            if not hexes:
                return {}
            marks = ",".join("?" * len(hexes))
            rows = self.db.execute(f"SELECT hex, ts, lat, lon, alt FROM pos WHERE ts >= ? AND hex IN ({marks}) ORDER BY hex, ts",
                                   (since, *hexes))
        else:
            rows = self.db.execute("SELECT hex, ts, lat, lon, alt FROM pos WHERE ts >= ? ORDER BY hex, ts", (since,))
        out: dict[str, list] = {}
        for h, ts, lat, lon, alt in rows:
            out.setdefault(h, []).append((ts, lat, lon, alt))
        return out

    def count_positions(self, since: float) -> int:
        return self.db.execute("SELECT COUNT(*) FROM pos WHERE ts >= ?", (since,)).fetchone()[0]

    def watched(self, since: float, types=tuple(WATCH_TYPES)) -> list[dict]:
        marks = ",".join("?" * len(types))
        info = [dict(zip(["hex", "callsign", "reg", "type", "descr", "operator", "last_seen", "lat", "lon", "alt", "heading", "gs"], r))
                for r in self.db.execute(f"""SELECT hex, callsign, reg, type, descr, operator, last_seen, last_lat, last_lon, last_alt,
                                                    last_trk, last_gs
                                             FROM ac WHERE type IN ({marks}) AND last_seen >= ? ORDER BY last_seen DESC""",
                                         (*types, since))]
        tracks = self.tracks(since, [i["hex"] for i in info])
        for i in info:
            i["type_label"] = WATCH_TYPES.get(i["type"], i["type"])
            i["track"] = [[round(lon, 3), round(lat, 3), alt] for _, lat, lon, alt in tracks.get(i["hex"], [])][-300:]
            a, d = nearest_airfield(i["lat"], i["lon"], 60) if i["lat"] is not None else (None, 0)
            i["near"] = {"icao": a["icao"], "name": a["name"], "km": round(d)} if a else None
        return info


# ------------------------------------------------------------ analysis


def _airborne(p) -> bool:
    return p[3] is None or p[3] > 0


def split_flights(points: list[tuple]) -> list[list[tuple]]:
    """Split one aircraft's [(ts, lat, lon, alt)] into flights: at long gaps and at landings."""
    flights, cur = [], []
    for p in points:
        if cur and p[0] - cur[-1][0] > FLIGHT_GAP_S:
            flights.append(cur)
            cur = []
        if not _airborne(p):
            if any(_airborne(q) for q in cur):  # just landed: this point ends the flight...
                cur.append(p)
                flights.append(cur)
            cur = [p]  # ...and is where the next one starts (parked/taxiing keeps only the latest ground point)
            continue
        cur.append(p)
    if cur:
        flights.append(cur)
    return [f for f in flights if len(f) >= 2 and any(_airborne(q) for q in f)]


def _end(lat, lon, alt) -> dict | None:
    if alt is not None and alt > LOW_ALT_FT:
        return None
    a, d = nearest_airfield(lat, lon)
    return {"icao": a["icao"], "name": a["name"], "country": a.get("country", ""), "km": round(d, 1)} if a else None


def flights(store: Store, since: float, info: dict | None = None, hex_code: str | None = None) -> list[dict]:
    info = info or store.aircraft_info()
    out = []
    for h, pts in store.iter_tracks(since, hex_code):
        meta = info.get(h, {})
        if not meta.get("military") and not hex_code:
            continue
        for f in split_flights(pts):
            dist = sum(geo.haversine_km(a[1], a[2], b[1], b[2]) for a, b in zip(f, f[1:]))
            start, end = f[0], f[-1]
            out.append({
                "hex": h, "callsign": meta.get("callsign") or "", "reg": meta.get("reg") or "", "type": meta.get("type") or "",
                "descr": meta.get("descr") or "", "operator": meta.get("operator") or "",
                "start_ts": start[0], "end_ts": end[0], "minutes": round((end[0] - start[0]) / 60),
                "start_lat": round(start[1], 3), "start_lon": round(start[2], 3), "end_lat": round(end[1], 3), "end_lon": round(end[2], 3),
                "from": _end(start[1], start[2], start[3]), "to": _end(end[1], end[2], end[3]),
                "max_alt": max((p[3] or 0) for p in f), "km": round(dist), "points": len(f),
            })
    out.sort(key=lambda r: -r["end_ts"])
    return out


def summarize(store: Store, since: float) -> dict:
    """Everything the dashboard's military section and the CSVs need, for one period."""
    info = store.aircraft_info()
    fl = flights(store, since, info)
    airfields: dict[str, dict] = {}
    for f in fl:
        for key, kind in (("from", "departures"), ("to", "arrivals")):
            a = f[key]
            if not a:
                continue
            row = airfields.setdefault(a["icao"], {"icao": a["icao"], "name": a["name"], "country": a["country"],
                                                   "departures": 0, "arrivals": 0, "aircraft": set(), "types": {}})
            row[kind] += 1
            row["aircraft"].add(f["hex"])
            if f["type"]:
                row["types"][f["type"]] = row["types"].get(f["type"], 0) + 1
    airfield_rows = []
    for r in airfields.values():
        r["unique_aircraft"] = len(r.pop("aircraft"))
        r["top_types"] = ", ".join(t for t, _ in sorted(r.pop("types").items(), key=lambda kv: -kv[1])[:3])
        r["movements"] = r["departures"] + r["arrivals"]
        airfield_rows.append(r)
    airfield_rows.sort(key=lambda r: -r["movements"])

    seen = {h: m for h, m in info.items() if m["military"] and (m["last_seen"] or 0) >= since}
    types: dict[str, dict] = {}
    operators: dict[str, int] = {}
    for m in seen.values():
        t = m["type"] or "?"
        row = types.setdefault(t, {"type": t, "descr": m["descr"] or "", "aircraft": 0})
        row["aircraft"] += 1
        if m["operator"]:
            operators[m["operator"]] = operators.get(m["operator"], 0) + 1
    per_aircraft = {}
    for f in fl:
        r = per_aircraft.setdefault(f["hex"], {"hex": f["hex"], "callsign": f["callsign"], "reg": f["reg"], "type": f["type"],
                                               "operator": f["operator"], "flights": 0, "km": 0, "airfields": set()})
        r["flights"] += 1
        r["km"] += f["km"]
        for key in ("from", "to"):
            if f[key]:
                r["airfields"].add(f[key]["icao"])
    aircraft_rows = []
    for r in per_aircraft.values():
        r["airfields"] = ", ".join(sorted(r["airfields"]))
        aircraft_rows.append(r)
    aircraft_rows.sort(key=lambda r: -r["km"])
    return {
        "since": since,
        "positions": store.count_positions(since),
        "aircraft_seen": len(seen),
        "flights": fl,
        "airfields": airfield_rows,
        "types": sorted(types.values(), key=lambda r: -r["aircraft"]),
        "operators": [{"operator": k, "aircraft": v} for k, v in sorted(operators.items(), key=lambda kv: -kv[1])],
        "aircraft": aircraft_rows,
    }


def write_csvs(summary: dict, folder: Path, suffix: str):
    """military_flights_<suffix>.csv, military_airfields_<suffix>.csv, military_aircraft_<suffix>.csv"""
    from datetime import datetime, timezone

    def iso(ts):
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M") if ts else ""

    folder.mkdir(parents=True, exist_ok=True)
    with (folder / f"military_flights_{suffix}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["start_utc", "end_utc", "minutes", "hex", "callsign", "reg", "type", "operator", "from_icao", "from_name",
                    "to_icao", "to_name", "start_lat", "start_lon", "end_lat", "end_lon", "km", "max_alt_ft"])
        for f in summary["flights"]:
            fr, to = f["from"] or {}, f["to"] or {}
            w.writerow([iso(f["start_ts"]), iso(f["end_ts"]), f["minutes"], f["hex"], f["callsign"], f["reg"], f["type"], f["operator"],
                        fr.get("icao", ""), fr.get("name", ""), to.get("icao", ""), to.get("name", ""),
                        f["start_lat"], f["start_lon"], f["end_lat"], f["end_lon"], f["km"], f["max_alt"]])
    with (folder / f"military_airfields_{suffix}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["icao", "name", "country", "movements", "departures", "arrivals", "unique_aircraft", "top_types"])
        for r in summary["airfields"]:
            w.writerow([r["icao"], r["name"], r["country"], r["movements"], r["departures"], r["arrivals"], r["unique_aircraft"], r["top_types"]])
    with (folder / f"military_aircraft_{suffix}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["hex", "callsign", "reg", "type", "operator", "flights", "km", "airfields"])
        for r in summary["aircraft"]:
            w.writerow([r["hex"], r["callsign"], r["reg"], r["type"], r["operator"], r["flights"], r["km"], r["airfields"]])


def aircraft_report(db_path: Path, hex_code: str, days: float = 7) -> dict:
    """Everything recorded about one aircraft: details, flights and a thinned track (for the dashboard lookup)."""
    store = Store(db_path)
    try:
        hex_code = hex_code.upper()
        info = store.aircraft_info()
        meta = info.get(hex_code)
        if not meta:
            return {"hex": hex_code, "found": False}
        since = time.time() - days * 86400
        fl = flights(store, since, info, hex_code)
        pts = next((p for h, p in store.iter_tracks(since, hex_code)), [])
        step = max(1, len(pts) // 500)
        near, d = nearest_airfield(meta["last_lat"], meta["last_lon"], 60) if meta.get("last_lat") is not None else (None, 0)
        return {
            "hex": hex_code, "found": True, "info": meta, "flights": fl, "positions": len(pts),
            "track": [[round(p[2], 3), round(p[1], 3), p[3]] for p in pts[::step]],
            "near": {"icao": near["icao"], "name": near["name"], "km": round(d)} if near else None,
            "airfields": sorted({f[k]["icao"] for f in fl for k in ("from", "to") if f[k]}),
            "km": sum(f["km"] for f in fl),
        }
    finally:
        store.db.close()
