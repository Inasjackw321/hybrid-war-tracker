"""ADS-B flight tracking from the free, readsb-compatible community APIs.

adsb.lol, airplanes.live and adsb.fi all serve the same "v2" JSON as readsb /
tar1090, so one parser covers them. They also publish per-aircraft trace files
(the track history tar1090 draws), which we use for the track line.
"""

from __future__ import annotations

import time

from .. import http
from ..models import Aircraft, TrackPoint

SOURCES = {
    "adsb.lol": {
        "api": "https://api.adsb.lol/v2",
        "paths": {"sqk": "/sqk/{}", "hex": "/hex/{}", "callsign": "/callsign/{}", "reg": "/reg/{}", "mil": "/mil",
                  "type": "/type/{}"},
        "globe": "https://globe.adsb.lol",
        "route": "https://api.adsb.lol/api/0/routeset",
    },
    "airplanes.live": {
        "api": "https://api.airplanes.live/v2",
        "paths": {"sqk": "/sqk/{}", "hex": "/hex/{}", "callsign": "/callsign/{}", "reg": "/reg/{}", "mil": "/mil",
                  "type": "/type/{}"},
        "globe": "https://globe.airplanes.live",
    },
    "adsb.fi": {
        "api": "https://opendata.adsb.fi/api/v2",
        "paths": {"sqk": "/sqk/{}", "hex": "/hex/{}", "callsign": "/callsign/{}", "reg": "/registration/{}", "mil": "/mil"},
        "globe": "https://globe.adsb.fi",
    },
}
DEFAULT_SOURCES = ("adsb.lol", "airplanes.live", "adsb.fi")
REQUEST_GAP_S = 1.1  # the community APIs ask for ~1 request per second


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_aircraft(d: dict, now_s: float, source: str = "") -> Aircraft:
    pos = d if d.get("lat") is not None else (d.get("lastPosition") or {})
    alt = d.get("alt_baro")
    on_ground = alt == "ground"
    seen = _float(d.get("seen_pos", pos.get("seen_pos")))
    msg_age = _float(d.get("seen"))
    db_flags = _int(d.get("dbFlags")) or 0
    return Aircraft(
        hex=str(d.get("hex", "")).upper(),
        callsign=str(d.get("flight") or "").strip(),
        registration=str(d.get("r") or "").strip(),
        type_code=str(d.get("t") or "").strip(),
        description=str(d.get("desc") or "").strip(),
        operator=str(d.get("ownOp") or "").strip(),
        lat=_float(pos.get("lat")),
        lon=_float(pos.get("lon")),
        alt_ft=0 if on_ground else _int(alt if alt is not None else d.get("alt_geom")),
        on_ground=on_ground,
        speed_kt=_float(d.get("gs")),
        heading=_float(d.get("track", d.get("true_heading"))),
        vrate_fpm=_int(d.get("baro_rate", d.get("geom_rate"))),
        squawk=str(d.get("squawk") or ""),
        emergency="" if d.get("emergency") in (None, "none") else str(d.get("emergency")),
        military=bool(db_flags & 1),
        seen_ts=now_s - (seen or 0),
        source=source,
        msg_age=msg_age,
        pos_age=seen,
    )


def parse_response(data: dict, source: str = "") -> list[Aircraft]:
    now_ms = data.get("now") or time.time() * 1000
    now_s = now_ms / 1000 if now_ms > 1e11 else now_ms  # some servers send seconds
    return [parse_aircraft(d, now_s, source) for d in data.get("ac") or data.get("aircraft") or []]


