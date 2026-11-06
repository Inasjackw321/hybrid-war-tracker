"""Made-up sample alerts, for `python -m plane_watch demo` and the tests.

Every callsign starts with DEMO and the cards are stamped SAMPLE DATA, so a
demo picture can't be mistaken for a real event.
"""

from __future__ import annotations

import math

from . import classify, geo
from .models import AcarsMessage, Aircraft, Alert, TrackPoint

T0 = 1793936000.0  # 2026-11-06 ~04:13 UTC


def _track(waypoints, start_ts, minutes):
    """waypoints: [(lat, lon, alt_ft), ...] -> TrackPoints every minute."""
    pts = []
    legs = list(zip(waypoints, waypoints[1:]))
    per_leg = max(1, minutes // len(legs))
    t = start_ts
    for (la1, lo1, a1), (la2, lo2, a2) in legs:
        for i in range(per_leg):
            f = i / per_leg
            wobble = 0.03 * math.sin(i / 4)
            pts.append(TrackPoint(t, la1 + (la2 - la1) * f + wobble, lo1 + (lo2 - lo1) * f, int(a1 + (a2 - a1) * f)))
            t += 60
    la, lo, a = waypoints[-1]
    pts.append(TrackPoint(t, la, lo, a))
    return pts


def _heading(track):
    a, b = track[-2], track[-1]
    return math.degrees(math.atan2(math.radians(b.lon - a.lon) * math.cos(math.radians(b.lat)), math.radians(b.lat - a.lat))) % 360


def _alert(ac, msgs, route=None):
    flags = classify.classify_aircraft(ac)
    for m in msgs:
        flags += classify.classify_message(m, airport_lookup=geo.airport)
    alert = Alert(ac, flags, msgs, route=route)
    for f in flags:
        if f.divert_to and not alert.divert_airport:
            alert.divert_airport = geo.airport(f.divert_to)
    return alert


def sample_alerts() -> list[Alert]:
    # 1) 7700 + medical over the Indian Ocean, diverting to Male
    tr = _track([(25.25, 55.36, 0), (24.6, 57.2, 24000), (19.0, 62.5, 37000), (11.0, 68.0, 37000), (6.6, 71.6, 31000), (5.0, 72.6, 14500)], T0 - 4 * 3600, 235)
    ac1 = Aircraft("896ABC", "DEMO771", "A6-DMO", "B77W", "BOEING 777-300ER", "Sample Airways", tr[-1].lat, tr[-1].lon, 14500,
                   speed_kt=412, heading=_heading(tr), vrate_fpm=-1800, squawk="7700", emergency="general", seen_ts=tr[-1].ts,
                   source="adsb.lol", track=tr)
    m1 = AcarsMessage(tr[-20].ts, "MEDICAL EMERGENCY ROW 41 PAX UNCONSCIOUS CARDIAC ARREST SUSPECTED. MEDLINK CONSULTED. "
                      "DIVERTING TO VRMM ETA 0455Z. REQ AMBULANCE AT STAND.", "A6-DMO", "DEMO771", "896ABC", "H1", "dumpvdl2", "136.975")
    a1 = _alert(ac1, [m1], route={"codes": "OMDB-YMML", "origin": geo.airport("OMDB"), "destination": geo.airport("YMML"), "plausible": True})

    # 2) ACARS bomb threat, diverting to Sofia
    tr2 = _track([(41.26, 28.74, 0), (41.6, 27.6, 21000), (42.4, 25.8, 36000), (42.7, 24.6, 23000)], T0 - 55 * 60, 54)
    ac2 = Aircraft("4B8DEF", "DEMO402", "TC-DMO", "A20N", "AIRBUS A-320neo", "Sample Air", tr2[-1].lat, tr2[-1].lon, 23000,
                   speed_kt=388, heading=_heading(tr2), vrate_fpm=-2400, squawk="7700", seen_ts=tr2[-1].ts, source="airplanes.live", track=tr2)
    m2 = AcarsMessage(tr2[-12].ts, "SECURITY: BOMB THREAT NOTE FOUND IN AFT LAV. CAPT DECLARED EMERGENCY. "
                      "DIVERTING TO LBSF. REQ LEO MEET ON ARRIVAL. SOB 180", "TC-DMO", "DEMO402", "4B8DEF", "5Z", "acarsdec", "131.725")
    a2 = _alert(ac2, [m2], route={"codes": "LTFM-EGLL", "origin": geo.airport("LTFM"), "destination": geo.airport("EGLL"), "plausible": True})

    # 3) 7600 over the Gulf of Mexico
    tr3 = _track([(29.98, -95.34, 0), (28.6, -93.2, 18000), (27.4, -90.4, 33000), (27.9, -88.1, 33000)], T0 - 70 * 60, 70)
    ac3 = Aircraft("A1B2C3", "DEMO19", "N19DM", "B738", "BOEING 737-800", "", tr3[-1].lat, tr3[-1].lon, 33000,
                   speed_kt=455, heading=_heading(tr3), squawk="7600", emergency="nordo", seen_ts=tr3[-1].ts, source="adsb.fi", track=tr3)
    a3 = _alert(ac3, [])
    return [a1, a2, a3]


SAMPLE_ACARS_JSONL = "\n".join([
    # acarsdec
    '{"timestamp":1793935000.5,"station_id":"DEMO-1","channel":2,"freq":131.725,"level":-12.3,"error":0,"mode":"2",'
    '"label":"5Z","block_id":"2","ack":false,"tail":".TC-DMO","flight":"DEMO402","msgno":"M12A",'
    '"text":"SECURITY: BOMB THREAT NOTE FOUND IN AFT LAV. DIVERTING TO LBSF"}',
    # dumpvdl2
    '{"vdl2":{"app":{"name":"dumpvdl2","ver":"2.3.0"},"t":{"sec":1793935500,"usec":0},"freq":136975000,'
    '"avlc":{"src":{"addr":"896ABC","type":"Aircraft","status":"Airborne"},"dst":{"addr":"10916D","type":"Ground station"},'
    '"acars":{"err":false,"crc_ok":true,"more":false,"reg":".A6-DMO","mode":"2","label":"H1","blk_id":"5","ack":"!",'
    '"flight":"DEMO771","msg_num":"F09","msg_num_seq":"A","msg_text":"PAX UNCONSCIOUS MEDLINK ADVISED DIVERT TO VRMM"}}}}',
    # dumphfdl
    '{"hfdl":{"app":{"name":"dumphfdl"},"t":{"sec":1793935600,"usec":0},"freq":8927000,'
    '"lpdu":{"src":{"type":"Aircraft","id":12},"ac_info":{"icao":"A1B2C3"},'
    '"hfnpdu":{"acars":{"reg":".N19DM","flight":"DEMO19","label":"80","msg_text":"FUEL STATUS NORMAL ETA 0610"}}}}}',
    # noise / non-JSON line
    "acarsdec started",
])
