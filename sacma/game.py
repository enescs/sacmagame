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

Two things bend that shape. Roughly one free-for-all round in seven is a boss
round: somebody is picked, given a health bar and better numbers, and everyone
else stops shooting each other until it is dealt with. And the server can be
in capture-the-flag mode instead, where the round runs on captures and a
respawn timer rather than on who is still breathing. Players pick the mode
from inside the game; a change lands at the next round.

Two powerups also reach outside a single player: a time stop freezes the whole
arena except whoever picked it up, and frost leaves ice on the floor that
slows everyone but the side that laid it.
"""

import math
import random

from .maps import MAPS, mover_rect, rotor_segment
from .shared import (
    ARENA_H, ARENA_W, BASE_RADIUS, BF_BOUNCE, BF_FROST, BF_GHOST, BF_GOLD,
    BF_HOMING, BF_PARRIED, BORDER, BOSS_BULLET_MULT, BOSS_CHANCE,
    BOSS_FIRE_MULT, BOSS_HP_BASE, BOSS_HP_MAX, BOSS_HP_PER_FOE, BOSS_MAG,
    BOSS_MIN_PLAYERS, BOSS_RADIUS, BOSS_RELOAD, BOSS_SPEED_MULT, BOUNCE_DAMP,
    BOUNCE_LIFETIME, BOUNCE_MAX, BOX_RADIUS, BULLET_LIFETIME, BULLET_RADIUS,
    BULLET_SPEED, CHAT_MAX_LEN, COUNTDOWN_TIME, CTF_CAPTURES_TO_WIN,
    CTF_RESPAWN_TIME, CTF_ROUND_TIME, CTF_TEAM_SIZE, FIRE_COOLDOWN,
    FLAG_CARRIED, FLAG_CARRY_MULT, FLAG_DROPPED, FLAG_HOME, FLAG_RADIUS,
    FLAG_RETURN_TIME, FROST_MAX, FROST_RADIUS, FROST_SLOW, FROST_TIME,
    GHOST_SPEED_MULT, GOLDEN_MAG, GOLDEN_RELOAD, GOLDEN_SPEED_MULT, HOLD_TIME,
    HOMING_RANGE, HOMING_TURN, LOOT_FIRST_DROP, LOOT_INTERVAL, LOOT_JITTER,
    LOOT_MAX_ON_FIELD, LOOT_SPREAD_X, LOOT_SPREAD_Y, MAG_SIZE, MAX_PLAYERS,
    MODE_BOSS, MODE_CTF, MODE_FFA, MODE_LABEL, PHASE_COUNTDOWN, PHASE_LIVE,
    PHASE_OVER, PHASE_WAITING, PLAYER_RADIUS, PLAYER_SPEED, POWERUPS,
    POWERUP_BIT, POWERUP_DURATION, P_AMMO, P_BOUNCE, P_FROST, P_GHOST,
    P_GOLDEN, P_HOLD, P_HOMING, P_INVIS, P_QUAKE, P_RAPID, P_REFLECT,
    P_SCATTER, P_SHIELD, P_SWIFT, P_VELOCITY, RAPID_MULT, REFLECT_SPEED_MULT,
    RELOAD_TIME, ROUND_OVER_TIME, ROUND_TIME_LIMIT, SCATTER_ANGLE, SPREAD,
    SWIFT_MULT, TEAM_NAMES, VELOCITY_MULT, circle_hits_rect, clamp,
    closest_point_on_segment, point_in_rect, segment_hits_circle,
    segments_min_dist_sq,
)


class Player:
    __slots__ = (
        "pid", "name", "color", "x", "y", "aim", "alive", "waiting",
        "ammo", "reload_left", "reload_total", "cooldown", "powers", "wins",
        "kills", "deaths", "inp", "team", "boss", "hp", "hp_max", "respawn_at",
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

        self.team = -1        # capture the flag only; -1 means no team
        self.boss = False
        # Everybody but a boss dies to one bullet, so this is 1 almost always.
        self.hp = 1
        self.hp_max = 1
        self.respawn_at = 0.0

    @property
    def radius(self):
        return BOSS_RADIUS if self.boss else PLAYER_RADIUS

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


class Flag:
    """One team's flag: sitting on its stand, on somebody's back, or on the floor."""

    __slots__ = ("team", "home_x", "home_y", "x", "y", "carrier", "return_at",
                 "at_home")

    def __init__(self, team, home_x, home_y):
        self.team = team
        self.home_x = float(home_x)
        self.home_y = float(home_y)
        self.x = self.home_x
        self.y = self.home_y
        self.carrier = None    # pid of whoever is running it
        self.return_at = 0.0   # when a dropped flag goes home by itself
        # Tracked rather than inferred from the coordinates: a carrier shot
        # dead standing on their own stand drops the flag exactly on it, and
        # that is a flag on the floor, not a flag that is home.
        self.at_home = True

    @property
    def state(self):
        if self.carrier is not None:
            return FLAG_CARRIED
        return FLAG_HOME if self.at_home else FLAG_DROPPED

    def send_home(self):
        self.carrier = None
        self.x, self.y = self.home_x, self.home_y
        self.return_at = 0.0
        self.at_home = True


class Game:
    def __init__(self, mode=MODE_FFA):
        self.mode = mode
        self.next_mode = None   # queued by a player; lands at the next round
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

        # Boss rounds (free-for-all) and flags (capture the flag) are both
        # per-round state; only one of them is ever in use on a given server.
        self.boss_pid = None
        self.last_boss = None  # so the same player is not picked twice running
        self.flags = []
        self.team_score = [0, 0]
        self._nocap_at = {}
        # Set when team assignments change under the server's feet, so it knows
        # to push a fresh roster.
        self.roster_dirty = False

        self._next_pid = 1
        self._next_bid = 1
        self.rects = []
        self.bars = []
        self._rebuild_geometry()

    @property
    def ctf(self):
        return self.mode == MODE_CTF

    @staticmethod
    def mode_cap(mode):
        return CTF_TEAM_SIZE * 2 if mode == MODE_CTF else MAX_PLAYERS

    @property
    def max_players(self):
        # If a switch to capture the flag is already queued, hold the roster at
        # four now -- filling the seventh seat and then having to throw three
        # people out when the round turns over would be worse.
        cap = self.mode_cap(self.mode)
        if self.next_mode is not None:
            cap = min(cap, self.mode_cap(self.next_mode))
        return cap

    # -- game modes -----------------------------------------------------------

    def request_mode(self, mode, by_name=""):
        """Queue a mode change for the next round. Returns a status string.

        Anyone can do this, and it is announced with their name on it, which is
        the same social contract as the map keys: fine among people who can see
        each other, and the host can still fix the mode with --no-mode-vote.
        """
        if mode not in MODE_LABEL:
            return "unknown mode"
        target = self.mode if self.next_mode is None else self.next_mode
        if mode == target:
            return "already"
        if mode == MODE_CTF and len(self.players) > self.mode_cap(MODE_CTF):
            self.events.append({
                "kind": "modefail", "mode": mode, "name": by_name,
                "reason": f"capture the flag is 2v2 -- "
                          f"{len(self.players)} players are connected",
            })
            return "too many players"

        if mode == self.mode:
            # Cancelling a queued change rather than asking for a new one.
            self.next_mode = None
        else:
            self.next_mode = mode
        self.events.append({"kind": "modenext", "mode": mode, "name": by_name})
        return "ok"

    def _apply_mode(self):
        """Swap in a queued mode. Called from _start_round, nowhere else."""
        if self.next_mode is None or self.next_mode == self.mode:
            self.next_mode = None
            return
        self.mode = self.next_mode
        self.next_mode = None
        self.flags = []
        self.team_score = [0, 0]
        if not self.ctf:
            # Leaving a team mode: everybody goes back to shooting everybody.
            for p in self.players.values():
                p.team = -1
        else:
            for p in self.players.values():
                p.team = -1
            for p in self.players.values():
                p.team = self._pick_team()
        self.roster_dirty = True
        self.events.append({"kind": "mode", "mode": self.mode})

    # -- roster ---------------------------------------------------------------

    def free_slots(self):
        return self.max_players - len(self.players)

    def _pick_team(self):
        """Put the newcomer on the thinner side, ties going to blue."""
        counts = [0, 0]
        for p in self.players.values():
            if p.team in (0, 1):
                counts[p.team] += 1
        return 0 if counts[0] <= counts[1] else 1

    def add_player(self, name):
        used = {p.color for p in self.players.values()}
        color = next((i for i in range(MAX_PLAYERS) if i not in used), 0)
        pid = self._next_pid
        self._next_pid += 1
        p = Player(pid, name, color)
        if self.ctf:
            p.team = self._pick_team()
        self.players[pid] = p
        self.events.append({"kind": "join", "name": name, "color": color,
                            "team": p.team})

        if self.ctf and self.phase == PHASE_LIVE:
            # No spectating in capture the flag -- there are respawns, so a
            # latecomer just walks in on the next one.
            p.waiting = False
            p.alive = False
            p.respawn_at = self.time + CTF_RESPAWN_TIME
        elif self.phase in (PHASE_WAITING, PHASE_OVER):
            # If we were idle they can start moving immediately, and may well
            # kick off the first round.
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
        # Do not let a flag leave with them.
        for flag in self.flags:
            if flag.carrier == pid:
                flag.send_home()
                self.events.append({"kind": "flagreturn", "team": flag.team})
        if self.boss_pid == pid:
            # The boss quit; the round has nothing left to be about.
            self.boss_pid = None
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

    def _place(self, player, x, y):
        """Drop a player at (x, y), nudged clear if something is already there.

        Only capture the flag needs the search: everyone on a team starts at
        the same stand, and on Gridlock the tidy spot next to it is a pillar.
        """
        if not self.blocked(x, y, player.radius + 1):
            player.x, player.y = float(x), float(y)
            return
        for step in range(1, 9):
            r = 26.0 * step
            for i in range(8):
                a = i * math.tau / 8 + step * 0.4
                nx = clamp(x + math.cos(a) * r, player.radius, ARENA_W - player.radius)
                ny = clamp(y + math.sin(a) * r, player.radius, ARENA_H - player.radius)
                if not self.blocked(nx, ny, player.radius + 1):
                    player.x, player.y = nx, ny
                    return
        player.x, player.y = float(x), float(y)

    def _reset_player(self, player):
        player.alive = True
        player.waiting = False
        player.respawn_at = 0.0
        player.hp = player.hp_max
        player.reload_left = 0.0
        player.cooldown = 0.0
        # Powers go first: the magazine you get depends on them, and coming
        # back with last life's golden gun would hand you a one-round rifle.
        player.powers.clear()
        player.ammo = self._mag_size(player)

    def _spawn(self, player, points):
        sx, sy = points.pop() if points else (ARENA_W / 2, ARENA_H / 2)
        self._place(player, sx, sy)
        self._reset_player(player)

    def _spawn_at_base(self, player):
        """Capture-the-flag spawn: on your own stand, facing the fight."""
        bases = self.map["bases"]
        bx, by = bases[player.team % len(bases)]
        # Fan teammates out around the stand rather than stacking them on it.
        a = random.uniform(0.0, math.tau)
        self._place(player, bx + math.cos(a) * 46, by + math.sin(a) * 46)
        self._reset_player(player)
        player.aim = 0.0 if player.team == 0 else math.pi

    def _reset_roles(self):
        for p in self.players.values():
            p.boss = False
            p.hp_max = 1
            p.hp = 1
        self.boss_pid = None

    def _maybe_pick_boss(self):
        """Promote somebody, if this round is going to have a boss at all.

        In free-for-all that is one round in seven on average, and only once
        there is a real mob -- a boss with a single hunter is just a duel with
        extra health. Boss rush skips the roll and takes anyone it can get,
        because a round without a boss would be the mode failing to happen.
        Either way, never the same player twice running.
        """
        floor = 2 if self.mode == MODE_BOSS else BOSS_MIN_PLAYERS
        if len(self.players) < floor:
            return
        if self.mode != MODE_BOSS and random.random() >= BOSS_CHANCE:
            return

        pool = [p for p in self.players.values() if p.pid != self.last_boss]
        if not pool:
            pool = list(self.players.values())
        boss = random.choice(pool)

        boss.boss = True
        foes = len(self.players) - 1
        boss.hp_max = min(BOSS_HP_MAX, BOSS_HP_BASE + BOSS_HP_PER_FOE * foes)
        boss.hp = boss.hp_max
        self.boss_pid = boss.pid
        self.last_boss = boss.pid

    def _reset_flags(self):
        bases = self.map["bases"]
        self.flags = [Flag(t, bases[t][0], bases[t][1]) for t in range(2)]
        self.team_score = [0, 0]

    def _start_round(self):
        # A queued mode change lands here, before anything is placed.
        self._apply_mode()

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

        # Roles are settled before anyone is placed, because a boss is wider
        # than a normal player and has to be spawned with that in mind.
        self._reset_roles()
        if self.ctf:
            self._reset_flags()
            for p in self.players.values():
                if p.team not in (0, 1):
                    p.team = self._pick_team()
                self._spawn_at_base(p)
        else:
            self._maybe_pick_boss()
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
        if self.boss_pid is not None:
            boss = self.players[self.boss_pid]
            self.events.append({"kind": "boss", "pid": boss.pid,
                                "name": boss.name, "color": boss.color,
                                "hp": boss.hp_max})

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

        if self.ctf:
            self._reset_flags()
            for p in self.players.values():
                self._spawn_at_base(p)
        else:
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

    def _finish(self, result):
        self.phase = PHASE_OVER
        self.phase_end = self.time + ROUND_OVER_TIME
        self.events.append(result)

    def _end_round(self, winner):
        if winner is not None:
            winner.wins += 1
            self.last_winner = winner.name
            self._finish({"kind": "win", "name": winner.name,
                          "color": winner.color})
        else:
            self.last_winner = ""
            self._finish({"kind": "draw"})

    def _end_boss_round(self, boss, boss_won):
        """Settle a boss round. The mob wins together or not at all."""
        hunters = [p for p in self.players.values()
                   if not p.boss and not p.waiting]
        if boss_won:
            boss.wins += 1
            self.last_winner = boss.name
        else:
            # Everyone who was in the round shares the win, including whoever
            # died drawing fire -- that is usually why the boss went down.
            for p in hunters:
                p.wins += 1
            self.last_winner = "the hunters"
        self._finish({"kind": "bossend", "boss_won": boss_won,
                      "name": boss.name, "color": boss.color,
                      "hunters": len(hunters)})

    def _end_ctf_round(self):
        a, b = self.team_score
        if a == b:
            self.last_winner = ""
        else:
            team = 0 if a > b else 1
            self.last_winner = TEAM_NAMES[team]
            for p in self.players.values():
                if p.team == team:
                    p.wins += 1
        self._finish({"kind": "teamend", "score": list(self.team_score)})

    def _living(self):
        return [p for p in self.players.values() if p.alive and not p.waiting]

    def _in_round(self):
        """Players taking part in this round, alive or not."""
        return [p for p in self.players.values() if not p.waiting]

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
                self.phase_end = self.time + (
                    CTF_ROUND_TIME if self.ctf else ROUND_TIME_LIMIT)

        elif self.phase == PHASE_LIVE:
            if not enough:
                self.phase = PHASE_WAITING
            elif self.ctf:
                if (max(self.team_score) >= CTF_CAPTURES_TO_WIN
                        or self.time >= self.phase_end):
                    self._end_ctf_round()
            elif self.boss_pid is not None:
                self._advance_boss_round()
            else:
                alive = self._living()
                if len(alive) <= 1:
                    self._end_round(alive[0] if alive else None)
                elif self.time >= self.phase_end:
                    self._end_round(None)

        elif self.phase == PHASE_OVER:
            if self.time >= self.phase_end:
                if enough:
                    self._start_round()
                else:
                    self.phase = PHASE_WAITING
                    self._reset_roles()
                    points = self._spawn_points()
                    for p in self.players.values():
                        self._spawn(p, points)

    def _advance_boss_round(self):
        """A boss round ends when the boss falls or the last hunter does.

        Running the clock out counts as a loss for the boss: it is the stronger
        side by a distance, so sitting in a corner should not be rewarded.
        """
        boss = self.players.get(self.boss_pid)
        if boss is None or boss.waiting:
            self.boss_pid = None
            return
        hunters_alive = [p for p in self._living() if not p.boss]
        if not boss.alive:
            self._end_boss_round(boss, boss_won=False)
        elif not hunters_alive:
            self._end_boss_round(boss, boss_won=True)
        elif self.time >= self.phase_end:
            self._end_boss_round(boss, boss_won=False)

    # -- simulation -----------------------------------------------------------

    def step(self, dt):
        self.tick += 1
        self.time += dt

        held = self.hold_until > self.time
        if held:
            # A time stop takes the round clock with it, so nobody loses the
            # round to a timer that ran while they could not move. Every other
            # deadline that decides a round goes with it for the same reason:
            # a frozen player should not respawn, and a dropped flag should not
            # walk itself home, on time nobody could play through.
            self.phase_end += dt
            for p in self.players.values():
                if p.respawn_at > 0.0 and p.pid != self.hold_pid:
                    p.respawn_at += dt
            for flag in self.flags:
                if flag.return_at > 0.0:
                    flag.return_at += dt
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

        if self.ctf and self.phase == PHASE_LIVE:
            self._step_respawns()

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
        if self.ctf and self.phase == PHASE_LIVE:
            self._step_flags(dt)

    def _step_respawns(self):
        for p in self.players.values():
            if p.alive or p.waiting or p.respawn_at <= 0.0:
                continue
            if self.time >= p.respawn_at:
                self._spawn_at_base(p)
                self.events.append({"kind": "respawn", "pid": p.pid,
                                    "x": p.x, "y": p.y, "team": p.team})

    def _move_player(self, p, dt):
        dx = (1 if p.inp["right"] else 0) - (1 if p.inp["left"] else 0)
        dy = (1 if p.inp["down"] else 0) - (1 if p.inp["up"] else 0)
        if not (dx or dy):
            return
        # Everything that touches move speed stacks multiplicatively, so a
        # sprinting boss slowed by ice is still faster than a walking one.
        speed = PLAYER_SPEED * (SWIFT_MULT if p.has(P_SWIFT, self.time) else 1.0)
        if self._in_frost(p):
            speed *= FROST_SLOW
        if p.boss:
            speed *= BOSS_SPEED_MULT
        if self.carried_by(p) is not None:
            speed *= FLAG_CARRY_MULT
        inv = speed * dt / math.hypot(dx, dy)
        self._slide(p, dx * inv, 0.0)
        self._slide(p, 0.0, dy * inv)

    def _slide(self, p, dx, dy):
        """Move on one axis, then push back out of any rectangle we entered.

        Resolving the axes separately is what lets you slide along a wall
        instead of sticking to it.
        """
        rad = p.radius
        p.x += dx
        p.y += dy
        for w in self.rects:
            if not circle_hits_rect(p.x, p.y, rad, w):
                continue
            if dx > 0:
                p.x = w[0] - rad
            elif dx < 0:
                p.x = w[0] + w[2] + rad
            if dy > 0:
                p.y = w[1] - rad
            elif dy < 0:
                p.y = w[1] + w[3] + rad

    def _push_out(self, p):
        """Shove a player clear of rotor bars and of blocks that moved into them.

        Runs every tick regardless of input, because on the dynamic maps the
        geometry comes to you.
        """
        pr = p.radius
        for x0, y0, x1, y1, rad in self.bars:
            cx, cy = closest_point_on_segment(p.x, p.y, x0, y0, x1, y1)
            dx, dy = p.x - cx, p.y - cy
            need = rad + pr
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
            if not circle_hits_rect(p.x, p.y, pr, w):
                continue
            # Escape along whichever face is nearest.
            moves = (
                (0, (w[0] - pr) - p.x),
                (0, (w[0] + w[2] + pr) - p.x),
                (1, (w[1] - pr) - p.y),
                (1, (w[1] + w[3] + pr) - p.y),
            )
            axis, delta = min(moves, key=lambda mv: abs(mv[1]))
            if axis == 0:
                p.x += delta
            else:
                p.y += delta

        p.x = clamp(p.x, pr, ARENA_W - pr)
        p.y = clamp(p.y, pr, ARENA_H - pr)

    # -- frost ----------------------------------------------------------------

    def _step_frost(self, dt):
        for z in self.frost:
            z.ttl -= dt
        self.frost = [z for z in self.frost if z.ttl > 0.0]

    def _in_frost(self, p):
        """True if `p` is standing in ice laid by the other side.

        Your own patches never slow you, and neither do a teammate's: ice
        follows the same sides as bullets do, so laying a field across a
        corridor in capture the flag does not strand the player you laid it
        for. In free-for-all that is everyone but you, exactly as before.
        """
        reach = FROST_RADIUS + p.radius
        for z in self.frost:
            if not self._hostile(z.owner, p):
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
        if p.has(P_GOLDEN, self.time):
            return GOLDEN_MAG
        return BOSS_MAG if p.boss else MAG_SIZE

    def _reload_time(self, p):
        if p.has(P_GOLDEN, self.time):
            return GOLDEN_RELOAD
        return BOSS_RELOAD if p.boss else RELOAD_TIME

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
        if p.boss:
            cd *= BOSS_FIRE_MULT
        p.cooldown = cd
        if not infinite:
            p.ammo -= 1
        self._fire(p)
        if p.ammo <= 0 and not infinite:
            self._begin_reload(p)

    def _fire(self, p):
        golden = p.has(P_GOLDEN, self.time)
        homing = p.has(P_HOMING, self.time)
        ghost = p.has(P_GHOST, self.time)

        speed = BULLET_SPEED
        if golden:
            speed *= GOLDEN_SPEED_MULT
        elif p.has(P_VELOCITY, self.time):
            speed *= VELOCITY_MULT
        if ghost:
            speed *= GHOST_SPEED_MULT
        if p.boss:
            speed *= BOSS_BULLET_MULT

        flags = ((BF_GOLD if golden else 0) | (BF_HOMING if homing else 0)
                 | (BF_BOUNCE if p.has(P_BOUNCE, self.time) else 0)
                 | (BF_FROST if p.has(P_FROST, self.time) else 0)
                 | (BF_GHOST if ghost else 0))
        # The golden gun stays a single precise round even under scatter.
        offsets = ((-SCATTER_ANGLE, 0.0, SCATTER_ANGLE)
                   if p.has(P_SCATTER, self.time) and not golden else (0.0,))
        spread = 0.0 if golden else SPREAD

        muzzle = p.radius + BULLET_RADIUS + 2
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

    def _hostile(self, owner_pid, victim):
        """Can a round fired by `owner_pid` hurt `victim`?

        Free-for-all says yes to everyone but yourself. Teams -- either half of
        a capture-the-flag match, or the mob during a boss round -- shoot
        through each other, so nobody loses a round to a teammate's stray shot
        while everyone is crowding the same corridor.
        """
        if victim.pid == owner_pid:
            return False
        owner = self.players.get(owner_pid)
        if owner is None:
            return True  # shooter disconnected; their rounds still count
        if self.ctf:
            return owner.team != victim.team
        if self.boss_pid is not None:
            return owner.boss != victim.boss
        return True

    def _home(self, b, dt):
        """Curve a homing round towards the nearest valid target."""
        best, best_d2 = None, HOMING_RANGE * HOMING_RANGE
        for p in self.players.values():
            if not p.alive or p.waiting or not self._hostile(b.owner, p):
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

            # A ghost round only reports blocked at the arena border, so the
            # two powerups compose: it passes through the map and, if it is
            # also a ricochet, rebounds off the edge instead of dying there.
            if self._bullet_blocked(x0, y0, b.x, b.y, b.flags):
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
                if not p.alive or p.waiting or not self._hostile(b.owner, p):
                    continue
                if segment_hits_circle(x0, y0, b.x, b.y, p.x, p.y,
                                       p.radius + BULLET_RADIUS):
                    victim = p
                    break

            if victim is None or self._resolve_hit(victim, b):
                alive.append(b)
            else:
                self._drop_frost(b)
        self.bullets = alive

    def _bullet_blocked(self, x0, y0, x1, y1, flags=0):
        if flags & BF_GHOST:
            # A ghost round ignores the map, but not the edge of it -- letting
            # them leave would just be a shot that quietly never existed.
            return any(point_in_rect(x1, y1, rect) for rect in BORDER)
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
        # Flags matter here: a ghost round is only ever "wedged" by the border,
        # and would otherwise be killed by the wall it is entitled to fly
        # through on its way out of the bounce.
        if self._bullet_blocked(b.x, b.y, b.x, b.y, b.flags):
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

        # A boss is the one thing here with a health bar; chip it instead.
        if victim.hp > 1:
            victim.hp -= 1
            if shooter and shooter is not victim:
                shooter.kills += 1
            self.events.append({"kind": "bosshit", "pid": victim.pid,
                                "hp": victim.hp, "hp_max": victim.hp_max,
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
            "boss": 1 if victim.boss else 0,
            "x": victim.x, "y": victim.y,
        })
        self._drop_flag(victim)

        if self.phase == PHASE_WAITING:
            # Practice mode: straight back in, no round to lose.
            self._spawn(victim, self._spawn_points())
        else:
            victim.alive = False
            victim.powers.clear()
            if self.ctf:
                victim.respawn_at = self.time + CTF_RESPAWN_TIME
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
                        <= (p.radius + BOX_RADIUS) ** 2):
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

    # -- capture the flag -----------------------------------------------------

    def carried_by(self, player):
        """The flag this player is running, if any. Cheap -- there are two."""
        for flag in self.flags:
            if flag.carrier == player.pid:
                return flag
        return None

    def _drop_flag(self, player):
        """Let go of whatever a player was carrying, wherever they were."""
        for flag in self.flags:
            if flag.carrier != player.pid:
                continue
            flag.carrier = None
            flag.at_home = False
            flag.x, flag.y = player.x, player.y
            flag.return_at = self.time + FLAG_RETURN_TIME
            self.events.append({"kind": "flagdrop", "team": flag.team,
                                "x": flag.x, "y": flag.y})

    def _step_flags(self, dt):
        """Pickups, returns and captures, in that order, once per tick."""
        for flag in self.flags:
            if flag.carrier is not None:
                carrier = self.players.get(flag.carrier)
                if carrier is None:
                    # Should not happen -- remove_player covers it -- but a
                    # flag stuck on a ghost carrier would be unrecoverable.
                    flag.send_home()
                    self.events.append({"kind": "flagreturn", "team": flag.team,
                                        "auto": 1})
                elif carrier.alive and not carrier.waiting:
                    flag.x, flag.y = carrier.x, carrier.y
                else:
                    # Died or was benched between ticks; treat it as a drop.
                    self._drop_flag(carrier)
                continue
            if flag.state == FLAG_DROPPED and self.time >= flag.return_at:
                flag.send_home()
                self.events.append({"kind": "flagreturn", "team": flag.team,
                                    "auto": 1})

        for p in self.players.values():
            if not p.alive or p.waiting:
                continue
            reach = (p.radius + FLAG_RADIUS) ** 2
            for flag in self.flags:
                if flag.carrier is not None:
                    continue
                if (p.x - flag.x) ** 2 + (p.y - flag.y) ** 2 > reach:
                    continue
                if flag.team == p.team:
                    # Touching your own flag on the floor puts it back on the
                    # stand -- the standard counter to a stalled capture.
                    if flag.state == FLAG_DROPPED:
                        flag.send_home()
                        self.events.append({"kind": "flagreturn",
                                            "team": flag.team, "pid": p.pid,
                                            "name": p.name})
                else:
                    flag.carrier = p.pid
                    flag.at_home = False
                    flag.x, flag.y = p.x, p.y
                    self.events.append({"kind": "flagtake", "team": flag.team,
                                        "pid": p.pid, "name": p.name,
                                        "color": p.color, "by_team": p.team,
                                        "x": p.x, "y": p.y})
            self._try_capture(p)

    def _try_capture(self, p):
        """Score if a carrier reaches home -- and their own flag is on it.

        Requiring your flag to be home is what makes defending worth doing:
        two players cannot simply run past each other and trade captures.
        """
        if p.team not in (0, 1):
            return
        carried = self.carried_by(p)
        if carried is None:
            return
        own = self.flags[p.team]
        home_x, home_y = own.home_x, own.home_y
        if (p.x - home_x) ** 2 + (p.y - home_y) ** 2 > BASE_RADIUS ** 2:
            return
        if not own.at_home:
            # Throttled: they are standing on the stand, so this would
            # otherwise fire sixty times a second while they wait.
            if self.time - self._nocap_at.get(p.pid, -99.0) > 2.5:
                self._nocap_at[p.pid] = self.time
                self.events.append({"kind": "nocap", "pid": p.pid,
                                    "team": p.team})
            return

        carried.send_home()
        self.team_score[p.team] += 1
        self.events.append({"kind": "capture", "pid": p.pid, "name": p.name,
                            "color": p.color, "team": p.team,
                            "score": list(self.team_score),
                            "x": p.x, "y": p.y})

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
        elif kind == P_QUAKE:
            # Nothing in the simulation moves for this one -- the server just
            # names everyone whose window should start rattling. Teammates are
            # spared, so it uses the same sides everything else does.
            targets = [q.pid for q in self.players.values()
                       if self._hostile(p.pid, q)]
            self.events.append({"kind": "quake", "pids": targets,
                                "secs": POWERUP_DURATION[P_QUAKE],
                                "name": p.name, "color": p.color})
        self.events.append({"kind": "pickup", "pid": p.pid, "power": kind,
                            "name": p.name, "color": p.color,
                            "x": p.x, "y": p.y})

    # -- serialisation --------------------------------------------------------

    def roster(self):
        return [{"id": p.pid, "name": p.name, "color": p.color,
                 "team": p.team}
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
            p.team,
            1 if p.boss else 0,
            p.hp, p.hp_max,
            # Seconds until they are back, for the client's respawn clock.
            (round(max(0.0, p.respawn_at - now), 1)
             if not p.alive and p.respawn_at > 0.0 else 0),
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
            "gm": self.mode,
            # What the next round will be. Equal to "gm" when nothing is
            # queued, so the client just compares the two.
            "nm": self.mode if self.next_mode is None else self.next_mode,
            "bs": self.boss_pid or 0,
            "sc": list(self.team_score) if self.ctf else [0, 0],
            # "fl" flags, "fz" frost -- both features arrived wanting to be
            # "f", and a dict literal keeps only the last of a repeated key.
            "fl": [[f.team, round(f.x, 1), round(f.y, 1),
                    f.carrier or 0, f.state] for f in self.flags],
            "p": [self._player_row(p, now, viewer)
                  for p in self.players.values()],
            "b": [[round(b.x, 1), round(b.y, 1), b.owner, b.flags]
                  for b in self.bullets],
            "l": [[box.bid, round(box.x, 1), round(box.y, 1), box.kind]
                  for box in self.loot],
            # Ice patches: position, who laid it (they walk through freely) and
            # how much life is left, which the client fades out.
            "fz": [[round(z.x, 1), round(z.y, 1), z.owner,
                    round(z.ttl / FROST_TIME, 2)] for z in self.frost],
            # Time stop: seconds left, and who is still allowed to move.
            "hd": round(max(0.0, self.hold_until - now), 2),
            "hp": self.hold_pid,
            # Obstacles are a pure function of the map clock, and clients have
            # the same maps.py, so one float keeps every screen in lockstep.
            "mt": round(self.map_time, 3),
        }