class AdsbClient:
    def __init__(self, sources=DEFAULT_SOURCES, gap_s: float = REQUEST_GAP_S):
        self.sources = [s for s in sources if s in SOURCES]
        self.gap_s = gap_s
        self.errors: list[str] = []
        self._last = 0.0
        self.on_progress = None  # optional callback(str) for status messages
        self.ok_requests = 0  # successful API answers since the last reset

    def _get(self, source: str, kind: str, arg: str = "") -> list[Aircraft]:
        conf = SOURCES[source]
        if self.on_progress:
            self.on_progress(f"Asking {source} for {'squawk ' + arg if kind == 'sqk' else kind + ' ' + arg}".strip())
        host = conf["api"].split("/")[2]
        if host in http.dead_hosts():  # don't even wait for the rate-limit gap
            raise http.FetchError(f"{host} skipped: it didn't respond a moment ago", network=True)
        wait = self.gap_s - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        found = parse_response(http.get_json(conf["api"] + conf["paths"][kind].format(arg)), source)
        self.ok_requests += 1
        return found

    def query(self, kind: str, arg: str = "") -> list[Aircraft]:
        """Ask each source in turn and merge by hex. The freshest report wins (the feeds can lag each other by
        a minute or more, and an old squawk is exactly what we must not show); older ones only fill gaps."""
        merged: dict[str, Aircraft] = {}
        for src in self.sources:
            if kind not in SOURCES[src]["paths"]:
                continue  # this feed doesn't offer that kind of query
            try:
                for ac in self._get(src, kind, arg):
                    old = merged.get(ac.hex)
                    if old is None:
                        merged[ac.hex] = ac
                    elif _fresher(ac, old):
                        _fill(ac, old)
                        merged[ac.hex] = ac
                    else:
                        _fill(old, ac)
            except http.FetchError as e:
                self.errors.append(f"{src} {kind} {arg}: {e}")
        return list(merged.values())

    def first(self, kind: str, arg: str) -> Aircraft | None:
        for src in self.sources:
            try:
                found = self._get(src, kind, arg)
            except http.FetchError as e:
                self.errors.append(f"{src} {kind} {arg}: {e}")
                continue
            if found:
                return found[0]
        return None

    MAX_MSG_AGE_S = 60  # ignore aircraft the feeds haven't heard from for this long

    def squawks(self, codes) -> list[Aircraft]:
        """Aircraft squawking these codes right now. A feed's /sqk list can include an aircraft that changed code
        a moment ago, or one it hasn't heard from in a while, so each result is checked against the freshest
        report from all the feeds."""
        out: dict[str, Aircraft] = {}
        for code in codes:
            for ac in self.query("sqk", code):
                if ac.squawk != code:
                    continue  # the freshest feed already has a different code
                if ac.msg_age is not None and ac.msg_age > self.MAX_MSG_AGE_S:
                    continue
                out.setdefault(ac.hex, ac)
        return list(out.values())

    EMERGENCY_KINDS = ("general", "lifeguard", "minfuel", "nordo", "unlawful", "downed")

    def emergencies(self, kinds=EMERGENCY_KINDS, military: list[Aircraft] | None = None) -> list[Aircraft]:
        """Military feed filtered to emergency status. (There's no global 'emergency' endpoint.)"""
        return [ac for ac in (self.query("mil") if military is None else military) if ac.emergency in kinds]

    def trace(self, hex_code: str, hours: float = 8.0) -> list[TrackPoint]:
        """Track history for the aircraft's current flight from the globe trace files."""
        h = hex_code.lower()
        for src in self.sources:
            globe = SOURCES[src].get("globe")
            if not globe:
                continue
            url = f"{globe}/data/traces/{h[-2:]}/trace_full_{h}.json"
            try:
                data = http.get_json(url, headers={"Referer": globe + "/"})
            except http.FetchError as e:
                self.errors.append(f"{src} trace {hex_code}: {e}")
                continue
            pts = parse_trace(data, hours)
            if pts:
                return pts
        return []

    def history(self, hex_code: str, since: float, start: int = 0) -> list[TrackPoint]:
        """Every position in the globe trace file newer than `since` (all legs, not just the current flight).
        `start` rotates which feed is asked first, to spread the load when downloading many aircraft."""
        h = hex_code.lower()
        globes = [s for s in self.sources if SOURCES[s].get("globe")]
        for src in globes[start % len(globes):] + globes[:start % len(globes)] if globes else []:
            globe = SOURCES[src]["globe"]
            try:
                data = http.get_json(f"{globe}/data/traces/{h[-2:]}/trace_full_{h}.json", headers={"Referer": globe + "/"})
            except http.FetchError:
                continue
            pts = [p for p in parse_trace_all(data) if p.ts >= since]
            if pts:
                return pts
        return []

    def route(self, callsign: str, lat: float, lon: float) -> dict | None:
        """Planned route (origin/destination) for a callsign, from adsb.lol's route database."""
        if not callsign:
            return None
        try:
            res = http.post_json(SOURCES["adsb.lol"]["route"], {"planes": [{"callsign": callsign, "lat": lat, "lng": lon}]})
        except http.FetchError as e:
            self.errors.append(f"route {callsign}: {e}")
            return None
        return parse_route(res)


