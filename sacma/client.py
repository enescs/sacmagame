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
import random
import socket
import sys
import time

import pygame

from .maps import MAPS, mover_rect, rotor_segment
from .net import Discovery, NetClient
from .shared import (
    ARENA_H, ARENA_W, BG, BORDER, BOX_RADIUS, BULLET_RADIUS, CHAT_MAX_LEN,
    CHAT_MAX_LINES, CHAT_SHOW_TIME, DEFAULT_PORT, GRID,
    HAZARD_EDGE, HAZARD_FILL, HUD_BG, HUD_H, MAG_SIZE, PHASE_COUNTDOWN,
    PHASE_LIVE, PHASE_OVER, PHASE_WAITING, PLAYER_COLORS, PLAYER_RADIUS,
    POWERUPS, POWERUP_BIT, POWERUP_COLOR, POWERUP_LABEL, P_SHIELD, TEXT,
    TEXT_DIM, WALL_EDGE, WALL_FILL, WINDOW_H, WINDOW_W,
)

# Snapshot player-row indices, mirroring Game.snapshot().
P_ID, P_X, P_Y, P_AIM, P_ALIVE, P_AMMO, P_RELOAD, P_BITS = 0, 1, 2, 3, 4, 5, 6, 7
P_WINS, P_KILLS, P_DEATHS, P_WAIT = 8, 9, 10, 11

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


