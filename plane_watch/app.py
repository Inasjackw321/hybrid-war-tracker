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

DEFAULT_SQUAWKS = classify.DEFAULT_SQUAWKS  # 7500 hijack, 7600 radio failure, 7700 emergency
CONTROL_HEADER = "X-PlaneWatch"  # other websites can't send this header without a CORS preflight we never answer
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
    """Serves the dashboard files, plus a tiny control API for the dashboard buttons."""

    app = None  # the LiveApp, set by start_server

    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/ping"):
            return self._json(200, {"app": "planewatch", "pid": os.getpid(), "paused": bool(self.app and self.app.paused.is_set())})
        return super().do_GET()

    def do_POST(self):
        if not self.path.startswith("/api/") or self.app is None:
            return self._json(404, {"error": "not found"})
        if self.headers.get(CONTROL_HEADER) != "1":
            return self._json(403, {"error": "missing header"})
        action = self.path[len("/api/"):].split("?")[0]
        if action not in ("pause", "resume", "check", "stop"):
            return self._json(404, {"error": "unknown action"})
        self.app.control(action)
        return self._json(200, {"ok": True, "action": action})


def start_server(directory: Path, port: int, app=None) -> int:
    handler = type("Handler", (QuietHandler,), {"app": app})
    for p in range(port, port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), partial(handler, directory=str(directory)))
        except OSError:
            continue
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return p
    raise SystemExit(f"no free port between {port} and {port + 19}")


def running_instance(port: int) -> bool:
    """Is a Plane Watch already serving on this port?"""
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as r:
            return json.loads(r.read()).get("app") == "planewatch"
    except (OSError, ValueError):
        return False


