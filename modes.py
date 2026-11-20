"""Ball League game modes. Every mode is a deterministic simulation (seeded) with one interface:

    m = MODE_CLASSES[name](seed, fps, variant)
    m.step_frame()            advance one video frame
    m.t, m.events             sim time, [(t, kind, ball, k)] with kind in bounce/pass/escape (drives the audio)
    m.decided, m.decide_t     result known? and when
    m.ranking()               team indices, best first
    m.draw(ctx)               draw the arena and balls
    m.chip(i)                 (value_text, bar_fraction) for the scoreboard
    HEADLINE / TITLES / RULE  texts for the video, the YouTube title and the description

Rotation (from match #4): ELIMINATION, GROW, MULTIPLY, PAINT, ESCAPE — all scoring for the same league table.
"""
import math
import random
from collections import deque

import cairo
import numpy as np

import make_video as mv

CX, CY = mv.CX, mv.CY
R_ARENA = mv.R_OUT
N = len(mv.TEAMS)
COLORS = [mv.rgb(h) for _, h in mv.TEAMS]
MODE_ORDER = ["ELIMINATION", "GROW", "MULTIPLY", "PAINT", "ESCAPE"]


def mode_for(match):
    return "ESCAPE" if match <= 3 else MODE_ORDER[(match - 4) % len(MODE_ORDER)]


def draw_ball(ctx, x, y, r, c, glow=True, outline=True):
    if glow:
        g = cairo.RadialGradient(x, y, r * 0.5, x, y, r * 2.4)
        g.add_color_stop_rgba(0, *c, 0.45)
        g.add_color_stop_rgba(1, *c, 0)
        ctx.set_source(g)
        ctx.arc(x, y, r * 2.4, 0, 2 * math.pi)
        ctx.fill()
    ctx.set_source_rgb(*c)
    ctx.arc(x, y, r, 0, 2 * math.pi)
    if outline:
        ctx.fill_preserve()
        ctx.set_source_rgba(1, 1, 1, 0.9)
        ctx.set_line_width(max(1.5, r * 0.12))
        ctx.stroke()
        ctx.set_source_rgba(1, 1, 1, 0.5)
        ctx.arc(x - r * 0.3, y - r * 0.35, r * 0.28, 0, 2 * math.pi)
    ctx.fill()


def draw_wall(ctx, r, start=0.0, end=2 * math.pi, alpha=0.95, width=10.0, col=(1, 1, 1)):
    ctx.new_path()
    ctx.set_source_rgba(*col, alpha)
    ctx.set_line_width(width)
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.arc(CX, CY, r, start, end)
    ctx.stroke()
    ctx.new_path()


class Base:
    SUB = 8
    HEADLINE = ""
    RULE = ""
    TITLES = []

    def __init__(self, seed, fps, variant):
        self.rng = random.Random(seed)
        self.dt = 1.0 / fps / self.SUB
        self.variant = variant
        self.t = 0.0
        self.events = []
        self.decided = False
        self.decide_t = None
        self.last_snd = -1.0

    def sound(self, kind, i, k=0, gap=0.06):
        if kind == "bounce" and self.t - self.last_snd < gap:
            return
        if kind == "bounce":
            self.last_snd = self.t
        self.events.append((self.t, kind, i, k))

    def decide(self):
        if not self.decided:
            self.decided = True
            self.decide_t = self.t
            self.sound("escape", self.ranking()[0])

    def step_frame(self):
        for _ in range(self.SUB):
            self._step()
            self.t += self.dt


