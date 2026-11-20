"""Ball League - generates one original physics-race Short per run.

Every run = one league match. 8 coloured balls race to escape rotating rings.
Each match uses a different arena variant (ring count, spin, gravity, colours) for variety.
Results feed a season table (league.json) so viewers follow their colour daily.

Usage:
    python make_video.py              # next match
    python make_video.py --match 3    # (re)render match 3
    python make_video.py --preview    # quick 30fps low-res test
Everything is generated from code: no third-party footage, music or fonts embedded.
"""
import argparse
import datetime
import json
import math
import random
import subprocess
import wave
from collections import deque
from pathlib import Path

import cairo
import imageio_ffmpeg
import numpy as np

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
STATE = ROOT / "league.json"

W, H = 1080, 1920
FPS = 60
SUB = 8
# Layout keeps everything inside the phone "safe zone": the Shorts UI covers roughly the
# bottom 25% (title, channel, music) and a column on the right (like/comment/share).
SAFE_BOTTOM = 1440
CX, CY = 540.0, 815.0
R0, R_OUT, RING_W = 160.0, 462.0, 10.0
BALL_R = 24.0
GAP_BONUS = 2 * (BALL_R - 17.0)   # gaps grow with the balls so races keep their pace
HOOK_LEAD = 0.45                  # video starts this long before the first ring break
VMIN, VMAX = 520.0, 1650.0
SR = 44100

TEAMS = [
    ("RED", "#ff3b3b"), ("BLUE", "#2f7bff"), ("GREEN", "#22d36b"), ("YELLOW", "#ffd60a"),
    ("PURPLE", "#a259ff"), ("ORANGE", "#ff8a00"), ("PINK", "#ff4fd8"), ("CYAN", "#19e3e3"),
]
POINTS = [10, 8, 6, 5, 4, 3, 2, 1]
END_HOLD = 0.8      # seconds of race after the winner escapes
CARD = 1.9          # seconds of results card (short: viewers who stay to the end replay)
LOOP = 0.35         # last seconds crossfade back to the first frame -> seamless loop
RACE_LO, RACE_HI = 14.0, 26.0   # target time of the first escape

# One arena per match, rotating daily. bg = (top, bottom) gradient, glow = arena light.
VARIANTS = [
    {"name": "CLASSIC", "rings": 6, "gap": (78, 105), "spin": (0.6, 1.4), "g": 1500,
     "bg": ((0.04, 0.04, 0.09), (0.09, 0.07, 0.20)), "glow": (0.35, 0.30, 0.90)},
    {"name": "TURBO SPIN", "rings": 5, "gap": (72, 98), "spin": (1.3, 2.2), "g": 1500,
     "bg": ((0.09, 0.03, 0.05), (0.20, 0.05, 0.09)), "glow": (0.90, 0.30, 0.35)},
    {"name": "LOW GRAVITY", "rings": 6, "gap": (80, 108), "spin": (0.5, 1.1), "g": 900,
     "bg": ((0.02, 0.06, 0.09), (0.04, 0.14, 0.20)), "glow": (0.20, 0.70, 0.90)},
    {"name": "TIGHT GAPS", "rings": 6, "gap": (66, 84), "spin": (0.7, 1.5), "g": 1600,
     "bg": ((0.03, 0.08, 0.05), (0.05, 0.18, 0.10)), "glow": (0.30, 0.90, 0.50)},
    {"name": "HEAVY", "rings": 5, "gap": (74, 96), "spin": (0.6, 1.3), "g": 2100,
     "bg": ((0.09, 0.06, 0.02), (0.20, 0.13, 0.04)), "glow": (0.95, 0.65, 0.20)},
]


def variant_for(match):
    return VARIANTS[(match - 1) % len(VARIANTS)]


def rgb(h):
    return tuple(int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))


def angdiff(a, b):
    return (a - b + math.pi) % (2 * math.pi) - math.pi


