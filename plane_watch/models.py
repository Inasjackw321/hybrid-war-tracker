"""Plain data classes shared by the sources, the classifier and the renderer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


@dataclass
class TrackPoint:
    ts: float  # unix seconds
    lat: float
    lon: float
    alt_ft: int | None = None  # None = unknown, 0 = on the ground


@dataclass
class Aircraft:
    hex: str  # ICAO 24-bit address, upper case
    callsign: str = ""
    registration: str = ""
    type_code: str = ""  # ICAO type designator, e.g. B738
    description: str = ""  # e.g. "BOEING 737-800"
    operator: str = ""
    lat: float | None = None
    lon: float | None = None
    alt_ft: int | None = None
    on_ground: bool = False
    speed_kt: float | None = None
    heading: float | None = None
    vrate_fpm: int | None = None
    squawk: str = ""
    emergency: str = ""  # ADS-B emergency status: general, lifeguard, minfuel, nordo, unlawful, downed
    military: bool = False
    seen_ts: float | None = None
    source: str = ""
    track: list[TrackPoint] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.callsign or self.registration or self.hex


@dataclass
class AcarsMessage:
    ts: float
    text: str
    registration: str = ""
    flight: str = ""
    hex: str = ""
    label: str = ""  # ACARS message label, e.g. H1, 5Z
    source: str = ""  # acarsdec, dumpvdl2, dumphfdl, airframes, ...
    frequency: str = ""
    raw: dict | None = None


@dataclass
class Flag:
    """One reason an aircraft is interesting."""

    category: str  # key into classify.CATEGORIES
    evidence: str  # what triggered it (squawk, ACARS excerpt, ...)
    origin: str  # "squawk", "adsb-emergency", "acars", "route"
    ts: float | None = None
    matched: str = ""  # the exact matched words, for highlighting
    divert_to: str = ""  # airport code if a diversion target was found


@dataclass
class Alert:
    aircraft: Aircraft
    flags: list[Flag]
    messages: list[AcarsMessage] = field(default_factory=list)
    divert_airport: dict | None = None  # airportsdata record
    route: dict | None = None  # {"origin": {...}, "destination": {...}} if known
    photo: dict | None = None  # {"url", "photographer", "link", "same_type"}
    card: str = ""  # path of the rendered PNG, relative to the output dir

    @property
    def id(self) -> str:
        day = datetime.fromtimestamp(self.aircraft.seen_ts or 0, timezone.utc).strftime("%Y%m%d")
        return f"{day}-{self.aircraft.hex}-{self.top.category}"

    @property
    def top(self) -> Flag:
        from .classify import severity

        return max(self.flags, key=lambda f: severity(f.category))

    def to_dict(self) -> dict:
        from .classify import CATEGORIES

        ac = asdict(self.aircraft)
        ac.pop("track")
        return {
            "id": self.id,
            "category": self.top.category,
            "category_label": CATEGORIES[self.top.category]["label"],
            "severity": CATEGORIES[self.top.category]["severity"],
            "aircraft": ac,
            "flags": [asdict(f) for f in self.flags],
            "acars": [{k: v for k, v in asdict(m).items() if k != "raw"} for m in self.messages],
            "divert_airport": self.divert_airport,
            "route": self.route,
            "photo": self.photo,
            "track_points": len(self.aircraft.track),
            "card": self.card,
        }