def _fresher(a: Aircraft, b: Aircraft) -> bool:
    """Is report a newer than report b? (by time since the last message, then since the last position)"""
    def age(x):
        return x.msg_age if x.msg_age is not None else (x.pos_age if x.pos_age is not None else 1e9)

    return age(a) < age(b)


def _fill(a: Aircraft, b: Aircraft):
    # never copy squawk or emergency status from an older report: those must come from the freshest one
    for f in ("callsign", "registration", "type_code", "description", "operator"):
        if not getattr(a, f) and getattr(b, f):
            setattr(a, f, getattr(b, f))
    if a.lat is None and b.lat is not None:
        a.lat, a.lon = b.lat, b.lon


def parse_trace_all(data: dict) -> list[TrackPoint]:
    base = float(data.get("timestamp") or 0)
    out = []
    for row in data.get("trace") or []:
        if len(row) < 4 or row[1] is None or row[2] is None:
            continue
        alt = row[3]
        out.append(TrackPoint(base + float(row[0]), float(row[1]), float(row[2]), 0 if alt == "ground" else _int(alt),
                              _float(row[4]) if len(row) > 4 else None))
    return out


def parse_trace(data: dict, hours: float = 8.0) -> list[TrackPoint]:
    base = float(data.get("timestamp") or 0)
    rows = data.get("trace") or []
    pts: list[TrackPoint] = []
    for row in rows:
        if len(row) < 4 or row[1] is None or row[2] is None:
            continue
        ts = base + float(row[0])
        alt = row[3]
        alt_ft = 0 if alt == "ground" else _int(alt)
        flags = _int(row[6]) if len(row) > 6 else 0
        # new leg: flagged by readsb (bit 2) or a long gap
        if pts and ((flags or 0) & 2 or ts - pts[-1].ts > 30 * 60):
            pts = []
        pts.append(TrackPoint(ts, float(row[1]), float(row[2]), alt_ft, _float(row[4]) if len(row) > 4 else None))
    if pts:
        cutoff = pts[-1].ts - hours * 3600
        pts = [p for p in pts if p.ts >= cutoff]
        # drop the taxi/ground prefix so the line starts at take-off
        while len(pts) > 2 and pts[0].alt_ft == 0 and pts[1].alt_ft == 0:
            pts.pop(0)
    return pts


def parse_route(res) -> dict | None:
    if not isinstance(res, list) or not res:
        return None
    r = res[0]
    airports = r.get("_airports") or []
    if len(airports) < 2:
        return None

    def ap(a):
        return {k: a.get(k) for k in ("icao", "iata", "name", "location", "lat", "lon")}

    return {
        "codes": r.get("airport_codes") or r.get("_airport_codes_iata") or "",
        "origin": ap(airports[0]),
        "destination": ap(airports[-1]),
        "plausible": bool(r.get("plausible", True)),
    }