# ---------------------------------------------------------------- physics
class Sim:
    def __init__(self, seed, fps=FPS, variant=None):
        variant = variant or VARIANTS[0]
        rng = random.Random(seed)
        self.dt = 1.0 / fps / SUB
        self.n = variant["rings"]
        self.g = variant["g"]
        step = (R_OUT - R0) / (self.n - 1)
        self.rings = []
        for k in range(self.n):
            r = R0 + k * step
            gap_px = rng.uniform(*variant["gap"]) + GAP_BONUS
            self.rings.append({
                "r": r,
                "gap": gap_px / r,
                "w": rng.uniform(*variant["spin"]) * (1 if k % 2 == 0 else -1),
                "a": rng.uniform(0, 2 * math.pi),
            })
        n = len(TEAMS)
        off = rng.uniform(0, 2 * math.pi)
        self.px, self.py, self.vx, self.vy = [], [], [], []
        for i in range(n):
            a = off + i * 2 * math.pi / n
            self.px.append(CX + 85 * math.cos(a))
            self.py.append(CY + 85 * math.sin(a))
            d = rng.uniform(0, 2 * math.pi)
            s = rng.uniform(650, 900)
            self.vx.append(s * math.cos(d))
            self.vy.append(s * math.sin(d))
        self.level = [0] * n
        self.level_t = [0.0] * n
        self.passing = [False] * n
        self.gone = [False] * n
        self.last_snd = [-1.0] * n
        self.order = []
        self.events = []          # (t, kind, ball, ring)
        self.flash = [0.0] * self.n
        self.flash_col = [0] * self.n
        self.t = 0.0

    def step_frame(self):
        for _ in range(SUB):
            self._step()
        fdt = self.dt * SUB
        self.flash = [max(0.0, f - fdt * 3.5) for f in self.flash]

    def _event(self, kind, i, k):
        self.events.append((self.t, kind, i, k))

    def _step(self):
        dt = self.dt
        for ring in self.rings:
            ring["a"] += ring["w"] * dt
        n = len(TEAMS)
        for i in range(n):
            if self.gone[i]:
                continue
            self.vy[i] += self.g * dt
            self.px[i] += self.vx[i] * dt
            self.py[i] += self.vy[i] * dt
            L = self.level[i]
            if L >= self.n:
                if self.py[i] > H + 80 or self.px[i] < -80 or self.px[i] > W + 80:
                    self.gone[i] = True
                continue
            ring = self.rings[L]
            dx, dy = self.px[i] - CX, self.py[i] - CY
            dist = math.hypot(dx, dy) or 1e-6
            inner = ring["r"] - RING_W / 2
            in_gap = abs(angdiff(math.atan2(dy, dx), ring["a"])) < ring["gap"] / 2 - (BALL_R / ring["r"]) * 0.85
            if self.passing[i]:
                if dist - BALL_R > ring["r"] + RING_W / 2:
                    self.passing[i] = False
                    self.level[i] += 1
                    self.level_t[i] = self.t
                    self.flash[L] = 1.0
                    self.flash_col[L] = i
                    if self.level[i] >= self.n:
                        self.order.append(i)
                        self._event("escape", i, L)
                    else:
                        self._event("pass", i, L)
                elif dist + BALL_R < inner:
                    self.passing[i] = False
            elif dist + BALL_R > inner:
                if in_gap:
                    self.passing[i] = True
                else:
                    nx, ny = dx / dist, dy / dist
                    vn = self.vx[i] * nx + self.vy[i] * ny
                    if vn > 0:
                        self.vx[i] -= 2 * vn * nx
                        self.vy[i] -= 2 * vn * ny
                        s = math.hypot(self.vx[i], self.vy[i])
                        ns = min(max(s * 1.015, VMIN), VMAX)
                        self.vx[i] *= ns / s
                        self.vy[i] *= ns / s
                        if vn > 160 and self.t - self.last_snd[i] > 0.07:
                            self.last_snd[i] = self.t
                            self._event("bounce", i, L)
                            self.flash[L] = max(self.flash[L], 0.45)
                            self.flash_col[L] = i
                    self.px[i] = CX + nx * (inner - BALL_R)
                    self.py[i] = CY + ny * (inner - BALL_R)
        # ball-ball collisions among balls on the same ring level
        for i in range(n):
            if self.level[i] >= self.n:
                continue
            for j in range(i + 1, n):
                if self.level[j] != self.level[i]:
                    continue
                dx, dy = self.px[j] - self.px[i], self.py[j] - self.py[i]
                d2 = dx * dx + dy * dy
                if d2 >= (2 * BALL_R) ** 2 or d2 == 0:
                    continue
                d = math.sqrt(d2)
                nx, ny = dx / d, dy / d
                rel = (self.vx[j] - self.vx[i]) * nx + (self.vy[j] - self.vy[i]) * ny
                if rel < 0:
                    self.vx[i] += rel * nx
                    self.vy[i] += rel * ny
                    self.vx[j] -= rel * nx
                    self.vy[j] -= rel * ny
                ov = (2 * BALL_R - d) / 2
                self.px[i] -= nx * ov
                self.py[i] -= ny * ov
                self.px[j] += nx * ov
                self.py[j] += ny * ov
        self.t += dt

    def ranking(self):
        rest = [i for i in range(len(TEAMS)) if i not in self.order]
        rest.sort(key=lambda i: (-self.level[i], self.level_t[i]))
        return self.order + rest


