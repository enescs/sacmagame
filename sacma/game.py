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
    ARENA_H, ARENA_W, BF_BOUNCE, BF_FROST, BF_GOLD, BF_HOMING, BF_PARRIED,
    BORDER, BOUNCE_DAMP, BOUNCE_LIFETIME, BOUNCE_MAX, BOX_RADIUS,
    BULLET_LIFETIME, BULLET_RADIUS, BULLET_SPEED, CHAT_MAX_LEN, COUNTDOWN_TIME,
    FIRE_COOLDOWN, FROST_MAX, FROST_RADIUS, FROST_SLOW, FROST_TIME,
    GOLDEN_MAG, GOLDEN_RELOAD, GOLDEN_SPEED_MULT, HOLD_TIME, HOMING_RANGE,
    HOMING_TURN, LOOT_FIRST_DROP, LOOT_INTERVAL, LOOT_JITTER,
    LOOT_MAX_ON_FIELD, LOOT_SPREAD_X, LOOT_SPREAD_Y, MAG_SIZE, MAX_PLAYERS,
    PHASE_COUNTDOWN, PHASE_LIVE, PHASE_OVER, PHASE_WAITING, PLAYER_RADIUS,
    PLAYER_SPEED, POWERUPS, POWERUP_BIT, POWERUP_DURATION, P_AMMO, P_BOUNCE,
    P_FROST, P_GOLDEN, P_HOLD, P_HOMING, P_INVIS, P_RAPID, P_REFLECT, P_SCATTER,
    P_SHIELD, P_SWIFT, P_VELOCITY, RAPID_MULT, REFLECT_SPEED_MULT, RELOAD_TIME, ROUND_OVER_TIME,
    ROUND_TIME_LIMIT, SCATTER_ANGLE, SPREAD, SWIFT_MULT, VELOCITY_MULT,
    circle_hits_rect, clamp, closest_point_on_segment, point_in_rect,
    segment_hits_circle, segments_min_dist_sq,
)


