"""plane_watch tests. Offline: every network call is mocked or disabled."""

import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["PLANE_WATCH_OFFLINE"] = "1"  # any unmocked fetch fails fast instead of hitting the network
os.environ.setdefault("PLANE_WATCH_CACHE", tempfile.mkdtemp())

from plane_watch import classify, geo, pipeline  # noqa: E402
from plane_watch.models import AcarsMessage, Aircraft, Alert, Flag  # noqa: E402
from plane_watch.sample import SAMPLE_ACARS_JSONL, sample_alerts  # noqa: E402
from plane_watch.sources import acars, adsb  # noqa: E402

READSB = {
    "now": 1793936000000,
    "ac": [
        {"hex": "a1b2c3", "flight": "UAL123  ", "r": "N12345", "t": "B738", "desc": "BOEING 737-800", "alt_baro": 31000,
         "gs": 450.2, "track": 271.5, "baro_rate": -64, "squawk": "7700", "emergency": "general", "lat": 40.1, "lon": -75.2,
         "seen_pos": 2.0, "dbFlags": 0},
        {"hex": "ae6808", "flight": "", "t": "P8", "alt_baro": "ground", "squawk": "7600", "emergency": "none",
         "lastPosition": {"lat": 4.2, "lon": 73.5, "seen_pos": 40}, "dbFlags": 1},
    ],
}


class ClassifyTests(unittest.TestCase):
    def test_squawks_and_adsb_emergency(self):
        ac = adsb.parse_response(READSB)[0]
        flags = classify.classify_aircraft(ac)
        self.assertEqual([f.category for f in flags], ["emergency"])  # emergency status doesn't duplicate the squawk
        self.assertEqual(flags[0].origin, "squawk")

        nordo = Aircraft("ABC123", squawk="1200", emergency="nordo")
        self.assertEqual([f.category for f in classify.classify_aircraft(nordo)], ["radio"])
        self.assertEqual(classify.classify_aircraft(Aircraft("ABC123", squawk="7500"))[0].category, "hijack")
        self.assertEqual(classify.classify_aircraft(Aircraft("ABC123", squawk="1000")), [])

    def test_acars_keywords(self):
        cats = lambda t: [c for c, _ in classify.classify_text(t)]  # noqa: E731
        self.assertIn("bomb", cats("SECURITY: BOMB THREAT NOTE FOUND IN AFT LAV"))
        self.assertIn("diversion", cats("WE ARE DIVERTING TO EGLL DUE MEDICAL"))
        self.assertIn("medical", cats("PAX UNCONSCIOUS MEDLINK ADVISED"))
        self.assertIn("security", cats("UNRULY PAX RESTRAINED REQ POLICE MEET"))
        self.assertIn("emergency", cats("CREW DECLARED EMERGENCY MAYDAY"))
        self.assertIn("fire", cats("SMOKE IN CABIN"))
        self.assertIn("hijack", cats("POSSIBLE HIJACKING IN PROGRESS"))
        # routine planning and test messages are not alerts
        self.assertEqual(cats("DIVERT FUEL 2400 ALTN EGSS"), [])
        self.assertEqual(cats("ENG FIRE TEST OK. FUEL ON BOARD 12.4"), [])
        self.assertEqual(cats("ETA 1432 GATE B12 WX CAVOK"), [])

    def test_divert_target_is_validated_against_airports(self):
        self.assertEqual(classify.divert_target("DIVERTING TO EGLL", geo.airport), "EGLL")
        self.assertEqual(classify.divert_target("DVRT LHR WX", geo.airport), "LHR")
        self.assertEqual(classify.divert_target("DIVERTING DUE WX TO KJFK", geo.airport), "")  # verb isn't next to it
        self.assertEqual(classify.divert_target("DIVERT FOR FUEL", geo.airport), "")

    def test_message_flags_carry_excerpt_and_target(self):
        m = AcarsMessage(1.0, "REQ LEO MEET. BOMB THREAT REC VIA CO. DIVERTING TO LBSF", flight="DEMO1")
        flags = {f.category: f for f in classify.classify_message(m, geo.airport)}
        self.assertEqual(set(flags), {"bomb", "security", "diversion"})
        self.assertEqual(flags["diversion"].divert_to, "LBSF")
        self.assertIn("BOMB", flags["bomb"].evidence)


