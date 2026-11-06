"""Command line.

  python -m plane_watch          the live app: watch everything, dashboard in the browser
  python -m plane_watch scan     find flagged aircraft now, render cards, save data
  python -m plane_watch render   card for specific aircraft (--hex / --reg / --callsign)
  python -m plane_watch demo     render the built-in sample alerts (offline)
  python -m plane_watch classify "ACARS TEXT"   show what the keyword rules match
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import classify, pipeline, render
from .sources import acars as acars_src
from .sources import photos as photo_src
from .sources.adsb import DEFAULT_SOURCES, AdsbClient


def _csv(s: str) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _render_all(alerts, out: Path, args, sample=False):
    (out / "cards").mkdir(parents=True, exist_ok=True)
    for a in alerts:
        if a.aircraft.lat is None:
            print(f"  {a.aircraft.label}: no position known, saved to data without a card", file=sys.stderr)
            continue
        photo = None
        if a.photo and not args.no_photos:
            photo = photo_src.download(a.photo["url"])
        name = f"cards/{a.id}.png"
        render.render_card(a, out / name, photo, title=args.title, brand=args.brand, sample=sample)
        a.card = name
        print(f"  {CAT(a)}  {a.aircraft.label:<10} {a.aircraft.description or a.aircraft.type_code:<28} -> {out / name}")


def CAT(a):
    return f"[{classify.CATEGORIES[a.top.category]['label']}]".ljust(34)


def _finish(alerts, out: Path, args, run_id: str, meta: dict, sample=False):
    _render_all(alerts, out, args, sample)
    overview = f"cards/overview-{run_id}.png"
    render.render_overview(alerts, out / overview, title=args.title, brand=args.brand, sample=sample)
    meta["overview"] = overview
    report = pipeline.save_run(alerts, out, run_id, meta)
    pipeline.prune_cards(out, args.keep_runs)
    print(f"\n{len(alerts)} alert(s). Overview: {out / overview}\nData: {out / 'data' / 'runs' / (run_id + '.json')}, "
          f"{out / 'data' / 'alerts.csv'}")
    return report


def cmd_scan(args) -> int:
    out = Path(args.out)
    client = AdsbClient(_csv(args.sources))
    squawks = tuple(_csv(args.squawks))
    history_path = out / "data" / "track_history.json"
    history = pipeline.load_history(history_path)

    # ACARS
    msgs = []
    since = time.time() - args.acars_hours * 3600
    for f in args.acars_file or []:
        try:
            msgs += acars_src.read_file(f, since)
        except OSError as e:
            client.errors.append(f"ACARS file {f}: {e}")
    if args.airframes or os.environ.get("AIRFRAMES_API_KEY"):
        try:
            msgs += acars_src.fetch_airframes(since_ts=since)
        except Exception as e:  # noqa: BLE001 - report and carry on with ADS-B
            client.errors.append(f"airframes.io: {e}")
    flagged = pipeline.flagged_messages(msgs)
    print(f"ACARS: {len(msgs)} message(s), {len(flagged)} flagged", file=sys.stderr)

    # ADS-B, optionally polled for a while to build tracks
    deadline = time.time() + args.watch_minutes * 60
    seen: dict = {}
    while True:
        for ac in client.squawks(squawks) + (client.emergencies() if args.military_emergencies else []):
            prev = seen.get(ac.hex)
            if prev:
                ac.track = prev.track
            if ac.lat is not None and (not ac.track or ac.track[-1].ts < ac.seen_ts):
                from .models import TrackPoint

                ac.track.append(TrackPoint(ac.seen_ts, ac.lat, ac.lon, ac.alt_ft))
            seen[ac.hex] = ac
        if time.time() >= deadline:
            break
        time.sleep(max(15, args.interval))
    print(f"ADS-B: {len(seen)} aircraft squawking {','.join(squawks)}", file=sys.stderr)

    alerts = pipeline.build_alerts(list(seen.values()), flagged, client, squawks)
    for a in alerts:
        live_track = a.aircraft.track
        a.aircraft.track = []
        pipeline.enrich(a, client, history, fetch_photos=not args.no_photos, trace_hours=args.trace_hours)
        if not a.aircraft.track or (live_track and len(live_track) > len(a.aircraft.track)):
            a.aircraft.track = live_track or a.aircraft.track
    pipeline.save_history(history_path, history, alerts)

    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    meta = {"squawks": list(squawks), "sources": client.sources, "acars_messages": len(msgs), "acars_flagged": len(flagged),
            "errors": client.errors}
    _finish(alerts, out, args, run_id, meta)
    for e in client.errors:
        print(f"warning: {e}", file=sys.stderr)
    if not seen and not msgs and client.errors and len(client.errors) >= len(client.sources) * len(squawks):
        print("error: every ADS-B request failed", file=sys.stderr)
        return 1
    return 0


def cmd_render(args) -> int:
    out = Path(args.out)
    client = AdsbClient(_csv(args.sources))
    targets = []
    for kind, values in (("hex", args.hex), ("reg", args.reg), ("callsign", args.callsign)):
        for v in _csv(values or ""):
            ac = client.first(kind, v)
            if ac:
                targets.append(ac)
            else:
                print(f"warning: {kind} {v} not found on {', '.join(client.sources)}", file=sys.stderr)
    msgs = []
    for f in args.acars_file or []:
        msgs += acars_src.read_file(f)
    alerts = pipeline.build_alerts(targets, pipeline.flagged_messages(msgs), client, tuple(classify.SQUAWKS), watch=targets)
    alerts = [a for a in alerts if any(a.aircraft.hex == t.hex for t in targets)]
    for a in alerts:
        pipeline.enrich(a, client, fetch_photos=not args.no_photos, trace_hours=args.trace_hours)
    for e in client.errors:
        print(f"warning: {e}", file=sys.stderr)
    if not alerts:
        return 1
    _render_all(alerts, out, args)
    return 0


def cmd_demo(args) -> int:
    from .sample import sample_alerts

    out = Path(args.out)
    alerts = sample_alerts()
    _finish(alerts, out, args, args.run_id or "sample", {"sample": True, "errors": []}, sample=True)
    return 0


def cmd_classify(args) -> int:
    text = " ".join(args.text) or sys.stdin.read()
    from . import geo

    hits = classify.classify_text(text)
    print(json.dumps({
        "matches": [{"category": c, "label": classify.CATEGORIES[c]["label"], "matched": m} for c, m in hits],
        "divert_to": classify.divert_target(text, geo.airport),
    }, indent=2))
    return 0 if hits else 1


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] == "app" or argv[0].startswith("-"):
        from .app import main as app_main

        return app_main(argv[1:] if argv and argv[0] == "app" else argv)
    p = argparse.ArgumentParser(prog="plane_watch", description="Flag aircraft in trouble from ADS-B + ACARS and render cards.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--out", default="site/planes", help="output folder (default: site/planes)")
        sp.add_argument("--title", default="PLANE ALERT")
        sp.add_argument("--brand", default="plane watch")
        sp.add_argument("--no-photos", action="store_true", help="don't fetch aircraft photos")
        sp.add_argument("--keep-runs", type=int, default=60, help="delete card images older than this many runs")

    s = sub.add_parser("scan", help="find flagged aircraft now")
    common(s)
    s.add_argument("--squawks", default=",".join(classify.SQUAWKS), help="squawk codes to look for (default: all, 7500,7600,7700,7400,7777)")
    s.add_argument("--sources", default=",".join(DEFAULT_SOURCES), help="ADS-B APIs: adsb.lol,airplanes.live,adsb.fi")
    s.add_argument("--acars-file", action="append", help="JSON lines from acarsdec/dumpvdl2/dumphfdl/ACARS Hub (repeatable)")
    s.add_argument("--airframes", action="store_true", help="also pull ACARS from airframes.io (needs AIRFRAMES_API_KEY)")
    s.add_argument("--acars-hours", type=float, default=6, help="ignore ACARS older than this (default 6)")
    s.add_argument("--military-emergencies", action="store_true", help="also check the military feed for emergency status")
    s.add_argument("--watch-minutes", type=float, default=0, help="keep polling this long to build live tracks")
    s.add_argument("--interval", type=float, default=60, help="seconds between polls in watch mode (min 15)")
    s.add_argument("--trace-hours", type=float, default=8)
    s.add_argument("--run-id", default="")
    s.set_defaults(func=cmd_scan)

    r = sub.add_parser("render", help="card for specific aircraft")
    common(r)
    r.add_argument("--hex", help="ICAO hex code(s), comma-separated")
    r.add_argument("--reg", help="registration(s)")
    r.add_argument("--callsign", help="callsign(s)")
    r.add_argument("--acars-file", action="append")
    r.add_argument("--sources", default=",".join(DEFAULT_SOURCES))
    r.add_argument("--trace-hours", type=float, default=8)
    r.set_defaults(func=cmd_render)

    d = sub.add_parser("demo", help="render the built-in sample alerts (no network)")
    common(d)
    d.set_defaults(out="sample_output", no_photos=True)
    d.add_argument("--run-id", default="")
    d.set_defaults(func=cmd_demo)

    c = sub.add_parser("classify", help="test the ACARS keyword rules on some text")
    c.add_argument("text", nargs="*")
    c.set_defaults(func=cmd_classify)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