def hook_skip(sim):
    """Seconds of simulation to cut so the video opens just before the first ring break."""
    first_pass = next((t for t, kind, _, _ in sim.events if kind in ("pass", "escape")), None)
    return 0.0 if first_pass is None else max(0.0, first_pass - HOOK_LEAD)


def pick_seed(match, fps, variant, lo=RACE_LO, hi=RACE_HI):
    """Find a seed whose race (from the hook cut to the first escape) lasts [lo, hi] seconds."""
    best = None
    mid = (lo + hi) / 2
    for k in range(150):
        seed = match * 1000 + k
        s = Sim(seed, fps=fps, variant=variant)
        while not s.order and s.t < hi + 25:
            s.step_frame()
        if not s.order:
            continue
        race = s.t - hook_skip(s)
        if lo <= race <= hi:
            return seed
        if best is None or abs(race - mid) < abs(best[1] - mid):
            best = (seed, race)
    return best[0] if best else match * 1000


# ---------------------------------------------------------------- audio
def make_audio(events, total, melody_seed, path, winner_t):
    n = int(total * SR) + SR
    buf = np.zeros(n, dtype=np.float64)
    base = 261.63
    scale = [0, 2, 4, 7, 9, 12, 14, 16, 19, 21, 24]
    rng = random.Random(melody_seed)
    idx = 4

    def tone(t0, f, dur, amp, decay):
        s0 = int(t0 * SR)
        m = min(int(dur * SR), n - s0)
        if m <= 0:
            return
        tt = np.arange(m) / SR
        env = np.exp(-tt / decay) * np.minimum(1, tt / 0.004)
        w = np.sin(2 * np.pi * f * tt) + 0.35 * np.sin(4 * np.pi * f * tt) + 0.12 * np.sin(6 * np.pi * f * tt)
        buf[s0:s0 + m] += amp * env * w

    for t, kind, i, k in events:
        if t > winner_t + END_HOLD:
            break
        if kind == "bounce":
            idx = max(0, min(len(scale) - 1, idx + rng.choice([-2, -1, 1, 1, 2])))
            f = base * 2 ** (scale[idx] / 12)
            tone(t, f, 0.45, 0.22, 0.16)
        elif kind == "pass":
            f = base * 2 ** ((12 + 2 * k) / 12)
            tone(t, f, 0.6, 0.3, 0.25)
            tone(t + 0.07, f * 1.5, 0.6, 0.22, 0.25)
        elif kind == "escape":
            for d, semi in enumerate([0, 4, 7, 12]):
                tone(t + d * 0.06, base * 2 * 2 ** (semi / 12), 1.2, 0.28, 0.5)
    # fanfare when the results card opens
    tc = winner_t + END_HOLD
    for d, semi in enumerate([0, 4, 7, 12, 16, 19, 24]):
        tone(tc + d * 0.09, base * 2 ** (semi / 12), 1.6, 0.25, 0.6)
    buf = np.tanh(buf * 1.4)
    buf = buf / (np.max(np.abs(buf)) + 1e-9) * 0.89
    buf = buf[: int(total * SR)]
    fade = int(LOOP * SR)
    buf[-fade:] *= np.linspace(1.0, 0.0, fade)   # quiet tail so the loop restart is clean
    data = (buf * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())