def send_control(port: int, action: str) -> bool:
    import urllib.request

    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/{action}", data=b"", method="POST", headers={CONTROL_HEADER: "1"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 200
    except OSError:
        return False


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
        self.squawks = tuple(x.strip() for x in args.squawks.split(",") if x.strip())
        self.paused = threading.Event()
        self.wake = threading.Event()  # set to skip the wait (check now, resume, stop)
        self.stopping = threading.Event()
        self.last_status: dict | None = None
        self.client.on_progress = self.progress
        self._last_progress = 0.0

    def progress(self, message: str, checking: bool = True):
        """Tell the dashboard what's happening right now (throttled)."""
        now = time.time()
        if checking and now - self._last_progress < 0.7:
            return
        self._last_progress = now
        try:
            self._write("progress.json", {"message": message, "ts": now, "checking": checking})
        except OSError:
            pass

    def _load(self, name, default):
        try:
            return json.loads((self.out / "data" / name).read_text())
        except (OSError, ValueError):
            return default

    def _write(self, name, obj):
        tmp = self.out / "data" / (name + ".tmp")
        tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
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
        self.client.ok_requests = 0
        started = time.time()
        self.progress("Reading ACARS…")
        msgs = self.acars()
        flagged = pipeline.flagged_messages(msgs)
        aircraft = {ac.hex: ac for ac in self.client.squawks(self.squawks)}
        for ac in self.client.emergencies():
            aircraft.setdefault(ac.hex, ac)

        # ACARS-only aircraft: look each one up once, not every cycle
        client = _CachedLocator(self.client, self.locate_cache)
        alerts = pipeline.build_alerts(list(aircraft.values()), flagged, client, self.squawks)

        now = time.time()
        current = []
        for alert in alerts:
            entry = self.known.get(alert.aircraft.hex)
            if entry:
                self._carry_over(entry["alert"], alert)
            else:
                self.progress(f"Getting track, route and photo for {alert.aircraft.label}")
                pipeline.enrich(alert, self.client, fetch_photos=not self.args.no_photos, trace_hours=8)
                entry = {"first_seen": now, "photo_bytes": self._photo(alert), "rendered_ts": None, "sig": None}
            entry.update(alert=alert, last_seen=now)
            self.known[alert.aircraft.hex] = entry
            # redraw when the aircraft moved or the evidence changed
            sig = (alert.aircraft.seen_ts, tuple(sorted((f.category, f.origin, f.ts or 0) for f in alert.flags)),
                   (alert.divert_airport or {}).get("icao"))
            if alert.aircraft.lat is not None and entry["sig"] != sig:
                name = f"cards/{alert.aircraft.hex}.png"
                self.progress(f"Drawing the card for {alert.aircraft.label}")
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

        self.progress("Drawing the overview map")
        render.render_overview(alerts, self.out / "cards" / "overview.png", title=self.args.title, brand=self.args.brand)
        self._record(alerts, flagged, now)
        feeds_ok = self.client.ok_requests > 0
        status = {
            "phase": "ok" if feeds_ok else "offline",
            "message": "" if feeds_ok else (
                "None of the flight-tracking feeds answered (" + ", ".join(self.client.sources) + "). Check your internet "
                "connection; a firewall, VPN, school/work network or antivirus may be blocking them. It keeps trying."),
            "updated_at": _iso(now),
            "updated_ts": now,
            "next_update_ts": started + self.args.interval,
            "interval_s": self.args.interval,
            "squawks": list(self.squawks),
            "sources": self.client.sources,
            "acars": {
                "messages": len(msgs), "flagged": len(flagged),
                "udp_port": self.args.udp_port if self.listener and not self.listener.error else None,
                "udp_received": self.listener.received if self.listener else 0,
                "airframes": bool(os.environ.get("AIRFRAMES_API_KEY")),
                "files": self.args.acars_file or [],
            },
            "errors": _tidy(self.client.errors) + ([self.listener.error] if self.listener and self.listener.error else []),
            "alerts": [self._summary(e, live=True) for e in sorted(current, key=lambda e: -classify.severity(e["alert"].top.category))],
            "cleared": [self._summary(e, live=False) for e in sorted(cleared, key=lambda e: -e["last_seen"])],
        }
        status["paused"] = self.paused.is_set()
        self.last_status = status
        self._write("live.json", status)
        self.progress("", checking=False)
        return status

    def control(self, action: str):
        """Called by the dashboard buttons (and --stop)."""
        if action == "pause":
            self.paused.set()
            print("  paused (press Resume on the dashboard to continue)", flush=True)
        elif action == "resume":
            self.paused.clear()
            print("  resumed", flush=True)
        elif action == "stop":
            self.stopping.set()
        self._mark_paused()
        self.wake.set()

    def _mark_paused(self):
        """Reflect pause/stop in live.json right away, without waiting for a check."""
        status = dict(self.last_status or self._stub("starting", "Checking the feeds for the first time…"))
        status["paused"] = self.paused.is_set()
        if self.stopping.is_set():
            status["phase"], status["message"] = "stopped", "Plane Watch was stopped."
        self._write("live.json", status)

    def _stub(self, phase: str, message: str) -> dict:
        """A live.json for before the first check finishes (or when a check crashed)."""
        now = time.time()
        return {
            "phase": phase, "message": message, "updated_at": _iso(now), "updated_ts": now, "next_update_ts": now,
            "interval_s": self.args.interval, "squawks": list(self.squawks), "sources": self.client.sources,
            "acars": {"messages": 0, "flagged": 0, "udp_port": self.args.udp_port if self.listener and not self.listener.error else None,
                      "udp_received": 0, "airframes": bool(os.environ.get("AIRFRAMES_API_KEY")), "files": self.args.acars_file or []},
            "errors": list(self.client.errors), "alerts": [], "cleared": [], "paused": self.paused.is_set(),
        }

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
        with (self.out / "data" / "acars_flagged.jsonl").open("a", encoding="utf-8") as fh:
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
        if running_instance(self.args.port):
            url = f"http://localhost:{self.args.port}/"
            print(f"  Plane Watch is already running: {url}")
            if not self.args.no_browser:
                webbrowser.open(url)
            return 0
        web = files(__package__).joinpath("web", "index.html").read_text(encoding="utf-8")
        (self.out / "index.html").write_text(web, encoding="utf-8")
        port = start_server(self.out, self.args.port, self)
        url = f"http://localhost:{port}/"
        if self.listener:
            self.listener.start()
        self._write("live.json", self._stub("starting", "Checking the feeds for the first time…"))
        self.progress("Starting…")
        print(f"\n  PLANE WATCH  ·  dashboard: {url}")
        print(f"  watching squawks {', '.join(self.squawks)} + transponder emergency status + ACARS threats")
        if self.listener and not self.listener.error:
            print(f"  ACARS: listening for decoder JSON on UDP port {self.args.udp_port}")
        print(f"  saving to {self.out}")
        print("  stop: Ctrl+C here, or the Stop button on the dashboard")
        print("  first check: about 20-60 seconds…\n", flush=True)
        if self.args.once:
            self._report(self.cycle())
            return 0
        if not self.args.no_browser:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        while not self.stopping.is_set():
            t0 = time.time()
            if not self.paused.is_set():
                try:
                    self._report(self.cycle())
                except Exception as e:  # noqa: BLE001 - keep the app alive through network hiccups
                    import traceback

                    traceback.print_exc()
                    print(f"  ! check failed: {e}  (trying again in {self.args.interval:.0f}s)", file=sys.stderr)
                    stub = self._stub("error", f"The last check failed: {type(e).__name__}: {e}")
                    stub["next_update_ts"] = time.time() + self.args.interval
                    self._write("live.json", stub)
                    self.progress("", checking=False)
            self.wake.clear()
            self.wake.wait(timeout=None if self.paused.is_set() else max(5, self.args.interval - (time.time() - t0)))
        self._mark_paused()
        print("  stopped.", flush=True)
        time.sleep(1.5)  # let the dashboard read the final status
        return 0

    @staticmethod
    def _report(status: dict):
        stamp = datetime.now().strftime("%H:%M:%S")
        alerts = status["alerts"]
        if status.get("phase") == "offline":
            print(f"  {stamp}  ! couldn't reach any flight-tracking feed. Problems:")
            for e in status["errors"][:6]:
                print(f"           - {e}")
            return
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


