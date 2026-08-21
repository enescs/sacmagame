"""pygame client: a menu that finds LAN games, then the game view.

The client is a thin renderer. It sends input every frame and draws whatever
snapshot arrived last -- no prediction, because on a LAN the round trip is
sub-frame anyway. The only thing it computes locally is cosmetic: obstacle
positions (a pure function of the map clock the server sends) and particles.

    python -m sacma.client                 # scan the LAN and pick a game
    python -m sacma.client --host 10.0.0.5 # straight in
"""

import argparse
import math
import os
import random
import socket
import sys
import time

import pygame

from .maps import MAPS, mover_rect, rotor_segment
from .net import Discovery, NetClient
from .shared import (
    ARENA_H, ARENA_W, BASE_RADIUS, BF_BOUNCE, BF_FROST, BF_GHOST, BF_GOLD,
    BF_HOMING, BF_PARRIED, BG, BORDER, BOSS_COLOR, BOSS_MAG, BOSS_RADIUS,
    BOX_RADIUS, BULLET_RADIUS, CHAT_MAX_LEN, CHAT_MAX_LINES, CHAT_SHOW_TIME,
    CTF_CAPTURES_TO_WIN, DEFAULT_PORT, FLAG_CARRIED, FLAG_DROPPED, FLAG_RADIUS,
    FROST_RADIUS, GRID, HAZARD_EDGE, HAZARD_FILL, HUD_BG, HUD_H, MAG_SIZE,
    MODE_BLURB, MODE_CTF, MODE_LABEL, MODE_ORDER, PHASE_COUNTDOWN, PHASE_LIVE,
    PHASE_OVER, PHASE_WAITING, PLAYER_COLORS, PLAYER_RADIUS, POWERUPS,
    POWERUP_BIT, POWERUP_COLOR, POWERUP_LABEL, P_BOUNCE, P_FROST, P_GHOST,
    P_GOLDEN, P_HOLD, P_HOMING, P_INVIS, P_REFLECT, P_SHIELD, QUAKE_AMPLITUDE,
    QUAKE_FREQ, TEAM_COLORS, TEAM_NAMES, TEXT, TEXT_DIM, WALL_EDGE, WALL_FILL,
    WINDOW_H, WINDOW_W,
)

# Snapshot player-row indices, mirroring Game.snapshot().
P_ID, P_X, P_Y, P_AIM, P_ALIVE, P_AMMO, P_RELOAD, P_BITS = 0, 1, 2, 3, 4, 5, 6, 7
P_WINS, P_KILLS, P_DEATHS, P_WAIT, P_HID = 8, 9, 10, 11, 12
P_TEAM, P_BOSS, P_HP, P_HPMAX, P_RESP = 13, 14, 15, 16, 17
P_PLEFT = 18  # {powerup: fraction of its duration left}, own row only
P_IMMUNE = 19  # seconds of capture-the-flag spawn immunity left


POWER_PANEL_W = 200  # the live-powerup readout in the arena's top right corner

ICON_DIR = os.path.join(os.path.dirname(__file__), "assets", "powerups")
ICON_SIZE = 20  # 16x16 art, drawn slightly enlarged to fill the crate

# Art named for the powerup as a player would say it rather than for the key
# the sim uses; both spellings load. Keys are written out literally because
# P_AMMO in this module is a snapshot column index, not the powerup name.
ICON_ALIASES = {
    "ammo": ("infammo",),
    "rapid": ("rapidfire",),
    "velocity": ("hotrounds",),
    "swift": ("sprint",),
    "invis": ("invisible",),
    "golden": ("goldengun",),
    "bounce": ("recochet", "ricochet"),
    "hold": ("timestop",),
    "ghost": ("ghostrounds",),
    "quake": ("earthquake",),
}


def load_powerup_icons():
    """Load `assets/powerups/<kind>.png` for every powerup, if it is there.

    Entirely optional: anything missing falls back to the lettered crate, so
    the game runs from a bare checkout and new art can be dropped in one file
    at a time without touching any code.
    """
    icons = {}
    for name in POWERUPS:
        for stem in (name,) + ICON_ALIASES.get(name, ()):
            path = os.path.join(ICON_DIR, stem + ".png")
            if not os.path.exists(path):
                continue
            try:
                img = pygame.image.load(path).convert_alpha()
            except pygame.error:
                continue
            if img.get_size() != (ICON_SIZE, ICON_SIZE):
                img = pygame.transform.smoothscale(img, (ICON_SIZE, ICON_SIZE))
            icons[name] = img
            break
    return icons


def mix(a, b, t):
    """Blend two colours; t=0 gives `a`, t=1 gives `b`."""
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

# Debug map cycling. F1/F2 are the advertised pair; PageUp/PageDown are bound
# too for anyone on a laptop that steals the function row for brightness.
MAP_PREV_KEYS = (pygame.K_F1, pygame.K_PAGEUP)
MAP_NEXT_KEYS = (pygame.K_F2, pygame.K_PAGEDOWN)


class Effects:
    """Throwaway particles and rings. Purely cosmetic, driven by server events."""

    def __init__(self):
        self.bits = []
        self.rings = []
        self.flashes = []

    def burst(self, x, y, color, count=7, speed=190, life=0.32, spread=math.tau):
        base = random.uniform(0, math.tau)
        for _ in range(count):
            a = base + random.uniform(-spread / 2, spread / 2)
            s = speed * random.uniform(0.35, 1.0)
            self.bits.append([x, y, math.cos(a) * s, math.sin(a) * s,
                              life, life, color])

    def ring(self, x, y, color, r0=6, r1=42, life=0.4, width=3):
        self.rings.append([x, y, r0, r1, life, life, color, width])

    def flash(self, x, y, aim, color, life=0.07):
        self.flashes.append([x, y, aim, life, life, color])

    def update(self, dt):
        for b in self.bits:
            b[0] += b[2] * dt
            b[1] += b[3] * dt
            b[2] *= 0.90
            b[3] *= 0.90
            b[4] -= dt
        self.bits = [b for b in self.bits if b[4] > 0]

        for r in self.rings:
            r[4] -= dt
        self.rings = [r for r in self.rings if r[4] > 0]

        for f in self.flashes:
            f[3] -= dt
        self.flashes = [f for f in self.flashes if f[3] > 0]

    def draw(self, surf):
        for x, y, aim, life, maxlife, color in self.flashes:
            t = life / maxlife
            end = (x + math.cos(aim) * (18 + 14 * t),
                   y + math.sin(aim) * (18 + 14 * t))
            pygame.draw.line(surf, _fade(color, t), (x, y), end, 4)

        for x, y, r0, r1, life, maxlife, color, width in self.rings:
            t = 1.0 - life / maxlife
            pygame.draw.circle(surf, _fade(color, 1.0 - t), (int(x), int(y)),
                               int(r0 + (r1 - r0) * t), width)

        for x, y, _vx, _vy, life, maxlife, color in self.bits:
            t = life / maxlife
            pygame.draw.circle(surf, _fade(color, t), (int(x), int(y)),
                               max(1, int(1 + 2.5 * t)))


def _fade(color, t):
    t = max(0.0, min(1.0, t))
    return (int(color[0] * t + BG[0] * (1 - t)),
            int(color[1] * t + BG[1] * (1 - t)),
            int(color[2] * t + BG[2] * (1 - t)))