# ---------------------------------------------------------------- drawing
FONT = "Arial"


def text(ctx, s, x, y, size, color=(1, 1, 1), alpha=1.0, anchor="c", bold=True):
    ctx.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
    ctx.set_font_size(size)
    ext = ctx.text_extents(s)
    if anchor == "c":
        x -= ext.width / 2 + ext.x_bearing
    elif anchor == "r":
        x -= ext.width + ext.x_bearing
    # shadow
    ctx.set_source_rgba(0, 0, 0, 0.55 * alpha)
    ctx.move_to(x + 3, y + 4)
    ctx.show_text(s)
    ctx.set_source_rgba(*color, alpha)
    ctx.move_to(x, y)
    ctx.show_text(s)
    ctx.new_path()


def rounded(ctx, x, y, w, h, r):
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    ctx.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    ctx.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    ctx.close_path()


def draw_bg(ctx, variant):
    g = cairo.LinearGradient(0, 0, 0, H)
    g.add_color_stop_rgb(0, *variant["bg"][0])
    g.add_color_stop_rgb(1, *variant["bg"][1])
    ctx.set_source(g)
    ctx.paint()
    rg = cairo.RadialGradient(CX, CY, 50, CX, CY, 600)
    rg.add_color_stop_rgba(0, *variant["glow"], 0.18)
    rg.add_color_stop_rgba(1, *variant["glow"], 0.0)
    ctx.set_source(rg)
    ctx.paint()


def draw_frame(ctx, game, match, t, winner_t, standings_after, season, variant, mode):
    draw_bg(ctx, variant)
    # header
    label = mode if mode != "ESCAPE" else variant["name"]
    text(ctx, f"BALL LEAGUE  ·  MATCH #{match}  ·  {label}", W / 2, 95, 30, (0.75, 0.75, 0.9))
    text(ctx, game.HEADLINE, W / 2, 195, 100 if len(game.HEADLINE) <= 15 else 78)
    pulse = 1.0 + (0.06 * math.sin(t * 10) if t < 3.0 else 0.0)
    text(ctx, "PICK YOUR COLOR!", W / 2, 285, 64 * pulse, (1, 0.84, 0.04))
    game.draw(ctx)

    # scoreboard: 2 rows x 4, right under the arena and clear of the Shorts UI
    cw, ch = 205, 64
    for i, (name, hx) in enumerate(TEAMS):
        col, row = i % 4, i // 4
        x0, y0 = 40 + col * 222, 1300 + row * (ch + 10)
        rounded(ctx, x0, y0, cw, ch, 14)
        ctx.set_source_rgba(1, 1, 1, 0.08)
        ctx.fill()
        c = rgb(hx)
        ctx.set_source_rgb(*c)
        ctx.arc(x0 + 22, y0 + 22, 10, 0, 2 * math.pi)
        ctx.fill()
        text(ctx, name, x0 + 40, y0 + 31, 24, (1, 1, 1), anchor="l")
        value, frac = game.chip(i)
        if value:
            text(ctx, value, x0 + cw - 10, y0 + 31, 22, (1, 0.84, 0.04), anchor="r")
        rounded(ctx, x0 + 14, y0 + 44, cw - 28, 11, 4)
        ctx.set_source_rgba(1, 1, 1, 0.15)
        ctx.fill()
        if frac > 0:
            rounded(ctx, x0 + 14, y0 + 44, max(11.0, (cw - 28) * min(1.0, frac)), 11, 4)
            ctx.set_source_rgb(*c)
            ctx.fill()

    # results card
    if winner_t is not None and t >= winner_t + END_HOLD:
        p = min(1.0, (t - winner_t - END_HOLD) / 0.25)
        ctx.new_path()
        ctx.set_source_rgba(0.02, 0.02, 0.06, 0.9 * p)
        ctx.paint()
        name, hx = TEAMS[game.ranking()[0]]
        c = rgb(hx)
        y = 260 + (1 - p) * 100
        text(ctx, "WINNER", W / 2, y, 62, (1, 1, 1), p)
        s = 1.0 + 0.04 * math.sin(t * 6)
        text(ctx, name, W / 2, y + 150, 160 * s, c, p)
        text(ctx, f"SEASON {season} STANDINGS", W / 2, y + 260, 44, (1, 0.84, 0.04), p)
        for r, (tname, pts) in enumerate(standings_after[:8]):
            yy = y + 335 + r * 70
            tc = rgb(dict(TEAMS)[tname])
            rounded(ctx, 150, yy - 49, 720, 62, 16)
            ctx.set_source_rgba(*tc, 0.22 * p)
            ctx.fill()
            text(ctx, f"{r + 1}.", 180, yy, 38, (1, 1, 1), p, anchor="l")
            ctx.set_source_rgba(*tc, p)
            ctx.arc(265, yy - 13, 15, 0, 2 * math.pi)
            ctx.fill()
            text(ctx, tname, 298, yy, 38, (1, 1, 1), p, anchor="l")
            text(ctx, f"{pts} pts", 845, yy, 38, (1, 1, 1), p, anchor="r")
        text(ctx, "COMMENT YOUR COLOR!", W / 2, 1340, 52, (1, 1, 1), p)


