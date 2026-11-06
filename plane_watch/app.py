"""The one-command app: watch everything, keep the cards fresh, show them in the browser.

    python planewatch.py

Every cycle (default 60 s) it:
- asks the ADS-B feeds for every special squawk (7500, 7600, 7700, 7400, 7777)
  and for military aircraft broadcasting an emergency,
- reads ACARS from a local decoder (JSON over UDP, port 5550 by default), from
  any --acars-file, and from airframes.io if AIRFRAMES_API_KEY is set,
- renders a card per flagged aircraft and an overview map,
- updates a live dashboard at http://localhost:8787 and the CSV/JSON history.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import threading
import time
import webbrowser
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path

from . import classify, pipeline, render
from .models import AcarsMessage, Alert, TrackPoint
from .sources import acars as acars_src
from .sources import photos as photo_src
from .sources.adsb import DEFAULT_SOURCES, AdsbClient

ALL_SQUAWKS = tuple(classify.SQUAWKS)  # 7500, 7600, 7700, 7400, 7777
FORGET_AFTER_S = 2 * 3600  # cleared aircraft stay in "recently cleared" this long


def _iso(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else ""


class AcarsListener:
    """Collects ACARS JSON that decoders push over UDP (acarsdec, dumpvdl2, dumphfdl, ACARS Hub, ...)."""

    def __init__(self, port: int, keep_s: float = 6 * 3600):
        self.port, self.keep_s = port, keep_s
        self.messages: deque[AcarsMessage] = deque(maxlen=20000)
        self.lock = threading.Lock()
        self.error = ""
        self.received = 0

    def start(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", self.port))
        except OSError as e:
            self.error = f"can't listen for ACARS on UDP {self.port}: {e}"
            return
        threading.Thread(target=self._loop, args=(sock,), daemon=True).start()

    def _loop(self, sock):
        while True:
            try:
                data, _ = sock.recvfrom(65535)
            except OSError:
                return
            for line in data.decode("utf-8", "replace").splitlines():
                try:
                    msg = acars_src.normalize(json.loads(line))
                except ValueError:
                    continue
                if msg:
                    with self.lock:
                        self.messages.append(msg)
                        self.received += 1

    def recent(self) -> list[AcarsMessage]:
        cutoff = time.time() - self.keep_s
        with self.lock:
            return [m for m in self.messages if m.ts >= cutoff]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()


def start_server(directory: Path, port: int) -> int:
    for p in range(port, port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), partial(QuietHandler, directory=str(directory)))
        except OSError:
            continue
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return p
    raise SystemExit(f"no free port between {port} and {port + 19}")


class LiveApp:
    def __init__(self, args):
        self.args = args
        self.out = Path(args.out).resolve()
        (self.out / "cards").mkdir(parents=True, exist_ok=True)
        (self.out / "photos").mkdir(exist_ok=True)
        (self.out / "data").mkdir(exist_ok=True)
        self.client = AdsbClient(args.sources.split(",") if args.sources else DEFAULT_SOURCES)
        self.listener = AcarsListener(args.udp_port, args.acars_hours * 3600) if args.udp_port else None
        self.known: dict[str, dict] = {}  # hex -> {"alert", "first_seen", "last_seen", "photo_bytes", "rendered_ts"}
        self.history = self._load("history.json", {})
        self.logged_msgs = set(self.history.get("_acars_logged", []))
        self.history.pop("_acars_logged", None)
        self.locate_cache: dict[str, object] = {}

    def _load(self, name, default):
        try:
            return json.loads((self.out / "data" / name).read_text())
        except (OSError, ValueError):
            return default

    def _write(self, name, obj):
        tmp = self.out / "data" / (name + ".tmp")
        tmp.write_text(json.dumps(obj, indent=1))
        tmp.replace(self.out / "data" / name)

    # ------------------------------------------------------------ gather

    def acars(self) -> list[AcarsMessage]:
        msgs = self.listener.recent() if self.listener else []
        since = time.time() - self.args.acars_hours * 3600
        for f in self.args.acars_file or []:
            try:
                msgs += acars_src.read_file(f, since)
            except OSError as e:
                self.client.errors.append(f"ACARS file {f}: {e}")
        if os.environ.get("AIRFRAMES_API_KEY"):
            try:
                msgs += acars_src.fetch_airframes(since_ts=since)
            except Exception as e:  # noqa: BLE001
                self.client.errors.append(f"airframes.io: {e}")
        return msgs

    def cycle(self) -> dict:
        self.client.errors = []
        started = time.time()
        msgs = self.acars()
        flagged = pipeline.flagged_messages(msgs)
        aircraft = {ac.hex: ac for ac in self.client.squawks(ALL_SQUAWKS)}
        for ac in self.client.emergencies():
            aircraft.setdefault(ac.hex, ac)

        # ACARS-only aircraft: look each one up once, not every cycle
        client = _CachedLocator(self.client, self.locate_cache)
        alerts = pipeline.build_alerts(list(aircraft.values()), flagged, client, ALL_SQUAWKS)

        now = time.time()
        current = []
        for alert in alerts:
            entry = self.known.get(alert.aircraft.hex)
            if entry:
                self._carry_over(entry["alert"], alert)
            else:
                pipeline.enrich(alert, self.client, fetch_photos=not self.args.no_photos, trace_hours=8)
                entry = {"first_seen": now, "photo_bytes": self._photo(alert), "rendered_ts": None, "sig": None}
            entry.update(alert=alert, last_seen=now)
            self.known[alert.aircraft.hex] = entry
            # redraw when the aircraft moved or the evidence changed
            sig = (alert.aircraft.seen_ts, tuple(sorted((f.category, f.origin, f.ts or 0) for f in alert.flags)),
                   (alert.divert_airport or {}).get("icao"))
            if alert.aircraft.lat is not None and entry["sig"] != sig:
                name = f"cards/{alert.aircraft.hex}.png"
                try:
                    render.render_card(alert, self.out / name, entry["photo_bytes"], title=self.args.title, brand=self.args.brand)
                    alert.card = name
                    entry["rendered_ts"], entry["sig"] = now, sig
                except Exception as e:  # noqa: BLE001 - one bad card shouldn't stop the app
                    self.client.errors.append(f"card for {alert.aircraft.label}: {e}")
            elif entry["rendered_ts"] is not None:
                alert.card = f"cards/{alert.aircraft.hex}.png"
            current.append(entry)

        live_hex = {a.aircraft.hex for a in alerts}
        cleared = [e for h, e in self.known.items() if h not in live_hex]
        for h in [h for h, e in self.known.items() if h not in live_hex and now - e["last_seen"] > FORGET_AFTER_S]:
            del self.known[h]

        render.render_overview(alerts, self.out / "cards" / "overview.png", title=self.args.title, brand=self.args.brand)
        self._record(alerts, flagged, now)
        status = {
            "updated_at": _iso(now),
            "updated_ts": now,
            "next_update_ts": started + self.args.interval,
            "interval_s": self.args.interval,
            "squawks": list(ALL_SQUAWKS),
            "sources": self.client.sources,
            "acars": {
                "messages": len(msgs), "flagged": len(flagged),
                "udp_port": self.args.udp_port if self.listener and not self.listener.error else None,
                "udp_received": self.listener.received if self.listener else 0,
                "airframes": bool(os.environ.get("AIRFRAMES_API_KEY")),
                "files": self.args.acars_file or [],
            },
            "errors": self.client.errors + ([self.listener.error] if self.listener and self.listener.error else []),
            "alerts": [self._summary(e, live=True) for e in sorted(current, key=lambda e: -classify.severity(e["alert"].top.category))],
            "cleared": [self._summary(e, live=False) for e in sorted(cleared, key=lambda e: -e["last_seen"])],
        }
        self._write("live.json", status)
        return status

    def _carry_over(self, old: Alert, new: Alert):
        """Reuse what we already fetched for this aircraft; extend its track."""
        track = old.aircraft.track
        ac = new.aircraft
        if ac.lat is not None and (not track or track[-1].ts < (ac.seen_ts or 0)):
            track.append(TrackPoint(ac.seen_ts or time.time(), ac.lat, ac.lon, ac.alt_ft))
        ac.track = track
        if ac.lat is None and track:
            ac.lat, ac.lon, ac.alt_ft = track[-1].lat, track[-1].lon, track[-1].alt_ft
        new.route, new.photo = old.route, old.photo
        # keep earlier ACARS evidence even after the message ages out
        seen = {(f.category, f.origin, f.ts) for f in new.flags}
        new.flags += [f for f in old.flags if (f.category, f.origin, f.ts) not in seen and f.origin in ("acars", "route")]
        mts = {m.ts for m in new.messages}
        new.messages += [m for m in old.messages if m.ts not in mts]
        new.divert_airport = old.divert_airport
        for f in new.flags:
            if f.divert_to and not new.divert_airport:
                from . import geo

                new.divert_airport = geo.airport(f.divert_to)
        pipeline.route_divert_check(new)

    def _photo(self, alert: Alert) -> bytes | None:
        path = self.out / "photos" / f"{alert.aircraft.hex}.jpg"
        if path.exists():
            return path.read_bytes()
        if alert.photo:
            data = photo_src.download(alert.photo["url"])
            if data:
                path.write_bytes(data)
                return data
        return None

    def _summary(self, entry: dict, live: bool) -> dict:
        d = entry["alert"].to_dict()
        d["live"] = live
        d["first_seen"] = _iso(entry["first_seen"])
        d["last_seen"] = _iso(entry["last_seen"])
        d["card_v"] = int(entry["rendered_ts"] or 0)
        return d

    def _record(self, alerts: list[Alert], flagged, now: float):
        """History for analysis: every alert ever seen, the CSV, and flagged ACARS."""
        new_ids = []
        for a in alerts:
            h = self.history.get(a.id)
            if h is None:
                new_ids.append(a)
                h = self.history[a.id] = {"first_seen": _iso(now), "category": a.top.category, "hex": a.aircraft.hex,
                                          "label": a.aircraft.label, "type": a.aircraft.description or a.aircraft.type_code}
            h.update(last_seen=_iso(now), categories=sorted({f.category for f in a.flags}),
                     squawk=a.aircraft.squawk, lat=a.aircraft.lat, lon=a.aircraft.lon,
                     divert_to=(a.divert_airport or {}).get("icao", ""), evidence=[f.evidence for f in a.flags][:6])
        if len(self.history) > 2000:
            for k in sorted(self.history, key=lambda k: self.history[k]["last_seen"])[: len(self.history) - 2000]:
                del self.history[k]
        if new_ids:
            pipeline._append_csv(self.out / "data" / "alerts.csv", new_ids, "live", _iso(now))
        with (self.out / "data" / "acars_flagged.jsonl").open("a") as fh:
            for m, flags in flagged:
                key = f"{m.ts}|{m.registration}|{m.flight}|{hashlib.sha1(m.text.encode()).hexdigest()[:12]}"
                if key in self.logged_msgs:
                    continue
                self.logged_msgs.add(key)
                row = {k: v for k, v in asdict(m).items() if k != "raw"}
                row["categories"] = [f.category for f in flags]
                fh.write(json.dumps(row) + "\n")
        self._write("history.json", {**self.history, "_acars_logged": list(self.logged_msgs)[-5000:]})

    # ------------------------------------------------------------ run

    def run(self):
        web = files(__package__).joinpath("web", "index.html").read_text(encoding="utf-8")
        (self.out / "index.html").write_text(web, encoding="utf-8")
        port = start_server(self.out, self.args.port)
        url = f"http://localhost:{port}/"
        if self.listener:
            self.listener.start()
        print(f"\n  PLANE WATCH  ·  dashboard: {url}")
        print(f"  watching squawks {', '.join(ALL_SQUAWKS)} + military emergencies + ACARS threats")
        if self.listener and not self.listener.error:
            print(f"  ACARS: listening for decoder JSON on UDP port {self.args.udp_port}")
        print(f"  saving to {self.out}   (Ctrl+C to stop)\n")
        if self.args.once:
            self._report(self.cycle())
            return 0
        if not self.args.no_browser:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        while True:
            t0 = time.time()
            try:
                self._report(self.cycle())
            except Exception as e:  # noqa: BLE001 - keep the app alive through network hiccups
                print(f"  ! cycle failed: {e}", file=sys.stderr)
            time.sleep(max(5, self.args.interval - (time.time() - t0)))

    @staticmethod
    def _report(status: dict):
        stamp = datetime.now().strftime("%H:%M:%S")
        alerts = status["alerts"]
        if not alerts:
            print(f"  {stamp}  all quiet: nothing squawking 7x00 or flagged in ACARS")
        else:
            print(f"  {stamp}  {len(alerts)} flagged:")
            for a in alerts:
                ac = a["aircraft"]
                print(f"           {a['category_label']:<30} {ac['callsign'] or ac['hex']:<9} {ac['description'] or ac['type_code']}")
        failed = len(status["errors"])
        if failed:
            print(f"           ({failed} source warning{'s' if failed > 1 else ''}; see the dashboard)")


class _CachedLocator:
    """Wraps AdsbClient.first so ACARS-only aircraft are looked up once per session (per key)."""

    def __init__(self, client: AdsbClient, cache: dict):
        self.client, self.cache = client, cache

    def first(self, kind, value):
        key = f"{kind}:{value}"
        if key not in self.cache:
            self.cache[key] = self.client.first(kind, value)
        return self.cache[key]


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="planewatch", description="Watch for aircraft in trouble (every emergency squawk + ACARS threats) and show live cards.",
        epilog="Other commands: demo (sample cards, offline), scan (one-off scan), render --hex X (card for any aircraft), "
               "classify TEXT (test the ACARS rules). Add --help after any of them.")
    p.add_argument("--out", default="planewatch-data", help="where cards and data are saved (default: ./planewatch-data)")
    p.add_argument("--interval", type=float, default=60, help="seconds between checks (default 60)")
    p.add_argument("--port", type=int, default=8787, help="dashboard port (default 8787)")
    p.add_argument("--udp-port", type=int, default=5550, help="UDP port for ACARS decoder JSON (0 = off; default 5550)")
    p.add_argument("--acars-file", action="append", help="also read ACARS JSON lines from this file (repeatable)")
    p.add_argument("--acars-hours", type=float, default=6, help="ignore ACARS older than this (default 6)")
    p.add_argument("--sources", default=",".join(DEFAULT_SOURCES), help="ADS-B feeds to use")
    p.add_argument("--no-browser", action="store_true", help="don't open the dashboard automatically")
    p.add_argument("--no-photos", action="store_true")
    p.add_argument("--once", action="store_true", help="check once and exit (no dashboard loop)")
    p.add_argument("--title", default="PLANE ALERT")
    p.add_argument("--brand", default="plane watch")
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        return LiveApp(args).run() or 0
    except KeyboardInterrupt:
        print("\n  stopped.")
        return 0
