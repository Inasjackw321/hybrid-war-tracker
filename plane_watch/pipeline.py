"""Glue: gather ADS-B + ACARS evidence, build alerts, enrich, render, save."""

from __future__ import annotations

import csv
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from . import classify, geo
from .models import AcarsMessage, Aircraft, Alert, Flag, TrackPoint
from .sources import photos as photo_src
from .sources.adsb import AdsbClient

ROUTE_OFF_KM = 300  # this far off the planned great circle counts as a possible diversion


def _key_match(ac: Aircraft, msg: AcarsMessage) -> bool:
    if msg.hex and msg.hex == ac.hex:
        return True
    if msg.registration and ac.registration and msg.registration.replace("-", "") == ac.registration.replace("-", ""):
        return True
    return bool(msg.flight and ac.callsign and _norm_flight(msg.flight) == _norm_flight(ac.callsign))


def _norm_flight(f: str) -> str:
    """'BA0117' / 'BAW117' style differences can't be fixed here; just strip padding zeros and spaces."""
    f = f.replace(" ", "").upper()
    head = "".join(ch for ch in f if not ch.isdigit())
    num = f[len(head):].lstrip("0")
    return head + num


def flagged_messages(msgs: list[AcarsMessage]) -> list[tuple[AcarsMessage, list[Flag]]]:
    out = []
    for m in msgs:
        flags = classify.classify_message(m, airport_lookup=geo.airport)
        if flags:
            out.append((m, flags))
    return out


def build_alerts(squawk_aircraft: list[Aircraft], acars: list[tuple[AcarsMessage, list[Flag]]],
                 client: AdsbClient | None = None, squawks=classify.DEFAULT_SQUAWKS,
                 watch: list[Aircraft] | None = None) -> list[Alert]:
    alerts: dict[str, Alert] = {}
    for ac in squawk_aircraft:
        flags = classify.classify_aircraft(ac, squawks)
        if flags:
            alerts[ac.hex] = Alert(ac, flags)

    for msg, flags in acars:
        alert = next((a for a in alerts.values() if _key_match(a.aircraft, msg)), None)
        if alert is None:
            ac = _locate(msg, client)
            alert = alerts.get(ac.hex) or Alert(ac, [])
            alerts[ac.hex] = alert
        alert.flags.extend(flags)
        alert.messages.append(msg)

    for ac in watch or []:
        if ac.hex not in alerts:
            alerts[ac.hex] = Alert(ac, [Flag("watch", "Requested aircraft", "watch", ac.seen_ts)])
    return list(alerts.values())


def _locate(msg: AcarsMessage, client: AdsbClient | None) -> Aircraft:
    """Find the live ADS-B record for an ACARS message's aircraft."""
    if client:
        for kind, value in (("hex", msg.hex), ("reg", msg.registration), ("callsign", msg.flight)):
            if value:
                found = client.first(kind, value)
                if found:
                    return found
    ident = msg.registration or msg.flight or f"msg{int(msg.ts)}"
    return Aircraft(hex=msg.hex or f"~{ident}", callsign=msg.flight,
                    registration=msg.registration, seen_ts=msg.ts, source="ACARS only")


def enrich(alert: Alert, client: AdsbClient | None, history: dict | None = None, fetch_photos: bool = True,
           trace_hours: float = 8.0):
    ac = alert.aircraft
    # track: globe trace file, else what earlier polls/runs recorded
    if client and not ac.track and not ac.hex.startswith("~"):
        ac.track = client.trace(ac.hex, trace_hours)
    if not ac.track and history and ac.hex in history:
        ac.track = [TrackPoint(**p) for p in history[ac.hex]]
    if ac.lat is not None and (not ac.track or ac.track[-1].ts < (ac.seen_ts or 0)):
        ac.track.append(TrackPoint(ac.seen_ts or time.time(), ac.lat, ac.lon, ac.alt_ft, ac.speed_kt))
    if ac.lat is None and ac.track:
        last = ac.track[-1]
        ac.lat, ac.lon, ac.alt_ft = last.lat, last.lon, last.alt_ft

    # diversion target named in ACARS
    for f in alert.flags:
        if f.divert_to and not alert.divert_airport:
            alert.divert_airport = geo.airport(f.divert_to)

    # planned route, and whether we're far off it
    if client and ac.callsign and ac.lat is not None:
        alert.route = client.route(ac.callsign, ac.lat, ac.lon)
    route_divert_check(alert)

    if fetch_photos and not ac.hex.startswith("~"):
        alert.photo = photo_src.find_photo(ac.hex, ac.registration)