class AcarsTests(unittest.TestCase):
    def test_reads_mixed_decoder_formats(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "acars.jsonl"
            f.write_text(SAMPLE_ACARS_JSONL)
            msgs = acars.read_file(f)
        self.assertEqual([m.source for m in msgs], ["acarsdec", "dumpvdl2", "dumphfdl"])
        self.assertEqual(msgs[0].registration, "TC-DMO")  # leading dot stripped
        self.assertEqual(msgs[1].hex, "896ABC")
        self.assertEqual(msgs[1].flight, "DEMO771")
        self.assertEqual(msgs[2].hex, "A1B2C3")

    def test_airframes_shape(self):
        m = acars.normalize({"timestamp": "2026-11-06T03:00:00Z", "tail": "N1", "flightNumber": "AA1", "text": "MAYDAY",
                             "label": "H1", "airframe": {"icao": "a00001"}})
        self.assertEqual((m.registration, m.flight, m.hex, m.source), ("N1", "AA1", "A00001", "airframes"))

    def test_airframes_needs_key(self):
        with mock.patch.dict(os.environ, {"AIRFRAMES_API_KEY": ""}):
            with self.assertRaises(Exception):
                acars.fetch_airframes(api_key="")

    def test_json_array_and_skips_empty(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "a.json"
            f.write_text(json.dumps([{"text": "", "tail": "X"}, {"text": "HELLO", "tail": "N1", "timestamp": 5}]))
            self.assertEqual(len(acars.read_file(f)), 1)


class AdsbTests(unittest.TestCase):
    def test_parse(self):
        a, b = adsb.parse_response(READSB, "adsb.lol")
        self.assertEqual((a.hex, a.callsign, a.alt_ft, a.squawk, a.emergency), ("A1B2C3", "UAL123", 31000, "7700", "general"))
        self.assertAlmostEqual(a.seen_ts, 1793935998.0)
        self.assertTrue(b.on_ground and b.military)
        self.assertEqual((b.lat, b.lon, b.emergency), (4.2, 73.5, ""))

    def test_client_merges_sources_and_records_errors(self):
        calls = []

        def fake(url, headers=None):
            calls.append(url)
            if "airplanes.live" in url:
                raise adsb.http.FetchError("HTTP 429")
            return READSB

        with mock.patch.object(adsb.http, "get_json", side_effect=fake):
            client = adsb.AdsbClient(("adsb.lol", "airplanes.live", "adsb.fi"), gap_s=0)
            found = client.squawks(["7700"])
        self.assertEqual(len(found), 2)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(client.errors), 1)
        self.assertIn("opendata.adsb.fi/api/v2/sqk/7700", calls[2])

    def test_trace_keeps_current_leg_and_drops_taxi(self):
        data = {"timestamp": 1000, "trace": [
            [0, 10, 10, 30000, 400, 90, 0],
            [60, 10.1, 10.1, 30000, 400, 90, 0],
            [4000, 20, 20, "ground", 10, 0, 0],  # >30 min gap: new leg
            [4030, 20, 20.001, "ground", 10, 0, 0],
            [4060, 20.01, 20.01, 1500, 160, 0, 0],
            [4120, 20.1, 20.1, 5000, 220, 0, 0],
        ]}
        pts = adsb.parse_trace(data)
        self.assertEqual([p.alt_ft for p in pts], [0, 1500, 5000])
        self.assertEqual(pts[0].ts, 5030)

    def test_route(self):
        res = [{"callsign": "BAW1", "airport_codes": "EGLL-KJFK", "plausible": 1, "_airports": [
            {"icao": "EGLL", "iata": "LHR", "name": "Heathrow", "location": "London", "lat": 51.47, "lon": -0.45},
            {"icao": "KJFK", "iata": "JFK", "name": "JFK", "location": "New York", "lat": 40.64, "lon": -73.78}]}]
        r = adsb.parse_route(res)
        self.assertEqual(r["codes"], "EGLL-KJFK")
        self.assertEqual(r["destination"]["icao"], "KJFK")
        self.assertIsNone(adsb.parse_route([]))


class GeoTests(unittest.TestCase):
    def test_country(self):
        self.assertEqual(geo.country_for_hex("AE6808"), ("US", "United States"))
        self.assertEqual(geo.country_for_hex("4b8def")[0], "TR")
        self.assertEqual(geo.country_for_hex("~2a1b3c"), ("", ""))

    def test_airports_and_distance(self):
        self.assertEqual(geo.airport("LHR")["icao"], "EGLL")
        self.assertIsNone(geo.airport("ZZZZ"))
        self.assertAlmostEqual(geo.haversine_km(51.47, -0.45, 40.64, -73.78), 5540, delta=30)
        # a point on the London-New York great circle is ~0 km off it; Madrid is far off
        self.assertLess(geo.cross_track_km(51.47, -0.45, 51.47, -0.45, 40.64, -73.78), 1)
        self.assertGreater(geo.cross_track_km(40.4, -3.7, 51.47, -0.45, 40.64, -73.78), 500)


class PipelineTests(unittest.TestCase):
    def test_acars_attaches_to_squawking_aircraft(self):
        ac = adsb.parse_response(READSB)[0]
        msg = AcarsMessage(1.0, "PAX ILL DIVERTING TO KPHL", registration="N12345")
        alerts = pipeline.build_alerts([ac], pipeline.flagged_messages([msg]))
        self.assertEqual(len(alerts), 1)
        self.assertEqual({f.category for f in alerts[0].flags}, {"emergency", "medical", "diversion"})
        self.assertEqual(alerts[0].top.category, "emergency")

    def test_acars_only_aircraft_is_looked_up(self):
        client = mock.Mock()
        client.first.return_value = Aircraft("ABCDEF", "DEMO9", lat=1.0, lon=2.0, seen_ts=5.0)
        msg = AcarsMessage(1.0, "BOMB THREAT", flight="DEMO9")
        alerts = pipeline.build_alerts([], pipeline.flagged_messages([msg]), client)
        self.assertEqual(alerts[0].aircraft.hex, "ABCDEF")
        self.assertEqual(alerts[0].top.category, "bomb")
        client.first.assert_called_with("callsign", "DEMO9")

    def test_route_deviation_adds_possible_diversion(self):
        ac = Aircraft("ABC123", "BAW1", lat=40.4, lon=-3.7, seen_ts=1.0)  # over Madrid
        alert = Alert(ac, [Flag("emergency", "x", "squawk")])
        alert.route = {"codes": "EGLL-KJFK", "plausible": True, "origin": geo.airport("EGLL"), "destination": geo.airport("KJFK")}
        pipeline.route_divert_check(alert)
        self.assertEqual(alert.flags[-1].category, "diversion")
        self.assertEqual(alert.flags[-1].origin, "route")

    def test_enrich_uses_history_and_divert_airport(self):
        ac = Aircraft("ABC123", lat=5.0, lon=72.6, alt_ft=14500, seen_ts=2000.0)
        alert = Alert(ac, [Flag("diversion", "x", "acars", divert_to="VRMM")])
        history = {"ABC123": [{"ts": 1000.0, "lat": 6.0, "lon": 71.0, "alt_ft": 30000}]}
        pipeline.enrich(alert, None, history, fetch_photos=False)
        self.assertEqual(len(ac.track), 2)
        self.assertEqual(alert.divert_airport["icao"], "VRMM")

    def test_save_run_writes_report_index_csv(self):
        alerts = sample_alerts()
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            pipeline.save_run(alerts, out, "r1", {"errors": []})
            pipeline.save_run(alerts[:1], out, "r2", {"errors": []})
            index = json.loads((out / "data" / "index.json").read_text())
            self.assertEqual([r["id"] for r in index["runs"]], ["r2", "r1"])
            self.assertEqual(index["runs"][1]["categories"], {"emergency": 1, "bomb": 1, "radio": 1})
            report = json.loads((out / "data" / "runs" / "r1.json").read_text())
            self.assertEqual(report["alerts"][0]["category"], "bomb")  # most severe first
            with (out / "data" / "alerts.csv").open() as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[0]["divert_to"], "VRMM")
            flagged = (out / "data" / "acars_flagged.jsonl").read_text().splitlines()
            self.assertEqual(len(flagged), 3)


