"""Constants and geometry shared by the server simulation and the client renderer.

Both sides must agree on these numbers exactly, so this module is imported by
`game.py` (authoritative sim) and `client.py` (rendering) alike. It deliberately
has no dependencies beyond the standard library -- the server must run without
pygame installed.
"""

import math

# --- networking ---------------------------------------------------------------

DEFAULT_PORT = 50500
DISCOVERY_PORT = 50505
DISCOVERY_MAGIC = "sacma-lan-v1"
MAX_PLAYERS = 8

# --- game modes --------------------------------------------------------------
# The host picks the starting mode with --mode, and anybody can change it from
# inside the game with [M]. A change always lands on the next round rather than
# the current one, so nobody has the match pulled out from under them. The
# current mode rides along in the beacon so the client's list can show it.

MODE_FFA = 0    # last player standing, with the occasional boss round
MODE_CTF = 1    # two teams of two, grab the flag, take it home
MODE_BOSS = 2   # every round is a boss round

MODES = {"ffa": MODE_FFA, "ctf": MODE_CTF, "boss": MODE_BOSS}
MODE_LABEL = {
    MODE_FFA: "free-for-all",
    MODE_CTF: "capture the flag",
    MODE_BOSS: "boss rush",
}
MODE_BLURB = {
    MODE_FFA: "last one standing, boss round now and then",
    MODE_CTF: "2v2, first to three captures  (four players max)",
    MODE_BOSS: "every round has a boss, everyone else teams up",
}
# Order the in-game picker walks through.
MODE_ORDER = (MODE_FFA, MODE_BOSS, MODE_CTF)

MODE_CHANGE_GAP = 1.5  # seconds a client must wait between mode requests

TICK_RATE = 60  # server simulation steps per second (also the snapshot rate)

CHAT_MAX_LEN = 90      # characters kept from a client's message
CHAT_MIN_GAP = 0.5     # seconds a client must wait between messages
CHAT_SHOW_TIME = 9.0   # seconds a message stays on screen
CHAT_MAX_LINES = 6     # most recent messages shown at once

# --- arena -------------------------------------------------------------------

ARENA_W, ARENA_H = 1280, 720
HUD_H = 92
WINDOW_W, WINDOW_H = ARENA_W, ARENA_H + HUD_H

WALL = 16  # border thickness

BORDER = [
    (0, 0, ARENA_W, WALL),
    (0, ARENA_H - WALL, ARENA_W, WALL),
    (0, 0, WALL, ARENA_H),
    (ARENA_W - WALL, 0, WALL, ARENA_H),
]

# --- round flow --------------------------------------------------------------

PHASE_WAITING = 0   # fewer than two players connected; free roam
PHASE_COUNTDOWN = 1
PHASE_LIVE = 2
PHASE_OVER = 3

COUNTDOWN_TIME = 3.0
ROUND_TIME_LIMIT = 100.0  # draw if nobody has won by then
ROUND_OVER_TIME = 4.0

# --- player ------------------------------------------------------------------

PLAYER_RADIUS = 14
PLAYER_SPEED = 265.0  # px/s
RESPAWN_GRACE = 0.0

# --- boss rounds (free-for-all only) -----------------------------------------
# Every so often one player is promoted and everybody else has to gang up on
# them. Rolled per round, so the gap between boss rounds is geometric with a
# mean of 1/BOSS_CHANCE -- about seven rounds.

BOSS_CHANCE = 1.0 / 7.0
BOSS_MIN_PLAYERS = 3      # a boss with only one hunter is just a duel

# The boss is the only thing in the game that takes more than one bullet, and
# it scales with the size of the mob so three hunters is not a formality.
BOSS_HP_BASE = 3
BOSS_HP_PER_FOE = 2
BOSS_HP_MAX = 15

BOSS_RADIUS = 22          # bigger, so it is also easier to hit
BOSS_SPEED_MULT = 1.22
BOSS_FIRE_MULT = 0.55     # fire cooldown multiplier
BOSS_BULLET_MULT = 1.15   # bullet speed multiplier
BOSS_MAG = 14
BOSS_RELOAD = 1.15
BOSS_COLOR = (255, 96, 72)

# --- capture the flag --------------------------------------------------------