# ------------------------------------------------------------------ 1. ESCAPE (the original game)
class EscapeMode:
    HEADLINE = "ONLY 1 ESCAPES"
    RULE = "8 colors race to escape {rings} spinning rings — the first one out wins."
    TITLES = mv.TITLES

    def __init__(self, seed, fps, variant):
        self.sim = mv.Sim(seed, fps=fps, variant=variant)
        self.trails = [deque(maxlen=9) for _ in range(N)]
        self.skip_t = 0.0

    @property
    def t(self):
        return self.sim.t

    @property
    def events(self):
        return self.sim.events

    @property
    def decided(self):
        return bool(self.sim.order)

    @property
    def decide_t(self):
        return next((t for t, kind, _, _ in self.sim.events if kind == "escape"), None)

    def step_frame(self):
        for i in range(N):
            self.trails[i].append((self.sim.px[i], self.sim.py[i]))
        self.sim.step_frame()

    def ranking(self):
        return self.sim.ranking()

    def chip(self, i):
        s = self.sim
        txt = f"#{s.order.index(i) + 1}" if i in s.order else ""
        return txt, s.level[i] / s.n

    def draw(self, ctx):
        s = self.sim
        active = [s.level[i] for i in range(N) if s.level[i] < s.n]
        min_level = min(active) if active else s.n
        for k, ring in enumerate(s.rings):
            alive = k >= min_level
            col = (1, 1, 1)
            f = s.flash[k]
            if f > 0:
                c = COLORS[s.flash_col[k]]
                col = tuple(col[j] * (1 - f) + c[j] * f for j in range(3))
            start, end = ring["a"] + ring["gap"] / 2, ring["a"] - ring["gap"] / 2 + 2 * math.pi
            if f > 0 and alive:
                draw_wall(ctx, ring["r"], start, end, 0.35 * f, mv.RING_W + 18 * f, col)
            draw_wall(ctx, ring["r"], start, end, 0.95 if alive else 0.12, mv.RING_W, col)
        for i in range(N):
            if s.gone[i]:
                continue
            for j, (x, y) in enumerate(self.trails[i]):
                a = (j + 1) / (len(self.trails[i]) + 1)
                ctx.set_source_rgba(*COLORS[i], 0.35 * a)
                ctx.arc(x, y, mv.BALL_R * (0.45 + 0.5 * a), 0, 2 * math.pi)
                ctx.fill()
            draw_ball(ctx, s.px[i], s.py[i], mv.BALL_R, COLORS[i])


# ------------------------------------------------------------------ shared ball-in-circle physics
class Balls(Base):
    def __init__(self, seed, fps, variant, r0, speed=(650, 900), spread=95):
        super().__init__(seed, fps, variant)
        off = self.rng.uniform(0, 2 * math.pi)
        self.px, self.py, self.vx, self.vy, self.r = [], [], [], [], []
        for i in range(N):
            a = off + i * 2 * math.pi / N
            self.px.append(CX + spread * math.cos(a))
            self.py.append(CY + spread * math.sin(a))
            d, s = self.rng.uniform(0, 2 * math.pi), self.rng.uniform(*speed)
            self.vx.append(s * math.cos(d))
            self.vy.append(s * math.sin(d))
            self.r.append(r0)
        self.alive = [True] * N
        self.trails = [deque(maxlen=8) for _ in range(N)]

    def wall(self, i, R, energy=1.012, vmin=520.0, vmax=1650.0):
        """Reflect ball i on the circular wall of radius R. Returns True on a real bounce."""
        dx, dy = self.px[i] - CX, self.py[i] - CY
        dist = math.hypot(dx, dy) or 1e-6
        lim = R - 5 - self.r[i]
        if dist <= lim:
            return False
        nx, ny = dx / dist, dy / dist
        vn = self.vx[i] * nx + self.vy[i] * ny
        hit = False
        if vn > 0:
            self.vx[i] -= 2 * vn * nx
            self.vy[i] -= 2 * vn * ny
            s = math.hypot(self.vx[i], self.vy[i]) or 1.0
            ns = min(max(s * energy, vmin), vmax)
            self.vx[i] *= ns / s
            self.vy[i] *= ns / s
            hit = vn > 150
        self.px[i], self.py[i] = CX + nx * lim, CY + ny * lim
        return hit

    def collide(self, idx):
        for a in range(len(idx)):
            i = idx[a]
            for b in range(a + 1, len(idx)):
                j = idx[b]
                dx, dy = self.px[j] - self.px[i], self.py[j] - self.py[i]
                rr = self.r[i] + self.r[j]
                d2 = dx * dx + dy * dy
                if d2 >= rr * rr or d2 == 0:
                    continue
                d = math.sqrt(d2)
                nx, ny = dx / d, dy / d
                mi, mj = self.r[i] ** 2, self.r[j] ** 2
                rel = (self.vx[j] - self.vx[i]) * nx + (self.vy[j] - self.vy[i]) * ny
                if rel < 0:
                    imp = 2 * rel / (mi + mj)
                    self.vx[i] += imp * mj * nx
                    self.vy[i] += imp * mj * ny
                    self.vx[j] -= imp * mi * nx
                    self.vy[j] -= imp * mi * ny
                ov = rr - d
                wi, wj = mj / (mi + mj), mi / (mi + mj)
                self.px[i] -= nx * ov * wi
                self.py[i] -= ny * ov * wi
                self.px[j] += nx * ov * wj
                self.py[j] += ny * ov * wj

    def step_frame(self):
        for i in range(N):
            self.trails[i].append((self.px[i], self.py[i], self.r[i]))
        super().step_frame()

    def draw_balls(self, ctx, glow=True):
        for i in range(N):
            if not self.alive[i]:
                continue
            for j, (x, y, r) in enumerate(self.trails[i]):
                a = (j + 1) / (len(self.trails[i]) + 1)
                ctx.set_source_rgba(*COLORS[i], 0.3 * a)
                ctx.arc(x, y, r * (0.45 + 0.5 * a), 0, 2 * math.pi)
                ctx.fill()
            draw_ball(ctx, self.px[i], self.py[i], self.r[i], COLORS[i], glow)


