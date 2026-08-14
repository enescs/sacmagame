"""Authoritative game simulation.

Deliberately free of networking and rendering: the server owns one `Game`,
steps it at a fixed rate, and serialises `snapshot()` to every client. Clients
never simulate anything -- they send inputs and draw whatever comes back. On a
LAN the round trip is a millisecond or two, so this stays responsive without
any prediction or rollback machinery.

Round flow: with two or more players connected we cycle
COUNTDOWN -> LIVE -> OVER -> COUNTDOWN on a fresh map. One bullet kills, so a
round ends the moment a single player is left standing. Alone on the server you
sit in WAITING, which is a free-roam practice mode with instant respawns.
"""

import math
import random

from .maps import MAPS, mover_rect, rotor_segment
from .shared import (
    ARENA_H, ARENA_W, BORDER, BOX_RADIUS, BULLET_LIFETIME, BULLET_RADIUS,
    BULLET_SPEED, COUNTDOWN_TIME, FIRE_COOLDOWN, LOOT_FIRST_DROP,
    LOOT_INTERVAL, LOOT_JITTER, LOOT_MAX_ON_FIELD, LOOT_SPREAD_X,
    LOOT_SPREAD_Y, MAG_SIZE, MAX_PLAYERS, PHASE_COUNTDOWN, PHASE_LIVE,
    PHASE_OVER, PHASE_WAITING, PLAYER_RADIUS, PLAYER_SPEED, POWERUPS,
    POWERUP_BIT, POWERUP_DURATION, P_AMMO, P_RAPID, P_SCATTER, P_SHIELD,
    P_SWIFT, P_VELOCITY, RAPID_MULT, RELOAD_TIME, ROUND_OVER_TIME,
    ROUND_TIME_LIMIT, SCATTER_ANGLE, SPREAD, SWIFT_MULT, VELOCITY_MULT,
    circle_hits_rect, clamp, closest_point_on_segment, point_in_rect,
    segment_hits_circle, segments_min_dist_sq,
)


class Player:
    __slots__ = (
        "pid", "name", "color", "x", "y", "aim", "alive", "waiting",
        "ammo", "reload_left", "cooldown", "powers", "wins", "kills",
        "deaths", "inp",
    )

    def __init__(self, pid, name, color):
        self.pid = pid
        self.name = name
        self.color = color
        self.x = 0.0
        self.y = 0.0
        self.aim = 0.0
        self.alive = False
        self.waiting = True   # joined mid-round; sits out until the next one
        self.ammo = MAG_SIZE
        self.reload_left = 0.0
        self.cooldown = 0.0
        self.powers = {}      # powerup name -> expiry timestamp
        self.wins = 0
        self.kills = 0
        self.deaths = 0
        self.inp = {"up": False, "down": False, "left": False, "right": False,
                    "shoot": False, "reload": False, "aim": 0.0}

    def has(self, power, now):
        return self.powers.get(power, 0.0) > now

    def power_bits(self, now):
        bits = 0
        for name in POWERUPS:
            if self.powers.get(name, 0.0) > now:
                bits |= POWERUP_BIT[name]
        return bits


class Bullet:
    __slots__ = ("x", "y", "vx", "vy", "ttl", "owner")

    def __init__(self, x, y, vx, vy, owner):
        self.x = x
        self.y = y
        self.vx = vx
        self.vy = vy
        self.ttl = BULLET_LIFETIME
        self.owner = owner


class LootBox:
    __slots__ = ("bid", "x", "y", "kind")

    def __init__(self, bid, x, y, kind):
        self.bid = bid
        self.x = x
        self.y = y
        self.kind = kind


