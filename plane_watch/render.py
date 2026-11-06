"""Draw alert cards: info panel on the left, map with the track on the right.

Everything is laid out in "design pixels" on an 800x450 canvas and rendered
at SCALE (2 -> 1600x900 PNG).
"""

from __future__ import annotations

import io
import math
import textwrap
from datetime import datetime, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.collections import LineCollection, PolyCollection  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402
from matplotlib.patches import FancyBboxPatch, Rectangle  # noqa: E402
from matplotlib.path import Path as MPath  # noqa: E402

from . import basemap, geo  # noqa: E402
from .classify import CATEGORIES, SQUAWKS  # noqa: E402
from .models import Alert, Flag  # noqa: E402

W, H = 800, 450
ROUTE_SHOW_KM = 1200  # show planned origin/destination on the map when this close
SCALE = 2
HEADER_H, FOOTER_H, PANEL_W = 36, 22, 203
MAP_X0 = PANEL_W + 4

C = {
    "bg": "#12161b", "header": "#1a1e24", "panel": "#171b21", "ocean": "#161b22", "land": "#272c34",
    "coast": "#3b424c", "text": "#f2f4f7", "muted": "#9aa3ad", "faint": "#6b747e", "city": "#d5dbe1",
    "footer": "#b3bf7a", "rule": "#2c323a",
}
ALT_STOPS = [0, 4000, 8000, 20000, 40000]
ALT_COLORS = ["#ff8c1a", "#ffd21a", "#35d63a", "#1fa8ff", "#ff2bf0"]
ALT_CMAP = LinearSegmentedColormap.from_list("alt", list(zip([0, 0.25, 0.5, 0.75, 1.0], ALT_COLORS)))

# aircraft silhouette pointing north, nose at (0, 1)
_HALF = [(0, 1.0), (0.08, 0.86), (0.09, 0.32), (0.92, -0.04), (0.92, -0.17), (0.09, 0.03),
         (0.07, -0.55), (0.34, -0.76), (0.34, -0.87), (0.0, -0.78)]
PLANE = _HALF + [(-x, y) for x, y in reversed(_HALF[1:-1])]


def fs(px: float) -> float:
    """Font size in points for a design-pixel height (100 dpi, SCALE)."""
    return px * SCALE * 0.72


def alt_frac(alt) -> float:
    if alt is None:
        return 0.75
    alt = max(0, min(ALT_STOPS[-1], alt))
    for i in range(len(ALT_STOPS) - 1):
        a, b = ALT_STOPS[i], ALT_STOPS[i + 1]
        if alt <= b:
            return (i + (alt - a) / (b - a)) / (len(ALT_STOPS) - 1)
    return 1.0


def plane_marker(heading: float | None) -> MPath:
    t = math.radians(heading or 0)
    verts = [(x * math.cos(t) + y * math.sin(t), -x * math.sin(t) + y * math.cos(t)) for x, y in PLANE]
    return MPath(verts + [verts[0]], [MPath.MOVETO] + [MPath.LINETO] * (len(verts) - 1) + [MPath.CLOSEPOLY])


def nice_desc(desc: str) -> str:
    """'BOEING 777-300ER' -> 'Boeing 777-300ER'; mixed-case text is left alone."""
    if not desc.isupper():
        return desc
    return " ".join(w.capitalize() if w.isalpha() and len(w) > 2 else w for w in desc.split())


def _hhmm(ts) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M UTC") if ts else "—"


def _badge_text(alert: Alert) -> str:
    top = alert.top
    cat = CATEGORIES[top.category]
    if top.origin == "squawk":
        return f"SQUAWK {top.matched} · {cat['label']}"
    if top.origin == "acars":
        return f"ACARS · {cat['label']}"
    return cat["label"]


# ------------------------------------------------------------------ map extent


def _unwrap(lons: list[float]) -> list[float]:
    out = []
    for lon in lons:
        if out:
            while lon - out[-1] > 180:
                lon -= 360
            while lon - out[-1] < -180:
                lon += 360
        out.append(lon)
    return out


def _extent(points: list[tuple[float, float]], aspect: float, min_span_deg: float = 12.0):
    """Projected (x0, x1, y0, y1) covering the points with padding, matching aspect (w/h)."""
    xs = [math.radians(lon) for lon, _ in points]
    ys = [geo.merc_y(lat) for _, lat in points]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    w = max(max(xs) - min(xs), math.radians(min_span_deg)) * 1.35
    h = max(max(ys) - min(ys), 1e-6) * 1.45
    if w / h < aspect:
        w = h * aspect
    else:
        h = w / aspect
    return cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2