class Player:
    __slots__ = (
        "pid", "name", "color", "x", "y", "aim", "alive", "waiting",
        "ammo", "reload_left", "reload_total", "cooldown", "powers", "wins",
        "kills", "deaths", "inp",
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
        self.reload_total = RELOAD_TIME
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
    __slots__ = ("x", "y", "vx", "vy", "ttl", "owner", "flags", "bounces")

    def __init__(self, x, y, vx, vy, owner, flags=0):
        self.x = x
        self.y = y
        self.vx = vx
        self.vy = vy
        self.ttl = BOUNCE_LIFETIME if flags & BF_BOUNCE else BULLET_LIFETIME
        self.owner = owner
        self.flags = flags
        self.bounces = BOUNCE_MAX if flags & BF_BOUNCE else 0


class FrostZone:
    """A patch of ice left where a frost round stopped."""

    __slots__ = ("x", "y", "owner", "ttl")

    def __init__(self, x, y, owner):
        self.x = x
        self.y = y
        self.owner = owner
        self.ttl = FROST_TIME


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
        self.frost = []
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
        self.hold_until = 0.0  # arena is frozen for everyone but hold_pid
        self.hold_pid = 0

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
        if self.hold_pid == pid:
            # Never leave the arena frozen for a player who is gone.
            self.hold_until = 0.0
            self.hold_pid = 0
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

    def chat(self, pid, text):
        """Queue a player's message for the next broadcast.

        Chat rides the normal event stream, so it reaches everyone in tick
        order alongside kills and pickups and needs no extra plumbing.
        """
        p = self.players.get(pid)
        if not p:
            return
        text = " ".join(str(text).split())[:CHAT_MAX_LEN]
        if not text:
            return
        self.events.append({"kind": "chat", "pid": pid, "name": p.name,
                            "color": p.color, "text": text})

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
        self.frost.clear()
        self.loot.clear()
        self.hold_until = 0.0
        self.hold_pid = 0
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

    def jump_map(self, delta):
        """Debug hook: hop to the next/previous map and restart on it.

        Handy for eyeballing layouts without waiting out a round. Solo this
        just reloads the arena; mid-match it restarts the round from the
        countdown so nobody is dropped into a wall.
        """
        self.map_index = (self.map_index + delta) % len(MAPS)
        self.map_time = 0.0
        self._rebuild_geometry()

        self.bullets.clear()
        self.frost.clear()
        self.loot.clear()
        self.hold_until = 0.0
        self.hold_pid = 0
        self.next_loot = LOOT_FIRST_DROP

        points = self._spawn_points()
        for p in self.players.values():
            self._spawn(p, points)

        if self.phase != PHASE_WAITING:
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

        held = self.hold_until > self.time
        if held:
            # A time stop takes the round clock with it, so nobody loses the
            # round to a timer that ran while they could not move.
            self.phase_end += dt
        elif self.hold_pid:
            self.events.append({"kind": "unhold"})
            self.hold_pid = 0

        # Obstacles only run during a round, so the countdown always starts
        # from the same configuration on every map. A time stop halts them too:
        # the whole arena freezes, not just the players in it.
        if self.phase in (PHASE_LIVE, PHASE_WAITING) and not held:
            self.map_time += dt
        self._rebuild_geometry()

        self._advance_phase()

        free_play = self.phase == PHASE_WAITING
        can_act = self.phase == PHASE_LIVE or free_play

        for p in self.players.values():
            frozen = held and p.pid != self.hold_pid
            if not frozen:
                # Frozen players cannot even turn on the spot.
                p.aim = p.inp["aim"]
            if not p.alive or p.waiting:
                continue

            if can_act and not frozen:
                self._move_player(p, dt)
                self._weapon(p, dt)
            self._push_out(p)

        self._step_bullets(dt, held)
        if not held:
            self._step_frost(dt)
        if can_act:
            self._step_loot(dt)

    def _move_player(self, p, dt):
        dx = (1 if p.inp["right"] else 0) - (1 if p.inp["left"] else 0)
        dy = (1 if p.inp["down"] else 0) - (1 if p.inp["up"] else 0)
        if not (dx or dy):
            return
        speed = PLAYER_SPEED * (SWIFT_MULT if p.has(P_SWIFT, self.time) else 1.0)
        if self._in_frost(p):
            speed *= FROST_SLOW
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

    # -- frost ----------------------------------------------------------------

    def _step_frost(self, dt):
        for z in self.frost:
            z.ttl -= dt
        self.frost = [z for z in self.frost if z.ttl > 0.0]

    def _in_frost(self, p):
        """True if `p` is standing in someone else's ice."""
        reach = FROST_RADIUS + PLAYER_RADIUS
        for z in self.frost:
            if z.owner == p.pid:
                continue
            if (p.x - z.x) ** 2 + (p.y - z.y) ** 2 <= reach * reach:
                return True
        return False

    def _drop_frost(self, b, x=None, y=None):
        """Leave a patch where a frost round came to rest.

        Wall impacts pass the last position outside the wall, so the patch
        lands on the floor you can actually walk on rather than half-buried in
        the geometry.
        """
        if not b.flags & BF_FROST:
            return
        x = b.x if x is None else x
        y = b.y if y is None else y
        if len(self.frost) >= FROST_MAX:
            del self.frost[0]
        self.frost.append(FrostZone(x, y, b.owner))
        self.events.append({"kind": "frost", "x": x, "y": y})

    # -- weapon ---------------------------------------------------------------

    def _mag_size(self, p):
        return GOLDEN_MAG if p.has(P_GOLDEN, self.time) else MAG_SIZE

    def _reload_time(self, p):
        return GOLDEN_RELOAD if p.has(P_GOLDEN, self.time) else RELOAD_TIME

    def _begin_reload(self, p):
        p.reload_total = self._reload_time(p)
        p.reload_left = p.reload_total

    def _weapon(self, p, dt):
        infinite = p.has(P_AMMO, self.time)
        mag = self._mag_size(p)
        p.cooldown -= dt

        if p.reload_left > 0.0:
            p.reload_left -= dt
            if p.reload_left <= 0.0:
                p.ammo = mag
                self.events.append({"kind": "reloaded", "pid": p.pid})
            return

        # Picking up or losing the golden gun swaps the magazine underneath you.
        if p.ammo > mag:
            p.ammo = mag

        if infinite:
            p.ammo = mag
        elif p.inp["reload"] and p.ammo < mag:
            self._begin_reload(p)
            return

        if not p.inp["shoot"] or p.cooldown > 0.0:
            return
        if p.ammo <= 0:
            self._begin_reload(p)
            return

        cd = FIRE_COOLDOWN * (RAPID_MULT if p.has(P_RAPID, self.time) else 1.0)
        p.cooldown = cd
        if not infinite:
            p.ammo -= 1
        self._fire(p)
        if p.ammo <= 0 and not infinite:
            self._begin_reload(p)

    def _fire(self, p):
        golden = p.has(P_GOLDEN, self.time)
        homing = p.has(P_HOMING, self.time)

        speed = BULLET_SPEED
        if golden:
            speed *= GOLDEN_SPEED_MULT
        elif p.has(P_VELOCITY, self.time):
            speed *= VELOCITY_MULT

        flags = ((BF_GOLD if golden else 0) | (BF_HOMING if homing else 0)
                 | (BF_BOUNCE if p.has(P_BOUNCE, self.time) else 0)
                 | (BF_FROST if p.has(P_FROST, self.time) else 0))
        # The golden gun stays a single precise round even under scatter.
        offsets = ((-SCATTER_ANGLE, 0.0, SCATTER_ANGLE)
                   if p.has(P_SCATTER, self.time) and not golden else (0.0,))
        spread = 0.0 if golden else SPREAD

        muzzle = PLAYER_RADIUS + BULLET_RADIUS + 2
        for off in offsets:
            ang = p.aim + off + random.uniform(-spread, spread)
            ca, sa = math.cos(ang), math.sin(ang)
            self.bullets.append(Bullet(
                p.x + ca * muzzle, p.y + sa * muzzle,
                ca * speed, sa * speed, p.pid, flags))

        # A muzzle flash would give away an invisible player's exact position;
        # their bullets are visible, which is tell enough.
        if not p.has(P_INVIS, self.time):
            self.events.append({"kind": "shot", "pid": p.pid, "x": p.x,
                                "y": p.y, "aim": round(p.aim, 3)})

    def _home(self, b, dt):
        """Curve a homing round towards the nearest valid target."""
        best, best_d2 = None, HOMING_RANGE * HOMING_RANGE
        for p in self.players.values():
            if not p.alive or p.waiting or p.pid == b.owner:
                continue
            d2 = (p.x - b.x) ** 2 + (p.y - b.y) ** 2
            if d2 < best_d2:
                best, best_d2 = p, d2
        if best is None:
            return

        speed = math.hypot(b.vx, b.vy)
        if speed < 1e-6:
            return
        cur = math.atan2(b.vy, b.vx)
        want = math.atan2(best.y - b.y, best.x - b.x)
        # Shortest signed angle, then clamp to the per-tick turn budget.
        diff = (want - cur + math.pi) % math.tau - math.pi
        limit = HOMING_TURN * dt
        cur += clamp(diff, -limit, limit)
        b.vx, b.vy = math.cos(cur) * speed, math.sin(cur) * speed

    def _step_bullets(self, dt, held=False):
        alive = []
        for b in self.bullets:
            # During a time stop everything already in the air hangs there --
            # its fuse stops too -- while the holder's own shots fly as normal,
            # which is the whole point of stopping time.
            if held and b.owner != self.hold_pid:
                alive.append(b)
                continue

            b.ttl -= dt
            if b.ttl <= 0.0:
                self._drop_frost(b)
                continue

            if b.flags & BF_HOMING:
                self._home(b, dt)

            x0, y0 = b.x, b.y
            b.x += b.vx * dt
            b.y += b.vy * dt

            if self._bullet_blocked(x0, y0, b.x, b.y):
                if not self._bounce(b, x0, y0):
                    self.events.append({"kind": "spark", "x": b.x, "y": b.y})
                    self._drop_frost(b, x0, y0)
                    continue
                self.events.append({"kind": "ricochet", "x": b.x, "y": b.y})

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

            if victim is None or self._resolve_hit(victim, b):
                alive.append(b)
            else:
                self._drop_frost(b)
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

    def _bounce(self, b, x0, y0):
        """Reflect a ricochet round off whatever it just hit.

        Returns False when the round has no bounces left (or never had any), in
        which case the caller kills it as usual. Walls flip a single axis --
        recovered by asking which axis of this tick's step actually carried the
        round inside -- while rotor bars use the true surface normal, since they
        sit at arbitrary angles.
        """
        if not b.flags & BF_BOUNCE or b.bounces <= 0:
            return False

        vx, vy = b.vx, b.vy
        hit = False

        for bx0, by0, bx1, by1, rad in self.bars:
            cx, cy = closest_point_on_segment(b.x, b.y, bx0, by0, bx1, by1)
            nx, ny = b.x - cx, b.y - cy
            n = math.hypot(nx, ny)
            reach = rad + BULLET_RADIUS
            if n > reach:
                continue
            if n < 1e-6:
                # Dead centre on the bar: just turn it around.
                vx, vy = -vx, -vy
            else:
                nx, ny = nx / n, ny / n
                dot = 2.0 * (vx * nx + vy * ny)
                vx, vy = vx - dot * nx, vy - dot * ny
            hit = True
            break

        if not hit:
            flip_x = flip_y = False
            for rect in self.rects:
                if not point_in_rect(b.x, b.y, rect):
                    continue
                inx = not point_in_rect(x0, b.y, rect)
                iny = not point_in_rect(b.x, y0, rect)
                if not inx and not iny:
                    # Clipped a corner: neither axis alone was outside.
                    inx = iny = True
                flip_x = flip_x or inx
                flip_y = flip_y or iny
                hit = True
            if not hit:
                return False
            if flip_x:
                vx = -vx
            if flip_y:
                vy = -vy

        b.bounces -= 1
        b.vx, b.vy = vx * BOUNCE_DAMP, vy * BOUNCE_DAMP

        # Restart from where the step began and nudge clear along the new
        # heading, so the round never spawns inside the surface it just hit.
        speed = math.hypot(b.vx, b.vy)
        if speed < 1e-6:
            return False
        step = BULLET_RADIUS + 2
        b.x = x0 + b.vx / speed * step
        b.y = y0 + b.vy / speed * step
        if self._bullet_blocked(b.x, b.y, b.x, b.y):
            # Wedged in a corner or a mover closed on it; let it die.
            return False
        return True

    def _resolve_hit(self, victim, bullet):
        """Apply a bullet that reached a player. Returns True if it lives on."""
        shooter = self.players.get(bullet.owner)

        if victim.has(P_REFLECT, self.time):
            # Parry: send it back the way it came and hand it to the parrier,
            # so a well-timed reflect is a kill rather than just a save.
            bullet.vx *= -REFLECT_SPEED_MULT
            bullet.vy *= -REFLECT_SPEED_MULT
            bullet.owner = victim.pid
            bullet.flags |= BF_PARRIED
            bullet.ttl = max(bullet.ttl, BULLET_LIFETIME * 0.6)
            # Clear the player it just bounced off, or it re-hits immediately.
            step = PLAYER_RADIUS + BULLET_RADIUS + 3
            speed = math.hypot(bullet.vx, bullet.vy) or 1.0
            bullet.x = victim.x + bullet.vx / speed * step
            bullet.y = victim.y + bullet.vy / speed * step
            self.events.append({"kind": "parry", "pid": victim.pid,
                                "x": victim.x, "y": victim.y})
            return True

        # The golden gun punches straight through a shield.
        if victim.has(P_SHIELD, self.time) and not bullet.flags & BF_GOLD:
            # A shield is worth exactly one bullet, then it pops.
            victim.powers.pop(P_SHIELD, None)
            self.events.append({"kind": "shieldbreak", "pid": victim.pid,
                                "x": bullet.x, "y": bullet.y})
            return False

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
        return False

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
        elif kind == P_HOLD:
            # Fires the instant it is picked up; there is nothing to hold on to.
            self.hold_until = self.time + HOLD_TIME
            self.hold_pid = p.pid
            self.events.append({"kind": "hold", "pid": p.pid, "name": p.name,
                                "color": p.color, "x": p.x, "y": p.y})
        elif kind == P_GOLDEN:
            # Hand over a loaded golden gun rather than whatever was left.
            p.ammo = GOLDEN_MAG
            p.reload_left = 0.0
        self.events.append({"kind": "pickup", "pid": p.pid, "power": kind,
                            "name": p.name, "color": p.color,
                            "x": p.x, "y": p.y})

    # -- serialisation --------------------------------------------------------

    def roster(self):
        return [{"id": p.pid, "name": p.name, "color": p.color}
                for p in self.players.values()]

    def anyone_invisible(self):
        """True if snapshots have to be built per viewer this tick."""
        now = self.time
        return any(p.alive and not p.waiting and p.has(P_INVIS, now)
                   for p in self.players.values())

    def _player_row(self, p, now, viewer):
        hidden = (viewer != p.pid and p.alive and not p.waiting
                  and p.has(P_INVIS, now))
        return [
            p.pid,
            # An invisible player's coordinates never leave the server, so no
            # amount of client tampering can reveal them.
            0.0 if hidden else round(p.x, 1),
            0.0 if hidden else round(p.y, 1),
            0.0 if hidden else round(p.aim, 3),
            1 if p.alive else 0,
            p.ammo,
            (round(p.reload_left / p.reload_total, 2)
             if p.reload_left > 0 and p.reload_total else 0),
            p.power_bits(now),
            p.wins, p.kills, p.deaths,
            1 if p.waiting else 0,
            1 if hidden else 0,
        ]

    def snapshot(self, viewer=None):
        """Compact positional state, sent every tick.

        Names live in the roster (they only change on join/leave) and map
        geometry lives in maps.py on both sides, so all that travels here is
        what actually moves.

        `viewer` is the player id this copy is destined for; it only matters
        while somebody is invisible, and the server skips the per-client build
        entirely when nobody is.
        """
        now = self.time
        return {
            "t": "s",
            "k": self.tick,
            "ph": self.phase,
            "pt": round(max(0.0, self.phase_end - now), 2),
            "rd": self.round_no,
            "mi": self.map_index,
            "p": [self._player_row(p, now, viewer)
                  for p in self.players.values()],
            "b": [[round(b.x, 1), round(b.y, 1), b.owner, b.flags]
                  for b in self.bullets],
            "l": [[box.bid, round(box.x, 1), round(box.y, 1), box.kind]
                  for box in self.loot],
            # Ice patches: position, who laid it (they walk through freely) and
            # how much life is left, which the client fades out.
            "f": [[round(z.x, 1), round(z.y, 1), z.owner,
                   round(z.ttl / FROST_TIME, 2)] for z in self.frost],
            # Time stop: seconds left, and who is still allowed to move.
            "hd": round(max(0.0, self.hold_until - now), 2),
            "hp": self.hold_pid,
            # Obstacles are a pure function of the map clock, and clients have
            # the same maps.py, so one float keeps every screen in lockstep.
            "mt": round(self.map_time, 3),
        }
