"""Geography helpers: ICAO address -> country, airports, distances, projection."""

from __future__ import annotations

import math
from functools import lru_cache

# ICAO 24-bit address blocks (Annex 10 Vol III). Not exhaustive: covers the
# states you are most likely to see. (start, end, ISO code, name)
_BLOCKS = [
    (0x008000, 0x00FFFF, "ZA", "South Africa"), (0x010000, 0x017FFF, "EG", "Egypt"),
    (0x018000, 0x01FFFF, "LY", "Libya"), (0x020000, 0x027FFF, "MA", "Morocco"),
    (0x028000, 0x02FFFF, "TN", "Tunisia"), (0x040000, 0x040FFF, "ET", "Ethiopia"),
    (0x04C000, 0x04CFFF, "KE", "Kenya"), (0x05A000, 0x05A3FF, "MV", "Maldives"),
    (0x064000, 0x064FFF, "NG", "Nigeria"), (0x06A000, 0x06A3FF, "QA", "Qatar"),
    (0x07C000, 0x07CFFF, "SD", "Sudan"), (0x0A0000, 0x0A7FFF, "DZ", "Algeria"),
    (0x0AC000, 0x0ACFFF, "CO", "Colombia"), (0x0B0000, 0x0B0FFF, "CU", "Cuba"),
    (0x0D0000, 0x0D7FFF, "MX", "Mexico"), (0x0D8000, 0x0DFFFF, "VE", "Venezuela"),
    (0x100000, 0x1FFFFF, "RU", "Russia"), (0x300000, 0x33FFFF, "IT", "Italy"),
    (0x340000, 0x37FFFF, "ES", "Spain"), (0x380000, 0x3BFFFF, "FR", "France"),
    (0x3C0000, 0x3FFFFF, "DE", "Germany"), (0x400000, 0x43FFFF, "GB", "United Kingdom"),
    (0x440000, 0x447FFF, "AT", "Austria"), (0x448000, 0x44FFFF, "BE", "Belgium"),
    (0x450000, 0x457FFF, "BG", "Bulgaria"), (0x458000, 0x45FFFF, "DK", "Denmark"),
    (0x460000, 0x467FFF, "FI", "Finland"), (0x468000, 0x46FFFF, "GR", "Greece"),
    (0x470000, 0x477FFF, "HU", "Hungary"), (0x478000, 0x47FFFF, "NO", "Norway"),
    (0x480000, 0x487FFF, "NL", "Netherlands"), (0x488000, 0x48FFFF, "PL", "Poland"),
    (0x490000, 0x497FFF, "PT", "Portugal"), (0x498000, 0x49FFFF, "CZ", "Czechia"),
    (0x4A0000, 0x4A7FFF, "RO", "Romania"), (0x4A8000, 0x4AFFFF, "SE", "Sweden"),
    (0x4B0000, 0x4B7FFF, "CH", "Switzerland"), (0x4B8000, 0x4BFFFF, "TR", "Turkey"),
    (0x4C0000, 0x4C7FFF, "RS", "Serbia"), (0x4C8000, 0x4C83FF, "CY", "Cyprus"),
    (0x4CA000, 0x4CAFFF, "IE", "Ireland"), (0x4CC000, 0x4CCFFF, "IS", "Iceland"),
    (0x4D0000, 0x4D03FF, "LU", "Luxembourg"), (0x4D2000, 0x4D23FF, "MT", "Malta"),
    (0x508000, 0x50FFFF, "UA", "Ukraine"), (0x510000, 0x5103FF, "BY", "Belarus"),
    (0x514000, 0x5143FF, "GE", "Georgia"), (0x600000, 0x6003FF, "AM", "Armenia"),
    (0x600800, 0x600BFF, "AZ", "Azerbaijan"), (0x683000, 0x6833FF, "KZ", "Kazakhstan"),
    (0x700000, 0x700FFF, "AF", "Afghanistan"), (0x702000, 0x702FFF, "BD", "Bangladesh"),
    (0x704000, 0x704FFF, "MM", "Myanmar"), (0x706000, 0x706FFF, "KW", "Kuwait"),
    (0x70A000, 0x70AFFF, "NP", "Nepal"), (0x70C000, 0x70C3FF, "OM", "Oman"),
    (0x710000, 0x717FFF, "SA", "Saudi Arabia"), (0x718000, 0x71FFFF, "KR", "South Korea"),
    (0x720000, 0x727FFF, "KP", "North Korea"), (0x728000, 0x72FFFF, "IQ", "Iraq"),
    (0x730000, 0x737FFF, "IR", "Iran"), (0x738000, 0x73FFFF, "IL", "Israel"),
    (0x740000, 0x747FFF, "JO", "Jordan"), (0x748000, 0x74FFFF, "LB", "Lebanon"),
    (0x750000, 0x757FFF, "MY", "Malaysia"), (0x758000, 0x75FFFF, "PH", "Philippines"),
    (0x760000, 0x767FFF, "PK", "Pakistan"), (0x768000, 0x76FFFF, "SG", "Singapore"),
    (0x770000, 0x777FFF, "LK", "Sri Lanka"), (0x778000, 0x77FFFF, "SY", "Syria"),
    (0x780000, 0x7BFFFF, "CN", "China"), (0x7C0000, 0x7FFFFF, "AU", "Australia"),
    (0x800000, 0x83FFFF, "IN", "India"), (0x840000, 0x87FFFF, "JP", "Japan"),
    (0x880000, 0x887FFF, "TH", "Thailand"), (0x888000, 0x88FFFF, "VN", "Vietnam"),
    (0x890000, 0x890FFF, "YE", "Yemen"), (0x894000, 0x894FFF, "BH", "Bahrain"),
    (0x896000, 0x896FFF, "AE", "United Arab Emirates"), (0x899000, 0x8993FF, "TW", "Taiwan"),
    (0x8A0000, 0x8A7FFF, "ID", "Indonesia"), (0xA00000, 0xAFFFFF, "US", "United States"),
    (0xC00000, 0xC3FFFF, "CA", "Canada"), (0xC80000, 0xC87FFF, "NZ", "New Zealand"),
    (0xE00000, 0xE3FFFF, "AR", "Argentina"), (0xE40000, 0xE7FFFF, "BR", "Brazil"),
    (0xE80000, 0xE80FFF, "CL", "Chile"), (0xE84000, 0xE84FFF, "PE", "Peru"),
]