# ------------------------------------------------------------------ drawing


class Card:
    def __init__(self, title: str, brand: str):
        self.fig = plt.figure(figsize=(W * SCALE / 100, H * SCALE / 100), dpi=100, facecolor=C["bg"])
        self.ui = self.fig.add_axes([0, 0, 1, 1], zorder=0)
        self.ui.set_xlim(0, W)
        self.ui.set_ylim(H, 0)
        self.ui.axis("off")
        self.title, self.brand = title, brand
        self._renderer = None

    @property
    def renderer(self):
        if self._renderer is None:
            self.fig.canvas.draw()
            self._renderer = self.fig.canvas.get_renderer()
        return self._renderer

    def text(self, x, y, s, px=8, color=None, weight="normal", ha="left", va="baseline", ax=None, **kw):
        ax = ax or self.ui
        return ax.text(x, y, s, fontsize=fs(px), color=color or C["text"], fontweight=weight, ha=ha, va=va, **kw)

    def rect(self, x, y, w, h, color, ax=None, **kw):
        (ax or self.ui).add_patch(Rectangle((x, y), w, h, facecolor=color, edgecolor="none", **kw))

    def chrome(self, accent: str, footer_left: str, footer_right: str, sample: bool = False):
        self.rect(0, 0, W, HEADER_H, C["header"])
        if sample:
            self.text(W - 12, 22, "SAMPLE DATA — NOT A REAL EVENT", px=7.5, weight="bold", color="#ff453a", ha="right",
                      bbox=dict(boxstyle="round,pad=0.35", fc="none", ec="#ff453a", lw=1.2))
        self.rect(0, HEADER_H, PANEL_W, H - HEADER_H - FOOTER_H, C["panel"])
        self.rect(PANEL_W + 1.5, HEADER_H, 2, H - HEADER_H - FOOTER_H, accent)
        # logo: rounded tile with a tiny plane
        self.ui.add_patch(FancyBboxPatch((8, 5), 26, 26, boxstyle="round,pad=0,rounding_size=4", fc="#26303c", ec="#3a4756", lw=1))
        self.ui.plot([21], [18], marker=plane_marker(45), markersize=fs(13), color=accent, markeredgewidth=0)
        self.text(42, 22, self.brand, px=7.5, color=C["muted"])
        brand_w = len(self.brand) * 4.1
        self.text(42 + brand_w + 6, 24.5, self.title, px=13, weight="bold")
        self.text(10, H - 7, footer_left, px=6.6, color=C["footer"])
        self.text(W - 10, H - 7, footer_right, px=6.6, color=C["text"], ha="right")

    def save(self, path):
        self.fig.savefig(path, dpi=100, facecolor=C["bg"])
        plt.close(self.fig)