def route_divert_check(alert: Alert):
    ac, r = alert.aircraft, alert.route
    if not r or not r.get("plausible", True) or ac.lat is None:
        return
    o, d = r.get("origin") or {}, r.get("destination") or {}
    if None in (o.get("lat"), d.get("lat")):
        return
    off = geo.cross_track_km(ac.lat, ac.lon, o["lat"], o["lon"], d["lat"], d["lon"])
    if off > ROUTE_OFF_KM and not any(f.category == "diversion" for f in alert.flags):
        alert.flags.append(Flag("diversion", f"{off:,.0f} km off the planned route {r.get('codes', '')} (possible diversion)",
                                "route", ac.seen_ts, matched=r.get("codes", "")))


# ------------------------------------------------------------------ output


def save_run(alerts: list[Alert], out: Path, run_id: str, meta: dict) -> dict:
    data_dir = out / "data"
    (data_dir / "runs").mkdir(parents=True, exist_ok=True)
    now = time.time()
    report = {
        "id": run_id,
        "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
        **meta,
        "alerts": [a.to_dict() for a in sorted(alerts, key=lambda a: -classify.severity(a.top.category))],
    }
    (data_dir / "runs" / f"{run_id}.json").write_text(json.dumps(report, indent=1), encoding="utf-8")

    index_path = data_dir / "index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {"runs": []}
    index["runs"] = [r for r in index["runs"] if r["id"] != run_id]
    counts: dict[str, int] = {}
    for a in alerts:
        counts[a.top.category] = counts.get(a.top.category, 0) + 1
    index["runs"].insert(0, {"id": run_id, "generated_at": report["generated_at"], "alerts": len(alerts), "categories": counts,
                             "overview": meta.get("overview", "")})
    index["runs"] = index["runs"][:500]
    index_path.write_text(json.dumps(index, indent=1), encoding="utf-8")

    _append_csv(data_dir / "alerts.csv", alerts, run_id, report["generated_at"])
    with (data_dir / "acars_flagged.jsonl").open("a", encoding="utf-8") as fh:
        for a in alerts:
            for m in a.messages:
                row = {k: v for k, v in asdict(m).items() if k != "raw"}
                row["run"] = run_id
                row["categories"] = [f.category for f in a.flags if f.origin == "acars" and f.ts == m.ts]
                fh.write(json.dumps(row) + "\n")
    return report


CSV_FIELDS = ["run", "generated_at", "alert_id", "category", "severity", "all_categories", "hex", "callsign", "registration",
              "type", "description", "operator", "country", "military", "squawk", "emergency", "lat", "lon", "alt_ft",
              "speed_kt", "heading", "seen_utc", "divert_to", "route", "acars_messages", "evidence", "card"]


def _append_csv(path: Path, alerts: list[Alert], run_id: str, generated_at: str):
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        if new:
            w.writeheader()
        for a in alerts:
            ac = a.aircraft
            w.writerow({
                "run": run_id, "generated_at": generated_at, "alert_id": a.id, "category": a.top.category,
                "severity": classify.severity(a.top.category),
                "all_categories": "|".join(sorted({f.category for f in a.flags})),
                "hex": ac.hex, "callsign": ac.callsign, "registration": ac.registration, "type": ac.type_code,
                "description": ac.description, "operator": ac.operator, "country": geo.country_for_hex(ac.hex)[1],
                "military": ac.military, "squawk": ac.squawk, "emergency": ac.emergency, "lat": ac.lat, "lon": ac.lon,
                "alt_ft": ac.alt_ft, "speed_kt": ac.speed_kt, "heading": ac.heading,
                "seen_utc": datetime.fromtimestamp(ac.seen_ts, timezone.utc).isoformat(timespec="seconds") if ac.seen_ts else "",
                "divert_to": (a.divert_airport or {}).get("icao", ""), "route": (a.route or {}).get("codes", ""),
                "acars_messages": len(a.messages), "evidence": " | ".join(f.evidence for f in a.flags), "card": a.card,
            })


def load_history(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_history(path: Path, history: dict, alerts: list[Alert], keep_hours: float = 12):
    cutoff = time.time() - keep_hours * 3600
    for a in alerts:
        if a.aircraft.track and not a.aircraft.hex.startswith("~"):
            history[a.aircraft.hex] = [asdict(p) for p in a.aircraft.track]
    history = {h: pts for h, pts in history.items() if pts and pts[-1]["ts"] >= cutoff}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history, separators=(",", ":")))


def prune_cards(out: Path, keep_runs: int = 60):
    """Delete card images that none of the newest `keep_runs` runs use (keeps the repo small)."""
    index_path = out / "data" / "index.json"
    cards = out / "cards"
    if not index_path.exists() or not cards.exists():
        return 0
    keep = set()
    for run in json.loads(index_path.read_text())["runs"][:keep_runs]:
        keep.add(run.get("overview", ""))
        try:
            report = json.loads((out / "data" / "runs" / f"{run['id']}.json").read_text())
        except (OSError, ValueError):
            continue
        keep.update(a.get("card", "") for a in report.get("alerts", []))
    removed = 0
    for png in cards.glob("*.png"):
        if f"cards/{png.name}" not in keep:
            png.unlink()
            removed += 1
    return removed