# ------------------------------------------------------------------ 2. ELIMINATION: last one inside wins
class EliminationMode(Balls):
    HEADLINE = "LAST ONE INSIDE WINS"
    RULE = "One spinning ring with a gap. Fall out and you're eliminated — the last color inside wins."
    TITLES = ["Only The Last Ball Inside Wins 😱 Pick Yours", "Don't Fall Out! Last Color Standing 🔴🔵",
              "1 Gap, 8 Balls, Only 1 Survives 🤯", "Which Color Survives Till The End? 👀"]

    def __init__(self, seed, fps, variant):
        super().__init__(seed, fps, variant, mv.BALL_R)
        self.g = variant["g"]
        self.gap = self.rng.uniform(150, 185) / R_ARENA
        self.w = self.rng.uniform(0.7, 1.2) * self.rng.choice([-1, 1])
        self.a = self.rng.uniform(0, 2 * math.pi)
        self.out = []                       # elimination order
        self.flash = 0.0

    def radius(self):
        return R_ARENA - min(140.0, self.t * 6.0)   # the arena slowly closes in

    def _step(self):
        dt = self.dt
        self.a += self.w * dt
        R = self.radius()
        idx = []
        for i in range(N):
            self.vy[i] += self.g * dt
            self.px[i] += self.vx[i] * dt
            self.py[i] += self.vy[i] * dt
            if not self.alive[i]:
                continue
            dx, dy = self.px[i] - CX, self.py[i] - CY
            dist = math.hypot(dx, dy)
            in_gap = abs(mv.angdiff(math.atan2(dy, dx), self.a)) < self.gap / 2 - (self.r[i] / R) * 0.8
            if dist + self.r[i] > R - 5 and in_gap and not self.decided:
                if dist - self.r[i] > R + 5:
                    self.alive[i] = False
                    self.out.append(i)
                    self.flash = 1.0
                    self.sound("pass", i)
                    if sum(self.alive) == 1:
                        self.decide()
                continue
            if self.wall(i, R):
                self.sound("bounce", i)
            idx.append(i)
        self.collide(idx)
        self.flash = max(0.0, self.flash - dt * 2.5)

    def ranking(self):
        survivors = [i for i in range(N) if self.alive[i]]
        return survivors + self.out[::-1]

    def chip(self, i):
        if self.alive[i]:
            return "IN", 1.0
        return f"OUT", 0.0

    def draw(self, ctx):
        R = self.radius()
        start, end = self.a + self.gap / 2, self.a - self.gap / 2 + 2 * math.pi
        if self.flash > 0:
            draw_wall(ctx, R, start, end, 0.35 * self.flash, 10 + 20 * self.flash, (1, 0.35, 0.35))
        draw_wall(ctx, R, start, end, 0.95, 10)
        self.draw_balls(ctx)