CTF_TEAM_SIZE = 2         # 2v2, so the server caps at four players in this mode
CTF_CAPTURES_TO_WIN = 3
CTF_ROUND_TIME = 180.0
CTF_RESPAWN_TIME = 3.0    # rounds do not end on a kill here, so death is a timer
# Long enough to get off your own stand before anyone can touch you, short
# enough that it is no use for anything else. It ends the moment you shoot or
# grab a flag, so it can never be carried into a fight.
CTF_SPAWN_IMMUNITY = 2.0
FLAG_RADIUS = 12
FLAG_RETURN_TIME = 12.0   # a dropped flag goes home on its own eventually
BASE_RADIUS = 34
# Carrying it is a serious commitment: half speed, so anyone who spots you
# runs you down easily and the flag only travels while your team is covering
# you. Sprint roughly cancels it out (1.45 * 0.5 = 0.73 -- still slower than a
# free jog), which makes a loose sprint the thing to fight over on the way out.
FLAG_CARRY_MULT = 0.5

FLAG_HOME = 0
FLAG_CARRIED = 1
FLAG_DROPPED = 2

TEAM_COLORS = [(96, 176, 255), (255, 118, 118)]
TEAM_NAMES = ["BLUE", "RED"]

# --- the one weapon ----------------------------------------------------------
# One bullet kills, so the interesting decisions are all about ammo and angles.

BULLET_RADIUS = 3
BULLET_SPEED = 760.0  # px/s
BULLET_LIFETIME = 1.4  # seconds
# A tick's flight is checked against the map in hops no longer than this. The
# thinnest wall in the game is 24px and the border is 16px, while a golden
# round covers 40px in a tick -- testing only where the round landed lets the
# fast ones step clean over cover.
BULLET_STEP = 8.0
FIRE_COOLDOWN = 0.26   # hold to fire, ~4 shots/s
MAG_SIZE = 6
RELOAD_TIME = 1.5
SPREAD = 0.018  # radians of random cone, purely for feel

# --- loot & powerups ---------------------------------------------------------

BOX_RADIUS = 13
LOOT_FIRST_DROP = 6.0     # seconds into a live round
LOOT_INTERVAL = 9.0       # +/- LOOT_JITTER between drops
LOOT_JITTER = 3.0
LOOT_MAX_ON_FIELD = 4
# Drops cluster on the middle of the map, so contesting loot means leaving cover.
LOOT_SPREAD_X = 190.0
LOOT_SPREAD_Y = 120.0

P_SHIELD = "shield"
P_AMMO = "ammo"
P_RAPID = "rapid"
P_VELOCITY = "velocity"
P_SWIFT = "swift"
P_SCATTER = "scatter"
P_INVIS = "invis"
P_GOLDEN = "golden"
P_REFLECT = "reflect"
P_HOMING = "homing"
P_BOUNCE = "bounce"
P_FROST = "frost"
P_HOLD = "hold"
P_GHOST = "ghost"
P_QUAKE = "quake"

# Order fixes the bit each one gets in a snapshot, so only ever append.
POWERUPS = [P_SHIELD, P_AMMO, P_RAPID, P_VELOCITY, P_SWIFT, P_SCATTER,
            P_INVIS, P_GOLDEN, P_REFLECT, P_HOMING, P_BOUNCE, P_FROST,
            P_HOLD, P_GHOST, P_QUAKE]
POWERUP_BIT = {name: 1 << i for i, name in enumerate(POWERUPS)}

POWERUP_DURATION = {
    # Short on purpose: a shield is a free life in a one-shot game, so it
    # should force you to go and use it rather than sit on it.
    P_SHIELD: 7.0,     # or until it eats a bullet, whichever comes first
    # Never reloading is the strongest thing a plain rifle can do, so it
    # gets a short window rather than the ten seconds it used to run for.
    P_AMMO: 6.0,
    P_RAPID: 10.0,
    P_VELOCITY: 12.0,
    P_SWIFT: 10.0,
    P_SCATTER: 10.0,
    P_INVIS: 3.0,
    P_GOLDEN: 5.0,
    P_REFLECT: 5.0,
    P_HOMING: 5.0,
    P_BOUNCE: 8.0,
    P_FROST: 8.0,
    P_HOLD: 1.5,   # must match HOLD_TIME: the effect *is* the duration
    # Short, because cover stops meaning anything while it is running -- for
    # you and, once they work out what you picked up, for everyone aiming at
    # the wall you are standing behind.
    P_GHOST: 6.0,
    P_QUAKE: 3.0,   # how long everyone else's window rattles for
}

