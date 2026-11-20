"""Ball League channel logo (800x800 PNG, safe inside YouTube's circular crop)."""
import math
import cairo
S = 800
C = [("#ff3b3b"), ("#ff8a00"), ("#ffd60a"), ("#22d36b"), ("#19e3e3"), ("#2f7bff"), ("#a259ff"), ("#ff4fd8")]
rgb = lambda h: tuple(int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, S, S)
ctx = cairo.Context(surf)
g = cairo.RadialGradient(S / 2, S * 0.42, 40, S / 2, S / 2, S * 0.72)
g.add_color_stop_rgb(0, 0.13, 0.12, 0.30)
g.add_color_stop_rgb(1, 0.03, 0.03, 0.09)
ctx.set_source(g); ctx.paint()
cx = cy = S / 2
# arena ring with a gap (the "escape" door)
ctx.set_line_width(22); ctx.set_line_cap(cairo.LINE_CAP_ROUND)
ctx.set_source_rgba(1, 1, 1, 0.92)
ctx.arc(cx, cy, 300, math.radians(-60), math.radians(240)); ctx.stroke()
# 8 glossy balls around the inside of the ring
for i, h in enumerate(C):
    a = math.radians(-90 + 360 * i / 8 + 22.5)
    x, y, r = cx + 238 * math.cos(a), cy + 238 * math.sin(a), 38
    c = rgb(h)
    glow = cairo.RadialGradient(x, y, r * 0.5, x, y, r * 1.8)
    glow.add_color_stop_rgba(0, *c, 0.45); glow.add_color_stop_rgba(1, *c, 0)
    ctx.set_source(glow); ctx.arc(x, y, r * 1.8, 0, 2 * math.pi); ctx.fill()
    b = cairo.RadialGradient(x - r * 0.35, y - r * 0.4, r * 0.1, x, y, r)
    b.add_color_stop_rgb(0, *(min(1, v + 0.45) for v in c)); b.add_color_stop_rgb(1, *(v * 0.75 for v in c))
    ctx.set_source(b); ctx.arc(x, y, r, 0, 2 * math.pi); ctx.fill()
# one ball escaping through the gap, top
x, y, r = cx + 0, cy - 300 - 4, 30
ctx.set_source_rgb(1, 0.84, 0.04); ctx.arc(x, y, r, 0, 2 * math.pi); ctx.fill()
# wordmark
def word(s, y, size, col):
    ctx.select_font_face("Arial Black", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    ctx.set_font_size(size)
    e = ctx.text_extents(s)
    for dx, dy, cc in ((0, 7, (0, 0, 0, 0.55)), (0, 0, col)):
        ctx.move_to(cx - e.x_advance / 2 + dx, y + dy); ctx.set_source_rgba(*cc); ctx.show_text(s)
word("BALL", cy + 2, 100, (1, 1, 1, 1))
word("LEAGUE", cy + 78, 58, (1, 0.84, 0.04, 1))
surf.write_to_png("branding/ball_league_logo.png")
print("ok")