class WindowShake:
    """The earthquake powerup: rattle the actual desktop window.

    Moving the window rather than the camera is the whole point -- the mouse
    stays where it is on the desk while the arena slides under it, so aiming
    genuinely wanders for a few seconds. Two frequencies per axis keep it from
    reading as a straight diagonal buzz, and the amplitude decays to nothing.

    Not every desktop lets an application place its own window (Wayland, most
    obviously). We find out by asking for a move and reading the position back;
    if nothing happened, we shake the drawn frame inside the window instead,
    which looks nearly the same from a chair.
    """

    def __init__(self):
        self.left = 0.0
        self.total = 0.0
        self.base = None
        self.can_move = None   # None until the first attempts tell us
        self.offset = (0, 0)   # used only by the fallback
        self._probes = 0

    def start(self, seconds):
        if self.left <= 0.0:
            self.base = self._position()
        # Overlapping quakes extend rather than stack, so two pickups in a row
        # cannot double the amplitude.
        self.left = max(self.left, seconds)
        self.total = max(self.total, seconds)

    @property
    def active(self):
        return self.left > 0.0

    def _position(self):
        try:
            return pygame.display.get_window_position()
        except (AttributeError, pygame.error):
            return None

    def _move_to(self, pos):
        try:
            pygame.display.set_window_position(pos)
            return True
        except (AttributeError, pygame.error):
            return False

    def update(self, dt, now):
        if self.left <= 0.0:
            return
        self.left -= dt
        if self.left <= 0.0:
            self.stop()
            return

        amp = QUAKE_AMPLITUDE * (self.left / self.total if self.total else 0.0)
        dx = amp * (math.sin(now * QUAKE_FREQ) * 0.7
                    + math.sin(now * QUAKE_FREQ * 2.3 + 1.1) * 0.3)
        dy = amp * (math.sin(now * QUAKE_FREQ * 1.4 + 2.0) * 0.7
                    + math.sin(now * QUAKE_FREQ * 3.1) * 0.3)

        if self.can_move is not False and self.base:
            want = (int(self.base[0] + dx), int(self.base[1] + dy))
            moved = self._move_to(want)
            if self.can_move is None:
                # Probe: a window manager that ignores us keeps reporting the
                # old spot. Give it a few frames -- the move is a round trip
                # through the WM, so the first read back can lag -- and only
                # judge on frames where we asked for a visible displacement.
                if abs(want[0] - self.base[0]) + abs(want[1] - self.base[1]) >= 3:
                    got = self._position()
                    if moved and got and got != self.base:
                        self.can_move = True
                    else:
                        self._probes += 1
                        if self._probes >= 8:
                            self.can_move = False
            if self.can_move:
                self.offset = (0, 0)
                return
            if self.can_move is None:
                return  # still deciding; do not shake twice over
        self.offset = (int(dx), int(dy))

    def stop(self):
        self.left = 0.0
        self.offset = (0, 0)
        if self.can_move and self.base:
            self._move_to(self.base)
        self.base = None