POWERUP_LABEL = {
    P_SHIELD: "SHIELD",
    P_AMMO: "INF AMMO",
    P_RAPID: "RAPID FIRE",
    P_VELOCITY: "HOT ROUNDS",
    P_SWIFT: "SPRINT",
    P_SCATTER: "SCATTER",
    P_INVIS: "INVISIBLE",
    P_GOLDEN: "GOLDEN GUN",
    P_REFLECT: "REFLECT",
    P_HOMING: "HOMING",
    P_BOUNCE: "RICOCHET",
    P_FROST: "FROST",
    P_HOLD: "TIME STOP",
    P_GHOST: "GHOST ROUNDS",
    P_QUAKE: "EARTHQUAKE",
}

POWERUP_COLOR = {
    P_SHIELD: (122, 208, 255),
    P_AMMO: (255, 199, 119),
    P_RAPID: (247, 118, 142),
    P_VELOCITY: (255, 145, 77),
    P_SWIFT: (158, 232, 148),
    P_SCATTER: (198, 160, 246),
    P_INVIS: (150, 158, 186),
    P_GOLDEN: (255, 214, 92),
    P_REFLECT: (120, 255, 224),
    P_HOMING: (255, 128, 200),
    P_BOUNCE: (255, 246, 143),
    P_FROST: (168, 246, 255),
    P_HOLD: (226, 214, 255),
    P_GHOST: (126, 158, 255),
    P_QUAKE: (206, 148, 96),
}

RAPID_MULT = 0.38       # fire cooldown multiplier
VELOCITY_MULT = 1.7     # bullet speed multiplier
SWIFT_MULT = 1.45       # move speed multiplier
SCATTER_ANGLE = 0.13    # radians between scatter pellets

# Golden gun: one shot in the magazine, slow to reload, but the round is fast
# and goes straight through a shield. Overrides scatter -- it stays a rifle.
GOLDEN_MAG = 1
# Reload has come down twice: 2.2s -> 1.47s, then halved again. At 0.74s the
# single round is back almost as fast as you can re-aim, so the golden gun is
# now a fast-cycling one-shot rifle rather than a one-chance gamble.
GOLDEN_RELOAD = 0.74
GOLDEN_SPEED_MULT = 3.2

# Reflect: a parried round is sent back the way it came, and now belongs to
# whoever parried it, so a good parry is a kill.
REFLECT_SPEED_MULT = 1.15

# Homing: steering is rate-limited so bullets curve rather than snap, which
# leaves cover and distance as real counters. Turn radius is speed/turn, so
# 6.0 gives ~127px -- tight enough to actually come around on someone at
# mid-range, where 3.2 just sailed past them.
HOMING_TURN = 6.0       # radians per second
HOMING_RANGE = 460.0    # px; beyond this a bullet flies straight

# Ricochet: rounds bounce off walls and rotor bars for a short while. They can
# never hit the player who fired them -- the sim skips the owner in every hit
# test -- so the risk is purely that a stray angle comes back at a teammate's
# position, not at you. A short leash keeps a bounced round from pinballing
# around the arena long after the fight moved on.
BOUNCE_MAX = 3          # wall hits before the round dies
BOUNCE_DAMP = 0.94      # speed kept per bounce
BOUNCE_LIFETIME = 2.2   # seconds; longer than a plain round so bounces land

# Ghost: rounds ignore walls, blocks and rotors entirely. The arena border
# still stops them, or half the shots on the map would sail off into nothing.
# Ghost beats ricochet where they meet: a round that passes through a wall
# never touches one, so there is nothing for it to bounce off.
GHOST_SPEED_MULT = 0.85  # slower, so the shot through a wall is still a read

# Earthquake: the one powerup that does nothing to the simulation. It rattles
# every rival's actual OS window for a few seconds -- their aim wanders because
# the window moves under the mouse, not because anything in the game changed.
# Deliberately gentle: enough to put a shot off, not enough to be unplayable
# or to make anybody feel ill.
QUAKE_TIME = 3.0
# Amplitude is what "stronger" means here -- it is the distance the window
# actually moves, and so directly how far a shot drifts. Frequency comes down
# a little as it goes up: a big fast wobble is a nauseating buzz, while a big
# slower one reads as ground moving, which is the idea.
QUAKE_AMPLITUDE = 20.0  # px of window travel at the start, decaying to zero
QUAKE_FREQ = 9.0        # rad/s of the base wobble

