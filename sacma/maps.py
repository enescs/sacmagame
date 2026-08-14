"""Map definitions. Pure data -- the server builds runtime objects from these,
and clients use the same table to draw, so only a map index goes over the wire.

Geometry is laid out in 180-degree rotationally symmetric pairs about the arena
centre, i.e. rot(x, y, w, h) == (ARENA_W - x - w, ARENA_H - y - h, w, h), so no
spawn point is meaningfully better than another.

  walls  -- static rectangles (x, y, w, h); the border is added automatically
  movers -- rectangles that ping-pong along an offset: (x, y, w, h, dx, dy,
            period_seconds, phase_0_to_1)
  rotors -- bars sweeping around a pivot, treated as capsules: (cx, cy,
            half_length, bar_radius, radians_per_second, phase_radians)
  spawns -- round start positions, one per player slot
"""

import math

from .shared import ARENA_H, ARENA_W

CORNERS = [
    (84, 84), (ARENA_W - 84, ARENA_H - 84),
    (ARENA_W - 84, 84), (84, ARENA_H - 84),
    (ARENA_W // 2, 74), (ARENA_W // 2, ARENA_H - 74),
    (74, ARENA_H // 2), (ARENA_W - 74, ARENA_H // 2),
]


def _grid_pillars():
    """Regular pillar field, centred so the whole grid is symmetric."""
    out = []
    for i in range(5):
        for j in range(3):
            out.append((152 + i * 230, 130 + j * 202, 56, 56))
    return out


MAPS = [
    {
        "name": "Crossfire",
        "blurb": "plain cover, no surprises",
        "walls": [
            (300, 140, 24, 180), (956, 400, 24, 180),
            (300, 140, 180, 24), (800, 556, 180, 24),
            (880, 200, 24, 150), (376, 370, 24, 150),
            (200, 560, 150, 24), (930, 136, 150, 24),
            (560, 330, 160, 60),  # self-symmetric centre block
        ],
        "movers": [],
        "rotors": [],
        "spawns": CORNERS,
    },
    {
        "name": "Millstone",
        "blurb": "two big sweepers, thin cover",
        "walls": [
            (100, 100, 120, 24), (1060, 596, 120, 24),
            (1060, 100, 120, 24), (100, 596, 120, 24),
            (620, 120, 40, 120), (620, 480, 40, 120),
        ],
        "movers": [],
        "rotors": [
            (400, 360, 150, 17, 1.10, 0.0),
            (880, 360, 150, 17, -1.10, 1.5708),
        ],
        "spawns": CORNERS,
    },
    {
        "name": "Pistons",
        "blurb": "sliding blocks, mind the timing",
        "walls": [
            (160, 320, 120, 24), (1000, 376, 120, 24),
            (240, 560, 24, 110), (1016, 50, 24, 110),
            (420, 620, 160, 24), (700, 76, 160, 24),
            (600, 330, 80, 60),
        ],
        "movers": [
            (200, 180, 140, 28, 560, 0, 7.0, 0.00),
            (740, 512, 140, 28, -560, 0, 7.0, 0.00),
            (560, 120, 28, 120, 0, 360, 5.5, 0.30),
            (692, 480, 28, 120, 0, -360, 5.5, 0.30),
        ],
        "rotors": [],
        "spawns": CORNERS,
    },
    {
        "name": "Carousel",
        "blurb": "four rotors, everything moves",
        "walls": [
            (590, 320, 100, 80),
            (140, 140, 100, 24), (1040, 556, 100, 24),
            (1040, 140, 100, 24), (140, 556, 100, 24),
        ],
        "movers": [],
        "rotors": [
            (340, 220, 112, 15, 1.35, 0.0),
            (940, 220, 112, 15, -1.35, 0.7854),
            (340, 500, 112, 15, -1.35, 1.5708),
            (940, 500, 112, 15, 1.35, 2.3562),
        ],
        "spawns": CORNERS,
    },
    {
        "name": "Gridlock",
        "blurb": "pillar maze, corner peeking",
        "walls": _grid_pillars(),
        "movers": [],
        "rotors": [],
        "spawns": CORNERS,
    },
]


def mover_rect(m, t):
    """Where a mover's rectangle sits at time `t`, as (x, y, w, h)."""
    x, y, w, h, dx, dy, period, phase = m
    # Triangle wave in [0, 1]: out along the offset, then back again.
    f = 2.0 * abs(((t / period + phase) % 1.0) - 0.5)
    # Negative extents just mean "anchored at the far edge"; normalise them so
    # collision code only ever sees positive width/height.
    if w < 0:
        x, w = x + w, -w
    if h < 0:
        y, h = y + h, -h
    return (x + dx * f, y + dy * f, w, h)


def rotor_segment(r, t):
    """A rotor's bar at time `t`, as ((x0, y0), (x1, y1), bar_radius)."""
    cx, cy, half, rad, speed, phase = r
    a = phase + speed * t
    ox, oy = math.cos(a) * half, math.sin(a) * half
    return (cx - ox, cy - oy), (cx + ox, cy + oy), rad
