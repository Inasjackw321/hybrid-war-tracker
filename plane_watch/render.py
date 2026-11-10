"""Draw alert cards: info panel on the left, map with the track on the right.

Laid out in "design pixels" on an 800x450 canvas, rendered at SCALE (2 ->
1600x900 PNG). The style avoids hard edges: the panel, header and footer fade
into the map, the map has a vignette, the track and aircraft glow, and the
boxes are borderless frosted glass.
"""

from __future__ import annotations

import io
import math
import textwrap
from datetime import datetime, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.patheffects as pe  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.collections import LineCollection, PolyCollection  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap, to_rgb  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402
from matplotlib.path import Path as MPath  # noqa: E402

from . import basemap, geo  # noqa: E402
from .classify import CATEGORIES, SQUAWKS  # noqa: E402
from .models import Alert, Flag  # noqa: E402


def _pick_font(names, fallback):
    from matplotlib import font_manager

    have = {f.name for f in font_manager.fontManager.ttflist}
    return next((n for n in names if n in have), fallback)


# Use the system's own UI font when there is one (Segoe UI on Windows, SF/Helvetica on macOS): cleaner than DejaVu.
SANS = _pick_font(("Segoe UI", "Inter", "SF Pro Text", "Helvetica Neue", "Helvetica", "Arial", "Liberation Sans", "Carlito"), "DejaVu Sans")
MONO = _pick_font(("Cascadia Mono", "Consolas", "SF Mono", "Menlo", "Liberation Mono"), "DejaVu Sans Mono")
matplotlib.rcParams["font.family"] = "sans-serif"
matplotlib.rcParams["font.sans-serif"] = [SANS, "DejaVu Sans"]

WATERMARK = "@Kaldockhi"  # shown on every graphic; the app sets it from --watermark

W, H = 800, 450
SCALE = 2
PT = SCALE * 0.72  # points per design pixel
HEADER_H, FOOTER_H, PANEL_W = 36, 22, 203
MAP_L = PANEL_W - 60  # the map starts under the panel so the panel can fade into it
VIS = (PANEL_W + 14, HEADER_H + 6, W, H - FOOTER_H - 4)  # the part of the map that's clearly visible
ROUTE_SHOW_KM = 1200  # show planned origin/destination on the map when this close

C = {
    "bg": "#0e1217", "panel": "#131820", "panel2": "#0f1319", "ocean": "#0f141b", "land": "#1f262f",
    "coast": "#2c3540", "text": "#f2f4f7", "muted": "#8f99a5", "faint": "#5d6773", "city": "#c3cbd4",
    "footer": "#a9b57c", "glass": "#0a0e13",
}
ALT_STOPS = [0, 4000, 8000, 20000, 40000]
ALT_COLORS = ["#ff8c1a", "#ffd21a", "#35d63a", "#1fa8ff", "#ff2bf0"]
ALT_CMAP = LinearSegmentedColormap.from_list("alt", list(zip([0, 0.25, 0.5, 0.75, 1.0], ALT_COLORS)))

# aircraft silhouette pointing north, nose at (0, 1)
_HALF = [(0, 1.0), (0.08, 0.86), (0.09, 0.32), (0.92, -0.04), (0.92, -0.17), (0.09, 0.03),
         (0.07, -0.55), (0.34, -0.76), (0.34, -0.87), (0.0, -0.78)]
PLANE = _HALF + [(-x, y) for x, y in reversed(_HALF[1:-1])]


def fs(px: float) -> float:
    """Font size in points for a design-pixel height."""
    return px * PT


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


def _smooth(t):
    t = np.clip(t, 0, 1)
    return t * t * (3 - 2 * t)


def _rgba(color, alpha):
    """Image of `color` with the given alpha array."""
    r, g, b = to_rgb(color)
    img = np.empty(alpha.shape + (4,))
    img[..., 0], img[..., 1], img[..., 2], img[..., 3] = r, g, b, alpha
    return img


HALO = [pe.withStroke(linewidth=2.6, foreground=C["ocean"], alpha=0.7)]


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


# ------------------------------------------------------------------ canvas


class Card:
    def __init__(self, title: str, brand: str):
        self.fig = plt.figure(figsize=(W * SCALE / 100, H * SCALE / 100), dpi=100, facecolor=C["bg"])
        self.title, self.brand = title, brand
        self.renderer = self.fig.canvas.get_renderer()
        # overlay for the panel, header, footer and their fades (above the map)
        self.ov = self.fig.add_axes([0, 0, 1, 1], zorder=3)
        self.ov.set_xlim(0, W)
        self.ov.set_ylim(H, 0)
        self.ov.axis("off")
        self.ov.patch.set_alpha(0)

    def text(self, x, y, s, px=8, color=None, weight="normal", ha="left", va="baseline", **kw):
        return self.ov.text(x, y, s, fontsize=fs(px), color=color or C["text"], fontweight=weight, ha=ha, va=va, zorder=6, **kw)

    def measure(self, s, px, weight="normal", family=None) -> float:
        kw = {"family": family} if family else {}
        t = self.fig.text(0, 0, s, fontsize=fs(px), fontweight=weight, **kw)
        w = t.get_window_extent(self.renderer).width / SCALE
        t.remove()
        return w

    def image(self, img, x0, y0, x1, y1, z=2):
        self.ov.imshow(img, extent=(x0, x1, y1, y0), origin="upper", aspect="auto", interpolation="bilinear", zorder=z)
        self.ov.set_xlim(0, W)
        self.ov.set_ylim(H, 0)

    def pill(self, x, y, s, px, fg, bg, bg_alpha=1.0, weight="bold", ha="left"):
        return self.text(x, y, s, px=px, color=fg, weight=weight, ha=ha, va="center",
                         bbox=dict(boxstyle="round,pad=0.42,rounding_size=0.75", fc=bg, ec="none", alpha=bg_alpha))

    def chrome(self, accent: str, footer_left: str, footer_right: str, sample: bool = False, subtitle: str = ""):
        ys = np.linspace(0, 1, 120)[:, None]
        xs = np.linspace(0, 1, 160)[None, :]
        # panel: solid, then a long soft fade into the map
        fade_end = PANEL_W + 44
        xpx = xs * fade_end
        a = 1 - _smooth((xpx - (PANEL_W - 22)) / 66)
        panel = _rgba(C["panel"], np.repeat(a, 120, axis=0))
        shade = ys[..., None] * 0.6  # slight vertical shade
        panel[..., :3] = panel[..., :3] * (1 - shade) + np.array(to_rgb(C["panel2"])) * shade
        self.image(panel, 0, 0, fade_end, H, z=2)
        # header and footer fade over the map
        hy = np.linspace(0, 1, 60)[:, None] * np.ones((1, 4))
        self.image(_rgba(C["bg"], 1 - _smooth((hy * 60 - 30) / 28)), 0, 0, W, 60, z=2.2)
        fy = np.linspace(0, 1, 60)[:, None] * np.ones((1, 4))
        self.image(_rgba(C["bg"], _smooth((fy * 46 - 6) / 32)), 0, H - 46, W, H, z=2.2)
        # accent bloom behind the logo
        bx, by = np.meshgrid(np.linspace(-1, 1, 80), np.linspace(-1, 1, 80))
        self.image(_rgba(accent, np.exp(-(bx ** 2 + by ** 2) * 3) * 0.22), -30, -40, 90, 76, z=2.3)

        self.ov.add_patch(FancyBboxPatch((10, 7), 22, 22, boxstyle="round,pad=0,rounding_size=7", fc="#ffffff", alpha=0.07, ec="none", zorder=5))
        self.ov.plot([21], [18], marker=plane_marker(45), markersize=fs(11), color=accent, markeredgewidth=0, zorder=6)
        self.text(41, 23.5, self.title, px=12.5, weight="bold")
        if subtitle:  # a thin rule, then what this picture is about
            x = 41 + self.measure(self.title, 12.5, "bold") + 10
            self.ov.plot([x, x], [12, 26], color="#ffffff", alpha=0.14, lw=0.8, zorder=6)
            self.text(x + 10, 22.5, subtitle, px=7.2, color=C["muted"], weight="bold")
        # watermark: top right, plus a faint one low over the map so a cropped picture still carries it
        self._right_x = W - 14
        if WATERMARK:
            self.text(self._right_x, 22.5, WATERMARK, px=8.6, weight="bold", ha="right", alpha=0.85)
            self._right_x -= self.measure(WATERMARK, 8.6, "bold") + 14
            self.text((PANEL_W + W) / 2 + 10, H - FOOTER_H - 9, WATERMARK, px=7, weight="bold", ha="center", alpha=0.28)
        if sample:
            self.right_pill("SAMPLE DATA — NOT A REAL EVENT", "#ff6b61", "#ff453a", 0.16)
        self.text(14, H - 8, footer_left, px=6.2, color=C["footer"], alpha=0.85)
        self.text(W - 14, H - 8, footer_right, px=6.2, color=C["muted"], ha="right")

    def right_pill(self, s, fg, bg, bg_alpha=1.0):
        """A pill in the header, right-aligned, to the left of the watermark and any earlier pill."""
        self.pill(self._right_x, 18, s, 6.8, fg, bg, bg_alpha=bg_alpha, ha="right")
        self._right_x -= self.measure(s, 6.8, "bold") + 20

    def save(self, path):
        self.fig.savefig(path, dpi=100, facecolor=C["bg"])
        plt.close(self.fig)