# Bullet flags, so clients can draw special rounds differently.
BF_GOLD = 1
BF_HOMING = 2
BF_PARRIED = 4
BF_BOUNCE = 8
BF_FROST = 16
BF_GHOST = 32

# Frost: a round leaves a patch of ice wherever it finally stops. Everyone who
# walks through one is slowed -- except the player who fired it, so you can lay
# a field down across your own escape route. Zones are area denial rather than
# damage, so they are generous in size and short in life.
FROST_RADIUS = 74.0
FROST_TIME = 3.5     # seconds a patch lingers
FROST_SLOW = 0.55    # move speed multiplier inside one
FROST_MAX = 8        # patches on the field at once, oldest dropped first

# Time stop: picking one up halts the entire arena -- every other player, every
# bullet already in the air, and the moving obstacles -- for a moment. The
# holder keeps playing, and rounds they fire during the stop still travel, so
# the window is long enough to line up one shot and no longer.
HOLD_TIME = 1.5

# --- presentation ------------------------------------------------------------

# One per player slot; index is assigned on join and sent in the roster.
PLAYER_COLORS = [
    (247, 118, 142),  # red
    (122, 208, 255),  # blue
    (158, 232, 148),  # green
    (255, 199, 119),  # amber
    (198, 160, 246),  # violet
    (240, 240, 240),  # white
    (129, 236, 226),  # teal
    (255, 145, 77),   # orange
]

BG = (18, 20, 28)
GRID = (26, 29, 40)
WALL_FILL = (52, 58, 78)
WALL_EDGE = (80, 88, 116)
HAZARD_FILL = (94, 72, 96)
HAZARD_EDGE = (176, 118, 176)
HUD_BG = (13, 14, 20)
TEXT = (226, 230, 240)
TEXT_DIM = (128, 136, 156)


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def circle_hits_rect(cx, cy, r, rect):
    """True if a circle overlaps an axis-aligned rectangle."""
    rx, ry, rw, rh = rect
    nx = clamp(cx, rx, rx + rw)
    ny = clamp(cy, ry, ry + rh)
    dx = cx - nx
    dy = cy - ny
    return dx * dx + dy * dy < r * r


def point_in_rect(x, y, rect):
    rx, ry, rw, rh = rect
    return rx <= x <= rx + rw and ry <= y <= ry + rh


def closest_point_on_segment(px, py, x0, y0, x1, y1):
    """Point on segment (x0,y0)-(x1,y1) nearest to (px,py)."""
    dx, dy = x1 - x0, y1 - y0
    seg2 = dx * dx + dy * dy
    if seg2 <= 1e-9:
        return x0, y0
    t = clamp(((px - x0) * dx + (py - y0) * dy) / seg2, 0.0, 1.0)
    return x0 + dx * t, y0 + dy * t


def segment_hits_circle(x0, y0, x1, y1, cx, cy, r):
    """Closest-approach test between a segment and a circle."""
    px, py = closest_point_on_segment(cx, cy, x0, y0, x1, y1)
    dx, dy = px - cx, py - cy
    return dx * dx + dy * dy <= r * r


def segments_min_dist_sq(ax0, ay0, ax1, ay1, bx0, by0, bx1, by1):
    """Squared distance between two segments (endpoint-sampled).

    Exact for the cases we care about -- a short bullet step against a long
    rotor bar -- and far shorter than the fully general formulation.
    """
    best = float("inf")
    for px, py, qx0, qy0, qx1, qy1 in (
        (ax0, ay0, bx0, by0, bx1, by1),
        (ax1, ay1, bx0, by0, bx1, by1),
        (bx0, by0, ax0, ay0, ax1, ay1),
        (bx1, by1, ax0, ay0, ax1, ay1),
    ):
        cx, cy = closest_point_on_segment(px, py, qx0, qy0, qx1, qy1)
        d = (cx - px) ** 2 + (cy - py) ** 2
        if d < best:
            best = d
    return best


def hypot(dx, dy):
    return math.hypot(dx, dy)