# ------------------------------------------------------------------ 3. GROW: every bounce makes you bigger
class GrowMode(Balls):
    HEADLINE = "BIGGEST BALL WINS"
    RULE = "Every bounce makes a ball bigger — the first to reach full size wins."
    TITLES = ["Every Bounce Makes It Bigger 😳 Pick Yours", "Which Color Grows The Fastest? 🔴🔵🟢",
              "Bounce, Grow, Win 🤯 Only 1 Gets Huge", "The Biggest Ball Wins 👀 Choose Now"]
    TARGET = 118.0
    R0 = 15.0

    def __init__(self, seed, fps, variant):
        super().__init__(seed, fps, variant, self.R0, spread=120)
        self.g = min(variant["g"], 1500)
        self.grow = self.rng.uniform(2.6, 3.2)
        self.cool = [0.0] * N
        self.peak_t = [0.0] * N

    def _step(self):
        dt = self.dt
        for i in range(N):
            self.vy[i] += self.g * dt
            self.px[i] += self.vx[i] * dt
            self.py[i] += self.vy[i] * dt
            if self.wall(i, R_ARENA, vmin=560) and self.t - self.cool[i] > 0.08 and not self.decided:
                self.cool[i] = self.t
                self.r[i] = min(self.TARGET, self.r[i] + self.grow)
                self.peak_t[i] = self.t
                self.sound("bounce", i)
                if self.r[i] >= self.TARGET:
                    self.decide()
        self.collide(list(range(N)))

    def ranking(self):
        return sorted(range(N), key=lambda i: (-self.r[i], self.peak_t[i]))

    def chip(self, i):
        return f"{int(self.r[i] * 2)}", (self.r[i] - self.R0) / (self.TARGET - self.R0)

    def draw(self, ctx):
        draw_wall(ctx, R_ARENA, alpha=0.9)
        self.draw_balls(ctx)