class ScanCliTests(unittest.TestCase):
    def test_scan_end_to_end_with_mocked_apis(self):
        from plane_watch.__main__ import main

        trace = {"timestamp": 1793930000, "trace": [[i * 60, 39.0 + i * 0.02, -77.0 + i * 0.03, 30000, 450, 60, 0] for i in range(50)]}

        def fake_get(url, headers=None):
            if "/sqk/7700" in url:
                return READSB
            if "trace_full_a1b2c3" in url:
                return trace
            raise adsb.http.FetchError("HTTP 404")

        route = [{"airport_codes": "KIAD-KBOS", "plausible": 1, "_airports": [
            {"icao": "KIAD", "lat": 38.94, "lon": -77.46, "name": "Dulles"}, {"icao": "KBOS", "lat": 42.36, "lon": -71.0, "name": "Logan"}]}]
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(adsb.http, "get_json", side_effect=fake_get), \
                mock.patch.object(adsb.http, "post_json", return_value=route), \
                mock.patch.object(adsb.time, "sleep"):
            acars_file = Path(d) / "a.jsonl"
            acars_file.write_text(json.dumps({"timestamp": 1793935990, "tail": "N12345", "flight": "UAL123",
                                              "text": "PAX UNCONSCIOUS DIVERTING TO KPHL", "label": "H1"}) + "\n")
            code = main(["scan", "--out", d, "--sources", "adsb.lol", "--squawks", "7700", "--acars-file", str(acars_file),
                         "--acars-hours", "1000000", "--run-id", "t1"])
            self.assertEqual(code, 0)
            report = json.loads((Path(d) / "data" / "runs" / "t1.json").read_text())
            self.assertEqual(len(report["alerts"]), 1)
            a = report["alerts"][0]
            self.assertEqual(a["aircraft"]["hex"], "A1B2C3")
            self.assertEqual({f["category"] for f in a["flags"]}, {"emergency", "medical", "diversion"})
            self.assertEqual(a["divert_airport"]["icao"], "KPHL")
            self.assertEqual(a["route"]["codes"], "KIAD-KBOS")
            self.assertGreaterEqual(a["track_points"], 50)
            self.assertTrue((Path(d) / a["card"]).exists())
            self.assertTrue((Path(d) / "data" / "track_history.json").exists())