class MapFrame:
    """A map axes that spans from MAP_L to the right edge and the full height.
    Positions handed around are design px relative to the visible area VIS."""

    def __init__(self, card: Card, points, min_span_deg=12.0, clamp_lat=None):
        self.card = card
        vx0, vy0, vx1, vy1 = VIS
        self.vw, self.vh = vx1 - vx0, vy1 - vy0
        x0, x1, y0, y1 = _extent(points, self.vw / self.vh, min_span_deg)
        if clamp_lat:
            y0, y1 = max(y0, geo.merc_y(clamp_lat[0])), min(y1, geo.merc_y(clamp_lat[1]))
            # keep the aspect after clamping by widening x
            need = (y1 - y0) * self.vw / self.vh
            if need > x1 - x0:
                cx = (x0 + x1) / 2
                x0, x1 = cx - need / 2, cx + need / 2
        self.ppx = self.vw / (x1 - x0)  # design px per projected unit
        self.vx0, self.vy1_data = x0, y1  # projected coords of the visible top-left
        self.ext = (x0, x1, y0, y1)
        # whole axes extent (under the panel/header/footer too)
        self.X0 = x0 - (vx0 - MAP_L) / self.ppx
        self.X1 = x1
        self.Y1 = y1 + vy0 / self.ppx
        self.Y0 = y0 - (H - vy1) / self.ppx
        self.AW, self.AH = W - MAP_L, H
        ax = card.fig.add_axes([MAP_L / W, 0, self.AW / W, 1], zorder=1)
        ax.set_facecolor(C["ocean"])
        ax.set_xticks([])
        ax.set_yticks([])
        for s in ax.spines.values():
            s.set_visible(False)
        ax.set_xlim(self.X0, self.X1)
        ax.set_ylim(self.Y0, self.Y1)
        self.ax = ax
        self.lon_lo, self.lon_hi = math.degrees(self.X0), math.degrees(self.X1)
        self.lat_lo, self.lat_hi = geo.inv_merc_y(self.Y0), geo.inv_merc_y(self.Y1)

    @staticmethod
    def P(lon, lat):
        return math.radians(lon), geo.merc_y(lat)

    def to_px(self, lon, lat):
        x, y = self.P(lon, lat)
        return (x - self.vx0) * self.ppx, (self.vy1_data - y) * self.ppx

    def axc(self, px, py):
        """Visible-area px -> axes fraction."""
        vx0, vy0, _, _ = VIS
        return (vx0 - MAP_L + px) / self.AW, 1 - (vy0 + py) / self.AH

    def land(self):
        outers, holes, _ = basemap.land()

        def polys(rings):
            out = []
            for ring in rings:
                rl = [p[0] for p in ring]
                rt = [p[1] for p in ring]
                if max(rt) < self.lat_lo or min(rt) > self.lat_hi:
                    continue
                for shift in (-360, 0, 360):
                    if max(rl) + shift < self.lon_lo or min(rl) + shift > self.lon_hi:
                        continue
                    out.append([self.P(lo + shift, max(-85, min(85, la))) for lo, la in ring])
            return out

        lw = 0.45 * PT
        land_polys = polys(outers)
        # a soft glow along the coast: wide, faint lines drawn under the land so only the sea side shows
        for width, alpha in ((7.0, 0.035), (3.6, 0.055), (1.6, 0.09)):
            self.ax.add_collection(PolyCollection(land_polys, facecolors="none", edgecolors="#7fa8d6", linewidths=width * PT,
                                                  alpha=alpha, zorder=0.9, joinstyle="round"))
        self.ax.add_collection(PolyCollection(land_polys, facecolors=C["land"], edgecolors=C["coast"], linewidths=lw, zorder=1))
        segs = []
        for line in basemap.borders():
            lo = [p[0] for p in line]
            la = [p[1] for p in line]
            if max(la) < self.lat_lo or min(la) > self.lat_hi or max(lo) < self.lon_lo or min(lo) > self.lon_hi:
                continue
            segs.append([self.P(x, y) for x, y in line])
        if segs:
            self.ax.add_collection(LineCollection(segs, colors="#ffffff", alpha=0.07, linewidths=0.5 * PT, zorder=1.1))
        if holes:
            self.ax.add_collection(PolyCollection(polys(holes), facecolors=C["ocean"], edgecolors=C["coast"], linewidths=lw, zorder=1))
        # faint graticule, like an aviation chart
        span = self.lon_hi - self.lon_lo
        step = 30 if span > 120 else 10 if span > 40 else 5 if span > 15 else 2 if span > 6 else 1
        for lon in range(int(math.floor(self.lon_lo / step) * step), int(self.lon_hi) + step, step):
            self.ax.plot([math.radians(lon)] * 2, [self.Y0, self.Y1], color="#ffffff", alpha=0.035, lw=0.45 * PT, zorder=1.2)
        for lat in range(int(math.floor(max(-80, self.lat_lo) / step) * step), int(min(84, self.lat_hi)) + step, step):
            y = geo.merc_y(lat)
            self.ax.plot([self.X0, self.X1], [y, y], color="#ffffff", alpha=0.035, lw=0.45 * PT, zorder=1.2)
        # vignette: darken towards the edges, slightly lift the middle
        xs, ys = np.meshgrid(np.linspace(-1, 1, 200), np.linspace(-1, 1, 120))
        d = np.sqrt((xs * 0.9) ** 2 + (ys * 1.05) ** 2)
        self.ax.imshow(_rgba("#000000", _smooth((d - 0.45) / 0.95) * 0.55), extent=(0, 1, 0, 1), transform=self.ax.transAxes,
                       aspect="auto", interpolation="bilinear", zorder=1.5)
        self.ax.set_xlim(self.X0, self.X1)
        self.ax.set_ylim(self.Y0, self.Y1)

    def glass(self, x, y, w, h, alpha=0.8, z=12):
        """Borderless frosted box (visible-area px), with a soft shadow."""
        bx, by = self.axc(x, y + h)
        p = FancyBboxPatch((bx, by), w / self.AW, h / self.AH, transform=self.ax.transAxes,
                           boxstyle=f"round,pad=0,rounding_size={6 / self.AW}", fc=C["glass"], ec="none", alpha=alpha, zorder=z,
                           mutation_aspect=self.AW / self.AH)
        p.set_path_effects([pe.SimplePatchShadow(offset=(0, -3), shadow_rgbFace="#000000", alpha=0.35), pe.Normal()])
        self.ax.add_patch(p)

    def glow_dot(self, lon, lat, color, size_px=16, z=9):
        x, y = self.P(lon, lat)
        for k, a in ((1.0, 0.05), (0.75, 0.07), (0.5, 0.1), (0.3, 0.14)):
            self.ax.plot([x], [y], "o", ms=size_px * k * PT * 2, color=color, alpha=a, mew=0, zorder=z)

    def text(self, px, py, s, size, color, z=12, **kw):
        return self.ax.text(*self.axc(px, py), s, transform=self.ax.transAxes, fontsize=fs(size), color=color, zorder=z, **kw)


# ------------------------------------------------------------------ panel