# ------------------------------------------------------------------ 4. MULTIPLY: hit a glowing zone to split
class MultiplyMode(Base):
    SUB = 4
    HEADLINE = "MOST BALLS WINS"
    RULE = "Hit a glowing zone on the wall and your ball splits in two — most balls at the end wins."
    TITLES = ["Hit The Glow, Multiply 🤯 Most Balls Wins", "Which Color Multiplies The Most? 🔴🔵🟢",
              "1 Ball Becomes 100?! Pick Your Color 😱", "Most Balls Wins — Choose Now 👀"]
    CAP = 90
    R = 9.0

    def __init__(self, seed, fps, variant):
        super().__init__(seed, fps, variant)
        self.g = 700.0
        self.duration = self.rng.uniform(18.0, 22.0)
        self.zones = [{"a": self.rng.uniform(0, 2 * math.pi), "w": self.rng.uniform(0.4, 0.9) * self.rng.choice([-1, 1])}
                      for _ in range(4)]
        self.zone_gap = 0.42
        self.balls = []                      # [x, y, vx, vy, team, cooldown]
        off = self.rng.uniform(0, 2 * math.pi)
        for i in range(N):
            a = off + i * 2 * math.pi / N
            d, s = self.rng.uniform(0, 2 * math.pi), self.rng.uniform(600, 800)
            self.balls.append([CX + 110 * math.cos(a), CY + 110 * math.sin(a), s * math.cos(d), s * math.sin(d), i, 0.0])
        self.count = [1] * N
        self.reach_t = [0.0] * N
        self.flash = [0.0] * len(self.zones)

    def _step(self):
        dt = self.dt
        for z in self.zones:
            z["a"] += z["w"] * dt
        lim = R_ARENA - 5 - self.R
        new = []
        for b in self.balls:
            b[3] += self.g * dt
            b[0] += b[2] * dt
            b[1] += b[3] * dt
            dx, dy = b[0] - CX, b[1] - CY
            dist = math.hypot(dx, dy) or 1e-6
            if dist <= lim:
                continue
            nx, ny = dx / dist, dy / dist
            vn = b[2] * nx + b[3] * ny
            if vn > 0:
                b[2] -= 2 * vn * nx
                b[3] -= 2 * vn * ny
                s = math.hypot(b[2], b[3]) or 1.0
                ns = min(max(s, 500.0), 1300.0)
                b[2] *= ns / s
                b[3] *= ns / s
                ang = math.atan2(dy, dx)
                for zi, z in enumerate(self.zones):
                    if abs(mv.angdiff(ang, z["a"])) < self.zone_gap / 2 and self.t - b[5] > 0.35 \
                            and self.count[b[4]] < self.CAP and not self.decided:
                        rot = self.rng.uniform(0.25, 0.6) * self.rng.choice([-1, 1])
                        c, s2 = math.cos(rot), math.sin(rot)
                        new.append([b[0], b[1], b[2] * c - b[3] * s2, b[2] * s2 + b[3] * c, b[4], self.t])
                        b[5] = self.t
                        self.count[b[4]] += 1
                        self.reach_t[b[4]] = self.t
                        self.flash[zi] = 1.0
                        self.sound("bounce", b[4], gap=0.05)
                        break
            b[0], b[1] = CX + nx * lim, CY + ny * lim
        self.balls.extend(new)
        self.flash = [max(0.0, f - dt * 4) for f in self.flash]
        if not self.decided and self.t >= self.duration:
            self.decide()

    def ranking(self):
        return sorted(range(N), key=lambda i: (-self.count[i], self.reach_t[i]))

    def chip(self, i):
        return str(self.count[i]), self.count[i] / self.CAP

    def draw(self, ctx):
        draw_wall(ctx, R_ARENA, alpha=0.55, width=8)
        for zi, z in enumerate(self.zones):
            f = self.flash[zi]
            s, e = z["a"] - self.zone_gap / 2, z["a"] + self.zone_gap / 2
            draw_wall(ctx, R_ARENA, s, e, 0.35 + 0.4 * f, 16 + 22 * f, (1, 0.9, 0.35))
            draw_wall(ctx, R_ARENA, s, e, 1.0, 12, (1, 0.95, 0.6))
        for b in self.balls:
            draw_ball(ctx, b[0], b[1], self.R, COLORS[b[4]], glow=False, outline=False)