class App:
    def __init__(self, name, host, port):
        pygame.init()
        pygame.display.set_caption("sacmagame")

        info = pygame.display.Info()
        self.scale = min(1.0,
                         (info.current_w - 40) / WINDOW_W,
                         (info.current_h - 100) / WINDOW_H)
        self.win_size = (int(WINDOW_W * self.scale), int(WINDOW_H * self.scale))
        self.screen = pygame.display.set_mode(self.win_size)
        self.canvas = pygame.Surface((WINDOW_W, WINDOW_H))
        self.clock = pygame.time.Clock()

        self.f_small = pygame.font.Font(None, 20)
        self.f_mid = pygame.font.Font(None, 26)
        self.f_big = pygame.font.Font(None, 44)
        self.f_huge = pygame.font.Font(None, 92)

        self.name = name
        self.net = None
        self.fx = Effects()
        self.feed = []          # [(surface, expiry)]
        self.chat = []          # [[name surface, text surface, expiry]]
        self.banner = ""
        self.round_result = ""

        self.discovery = Discovery()
        self.discovery.start()
        self.sel = 0
        self.typing = None      # None | "name" | "host" | "chat"
        self.typed = ""
        self.status = ""

        self.screen_state = "menu"
        if host:
            self.connect(host, port)

        self.port = port
        self.last_ping = 0.0

    # -- connection -----------------------------------------------------------

    def connect(self, host, port):
        self.net = NetClient(host, port, self.name)
        self.net.start()
        self.screen_state = "play"
        self.feed.clear()
        self.chat.clear()
        self.banner = ""
        self.round_result = ""

    def disconnect(self):
        if self.net:
            self.net.close()
            self.net = None
        if self.typing == "chat":
            self.typing = None
        self.screen_state = "menu"

    # -- main loop ------------------------------------------------------------

    def run(self):
        running = True
        while running:
            dt = min(self.clock.tick(60) / 1000.0, 0.05)
            running = self.handle_events()
            self.fx.update(dt)

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

            if self.scale == 1.0:
                self.screen.blit(self.canvas, (0, 0))
            else:
                pygame.transform.smoothscale(self.canvas, self.win_size,
                                             self.screen)
            pygame.display.flip()

        self.disconnect()
        self.discovery.running = False
        pygame.quit()

    def mouse_canvas(self):
        mx, my = pygame.mouse.get_pos()
        return mx / self.scale, my / self.scale

    def handle_events(self):
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return False
            if ev.type != pygame.KEYDOWN:
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

            if ev.key == pygame.K_ESCAPE:
                if self.screen_state == "play":
                    self.status = ""
                    self.disconnect()
                else:
                    return False

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

        # While the chat box is open the keyboard belongs to it -- keep aiming
        # (the mouse is still yours) but stop moving, shooting and reloading.
        chatting = self.typing == "chat"

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

            elif kind == "kill":
                col = PLAYER_COLORS[ev["victim_color"] % len(PLAYER_COLORS)]
                self.fx.burst(ev["x"], ev["y"], col, count=18, speed=260,
                              life=0.6)
                self.fx.ring(ev["x"], ev["y"], col, r0=8, r1=54, life=0.45)
                self.push_feed(f"{ev['killer']}  ->  {ev['victim']}",
                               PLAYER_COLORS[ev["killer_color"]
                                             % len(PLAYER_COLORS)])

            elif kind == "shieldbreak":
                self.fx.ring(ev["x"], ev["y"], POWERUP_COLOR[P_SHIELD],
                             r0=10, r1=46, life=0.35, width=4)

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
                self.push_feed(f"round {ev['round']} -- {ev['name']} "
                               f"({ev['blurb']})", TEXT_DIM)

            elif kind == "win":
                col = PLAYER_COLORS[ev["color"] % len(PLAYER_COLORS)]
                self.round_result = f"{ev['name']} WINS"
                self.push_feed(f"{ev['name']} takes the round", col)

            elif kind == "draw":
                self.round_result = "DRAW"

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
                    pygame.draw.rect(c, (34, 38, 52), (56, row_y - 6, 900, 40),
                                     border_radius=4)
                col = TEXT if selected else TEXT_DIM
                _text(c, self.f_mid,
                      f"{'>' if selected else ' '}  {s['name']}", (64, row_y), col)
                _text(c, self.f_small,
                      f"{s['host']}:{s['port']}", (470, row_y + 4), TEXT_DIM)
                _text(c, self.f_small,
                      f"{s['players']}/{s['max']} players", (660, row_y + 4),
                      TEXT_DIM)
                _text(c, self.f_small, f"map: {s['map']}", (800, row_y + 4),
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
        self.draw_feed(arena)
        self.draw_overlay(arena, snap)
        self.draw_chat(arena)
        self.draw_hud(c, snap)

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

        for bid, bx, by, kind in snap["l"]:
            self.draw_box(s, bx, by, kind)

        for bx, by, _owner in snap["b"]:
            pygame.draw.circle(s, (255, 240, 190), (int(bx), int(by)),
                               BULLET_RADIUS + 2)
            pygame.draw.circle(s, (255, 255, 255), (int(bx), int(by)),
                               BULLET_RADIUS - 1)

        for row in snap["p"]:
            self.draw_player(s, row)

    def draw_box(self, s, x, y, kind):
        col = POWERUP_COLOR.get(kind, (255, 255, 255))
        a = time.time() * 2.0
        pts = [(x + math.cos(a + i * math.pi / 2) * BOX_RADIUS,
                y + math.sin(a + i * math.pi / 2) * BOX_RADIUS)
               for i in range(4)]
        pygame.draw.polygon(s, col, pts)
        pygame.draw.polygon(s, (255, 255, 255), pts, 2)
        letter = self.f_small.render(POWERUP_LABEL[kind][0], True, (20, 20, 26))
        s.blit(letter, letter.get_rect(center=(int(x), int(y))))

    def draw_player(self, s, row):
        if not row[P_ALIVE] or row[P_WAIT]:
            return

        info = self.net.roster.get(row[P_ID], {})
        col = PLAYER_COLORS[info.get("color", 0) % len(PLAYER_COLORS)]
        x, y, aim = row[P_X], row[P_Y], row[P_AIM]
        mine = row[P_ID] == self.net.my_id

        if row[P_BITS] & POWERUP_BIT[P_SHIELD]:
            r = PLAYER_RADIUS + 7 + math.sin(time.time() * 6) * 1.5
            pygame.draw.circle(s, POWERUP_COLOR[P_SHIELD], (int(x), int(y)),
                               int(r), 2)

        pygame.draw.line(s, col, (x, y),
                         (x + math.cos(aim) * (PLAYER_RADIUS + 10),
                          y + math.sin(aim) * (PLAYER_RADIUS + 10)), 5)
        pygame.draw.circle(s, col, (int(x), int(y)), PLAYER_RADIUS)
        pygame.draw.circle(s, (255, 255, 255) if mine else (22, 24, 32),
                           (int(x), int(y)), PLAYER_RADIUS, 2 if mine else 1)

        label = self.f_small.render(info.get("name", "?"), True,
                                    TEXT if mine else TEXT_DIM)
        s.blit(label, label.get_rect(center=(int(x), int(y) - PLAYER_RADIUS - 12)))

    def draw_feed(self, s):
        y = 26
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

        elif phase == PHASE_OVER:
            col = TEXT
            if self.round_result.endswith("WINS"):
                col = (255, 226, 150)
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
            if me[P_WAIT]:
                _centered(s, self.f_big, "SPECTATING -- you join next round",
                          centre, TEXT_DIM)
            elif not me[P_ALIVE]:
                _centered(s, self.f_big, "ELIMINATED", centre, (235, 120, 130))

    def draw_hud(self, c, snap):
        pygame.draw.rect(c, HUD_BG, (0, ARENA_H, WINDOW_W, HUD_H))
        pygame.draw.line(c, (40, 44, 60), (0, ARENA_H), (WINDOW_W, ARENA_H))
        top = ARENA_H + 10
        me = self.find_me()

        # --- left block: you, your magazine, your powerups
        col = PLAYER_COLORS[self.net.my_color % len(PLAYER_COLORS)]
        _text(c, self.f_mid, self.name, (20, top), col)

        if me:
            if me[P_RELOAD] > 0:
                width = int(120 * (1.0 - me[P_RELOAD]))
                pygame.draw.rect(c, (44, 48, 64), (20, top + 30, 120, 12))
                pygame.draw.rect(c, (255, 199, 119), (20, top + 30, width, 12))
                _text(c, self.f_small, "reloading", (150, top + 28), TEXT_DIM)
            else:
                for i in range(MAG_SIZE):
                    filled = i < me[P_AMMO]
                    rect = (20 + i * 20, top + 30, 14, 12)
                    pygame.draw.rect(c, col if filled else (44, 48, 64), rect)
                _text(c, self.f_small, "[R] reload", (150, top + 28), TEXT_DIM)

            x = 20
            for name in POWERUPS:
                if not me[P_BITS] & POWERUP_BIT[name]:
                    continue
                label = self.f_small.render(POWERUP_LABEL[name], True,
                                            POWERUP_COLOR[name])
                pygame.draw.rect(c, (30, 33, 46),
                                 (x - 4, top + 52, label.get_width() + 8, 20),
                                 border_radius=3)
                c.blit(label, (x, top + 55))
                x += label.get_width() + 16

        # --- middle block: what the round is doing
        phase = snap["ph"]
        m = MAPS[snap["mi"] % len(MAPS)]
        alive = sum(1 for r in snap["p"] if r[P_ALIVE] and not r[P_WAIT])
        if phase == PHASE_LIVE:
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
        mid_x = WINDOW_W // 2 - 90
        _centered(c, self.f_mid, status, (mid_x, top + 12), TEXT)
        _centered(c, self.f_small, sub, (mid_x, top + 38), TEXT_DIM)
        _centered(c, self.f_small,
                  f"{int(self.net.ping_ms)}ms   [T] chat   [Esc] leave   "
                  f"[F1/F2] map",
                  (mid_x, top + 62), TEXT_DIM)

        # --- right block: scoreboard, best round-wins first
        rows = sorted(snap["p"], key=lambda r: (-r[P_WINS], -r[P_KILLS]))
        board_x = WINDOW_W - 274
        _text(c, self.f_small, "WINS  K/D", (board_x, top - 4), TEXT_DIM)
        for i, r in enumerate(rows[:8]):
            info = self.net.roster.get(r[P_ID], {})
            rc = PLAYER_COLORS[info.get("color", 0) % len(PLAYER_COLORS)]
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
