"""Decide why an aircraft is interesting.

Three kinds of evidence:
- the squawk code (7500 hijack, 7600 radio failure, 7700 emergency, ...)
- the ADS-B emergency status the transponder broadcasts
- keywords in ACARS message text (bomb threat, diversion, medical, ...)

ACARS text is upper case and full of abbreviations, so the patterns below
include the common ones (DVRT, ATB, PAX, MEDLINK, ...).
"""

from __future__ import annotations

import re

from .models import AcarsMessage, Aircraft, Flag

# severity: 4 critical, 3 high, 2 elevated, 1 notable
CATEGORIES: dict[str, dict] = {
    "hijack": {"label": "HIJACK / UNLAWFUL INTERFERENCE", "severity": 4, "color": "#ff2d55"},
    "bomb": {"label": "BOMB THREAT", "severity": 4, "color": "#ff2d55"},
    "security": {"label": "SECURITY THREAT", "severity": 3, "color": "#ff6b2d"},
    "emergency": {"label": "GENERAL EMERGENCY", "severity": 3, "color": "#ff3b30"},
    "fire": {"label": "FIRE / SMOKE", "severity": 3, "color": "#ff6b2d"},
    "downed": {"label": "AIRCRAFT DOWNED", "severity": 4, "color": "#ff2d55"},
    "technical": {"label": "TECHNICAL FAILURE", "severity": 2, "color": "#ff9f0a"},
    "medical": {"label": "MEDICAL EMERGENCY", "severity": 2, "color": "#ff9f0a"},
    "fuel": {"label": "FUEL EMERGENCY", "severity": 2, "color": "#ff9f0a"},
    "diversion": {"label": "DIVERTED", "severity": 2, "color": "#ffd60a"},
    "radio": {"label": "RADIO FAILURE", "severity": 2, "color": "#ffd60a"},
    "lost_link": {"label": "UAV LOST LINK", "severity": 1, "color": "#64d2ff"},
    "intercept": {"label": "MILITARY INTERCEPT", "severity": 1, "color": "#64d2ff"},
    "watch": {"label": "WATCHED AIRCRAFT", "severity": 0, "color": "#5ac8a8"},
}

SQUAWKS = {
    "7500": ("hijack", "Squawk 7500: unlawful interference (hijack)"),
    "7600": ("radio", "Squawk 7600: radio communication failure"),
    "7700": ("emergency", "Squawk 7700: general emergency"),
    "7400": ("lost_link", "Squawk 7400: UAV lost control link"),
    "7777": ("intercept", "Squawk 7777: military intercept (US)"),
}
DEFAULT_SQUAWKS = ("7500", "7600", "7700")

ADSB_EMERGENCY = {
    "general": "emergency",
    "lifeguard": "medical",
    "minfuel": "fuel",
    "nordo": "radio",
    "unlawful": "hijack",
    "downed": "downed",
}

_NOT_PLANNING = r"(?!\s*(?:FUEL|FL\b|ALT|ALTN|ALTERNATE|PLAN|WX|WEATHER|MINIMA|AIRPORT LIST))"

# (category, pattern). Order matters only for which category claims a match first.
KEYWORDS: list[tuple[str, str]] = [
    ("hijack", r"\bHI-?JACK\w*|\bUNLAWFUL INTERFERENCE\b|\bSQUAWK(?:ING)? 7500\b|\bSKYJACK\w*"),
    ("bomb", r"\bBOMB\w*|\bIED\b|\bEXPLOSIVE(?:S| DEVICE)?\b|\bSUSPICIOUS (?:DEVICE|PACKAGE|ITEM|BAG|OBJECT)\b"),
    ("security", r"\bSECURITY (?:THREAT|ISSUE|INCIDENT|CONCERN|ALERT)\b|\bTHREAT (?:AGAINST|TO THE|RECEIVED|NOTE|ASSESSMENT)\b"
                 r"|\bUNRULY\b|\bDISRUPTIVE (?:PAX|PASSENGER)\b|\bRESTRAIN(?:ED|TS)\b|\b(?:LEO|POLICE|LAW ENFORCEMENT) (?:MEET|TO MEET|REQ\w*)\b"
                 r"|\bAIR ?MARSHAL\b|\bFLIGHT DECK BREACH\b|\bWEAPON\b|\bSTOWAWAY\b"),
    ("emergency", r"\bMAYDAY\b|\bPAN[ -]?PAN\b|\bDECLARED? (?:AN )?EMERGENCY\b|\bEMERGENCY (?:DECLARED|LANDING|DESCENT)\b"
                  r"|\bSQUAWK(?:ING)? 7700\b|\bEMER\b"),
    ("fire", r"\bSMOKE\b|\bFIRE\b(?! (?:TEST|EXT\w* TEST|LOOP TEST|DRILL))|\bFUMES\b|\bBURNING (?:SMELL|ODOR|ODOUR)\b"),
    ("technical", r"\bENG(?:INE)? (?:FAIL\w*|SHUT ?DOWN|SHUT|FIRE\b(?! (?:TEST|DRILL|LOOP)))\b|\bIFSD\b|\bHYD(?:RAULIC)? (?:FAIL\w*|LOSS|LEAK)\b"
                  r"|\bDEPRESS\w*|\bPRESSURI[SZ]ATION (?:FAIL\w*|LOSS|PROBLEM)\b|\bRAPID DECOMP\w*|\bGEAR (?:PROBLEM|UNSAFE|ISSUE)\b"
                  r"|\bBIRD ?STRIKE\b|\bTAIL ?STRIKE\b|\bLIGHTNING STRIKE\b|\bFLAP (?:PROBLEM|FAIL\w*|ASYM\w*)\b"),
    ("medical", r"\bMEDICAL\b|\bMED ?LINK\b|\bMEDAIRE\b|\bSTAT-?MD\b|\bDR ON BOARD\b|\bDOCTOR\b|\bPAX (?:ILL|SICK|UNCONSCIOUS|DECEASED)\b"
                r"|\bCARDIAC\b|\bHEART ATTACK\b|\bUNCONSCIOUS\b|\bSEIZURE\b|\bSTROKE\b|\bNOT BREATHING\b|\bAMBULANCE\b|\bPARAMEDIC\w*|\bDEFIB\w*"),
    ("fuel", r"\bMIN(?:IMUM)? FUEL\b|\bLOW FUEL\b|\bFUEL (?:EMERGENCY|LEAK|CRITICAL)\b|\bMAYDAY FUEL\b"),
    ("diversion", r"\bDIVERT(?:ING|ED|S)?\b" + _NOT_PLANNING + r"|\bDIVERSION\b" + _NOT_PLANNING + r"|\bDVRT\w*|\bDIV TO\b"
                  r"|\bAIR ?TURN ?BACK\b|\bATB\b|\bRETURN(?:ING)? TO (?:FIELD|GATE|STAND|ORIGIN|BASE|DEP\w*)\b|\bRTF\b|\bTURNBACK\b"),
    ("radio", r"\bNORDO\b|\bLOST COMM\w*|\bRADIO FAIL\w*|\bCOMM?S? FAIL\w*|\bSQUAWK(?:ING)? 7600\b"),
]
_COMPILED = [(cat, re.compile(p)) for cat, p in KEYWORDS]