# ------------------------------------------------------------------ 5. PAINT: most painted area wins
class PaintMode(Balls):
    HEADLINE = "MOST COLOR WINS"
    RULE = "Every ball paints the arena with its color — the color covering the most space wins."
    TITLES = ["Which Color Paints The Most? 🎨 Pick Yours", "Color War! Most Paint Wins 🔴🔵🟢",
              "8 Colors Fight For The Canvas 🤯", "Paint It All! Choose Your Color Now 👀"]
    GRID = 96

    def __init__(self, seed, fps, variant):
        super().__init__(seed, fps, variant, 22.0, speed=(620, 700), spread=150)
        self.duration = self.rng.uniform(18.0, 22.0)
        self.speed = 680.0
        G = self.GRID
        self.cell = 2 * R_ARENA / G
        ys, xs = np.mgrid[0:G, 0:G]
        cx = (xs + 0.5) * self.cell - R_ARENA
        cy = (ys + 0.5) * self.cell - R_ARENA
        self.inside = cx ** 2 + cy ** 2 <= (R_ARENA - 6) ** 2
        self.owner = np.full((G, G), -1, dtype=np.int8)
        self.total = int(self.inside.sum())
        rad = int(math.ceil(22.0 / self.cell))
        yy, xx = np.mgrid[-rad:rad + 1, -rad:rad + 1]
        self.stamp = (xx ** 2 + yy ** 2) * self.cell ** 2 <= (22.0 + self.cell * 0.5) ** 2
        self.rad = rad
        self.counts = [0] * N
        pal = np.zeros((N + 1, 4), dtype=np.uint8)            # BGRA, index 0 = unpainted
        pal[0] = (40, 22, 22, 255)
        for i, c in enumerate(COLORS):
            pal[i + 1] = (int(c[2] * 255 * 0.85), int(c[1] * 255 * 0.85), int(c[0] * 255 * 0.85), 255)
        self.pal = pal

    def paint(self, i):
        G, rad = self.GRID, self.rad
        gx = int((self.px[i] - (CX - R_ARENA)) / self.cell)
        gy = int((self.py[i] - (CY - R_ARENA)) / self.cell)
        x0, x1, y0, y1 = gx - rad, gx + rad + 1, gy - rad, gy + rad + 1
        sx0, sy0 = max(0, -x0), max(0, -y0)
        x0c, y0c, x1c, y1c = max(0, x0), max(0, y0), min(G, x1), min(G, y1)
        if x0c >= x1c or y0c >= y1c:
            return
        st = self.stamp[sy0:sy0 + (y1c - y0c), sx0:sx0 + (x1c - x0c)]
        region = self.owner[y0c:y1c, x0c:x1c]
        mask = st & self.inside[y0c:y1c, x0c:x1c]
        region[mask] = i

    def _step(self):
        dt = self.dt
        for i in range(N):
            self.px[i] += self.vx[i] * dt
            self.py[i] += self.vy[i] * dt
            if self.wall(i, R_ARENA, energy=1.0, vmin=self.speed, vmax=self.speed):
                self.sound("bounce", i, gap=0.08)
        self.collide(list(range(N)))
        if not self.decided:
            for i in range(N):
                self.paint(i)
            if self.t >= self.duration:
                self.counts = [int((self.owner == i).sum()) for i in range(N)]
                self.decide()

    def step_frame(self):
        super().step_frame()
        if not self.decided:
            self.counts = [int((self.owner == i).sum()) for i in range(N)]

    def ranking(self):
        return sorted(range(N), key=lambda i: -self.counts[i])

    def chip(self, i):
        pct = 100.0 * self.counts[i] / self.total
        return f"{pct:.0f}%", min(1.0, pct / 30.0)

    def draw(self, ctx):
        img = self.pal[(self.owner.astype(np.int16) + 1)]
        img[~self.inside] = (0, 0, 0, 0)
        img = np.ascontiguousarray(img)
        surf = cairo.ImageSurface.create_for_data(memoryview(img), cairo.FORMAT_ARGB32, self.GRID, self.GRID,
                                                  self.GRID * 4)
        ctx.save()
        ctx.new_path()
        ctx.arc(CX, CY, R_ARENA - 4, 0, 2 * math.pi)
        ctx.clip()
        ctx.translate(CX - R_ARENA, CY - R_ARENA)
        ctx.scale(self.cell, self.cell)
        ctx.set_source_surface(surf, 0, 0)
        ctx.get_source().set_filter(cairo.FILTER_NEAREST)
        ctx.paint()
        ctx.restore()
        draw_wall(ctx, R_ARENA, alpha=0.9)
        self.draw_balls(ctx, glow=False)


MODE_CLASSES = {"ESCAPE": EscapeMode, "ELIMINATION": EliminationMode, "GROW": GrowMode,
                "MULTIPLY": MultiplyMode, "PAINT": PaintMode}
WINDOW = {"ELIMINATION": (14.0, 26.0), "GROW": (14.0, 26.0)}     # modes whose length depends on the seed


def pick_seed(mode, match, fps, variant):
    if mode == "ESCAPE":
        return mv.pick_seed(match, fps, variant)
    if mode not in WINDOW:
        return match * 1000
    lo, hi = WINDOW[mode]
    best = None
    for k in range(60):
        seed = match * 1000 + k
        m = MODE_CLASSES[mode](seed, fps, variant)
        while not m.decided and m.t < hi + 10:
            m.step_frame()
        if m.decided and lo <= m.decide_t <= hi:
            return seed
        if m.decided and (best is None or abs(m.decide_t - 20) < abs(best[1] - 20)):
            best = (seed, m.decide_t)
    return best[0] if best else match * 1000
