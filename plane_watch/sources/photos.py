"""Aircraft photos from planespotters.net's public API (credit the photographer)."""

from __future__ import annotations

from .. import http

API = "https://api.planespotters.net/pub/photos"


def find_photo(hex_code: str = "", registration: str = "") -> dict | None:
    """{"url", "photographer", "link", "same_type": False} or None."""
    for kind, value in (("hex", hex_code), ("reg", registration)):
        if not value:
            continue
        try:
            data = http.get_json(f"{API}/{kind}/{value}")
        except http.FetchError:
            continue
        photos = data.get("photos") or []
        if photos:
            p = photos[0]
            thumb = p.get("thumbnail_large") or p.get("thumbnail") or {}
            if thumb.get("src"):
                return {"url": thumb["src"], "photographer": p.get("photographer", ""), "link": p.get("link", ""), "same_type": False}
    return None


def download(url: str) -> bytes | None:
    try:
        return http.get_bytes(url, headers={"Accept": "image/*"})
    except http.FetchError:
        return None
