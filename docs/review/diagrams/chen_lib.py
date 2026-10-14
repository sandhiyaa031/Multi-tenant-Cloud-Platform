"""Chen-notation ER drawing helpers: rectangles = entities, diamonds = relationships,
ovals = attributes (primary key underlined), cardinality labels on the lines."""
import math

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Ellipse, Polygon, Rectangle

matplotlib.rcParams["svg.fonttype"] = "path"
matplotlib.rcParams["font.family"] = "DejaVu Sans"

INK = "#1f2937"
ENT_FC, ENT_EC = "#d1fae5", "#047857"       # entity: light green
REL_FC, REL_EC = "#dbeafe", "#1d4ed8"       # relationship: light blue
ATT_FC, ATT_EC = "#fce7f3", "#be185d"       # attribute: light pink
ENT_W, ENT_H = 3.6, 1.0
REL_W, REL_H = 3.4, 1.65
OV_H = 0.74


class Chen:
    def __init__(self, w, h, title, subtitle):
        self.fig = plt.figure(figsize=(w, h), dpi=100)
        self.ax = self.fig.add_axes([0, 0, 1, 1])
        self.ax.set_xlim(0, w)
        self.ax.set_ylim(0, h)
        self.ax.axis("off")
        self.w, self.h = w, h
        self.ax.add_patch(Rectangle((0, 0), w, h, color="white", zorder=0))
        self.ax.text(1.2, h - 1.3, title, fontsize=40, fontweight="bold", color=INK, va="center")
        self.ax.text(1.2, h - 2.7, subtitle, fontsize=20, color="#4b5563", va="center")
        self.shapes = {}

    # ── shapes ──────────────────────────────────────────────────────────────
    def entity(self, key, x, y, label=None, attrs=(), side="T", w=ENT_W, weak=False, size=17, gap=0.0, shift=0.0):
        a = self.ax
        label = label or key
        for inset in ((0.0, 0.13) if weak else (0.0,)):
            a.add_patch(Rectangle((x - w / 2 + inset, y - ENT_H / 2 + inset), w - 2 * inset, ENT_H - 2 * inset,
                                  fc=ENT_FC if inset == 0 else "none", ec=ENT_EC, lw=2.4, zorder=4))
        a.text(x, y, label, fontsize=size, fontweight="bold", color=INK, ha="center", va="center", zorder=5)
        self.shapes[key] = ("rect", x, y, w / 2, ENT_H / 2)
        self._attrs(x, y, w / 2, ENT_H / 2, attrs, side, gap, shift)

    def rel(self, key, x, y, label, attrs=(), side="T", dashed=False, size=12.5):
        a = self.ax
        hw, hh = REL_W / 2, REL_H / 2
        a.add_patch(Polygon([(x, y + hh), (x + hw, y), (x, y - hh), (x - hw, y)], closed=True, fc=REL_FC,
                            ec=REL_EC, lw=2.2, ls=(0, (4, 3)) if dashed else "-", zorder=4))
        a.text(x, y, label, fontsize=size, fontweight="bold", color=INK, ha="center", va="center", zorder=5,
               linespacing=1.1)
        self.shapes[key] = ("diamond", x, y, hw, hh)
        self._attrs(x, y, hw, hh, attrs, side, 0.0, 0.0)

    def _attrs(self, x, y, hw, hh, attrs, side, gap, shift=0.0):
        if not attrs:
            return
        items = [(t, k) if isinstance(t, str) else t for t, k in [(a if isinstance(a, tuple) else (a, "n")) for a in attrs]]
        widths = [1.0 + 0.138 * len(t) for t, _ in items]
        n = len(items)
        pos = []
        if side in ("L", "R"):
            sign = -1 if side == "L" else 1
            cx = x + sign * (hw + 1.3 + max(widths) / 2 + gap)
            step = 1.05
            for i in range(n):
                pos.append((cx, y + shift + (n - 1) / 2 * step - i * step))
        else:
            sign = 1 if side == "T" else -1
            step = max(widths) + 0.25
            if n > 3:        # two staggered rows keep wide rows narrow
                step = max(widths) * 0.62 + 0.15
            for i in range(n):
                dx = (i - (n - 1) / 2) * step
                lvl = (i % 2) if n > 3 else 0
                pos.append((x + dx, y + sign * (hh + 1.15 + gap + lvl * 0.95)))
        for (t, k), (ox, oy), ow in zip(items, pos, widths):
            self._oval(x, y, hw, hh, ox, oy, ow, t, k)

    def _oval(self, px, py, phw, phh, ox, oy, ow, text, kind):
        a = self.ax
        a.plot([px, ox], [py, oy], color="#6b7280", lw=1.3, zorder=2)
        a.add_patch(Ellipse((ox, oy), ow, OV_H, fc=ATT_FC, ec=ATT_EC, lw=1.8, zorder=4,
                            ls=(0, (3, 2)) if kind == "d" else "-"))
        t = a.text(ox, oy, text, fontsize=12.5, color=INK, ha="center", va="center", zorder=5)
        if kind == "k":     # primary key: underline
            half = 0.068 * len(text) + 0.05
            a.plot([ox - half, ox + half], [oy - 0.2, oy - 0.2], color=INK, lw=1.5, zorder=6)

    # ── connections ─────────────────────────────────────────────────────────
    def _edge(self, key, tx, ty):
        kind, x, y, hw, hh = self.shapes[key]
        dx, dy = tx - x, ty - y
        if dx == 0 and dy == 0:
            return x, y
        if kind == "rect":
            s = min(hw / abs(dx) if dx else 1e9, hh / abs(dy) if dy else 1e9)
        else:
            s = 1 / (abs(dx) / hw + abs(dy) / hh)
        return x + dx * s, y + dy * s

    def line(self, a, b, la="", lb="", dashed=False, via=None, lw=2.0, color=INK, offa=(0, 0), offb=(0, 0)):
        pa, pb = self.shapes[a], self.shapes[b]
        pts_mid = via or []
        first_t = pts_mid[0] if pts_mid else (pb[1], pb[2])
        last_t = pts_mid[-1] if pts_mid else (pa[1], pa[2])
        p0 = self._edge(a, *first_t)
        p1 = self._edge(b, *last_t)
        pts = [p0, *pts_mid, p1]
        xs, ys = zip(*pts)
        self.ax.plot(xs, ys, color=color, lw=lw, ls=(0, (6, 4)) if dashed else "-", zorder=3, solid_capstyle="round")
        for (p, q), txt, off in ((pts[:2], la, offa), (pts[:-3:-1], lb, offb)):
            if not txt:
                continue
            (x0, y0), (x1, y1) = p, q
            n = math.hypot(x1 - x0, y1 - y0) or 1
            ux, uy = (x1 - x0) / n, (y1 - y0) / n
            d = min(0.55, n * 0.4)
            tx, ty = x0 + ux * d - uy * 0.3 + off[0], y0 + uy * d + ux * 0.3 + off[1]
            self.ax.text(tx, ty, txt, fontsize=19, fontweight="bold", color="#b91c1c", ha="center", va="center",
                         zorder=7, bbox=dict(fc="white", ec="none", pad=0.8))

    def band(self, x, y, w, h, label, color, tint):
        self.ax.add_patch(Rectangle((x, y), w, h, fc=tint, ec=color, lw=3, alpha=0.35, zorder=1, ls=(0, (8, 5))))
        self.ax.text(x + 0.6, y + h - 0.9, label, fontsize=30, fontweight="bold", color=color, va="center", zorder=2)

    def text(self, x, y, s, size=15, color=INK, weight="normal", ha="left"):
        self.ax.text(x, y, s, fontsize=size, color=color, fontweight=weight, va="top", ha=ha, zorder=6, linespacing=1.5)

    def save(self, name, outdir, dpi=100):
        self.fig.savefig(f"{outdir}/{name}.png", dpi=dpi)
        self.fig.savefig(f"{outdir}/{name}.svg")
        plt.close(self.fig)