def _draw_photo(card: Card, alert: Alert, x, y, w, h, photo_bytes: bytes | None):
    ax = card.fig.add_axes([x / W, 1 - (y + h) / H, w / W, h / H], zorder=2)
    ax.set_axis_off()
    img = None
    if photo_bytes:
        try:
            import matplotlib.image as mpimg

            img = mpimg.imread(io.BytesIO(photo_bytes), format="jpg")
        except Exception:  # noqa: BLE001 - any decode error -> placeholder
            img = None
    if img is not None:
        ih, iw = img.shape[:2]
        target = w / h
        if iw / ih > target:  # too wide: crop sides
            nw = int(ih * target)
            img = img[:, (iw - nw) // 2:(iw - nw) // 2 + nw]
        else:
            nh = int(iw / target)
            img = img[(ih - nh) // 2:(ih - nh) // 2 + nh]
        ax.imshow(img, aspect="auto", interpolation="antialiased")
    else:
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.add_patch(Rectangle((0, 0), 1, 1, fc="#1f252d", ec="#2f3742"))
        ax.plot([0.5], [0.58], marker=plane_marker(90), markersize=fs(46), color="#3a4554", markeredgewidth=0)
        ax.text(0.5, 0.16, "NO PHOTO", ha="center", fontsize=fs(7), color=C["faint"], fontweight="bold")


def _panel(card: Card, alert: Alert, photo_bytes: bytes | None):
    ac = alert.aircraft
    cat = CATEGORIES[alert.top.category]
    x0, xr = 11, PANEL_W - 9
    card.text(x0 + 2, 55, _badge_text(alert), px=6.4, color="#111", weight="bold",
              bbox=dict(boxstyle="round,pad=0.35,rounding_size=0.25", fc=cat["color"], ec="none"))
    ident = ac.callsign or ac.registration or ac.hex
    card.text(x0, 84, ident, px=17, weight="bold")
    cc, country = geo.country_for_hex(ac.hex)
    if cc:
        card.text(xr, 79, cc, px=8, weight="bold", ha="right", color=C["text"],
                  bbox=dict(boxstyle="round,pad=0.3,rounding_size=0.3", fc="none", ec=C["muted"], lw=1))
    desc = ac.description or ac.type_code or "Unknown type"
    card.text(x0, 103, textwrap.shorten(nice_desc(desc), 34, placeholder="…"), px=8, color="#d9dee4")
    sub = " · ".join(s for s in (ac.operator, country, "Military" if ac.military else "") if s)
    card.text(x0, 117, textwrap.shorten(sub or "Operator unknown", 40, placeholder="…"), px=6.8, color=C["muted"])

    _draw_photo(card, alert, x0, 128, xr - x0, 112, photo_bytes)
    if alert.photo:
        card.text(x0, 250, textwrap.shorten(f"Photo: {alert.photo.get('photographer') or 'unknown'} / planespotters.net", 46, placeholder="…"),
                  px=5.8, color=C["muted"])
        if alert.photo.get("same_type"):
            card.text(x0, 260, "Same type — not this airframe", px=5.8, color="#e8c547")
    card.rect(x0, 268, xr - x0, 0.8, C["rule"])

    sq = ac.squawk or "—"
    rows = [
        ("ALTITUDE", "GROUND" if ac.on_ground else (f"{ac.alt_ft:,} ft" if ac.alt_ft is not None else "—"), None),
        ("SPEED", f"{ac.speed_kt:.0f} kt" if ac.speed_kt is not None else "—", None),
        ("HEADING", f"{ac.heading:.0f}°" if ac.heading is not None else "—", None),
        ("SQUAWK", sq, CATEGORIES[SQUAWKS[sq][0]]["color"] if sq in SQUAWKS else None),
        ("HEX", ac.hex, None),
        ("REG · TYPE", " · ".join(s for s in (ac.registration, ac.type_code) if s) or "—", None),
        ("TRACK START", _hhmm(ac.track[0].ts) if ac.track else "—", None),
        ("SEEN", _hhmm(ac.seen_ts), None),
    ]
    y = 284
    for label, value, color in rows:
        card.text(x0, y, label, px=5.6, color=C["muted"])
        card.text(xr, y, value, px=7.6, weight="bold", ha="right", color=color or C["text"])
        y += 18.2


def _map(card: Card, alert: Alert, sources: dict):
    ac = alert.aircraft
    cat = CATEGORIES[alert.top.category]
    mx0, my0, mw, mh = MAP_X0, HEADER_H, W - MAP_X0, H - HEADER_H - FOOTER_H
    ax = card.fig.add_axes([mx0 / W, FOOTER_H / H, mw / W, mh / H], zorder=1)
    ax.set_facecolor(C["ocean"])
    ax.set_xticks([])
    ax.set_yticks([])
    for s in ax.spines.values():
        s.set_visible(False)

    track = ac.track[:]
    lons = _unwrap([p.lon for p in track] + ([ac.lon] if ac.lon is not None else []))
    lats = [p.lat for p in track] + ([ac.lat] if ac.lat is not None else [])
    pos = (lons[-1], lats[-1]) if lons else (0.0, 0.0)

    def near(lon):  # bring a longitude next to the aircraft's
        while lon - pos[0] > 180:
            lon -= 360
        while lon - pos[0] < -180:
            lon += 360
        return lon

    pts = list(zip(lons, lats)) or [pos]
    extras = []
    if alert.divert_airport:
        d = alert.divert_airport
        extras.append(("divert", near(d["lon"]), d["lat"], d))
    if alert.route:
        for key in ("origin", "destination"):
            a = alert.route.get(key) or {}
            if a.get("lat") is not None and geo.haversine_km(pos[1], pos[0], a["lat"], a["lon"]) < ROUTE_SHOW_KM:
                extras.append((key, near(a["lon"]), a["lat"], a))
    ext = _extent(pts + [(e[1], e[2]) for e in extras], mw / mh)
    x0, x1, y0, y1 = ext
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    px_per_x = mw / (x1 - x0)  # design px per projected unit

    def P(lon, lat):
        return math.radians(lon), geo.merc_y(lat)

    # land
    outers, holes, _ = basemap.land()
    lon_lo, lon_hi = math.degrees(x0), math.degrees(x1)
    lat_lo, lat_hi = geo.inv_merc_y(y0), geo.inv_merc_y(y1)

    def polys(rings):
        out = []
        for ring in rings:
            rl = [p[0] for p in ring]
            rt = [p[1] for p in ring]
            if max(rt) < lat_lo or min(rt) > lat_hi:
                continue
            for shift in (-360, 0, 360):
                if max(rl) + shift < lon_lo or min(rl) + shift > lon_hi:
                    continue
                out.append([P(lo + shift, la) for lo, la in ring])
        return out

    ax.add_collection(PolyCollection(polys(outers), facecolors=C["land"], edgecolors=C["coast"], linewidths=0.7 * SCALE * 0.5, zorder=1))
    if holes:
        ax.add_collection(PolyCollection(polys(holes), facecolors=C["ocean"], edgecolors=C["coast"], linewidths=0.6 * SCALE * 0.5, zorder=1))

    def to_px(lon, lat):
        x, y = P(lon, lat)
        return (x - x0) * px_per_x, (y1 - y) * px_per_x  # px from map top-left

    track_px = [to_px(lo, la) for lo, la in zip(lons, lats)]
    apx, apy = to_px(*pos)
    extra_px = [to_px(lon, lat) for _, lon, lat, _ in extras]
    divert = next((e for e in extras if e[0] == "divert"), None)
    if divert:  # the dashed line to the diversion airport
        ex, ey = to_px(divert[1], divert[2])
        extra_px += [(apx + (ex - apx) * k / 12, apy + (ey - apy) * k / 12) for k in range(1, 12)]
    blocked = _signals(card, ax, alert, mw, mh, track_px + extra_px + [(apx, apy)] * 25)

    # airport labels go on the side away from the aircraft; reserve their space
    extra_labels = []
    for kind, lon, lat, a in extras:
        code = a.get("icao") or a.get("iata") or ""
        ex, ey = to_px(lon, lat)
        if kind == "divert":
            name = textwrap.shorten(a.get("name") or a.get("location") or "", 26, placeholder="…")
            dist = geo.haversine_km(pos[1], pos[0], lat, lon)
            text, size = f"DIVERT → {code}  {name}\n{dist:,.0f} km", 6.4
        else:
            place = textwrap.shorten(a.get("city") or a.get("location") or "", 16, placeholder="…")
            text, size = ("FROM " if kind == "origin" else "TO ") + code + (f" · {place}" if place else ""), 6
        side = "right" if ex < apx else "left"  # = the text's horizontal alignment
        w = max(len(t) for t in text.split("\n")) * size * 0.62 + 12
        h = size * 1.5 * (text.count("\n") + 1) + 6
        blocked.append((ex - w, ey - 2, ex + 4, ey + h) if side == "right" else (ex - 4, ey - 2, ex + w, ey + h))
        extra_labels.append((text, side))

    desc = textwrap.shorten(nice_desc(ac.description or ac.type_code or ac.label), 30, placeholder="…")
    label_text = desc if alert.top.category == "watch" else f"{desc}\n{_badge_text(alert)}"
    label_pos, label_rect = _place_label(label_text, apx, apy, mw, mh, track_px + extra_px * 3, blocked)
    blocked.append(label_rect)

    # cities: biggest first, keep labels apart and off the aircraft, track, airports and overlays
    taken = [(apx, apy)] + [(ex - 30, ey) for ex, ey in extra_px[: len(extras)]]
    shown = 0
    for name, lat, lon, _rank in basemap.cities():
        if shown >= 16:
            break
        lon = near(lon)
        if not (lon_lo < lon < lon_hi and lat_lo < lat < lat_hi):
            continue
        cx, cy = to_px(lon, lat)
        if cx > mw - 70 or cy < 30 or cy > mh - 34:
            continue
        if any(abs(cx - tx) < 62 and abs(cy - ty) < 16 for tx, ty in taken):
            continue
        if any(bx0 - 4 < cx < bx1 + 4 and by0 - 4 < cy < by1 + 10 for bx0, by0, bx1, by1 in blocked):
            continue
        if any(cx - 6 < tx < cx + 8 + 4.2 * len(name) and cy - 12 < ty < cy + 6 for tx, ty in track_px[::2]):
            continue
        taken.append((cx, cy))
        x, y = P(lon, lat)
        ax.plot([x], [y], "o", ms=2.2 * SCALE * 0.72, color=C["city"], zorder=3)
        ax.annotate(name, (x, y), xytext=(4 * SCALE * 0.72, 3 * SCALE * 0.72), textcoords="offset points",
                    fontsize=fs(6.6), color=C["city"], zorder=3)
        shown += 1

    # planned route airports, diversion target
    for (kind, lon, lat, a), (text, side) in zip(extras, extra_labels):
        x, y = P(lon, lat)
        k = 1 if side == "left" else -1
        if kind == "divert":
            ax.plot([P(*pos)[0], x], [P(*pos)[1], y], ls=(0, (4, 3)), lw=1.4 * SCALE * 0.72, color=cat["color"], zorder=4)
            ax.plot([x], [y], marker="^", ms=6 * SCALE * 0.72, color=cat["color"], mec="#111", mew=0.8, zorder=6)
            ax.annotate(text, (x, y), xytext=(9 * k, -5), textcoords="offset points", fontsize=fs(6.4), color=cat["color"],
                        fontweight="bold", va="top", ha=side, zorder=6, bbox=dict(boxstyle="round,pad=0.25", fc="#0d1014", ec="none", alpha=0.75))
        else:
            ax.plot([x], [y], marker="^", ms=4.5 * SCALE * 0.72, color=C["muted"], mec="#111", mew=0.6, zorder=5)
            ax.annotate(text, (x, y), xytext=(7 * k, -3), textcoords="offset points", fontsize=fs(6), color=C["muted"],
                        va="top", ha=side, zorder=5)

    # track coloured by altitude
    if len(track) >= 2:
        xy = [P(lo, la) for lo, la in zip(lons[: len(track)], lats[: len(track)])]
        segs = [[xy[i], xy[i + 1]] for i in range(len(xy) - 1)]
        cols = [ALT_CMAP(alt_frac(track[i + 1].alt_ft)) for i in range(len(segs))]
        ax.add_collection(LineCollection(segs, colors="#000000", linewidths=3.6 * SCALE * 0.72, alpha=0.55, zorder=7, capstyle="round"))
        ax.add_collection(LineCollection(segs, colors=cols, linewidths=2.0 * SCALE * 0.72, zorder=8, capstyle="round"))
        ax.plot([xy[0][0]], [xy[0][1]], "o", ms=4 * SCALE * 0.72, mfc="none", mec=cols[0], mew=1.2, zorder=8)

    # aircraft
    px, py = P(*pos)
    ax.plot([px], [py], "o", ms=17 * SCALE * 0.72, mfc="none", mec=cat["color"], mew=1.2, alpha=0.55, zorder=9)
    ax.plot([px], [py], marker=plane_marker(ac.heading), ms=13 * SCALE * 0.72, color=cat["color"], mec="#ffffff", mew=0.7, zorder=10)

    # label box, on whichever side is clearest
    lx, ly, lha, lva = label_pos
    ax.annotate(label_text, (px, py), xytext=(lx * SCALE * 0.72, -ly * SCALE * 0.72), textcoords="offset points",
                ha=lha, va=lva, multialignment="center", fontsize=fs(7.2), fontweight="bold", color="#14181d", zorder=11, linespacing=1.25,
                bbox=dict(boxstyle="round,pad=0.45,rounding_size=0.3", fc="#f4f5ef", ec=cat["color"], lw=1.2))

    _scale_bar(ax, ext, mw, mh)
    _alt_legend(ax, mw, mh)


def _signals(card: Card, ax, alert: Alert, mw, mh, avoid_px) -> list[tuple]:
    """Chips for every kind of flag, plus the most relevant ACARS message.
    Returns the rectangles (design px from map top-left) it covered."""
    def axc(px, py):  # design px from map top-left -> axes fraction
        return px / mw, 1 - py / mh

    blocked = []
    x = 10
    order = {"squawk": 0, "adsb-emergency": 1, "acars": 2, "route": 3}
    best: dict[str, Flag] = {}
    for f in sorted(alert.flags, key=lambda f: order.get(f.origin, 9)):
        best.setdefault(f.category, f)
    chips = sorted(best.values(), key=lambda f: -CATEGORIES[f.category]["severity"]) if len(best) > 1 else []
    for f in chips:
        chip = f"{'ACARS' if f.origin == 'acars' else 'SQK ' + f.matched if f.origin == 'squawk' else 'ADS-B'} · {CATEGORIES[f.category]['label']}"
        t = ax.text(*axc(x, 14), chip, transform=ax.transAxes, fontsize=fs(5.8), fontweight="bold", color="#111", va="center", zorder=12,
                    bbox=dict(boxstyle="round,pad=0.3,rounding_size=0.25", fc=CATEGORIES[f.category]["color"], ec="none"))
        w = t.get_window_extent(card.renderer).width / SCALE
        x += w + 10
        if x > mw - 120:
            break
    if chips:
        blocked.append((0, 0, x, 24))

    msg_flag = next((f for f in sorted(alert.flags, key=lambda f: -CATEGORIES[f.category]["severity"]) if f.origin == "acars"), None)
    if not msg_flag:
        return blocked
    msg = next((m for m in alert.messages if m.ts == msg_flag.ts), alert.messages[0] if alert.messages else None)
    text = msg.text if msg else msg_flag.evidence
    flat = " ".join(text.split())
    lines = textwrap.wrap(flat, 60)
    if len(lines) > 5:
        # keep the lines around the match
        i = next((n for n, ln in enumerate(lines) if msg_flag.matched and msg_flag.matched.upper() in ln.upper()), 0)
        start = max(0, min(i - 1, len(lines) - 5))
        lines = (["…"] if start else []) + lines[start:start + 5] + (["…"] if start + 5 < len(lines) else [])
    box_w, line_h = 268, 9.5
    box_h = 20 + line_h * len(lines) + 4
    top = 28 if chips else 10
    corners = [(10, top), (mw - 10 - box_w, top), (10, mh - 36 - box_h), (mw - 10 - box_w, mh - 40 - box_h)]

    def overlap(c):
        cx, cy = c
        return sum(1 for px, py in avoid_px if cx - 20 < px < cx + box_w + 20 and cy - 20 < py < cy + box_h + 40)

    bx, by = min(corners, key=overlap)
    blocked.append((bx, by, bx + box_w, by + box_h))
    ax.add_patch(FancyBboxPatch(axc(bx, by + box_h), box_w / mw, box_h / mh, transform=ax.transAxes,
                                boxstyle="round,pad=0,rounding_size=0.008", fc="#0d1014", ec="#39414b", lw=1, alpha=0.92, zorder=12))
    head = "ACARS"
    if msg:
        bits = [_hhmm(msg.ts), msg.flight or msg.registration, f"label {msg.label}" if msg.label else "", msg.source]
        head += " · " + " · ".join(b for b in bits if b)
    ax.text(*axc(bx + 8, by + 12), head, transform=ax.transAxes, fontsize=fs(5.8), color=C["muted"], fontweight="bold", zorder=13, va="center")
    mono = dict(fontsize=fs(6.3), family="DejaVu Sans Mono", zorder=13, transform=ax.transAxes, va="center")
    probe = ax.text(0, 0, "M" * 10, **mono)
    char_w = probe.get_window_extent(card.renderer).width / 10 / SCALE
    probe.remove()
    color = CATEGORIES[msg_flag.category]["color"]
    for n, ln in enumerate(lines):
        y = by + 24 + n * line_h
        ax.text(*axc(bx + 8, y), ln, color="#d6dbe0", **mono)
        if msg_flag.matched:
            i = ln.upper().find(msg_flag.matched.upper())
            if i >= 0:
                seg = ln[i:i + len(msg_flag.matched)]
                ax.text(*axc(bx + 8 + i * char_w, y), seg, color="#111", fontweight="bold",
                        bbox=dict(boxstyle="square,pad=0.08", fc=color, ec="none"), **mono)
    return blocked


def _place_label(text: str, apx, apy, mw, mh, avoid_px, blocked):
    """Pick below/above/right/left of the aircraft, whichever covers the least.
    Returns ((dx, dy, ha, va), rect) in design px; dy grows downward."""
    lines = text.split("\n")
    bw = max(len(ln) for ln in lines) * 4.7 + 14
    bh = 11 * len(lines) + 8
    gap = 24
    cands = [
        ((0, gap, "center", "top"), (apx - bw / 2, apy + gap, apx + bw / 2, apy + gap + bh)),
        ((0, -gap, "center", "bottom"), (apx - bw / 2, apy - gap - bh, apx + bw / 2, apy - gap)),
        ((gap, 0, "left", "center"), (apx + gap, apy - bh / 2, apx + gap + bw, apy + bh / 2)),
        ((-gap, 0, "right", "center"), (apx - gap - bw, apy - bh / 2, apx - gap, apy + bh / 2)),
    ]

    def cost(rect):
        x0, y0, x1, y1 = rect
        c = sum(1 for px, py in avoid_px if x0 - 6 < px < x1 + 6 and y0 - 6 < py < y1 + 6)
        c += 1000 * (x0 < 2 or y0 < 26 or x1 > mw - 2 or y1 > mh - 34)
        c += sum(500 for b in blocked if x0 < b[2] and x1 > b[0] and y0 < b[3] and y1 > b[1])
        return c

    return min(cands, key=lambda c: cost(c[1]))


def _scale_bar(ax, ext, mw, mh):
    x0, x1, y0, y1 = ext
    lat_c = geo.inv_merc_y((y0 + y1) / 2)
    km_per_unit = 6371.0 * math.cos(math.radians(lat_c))
    km_map = (x1 - x0) * km_per_unit
    nice = next((n for n in (10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000) if n >= km_map / 6), 5000)
    frac = nice / km_map
    bx, by = 18 / mw, 14 / mh
    ax.plot([bx, bx + frac], [by, by], transform=ax.transAxes, color="#e6e9ed", lw=1.2, zorder=12, solid_capstyle="butt")
    for xx in (bx, bx + frac):
        ax.plot([xx, xx], [by - 3 / mh, by + 3 / mh], transform=ax.transAxes, color="#e6e9ed", lw=1.2, zorder=12)
    ax.text(bx + frac / 2, by + 6 / mh, f"{nice:,} km", transform=ax.transAxes, ha="center", fontsize=fs(6), color="#e6e9ed", zorder=12)


def _alt_legend(ax, mw, mh):
    seg_w, h = 29, 5
    x_end = mw - 12
    x_start = x_end - seg_w * (len(ALT_COLORS) - 1)
    y = 10
    for i in range(len(ALT_COLORS) - 1):
        for k in range(6):  # small gradient steps inside each segment
            f0 = (i + k / 6) / (len(ALT_COLORS) - 1)
            ax.add_patch(Rectangle(((x_start + seg_w * (i + k / 6)) / mw, y / mh), seg_w / 6 / mw, h / mh, transform=ax.transAxes,
                                   fc=ALT_CMAP(f0), ec="none", zorder=12))
    labels = ["0", "4k", "8k", "20k", "40k"]
    for i, lab in enumerate(labels):
        ax.text((x_start + seg_w * i) / mw, (y + h + 3) / mh, lab, transform=ax.transAxes, ha="center", fontsize=fs(5.4), color="#dfe3e8", zorder=12)
    ax.text((x_start - 8) / mw, (y + 1) / mh, "ft", transform=ax.transAxes, ha="right", fontsize=fs(5.4), color="#dfe3e8", zorder=12)


def render_card(alert: Alert, path, photo_bytes: bytes | None = None, title: str = "PLANE ALERT",
                brand: str = "hybrid war tracker", sources: dict | None = None, sample: bool = False):
    sources = sources or {}
    cat = CATEGORIES[alert.top.category]
    card = Card(title, brand)
    _, _, land_src = basemap.land()
    parts = [f"Track: {sources.get('track', 'live positions')}", f"position: {sources.get('position', alert.aircraft.source or 'ADS-B')}"]
    if alert.messages:
        parts.append(f"ACARS: {sources.get('acars', alert.messages[0].source)}")
    parts += [f"map: {land_src}", brand]
    when = datetime.fromtimestamp(alert.aircraft.seen_ts or 0, timezone.utc).strftime("%d %b %Y · %H:%M UTC")
    card.chrome(cat["color"], " · ".join(parts), when, sample)
    _panel(card, alert, photo_bytes)
    _map(card, alert, sources)
    card.save(path)
    return path


def render_overview(alerts: list[Alert], path, title: str = "PLANE ALERT", brand: str = "hybrid war tracker",
                    when_ts: float | None = None, sample: bool = False):
    """One picture of every current alert: list on the left, dots on a world map."""
    card = Card(title, brand)
    worst = max(alerts, key=lambda a: CATEGORIES[a.top.category]["severity"]) if alerts else None
    accent = CATEGORIES[worst.top.category]["color"] if worst else "#5ac8a8"
    when_ts = when_ts or max((a.aircraft.seen_ts or 0 for a in alerts), default=0) or datetime.now(timezone.utc).timestamp()
    when = datetime.fromtimestamp(when_ts, timezone.utc).strftime("%d %b %Y · %H:%M UTC")
    card.chrome(accent, f"{len(alerts)} flagged aircraft · position: ADS-B community feeds · map: Natural Earth · {brand}", when, sample)
    card.text(11, 58, "ACTIVE ALERTS", px=6.4, color="#111", weight="bold",
              bbox=dict(boxstyle="round,pad=0.35,rounding_size=0.25", fc=accent, ec="none"))
    y = 80
    ranked = sorted(alerts, key=lambda a: -CATEGORIES[a.top.category]["severity"])
    for a in ranked[:17]:
        cat = CATEGORIES[a.top.category]
        card.rect(11, y - 8, 3, 11, cat["color"])
        card.text(19, y, a.aircraft.label, px=7.6, weight="bold")
        card.text(PANEL_W - 9, y, textwrap.shorten(cat["label"], 22, placeholder="…"), px=5.6, color=cat["color"], ha="right", weight="bold")
        card.text(19, y + 9, textwrap.shorten(nice_desc(a.aircraft.description or a.aircraft.type_code or a.aircraft.hex), 36, placeholder="…"), px=5.6, color=C["muted"])
        y += 20.5
    if len(ranked) > 17:
        card.text(19, y, f"+ {len(ranked) - 17} more", px=6, color=C["muted"])
    if not alerts:
        card.text(19, 84, "Nothing flagged right now.", px=7, color=C["muted"])

    mw, mh = W - MAP_X0, H - HEADER_H - FOOTER_H
    ax = card.fig.add_axes([MAP_X0 / W, FOOTER_H / H, mw / W, mh / H], zorder=1)
    ax.set_facecolor(C["ocean"])
    ax.set_xticks([])
    ax.set_yticks([])
    for s in ax.spines.values():
        s.set_visible(False)
    pts = [(a.aircraft.lon, a.aircraft.lat) for a in alerts if a.aircraft.lat is not None]
    x0, x1, y0, y1 = _extent(pts or [(-150, -50), (170, 70)], mw / mh, min_span_deg=60 if pts else 320)
    y0, y1 = max(y0, geo.merc_y(-75)), min(y1, geo.merc_y(80))
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    outers, holes, _ = basemap.land()
    proj = []
    for ring in outers:
        for shift in (-360, 0, 360):
            r = [(math.radians(lo + shift), geo.merc_y(la)) for lo, la in ring]
            if max(p[0] for p in r) >= x0 and min(p[0] for p in r) <= x1:
                proj.append(r)
    ax.add_collection(PolyCollection(proj, facecolors=C["land"], edgecolors=C["coast"], linewidths=0.6))
    if holes:
        ax.add_collection(PolyCollection([[(math.radians(lo), geo.merc_y(la)) for lo, la in r] for r in holes],
                                         facecolors=C["ocean"], edgecolors=C["coast"], linewidths=0.5))
    for a in alerts:
        if a.aircraft.lat is None:
            continue
        cat = CATEGORIES[a.top.category]
        x, y = math.radians(a.aircraft.lon), geo.merc_y(a.aircraft.lat)
        ax.plot([x], [y], "o", ms=16, mfc="none", mec=cat["color"], mew=1.2, alpha=0.5, zorder=5)
        ax.plot([x], [y], marker=plane_marker(a.aircraft.heading), ms=13, color=cat["color"], mec="white", mew=0.6, zorder=6)
        ax.annotate(a.aircraft.label, (x, y), xytext=(9, 6), textcoords="offset points", fontsize=fs(6.4), color=C["text"],
                    fontweight="bold", zorder=7, bbox=dict(boxstyle="round,pad=0.25", fc="#0d1014", ec=cat["color"], lw=0.8, alpha=0.85))
    card.save(path)
    return path