# ---------------------------------------------------------------- league
def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"season": 1, "matches": {}}


def standings(state, upto=None):
    pts = {n: 0 for n, _ in TEAMS}
    wins = {n: 0 for n, _ in TEAMS}
    for m, res in state["matches"].items():
        if upto is not None and int(m) > upto:
            continue
        for pos, name in enumerate(res["ranking"]):
            pts[name] += POINTS[pos]
        wins[res["ranking"][0]] += 1
    return sorted(pts.items(), key=lambda kv: (-kv[1], -wins[kv[0]], kv[0]))


# Stakes first, short, no match numbers (they mean nothing to new viewers; they live in the description)
TITLES = [
    "Only 1 Ball Can Escape 😱 Pick Yours",
    "Pick a Color Before It Starts 🔴🔵🟢",
    "Your Color vs 7 Others 🏁",
    "{rings} Rings, 8 Balls, Only 1 Escapes 🤯",
    "Which Color Gets Out First? 👀",
    "If Your Color Wins, You Win 🏆",
    "This One Is Way Too Close 😳",
]


def metadata(match, season, ranking, table, date, variant, mode="ESCAPE"):
    import modes
    game_cls = modes.MODE_CLASSES[mode]
    leader = table[0][0].title()
    titles = game_cls.TITLES
    title = titles[(match - 1) % len(titles)].format(n=match, leader=leader, rings=variant["rings"])
    rule = game_cls.RULE.format(rings=variant["rings"])
    lines = [
        "👇 Which color did you pick BEFORE the race? Comment it below!",
        "",
        f"Ball League — Season {season}, Match #{match} ({date}).",
        f"Today's game: {mode.title()} — {rule} "
        "A new match every day, every game counts for the season — pick your color!",
        "",
        f"🏆 Winner: {ranking[0]}",
        "",
        "📊 Season standings:",
    ]
    lines += [f"{i + 1}. {n} — {p} pts" for i, (n, p) in enumerate(table)]
    lines += [
        "",
        "Points: 1st 10 · 2nd 8 · 3rd 6 · 4th 5 · 5th 4 · 6th 3 · 7th 2 · 8th 1",
        "Comment your color 👇 and come back tomorrow for the next match!",
        "",
        "Original physics simulation and sound, generated with code.",
        "#shorts #satisfying #simulation #ballrace #marblerace",
    ]
    return {
        "title": title[:100],
        "description": "\n".join(lines),
        "tags": ["ball race", "satisfying", "physics simulation", "marble race", "which color wins",
                 "ball league", "oddly satisfying", "bouncing balls", "shorts"],
        "categoryId": "20",
        "madeForKids": False,
    }


