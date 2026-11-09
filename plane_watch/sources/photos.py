"""Aircraft photos from planespotters.net's public API (credit the photographer)."""

from __future__ import annotations

from .. import http

API = "https://api.planespotters.net/pub/photos"


def _matches(photo: dict, registration: str) -> bool:
    """planespotters photo links end in the registration (".../photo/123/hb-jcl-swiss-..."). ICAO addresses and
    test registrations get reused, so a photo found by address can show a different aircraft: check it."""
    if not registration:
        return True
    link = (photo.get("link") or "").lower()
    if not link:
        return True
    return registration.lower().replace(" ", "") in link


def find_photo(hex_code: str = "", registration: str = "") -> dict | None:
    """{"url", "photographer", "link", "same_type": False} or None. Only returns a photo of this registration."""
    for kind, value in (("hex", hex_code), ("reg", registration)):
        if not value:
            continue
        try:
            data = http.get_json(f"{API}/{kind}/{value}")
        except http.FetchError:
            continue
        for p in data.get("photos") or []:
            thumb = p.get("thumbnail_large") or p.get("thumbnail") or {}
            if thumb.get("src") and _matches(p, registration):
                return {"url": thumb["src"], "photographer": p.get("photographer", ""), "link": p.get("link", ""), "same_type": False}
    return None


def download(url: str) -> bytes | None:
    try:
        return http.get_bytes(url, headers={"Accept": "image/*"})
    except http.FetchError:
        return None