def _tidy(errors: list[str]) -> list[str]:
    """Drop the follow-up "skipped" notes and exact repeats; keep the first real error per problem."""
    out = []
    for e in errors:
        if "skipped: it didn't respond" in e or e in out:
            continue
        out.append(e)
    return out


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
        prog="planewatch", description="Watch for aircraft in an emergency (squawk 7500/7600/7700 + ACARS threats) and show live cards.",
        epilog="Other commands: demo (sample cards, offline), scan (one-off scan), render --hex X (card for any aircraft), "
               "classify TEXT (test the ACARS rules). Add --help after any of them.")
    p.add_argument("--out", default="planewatch-data", help="where cards and data are saved (default: ./planewatch-data)")
    p.add_argument("--squawks", default=",".join(DEFAULT_SQUAWKS),
                   help="squawk codes to watch (default 7500,7600,7700; 7400 and 7777 also work)")
    p.add_argument("--background", action="store_true", help="keep running in the background with no terminal window")
    p.add_argument("--stop", action="store_true", help="stop a Plane Watch that's running in the background")
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


def _launch_command() -> list[str]:
    """How to start this same app again in a new process."""
    exe = sys.executable
    if os.name == "nt":  # pythonw.exe runs without a console window
        w = Path(exe).with_name("pythonw.exe")
        exe = str(w) if w.exists() else exe
    script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if script.endswith((".pyz", "planewatch.py")):
        return [exe, script]
    return [exe, "-m", "plane_watch"]


def start_background(args, argv) -> int:
    """Start a detached copy of the app, wait until its dashboard answers, open it, and return."""
    import subprocess

    url = f"http://localhost:{args.port}/"
    if running_instance(args.port):
        print(f"  Plane Watch is already running: {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 0
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    log = out / "planewatch.log"
    child_args = [a for a in argv if a != "--background"] + ["--no-browser", "--out", str(out), "--_log", str(log)]
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
    else:
        kw["start_new_session"] = True
    env = dict(os.environ)
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    subprocess.Popen(_launch_command() + child_args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, cwd=os.getcwd(), env=env, close_fds=True, **kw)
    for _ in range(60):
        time.sleep(0.5)
        if running_instance(args.port):
            print(f"  Plane Watch is running in the background: {url}")
            print("  Stop it with the Stop button on the dashboard, or Stop Plane Watch.bat / --stop.")
            if not args.no_browser:
                webbrowser.open(url)
            return 0
    print(f"  Plane Watch didn't start. See {log}", file=sys.stderr)
    return 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    p = parser()
    p.add_argument("--_log", help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.stop:
        if send_control(args.port, "stop"):
            print("  Plane Watch stopped.")
            return 0
        print(f"  Plane Watch isn't running (nothing answered on port {args.port}).")
        return 1
    if args.background:
        return start_background(args, argv)
    if args._log:  # background copy: there's no console, so write everything to the log file
        fh = open(args._log, "a", buffering=1, encoding="utf-8")  # noqa: SIM115 - lives as long as the process
        sys.stdout = sys.stderr = fh
    try:
        return LiveApp(args).run() or 0
    except KeyboardInterrupt:
        print("\n  stopped.")
        return 0