def _draw_photo(card: Card, alert: Alert, x, y, w, h, photo_bytes: bytes | None, accent: str):
    img = None
    if photo_bytes:
        try:
            import matplotlib.image as mpimg

            img = mpimg.imread(io.BytesIO(photo_bytes), format="jpg")
        except Exception:  # noqa: BLE001 - any decode error -> placeholder
            img = None
    ys, xs = np.meshgrid(np.linspace(0, 1, 240), np.linspace(0, 1, 400), indexing="ij")
    # feathered mask: soft on every side, softest at the bottom where it melts into the panel
    mask = _smooth(xs / 0.07) * _smooth((1 - xs) / 0.07) * _smooth(ys / 0.08) * _smooth((1 - ys) / 0.3)
    if img is not None:
        if img.dtype != np.float32 and img.dtype != np.float64:
            img = img.astype(np.float32) / 255.0
        ih, iw = img.shape[:2]
        target = w / h
        if iw / ih > target:  # too wide: crop sides
            nw = int(ih * target)
            img = img[:, (iw - nw) // 2:(iw - nw) // 2 + nw]
        else:
            nh = int(iw / target)
            img = img[(ih - nh) // 2:(ih - nh) // 2 + nh]
        from matplotlib.transforms import Affine2D  # noqa: F401  (keeps matplotlib's image stack loaded)

        # resample mask to the image size
        mh_, mw_ = img.shape[:2]
        my = np.linspace(0, 1, mh_)[:, None]
        mx = np.linspace(0, 1, mw_)[None, :]
        m = _smooth(mx / 0.07) * _smooth((1 - mx) / 0.07) * _smooth(my / 0.08) * _smooth((1 - my) / 0.3)
        rgba = np.dstack([img[..., :3], m])
        card.image(rgba, x, y, x + w, y + h, z=4)
    else:
        glow = np.exp(-(((xs - 0.5) / 0.38) ** 2 + ((ys - 0.48) / 0.42) ** 2)) * 0.16 * mask
        card.image(_rgba(accent, glow), x, y, x + w, y + h, z=4)
        card.ov.plot([x + w / 2], [y + h * 0.45], marker=plane_marker(90), markersize=fs(42), color="#ffffff", alpha=0.13, mew=0, zorder=5)
        card.text(x + w / 2, y + h * 0.86, "NO PHOTO", px=6, color=C["faint"], weight="bold", ha="center")


def _fade_rule(card: Card, x0, x1, y, alpha=0.18):
    xs = np.linspace(0, 1, 200)[None, :]
    a = np.sin(xs * math.pi) ** 1.5 * alpha
    card.image(_rgba("#ffffff", a), x0, y - 0.5, x1, y + 0.5, z=4)


def _panel(card: Card, alert: Alert, photo_bytes: bytes | None):
    ac = alert.aircraft
    cat = CATEGORIES[alert.top.category]
    x0, xr = 12, PANEL_W - 12
    card.pill(x0 + 3, 52, _badge_text(alert), 6.3, "#0b0d10", cat["color"])
    ident = ac.callsign or ac.registration or ac.hex
    card.text(x0, 84, ident, px=17, weight="bold")
    cc, country = geo.country_for_hex(ac.hex)
    if cc:
        card.pill(xr, 78, cc, 7.4, C["text"], "#ffffff", bg_alpha=0.09, ha="right")
    desc = ac.description or ac.type_code or "Unknown type"
    card.text(x0, 103, textwrap.shorten(nice_desc(desc), 34, placeholder="…"), px=8, color="#d9dee4")
    sub = " · ".join(s for s in (ac.operator, country, "Military" if ac.military else "") if s)
    card.text(x0, 117, textwrap.shorten(sub or "Operator unknown", 40, placeholder="…"), px=6.8, color=C["muted"])

    _draw_photo(card, alert, x0 - 4, 124, xr - x0 + 8, 96, photo_bytes, cat["color"])
    if alert.photo:
        credit = f"Photo: {alert.photo.get('photographer') or 'unknown'} / planespotters.net"
        card.text(x0, 226, textwrap.shorten(credit, 46, placeholder="…"), px=5.2, color=C["faint"])

    # the three numbers people look for first, as tiles
    vr = ac.vrate_fpm
    trend = "" if not vr or abs(vr) < 200 else (f"▲ {abs(vr):,} fpm" if vr > 0 else f"▼ {abs(vr):,} fpm")
    tiles = [
        ("ALTITUDE", "GND" if ac.on_ground else (f"{ac.alt_ft:,}" if ac.alt_ft is not None else "—"), "" if ac.on_ground else "ft", trend),
        ("SPEED", f"{ac.speed_kt:.0f}" if ac.speed_kt is not None else "—", "kt", ""),
        ("HEADING", f"{ac.heading:.0f}°" if ac.heading is not None else "—", "", ""),
    ]
    gap, tw = 5, (xr - x0 - 10) / 3
    for k, (label, value, unit, note) in enumerate(tiles):
        tx = x0 + k * (tw + gap)
        card.ov.add_patch(FancyBboxPatch((tx, 234), tw, 36, boxstyle="round,pad=0,rounding_size=5", fc="#ffffff", alpha=0.045, ec="none", zorder=5))
        card.text(tx + 6, 244, label, px=4.8, color=C["faint"], weight="bold")
        v = card.text(tx + 6, 259, value, px=10.5, weight="bold")
        if unit:
            card.text(tx + 6 + card.measure(value, 10.5, "bold") + 2, 259, unit, px=5.6, color=C["muted"], weight="bold")
        if note:
            card.text(tx + 6, 267, note, px=4.6, color="#ff9f0a" if vr < 0 else "#30d158", weight="bold")
        del v

    _altitude_profile(card, ac, x0, 280, xr - x0, 44)

    sq = ac.squawk or "—"
    rows = [
        ("SQUAWK", sq, CATEGORIES[SQUAWKS[sq][0]]["color"] if sq in SQUAWKS else None),
        ("HEX", ac.hex, None),
        ("REG · TYPE", " · ".join(s for s in (ac.registration, ac.type_code) if s) or "—", None),
        ("TRACK START", _hhmm(ac.track[0].ts) if ac.track else "—", None),
        ("SEEN", _hhmm(ac.seen_ts), None),
    ]
    y = 346
    for label, value, color in rows:
        card.text(x0, y, label, px=5.4, color=C["muted"])
        card.text(xr, y, value, px=7.4, weight="bold", ha="right", color=color or C["text"])
        y += 16


def _altitude_profile(card: Card, ac, x, y, w, h):
    """A small chart of altitude over the flight, filled with the same colours as the track."""
    card.text(x, y, "ALTITUDE PROFILE", px=4.8, color=C["faint"], weight="bold")
    pts = [(p.ts, p.alt_ft or 0) for p in ac.track if p.alt_ft is not None]
    top, base = y + 8, y + h
    if len(pts) < 3 or pts[-1][0] - pts[0][0] < 120:
        card.text(x, base - 6, "Not enough track yet", px=5.4, color=C["faint"])
        return
    t0, t1 = pts[0][0], pts[-1][0]
    peak = max(max(a for _, a in pts), 2000)
    n = 120
    ts = np.linspace(t0, t1, n)
    alts = np.interp(ts, [p[0] for p in pts], [p[1] for p in pts])
    # image: each column coloured by its altitude, opaque below the line and fading towards the bottom
    rows_ = 40
    yy = np.linspace(1, 0, rows_)[:, None]  # 1 at the top of the chart
    frac = (alts / peak)[None, :]
    img = np.zeros((rows_, n, 4))
    cols = ALT_CMAP([alt_frac(a) for a in alts])
    img[..., :3] = cols[None, :, :3]
    img[..., 3] = np.where(yy <= frac, 0.18 + 0.5 * (yy / np.maximum(frac, 1e-6)) ** 1.5, 0)
    card.image(img, x, top, x + w, base, z=5)
    xs = x + (ts - t0) / (t1 - t0) * w
    ys = base - alts / peak * (base - top)
    segs = [[(xs[k], ys[k]), (xs[k + 1], ys[k + 1])] for k in range(n - 1)]
    card.ov.add_collection(LineCollection(segs, colors=cols[1:], linewidths=1.2 * PT, zorder=6, capstyle="round"))
    card.ov.plot([xs[-1]], [ys[-1]], "o", ms=3 * PT, color="#ffffff", mew=0, zorder=7)
    card.text(x + w, y, f"max {peak:,.0f} ft", px=4.8, color=C["muted"], ha="right", weight="bold")
    card.text(x, base + 7, _hhmm(t0)[:5], px=4.6, color=C["faint"])
    card.text(x + w, base + 7, _hhmm(t1)[:5], px=4.6, color=C["faint"], ha="right")


# ------------------------------------------------------------------ map


def _map(card: Card, alert: Alert):
    ac = alert.aircraft
    cat = CATEGORIES[alert.top.category]

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

    extras = []
    if alert.divert_airport:
        d = alert.divert_airport
        extras.append(("divert", near(d["lon"]), d["lat"], d))
    if alert.route:
        for key in ("origin", "destination"):
            a = alert.route.get(key) or {}
            if a.get("lat") is not None and geo.haversine_km(pos[1], pos[0], a["lat"], a["lon"]) < ROUTE_SHOW_KM:
                extras.append((key, near(a["lon"]), a["lat"], a))
    m = MapFrame(card, (list(zip(lons, lats)) or [pos]) + [(e[1], e[2]) for e in extras])
    ax, mw, mh, P = m.ax, m.vw, m.vh, m.P
    m.land()

    track_px = [m.to_px(lo, la) for lo, la in zip(lons, lats)]
    apx, apy = m.to_px(*pos)
    extra_px = [m.to_px(lon, lat) for _, lon, lat, _ in extras]
    divert = next((e for e in extras if e[0] == "divert"), None)
    if divert:  # the dashed line to the diversion airport
        ex, ey = m.to_px(divert[1], divert[2])
        extra_px += [(apx + (ex - apx) * k / 12, apy + (ey - apy) * k / 12) for k in range(1, 12)]
    blocked = [(-80, -20, 0, mh + 20)]  # under the panel fade
    blocked += _signals(card, m, alert, track_px + extra_px + [(apx, apy)] * 25)

    # airport labels go on the side away from the aircraft; reserve their space
    extra_labels = []
    for kind, lon, lat, a in extras:
        code = a.get("icao") or a.get("iata") or ""
        ex, ey = m.to_px(lon, lat)
        if kind == "divert":
            name = textwrap.shorten(a.get("name") or a.get("location") or "", 26, placeholder="…")
            dist = geo.haversine_km(pos[1], pos[0], lat, lon)
            text, size = f"DIVERT → {code}  {name}\n{dist:,.0f} km", 6.4
        else:
            place = textwrap.shorten(a.get("city") or a.get("location") or "", 16, placeholder="…")
            text, size = ("FROM " if kind == "origin" else "TO ") + code + (f" · {place}" if place else ""), 6
        side = "right" if ex < apx else "left"  # = the text's horizontal alignment
        w = max(card.measure(t, size, "bold") for t in text.split("\n")) + 12
        h = size * 1.5 * (text.count("\n") + 1) + 6
        blocked.append((ex - w, ey - 2, ex + 4, ey + h) if side == "right" else (ex - 4, ey - 2, ex + w, ey + h))
        extra_labels.append((text, side))

    title = ac.label
    bits = [ac.type_code or textwrap.shorten(nice_desc(ac.description or ""), 18, placeholder="…"),
            "GROUND" if ac.on_ground else (f"{ac.alt_ft:,} ft" if ac.alt_ft is not None else ""),
            f"{ac.speed_kt:.0f} kt" if ac.speed_kt is not None else ""]
    sub = "  ·  ".join(b for b in bits if b)
    lw_ = max(card.measure(title, 7.8, "bold") + 12, card.measure(sub, 6, "bold") if sub else 0) + 18
    lh_ = 32 if sub else 20
    ahead_px = []  # the projected path (drawn later): keep the label off it
    if ac.heading is not None and ac.speed_kt and ac.speed_kt > 60 and not ac.on_ground:
        for k in range(1, 9):
            la, lo = geo.destination(pos[1], pos[0], ac.heading, ac.speed_kt * 1.852 / 6 * k / 8)
            ahead_px.append(m.to_px(near(lo), la))
    lrect = _place_label(lw_, lh_, apx, apy, mw, mh, track_px + extra_px * 3 + ahead_px * 3, blocked)
    blocked.append(lrect)

    # cities: biggest first, keep labels apart and off the aircraft, track, airports and overlays
    taken = [(apx, apy)] + [(ex - 30, ey) for ex, ey in extra_px[: len(extras)]]
    shown = 0
    for name, lat, lon, _rank in basemap.cities():
        if shown >= 10:
            break
        lon = near(lon)
        cx, cy = m.to_px(lon, lat)
        if not (0 < cx < mw - 70 and 26 < cy < mh - 34):
            continue
        if any(abs(cx - tx) < 78 and abs(cy - ty) < 20 for tx, ty in taken):
            continue
        if any(bx0 - 4 < cx < bx1 + 4 and by0 - 4 < cy < by1 + 10 for bx0, by0, bx1, by1 in blocked):
            continue
        if any(cx - 6 < tx < cx + 8 + 4.2 * len(name) and cy - 12 < ty < cy + 6 for tx, ty in track_px[::2]):
            continue
        taken.append((cx, cy))
        x, y = P(lon, lat)
        ax.plot([x], [y], "o", ms=2.1 * PT, color=C["city"], alpha=0.75, mew=0, zorder=3)
        ax.annotate(name, (x, y), xytext=(4 * PT, 3 * PT), textcoords="offset points", fontsize=fs(6.5), color=C["city"],
                    alpha=0.85, zorder=3, path_effects=HALO)
        shown += 1

    # planned route airports, diversion target
    for (kind, lon, lat, a), (text, side) in zip(extras, extra_labels):
        x, y = P(lon, lat)
        k = 1 if side == "left" else -1
        if kind == "divert":
            dx, dy = P(*pos)
            ax.plot([dx, x], [dy, y], lw=5 * PT, color=cat["color"], alpha=0.1, solid_capstyle="round", zorder=4)
            ax.plot([dx, x], [dy, y], ls=(0, (3, 2.5)), lw=1.3 * PT, color=cat["color"], alpha=0.9, zorder=4)
            m.glow_dot(lon, lat, cat["color"], 11, z=5)
            ax.plot([x], [y], marker="^", ms=5.5 * PT, color=cat["color"], mew=0, zorder=6)
            ax.annotate(text, (x, y), xytext=(9 * k, -5), textcoords="offset points", fontsize=fs(6.4), color=cat["color"],
                        fontweight="bold", va="top", ha=side, zorder=6, path_effects=[pe.withStroke(linewidth=3, foreground=C["bg"], alpha=0.85)])
        else:
            ax.plot([x], [y], marker="^", ms=4.3 * PT, color=C["muted"], alpha=0.9, mew=0, zorder=5)
            ax.annotate(text, (x, y), xytext=(7 * k, -3), textcoords="offset points", fontsize=fs(6), color=C["muted"],
                        va="top", ha=side, zorder=5, path_effects=HALO)

    # track coloured by altitude: glow underneath, and older parts fade out
    if len(track) >= 2:
        xy = [P(lo, la) for lo, la in zip(lons[: len(track)], lats[: len(track)])]
        segs = [[xy[i], xy[i + 1]] for i in range(len(xy) - 1)]
        n = len(segs)
        base = [ALT_CMAP(alt_frac(track[i + 1].alt_ft)) for i in range(n)]
        age = [0.22 + 0.78 * _smooth(np.array(i / max(1, n - 1))) for i in range(n)]
        for lw, a in ((11, 0.05), (6.5, 0.1), (3.8, 0.22)):
            ax.add_collection(LineCollection(segs, colors=[(c[0], c[1], c[2], a * f) for c, f in zip(base, age)],
                                             linewidths=lw * PT, capstyle="butt", zorder=7))
        ax.add_collection(LineCollection(segs, colors=[(c[0], c[1], c[2], f) for c, f in zip(base, age)],
                                         linewidths=1.9 * PT, capstyle="butt", zorder=8))
        ax.plot([xy[0][0]], [xy[0][1]], "o", ms=3.4 * PT, color=base[0], alpha=0.45, mew=0, zorder=8)
        # time marks: the start, then every full hour (if there's room), so the track reads like a timeline
        marks, last_px = [], None
        for k, p in enumerate(track):
            hour_change = k and int(p.ts // 3600) != int(track[k - 1].ts // 3600)
            if k == 0 or hour_change:
                tx, ty = m.to_px(lons[k], lats[k])
                if last_px is None or math.hypot(tx - last_px[0], ty - last_px[1]) > 46:
                    if math.hypot(tx - apx, ty - apy) > 40 and 10 < tx < mw - 40 and 26 < ty < mh - 34:
                        marks.append((k, tx, ty))
                        last_px = (tx, ty)
        for k, tx, ty in marks:
            p = track[k]
            stamp = datetime.fromtimestamp(p.ts if k == 0 else (p.ts // 3600) * 3600, timezone.utc).strftime("%H:%M")
            ax.plot([xy[k][0]], [xy[k][1]], "o", ms=2.6 * PT, color="#ffffff", alpha=0.7, mew=0, zorder=8.5)
            m.text(tx + 5, ty - 5, stamp, 5.2, C["muted"], z=8.5, alpha=0.9, path_effects=HALO)

    # where it will be in 10 minutes if it holds this heading and speed
    if ac.heading is not None and ac.speed_kt and ac.speed_kt > 60 and not ac.on_ground:
        ahead_km = ac.speed_kt * 1.852 / 6
        steps = [geo.destination(pos[1], pos[0], ac.heading, ahead_km * k / 8) for k in range(1, 9)]
        fx = [P(near(lo), la)[0] for la, lo in steps]
        fy = [P(near(lo), la)[1] for la, lo in steps]
        ax.plot([P(*pos)[0]] + fx, [P(*pos)[1]] + fy, ls=(0, (1, 2.2)), lw=1.2 * PT, color="#ffffff", alpha=0.45, zorder=8.8,
                dash_capstyle="round")
        ex, ey = m.to_px(near(steps[-1][1]), steps[-1][0])
        if 10 < ex < mw - 40 and 26 < ey < mh - 34 and math.hypot(ex - apx, ey - apy) > 26:
            ax.plot([fx[-1]], [fy[-1]], "o", ms=2.4 * PT, mfc="none", mec="#ffffff", mew=0.8, alpha=0.6, zorder=8.8)
            m.text(ex + 5, ey - 4, "+10 min", 4.8, C["muted"], z=8.8, alpha=0.9, path_effects=HALO)

    # aircraft: soft halo, shadow, icon
    px, py = P(*pos)
    m.glow_dot(pos[0], pos[1], cat["color"], 20, z=9)
    shadow_off = 1.5 / m.ppx
    ax.plot([px + shadow_off], [py - shadow_off], marker=plane_marker(ac.heading), ms=13 * PT, color="#000000", alpha=0.45, mew=0, zorder=9.5)
    ax.plot([px], [py], marker=plane_marker(ac.heading), ms=13 * PT, color=cat["color"], mec="#ffffff", mew=0.35, zorder=10)

    # label: frosted glass, a faint leader line to the aircraft
    lx0, ly0, lx1, ly1 = lrect
    cxl, cyl = min(max(apx, lx0), lx1), min(max(apy, ly0), ly1)
    if math.hypot(cxl - apx, cyl - apy) > 14:
        a0, a1 = m.axc(apx, apy), m.axc(cxl, cyl)
        ax.plot([a0[0], a1[0]], [a0[1], a1[1]], transform=ax.transAxes, color="#ffffff", alpha=0.22, lw=0.8 * PT, zorder=10.5)
    m.glass(lx0, ly0, lx1 - lx0, ly1 - ly0, alpha=0.84, z=11)
    m.text(lx0 + 9, ly0 + 11, "●", 5.5, cat["color"], z=11.5, va="center")
    m.text(lx0 + 19, ly0 + 11, title, 7.8, C["text"], z=11.5, fontweight="bold", va="center")
    if sub:
        m.text(lx0 + 9, ly0 + 23, sub, 6, C["muted"], z=11.5, fontweight="bold", va="center")

    _scale_bar(m)
    _alt_legend(m)


def _signals(card: Card, m: MapFrame, alert: Alert, avoid_px) -> list[tuple]:
    """Tinted chips for every kind of flag, plus the most relevant ACARS message.
    Returns the rectangles (visible-area px) it covered."""
    mw, mh = m.vw, m.vh
    blocked = []
    x = 6
    order = {"squawk": 0, "adsb-emergency": 1, "acars": 2, "route": 3}
    best: dict[str, Flag] = {}
    for f in sorted(alert.flags, key=lambda f: order.get(f.origin, 9)):
        best.setdefault(f.category, f)
    chips = sorted(best.values(), key=lambda f: -CATEGORIES[f.category]["severity"]) if len(best) > 1 else []
    for f in chips:
        color = CATEGORIES[f.category]["color"]
        src = "ACARS" if f.origin == "acars" else f"SQK {f.matched}" if f.origin == "squawk" else "ROUTE" if f.origin == "route" else "ADS-B"
        label = f"{src} · {CATEGORIES[f.category]['label']}"
        m.text(x, 8, label, 5.8, color, z=12, fontweight="bold", va="center",
               bbox=dict(boxstyle="round,pad=0.45,rounding_size=0.8", fc=color, ec="none", alpha=0.16))
        x += card.measure(label, 5.8, "bold") + 16
        if x > mw - 120:
            break
    if chips:
        blocked.append((0, -6, x, 18))

    msg_flag = next((f for f in sorted(alert.flags, key=lambda f: -CATEGORIES[f.category]["severity"]) if f.origin == "acars"), None)
    if not msg_flag:
        return blocked
    msg = next((mm for mm in alert.messages if mm.ts == msg_flag.ts), alert.messages[0] if alert.messages else None)
    flat = " ".join((msg.text if msg else msg_flag.evidence).split())
    lines = textwrap.wrap(flat, 60)
    if len(lines) > 5:  # keep the lines around the match
        i = next((n for n, ln in enumerate(lines) if msg_flag.matched and msg_flag.matched.upper() in ln.upper()), 0)
        start = max(0, min(i - 1, len(lines) - 5))
        lines = (["…"] if start else []) + lines[start:start + 5] + (["…"] if start + 5 < len(lines) else [])
    box_w, line_h = 272, 9.5
    box_h = 22 + line_h * len(lines) + 6
    top = 24 if chips else 6
    corners = [(6, top), (mw - 10 - box_w, top), (6, mh - 34 - box_h), (mw - 10 - box_w, mh - 38 - box_h)]

    def overlap(c):
        cx, cy = c
        return sum(1 for px, py in avoid_px if cx - 20 < px < cx + box_w + 20 and cy - 20 < py < cy + box_h + 40)

    bx, by = min(corners, key=overlap)
    blocked.append((bx, by, bx + box_w, by + box_h))
    m.glass(bx, by, box_w, box_h, alpha=0.8, z=12)
    head = "ACARS"
    if msg:
        bits = [_hhmm(msg.ts), msg.flight or msg.registration, f"label {msg.label}" if msg.label else "", msg.source]
        head += "  ·  " + "  ·  ".join(b for b in bits if b)
    color = CATEGORIES[msg_flag.category]["color"]
    m.text(bx + 10, by + 12, "●", 5, color, z=13, va="center")
    m.text(bx + 18, by + 12, head, 5.7, C["muted"], z=13, fontweight="bold", va="center")
    mono = dict(family=MONO, va="center")
    char_w = card.measure("M" * 20, 6.3, family=MONO) / 20
    for n, ln in enumerate(lines):
        y = by + 27 + n * line_h
        m.text(bx + 10, y, ln, 6.3, "#d3d9df", z=13, **mono)
        if msg_flag.matched:
            i = ln.upper().find(msg_flag.matched.upper())
            if i >= 0:
                m.text(bx + 10 + i * char_w, y, ln[i:i + len(msg_flag.matched)], 6.3, "#0b0d10", z=13.5, fontweight="bold",
                       bbox=dict(boxstyle="round,pad=0.12,rounding_size=0.3", fc=color, ec="none"), **mono)
    return blocked


def _place_label(bw, bh, apx, apy, mw, mh, avoid_px, blocked):
    """Rect (x0, y0, x1, y1) below/above/right/left of the aircraft, whichever covers the least."""
    gap = 22
    cands = [
        (apx - bw / 2, apy + gap, apx + bw / 2, apy + gap + bh),
        (apx - bw / 2, apy - gap - bh, apx + bw / 2, apy - gap),
        (apx + gap, apy - bh / 2, apx + gap + bw, apy + bh / 2),
        (apx - gap - bw, apy - bh / 2, apx - gap, apy + bh / 2),
        (apx + gap * 0.7, apy + gap * 0.7, apx + gap * 0.7 + bw, apy + gap * 0.7 + bh),
        (apx - gap * 0.7 - bw, apy - gap * 0.7 - bh, apx - gap * 0.7, apy - gap * 0.7),
    ]

    def cost(rect):
        x0, y0, x1, y1 = rect
        c = sum(1 for px, py in avoid_px if x0 - 6 < px < x1 + 6 and y0 - 6 < py < y1 + 6)
        c += 1000 * (x0 < 2 or y0 < 20 or x1 > mw - 4 or y1 > mh - 34)
        c += sum(500 for b in blocked if x0 < b[2] and x1 > b[0] and y0 < b[3] and y1 > b[1])
        return c

    return min(cands, key=cost)


def _scale_bar(m: MapFrame):
    x0, x1, y0, y1 = m.ext
    lat_c = geo.inv_merc_y((y0 + y1) / 2)
    km_map = (x1 - x0) * 6371.0 * math.cos(math.radians(lat_c))
    nice = next((n for n in (10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000) if n >= km_map / 6), 5000)
    w = nice / km_map * m.vw
    bx, by = 12, m.vh - 12
    a0, a1 = m.axc(bx, by), m.axc(bx + w, by)
    for lw, a in ((4, 0.12), (1.1, 0.75)):
        m.ax.plot([a0[0], a1[0]], [a0[1], a1[1]], transform=m.ax.transAxes, color="#e6e9ed", lw=lw * PT * 0.6, alpha=a,
                  solid_capstyle="round", zorder=12)
    m.text(bx + w / 2, by - 7, f"{nice:,} km", 5.8, "#e6e9ed", z=12, ha="center", alpha=0.85, path_effects=HALO)


def _alt_legend(m: MapFrame):
    w, h = 118, 4.5
    x1 = m.vw - 12
    x0 = x1 - w
    y = m.vh - 13
    xs = np.linspace(0, 1, 256)[None, :] * np.ones((8, 1))
    img = ALT_CMAP(xs)
    img[..., 3] = _smooth(xs / 0.06) * _smooth((1 - xs) / 0.06) * 0.95
    (ax0, ay0), (ax1, ay1) = m.axc(x0, y + h / 2), m.axc(x1, y - h / 2)
    m.ax.imshow(img, extent=(ax0, ax1, ay0, ay1), transform=m.ax.transAxes, aspect="auto", interpolation="bilinear", zorder=12)
    m.ax.set_xlim(m.X0, m.X1)
    m.ax.set_ylim(m.Y0, m.Y1)
    for i, lab in enumerate(["0", "4k", "8k", "20k", "40k"]):
        m.text(x0 + w * i / 4, y - 7, lab, 5.3, "#dfe3e8", z=12, ha="center", alpha=0.85, path_effects=HALO)
    m.text(x0 - 7, y, "ft", 5.3, "#dfe3e8", z=12, ha="right", va="center", alpha=0.85)


# ------------------------------------------------------------------ public


def render_card(alert: Alert, path, photo_bytes: bytes | None = None, title: str = "PLANE ALERT",
                brand: str = "plane watch", sources: dict | None = None, sample: bool = False):
    sources = sources or {}
    cat = CATEGORIES[alert.top.category]
    card = Card(title, brand)
    _, _, land_src = basemap.land()
    parts = [f"ADS-B: {sources.get('position', alert.aircraft.source or 'community feeds')}"]
    if alert.messages:
        parts.append(f"ACARS: {sources.get('acars', alert.messages[0].source)}")
    parts.append(f"Map: {land_src}")
    when = datetime.fromtimestamp(alert.aircraft.seen_ts or 0, timezone.utc).strftime("%d %b %Y · %H:%M UTC")
    card.chrome(cat["color"], " · ".join(parts), when, sample, subtitle=f"{cat['label']} · {_hhmm(alert.aircraft.seen_ts)}")
    if getattr(alert, "ground_test", False) and not sample:
        card.right_pill("ON THE GROUND · PROBABLY A TEST", C["muted"], "#ffffff", 0.08)
    _panel(card, alert, photo_bytes)
    _map(card, alert)
    card.save(path)
    return path


def map_item(alert: Alert) -> dict:
    """What the overview map needs to remember about an aircraft (small enough to keep for hours)."""
    ac = alert.aircraft
    track = ac.track
    step = max(1, math.ceil(len(track) / 200))
    pts = [[round(p.lon, 3), round(p.lat, 3), p.alt_ft] for p in track[::step]]
    if track and (not pts or pts[-1][:2] != [round(track[-1].lon, 3), round(track[-1].lat, 3)]):
        pts.append([round(track[-1].lon, 3), round(track[-1].lat, 3), track[-1].alt_ft])
    return {
        "hex": ac.hex, "label": ac.label, "desc": nice_desc(ac.description or ac.type_code or ac.hex),
        "category": alert.top.category, "squawk": ac.squawk, "lat": ac.lat, "lon": ac.lon, "heading": ac.heading,
        "track": pts, "last_seen": ac.seen_ts,
    }


def render_overview(alerts: list[Alert], path, title: str = "PLANE ALERT", brand: str = "plane watch",
                    when_ts: float | None = None, sample: bool = False, earlier: list[dict] | None = None,
                    keep_hours: float | None = None):
    """The emergency map: aircraft in an emergency now (bright, glowing) and, dimmed, the ones
    that were in an emergency earlier (`earlier`, map_item dicts) with the time they cleared."""
    from .classify import is_emergency

    active = [map_item(a) for a in alerts if is_emergency(a) and a.aircraft.lat is not None]
    active_hex = {i["hex"] for i in active}
    past = sorted((i for i in (earlier or []) if i["hex"] not in active_hex and i.get("lat") is not None),
                  key=lambda i: -(i.get("last_seen") or 0))
    card = Card(title, brand)
    worst = max(active, key=lambda i: CATEGORIES[i["category"]]["severity"]) if active else None
    accent = CATEGORIES[worst["category"]]["color"] if worst else "#5ac8a8"
    when_ts = when_ts or max((i.get("last_seen") or 0 for i in active), default=0) or datetime.now(timezone.utc).timestamp()
    when = datetime.fromtimestamp(when_ts, timezone.utc).strftime("%d %b %Y · %H:%M UTC")
    keep = f" (last {keep_hours:g} h)" if keep_hours else ""
    card.chrome(accent, f"{len(active)} emergenc{'y' if len(active) == 1 else 'ies'} now · {len(past)} earlier{keep} · "
                        f"ADS-B community feeds · Map: Natural Earth", when, sample, subtitle="Emergency map")

    # ---- list
    if active:
        card.pill(15, 54, f"{len(active)} EMERGENC{'Y' if len(active) == 1 else 'IES'} NOW", 6.3, "#0b0d10", accent)
    else:
        card.pill(15, 54, "NO ACTIVE EMERGENCIES", 6.3, C["text"], "#ffffff", bg_alpha=0.08)
    y, rows = 82, 0
    for i in sorted(active, key=lambda i: -CATEGORIES[i["category"]]["severity"]):
        if rows >= 15:
            break
        _list_row(card, y, i, live=True)
        y, rows = y + 21, rows + 1
    if past and rows < 15:
        y += 4
        card.text(15, y, f"EARLIER{keep.upper()}", px=5.4, color=C["faint"], weight="bold")
        y += 15
        for i in past:
            if rows >= 15:
                break
            _list_row(card, y, i, live=False)
            y, rows = y + 21, rows + 1
    hidden = len(active) + len(past) - rows
    if hidden > 0:
        card.text(25, y, f"+ {hidden} more", px=6, color=C["muted"])

    # ---- map
    pts = [(i["lon"], i["lat"]) for i in active + past]
    for i in active + past:
        pts += [(p[0], p[1]) for p in i.get("track", [])[::10]]
    m = MapFrame(card, pts or [(-150, -50), (170, 70)], min_span_deg=60 if pts else 320, clamp_lat=(-70, 80))
    m.land()
    for i in past:  # dimmed first, so the live ones sit on top
        _map_item(m, i, live=False)
    for i in active:
        _map_item(m, i, live=True)
    card.save(path)
    return path


def _list_row(card: Card, y, item: dict, live: bool):
    cat = CATEGORIES[item["category"]]
    a = 1.0 if live else 0.45
    card.ov.plot([16], [y - 3], "o", ms=4 * PT, color=cat["color"], alpha=a, mew=0, zorder=6)
    if live:
        card.ov.plot([16], [y - 3], "o", ms=9 * PT, color=cat["color"], alpha=0.18, mew=0, zorder=6)
    card.text(25, y, item["label"], px=7.6, weight="bold", color=C["text"] if live else C["muted"])
    right = textwrap.shorten(cat["label"], 22, placeholder="…") if live else f"cleared {_hhmm(item.get('last_seen'))}"
    card.text(PANEL_W - 12, y, right, px=5.5, color=cat["color"] if live else C["faint"], ha="right", weight="bold")
    card.text(25, y + 9, textwrap.shorten(item.get("desc") or "", 36, placeholder="…"), px=5.6, color=C["muted"] if live else C["faint"])


def _map_item(m: MapFrame, item: dict, live: bool):
    cat = CATEGORIES[item["category"]]
    track = item.get("track") or []
    if len(track) >= 2:
        lons = _unwrap([p[0] for p in track])
        xy = [m.P(lo, p[1]) for lo, p in zip(lons, track)]
        segs = [[xy[k], xy[k + 1]] for k in range(len(xy) - 1)]
        n = len(segs)
        cols = []
        for k in range(n):
            c = ALT_CMAP(alt_frac(track[k + 1][2]))
            f = (0.15 + 0.7 * (k / max(1, n - 1))) if live else 0.22
            cols.append((c[0], c[1], c[2], f))
        if live:
            m.ax.add_collection(LineCollection(segs, colors=[(c[0], c[1], c[2], c[3] * 0.25) for c in cols], linewidths=4 * PT, capstyle="butt", zorder=3))
        m.ax.add_collection(LineCollection(segs, colors=cols, linewidths=(1.3 if live else 0.9) * PT, capstyle="butt", zorder=3.2))
    x, y = m.P(item["lon"], item["lat"])
    if live:
        m.glow_dot(item["lon"], item["lat"], cat["color"], 16, z=5)
        m.ax.plot([x], [y], marker=plane_marker(item.get("heading")), ms=11 * PT, color=cat["color"], mec="#ffffff", mew=0.3, zorder=6)
        m.ax.annotate(item["label"], (x, y), xytext=(9 * PT, 5 * PT), textcoords="offset points", fontsize=fs(6.4), color=C["text"],
                      fontweight="bold", zorder=7, bbox=dict(boxstyle="round,pad=0.4,rounding_size=0.6", fc=C["glass"], ec="none", alpha=0.78))
    else:
        m.ax.plot([x], [y], marker=plane_marker(item.get("heading")), ms=8 * PT, color=cat["color"], alpha=0.4, mew=0, zorder=4)
        m.ax.annotate(f"{item['label']} · cleared {_hhmm(item.get('last_seen'))[:5]}", (x, y), xytext=(7 * PT, 4 * PT),
                      textcoords="offset points", fontsize=fs(5.6), color=C["muted"], zorder=4.5,
                      bbox=dict(boxstyle="round,pad=0.35,rounding_size=0.6", fc=C["glass"], ec="none", alpha=0.55))


# ------------------------------------------------------------------ military heatmap

HEAT_CMAP = LinearSegmentedColormap.from_list("heat", [(0, "#1b1f6b"), (0.25, "#5a2a9c"), (0.5, "#c23b8f"), (0.7, "#ff6a5a"),
                                                       (0.86, "#ffb36b"), (1, "#fff6d8")])


def _rasterize_segments(chunks, X0, X1, Y0, Y1, nx, ny, max_gap_s=900):
    """Accumulate flight paths (consecutive positions joined by straight lines) into an ny x nx grid."""
    hist = np.zeros(ny * nx)
    for arr in chunks:
        if arr.size == 0:
            continue
        lon2, lat2, lon1, lat1, dt = arr.T
        ok = ~np.isnan(lon1) & (dt <= max_gap_s) & (np.abs(lon2 - lon1) < 20) & (np.abs(lat2 - lat1) < 20)
        if not ok.any():
            continue
        lon1, lat1, lon2, lat2 = lon1[ok], lat1[ok], lon2[ok], lat2[ok]

        def gx(lon):
            return (np.radians(lon) - X0) / (X1 - X0) * nx

        def gy(lat):
            return (np.log(np.tan(np.pi / 4 + np.radians(np.clip(lat, -85, 85)) / 2)) - Y0) / (Y1 - Y0) * ny

        x1, y1, x2, y2 = gx(lon1), gy(lat1), gx(lon2), gy(lat2)
        n = np.clip(np.ceil(np.hypot(x2 - x1, y2 - y1) / 0.6), 1, 400).astype(int)
        idx = np.repeat(np.arange(len(n)), n)
        t = (np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)) / np.repeat(n, n)
        sx = (x1[idx] + (x2 - x1)[idx] * t).astype(int)
        sy = (y1[idx] + (y2 - y1)[idx] * t).astype(int)
        inside = (sx >= 0) & (sx < nx) & (sy >= 0) & (sy < ny)
        hist += np.bincount(sy[inside] * nx + sx[inside], minlength=ny * nx)
    return hist.reshape(ny, nx)


def _blur(a, sigma):
    r = max(1, int(sigma * 3))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    a = np.apply_along_axis(lambda v: np.convolve(v, k, mode="same"), 0, a)
    return np.apply_along_axis(lambda v: np.convolve(v, k, mode="same"), 1, a)


def _side_list(card: Card, y, heading, rows, max_rows=5):
    """A small titled list in the left panel: rows of (left, right, sub)."""
    card.text(15, y, heading, px=5.4, color=C["faint"], weight="bold")
    y += 14
    for left, right, sub in rows[:max_rows]:
        card.text(15, y, textwrap.shorten(left, 30, placeholder="…"), px=6.8, weight="bold")
        card.text(PANEL_W - 12, y, right, px=6.2, color=C["muted"], ha="right", weight="bold")
        if sub:
            card.text(15, y + 8.5, textwrap.shorten(sub, 42, placeholder="…"), px=5.4, color=C["faint"])
        y += 19 if sub else 13
    if not rows:
        card.text(15, y, "Nothing yet", px=6, color=C["faint"])
        y += 13
    return y + 8


def render_heatmap(points, path, summary: dict, period_label: str, title: str = "PLANE ALERT", brand: str = "plane watch",
                   sample: bool = False, segments=None, filter_label: str = ""):
    """Density of military aircraft positions recorded over a period."""
    accent = "#fd7a5c"
    card = Card(title, brand)
    when = datetime.now(timezone.utc).strftime("%d %b %Y · %H:%M UTC")
    card.chrome(accent, f"{summary.get('positions', 0):,} positions · {summary.get('aircraft_seen', 0):,} military aircraft · "
                        f"{period_label} · Map: Natural Earth", when, sample,
                subtitle="Military heatmap" + (f" · {filter_label}" if filter_label else ""))
    card.pill(15, 54, f"MILITARY ACTIVITY · {period_label.upper()}", 6.3, "#0b0d10", accent)
    card.text(15, 88, f"{summary.get('aircraft_seen', 0):,}", px=20, weight="bold")
    card.text(15, 101, "military aircraft seen", px=6.4, color=C["muted"])
    card.text(PANEL_W - 12, 88, f"{len(summary.get('flights', [])):,}", px=13, weight="bold", ha="right")
    card.text(PANEL_W - 12, 101, "flights", px=6.4, color=C["muted"], ha="right")
    y = _side_list(card, 124, "MOST SEEN TYPES",
                   [(t["type"], f"{t['aircraft']}", nice_desc(t["descr"])) for t in summary.get("types", []) if t["type"] != "?"])
    _side_list(card, y, "BUSIEST AIRFIELDS",
               [(f"{a['icao']}  {a['name']}", f"{a['movements']}", f"{a['departures']} out · {a['arrivals']} in · {a['top_types']}")
                for a in summary.get("airfields", [])])

    arr = np.array(points, dtype=float)
    arr = arr.reshape(-1, arr.shape[1] if arr.ndim == 2 else 2)
    pts = arr[:, :2]
    weights = arr[:, 2] if arr.shape[1] > 2 else np.ones(len(arr))
    if len(pts) >= 20:
        def wpct(v, q):  # weighted percentile, so busy cells count more than one-off positions
            order = np.argsort(v)
            cw = np.cumsum(weights[order])
            return v[order][np.searchsorted(cw, cw[-1] * np.array(q) / 100)]

        lo_lon, hi_lon = wpct(pts[:, 0], [1, 99])
        lo_lat, hi_lat = wpct(pts[:, 1], [1, 99])
        frame_pts = [(lo_lon, lo_lat), (hi_lon, hi_lat)]
        span = 30
    else:
        frame_pts, span = ([(-150, -50), (170, 70)] if len(pts) == 0 else [tuple(p) for p in pts]), 320 if len(pts) == 0 else 30
    m = MapFrame(card, frame_pts, min_span_deg=span, clamp_lat=(-70, 80))
    m.land()
    if len(pts):
        # dim the land a little so the heat stands out
        m.ax.imshow(_rgba("#000000", np.full((2, 2), 0.25)), extent=(0, 1, 0, 1), transform=m.ax.transAxes, aspect="auto", zorder=1.6)
        nx = 760
        ny = max(80, int(nx * (m.Y1 - m.Y0) / (m.X1 - m.X0)))
        hist = _rasterize_segments(segments, m.X0, m.X1, m.Y0, m.Y1, nx, ny) if segments is not None else np.zeros((ny, nx))
        if hist.sum() == 0:  # no paths (or old SQLite): fall back to the binned positions
            xs = np.radians(pts[:, 0])
            ys = np.array([geo.merc_y(v) for v in pts[:, 1]])
            hist, _, _ = np.histogram2d(ys, xs, bins=[ny, nx], range=[[m.Y0, m.Y1], [m.X0, m.X1]], weights=weights)
            hist = _blur(hist, 1.2)
        ext = (m.X0, m.X1, m.Y0, m.Y1)
        # soft glow underneath
        wide = np.log1p(_blur(hist, 7.0) * 3)
        wide /= wide.max() or 1
        glow = HEAT_CMAP(np.clip(wide * 0.75, 0, 1))
        glow[..., 3] = _smooth(wide / 0.05) * wide ** 0.8 * 0.55
        m.ax.imshow(glow, extent=ext, origin="lower", aspect="auto", interpolation="bilinear", zorder=2)
        # crisp paths on top
        fine = np.log1p(_blur(hist, 0.75) * 2)
        fine /= np.percentile(fine[fine > 0], 99.7) if (fine > 0).any() else 1
        fine = np.clip(fine, 0, 1) ** 0.85
        img = HEAT_CMAP(0.25 + 0.75 * fine)
        img[..., 3] = _smooth(fine / 0.08) * (0.35 + 0.65 * fine)
        m.ax.imshow(img, extent=ext, origin="lower", aspect="auto", interpolation="bilinear", zorder=2.1)
        m.ax.set_xlim(m.X0, m.X1)
        m.ax.set_ylim(m.Y0, m.Y1)
        # label the busiest airfields
        placed = []
        for a in summary.get("airfields", [])[:6]:
            ap = geo.airport(a["icao"])
            if not ap:
                continue
            px, py = m.to_px(ap["lon"], ap["lat"])
            text = f"{a['icao']}  {a['movements']}"
            w_ = card.measure(text, 5.8, "bold") + 12
            r = (px + 7, py - 6, px + 7 + w_, py + 7)
            if not (0 < px < m.vw - w_ - 10 and 20 < py < m.vh - 34) or any(r[0] < q[2] and r[2] > q[0] and r[1] < q[3] and r[3] > q[1] for q in placed):
                continue
            placed.append(r)
            x_, y_ = m.P(ap["lon"], ap["lat"])
            m.ax.plot([x_], [y_], "o", ms=3 * PT, color="#ffffff", alpha=0.9, mew=0, zorder=8)
            m.text(px + 9, py, text, 5.8, C["text"], z=8, fontweight="bold", va="center",
                   bbox=dict(boxstyle="round,pad=0.35,rounding_size=0.6", fc=C["glass"], ec="none", alpha=0.72))
    else:
        m.text(m.vw / 2, m.vh / 2, "No military positions recorded yet. Leave Plane Watch running to build this up.",
               8, C["muted"], z=12, ha="center", va="center")
    # legend
    w, h = 118, 4.5
    x1, y = m.vw - 12, m.vh - 13
    x0 = x1 - w
    xs_ = np.linspace(0, 1, 256)[None, :] * np.ones((8, 1))
    leg = HEAT_CMAP(xs_)
    leg[..., 3] = _smooth(xs_ / 0.06) * _smooth((1 - xs_) / 0.06)
    (ax0, ay0), (ax1, ay1) = m.axc(x0, y + h / 2), m.axc(x1, y - h / 2)
    m.ax.imshow(leg, extent=(ax0, ax1, ay0, ay1), transform=m.ax.transAxes, aspect="auto", interpolation="bilinear", zorder=12)
    m.ax.set_xlim(m.X0, m.X1)
    m.ax.set_ylim(m.Y0, m.Y1)
    m.text(x0, y - 7, "fewer", 5.3, "#dfe3e8", z=12, ha="left", alpha=0.85, path_effects=HALO)
    m.text(x1, y - 7, "busier", 5.3, "#dfe3e8", z=12, ha="right", alpha=0.85, path_effects=HALO)
    card.save(path)
    return path


# ------------------------------------------------------------------ watched types (Osprey, H-60)

TYPE_COLORS = {"V22": "#ff9f0a", "H60": "#64d2ff"}
TYPE_PALETTE = ["#ff9f0a", "#64d2ff", "#30d158", "#ff375f", "#bf5af2", "#ffd60a", "#5ac8fa", "#ff6b2d"]


def type_colors(types) -> dict:
    out, free = {}, [c for c in TYPE_PALETTE if c not in TYPE_COLORS.values()]
    for t in types:
        out[t] = TYPE_COLORS.get(t) or free[len([x for x in out if x not in TYPE_COLORS]) % len(free)]
    return out


def render_watched(items: list[dict], path, hours: float = 24, title: str = "PLANE ALERT", brand: str = "plane watch",
                   now: float | None = None, sample: bool = False, types: list[str] | None = None):
    """Last known position of each aircraft of the given types (default V-22 and H-60) over the last `hours`."""
    now = now or datetime.now(timezone.utc).timestamp()
    types = list(types or ["V22", "H60"])
    colors = type_colors(types + sorted({i["type"] for i in items} - set(types)))
    accent = colors[types[0]]
    card = Card(title, brand)
    when = datetime.fromtimestamp(now, timezone.utc).strftime("%d %b %Y · %H:%M UTC")
    counts = {}
    for i in items:
        counts[i["type"]] = counts.get(i["type"], 0) + 1
    names = {"V22": "Ospreys", "H60": "H-60s"}
    heading = "OSPREY & H-60" if sorted(types) == ["H60", "V22"] else " · ".join(types[:4]) + (" …" if len(types) > 4 else "")
    card.chrome(accent, f"{len(items)} aircraft · last positions, {hours:g} h · ADS-B community feeds · Map: Natural Earth",
                when, sample, subtitle=f"Type tracker · last {hours:g} h")
    card.pill(15, 54, f"{heading} · LAST {hours:g} H", 6.3, "#0b0d10", accent)
    x = 15
    for t in types[:3]:
        card.text(x, 88, str(counts.get(t, 0)), px=17, weight="bold", color=colors[t])
        card.text(x, 100, names.get(t, t), px=6.2, color=C["muted"])
        x += 62
    y = 122
    for i in items[:14]:
        color = colors.get(i["type"], C["muted"])
        fresh = now - (i["last_seen"] or 0) < 15 * 60
        card.ov.plot([16], [y - 3], "o", ms=4 * PT, color=color, alpha=1 if fresh else 0.5, mew=0, zorder=6)
        card.text(25, y, i.get("callsign") or i.get("reg") or i["hex"], px=7.2, weight="bold", color=C["text"] if fresh else C["muted"])
        card.text(PANEL_W - 12, y, _hhmm(i["last_seen"]), px=5.6, color=C["muted"] if fresh else C["faint"], ha="right", weight="bold")
        near = i.get("near")
        kind = {"V22": "Osprey", "H60": "H-60"}.get(i["type"], i["type"])
        sub = kind + (f" · near {near['icao']} {near['name']}" if near else "")
        card.text(25, y + 8.5, textwrap.shorten(sub, 40, placeholder="…"), px=5.4, color=C["faint"])
        y += 20
    if len(items) > 14:
        card.text(25, y, f"+ {len(items) - 14} more", px=6, color=C["muted"])
    if not items:
        card.text(15, 124, f"None seen in the last {hours:g} hours.", px=6.6, color=C["muted"])

    pts = [(i["lon"], i["lat"]) for i in items if i.get("lat") is not None]
    for i in items:
        pts += [(p[0], p[1]) for p in i.get("track", [])[::15]]
    m = MapFrame(card, pts or [(-150, -50), (170, 70)], min_span_deg=10 if pts else 320, clamp_lat=(-70, 80))
    m.land()
    # labels: freshest first get the best spots; one that would overlap another is moved or left out
    placed = []
    label_at = {}
    for i in sorted(items, key=lambda i: -(i["last_seen"] or 0)):
        if i.get("lat") is None:
            continue
        px, py = m.to_px(i["lon"], i["lat"])
        text = f"{i.get('callsign') or i.get('reg') or i['hex']} · {_hhmm(i['last_seen'])[:5]}"
        w, h = card.measure(text, 5.8) + 10, 12
        for dx, dy in ((8, -14), (8, 2), (-8 - w, -14), (-8 - w, 2), (8, -28), (8, 16)):
            r = (px + dx, py + dy, px + dx + w, py + dy + h)
            if r[0] < 0 or r[2] > m.vw or r[1] < 0 or r[3] > m.vh - 30:
                continue
            if not any(r[0] < q[2] and r[2] > q[0] and r[1] < q[3] and r[3] > q[1] for q in placed):
                placed.append(r)
                label_at[i["hex"]] = (text, r)
                break
    for i in sorted(items, key=lambda i: i["last_seen"] or 0):
        if i.get("lat") is None:
            continue
        color = colors.get(i["type"], C["muted"])
        fresh = now - (i["last_seen"] or 0) < 15 * 60
        tr = i.get("track") or []
        if len(tr) >= 2:
            lons = _unwrap([p[0] for p in tr])
            xy = [m.P(lo, p[1]) for lo, p in zip(lons, tr)]
            m.ax.plot([p[0] for p in xy], [p[1] for p in xy], color=color, alpha=0.35 if fresh else 0.2, lw=1.0 * PT, zorder=3,
                      solid_joinstyle="round")
        x, y_ = m.P(i["lon"], i["lat"])
        if fresh:
            m.glow_dot(i["lon"], i["lat"], color, 12, z=5)
        m.ax.plot([x], [y_], marker=plane_marker(i.get("heading")), ms=(10 if fresh else 8) * PT, color=color,
                  alpha=1 if fresh else 0.55, mew=0, zorder=6)
        if i["hex"] in label_at:
            text, r = label_at[i["hex"]]
            m.text(r[0] + 5, (r[1] + r[3]) / 2, text, 5.8, C["text"] if fresh else C["muted"], z=7, va="center",
                   bbox=dict(boxstyle="round,pad=0.35,rounding_size=0.6", fc=C["glass"], ec="none", alpha=0.75 if fresh else 0.5))
    card.save(path)
    return path


# matplotlib's pyplot isn't thread-safe; the dashboard can ask for a filtered picture while the main loop draws
import functools  # noqa: E402
import threading  # noqa: E402

RENDER_LOCK = threading.RLock()


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        with RENDER_LOCK:
            return fn(*a, **kw)
    return wrapper


render_card = _locked(render_card)
render_overview = _locked(render_overview)
render_heatmap = _locked(render_heatmap)
render_watched = _locked(render_watched)


# ------------------------------------------------------------------ daily brief


def _hbars(card: Card, x, y, w, heading, rows, color, max_rows=6):
    """Horizontal bar list: rows of (label, value, sub). Returns the y below it."""
    card.text(x, y, heading, px=5.4, color=C["faint"], weight="bold")
    y += 12
    top = max((r[1] for r in rows[:max_rows]), default=0) or 1
    for label, value, sub in rows[:max_rows]:
        card.text(x, y, textwrap.shorten(label, 34, placeholder="…"), px=6.6, weight="bold")
        card.text(x + w, y, f"{value:,}" if isinstance(value, int) else f"{value:,.0f}", px=6.6, weight="bold", ha="right")
        card.ov.add_patch(FancyBboxPatch((x, y + 4), w, 3.2, boxstyle="round,pad=0,rounding_size=1.6", fc="#ffffff", alpha=0.06, ec="none", zorder=5))
        card.ov.add_patch(FancyBboxPatch((x, y + 4), max(3.2, w * value / top), 3.2, boxstyle="round,pad=0,rounding_size=1.6",
                                         fc=color, alpha=0.9, ec="none", zorder=5.1))
        if sub:
            card.text(x, y + 15, textwrap.shorten(sub, 52, placeholder="…"), px=5, color=C["faint"])
        y += 24 if sub else 17
    if not rows:
        card.text(x, y, "Nothing yet", px=6, color=C["faint"])
        y += 14
    return y


def render_brief(summary: dict, path, period_label: str, title: str = "PLANE ALERT", brand: str = "plane watch",
                 sample: bool = False, filter_label: str = ""):
    """A one-picture briefing: the key numbers, what was found, activity by hour, busiest airfields, types and routes."""
    accent = "#64d2ff"
    card = Card(title, brand)
    when = datetime.now(timezone.utc).strftime("%d %b %Y · %H:%M UTC")
    card.chrome(accent, f"{summary.get('positions', 0):,} positions analysed · {period_label} · recorded by Plane Watch", when, sample,
                subtitle="Military brief" + (f" · {filter_label}" if filter_label else ""))
    # soft backdrop on the right so it doesn't look empty without a map
    gx, gy = np.meshgrid(np.linspace(-1, 1, 160), np.linspace(-1, 1, 100))
    card.image(_rgba("#1a2a3d", np.exp(-((gx * 0.9) ** 2 + (gy * 1.2) ** 2) * 1.6) * 0.55), PANEL_W - 40, 30, W, H - 10, z=1)

    # ---- left: numbers and findings
    x0, xr = 14, PANEL_W - 12
    card.pill(x0 + 1, 54, f"MILITARY BRIEF · {period_label.upper()}", 6.3, "#0b0d10", accent)
    card.text(x0, 90, f"{summary.get('aircraft_seen', 0):,}", px=21, weight="bold")
    card.text(x0, 102, "aircraft seen", px=6.2, color=C["muted"])
    prev = summary.get("previous") or {}
    if prev.get("change_pct") is not None:
        up = prev["change_pct"] >= 0
        card.pill(xr, 84, f"{'▲' if up else '▼'} {abs(prev['change_pct'])}% vs before", 6, "#30d158" if up else "#ff9f0a",
                  "#30d158" if up else "#ff9f0a", bg_alpha=0.16, ha="right")
    tiles = [("FLIGHTS", f"{len(summary.get('flights', [])):,}"), ("HOURS", f"{summary.get('flight_hours', 0):,.0f}"),
             ("AIRFIELDS", f"{len(summary.get('airfields', [])):,}")]
    tw = (xr - x0 - 10) / 3
    for k, (label, value) in enumerate(tiles):
        tx = x0 + k * (tw + 5)
        card.ov.add_patch(FancyBboxPatch((tx, 112), tw, 32, boxstyle="round,pad=0,rounding_size=5", fc="#ffffff", alpha=0.045, ec="none", zorder=5))
        card.text(tx + 6, 122, label, px=4.8, color=C["faint"], weight="bold")
        card.text(tx + 6, 137, value, px=10, weight="bold")
    card.text(x0, 160, "KEY FINDINGS", px=5.4, color=C["faint"], weight="bold")
    y = 173
    for line in (summary.get("insights") or [])[1:]:
        wrapped = textwrap.wrap(line, 47)
        if y + len(wrapped) * 8.6 > H - FOOTER_H - 16:
            break
        card.ov.plot([x0 + 2], [y - 2.2], "o", ms=2.2 * PT, color=accent, mew=0, zorder=6)
        for w_ in wrapped:
            card.text(x0 + 9, y, w_, px=5.9, color="#d3d9df")
            y += 8.6
        y += 4.5

    # ---- right top: activity by hour
    cx0, cx1 = PANEL_W + 34, W - 22
    card.text(cx0, 58, "AIRCRAFT AIRBORNE BY HOUR (UTC)", px=5.4, color=C["faint"], weight="bold")
    hourly = summary.get("hourly") or []
    base, top = 168, 70
    if hourly:
        peak = max(h["airborne"] for h in hourly) or 1
        bw = (cx1 - cx0) / len(hourly)
        for k, h in enumerate(hourly):
            hgt = (base - top) * h["airborne"] / peak
            if hgt > 0.4:
                col = HEAT_CMAP(0.25 + 0.7 * h["airborne"] / peak)
                card.ov.add_patch(FancyBboxPatch((cx0 + k * bw + bw * 0.14, base - hgt), bw * 0.72, hgt,
                                                 boxstyle=f"round,pad=0,rounding_size={min(bw * 0.3, 2.2, hgt / 2)}", fc=col, ec="none",
                                                 alpha=0.95, zorder=5))
        dep_peak = max(h["departures"] for h in hourly) or 1
        xs = [cx0 + (k + 0.5) * bw for k in range(len(hourly))]
        ys = [base - (base - top) * 0.9 * h["departures"] / dep_peak for h in hourly]
        card.ov.plot(xs, ys, color="#ffffff", alpha=0.75, lw=0.9 * PT, zorder=6, solid_joinstyle="round")
        card.text(cx1, 58, f"bars: aircraft airborne (peak {peak})   ·   line: take-offs (peak {dep_peak})", px=5, color=C["muted"], ha="right")
        step = max(1, len(hourly) // 8)
        for k in range(0, len(hourly), step):
            stamp = datetime.fromtimestamp(hourly[k]["ts"], timezone.utc)
            card.text(cx0 + (k + 0.5) * bw, base + 9, stamp.strftime("%H:00" if len(hourly) <= 30 else "%d %b"), px=4.8, color=C["faint"], ha="center")
    card.ov.plot([cx0, cx1], [base, base], color="#ffffff", alpha=0.12, lw=0.6, zorder=5)

    # ---- right bottom: three lists
    col_w = (cx1 - cx0 - 36) / 3
    y0 = 200
    _hbars(card, cx0, y0, col_w, "BUSIEST AIRFIELDS (MOVEMENTS)",
           [(f"{a['icao']}  {a['name']}", a["movements"], f"{a['departures']} out · {a['arrivals']} in · {a['top_types']}")
            for a in summary.get("airfields", [])], "#fd7a5c")
    _hbars(card, cx0 + col_w + 18, y0, col_w, "MOST FLYING BY TYPE (HOURS)",
           [(f"{t['type']}  {nice_desc(t['descr'])}", t["hours"], f"{t['flights']} flights · avg {t['avg_minutes']} min · {t['aircraft']} aircraft")
            for t in sorted((t for t in summary.get("types", []) if t.get("flights")), key=lambda t: -t["hours"])], "#64d2ff")
    _hbars(card, cx0 + 2 * (col_w + 18), y0, col_w, "TOP ROUTES (FLIGHTS)",
           [(f"{r['from']} → {r['to']}", r["flights"], f"{r['km']:,} km · {r['types']}") for r in summary.get("routes", [])], "#bf5af2")
    card.save(path)
    return path


render_brief = _locked(render_brief)