# ---------------------------------------------------------------- main
def render(match, preview=False):
    import modes
    state = load_state()
    season = state["season"]
    fps = 30 if preview else FPS
    variant = variant_for(match)
    mode = modes.mode_for(match)
    game_cls = modes.MODE_CLASSES[mode]
    seed = modes.pick_seed(mode, match, fps, variant)

    # pass 1: full sim to know when the result is decided and the ranking (deterministic)
    probe = game_cls(seed, fps, variant)
    while not probe.decided and probe.t < 80:
        probe.step_frame()
    skip_frames = int(round(hook_skip(probe.sim) * fps)) if mode == "ESCAPE" else 0
    skip = skip_frames / fps
    winner_t = probe.decide_t - skip                  # all times below are video times
    end_t = winner_t + END_HOLD
    while probe.t < end_t + skip:
        probe.step_frame()
    ranking = [TEAMS[i][0] for i in probe.ranking()]
    total = end_t + CARD
    events = [(t - skip, kind, i, k) for t, kind, i, k in probe.events if t >= skip]

    state["matches"][str(match)] = {"seed": seed, "ranking": ranking, "variant": variant["name"], "mode": mode,
                                    "date": datetime.date.today().isoformat()}
    table = standings(state, upto=match)

    folder = OUT / f"match_{match:03d}"
    folder.mkdir(parents=True, exist_ok=True)
    wav = folder / "audio.wav"
    make_audio(events, total, seed, wav, winner_t)

    scale = 0.5 if preview else 1.0
    ow, oh = int(W * scale), int(H * scale)
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, ow, oh)
    ctx = cairo.Context(surf)
    ctx.scale(scale, scale)
    mp4 = folder / ("preview.mp4" if preview else "video.mp4")
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{ow}x{oh}", "-r", str(fps), "-i", "-",
           "-i", str(wav), "-c:v", "libx264", "-preset", "veryfast" if preview else "medium",
           "-crf", "19", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
           "-shortest", "-movflags", "+faststart", str(mp4)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    game = game_cls(seed, fps, variant)
    for _ in range(skip_frames):                      # fast-forward to the hook (no frames written)
        game.step_frame()
    first = None
    loop_from = total - LOOP
    nframes = int(total * fps)
    thumb_frame = int(min(winner_t * 0.6, 8.0) * fps)
    for f in range(nframes):
        t = f / fps
        if t < end_t:
            game.step_frame()
        draw_frame(ctx, game, match, t, winner_t if t >= winner_t else None, table, season, variant, mode)
        if first is None:
            surf.flush()
            first = cairo.ImageSurface(cairo.FORMAT_ARGB32, ow, oh)
            fc = cairo.Context(first)
            fc.set_source_surface(surf, 0, 0)
            fc.paint()
        elif t >= loop_from:
            ctx.save()
            ctx.identity_matrix()
            ctx.set_source_surface(first, 0, 0)
            ctx.paint_with_alpha(min(1.0, (t - loop_from) / max(LOOP - 1.5 / fps, 1e-3)))
            ctx.restore()
        surf.flush()
        proc.stdin.write(bytes(surf.get_data()))
        if f == thumb_frame:
            surf.write_to_png(str(folder / "thumbnail.png"))
    proc.stdin.close()
    proc.wait()
    wav.unlink(missing_ok=True)

    if not preview:
        STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
        meta = metadata(match, season, ranking, table, state["matches"][str(match)]["date"], variant, mode)
        (folder / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"match {match} [{mode} / {variant['name']}]: seed {seed}, winner {ranking[0]} at {winner_t:.1f}s, "
          f"length {total:.1f}s -> {mp4}")
    return mp4


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--match", type=int)
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--count", type=int, default=1, help="render this many consecutive matches")
    ap.add_argument("--if-needed", action="store_true",
                    help="render only if no rendered match is still waiting to be uploaded")
    a = ap.parse_args()
    if a.if_needed:
        waiting = [f.name for f in sorted(OUT.glob("match_*")) if (f / "video.mp4").exists()
                   and (f / "metadata.json").exists() and not (f / "uploaded.json").exists()]
        if waiting:
            print(f"not rendering: {', '.join(waiting)} still waiting to be uploaded")
            return
        today = datetime.date.today().isoformat()
        done_today = [u.parent.name for u in OUT.glob("match_*/uploaded.json")
                      if json.loads(u.read_text(encoding="utf-8")).get("uploaded", "").startswith(today)]
        if done_today:
            print(f"not rendering: already published today ({', '.join(done_today)})")
            return
    state = load_state()
    start = a.match or (max(map(int, state["matches"]), default=0) + 1)
    for m in range(start, start + a.count):
        render(m, a.preview)


if __name__ == "__main__":
    main()
