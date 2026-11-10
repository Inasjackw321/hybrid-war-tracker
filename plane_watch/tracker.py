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


_MILITARY_WORDS = ("air base", "afb", " nas ", "naval", "mcas", "army", "air force", " aaf", "air national guard", "joint base",
                   "raf ", "air station", "nolf", "airbase", "military", "air reserve", " arb", " ab ", "marine corps")


def _is_military_base(a: dict) -> bool:
    name = " " + (a.get("name") or "").lower() + " "
    return any(w in name for w in _MILITARY_WORDS)


def _is_major(a: dict) -> bool:
    """A real airport (has an IATA code) or a military base, as opposed to a private strip or farm field."""
    return bool(a.get("iata")) or _is_military_base(a)


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


class Filter:
    """What the dashboard's filter bar asks for. Empty fields match everything."""

    def __init__(self, types=(), operators=(), airfield: str = "", text: str = ""):
        self.types = tuple(t.strip().upper() for t in types if t and t.strip())
        self.operators = tuple(o.strip() for o in operators if o and o.strip())
        self.airfield = (airfield or "").strip().upper()
        self.text = (text or "").strip().lower()

    @classmethod
    def from_query(cls, q: dict) -> "Filter":
        def many(k):
            return [x for v in q.get(k, []) for x in v.split(",")]

        return cls(many("type"), many("operator"), (q.get("airfield") or [""])[0], (q.get("q") or [""])[0])

    @property
    def empty(self) -> bool:
        return not (self.types or self.operators or self.airfield or self.text)

    def key(self) -> str:
        return "|".join([",".join(sorted(self.types)), ",".join(sorted(self.operators)), self.airfield, self.text])

    def match_meta(self, m: dict) -> bool:
        if self.types and (m.get("type") or "").upper() not in self.types:
            return False
        if self.operators and not any(o.lower() in (m.get("operator") or "").lower() for o in self.operators):
            return False
        if self.text:
            hay = " ".join(str(m.get(k) or "") for k in ("hex", "callsign", "reg", "type", "descr", "operator")).lower()
            if self.text not in hay:
                return False
        return True

    def sql(self) -> tuple[str, list]:
        """Extra WHERE conditions on the `ac` table (airfield is handled separately, it needs flights)."""
        conds, params = [], []
        if self.types:
            conds.append(f"UPPER(ac.type) IN ({','.join('?' * len(self.types))})")
            params += list(self.types)
        if self.operators:
            conds.append("(" + " OR ".join("LOWER(ac.operator) LIKE ?" for _ in self.operators) + ")")
            params += [f"%{o.lower()}%" for o in self.operators]
        if self.text:
            conds.append("LOWER(COALESCE(ac.hex,'') || ' ' || COALESCE(ac.callsign,'') || ' ' || COALESCE(ac.reg,'') || ' ' || "
                         "COALESCE(ac.type,'') || ' ' || COALESCE(ac.descr,'') || ' ' || COALESCE(ac.operator,'')) LIKE ?")
            params.append(f"%{self.text}%")
        return ("".join(" AND " + c for c in conds), params)