# 3-4 letter tokens right after a diversion verb that are words, not airports.
_STOP = {"THE", "AND", "FOR", "DUE", "WITH", "BACK", "OUR", "ALTN", "FUEL", "PLAN", "NOW", "TO", "ARR", "DEP",
         "ETA", "WILL", "MAY", "PAX", "MED", "WX", "NEAR", "INTO", "FROM", "AREA", "CREW", "BASE", "GATE", "THIS"}
_DIVERT_TARGET = re.compile(
    r"\b(?:DIVERT(?:ING|ED)?|DVRT\w*|DIVERSION|DIV|RETURN(?:ING)?|LANDING|LAND|PROCEED(?:ING)?)"
    r"(?:\s+(?:TO|INTO|AT|FOR|IN))?\s+([A-Z]{3,4})\b"
)


def severity(category: str) -> int:
    return CATEGORIES.get(category, {"severity": 0})["severity"]


def classify_aircraft(ac: Aircraft, squawks: tuple[str, ...] = DEFAULT_SQUAWKS) -> list[Flag]:
    flags: list[Flag] = []
    if ac.squawk in squawks and ac.squawk in SQUAWKS:
        cat, text = SQUAWKS[ac.squawk]
        flags.append(Flag(cat, text, "squawk", ac.seen_ts, matched=ac.squawk))
    em = (ac.emergency or "").lower()
    if em in ADSB_EMERGENCY:
        cat = ADSB_EMERGENCY[em]
        if not any(f.category == cat for f in flags):
            flags.append(Flag(cat, f"ADS-B emergency status: {em}", "adsb-emergency", ac.seen_ts, matched=em))
    return flags


def classify_text(text: str) -> list[tuple[str, str]]:
    """Return [(category, matched words), ...] for one ACARS message text."""
    up = (text or "").upper()
    hits: list[tuple[str, str]] = []
    for cat, rx in _COMPILED:
        m = rx.search(up)
        if m and not any(c == cat for c, _ in hits):
            hits.append((cat, m.group(0).strip()))
    return hits


def divert_target(text: str, airport_lookup=None) -> str:
    """The airport code a diversion message names, if any (validated against airport_lookup)."""
    for m in _DIVERT_TARGET.finditer((text or "").upper()):
        code = m.group(1)
        if code in _STOP:
            continue
        if airport_lookup is None or airport_lookup(code):
            return code
    return ""


def classify_message(msg: AcarsMessage, airport_lookup=None) -> list[Flag]:
    flags = []
    for cat, matched in classify_text(msg.text):
        flag = Flag(cat, excerpt(msg.text, matched), "acars", msg.ts, matched=matched)
        if cat == "diversion":
            flag.divert_to = divert_target(msg.text, airport_lookup)
        flags.append(flag)
    return flags


def excerpt(text: str, matched: str, width: int = 140) -> str:
    flat = " ".join((text or "").split())
    i = flat.upper().find(matched.upper()) if matched else -1
    if len(flat) <= width or i < 0:
        return flat[:width] + ("…" if len(flat) > width else "")
    start = max(0, i - width // 3)
    end = min(len(flat), start + width)
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


# What goes on the overview map: the transponder says emergency, or ACARS says something critical.
EMERGENCY_SQUAWK_CATEGORIES = {"hijack", "radio", "emergency"}  # 7500, 7600, 7700
EMERGENCY_ACARS_CATEGORIES = {"hijack", "bomb", "emergency", "fire", "downed"}


def is_emergency(alert) -> bool:
    if getattr(alert, "ground_test", False):
        return False
    for f in alert.flags:
        if f.origin == "squawk" and f.category in EMERGENCY_SQUAWK_CATEGORIES:
            return True
        if f.origin == "adsb-emergency":
            return True
        if f.origin == "acars" and f.category in EMERGENCY_ACARS_CATEGORIES:
            return True
    return False
