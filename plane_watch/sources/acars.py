"""ACARS messages from your own receiver or from airframes.io.

Supported inputs:
- JSON lines written by acarsdec (-j / --output json:file), dumpvdl2
  (--output decoded:json:file), dumphfdl (same), vdlm2dec (-j), or ACARS Hub
  (which relays those decoders' JSON). Mixed formats in one file are fine.
- A JSON array of such objects.
- The airframes.io REST API (needs an API key: AIRFRAMES_API_KEY).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .. import http
from ..models import AcarsMessage

AIRFRAMES_URL = os.environ.get("AIRFRAMES_API_URL", "https://api.airframes.io/v1/messages")


def _first(d: dict, *keys, default=""):
    for k in keys:
        v = d.get(k) if isinstance(d, dict) else None
        if v not in (None, ""):
            return v
    return default


def _ts(v) -> float:
    if v in (None, ""):
        return time.time()
    if isinstance(v, (int, float)):
        return float(v) / 1000 if v > 1e11 else float(v)
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def _clean_reg(r) -> str:
    return str(r or "").strip().lstrip(".").upper()


def normalize(obj: dict) -> AcarsMessage | None:
    """Turn one decoder / API JSON object into an AcarsMessage (None if it has no text)."""
    if not isinstance(obj, dict):
        return None

    # dumpvdl2: {"vdl2": {"t": {"sec"}, "freq", "avlc": {"src": {"addr", "type"}, "acars": {...}}}}
    if "vdl2" in obj:
        v = obj["vdl2"]
        avlc = v.get("avlc") or {}
        ac = avlc.get("acars") or {}
        src = avlc.get("src") or {}
        text = _first(ac, "msg_text")
        if not text:
            return None
        return AcarsMessage(
            ts=_ts((v.get("t") or {}).get("sec")), text=text, registration=_clean_reg(ac.get("reg")),
            flight=str(ac.get("flight") or "").strip(), label=str(ac.get("label") or ""),
            hex=str(src.get("addr") or "").upper() if src.get("type") == "Aircraft" else "",
            source="dumpvdl2", frequency=str(v.get("freq") or ""), raw=obj,
        )

    # dumphfdl: {"hfdl": {"t": {"sec"}, "freq", "lpdu": {"ac_info": {"icao"}, "hfnpdu": {"acars": {...}}}}}
    if "hfdl" in obj:
        h = obj["hfdl"]
        lpdu = h.get("lpdu") or {}
        ac = ((lpdu.get("hfnpdu") or {}).get("acars")) or {}
        text = _first(ac, "msg_text")
        if not text:
            return None
        return AcarsMessage(
            ts=_ts((h.get("t") or {}).get("sec")), text=text, registration=_clean_reg(ac.get("reg")),
            flight=str(ac.get("flight") or "").strip(), label=str(ac.get("label") or ""),
            hex=str((lpdu.get("ac_info") or {}).get("icao") or "").upper(),
            source="dumphfdl", frequency=str(h.get("freq") or ""), raw=obj,
        )

    # airframes.io: {"text", "timestamp", "tail", "flightNumber"/"flight", "airframe": {"icao", "tail"}, ...}
    if "airframe" in obj or "flightNumber" in obj:
        af = obj.get("airframe") or {}
        fl = obj.get("flight") if isinstance(obj.get("flight"), dict) else {}
        text = _first(obj, "text", "data")
        if not text:
            return None
        return AcarsMessage(
            ts=_ts(_first(obj, "timestamp", "createdAt", default=None)), text=text,
            registration=_clean_reg(_first(obj, "tail") or af.get("tail")),
            flight=str(_first(obj, "flightNumber") or fl.get("flight") or "").strip(),
            hex=str(af.get("icao") or "").upper(), label=str(obj.get("label") or ""),
            source="airframes", frequency=str(_first(obj, "frequency")), raw=obj,
        )

    # acarsdec / vdlm2dec / generic: {"timestamp", "tail", "flight", "text", "label", "freq", "icao"}
    text = _first(obj, "text", "msg_text", "message")
    if not text:
        return None
    hex_code = obj.get("icao")
    if isinstance(hex_code, int):
        hex_code = f"{hex_code:06X}"
    app = obj.get("app") if isinstance(obj.get("app"), dict) else {}
    source = app.get("name") or ("acarsdec" if "station_id" in obj or "channel" in obj else "acars")
    return AcarsMessage(
        ts=_ts(_first(obj, "timestamp", "time", default=None)), text=str(text),
        registration=_clean_reg(_first(obj, "tail", "reg", "registration")),
        flight=str(_first(obj, "flight", "flight_number")).strip(), hex=str(hex_code or "").upper(),
        label=str(obj.get("label") or ""), source=str(source),
        frequency=str(_first(obj, "freq", "frequency")), raw=obj,
    )


def read_file(path: str | Path, since_ts: float = 0) -> list[AcarsMessage]:
    """Read JSON lines (or one JSON array) of decoder output."""
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    objs: list = []
    stripped = raw.lstrip()
    if stripped.startswith("["):
        objs = json.loads(stripped)
    else:
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                objs.append(json.loads(line))
            except ValueError:
                continue  # half-written last line, banner text, ...
    msgs = [m for m in (normalize(o) for o in objs) if m]
    return [m for m in msgs if m.ts >= since_ts]


def fetch_airframes(api_key: str | None = None, since_ts: float = 0, limit: int = 500, query: str = "") -> list[AcarsMessage]:
    """Recent messages from airframes.io. The endpoint and parameters can be
    overridden with AIRFRAMES_API_URL if airframes changes its API."""
    key = api_key or os.environ.get("AIRFRAMES_API_KEY")
    if not key:
        raise http.FetchError("AIRFRAMES_API_KEY is not set")
    params = f"?limit={limit}"
    if query:
        from urllib.parse import quote

        params += f"&text={quote(query)}"
    try:
        data = http.get_json(AIRFRAMES_URL + params, headers={"Authorization": f"Bearer {key}"})  # per docs.airframes.io
    except http.FetchError as e:
        if "HTTP 401" in str(e) or "HTTP 403" in str(e):
            raise http.FetchError("airframes.io rejected the API key (check it, or ask api@airframes.io)") from None
        if "HTTP 429" in str(e):
            raise http.FetchError("airframes.io rate limit reached; it'll retry next check") from None
        raise
    items = data if isinstance(data, list) else (data.get("messages") or data.get("data") or data.get("results") or [])
    msgs = [m for m in (normalize(o) for o in items) if m]
    return [m for m in msgs if m.ts >= since_ts]