NO_FILTER = Filter()


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

    def _filter_sql(self, filt: Filter | None, hexes: set | None) -> tuple[str, list]:
        clause, params = (filt or NO_FILTER).sql()
        if hexes is not None:
            clause += f" AND p.hex IN ({','.join('?' * len(hexes))})" if hexes else " AND 0"
            params += sorted(hexes)
        return clause, params

    def points(self, since: float, military_only: bool = True, cell_deg: float = 0.1, filt: Filter | None = None,
               hexes: set | None = None) -> list[tuple[float, float, int]]:
        """(lon, lat, count) per cell_deg grid cell: the heatmap input, binned inside SQLite."""
        q = f"SELECT ROUND(p.lon / {cell_deg}) * {cell_deg}, ROUND(p.lat / {cell_deg}) * {cell_deg}, COUNT(*) FROM pos p"
        q += " JOIN ac ON ac.hex = p.hex" + (" AND ac.military = 1" if military_only else "")
        clause, params = self._filter_sql(filt, hexes)
        return self.db.execute(q + " WHERE p.ts >= ?" + clause + " GROUP BY 1, 2", (since, *params)).fetchall()

    def segment_chunks(self, since: float, chunk: int = 200_000, filt: Filter | None = None, hexes: set | None = None):
        """Yield numpy arrays of [lon, lat, prev_lon, prev_lat, seconds_since_prev] for military positions:
        consecutive points of the same aircraft, so the heatmap can draw paths instead of dots."""
        import numpy as np

        q = """SELECT p.lon, p.lat, LAG(p.lon) OVER w, LAG(p.lat) OVER w, p.ts - LAG(p.ts) OVER w
               FROM pos p JOIN ac ON ac.hex = p.hex AND ac.military = 1
               WHERE p.ts >= ? {clause}
               WINDOW w AS (PARTITION BY p.hex ORDER BY p.ts)"""
        clause, params = self._filter_sql(filt, hexes)
        try:
            cur = self.db.execute(q.replace("{clause}", clause), (since, *params))
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

    def count_positions(self, since: float, filt: Filter | None = None, hexes: set | None = None) -> int:
        if (filt is None or filt.empty) and hexes is None:
            return self.db.execute("SELECT COUNT(*) FROM pos WHERE ts >= ?", (since,)).fetchone()[0]
        clause, params = self._filter_sql(filt, hexes)
        return self.db.execute("SELECT COUNT(*) FROM pos p JOIN ac ON ac.hex = p.hex AND ac.military = 1 WHERE p.ts >= ?" + clause,
                               (since, *params)).fetchone()[0]

    def catalog(self, since: float) -> dict:
        """Types and operators seen, for the dashboard's filter menus."""
        types = [{"type": t, "descr": d or "", "aircraft": n} for t, d, n in self.db.execute(
            "SELECT type, MAX(descr), COUNT(*) FROM ac WHERE military = 1 AND last_seen >= ? AND type <> '' GROUP BY type ORDER BY 3 DESC",
            (since,))]
        ops = [{"operator": o, "aircraft": n} for o, n in self.db.execute(
            "SELECT operator, COUNT(*) FROM ac WHERE military = 1 AND last_seen >= ? AND operator <> '' GROUP BY operator ORDER BY 2 DESC",
            (since,))]
        return {"types": types, "operators": ops}

    def watched(self, since: float, types=tuple(WATCH_TYPES)) -> list[dict]:
        marks = ",".join("?" * len(types))
        info = [dict(zip(["hex", "callsign", "reg", "type", "descr", "operator", "last_seen", "lat", "lon", "alt", "heading", "gs"], r))
                for r in self.db.execute(f"""SELECT hex, callsign, reg, type, descr, operator, last_seen, last_lat, last_lon, last_alt,
                                                    last_trk, last_gs
                                             FROM ac WHERE UPPER(type) IN ({marks}) AND military = 1 AND last_seen >= ?
                                             ORDER BY last_seen DESC""",
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


# ---- where a flight started and ended
#
# Confirmed: the aircraft reported "on the ground" at an airfield.
# Inferred: the signal started or stopped while it was low, close to an airfield, and climbing away from it /
# descending towards it. Receivers lose aircraft below the horizon, so most real landings look like this.
INFER_MAX_KM = 90  # never infer an airfield further away than this
INFER_MAX_HEIGHT_FT = 12000  # ...or from higher above the field than this
GLIDE_KM_PER_1000FT = 9.0  # a 3-degree approach covers ~6 km per 1000 ft; allow 1.5x for shallow/vectored approaches
GROUND_RADIUS_KM = 25


def airfields_within(lat: float, lon: float, radius_km: float) -> list[tuple[dict, float]]:
    grid = _airfield_grid()
    la, lo = math.floor(lat), math.floor(lon)
    span = max(1, math.ceil(radius_km / (111.0 * max(0.2, math.cos(math.radians(lat))))))
    out = []
    for dy in (-1, 0, 1):
        for dx in range(-span, span + 1):
            for a in grid.get((la + dy, (lo + dx + 180) % 360 - 180), []):
                d = geo.haversine_km(lat, lon, a["lat"], a["lon"])
                if d <= radius_km:
                    out.append((a, d))
    return out


def _bearing_deg(lat1, lon1, lat2, lon2) -> float:
    return math.degrees(geo._bearing(lat1, lon1, lat2, lon2)) % 360


def _angle_diff(a, b) -> float:
    return abs((a - b + 180) % 360 - 180)


def infer_end(points: list[tuple], which: str, military: bool = True) -> dict | None:
    """The airfield a flight started from (which="start") or ended at ("end"), or None if it was en route.
    Returns {"icao", "name", "country", "km", "how": "ground" | "inferred", "alt": ft at that moment}."""
    p = points[0] if which == "start" else points[-1]
    ts, lat, lon, alt = p

    def pack(a, d, how):
        return {"icao": a["icao"], "name": a["name"], "country": a.get("country", ""), "km": round(d, 1), "how": how,
                "alt": alt, "lat": a["lat"], "lon": a["lon"]}

    if alt == 0:  # reported on the ground
        a, d = nearest_airfield(lat, lon, GROUND_RADIUS_KM)
        return pack(a, d, "ground") if a else None

    # the trend just after the start / just before the end
    near = points[1:5] if which == "start" else points[-5:-1][::-1]
    vrate = track = None
    for q in near:
        dt_min = abs(q[0] - ts) / 60
        if dt_min < 0.5 or dt_min > 12:
            continue
        if alt is not None and q[3] is not None and vrate is None:
            vrate = (q[3] - alt) / dt_min if which == "start" else (alt - q[3]) / dt_min  # ft/min, + = climbing over time
        if track is None and geo.haversine_km(lat, lon, q[1], q[2]) > 0.5:
            track = _bearing_deg(lat, lon, q[1], q[2]) if which == "start" else _bearing_deg(q[1], q[2], lat, lon)
        if vrate is not None and track is not None:
            break

    best, best_score = None, 1e9
    for a, d in airfields_within(lat, lon, INFER_MAX_KM):
        height = (alt if alt is not None else 99999) - (a.get("elevation") or 0)
        # allow a level stretch at approach altitude (15 km) plus the descent itself
        if height > INFER_MAX_HEIGHT_FT or d > 15 + max(height, 0) / 1000 * GLIDE_KM_PER_1000FT:
            continue
        # must be heading the right way vertically (unless already very low)
        if height > 1500:
            if vrate is None:
                if height > 4000:
                    continue
            elif (which == "end" and vrate > 300) or (which == "start" and vrate < -300):
                continue
        # ...and horizontally (unless it's right on top of the field)
        off = 0.0
        if track is not None and d > 12:
            want = _bearing_deg(lat, lon, a["lat"], a["lon"]) if which == "end" else _bearing_deg(a["lat"], a["lon"], lat, lon)
            off = _angle_diff(track, want)
            if off > 70:
                continue
        major = _is_major(a)
        if not major and (d > 10 or height > 2500):
            continue  # a private strip only counts if the aircraft was very low and right next to it
        # closer is better, but pointing straight at the field matters more than a few km, and a military
        # aircraft is more likely going to a military base than to the civil airport next door
        score = (d + (0 if major else 40)) * (1 + off / 30) * (0.75 if military and _is_military_base(a) else 1.0)
        if score < best_score:
            best, best_score = (a, d), score
    return pack(best[0], best[1], "inferred") if best else None


def loiter(points: list[tuple]) -> dict | None:
    """Did this flight orbit / hold somewhere? Looks for 40-minute stretches at altitude where the aircraft flew a
    long way but ended up close to where it began. Returns the centre and how long it stayed."""
    if len(points) < 8:
        return None
    cum = [0.0]
    for a, b in zip(points, points[1:]):
        cum.append(cum[-1] + geo.haversine_km(a[1], a[2], b[1], b[2]))
    flagged = [False] * len(points)
    j = 0
    for i in range(len(points)):
        while j < len(points) - 1 and points[j][0] - points[i][0] < 40 * 60:
            j += 1
        if points[j][0] - points[i][0] < 35 * 60:
            break
        if min((q[3] or 0) for q in points[i:j + 1]) < 8000:
            continue
        path, disp = cum[j] - cum[i], geo.haversine_km(points[i][1], points[i][2], points[j][1], points[j][2])
        if path > 150 and disp < path * 0.3 and disp < 120:
            for k in range(i, j + 1):
                flagged[k] = True
    idx = [k for k, f in enumerate(flagged) if f]
    if not idx:
        return None
    minutes = sum((points[k + 1][0] - points[k][0]) / 60 for k in idx[:-1] if flagged[k + 1] and points[k + 1][0] - points[k][0] < 900)
    lat = sum(points[k][1] for k in idx) / len(idx)
    lon = sum(points[k][2] for k in idx) / len(idx)
    a, d = nearest_airfield(lat, lon, 150)
    return {"lat": round(lat, 2), "lon": round(lon, 2), "minutes": round(minutes),
            "alt": round(sum((points[k][3] or 0) for k in idx) / len(idx), -2),
            "near": {"icao": a["icao"], "name": a["name"], "km": round(d)} if a else None}


def flights(store: Store, since: float, info: dict | None = None, hex_code: str | None = None,
            filt: Filter | None = None) -> list[dict]:
    info = info or store.aircraft_info()
    filt = filt or NO_FILTER
    out = []
    for h, pts in store.iter_tracks(since, hex_code):
        meta = info.get(h, {})
        if not hex_code and (not meta.get("military") or not filt.match_meta(meta)):
            continue
        for f in split_flights(pts):
            dist = sum(geo.haversine_km(a[1], a[2], b[1], b[2]) for a, b in zip(f, f[1:]))
            start, end = f[0], f[-1]
            mil = bool(meta.get("military", True))
            frm, to = infer_end(f, "start", mil), infer_end(f, "end", mil)
            lo = loiter(f)
            minutes = round((end[0] - start[0]) / 60)
            if frm and to:
                kind = "local" if frm["icao"] == to["icao"] else "transit"
            elif frm or to:
                kind = "partial"  # one end seen
            else:
                kind = "en route"  # passed through coverage
            out.append({
                "hex": h, "callsign": meta.get("callsign") or "", "reg": meta.get("reg") or "", "type": meta.get("type") or "",
                "descr": meta.get("descr") or "", "operator": meta.get("operator") or "",
                "start_ts": start[0], "end_ts": end[0], "minutes": minutes,
                "start_lat": round(start[1], 3), "start_lon": round(start[2], 3), "end_lat": round(end[1], 3), "end_lon": round(end[2], 3),
                "from": frm, "to": to, "kind": kind, "loiter": lo,
                "max_alt": max((p[3] or 0) for p in f), "km": round(dist), "points": len(f),
                "kmh": round(dist / (minutes / 60)) if minutes >= 5 else None,
            })
    if filt.airfield:
        a = filt.airfield.lower()
        out = [f for f in out if any(f[k] and (f[k]["icao"].lower() == a or a in f[k]["name"].lower()) for k in ("from", "to"))]
    out.sort(key=lambda r: -r["end_ts"])
    return out


def _top(counter: dict, n: int = 3) -> str:
    return ", ".join(k for k, _ in sorted(counter.items(), key=lambda kv: -kv[1])[:n])


def summarize(store: Store, since: float, filt: Filter | None = None, now: float | None = None) -> dict:
    """Everything the dashboard's military section and the CSVs need, for one period (optionally filtered):
    flights, airfields, routes, loitering, per-type and per-aircraft statistics, activity by hour, a comparison
    with the period before, and a few plain-language findings."""
    filt = filt or NO_FILTER
    now = now or time.time()
    info = store.aircraft_info()
    fl = flights(store, since, info, filt=filt)
    airfield_hexes = {f["hex"] for f in fl} if filt.airfield else None

    # ---- airfields, with confirmed vs inferred movements and activity by hour of day (UTC)
    airfields: dict[str, dict] = {}
    for f in fl:
        for key, kind, ts in (("from", "departures", f["start_ts"]), ("to", "arrivals", f["end_ts"])):
            a = f[key]
            if not a:
                continue
            row = airfields.setdefault(a["icao"], {
                "icao": a["icao"], "name": a["name"], "country": a["country"], "lat": a["lat"], "lon": a["lon"], "departures": 0,
                "arrivals": 0, "inferred": 0, "aircraft": set(), "types": {}, "links": {}, "by_hour": [0] * 24})
            row[kind] += 1
            row["inferred"] += a["how"] == "inferred"
            row["aircraft"].add(f["hex"])
            row["by_hour"][int(ts // 3600) % 24] += 1
            if f["type"]:
                row["types"][f["type"]] = row["types"].get(f["type"], 0) + 1
            other = f["to" if key == "from" else "from"]
            if other and other["icao"] != a["icao"]:
                row["links"][other["icao"]] = row["links"].get(other["icao"], 0) + 1
    airfield_rows = []
    for r in airfields.values():
        r["unique_aircraft"] = len(r.pop("aircraft"))
        r["top_types"] = _top(r.pop("types"))
        r["top_links"] = _top(r.pop("links"))
        r["movements"] = r["departures"] + r["arrivals"]
        r["peak_hour"] = max(range(24), key=lambda h: r["by_hour"][h])
        airfield_rows.append(r)
    airfield_rows.sort(key=lambda r: -r["movements"])

    # ---- routes between two different airfields
    routes: dict[tuple, dict] = {}
    for f in fl:
        if f["kind"] != "transit":
            continue
        key = (f["from"]["icao"], f["to"]["icao"])
        r = routes.setdefault(key, {"from": f["from"]["icao"], "from_name": f["from"]["name"], "to": f["to"]["icao"],
                                    "to_name": f["to"]["name"], "flights": 0, "aircraft": set(), "types": {}, "minutes": 0,
                                    "km": round(geo.haversine_km(f["from"]["lat"], f["from"]["lon"], f["to"]["lat"], f["to"]["lon"]))})
        r["flights"] += 1
        r["aircraft"].add(f["hex"])
        r["minutes"] += f["minutes"]
        if f["type"]:
            r["types"][f["type"]] = r["types"].get(f["type"], 0) + 1
    route_rows = []
    for r in routes.values():
        r["aircraft"] = len(r["aircraft"])
        r["types"] = _top(r["types"])
        r["avg_minutes"] = round(r.pop("minutes") / r["flights"])
        route_rows.append(r)
    route_rows.sort(key=lambda r: (-r["flights"], -r["km"]))

    # ---- who was seen
    seen = {h: m for h, m in info.items() if m["military"] and (m["last_seen"] or 0) >= since and filt.match_meta(m)
            and (airfield_hexes is None or h in airfield_hexes)}
    types: dict[str, dict] = {}
    operators: dict[str, int] = {}
    for m in seen.values():
        t = m["type"] or "?"
        row = types.setdefault(t, {"type": t, "descr": m["descr"] or "", "aircraft": 0, "flights": 0, "minutes": 0, "km": 0,
                                   "max_alt": 0, "loiter_minutes": 0, "airfields": {}})
        row["aircraft"] += 1
        if m["operator"]:
            operators[m["operator"]] = operators.get(m["operator"], 0) + 1
    new_aircraft = sorted((m for m in seen.values() if (m["first_seen"] or 0) >= now - 86400 and (m["first_seen"] or 0) > since + 3600),
                          key=lambda m: -(m["first_seen"] or 0))

    # ---- per type and per aircraft
    per_aircraft: dict[str, dict] = {}
    kinds: dict[str, int] = {}
    ends = {"ground": 0, "inferred": 0, "unknown": 0}
    loiters = []
    for f in fl:
        kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
        for key in ("from", "to"):
            ends[f[key]["how"] if f[key] else "unknown"] += 1
        t = types.setdefault(f["type"] or "?", {"type": f["type"] or "?", "descr": f["descr"], "aircraft": 0, "flights": 0, "minutes": 0,
                                                "km": 0, "max_alt": 0, "loiter_minutes": 0, "airfields": {}})
        t["flights"] += 1
        t["minutes"] += f["minutes"]
        t["km"] += f["km"]
        t["max_alt"] = max(t["max_alt"], f["max_alt"])
        r = per_aircraft.setdefault(f["hex"], {"hex": f["hex"], "callsign": f["callsign"], "reg": f["reg"], "type": f["type"],
                                               "operator": f["operator"], "flights": 0, "km": 0, "minutes": 0, "airfields": {},
                                               "loiter_minutes": 0, "last_ts": 0, "last_airfield": ""})
        r["flights"] += 1
        r["km"] += f["km"]
        r["minutes"] += f["minutes"]
        if f["end_ts"] > r["last_ts"]:
            r["last_ts"], r["last_airfield"] = f["end_ts"], (f["to"] or {}).get("icao", "")
        for key in ("from", "to"):
            if f[key]:
                r["airfields"][f[key]["icao"]] = r["airfields"].get(f[key]["icao"], 0) + 1
                t["airfields"][f[key]["icao"]] = t["airfields"].get(f[key]["icao"], 0) + 1
        if f["loiter"]:
            lo = f["loiter"]
            r["loiter_minutes"] += lo["minutes"]
            t["loiter_minutes"] += lo["minutes"]
            loiters.append({"hex": f["hex"], "callsign": f["callsign"], "type": f["type"], "operator": f["operator"],
                            "start_ts": f["start_ts"], "minutes": lo["minutes"], "alt": lo["alt"], "lat": lo["lat"], "lon": lo["lon"],
                            "near": lo["near"], "from": f["from"], "to": f["to"]})
    aircraft_rows = []
    for r in per_aircraft.values():
        r["home"] = max(r["airfields"], key=r["airfields"].get) if r["airfields"] else ""  # the airfield it used most
        r["airfields"] = ", ".join(sorted(r["airfields"]))
        r["hours"] = round(r.pop("minutes") / 60, 1)
        aircraft_rows.append(r)
    aircraft_rows.sort(key=lambda r: -r["km"])
    type_rows = []
    for t in types.values():
        t["hours"] = round(t["minutes"] / 60, 1)
        t["avg_minutes"] = round(t.pop("minutes") / t["flights"]) if t["flights"] else 0
        t["avg_km"] = round(t["km"] / t["flights"]) if t["flights"] else 0
        t["top_airfields"] = _top(t.pop("airfields"))
        type_rows.append(t)
    type_rows.sort(key=lambda r: (-r["aircraft"], -r["flights"]))
    loiters.sort(key=lambda r: -r["minutes"])

    # ---- activity over time, and against the period before
    clause, params = store._filter_sql(filt, airfield_hexes)
    hourly_air = dict(store.db.execute(
        "SELECT CAST(p.ts / 3600 AS INTEGER), COUNT(DISTINCT p.hex) FROM pos p JOIN ac ON ac.hex = p.hex AND ac.military = 1 "
        "WHERE p.ts >= ?" + clause + " GROUP BY 1", (since, *params)).fetchall())
    dep_by_hour: dict[int, int] = {}
    for f in fl:
        if f["from"]:
            dep_by_hour[int(f["start_ts"] // 3600)] = dep_by_hour.get(int(f["start_ts"] // 3600), 0) + 1
    hourly = [{"ts": h * 3600, "airborne": hourly_air.get(h, 0), "departures": dep_by_hour.get(h, 0)}
              for h in range(int(since // 3600), int(now // 3600) + 1)]
    span = now - since
    first_ts = store.db.execute("SELECT MIN(ts) FROM pos").fetchone()[0]
    previous = None
    if first_ts is not None and first_ts <= since - span * 0.9:  # only compare if the earlier period was fully recorded
        prev_aircraft, prev_positions = store.db.execute(
            "SELECT COUNT(DISTINCT p.hex), COUNT(*) FROM pos p JOIN ac ON ac.hex = p.hex AND ac.military = 1 "
            "WHERE p.ts >= ? AND p.ts < ?" + clause, (since - span, since, *params)).fetchone()
        active_now = store.db.execute(
            "SELECT COUNT(DISTINCT p.hex) FROM pos p JOIN ac ON ac.hex = p.hex AND ac.military = 1 WHERE p.ts >= ?" + clause,
            (since, *params)).fetchone()[0]
        previous = {"aircraft": prev_aircraft, "positions": prev_positions, "aircraft_now": active_now,
                    "change_pct": round((active_now - prev_aircraft) / prev_aircraft * 100) if prev_aircraft else None}

    out = {
        "since": since,
        "positions": store.count_positions(since, filt, airfield_hexes),
        "hexes": airfield_hexes,
        "aircraft_seen": len(seen),
        "flights": fl,
        "airfields": airfield_rows,
        "routes": route_rows,
        "loiters": loiters,
        "types": type_rows,
        "operators": [{"operator": k, "aircraft": v} for k, v in sorted(operators.items(), key=lambda kv: -kv[1])],
        "aircraft": aircraft_rows,
        "kinds": kinds,
        "ends": ends,
        "hourly": hourly,
        "previous": previous,
        "new_aircraft": [{"hex": m["hex"], "callsign": m["callsign"] or "", "type": m["type"] or "", "descr": m["descr"] or "",
                          "operator": m["operator"] or "", "first_seen": m["first_seen"]} for m in new_aircraft[:100]],
        "flight_hours": round(sum(f["minutes"] for f in fl) / 60, 1),
    }
    out["insights"] = insights(out)
    return out


def insights(s: dict) -> list[str]:
    """A few plain-language findings from a summary, most important first."""
    out = []
    fl = s["flights"]
    if not fl and not s["aircraft_seen"]:
        return ["Nothing recorded for this period yet."]
    out.append(f"{s['aircraft_seen']:,} military aircraft seen, flying {len(fl):,} flights and {s['flight_hours']:,.0f} hours.")
    p = s.get("previous")
    if p and p.get("change_pct") is not None:
        word = "up" if p["change_pct"] > 0 else "down" if p["change_pct"] < 0 else "level"
        out.append(f"Activity is {word} {abs(p['change_pct'])}% on the period before ({p['aircraft_now']} aircraft vs {p['aircraft']})."
                   if word != "level" else f"Activity is level with the period before ({p['aircraft']} aircraft).")
    if s["airfields"]:
        a = s["airfields"][0]
        out.append(f"Busiest airfield: {a['icao']} {a['name']} with {a['movements']} movements ({a['departures']} out, {a['arrivals']} in), "
                   f"mostly {a['top_types'] or 'mixed types'}; busiest around {a['peak_hour']:02d}:00 UTC.")
    if s["routes"]:
        r = s["routes"][0]
        out.append(f"Most flown route: {r['from']} → {r['to']} ({r['flights']} flights, about {r['km']:,} km, {r['types'] or 'mixed types'}).")
    flown = [t for t in s["types"] if t["flights"]]
    if flown:
        t = max(flown, key=lambda t: t["hours"])
        out.append(f"Most flying by type: {t['type']} with {t['hours']:,.0f} hours over {t['flights']} flights "
                   f"(average {t['avg_minutes']} min, {t['avg_km']:,} km).")
    if s["loiters"]:
        lo = s["loiters"][0]
        where = f"near {lo['near']['icao']}" if lo["near"] else f"around {lo['lat']}, {lo['lon']}"
        n = len(s["loiters"])
        out.append(f"{n} flight{'s' if n != 1 else ''} orbited or held. Longest: {lo['callsign'] or lo['hex']} ({lo['type']}) for "
                   f"{lo['minutes']} min at about {lo['alt']:,.0f} ft {where}.")
    k = s["kinds"]
    if fl:
        out.append(f"Flight mix: {k.get('transit', 0)} between airfields, {k.get('local', 0)} local (out and back), "
                   f"{k.get('partial', 0)} with one end seen, {k.get('en route', 0)} passing through coverage.")
    e = s["ends"]
    known = e["ground"] + e["inferred"]
    if known:
        out.append(f"Take-offs and landings: {e['ground']} confirmed on the ground, {e['inferred']} inferred from a low approach or "
                   f"climb-out where the signal started or stopped.")
    peak = max(s["hourly"], key=lambda h: h["airborne"], default=None)
    if peak and peak["airborne"]:
        from datetime import datetime, timezone

        out.append(f"Peak hour: {datetime.fromtimestamp(peak['ts'], timezone.utc).strftime('%d %b %H:00')} UTC with "
                   f"{peak['airborne']} aircraft airborne.")
    if s["new_aircraft"]:
        out.append(f"{len(s['new_aircraft'])} aircraft seen for the first time in the last 24 hours.")
    return out


def write_csvs(summary: dict, folder: Path, suffix: str):
    """military_{flights,airfields,aircraft,routes,types,loiters}_<suffix>.csv"""
    from datetime import datetime, timezone

    def iso(ts):
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M") if ts else ""

    def write(name, header, rows):
        with (folder / f"military_{name}_{suffix}.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(rows)

    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for f in summary["flights"]:
        fr, to, lo = f["from"] or {}, f["to"] or {}, f.get("loiter") or {}
        rows.append([iso(f["start_ts"]), iso(f["end_ts"]), f["minutes"], f["hex"], f["callsign"], f["reg"], f["type"], f["operator"],
                     f.get("kind", ""), fr.get("icao", ""), fr.get("name", ""), fr.get("how", ""), to.get("icao", ""), to.get("name", ""),
                     to.get("how", ""), f["start_lat"], f["start_lon"], f["end_lat"], f["end_lon"], f["km"], f["max_alt"],
                     f.get("kmh") or "", lo.get("minutes", ""), lo.get("lat", ""), lo.get("lon", "")])
    write("flights", ["start_utc", "end_utc", "minutes", "hex", "callsign", "reg", "type", "operator", "kind", "from_icao", "from_name",
                      "from_how", "to_icao", "to_name", "to_how", "start_lat", "start_lon", "end_lat", "end_lon", "km", "max_alt_ft",
                      "avg_kmh", "loiter_minutes", "loiter_lat", "loiter_lon"], rows)
    write("airfields", ["icao", "name", "country", "movements", "departures", "arrivals", "inferred_movements", "unique_aircraft",
                        "top_types", "top_links", "peak_hour_utc"],
          [[r["icao"], r["name"], r["country"], r["movements"], r["departures"], r["arrivals"], r.get("inferred", ""), r["unique_aircraft"],
            r["top_types"], r.get("top_links", ""), r.get("peak_hour", "")] for r in summary["airfields"]])
    write("aircraft", ["hex", "callsign", "reg", "type", "operator", "flights", "hours", "km", "home_airfield", "last_airfield",
                       "loiter_minutes", "airfields"],
          [[r["hex"], r["callsign"], r["reg"], r["type"], r["operator"], r["flights"], r.get("hours", ""), r["km"], r.get("home", ""),
            r.get("last_airfield", ""), r.get("loiter_minutes", ""), r["airfields"]] for r in summary["aircraft"]])
    write("routes", ["from_icao", "from_name", "to_icao", "to_name", "flights", "aircraft", "types", "km", "avg_minutes"],
          [[r["from"], r["from_name"], r["to"], r["to_name"], r["flights"], r["aircraft"], r["types"], r["km"], r["avg_minutes"]]
           for r in summary.get("routes", [])])
    write("types", ["type", "description", "aircraft", "flights", "hours", "avg_minutes", "avg_km", "max_alt_ft", "loiter_minutes",
                    "top_airfields"],
          [[t["type"], t["descr"], t["aircraft"], t.get("flights", ""), t.get("hours", ""), t.get("avg_minutes", ""), t.get("avg_km", ""),
            t.get("max_alt", ""), t.get("loiter_minutes", ""), t.get("top_airfields", "")] for t in summary.get("types", [])])
    write("loiters", ["start_utc", "hex", "callsign", "type", "operator", "minutes", "alt_ft", "lat", "lon", "near_icao", "near_km"],
          [[iso(r["start_ts"]), r["hex"], r["callsign"], r["type"], r["operator"], r["minutes"], r["alt"], r["lat"], r["lon"],
            (r["near"] or {}).get("icao", ""), (r["near"] or {}).get("km", "")] for r in summary.get("loiters", [])])


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