class App:
    def __init__(self, name, host, port):
        pygame.init()
        pygame.display.set_caption("sacmagame")

        info = pygame.display.Info()
        self.desktop_size = (info.current_w, info.current_h)
        self.windowed_scale = min(1.0,
                                  (info.current_w - 40) / WINDOW_W,
                                  (info.current_h - 100) / WINDOW_H)
        self.fullscreen = False
        self.canvas = pygame.Surface((WINDOW_W, WINDOW_H))
        self.scale = 1.0
        self.win_size = (WINDOW_W, WINDOW_H)
        # Where the scaled frame sits in the window: (0, 0) windowed, and the
        # letterbox margin in fullscreen, where the screen is a different shape.
        self.origin = (0, 0)
        self.scaled = None
        self._apply_video_mode()
        self.clock = pygame.time.Clock()

        self.f_small = pygame.font.Font(None, 20)
        self.f_mid = pygame.font.Font(None, 26)
        self.f_big = pygame.font.Font(None, 44)
        self.f_huge = pygame.font.Font(None, 92)

        self.icons = load_powerup_icons()

        self.name = name
        self.net = None
        self.fx = Effects()
        self.shake = WindowShake()
        self.feed = []          # [(surface, expiry)]
        self.chat = []          # [[name surface, text surface, expiry]]
        self.banner = ""
        self.sub_banner = ""    # boss announcement, shown under the countdown
        self.round_result = ""

        self.discovery = Discovery()
        self.discovery.start()
        self.sel = 0
        self.typing = None      # None | "name" | "host" | "chat"
        self.typed = ""
        self.status = ""
        self.picking = False    # the in-game mode picker is open
        self.pick_sel = 0

        self.screen_state = "menu"
        if host:
            self.connect(host, port)

        self.port = port
        self.last_ping = 0.0

    # -- video mode -----------------------------------------------------------

    def _apply_video_mode(self):
        """(Re)open the display for the current windowed/fullscreen choice."""
        if self.fullscreen:
            self.screen = pygame.display.set_mode(self.desktop_size,
                                                  pygame.FULLSCREEN)
            w, h = self.screen.get_size()
            # Fit the frame inside the screen without distorting it; whatever
            # is left over becomes a black border.
            self.scale = min(w / WINDOW_W, h / WINDOW_H)
        else:
            self.scale = self.windowed_scale
            w = int(WINDOW_W * self.scale)
            h = int(WINDOW_H * self.scale)
            self.screen = pygame.display.set_mode((w, h))
        self.win_size = (int(WINDOW_W * self.scale), int(WINDOW_H * self.scale))
        self.origin = ((w - self.win_size[0]) // 2, (h - self.win_size[1]) // 2)
        self.scaled = pygame.Surface(self.win_size)

    def toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        # Nothing to move a fullscreen window to, so quakes shake the frame
        # inside it instead. Leaving fullscreen re-probes the window manager.
        self.shake.can_move = False if self.fullscreen else None
        self._apply_video_mode()

    # -- connection -----------------------------------------------------------

    def connect(self, host, port):
        self.net = NetClient(host, port, self.name)
        self.net.start()
        pygame.mouse.set_visible(False)
        self.screen_state = "play"
        self.feed.clear()
        self.chat.clear()
        self.banner = ""
        self.sub_banner = ""
        self.round_result = ""

    def disconnect(self):
        if self.net:
            self.net.close()
            self.net = None
        if self.typing == "chat":
            self.typing = None
        self.shake.stop()
        pygame.mouse.set_visible(True)
        self.screen_state = "menu"

    # -- main loop ------------------------------------------------------------

    def run(self):
        running = True
        while running:
            dt = min(self.clock.tick(60) / 1000.0, 0.05)
            running = self.handle_events()
            self.fx.update(dt)
            self.shake.update(dt, time.perf_counter())

            if self.screen_state == "menu":
                self.draw_menu()
            else:
                if self.net and self.net.error:
                    self.status = self.net.error
                    self.disconnect()
                else:
                    self.send_input()
                    self.drain_events()
                    self.draw_game()

            # The fallback path for desktops that will not let us move the
            # window: shift the frame inside it instead, on the same numbers.
            ox, oy = self.origin
            sx, sy = self.shake.offset
            at = (ox + sx, oy + sy)
            if at != (0, 0):
                self.screen.fill((0, 0, 0))
            if self.win_size == (WINDOW_W, WINDOW_H):
                self.screen.blit(self.canvas, at)
            elif at == (0, 0):
                pygame.transform.smoothscale(self.canvas, self.win_size,
                                             self.screen)
            else:
                pygame.transform.smoothscale(self.canvas, self.win_size,
                                             self.scaled)
                self.screen.blit(self.scaled, at)
            pygame.display.flip()

        self.disconnect()
        self.discovery.running = False
        pygame.quit()

    def mouse_canvas(self):
        mx, my = pygame.mouse.get_pos()
        return ((mx - self.origin[0]) / self.scale,
                (my - self.origin[1]) / self.scale)

    def handle_events(self):
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return False
            if ev.type != pygame.KEYDOWN:
                continue

            if ev.key == pygame.K_F11:
                # Checked ahead of everything else so it works while typing or
                # with the mode picker up.
                self.toggle_fullscreen()
                continue

            if self.typing:
                limit = CHAT_MAX_LEN if self.typing == "chat" else 24
                if ev.key == pygame.K_RETURN:
                    self.commit_typing()
                elif ev.key == pygame.K_ESCAPE:
                    self.typing = None
                elif ev.key == pygame.K_BACKSPACE:
                    self.typed = self.typed[:-1]
                elif ev.unicode and ev.unicode.isprintable() and len(self.typed) < limit:
                    self.typed += ev.unicode
                continue

            if self.picking:
                # The picker owns the keyboard while it is up, so choosing a
                # mode can never also walk you into the open.
                self.handle_pick_key(ev.key)
                continue

            if ev.key == pygame.K_ESCAPE:
                if self.screen_state == "play":
                    self.status = ""
                    self.disconnect()
                else:
                    return False

            elif (self.screen_state == "play" and self.net
                    and ev.key == pygame.K_m):
                self.open_picker()

            elif (self.screen_state == "play" and self.net
                    and ev.key in (pygame.K_t, pygame.K_RETURN)):
                # Open the chat box. Movement keys stop being read while it is
                # up, so typing "sw" does not walk you into a wall.
                self.typing, self.typed = "chat", ""

            elif (self.screen_state == "play" and self.net
                    and ev.key in MAP_PREV_KEYS + MAP_NEXT_KEYS):
                # Debug: flip through maps without playing rounds out. The
                # server ignores this if it was started with --no-debug.
                # Function and page keys sit in the same physical spot on every
                # keyboard layout, unlike the bracket keys they replaced --
                # those need AltGr on a Turkish keyboard.
                self.net.send({"t": "map",
                               "dir": 1 if ev.key in MAP_NEXT_KEYS else -1})

            elif self.screen_state == "menu":
                servers = self.discovery.servers()
                if ev.key in (pygame.K_DOWN, pygame.K_s) and servers:
                    self.sel = (self.sel + 1) % len(servers)
                elif ev.key in (pygame.K_UP, pygame.K_w) and servers:
                    self.sel = (self.sel - 1) % len(servers)
                elif ev.key == pygame.K_RETURN and servers:
                    s = servers[self.sel % len(servers)]
                    self.connect(s["host"], s["port"])
                elif ev.key == pygame.K_m:
                    self.typing, self.typed = "host", ""
                elif ev.key == pygame.K_TAB:
                    self.typing, self.typed = "name", self.name
        return True

    # -- mode picker ----------------------------------------------------------

    def open_picker(self):
        """Open on whatever the next round is already set to be."""
        snap = self.snapshot()
        current = snap.get("nm", snap.get("gm", 0)) if snap else 0
        self.pick_sel = (MODE_ORDER.index(current)
                         if current in MODE_ORDER else 0)
        self.picking = True

    def handle_pick_key(self, key):
        if key in (pygame.K_ESCAPE, pygame.K_m):
            self.picking = False
        elif key in (pygame.K_DOWN, pygame.K_s):
            self.pick_sel = (self.pick_sel + 1) % len(MODE_ORDER)
        elif key in (pygame.K_UP, pygame.K_w):
            self.pick_sel = (self.pick_sel - 1) % len(MODE_ORDER)
        elif pygame.K_1 <= key < pygame.K_1 + len(MODE_ORDER):
            self.pick_sel = key - pygame.K_1
            self.commit_pick()
        elif key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
            self.commit_pick()

    def commit_pick(self):
        if self.net:
            self.net.send({"t": "mode", "mode": MODE_ORDER[self.pick_sel]})
        self.picking = False

    def commit_typing(self):
        if self.typing == "name":
            self.name = self.typed.strip()[:14] or "player"
        elif self.typing == "chat":
            text = self.typed.strip()
            if text and self.net:
                self.net.send({"t": "chat", "text": text[:CHAT_MAX_LEN]})
        elif self.typing == "host":
            raw = self.typed.strip()
            if raw:
                host, _, p = raw.partition(":")
                try:
                    port = int(p) if p else self.port
                except ValueError:
                    port = self.port
                self.connect(host, port)
        self.typing = None

    # -- outbound input -------------------------------------------------------

    def send_input(self):
        net = self.net
        if not net or not net.connected:
            return

        keys = pygame.key.get_pressed()
        me = self.find_me()
        mx, my = self.mouse_canvas()
        aim = 0.0
        if me:
            aim = math.atan2(my - me[P_Y], mx - me[P_X])

        # While the chat box or the mode picker is open the keyboard belongs to
        # it -- keep aiming (the mouse is still yours) but stop moving,
        # shooting and reloading.
        chatting = self.typing == "chat" or self.picking

        net.send({
            "t": "input",
            "up": not chatting and (keys[pygame.K_w] or keys[pygame.K_UP]),
            "down": not chatting and (keys[pygame.K_s] or keys[pygame.K_DOWN]),
            "left": not chatting and (keys[pygame.K_a] or keys[pygame.K_LEFT]),
            "right": not chatting and (keys[pygame.K_d] or keys[pygame.K_RIGHT]),
            "shoot": (not chatting
                      and (pygame.mouse.get_pressed()[0] or keys[pygame.K_SPACE])),
            "reload": not chatting and keys[pygame.K_r],
            "aim": round(aim, 4),
        })

        now = time.perf_counter()
        if now - self.last_ping > 0.5:
            self.last_ping = now
            net.send({"t": "ping", "ts": now})

    # -- inbound events -> effects & feed -------------------------------------

    def drain_events(self):
        net = self.net
        now = time.time()
        while net.events:
            ev = net.events.popleft()
            kind = ev.get("kind")

            if kind == "shot":
                who = net.roster.get(ev["pid"], {})
                col = PLAYER_COLORS[who.get("color", 5) % len(PLAYER_COLORS)]
                self.fx.flash(ev["x"], ev["y"], ev["aim"], col)

            elif kind == "spark":
                self.fx.burst(ev["x"], ev["y"], (255, 226, 160),
                              count=6, speed=150, life=0.25)

            elif kind == "hold":
                col = POWERUP_COLOR[P_HOLD]
                self.fx.ring(ev["x"], ev["y"], col, r0=10, r1=520, life=0.5,
                             width=6)
                self.fx.ring(ev["x"], ev["y"], col, r0=10, r1=300, life=0.35,
                             width=3)
                self.push_feed(f"{ev['name']} stopped time", col)

            elif kind == "frost":
                self.fx.ring(ev["x"], ev["y"], POWERUP_COLOR[P_FROST],
                             r0=8, r1=int(FROST_RADIUS), life=0.35, width=3)

            elif kind == "ricochet":
                self.fx.burst(ev["x"], ev["y"], POWERUP_COLOR[P_BOUNCE],
                              count=5, speed=120, life=0.2)

            elif kind == "kill":
                col = PLAYER_COLORS[ev["victim_color"] % len(PLAYER_COLORS)]
                big = ev.get("boss")
                self.fx.burst(ev["x"], ev["y"], BOSS_COLOR if big else col,
                              count=34 if big else 18,
                              speed=330 if big else 260, life=0.6)
                self.fx.ring(ev["x"], ev["y"], col, r0=8,
                             r1=110 if big else 54, life=0.45)
                self.push_feed(f"{ev['killer']}  ->  {ev['victim']}",
                               PLAYER_COLORS[ev["killer_color"]
                                             % len(PLAYER_COLORS)])

            elif kind == "bosshit":
                self.fx.burst(ev["x"], ev["y"], BOSS_COLOR, count=8, speed=170,
                              life=0.3)

            elif kind == "shieldbreak":
                self.fx.ring(ev["x"], ev["y"], POWERUP_COLOR[P_SHIELD],
                             r0=10, r1=46, life=0.35, width=4)

            elif kind == "parry":
                col = POWERUP_COLOR[P_REFLECT]
                self.fx.ring(ev["x"], ev["y"], col, r0=26, r1=8, life=0.3,
                             width=4)
                self.fx.burst(ev["x"], ev["y"], col, count=8, speed=210,
                              life=0.28)

            elif kind == "pickup":
                col = POWERUP_COLOR[ev["power"]]
                self.fx.ring(ev["x"], ev["y"], col, r0=8, r1=50, life=0.45)
                self.push_feed(f"{ev['name']} got {POWERUP_LABEL[ev['power']]}",
                               col)

            elif kind == "drop":
                self.fx.ring(ev["x"], ev["y"], (240, 240, 240),
                             r0=40, r1=8, life=0.5, width=2)
                self.push_feed("supply drop", TEXT_DIM)

            elif kind == "round":
                self.round_result = ""
                self.banner = f"{ev['name'].upper()}"
                self.sub_banner = ""
                self.push_feed(f"round {ev['round']} -- {ev['name']} "
                               f"({ev['blurb']})", TEXT_DIM)

            elif kind == "win":
                col = PLAYER_COLORS[ev["color"] % len(PLAYER_COLORS)]
                self.round_result = f"{ev['name']} WINS"
                self.push_feed(f"{ev['name']} takes the round", col)

            elif kind == "draw":
                self.round_result = "DRAW"

            elif kind == "boss":
                # Announced during the countdown, so everybody has three
                # seconds to work out where the boss is standing.
                mine = ev["pid"] == self.net.my_id
                self.sub_banner = ("YOU ARE THE BOSS" if mine
                                   else f"BOSS: {ev['name']}")
                self.push_feed(f"BOSS ROUND -- {ev['name']} has "
                               f"{ev['hp']} hp, everyone else is on the same "
                               f"side", BOSS_COLOR)

            elif kind == "bossend":
                if ev["boss_won"]:
                    self.round_result = f"{ev['name']} SURVIVED"
                    self.push_feed(f"the boss beat all {ev['hunters']} of them",
                                   BOSS_COLOR)
                else:
                    self.round_result = "BOSS DOWN"
                    self.push_feed("the hunters take the round", TEXT)

            elif kind == "teams":
                self.push_feed(f"teams: {' + '.join(ev['blue'])}  vs  "
                               f"{' + '.join(ev['red'])}", TEXT)

            elif kind == "modenext":
                who = ev.get("name") or "somebody"
                self.push_feed(f"{who} set the next round to "
                               f"{MODE_LABEL[ev['mode']]}", (255, 214, 92))

            elif kind == "mode":
                self.push_feed(f"mode: {MODE_LABEL[ev['mode']]}", TEXT)

            elif kind == "modefail":
                self.push_feed(f"can't switch: {ev['reason']}",
                               (235, 130, 130))

            elif kind == "notice":
                self.push_feed(ev["text"], (235, 180, 120))

            elif kind == "quake":
                if self.net.my_id in ev.get("pids", []):
                    self.shake.start(ev.get("secs", 3.0))

            elif kind == "flagtake":
                col = TEAM_COLORS[ev["team"] % 2]
                self.fx.ring(ev["x"], ev["y"], col, r0=30, r1=8, life=0.4,
                             width=3)
                self.push_feed(f"{ev['name']} has the "
                               f"{TEAM_NAMES[ev['team'] % 2]} flag", col)

            elif kind == "flagdrop":
                col = TEAM_COLORS[ev["team"] % 2]
                self.fx.burst(ev["x"], ev["y"], col, count=10, speed=150,
                              life=0.4)
                self.push_feed(f"{TEAM_NAMES[ev['team'] % 2]} flag dropped",
                               col)

            elif kind == "flagreturn":
                name = TEAM_NAMES[ev["team"] % 2]
                who = ev.get("name")
                self.push_feed(f"{name} flag returned"
                               + (f" by {who}" if who else ""),
                               TEAM_COLORS[ev["team"] % 2])

            elif kind == "capture":
                col = TEAM_COLORS[ev["team"] % 2]
                self.fx.ring(ev["x"], ev["y"], col, r0=10, r1=140, life=0.7,
                             width=4)
                self.fx.burst(ev["x"], ev["y"], col, count=24, speed=300,
                              life=0.7)
                a, b = ev["score"]
                self.push_feed(f"{ev['name']} scores -- {a} : {b}", col)

            elif kind == "nocap":
                if ev["pid"] == self.net.my_id:
                    self.push_feed("your own flag has to be home to score",
                                   (235, 180, 120))

            elif kind == "teamend":
                a, b = ev["score"]
                if a == b:
                    self.round_result = f"DRAW  {a} : {b}"
                else:
                    team = 0 if a > b else 1
                    self.round_result = f"{TEAM_NAMES[team]} WINS  {a} : {b}"

            elif kind == "respawn":
                col = TEAM_COLORS[ev["team"] % 2] if ev["team"] in (0, 1) \
                    else TEXT_DIM
                self.fx.ring(ev["x"], ev["y"], col, r0=34, r1=6, life=0.35,
                             width=2)

            elif kind == "chat":
                self.push_chat(ev["name"], ev["color"], ev["text"])

            elif kind == "join":
                self.push_feed(f"{ev['name']} joined", TEXT_DIM)

            elif kind == "leave":
                self.push_feed(f"{ev['name']} left", TEXT_DIM)

        self.feed = [f for f in self.feed if f[1] > now]

    def push_feed(self, text, color):
        self.feed.append((self.f_small.render(text, True, color),
                          time.time() + 4.0))
        self.feed = self.feed[-6:]

    def push_chat(self, name, color, text):
        col = PLAYER_COLORS[color % len(PLAYER_COLORS)]
        self.chat.append([self.f_small.render(f"{name}:", True, col),
                          self.f_small.render(text, True, TEXT),
                          time.time() + CHAT_SHOW_TIME])
        self.chat = self.chat[-CHAT_MAX_LINES:]

    # -- helpers --------------------------------------------------------------

    def snapshot(self):
        return self.net.state if self.net else None

    def find_me(self):
        snap = self.snapshot()
        if not snap or self.net.my_id is None:
            return None
        for row in snap["p"]:
            if row[P_ID] == self.net.my_id:
                return row
        return None

    # -- drawing: menu --------------------------------------------------------

    def draw_menu(self):
        c = self.canvas
        c.fill(BG)
        _text(c, self.f_huge, "SACMAGAME", (60, 60), TEXT)
        _text(c, self.f_mid,
              "last one standing  ·  one bullet kills  ·  same wifi only",
              (64, 150), TEXT_DIM)

        _text(c, self.f_mid, f"you are:  {self.name}", (64, 210), TEXT)
        _text(c, self.f_small, "[Tab] rename", (330, 216), TEXT_DIM)

        _text(c, self.f_big, "GAMES ON THIS NETWORK", (64, 270), TEXT)

        servers = self.discovery.servers()
        y = 330
        if self.discovery.error:
            _text(c, self.f_mid, self.discovery.error, (64, y), (230, 140, 140))
        elif not servers:
            _text(c, self.f_mid, "scanning...  (nobody is hosting yet)",
                  (64, y), TEXT_DIM)
            _text(c, self.f_small,
                  "to host:  .venv/bin/python -m sacma.server",
                  (64, y + 34), TEXT_DIM)
        else:
            self.sel %= len(servers)
            for i, s in enumerate(servers):
                row_y = y + i * 44
                selected = i == self.sel
                if selected:
                    pygame.draw.rect(c, (34, 38, 52), (56, row_y - 6, 960, 40),
                                     border_radius=4)
                col = TEXT if selected else TEXT_DIM
                _text(c, self.f_mid,
                      f"{'>' if selected else ' '}  {s['name']}", (64, row_y), col)
                _text(c, self.f_small,
                      f"{s['host']}:{s['port']}", (470, row_y + 4), TEXT_DIM)
                _text(c, self.f_small,
                      f"{s['players']}/{s['max']} players", (628, row_y + 4),
                      TEXT_DIM)
                mode = s.get("mode", 0)
                _text(c, self.f_small, MODE_LABEL.get(mode, "?"),
                      (748, row_y + 4),
                      TEAM_COLORS[0] if mode == MODE_CTF else TEXT_DIM)
                _text(c, self.f_small, f"map: {s['map']}", (888, row_y + 4),
                      TEXT_DIM)

        _text(c, self.f_mid,
              "[Enter] join     [M] type an IP     [Esc] quit",
              (64, WINDOW_H - 150), TEXT_DIM)

        if self.typing:
            label = "your name" if self.typing == "name" else "host  (ip or ip:port)"
            pygame.draw.rect(c, (28, 31, 44), (56, WINDOW_H - 108, 700, 52),
                             border_radius=4)
            _text(c, self.f_mid, f"{label}:  {self.typed}_",
                  (72, WINDOW_H - 92), TEXT)
        elif self.status:
            _text(c, self.f_mid, self.status, (64, WINDOW_H - 96), (235, 130, 130))

    # -- drawing: game --------------------------------------------------------

    def draw_game(self):
        c = self.canvas
        c.fill(BG)
        snap = self.snapshot()

        if not snap:
            _text(c, self.f_big, "connecting...", (60, 60), TEXT_DIM)
            return

        arena = c.subsurface((0, 0, ARENA_W, ARENA_H))
        self.draw_arena(arena, snap)
        self.fx.draw(arena)
        self.draw_feed(arena, self.draw_power_timers(arena, snap))
        self.draw_overlay(arena, snap)
        self.draw_pending_mode(arena, snap)
        self.draw_chat(arena)
        if self.picking:
            self.draw_picker(arena, snap)
        self.draw_crosshair(arena)
        self.draw_hud(c, snap)

    def draw_crosshair(self, s):
        """Draw the aiming reticle where the mouse is.

        The system cursor is hidden while playing: an arrow points from its
        top-left corner, which is a few pixels off from where the round
        actually goes, and on a scaled window it is the wrong size as well.
        This sits exactly on the point the server aims at.

        Four ticks with a gap in the middle rather than a full cross, so the
        thing you are about to shoot is never underneath it.
        """
        mx, my = self.mouse_canvas()
        if my > ARENA_H:
            return  # over the HUD; nothing to aim at down there
        x, y = int(mx), int(my)
        col = PLAYER_COLORS[self.net.my_color % len(PLAYER_COLORS)]

        gap, arm = 5, 11
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            pygame.draw.line(s, col,
                             (x + dx * gap, y + dy * gap),
                             (x + dx * arm, y + dy * arm), 2)
        pygame.draw.circle(s, col, (x, y), 1)

    def draw_pending_mode(self, s, snap):
        """A queued mode change, said out loud until it lands.

        Top-centre of the arena: the HUD is full, the feed owns the right and
        chat owns the bottom left, and this is a thing you want to notice
        without going looking for it.
        """
        queued = snap.get("nm", snap.get("gm", 0))
        if queued == snap.get("gm", 0):
            return
        text = f"next round:  {MODE_LABEL[queued].upper()}"
        img = self.f_small.render(text, True, (255, 214, 92))
        pad = 10
        w, h = img.get_width() + pad * 2, img.get_height() + 8
        x = (ARENA_W - w) // 2
        plate = pygame.Surface((w, h), pygame.SRCALPHA)
        plate.fill((10, 12, 18, 190))
        s.blit(plate, (x, 8))
        pygame.draw.rect(s, (92, 82, 46), (x, 8, w, h), 1)
        s.blit(img, (x + pad, 12))

    def draw_picker(self, s, snap):
        """The [M] menu: pick what the next round is played as."""
        now = snap.get("gm", 0)
        queued = snap.get("nm", now)

        w, h = 560, 108 + len(MODE_ORDER) * 62
        x, y = (ARENA_W - w) // 2, (ARENA_H - h) // 2 - 30
        panel = pygame.Surface((w, h), pygame.SRCALPHA)
        panel.fill((10, 12, 18, 234))
        s.blit(panel, (x, y))
        pygame.draw.rect(s, (70, 78, 104), (x, y, w, h), 1)

        _text(s, self.f_big, "GAME MODE", (x + 24, y + 18), TEXT)
        _text(s, self.f_small, "takes effect next round",
              (x + 232, y + 32), TEXT_DIM)

        for i, mode in enumerate(MODE_ORDER):
            row_y = y + 68 + i * 62
            picked = i == self.pick_sel
            if picked:
                pygame.draw.rect(s, (34, 38, 52),
                                 (x + 14, row_y - 8, w - 28, 54),
                                 border_radius=4)
            col = TEXT if picked else TEXT_DIM
            _text(s, self.f_mid, f"{i + 1}   {MODE_LABEL[mode]}",
                  (x + 26, row_y), col)
            _text(s, self.f_small, MODE_BLURB[mode], (x + 26, row_y + 26),
                  TEXT_DIM)
            # Say plainly which one is running and which one is on its way.
            if mode == queued and queued != now:
                _text(s, self.f_small, "NEXT ROUND", (x + w - 118, row_y + 4),
                      (255, 214, 92))
            elif mode == now:
                _text(s, self.f_small, "PLAYING NOW", (x + w - 118, row_y + 4),
                      TEAM_COLORS[0])

        _text(s, self.f_small,
              "[1-3] or [Enter] choose     [M] / [Esc] close",
              (x + 24, y + h - 30), TEXT_DIM)

    def draw_arena(self, s, snap):
        m = MAPS[snap["mi"] % len(MAPS)]
        t = snap.get("mt", 0.0)

        for x in range(0, ARENA_W, 64):
            pygame.draw.line(s, GRID, (x, 0), (x, ARENA_H))
        for y in range(0, ARENA_H, 64):
            pygame.draw.line(s, GRID, (0, y), (ARENA_W, y))

        for rect in BORDER + list(m["walls"]):
            pygame.draw.rect(s, WALL_FILL, rect)
            pygame.draw.rect(s, WALL_EDGE, rect, 2)

        # Moving and rotating obstacles get a warmer colour so it reads at a
        # glance that they will not stay where they are.
        for mv in m["movers"]:
            r = mover_rect(mv, t)
            rect = pygame.Rect(int(r[0]), int(r[1]), int(r[2]), int(r[3]))
            pygame.draw.rect(s, HAZARD_FILL, rect)
            pygame.draw.rect(s, HAZARD_EDGE, rect, 2)

        for rt in m["rotors"]:
            (x0, y0), (x1, y1), rad = rotor_segment(rt, t)
            pygame.draw.line(s, HAZARD_FILL, (x0, y0), (x1, y1), int(rad * 2))
            pygame.draw.circle(s, HAZARD_EDGE, (int(x0), int(y0)), int(rad), 2)
            pygame.draw.circle(s, HAZARD_EDGE, (int(x1), int(y1)), int(rad), 2)
            pygame.draw.circle(s, HAZARD_EDGE, (int(rt[0]), int(rt[1])), 5)

        # Ice goes under everything that moves, so it reads as ground cover
        # rather than as another thing flying around.
        for zx, zy, zowner, zlife in snap.get("fz", ()):
            self.draw_frost(s, zx, zy, zowner, zlife)

        if snap.get("gm") == MODE_CTF:
            self.draw_ctf(s, snap)

        for bid, bx, by, kind in snap["l"]:
            self.draw_box(s, bx, by, kind)

        for bullet in snap["b"]:
            self.draw_bullet(s, bullet)

        for row in snap["p"]:
            self.draw_player(s, row)

        if snap.get("hd", 0) > 0:
            self.draw_hold(s, snap)

    def draw_hold(self, s, snap):
        """Tint the arena while time is stopped and mark who is still moving.

        Everything on screen is already standing still because the server sent
        it that way; this just makes it obvious that it is a time stop and not
        a dropped connection.
        """
        col = POWERUP_COLOR[P_HOLD]
        holder = snap.get("hp", 0)
        mine = holder == self.net.my_id

        veil = pygame.Surface((ARENA_W, ARENA_H), pygame.SRCALPHA)
        veil.fill(col + (26,))
        s.blit(veil, (0, 0))
        pygame.draw.rect(s, col, (0, 0, ARENA_W, ARENA_H), 3)

        # A halo on the one player who can still act.
        for row in snap["p"]:
            if row[P_ID] != holder or not row[P_ALIVE] or row[P_WAIT]:
                continue
            pygame.draw.circle(s, col, (int(row[P_X]), int(row[P_Y])),
                               PLAYER_RADIUS + 12, 2)

        info = self.net.roster.get(holder, {})
        who = "YOU" if mine else info.get("name", "?")
        label = self.f_big.render(f"TIME STOP -- {who}", True, col)
        s.blit(label, label.get_rect(center=(ARENA_W // 2, 70)))

    def draw_frost(self, s, x, y, owner, life):
        """Draw one ice patch, fading as it expires.

        The rim carries the colour of whoever laid it, since that is also who
        can run through it at full speed -- your own patches are safe ground.
        """
        r = int(FROST_RADIUS)
        info = self.net.roster.get(owner, {})
        rim = PLAYER_COLORS[info.get("color", 5) % len(PLAYER_COLORS)]
        cold = POWERUP_COLOR[P_FROST]

        layer = pygame.Surface((r * 2, r * 2), pygame.SRCALPHA)
        pygame.draw.circle(layer, cold + (int(60 * life),), (r, r), r)
        pygame.draw.circle(layer, cold + (int(120 * life),), (r, r), r, 2)
        pygame.draw.circle(layer, rim + (int(150 * life),), (r, r), r - 3, 1)
        # A couple of shards so the patch has some texture at a glance.
        for i in range(6):
            a = i * math.pi / 3 + x * 0.01
            pygame.draw.line(
                layer, cold + (int(90 * life),),
                (r + math.cos(a) * r * 0.35, r + math.sin(a) * r * 0.35),
                (r + math.cos(a) * r * 0.8, r + math.sin(a) * r * 0.8), 2)
        s.blit(layer, (int(x) - r, int(y) - r))

    def draw_bullet(self, s, bullet):
        """Draw a round in its shooter's colour.

        Whose bullet a round is matters more in the moment than what kind it
        is, so the body always carries the owner's player colour and the
        special-round accent is demoted to a halo and a bright core. Ownership
        follows a parry, so a reflected round changes colour with it.
        """
        bx, by, owner = bullet[0], bullet[1], bullet[2]
        fl = bullet[3] if len(bullet) > 3 else 0

        info = self.net.roster.get(owner, {})
        body = PLAYER_COLORS[info.get("color", 5) % len(PLAYER_COLORS)]

        rad = 0
        if fl & BF_GOLD:
            halo, core, rad = POWERUP_COLOR[P_GOLDEN], (255, 250, 225), 2
        elif fl & BF_HOMING:
            halo, core, rad = POWERUP_COLOR[P_HOMING], (255, 235, 250), 1
        elif fl & BF_PARRIED:
            halo, core, rad = POWERUP_COLOR[P_REFLECT], (235, 255, 250), 1
        elif fl & BF_FROST:
            halo, core, rad = POWERUP_COLOR[P_FROST], (240, 255, 255), 1
        elif fl & BF_BOUNCE:
            halo, core, rad = POWERUP_COLOR[P_BOUNCE], (255, 255, 240), 1
        else:
            halo, core = mix(body, BG, 0.45), mix(body, (255, 255, 255), 0.7)

        pos = (int(bx), int(by))
        if fl & BF_GHOST:
            # Hollow and unfilled, so a round crossing a wall reads as passing
            # through it rather than sitting on top of it. The owner's colour
            # stays on the rim, which is what you need mid-fight.
            pygame.draw.circle(s, POWERUP_COLOR[P_GHOST], pos,
                               BULLET_RADIUS + 3 + rad, 2)
            pygame.draw.circle(s, mix(body, BG, 0.35), pos,
                               BULLET_RADIUS + 1 + rad, 1)
            pygame.draw.circle(s, mix(core, BG, 0.45), pos,
                               max(1, BULLET_RADIUS - 1 + rad))
            return
        pygame.draw.circle(s, halo, pos, BULLET_RADIUS + 3 + rad)
        pygame.draw.circle(s, body, pos, BULLET_RADIUS + 1 + rad)
        pygame.draw.circle(s, core, pos, max(1, BULLET_RADIUS - 1 + rad))

    def draw_ctf(self, s, snap):
        """Stands and flags, drawn under everything else on the field."""
        m = MAPS[snap["mi"] % len(MAPS)]
        now = time.time()

        for team, (bx, by) in enumerate(m["bases"]):
            col = TEAM_COLORS[team % 2]
            pygame.draw.circle(s, _fade(col, 0.30), (int(bx), int(by)),
                               BASE_RADIUS)
            pygame.draw.circle(s, col, (int(bx), int(by)), BASE_RADIUS, 2)
            label = self.f_small.render(TEAM_NAMES[team % 2], True,
                                        _fade(col, 0.8))
            s.blit(label, label.get_rect(center=(int(bx),
                                                 int(by) + BASE_RADIUS + 12)))

        for team, fx, fy, carrier, state in snap.get("fl", []):
            col = TEAM_COLORS[team % 2]
            # A dropped flag pulses, because finding it is the whole problem.
            if state == FLAG_DROPPED:
                r = BASE_RADIUS * 0.6 + math.sin(now * 5) * 4
                pygame.draw.circle(s, _fade(col, 0.55), (int(fx), int(fy)),
                                   int(r), 2)
            top = (fx, fy - FLAG_RADIUS - 6)
            pygame.draw.line(s, (232, 236, 246), top,
                             (fx, fy + FLAG_RADIUS), 2)
            pennant = [top, (fx + 18, fy - FLAG_RADIUS + 1),
                       (fx, fy - FLAG_RADIUS + 10)]
            pygame.draw.polygon(s, col, pennant)
            pygame.draw.polygon(s, (250, 250, 255), pennant, 1)
            if state == FLAG_CARRIED:
                pygame.draw.circle(s, col, (int(fx), int(fy)),
                                   int(PLAYER_RADIUS + 10 + math.sin(now * 8)
                                       * 1.5), 1)

    def draw_box(self, s, x, y, kind):
        col = POWERUP_COLOR.get(kind, (255, 255, 255))
        icon = self.icons.get(kind)
        x, y = int(x), int(y)

        if icon is None:
            # No art yet: the old spinning diamond with the label's initial.
            # Several labels share a first letter, so this is a placeholder.
            a = time.time() * 2.0
            pts = [(x + math.cos(a + i * math.pi / 2) * BOX_RADIUS,
                    y + math.sin(a + i * math.pi / 2) * BOX_RADIUS)
                   for i in range(4)]
            pygame.draw.polygon(s, col, pts)
            pygame.draw.polygon(s, (255, 255, 255), pts, 2)
            letter = self.f_small.render(POWERUP_LABEL[kind][0], True,
                                         (20, 20, 26))
            s.blit(letter, letter.get_rect(center=(x, y)))
            return

        # With art, the crate becomes a dark plate ringed in the powerup's
        # colour. Filling it with that colour instead would wash out any icon
        # drawn in a similar hue -- an icy icon on an icy crate is invisible --
        # and a square holds a 16x16 sprite that a spinning diamond clips.
        r = ICON_SIZE // 2 + 4
        pulse = 1 + 0.5 * math.sin(time.time() * 3.0)
        plate = pygame.Rect(x - r, y - r, r * 2, r * 2)
        glow = pygame.Surface((r * 2 + 8, r * 2 + 8), pygame.SRCALPHA)
        pygame.draw.rect(glow, col + (int(40 + 30 * pulse),),
                         glow.get_rect(), border_radius=6)
        s.blit(glow, (x - r - 4, y - r - 4))
        pygame.draw.rect(s, (16, 18, 26), plate, border_radius=4)
        pygame.draw.rect(s, col, plate, 2, border_radius=4)
        # The icon never rotates: a spinning glyph is unreadable at 20px, and
        # reading it at a glance is the whole job.
        s.blit(icon, icon.get_rect(center=(x, y)))

    def draw_player(self, s, row):
        if not row[P_ALIVE] or row[P_WAIT]:
            return
        # The server blanks the coordinates of players hidden from us, so there
        # is nothing here to draw even if we wanted to.
        hidden = len(row) > P_HID and row[P_HID]
        if hidden:
            return

        info = self.net.roster.get(row[P_ID], {})
        col = PLAYER_COLORS[info.get("color", 0) % len(PLAYER_COLORS)]
        x, y, aim = row[P_X], row[P_Y], row[P_AIM]
        mine = row[P_ID] == self.net.my_id
        bits = row[P_BITS]
        now = time.time()

        boss = row[P_BOSS] if len(row) > P_BOSS else 0
        team = row[P_TEAM] if len(row) > P_TEAM else -1
        radius = BOSS_RADIUS if boss else PLAYER_RADIUS

        # In a team game the side matters more than the individual, so the body
        # takes the team colour and the player's own colour shrinks to a dot in
        # the middle. Nobody should have to read a name to know who to shoot.
        teamed = team in (0, 1)
        body = TEAM_COLORS[team] if teamed else col

        # Your own cloak: drawn faint so you can see it is running.
        if bits & POWERUP_BIT[P_INVIS]:
            col = _fade(col, 0.34)
            body = _fade(body, 0.34)

        if boss:
            # A slow ring of thorns, so the boss is unmistakable even in the
            # corner of your eye while you are backing away from it.
            spin = now * 1.2
            for i in range(9):
                a = spin + i * math.tau / 9
                pygame.draw.line(
                    s, BOSS_COLOR,
                    (x + math.cos(a) * (radius + 2),
                     y + math.sin(a) * (radius + 2)),
                    (x + math.cos(a) * (radius + 9),
                     y + math.sin(a) * (radius + 9)), 2)

        # Spawn immunity: a dashed ring, deliberately unlike the solid shield.
        if len(row) > P_IMMUNE and row[P_IMMUNE]:
            spin = now * 2.2
            for i in range(8):
                a0 = spin + i * math.tau / 8
                pygame.draw.arc(s, (235, 240, 255),
                                (int(x - radius - 6), int(y - radius - 6),
                                 int((radius + 6) * 2), int((radius + 6) * 2)),
                                a0, a0 + 0.30, 2)

        if bits & POWERUP_BIT[P_SHIELD]:
            r = radius + 7 + math.sin(now * 6) * 1.5
            pygame.draw.circle(s, POWERUP_COLOR[P_SHIELD], (int(x), int(y)),
                               int(r), 2)

        # Reflect reads as a spinning guard rather than a solid ring, so it is
        # never mistaken for a shield.
        if bits & POWERUP_BIT[P_REFLECT]:
            spin = now * 4.0
            for i in range(4):
                a0 = spin + i * math.pi / 2
                r = radius + 9
                pygame.draw.arc(
                    s, POWERUP_COLOR[P_REFLECT],
                    (int(x - r), int(y - r), int(r * 2), int(r * 2)),
                    a0, a0 + 0.55, 3)

        barrel = (POWERUP_COLOR[P_GOLDEN] if bits & POWERUP_BIT[P_GOLDEN]
                  else body)
        width = 7 if bits & POWERUP_BIT[P_GOLDEN] else 5
        pygame.draw.line(s, barrel, (x, y),
                         (x + math.cos(aim) * (radius + 12),
                          y + math.sin(aim) * (radius + 12)), width)
        pygame.draw.circle(s, body, (int(x), int(y)), radius)
        if teamed:
            pygame.draw.circle(s, col, (int(x), int(y)), max(4, radius // 3))
        pygame.draw.circle(s, (255, 255, 255) if mine else (22, 24, 32),
                           (int(x), int(y)), radius, 2 if mine else 1)

        name_y = int(y) - radius - 12
        if boss and len(row) > P_HPMAX and row[P_HPMAX] > 1:
            self.draw_hp_bar(s, x, name_y - 2, row[P_HP], row[P_HPMAX])
            name_y -= 20

        label = self.f_small.render(info.get("name", "?"), True,
                                    TEXT if mine else TEXT_DIM)
        s.blit(label, label.get_rect(center=(int(x), name_y)))

    def draw_hp_bar(self, s, x, y, hp, hp_max):
        """Segmented bar over the boss -- the only health in the game."""
        seg = max(4, min(12, int(96 / max(1, hp_max))))
        gap = 2
        total = hp_max * seg + (hp_max - 1) * gap
        left = int(x - total / 2)
        for i in range(hp_max):
            rect = (left + i * (seg + gap), int(y) - 6, seg, 6)
            pygame.draw.rect(s, BOSS_COLOR if i < hp else (52, 40, 44), rect)

    def draw_power_timers(self, s, snap):
        """Every live powerup in the arena, top right, each draining to empty.

        Deliberately not tucked into your own corner of the HUD: knowing how
        much longer somebody else's time stop or golden gun has to run is
        exactly what the rest of the room needs to see. Returns the y the kill
        feed should start at, so the two never overlap.
        """
        rows = []
        for r in snap["p"]:
            left = r[P_PLEFT] if len(r) > P_PLEFT else {}
            if not left or r[P_WAIT] or not r[P_ALIVE]:
                continue
            who = self.net.roster.get(r[P_ID], {}).get("name", "?")[:10]
            for name in POWERUPS:
                frac = left.get(name)
                if frac:
                    rows.append((who, name, max(0.0, min(1.0, float(frac)))))
        if not rows:
            return 26

        rows = rows[:6]
        x = ARENA_W - POWER_PANEL_W - 26
        h = len(rows) * 26 + 12
        plate = pygame.Surface((POWER_PANEL_W, h), pygame.SRCALPHA)
        plate.fill((18, 20, 30, 170))
        s.blit(plate, (x, 26))

        y = 34
        bar_w = POWER_PANEL_W - 16
        for who, name, frac in rows:
            col = POWERUP_COLOR[name]
            _text(s, self.f_small, f"{who} · {POWERUP_LABEL[name]}",
                  (x + 8, y), col)
            pygame.draw.rect(s, (44, 48, 64), (x + 8, y + 16, bar_w, 4))
            pygame.draw.rect(s, col,
                             (x + 8, y + 16, max(1, int(bar_w * frac)), 4))
            y += 26
        return 26 + h + 8

    def draw_feed(self, s, top=26):
        y = top
        for surf, _exp in self.feed:
            s.blit(surf, (ARENA_W - surf.get_width() - 26, y))
            y += 22

    def draw_chat(self, s):
        """Recent messages bottom-left, plus the input box while typing.

        Everything here is drawn on translucent plates and expires on its own,
        so a conversation never hides the corner of the arena you are fighting
        in for long.
        """
        now = time.time()
        self.chat = [c for c in self.chat if c[2] > now]

        line_h = 21
        box_h = 32
        bottom = ARENA_H - 14 - (box_h + 6 if self.typing == "chat" else 0)

        for i, (name_surf, text_surf, expiry) in enumerate(reversed(self.chat)):
            # Hold full opacity, then fade over the last stretch of the life.
            alpha = int(255 * min(1.0, (expiry - now) / 1.2))
            y = bottom - line_h * (i + 1)
            width = name_surf.get_width() + text_surf.get_width() + 20
            plate = pygame.Surface((width, line_h), pygame.SRCALPHA)
            plate.fill((10, 12, 18, alpha * 100 // 255))
            s.blit(plate, (16, y))
            name_surf.set_alpha(alpha)
            text_surf.set_alpha(alpha)
            s.blit(name_surf, (24, y + 3))
            s.blit(text_surf, (24 + name_surf.get_width() + 8, y + 3))

        if self.typing == "chat":
            top = ARENA_H - 14 - box_h
            box = pygame.Surface((ARENA_W - 32, box_h), pygame.SRCALPHA)
            box.fill((10, 12, 18, 165))
            s.blit(box, (16, top))
            pygame.draw.rect(s, (70, 78, 104), (16, top, ARENA_W - 32, box_h), 1)
            caret = "|" if int(now * 2) % 2 == 0 else " "
            _text(s, self.f_mid, f"say:  {self.typed}{caret}", (26, top + 6), TEXT)
            _text(s, self.f_small, "[Enter] send   [Esc] cancel",
                  (ARENA_W - 210, top + 10), TEXT_DIM)

    def draw_overlay(self, s, snap):
        phase = snap["ph"]
        me = self.find_me()
        centre = (ARENA_W // 2, ARENA_H // 2 - 40)

        if phase == PHASE_COUNTDOWN:
            n = max(1, math.ceil(snap["pt"]))
            _centered(s, self.f_huge, str(n), centre, TEXT)
            _centered(s, self.f_big, self.banner,
                      (centre[0], centre[1] + 80), TEXT_DIM)
            if self.sub_banner:
                _centered(s, self.f_big, self.sub_banner,
                          (centre[0], centre[1] + 124), BOSS_COLOR)

        elif phase == PHASE_OVER:
            col = TEXT
            if "WINS" in self.round_result or "SURVIVED" in self.round_result:
                col = (255, 226, 150)
            elif self.round_result == "BOSS DOWN":
                col = BOSS_COLOR
            _centered(s, self.f_huge, self.round_result or "ROUND OVER",
                      centre, col)
            _centered(s, self.f_mid, "next map loading...",
                      (centre[0], centre[1] + 76), TEXT_DIM)

        elif phase == PHASE_WAITING:
            _centered(s, self.f_big, "WAITING FOR ANOTHER PLAYER",
                      centre, TEXT_DIM)
            _centered(s, self.f_mid, "free roam -- shoot things, respawn instantly",
                      (centre[0], centre[1] + 50), TEXT_DIM)

        elif phase == PHASE_LIVE and me is not None:
            ctf = snap.get("gm") == MODE_CTF
            if me[P_WAIT]:
                _centered(s, self.f_big, "SPECTATING -- you join next round",
                          centre, TEXT_DIM)
            elif not me[P_ALIVE] and ctf:
                left = me[P_RESP] if len(me) > P_RESP else 0
                _centered(s, self.f_big, "DOWN", centre, (235, 120, 130))
                _centered(s, self.f_mid, f"back in {max(1, math.ceil(left))}s",
                          (centre[0], centre[1] + 50), TEXT_DIM)
            elif not me[P_ALIVE]:
                _centered(s, self.f_big, "ELIMINATED", centre, (235, 120, 130))
            elif snap.get("bs") and me[P_BOSS]:
                _centered(s, self.f_mid, "YOU ARE THE BOSS -- everyone else "
                          "is hunting you", (centre[0], ARENA_H - 74),
                          BOSS_COLOR)

            elif ctf:
                # Say it plainly, or the sudden loss of speed reads as lag.
                mine = next((f for f in snap.get("fl", [])
                             if f[3] == self.net.my_id), None)
                if mine:
                    _centered(s, self.f_mid,
                              f"YOU HAVE THE {TEAM_NAMES[mine[0] % 2]} FLAG "
                              f"-- heavy, get it home",
                              (centre[0], ARENA_H - 74),
                              TEAM_COLORS[mine[0] % 2])

    def draw_hud(self, c, snap):
        pygame.draw.rect(c, HUD_BG, (0, ARENA_H, WINDOW_W, HUD_H))
        pygame.draw.line(c, (40, 44, 60), (0, ARENA_H), (WINDOW_W, ARENA_H))
        top = ARENA_H + 10
        me = self.find_me()
        ctf = snap.get("gm") == MODE_CTF

        # --- left block: you, your magazine, your powerups
        col = PLAYER_COLORS[self.net.my_color % len(PLAYER_COLORS)]
        _text(c, self.f_mid, self.name, (20, top), col)
        if me is not None and me[P_BOSS]:
            _text(c, self.f_mid, "BOSS", (20 + self.f_mid.size(self.name)[0] + 14,
                                          top), BOSS_COLOR)
        elif ctf and self.net.my_team in (0, 1):
            team = self.net.my_team
            _text(c, self.f_mid, TEAM_NAMES[team],
                  (20 + self.f_mid.size(self.name)[0] + 14, top),
                  TEAM_COLORS[team])

        if me:
            if me[P_RELOAD] > 0:
                width = int(120 * (1.0 - me[P_RELOAD]))
                pygame.draw.rect(c, (44, 48, 64), (20, top + 30, 120, 12))
                pygame.draw.rect(c, (255, 199, 119), (20, top + 30, width, 12))
                _text(c, self.f_small, "reloading", (150, top + 28), TEXT_DIM)
            elif me[P_BITS] & POWERUP_BIT[P_GOLDEN]:
                # The golden gun holds one round, so show one wide pip rather
                # than six mostly-empty ones.
                gold = POWERUP_COLOR[P_GOLDEN]
                pygame.draw.rect(c, gold if me[P_AMMO] > 0 else (44, 48, 64),
                                 (20, top + 30, 74, 12))
                _text(c, self.f_small, "golden round", (102, top + 28), gold)
            else:
                # The boss carries a much deeper magazine, so the pips get
                # thinner rather than the row getting longer.
                mag = BOSS_MAG if me[P_BOSS] else MAG_SIZE
                step = 20 if mag <= 6 else max(7, int(120 / mag))
                for i in range(mag):
                    filled = i < me[P_AMMO]
                    rect = (20 + i * step, top + 30, max(4, step - 6), 12)
                    pygame.draw.rect(c, col if filled else (44, 48, 64), rect)
                _text(c, self.f_small, "[R] reload",
                      (max(150, 28 + mag * step), top + 28), TEXT_DIM)

            x = 20
            for name in POWERUPS:
                if not me[P_BITS] & POWERUP_BIT[name]:
                    continue
                label = self.f_small.render(POWERUP_LABEL[name], True,
                                            POWERUP_COLOR[name])
                if x + label.get_width() > 400:
                    break  # keep clear of the round status in the middle
                pygame.draw.rect(c, (30, 33, 46),
                                 (x - 4, top + 52, label.get_width() + 8, 20),
                                 border_radius=3)
                c.blit(label, (x, top + 55))
                x += label.get_width() + 16

        # --- middle block: what the round is doing
        phase = snap["ph"]
        m = MAPS[snap["mi"] % len(MAPS)]
        alive = sum(1 for r in snap["p"] if r[P_ALIVE] and not r[P_WAIT])
        if phase == PHASE_LIVE and ctf:
            a, b = snap.get("sc", [0, 0])
            status = f"{TEAM_NAMES[0]} {a}  ·  {b} {TEAM_NAMES[1]}"
            sub = f"first to {CTF_CAPTURES_TO_WIN} · {int(snap['pt'])}s left"
        elif phase == PHASE_LIVE and snap.get("bs"):
            boss = next((r for r in snap["p"] if r[P_ID] == snap["bs"]), None)
            who = self.net.roster.get(snap["bs"], {}).get("name", "?")
            hp = f" · {boss[P_HP]}/{boss[P_HPMAX]} hp" if boss else ""
            status = f"BOSS ROUND · {who}{hp}"
            sub = f"{alive - 1} hunters left · {int(snap['pt'])}s"
        elif phase == PHASE_LIVE:
            status = f"ROUND {snap['rd']} · {m['name']} · {alive} alive"
            sub = f"{int(snap['pt'])}s left"
        elif phase == PHASE_COUNTDOWN:
            status = f"ROUND {snap['rd']} · {m['name']}"
            sub = "get ready"
        elif phase == PHASE_OVER:
            status = self.round_result or "round over"
            sub = "next round shortly"
        else:
            status = "waiting for players"
            sub = m["name"]
        status_col = TEXT
        if phase == PHASE_LIVE and snap.get("bs"):
            status_col = BOSS_COLOR
        mid_x = WINDOW_W // 2 - 90
        _centered(c, self.f_mid, status, (mid_x, top + 12), status_col)
        _centered(c, self.f_small, sub, (mid_x, top + 38), TEXT_DIM)
        _centered(c, self.f_small,
                  f"{int(self.net.ping_ms)}ms   [T] chat   [M] mode   "
                  f"[Esc] leave   [F1/F2] map   [F11] fullscreen",
                  (mid_x, top + 62), TEXT_DIM)


        # --- right block: scoreboard, best round-wins first
        if ctf:
            rows = sorted(snap["p"], key=lambda r: (r[P_TEAM], -r[P_KILLS]))
        else:
            rows = sorted(snap["p"], key=lambda r: (-r[P_WINS], -r[P_KILLS]))
        board_x = WINDOW_W - 274
        _text(c, self.f_small, "WINS  K/D", (board_x, top - 4), TEXT_DIM)
        for i, r in enumerate(rows[:8]):
            info = self.net.roster.get(r[P_ID], {})
            rc = PLAYER_COLORS[info.get("color", 0) % len(PLAYER_COLORS)]
            if r[P_BOSS]:
                rc = BOSS_COLOR
            elif ctf and r[P_TEAM] in (0, 1):
                rc = TEAM_COLORS[r[P_TEAM]]
            cx = board_x + (i // 4) * 134
            cy = top + 14 + (i % 4) * 18
            dim = not r[P_ALIVE] and phase == PHASE_LIVE
            pygame.draw.rect(c, _fade(rc, 0.4 if dim else 1.0), (cx, cy + 3, 8, 8))
            _text(c, self.f_small, info.get("name", "?")[:9], (cx + 14, cy),
                  TEXT_DIM if dim else TEXT)
            _text(c, self.f_small, f"{r[P_WINS]}  {r[P_KILLS]}/{r[P_DEATHS]}",
                  (cx + 84, cy), TEXT_DIM)


def _text(surf, font, msg, pos, color):
    surf.blit(font.render(msg, True, color), pos)


def _centered(surf, font, msg, centre, color):
    img = font.render(msg, True, color)
    surf.blit(img, img.get_rect(center=centre))


def main():
    ap = argparse.ArgumentParser(description="Play sacmagame on your LAN.")
    ap.add_argument("--name", default=socket.gethostname()[:14],
                    help="Name other players see.")
    ap.add_argument("--host", default=None,
                    help="Skip the menu and connect straight to this address.")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()

    host, port = args.host, args.port
    if host and ":" in host:
        host, _, raw = host.partition(":")
        try:
            port = int(raw)
        except ValueError:
            pass

    try:
        App(args.name, host, port).run()
    except KeyboardInterrupt:
        pygame.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