def country_for_hex(hex_code: str) -> tuple[str, str]:
    """('US', 'United States') or ('', '') if unknown / non-ICAO (~ prefixed) address."""
    try:
        n = int(hex_code.strip(), 16)  # "~abc123" (TIS-B/non-ICAO) fails here on purpose
    except ValueError:
        return "", ""
    for start, end, code, name in _BLOCKS:
        if start <= n <= end:
            return code, name
    return "", ""


@lru_cache(maxsize=1)
def _airports() -> tuple[dict, dict]:
    try:
        import airportsdata
    except ImportError:  # optional dependency
        return {}, {}
    icao = airportsdata.load("ICAO")
    iata = {a["iata"]: a for a in icao.values() if a.get("iata")}
    return icao, iata


def airport(code: str) -> dict | None:
    """Look up an airport by 4-letter ICAO or 3-letter IATA code."""
    code = (code or "").upper().strip()
    icao, iata = _airports()
    if len(code) == 4:
        return icao.get(code)
    if len(code) == 3:
        return iata.get(code)
    return None


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


def cross_track_km(lat, lon, lat1, lon1, lat2, lon2) -> float:
    """Distance of a point from the great circle through two points."""
    d13 = haversine_km(lat1, lon1, lat, lon) / 6371.0
    b13 = _bearing(lat1, lon1, lat, lon)
    b12 = _bearing(lat1, lon1, lat2, lon2)
    return abs(math.asin(math.sin(d13) * math.sin(b13 - b12)) * 6371.0)


def _bearing(lat1, lon1, lat2, lon2) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    return math.atan2(math.sin(dl) * math.cos(p2), math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))


# Web-Mercator style y (unitless). x is plain longitude in radians.
def merc_y(lat: float) -> float:
    lat = max(-85.0, min(85.0, lat))
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def inv_merc_y(y: float) -> float:
    return math.degrees(2 * math.atan(math.exp(y)) - math.pi / 2)


def project(lon: float, lat: float) -> tuple[float, float]:
    return math.radians(lon), merc_y(lat)