class Game:
    def __init__(self):
        self.players = {}
        self.bullets = []
        self.loot = []
        self.time = 0.0
        self.tick = 0
        self.events = []  # drained by the server after each broadcast

        self.map_index = random.randrange(len(MAPS))
        self.round_no = 0
        self.phase = PHASE_WAITING
        self.phase_end = 0.0
        self.map_time = 0.0    # obstacle clock, reset per round so it is fair
        self.next_loot = 0.0
        self.last_winner = ""

        self._next_pid = 1
        self._next_bid = 1
        self.rects = []
        self.bars = []
        self._rebuild_geometry()

    # -- roster ---------------------------------------------------------------

    def free_slots(self):
        return MAX_PLAYERS - len(self.players)

    def add_player(self, name):
        used = {p.color for p in self.players.values()}
        color = next((i for i in range(MAX_PLAYERS) if i not in used), 0)
        pid = self._next_pid
        self._next_pid += 1
        p = Player(pid, name, color)
        self.players[pid] = p
        self.events.append({"kind": "join", "name": name, "color": color})

        # Latecomers spectate the round in progress; if we were idle, they can
        # start moving immediately and may well kick off the first round.
        if self.phase in (PHASE_WAITING, PHASE_OVER):
            self._spawn(p, self._spawn_points())
            p.waiting = False
        return p

    def remove_player(self, pid):
        p = self.players.pop(pid, None)
        if not p:
            return
        self.bullets = [b for b in self.bullets if b.owner != pid]
        self.events.append({"kind": "leave", "name": p.name, "color": p.color})

    def set_input(self, pid, data):
        p = self.players.get(pid)
        if not p:
            return
        for key in ("up", "down", "left", "right", "shoot", "reload"):
            p.inp[key] = bool(data.get(key))
        try:
            p.inp["aim"] = float(data.get("aim", p.inp["aim"]))
        except (TypeError, ValueError):
            pass

    # -- map geometry ---------------------------------------------------------

    @property
    def map(self):
        return MAPS[self.map_index]

    def _rebuild_geometry(self):
        """Recompute solid shapes for the current instant.

        Static walls and moving blocks both end up as plain rectangles; rotor
        bars are capsules (a segment plus a radius). Everything downstream --
        player push-out, bullet blocking, loot placement -- works off these two
        lists and never needs to know which map is loaded.
        """
        m = self.map
        self.rects = list(BORDER) + list(m["walls"])
        self.rects.extend(mover_rect(mv, self.map_time) for mv in m["movers"])
        self.bars = []
        for r in m["rotors"]:
            (x0, y0), (x1, y1), rad = rotor_segment(r, self.map_time)
            self.bars.append((x0, y0, x1, y1, rad))

    def blocked(self, x, y, r):
        """Would a circle of radius `r` at (x, y) overlap solid geometry?"""
        for rect in self.rects:
            if circle_hits_rect(x, y, r, rect):
                return True
        for x0, y0, x1, y1, rad in self.bars:
            cx, cy = closest_point_on_segment(x, y, x0, y0, x1, y1)
            if (cx - x) ** 2 + (cy - y) ** 2 < (r + rad) ** 2:
                return True
        return False

    # -- round flow -----------------------------------------------------------

    def _spawn_points(self):
        pts = list(self.map["spawns"])
        random.shuffle(pts)
        return pts

    def _spawn(self, player, points):
        sx, sy = points.pop() if points else (ARENA_W / 2, ARENA_H / 2)
        player.x, player.y = float(sx), float(sy)
        player.alive = True
        player.waiting = False
        player.ammo = MAG_SIZE
        player.reload_left = 0.0
        player.cooldown = 0.0
        player.powers.clear()

    def _start_round(self):
        # Rotate maps in order but with a random start, so a session sees all
        # five rather than re-rolling the same one twice in a row.
        if self.round_no > 0:
            self.map_index = (self.map_index + 1) % len(MAPS)
        self.round_no += 1
        self.map_time = 0.0
        self._rebuild_geometry()

        self.bullets.clear()
        self.loot.clear()
        self.next_loot = LOOT_FIRST_DROP

        points = self._spawn_points()
        for p in self.players.values():
            self._spawn(p, points)

        self.phase = PHASE_COUNTDOWN
        self.phase_end = self.time + COUNTDOWN_TIME
        self.events.append({
            "kind": "round", "round": self.round_no,
            "map": self.map_index, "name": self.map["name"],
            "blurb": self.map["blurb"],
        })

    def _end_round(self, winner):
        self.phase = PHASE_OVER
        self.phase_end = self.time + ROUND_OVER_TIME
        if winner is not None:
            winner.wins += 1
            self.last_winner = winner.name
            self.events.append({"kind": "win", "name": winner.name,
                                "color": winner.color})
        else:
            self.last_winner = ""
            self.events.append({"kind": "draw"})

    def _living(self):
        return [p for p in self.players.values() if p.alive and not p.waiting]

    def _advance_phase(self):
        enough = len(self.players) >= 2

        if self.phase == PHASE_WAITING:
            if enough:
                self._start_round()

        elif self.phase == PHASE_COUNTDOWN:
            if not enough:
                self.phase = PHASE_WAITING
            elif self.time >= self.phase_end:
                self.phase = PHASE_LIVE
                self.phase_end = self.time + ROUND_TIME_LIMIT

        elif self.phase == PHASE_LIVE:
            alive = self._living()
            if not enough:
                self.phase = PHASE_WAITING
            elif len(alive) <= 1:
                self._end_round(alive[0] if alive else None)
            elif self.time >= self.phase_end:
                self._end_round(None)

        elif self.phase == PHASE_OVER:
            if self.time >= self.phase_end:
                if enough:
                    self._start_round()
                else:
                    self.phase = PHASE_WAITING
                    points = self._spawn_points()
                    for p in self.players.values():
                        self._spawn(p, points)

    # -- simulation -----------------------------------------------------------

    def step(self, dt):
        self.tick += 1
        self.time += dt

        # Obstacles only run during a round, so the countdown always starts
        # from the same configuration on every map.
        if self.phase in (PHASE_LIVE, PHASE_WAITING):
            self.map_time += dt
        self._rebuild_geometry()

        self._advance_phase()

        free_play = self.phase == PHASE_WAITING
        can_act = self.phase == PHASE_LIVE or free_play

        for p in self.players.values():
            p.aim = p.inp["aim"]
            if not p.alive or p.waiting:
                continue

            if can_act:
                self._move_player(p, dt)
                self._weapon(p, dt)
            self._push_out(p)

        self._step_bullets(dt)
        if can_act:
            self._step_loot(dt)

    def _move_player(self, p, dt):
        dx = (1 if p.inp["right"] else 0) - (1 if p.inp["left"] else 0)
        dy = (1 if p.inp["down"] else 0) - (1 if p.inp["up"] else 0)
        if not (dx or dy):
            return
        speed = PLAYER_SPEED * (SWIFT_MULT if p.has(P_SWIFT, self.time) else 1.0)
        inv = speed * dt / math.hypot(dx, dy)
        self._slide(p, dx * inv, 0.0)
        self._slide(p, 0.0, dy * inv)

    def _slide(self, p, dx, dy):
        """Move on one axis, then push back out of any rectangle we entered.

        Resolving the axes separately is what lets you slide along a wall
        instead of sticking to it.
        """
        p.x += dx
        p.y += dy
        for w in self.rects:
            if not circle_hits_rect(p.x, p.y, PLAYER_RADIUS, w):
                continue
            if dx > 0:
                p.x = w[0] - PLAYER_RADIUS
            elif dx < 0:
                p.x = w[0] + w[2] + PLAYER_RADIUS
            if dy > 0:
                p.y = w[1] - PLAYER_RADIUS
            elif dy < 0:
                p.y = w[1] + w[3] + PLAYER_RADIUS

    def _push_out(self, p):
        """Shove a player clear of rotor bars and of blocks that moved into them.

        Runs every tick regardless of input, because on the dynamic maps the
        geometry comes to you.
        """
        for x0, y0, x1, y1, rad in self.bars:
            cx, cy = closest_point_on_segment(p.x, p.y, x0, y0, x1, y1)
            dx, dy = p.x - cx, p.y - cy
            need = rad + PLAYER_RADIUS
            d2 = dx * dx + dy * dy
            if d2 >= need * need:
                continue
            d = math.sqrt(d2)
            if d < 1e-6:
                # Dead centre on the bar: pick a deterministic escape direction.
                dx, dy, d = 1.0, 0.0, 1.0
            p.x = cx + dx / d * need
            p.y = cy + dy / d * need

        for w in self.rects:
            if not circle_hits_rect(p.x, p.y, PLAYER_RADIUS, w):
                continue
            # Escape along whichever face is nearest.
            moves = (
                (0, (w[0] - PLAYER_RADIUS) - p.x),
                (0, (w[0] + w[2] + PLAYER_RADIUS) - p.x),
                (1, (w[1] - PLAYER_RADIUS) - p.y),
                (1, (w[1] + w[3] + PLAYER_RADIUS) - p.y),
            )
            axis, delta = min(moves, key=lambda mv: abs(mv[1]))
            if axis == 0:
                p.x += delta
            else:
                p.y += delta

        p.x = clamp(p.x, PLAYER_RADIUS, ARENA_W - PLAYER_RADIUS)
        p.y = clamp(p.y, PLAYER_RADIUS, ARENA_H - PLAYER_RADIUS)

    # -- weapon ---------------------------------------------------------------

    def _weapon(self, p, dt):
        infinite = p.has(P_AMMO, self.time)
        p.cooldown -= dt

        if p.reload_left > 0.0:
            p.reload_left -= dt
            if p.reload_left <= 0.0:
                p.ammo = MAG_SIZE
                self.events.append({"kind": "reloaded", "pid": p.pid})
            return

        if infinite:
            p.ammo = MAG_SIZE
        elif p.inp["reload"] and p.ammo < MAG_SIZE:
            p.reload_left = RELOAD_TIME
            return

        if not p.inp["shoot"] or p.cooldown > 0.0:
            return
        if p.ammo <= 0:
            p.reload_left = RELOAD_TIME
            return

        cd = FIRE_COOLDOWN * (RAPID_MULT if p.has(P_RAPID, self.time) else 1.0)
        p.cooldown = cd
        if not infinite:
            p.ammo -= 1
        self._fire(p)
        if p.ammo <= 0 and not infinite:
            p.reload_left = RELOAD_TIME

    def _fire(self, p):
        speed = BULLET_SPEED * (
            VELOCITY_MULT if p.has(P_VELOCITY, self.time) else 1.0)
        offsets = ((-SCATTER_ANGLE, 0.0, SCATTER_ANGLE)
                   if p.has(P_SCATTER, self.time) else (0.0,))
        muzzle = PLAYER_RADIUS + BULLET_RADIUS + 2
        for off in offsets:
            ang = p.aim + off + random.uniform(-SPREAD, SPREAD)
            ca, sa = math.cos(ang), math.sin(ang)
            self.bullets.append(Bullet(
                p.x + ca * muzzle, p.y + sa * muzzle,
                ca * speed, sa * speed, p.pid))
        self.events.append({"kind": "shot", "pid": p.pid, "x": p.x, "y": p.y,
                            "aim": round(p.aim, 3)})

    def _step_bullets(self, dt):
        alive = []
        for b in self.bullets:
            b.ttl -= dt
            if b.ttl <= 0.0:
                continue

            x0, y0 = b.x, b.y
            b.x += b.vx * dt
            b.y += b.vy * dt

            if self._bullet_blocked(x0, y0, b.x, b.y):
                self.events.append({"kind": "spark", "x": b.x, "y": b.y})
                continue

            # Swept test against players: at 760 px/s a bullet covers ~13 px per
            # tick and players are 28 px across, so a point test would nearly
            # always do -- but the segment check costs little and the "hot
            # rounds" powerup makes bullets fast enough for it to matter.
            victim = None
            for p in self.players.values():
                if not p.alive or p.waiting or p.pid == b.owner:
                    continue
                if segment_hits_circle(x0, y0, b.x, b.y, p.x, p.y,
                                       PLAYER_RADIUS + BULLET_RADIUS):
                    victim = p
                    break

            if victim is None:
                alive.append(b)
            else:
                self._resolve_hit(victim, b)
        self.bullets = alive

    def _bullet_blocked(self, x0, y0, x1, y1):
        for rect in self.rects:
            if point_in_rect(x1, y1, rect):
                return True
        for bx0, by0, bx1, by1, rad in self.bars:
            reach = rad + BULLET_RADIUS
            if segments_min_dist_sq(x0, y0, x1, y1,
                                    bx0, by0, bx1, by1) <= reach * reach:
                return True
        return False

    def _resolve_hit(self, victim, bullet):
        shooter = self.players.get(bullet.owner)

        if victim.has(P_SHIELD, self.time):
            # A shield is worth exactly one bullet, then it pops.
            victim.powers.pop(P_SHIELD, None)
            self.events.append({"kind": "shieldbreak", "pid": victim.pid,
                                "x": bullet.x, "y": bullet.y})
            return

        self.events.append({"kind": "spark", "x": bullet.x, "y": bullet.y})
        victim.deaths += 1
        if shooter and shooter is not victim:
            shooter.kills += 1
        self.events.append({
            "kind": "kill",
            "killer": shooter.name if shooter else "?",
            "killer_color": shooter.color if shooter else 5,
            "victim": victim.name,
            "victim_color": victim.color,
            "x": victim.x, "y": victim.y,
        })

        if self.phase == PHASE_WAITING:
            # Practice mode: straight back in, no round to lose.
            self._spawn(victim, self._spawn_points())
        else:
            victim.alive = False
            victim.powers.clear()

    # -- loot -----------------------------------------------------------------

    def _step_loot(self, dt):
        if self.phase != PHASE_LIVE:
            return
        self.next_loot -= dt
        if self.next_loot <= 0.0:
            self.next_loot = LOOT_INTERVAL + random.uniform(
                -LOOT_JITTER, LOOT_JITTER)
            if len(self.loot) < LOOT_MAX_ON_FIELD:
                self._drop_loot()

        for box in list(self.loot):
            for p in self.players.values():
                if not p.alive or p.waiting:
                    continue
                if ((p.x - box.x) ** 2 + (p.y - box.y) ** 2
                        <= (PLAYER_RADIUS + BOX_RADIUS) ** 2):
                    self._grant(p, box.kind)
                    self.loot.remove(box)
                    break

    def _drop_loot(self):
        """Place a box near the middle, so fighting over one means exposure."""
        for _ in range(60):
            x = clamp(random.gauss(ARENA_W / 2, LOOT_SPREAD_X),
                      60, ARENA_W - 60)
            y = clamp(random.gauss(ARENA_H / 2, LOOT_SPREAD_Y),
                      60, ARENA_H - 60)
            if self.blocked(x, y, BOX_RADIUS + 10):
                continue
            if any((p.x - x) ** 2 + (p.y - y) ** 2 < 90 * 90
                   for p in self.players.values() if p.alive):
                continue
            kind = random.choice(POWERUPS)
            box = LootBox(self._next_bid, x, y, kind)
            self._next_bid += 1
            self.loot.append(box)
            self.events.append({"kind": "drop", "x": x, "y": y})
            return

    def _grant(self, p, kind):
        p.powers[kind] = self.time + POWERUP_DURATION[kind]
        if kind == P_AMMO:
            p.ammo = MAG_SIZE
            p.reload_left = 0.0
        self.events.append({"kind": "pickup", "pid": p.pid, "power": kind,
                            "name": p.name, "color": p.color,
                            "x": p.x, "y": p.y})

    # -- serialisation --------------------------------------------------------

    def roster(self):
        return [{"id": p.pid, "name": p.name, "color": p.color}
                for p in self.players.values()]

    def snapshot(self):
        """Compact positional state, sent every tick.

        Names live in the roster (they only change on join/leave) and map
        geometry lives in maps.py on both sides, so all that travels here is
        what actually moves.
        """
        now = self.time
        return {
            "t": "s",
            "k": self.tick,
            "ph": self.phase,
            "pt": round(max(0.0, self.phase_end - now), 2),
            "rd": self.round_no,
            "mi": self.map_index,
            "p": [[
                p.pid,
                round(p.x, 1), round(p.y, 1), round(p.aim, 3),
                1 if p.alive else 0,
                p.ammo,
                round(p.reload_left / RELOAD_TIME, 2) if p.reload_left > 0 else 0,
                p.power_bits(now),
                p.wins, p.kills, p.deaths,
                1 if p.waiting else 0,
            ] for p in self.players.values()],
            "b": [[round(b.x, 1), round(b.y, 1), b.owner] for b in self.bullets],
            "l": [[box.bid, round(box.x, 1), round(box.y, 1), box.kind]
                  for box in self.loot],
            # Obstacles are a pure function of the map clock, and clients have
            # the same maps.py, so one float keeps every screen in lockstep.
            "mt": round(self.map_time, 3),
        }