class RenderTests(unittest.TestCase):
    def test_demo_renders_cards_and_overview(self):
        from matplotlib import image as mpimg

        from plane_watch.__main__ import main

        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(main(["demo", "--out", d]), 0)
            cards = sorted(p.name for p in (Path(d) / "cards").glob("*.png"))
            self.assertEqual(len(cards), 4)
            self.assertIn("overview-sample.png", cards)
            img = mpimg.imread(Path(d) / "cards" / cards[0])
            self.assertEqual(img.shape[:2], (900, 1600))
            report = json.loads((Path(d) / "data" / "runs" / "sample.json").read_text())
            self.assertTrue(all(a["card"] for a in report["alerts"]))

    def test_card_without_track_or_extras(self):
        from plane_watch import render

        ac = Aircraft("ABC123", "TEST1", lat=51.0, lon=0.0, squawk="7700", seen_ts=1793936000.0)
        alert = Alert(ac, classify.classify_aircraft(ac))
        with tempfile.TemporaryDirectory() as d:
            render.render_card(alert, Path(d) / "c.png")
            render.render_overview([], Path(d) / "o.png")
            self.assertTrue((Path(d) / "c.png").stat().st_size > 10000)


if __name__ == "__main__":
    unittest.main()


class LiveAppTests(unittest.TestCase):
    def _args(self, out, **kw):
        from plane_watch.app import parser

        args = parser().parse_args(["--out", out, "--udp-port", "0", "--no-browser", "--sources", "adsb.lol"])
        for k, v in kw.items():
            setattr(args, k, v)
        return args

    def test_two_cycles_extend_track_and_dont_duplicate_history(self):
        from plane_watch.app import LiveApp

        state = {"lat": 40.1}

        def fake_get(url, headers=None):
            if "/sqk/7700" in url:
                data = json.loads(json.dumps(READSB))
                data["ac"] = data["ac"][:1]
                data["ac"][0]["lat"] = state["lat"]
                data["now"] += int((state["lat"] - 40.1) * 1e6)
                return data
            if url.endswith("/mil") or "/sqk/" in url:
                return {"now": READSB["now"], "ac": []}
            raise adsb.http.FetchError("HTTP 404")

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(adsb.http, "get_json", side_effect=fake_get), \
                mock.patch.object(adsb.http, "post_json", side_effect=adsb.http.FetchError("x")), \
                mock.patch.object(adsb.time, "sleep"):
            app = LiveApp(self._args(d))
            s1 = app.cycle()
            state["lat"] = 40.3
            s2 = app.cycle()
            self.assertEqual(len(s1["alerts"]), 1)
            self.assertEqual(s2["alerts"][0]["track_points"], 2)
            self.assertTrue((Path(d) / "cards" / "A1B2C3.png").exists())
            self.assertTrue((Path(d) / "cards" / "overview.png").exists())
            live = json.loads((Path(d) / "data" / "live.json").read_text())
            self.assertEqual(live["alerts"][0]["category"], "emergency")
            self.assertEqual(live["squawks"], ["7500", "7600", "7700", "7400", "7777"])
            with (Path(d) / "data" / "alerts.csv").open() as fh:
                self.assertEqual(len(list(csv.DictReader(fh))), 1)  # same alert, logged once

            # aircraft stops squawking: moves to "recently cleared"
            state["lat"] = None
            with mock.patch.object(adsb.http, "get_json", return_value={"now": READSB["now"], "ac": []}):
                s3 = app.cycle()
            self.assertEqual(s3["alerts"], [])
            self.assertEqual(s3["cleared"][0]["aircraft"]["hex"], "A1B2C3")

    def test_udp_listener_parses_decoder_json(self):
        import socket
        import time as _t

        from plane_watch.app import AcarsListener

        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        listener = AcarsListener(port)
        listener.start()
        self.assertEqual(listener.error, "")
        line = json.dumps({"timestamp": _t.time(), "tail": "N1", "flight": "X1", "text": "BOMB THREAT", "station_id": "a"})
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(line.encode(), ("127.0.0.1", port))
        for _ in range(50):
            if listener.recent():
                break
            _t.sleep(0.05)
        self.assertEqual(listener.recent()[0].text, "BOMB THREAT")

    def test_no_command_runs_app(self):
        from plane_watch import __main__ as cli

        with mock.patch("plane_watch.app.main", return_value=0) as app_main:
            self.assertEqual(cli.main([]), 0)
            cli.main(["--interval", "30"])
        self.assertEqual(app_main.call_args_list[1].args[0], ["--interval", "30"])


class BuildAppTests(unittest.TestCase):
    def test_pyz_runs_demo(self):
        import subprocess
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        from build_app import build

        with tempfile.TemporaryDirectory() as d:
            pyz = build(Path(d) / "planewatch.pyz")
            r = subprocess.run([sys.executable, str(pyz), "demo", "--out", str(Path(d) / "out")], capture_output=True, text=True,
                               env={**os.environ, "PLANE_WATCH_OFFLINE": "1"}, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(len(list((Path(d) / "out" / "cards").glob("*.png"))), 4)


class NetworkFailureTests(unittest.TestCase):
    def test_unreachable_host_is_skipped_quickly(self):
        import urllib.error

        from plane_watch import http

        http._dead_hosts.clear()
        env = {k: v for k, v in os.environ.items() if k != "PLANE_WATCH_OFFLINE"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timed out")) as urlopen:
            with self.assertRaises(http.FetchError) as first:
                http.get_json("https://feed.example/a")
            with self.assertRaises(http.FetchError):
                http.get_json("https://feed.example/b")
        self.assertTrue(first.exception.network)
        self.assertEqual(urlopen.call_count, 1)  # second request never left the machine
        self.assertIn("feed.example", http.dead_hosts())
        http._dead_hosts.clear()

    def test_app_says_offline_instead_of_all_quiet(self):
        from plane_watch.app import LiveApp, parser

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(adsb.http, "get_json", side_effect=adsb.http.FetchError("x unreachable", network=True)), \
                mock.patch.object(adsb.time, "sleep"):
            app = LiveApp(parser().parse_args(["--out", d, "--udp-port", "0", "--no-browser"]))
            status = app.cycle()
        self.assertEqual(status["phase"], "offline")
        self.assertIn("feeds", status["message"])
