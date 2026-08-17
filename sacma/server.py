"""Authoritative LAN server.

Runs the simulation at a fixed tick and pushes a snapshot to every client each
step. Messages are newline-delimited JSON over TCP -- on a LAN the latency is a
millisecond or two, so TCP's ordering guarantees cost nothing and save us
writing a reliability layer.

Also broadcasts a small UDP beacon once a second so clients can list nearby
games instead of asking everyone for an IP address.

    python -m sacma.server --name "Office FFA"
"""

import argparse
import asyncio
import ipaddress
import json
import socket
import sys
import time

from .game import Game
from .shared import (
    CHAT_MAX_LEN, CHAT_MIN_GAP, DEFAULT_PORT, DISCOVERY_MAGIC, DISCOVERY_PORT,
    MODE_CHANGE_GAP, MODE_CTF, MODE_FFA, MODE_LABEL, MODES, TICK_RATE,
)


def _encode(obj):
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode()


class Conn:
    """One connected client: an outbound queue plus its player id."""

    def __init__(self, writer, peer):
        self.writer = writer
        self.peer = peer
        self.pid = None
        self.name = "?"
        self.queue = asyncio.Queue(maxsize=120)
        self.open = True
        self.last_chat = 0.0
        self.last_mode = 0.0

    def send(self, obj):
        if not self.open:
            return
        if self.queue.full():
            # A client this far behind wants the newest state, not the oldest.
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self.queue.put_nowait(_encode(obj))
        except asyncio.QueueFull:
            pass


class Server:
    def __init__(self, port, name, debug=True, announce=(), mode=MODE_FFA,
                 mode_vote=True):
        self.port = port
        self.name = name
        self.debug = debug
        self.mode_vote = mode_vote
        self.announce = tuple(announce)
        self.game = Game(mode)
        self.conns = set()

    # -- connection handling --------------------------------------------------

    async def handle(self, reader, writer):
        peer = writer.get_extra_info("peername")
        try:
            writer.get_extra_info("socket").setsockopt(
                socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

        conn = Conn(writer, peer)
        self.conns.add(conn)
        pump = asyncio.create_task(self._pump(conn))

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                self._on_message(conn, msg)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            conn.open = False
            pump.cancel()
            self.conns.discard(conn)
            if conn.pid is not None:
                self.game.remove_player(conn.pid)
                self._broadcast_roster()
                print(f"-- {conn.name} left ({len(self.game.players)} playing)")
            writer.close()

    async def _pump(self, conn):
        """Drain a client's queue, coalescing whatever piled up into one write."""
        try:
            while conn.open:
                chunk = await conn.queue.get()
                while not conn.queue.empty():
                    chunk += conn.queue.get_nowait()
                conn.writer.write(chunk)
                await conn.writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception:
            pass

    def _on_message(self, conn, msg):
        kind = msg.get("t")

        if kind == "join":
            if conn.pid is not None:
                return
            if self.game.free_slots() <= 0:
                full = ("Both teams are full -- capture the flag is 2v2."
                        if self.game.ctf else "Server is full.")
                conn.send({"t": "error", "msg": full})
                conn.open = False
                return
            name = str(msg.get("name", "player")).strip()[:14] or "player"
            player = self.game.add_player(name)
            conn.pid = player.pid
            conn.name = name
            conn.send({
                "t": "welcome",
                "id": player.pid,
                "color": player.color,
                "team": player.team,
                "mode": self.game.mode,
                "tick_rate": TICK_RATE,
                "server": self.name,
            })
            self._broadcast_roster()
            print(f"++ {name} joined from {conn.peer[0]} "
                  f"({len(self.game.players)} playing)")

        elif kind == "input" and conn.pid is not None:
            self.game.set_input(conn.pid, msg)

        elif kind == "chat" and conn.pid is not None:
            now = time.monotonic()
            if now - conn.last_chat < CHAT_MIN_GAP:
                return  # someone holding down enter; drop it silently
            conn.last_chat = now
            text = str(msg.get("text", ""))
            self.game.chat(conn.pid, text)
            print(f"<{conn.name}> {text[:CHAT_MAX_LEN]}")

        elif kind == "map" and conn.pid is not None and self.debug:
            self.game.jump_map(1 if msg.get("dir", 1) >= 0 else -1)
            print(f"~~ {conn.name} switched to {self.game.map['name']}")

        elif kind == "mode" and conn.pid is not None:
            if not self.mode_vote:
                # A notice, not an error: "error" is the fatal kind and would
                # drop them back to the server list over a refused keypress.
                conn.send({"t": "notice",
                           "msg": "the host fixed the mode on this server"})
                return
            now = time.monotonic()
            if now - conn.last_mode < MODE_CHANGE_GAP:
                return  # someone leaning on the key
            conn.last_mode = now
            try:
                want = int(msg.get("mode", -1))
            except (TypeError, ValueError):
                return
            result = self.game.request_mode(want, conn.name)
            if result == "ok":
                print(f"~~ {conn.name} queued {MODE_LABEL[want]} "
                      f"for the next round")

        elif kind == "ping":
            conn.send({"t": "pong", "ts": msg.get("ts")})

    # -- broadcast ------------------------------------------------------------

    def _broadcast(self, obj):
        for conn in self.conns:
            if conn.pid is not None:
                conn.send(obj)

    def _broadcast_roster(self):
        self._broadcast({"t": "roster", "players": self.game.roster()})

    # -- loops ----------------------------------------------------------------

    async def game_loop(self):
        dt = 1.0 / TICK_RATE
        next_at = time.perf_counter()
        while True:
            self.game.step(dt)

            if self.game.roster_dirty:
                # A mode change reshuffled the teams.
                self.game.roster_dirty = False
                self._broadcast_roster()

            if self.game.anyone_invisible():
                # Invisible players are filtered out server-side, so each
                # client needs its own copy. Only pay for that while it matters.
                for conn in self.conns:
                    if conn.pid is not None:
                        conn.send(self.game.snapshot(conn.pid))
            else:
                self._broadcast(self.game.snapshot())

            if self.game.events:
                self._broadcast({"t": "ev", "items": self.game.events})
                self.game.events = []

            next_at += dt
            delay = next_at - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                # Fell behind (suspended laptop, GC pause) -- resync rather than
                # trying to catch up with a burst of ticks.
                next_at = time.perf_counter()

    async def beacon(self):
        """Announce this game on the LAN once a second."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setblocking(False)
        while True:
            payload = json.dumps({
                "magic": DISCOVERY_MAGIC,
                "name": self.name,
                "port": self.port,
                "players": len(self.game.players),
                "max": self.game.max_players,
                "map": self.game.map["name"],
                "round": self.game.round_no,
                "mode": self.game.mode,
            }).encode()
            # 127.0.0.1 as well, so a client on this same machine always sees
            # the game even if the OS declines to loop the broadcast back.
            # --announce targets get the beacon by unicast: on an office
            # network the players are often on a different subnet (wifi vs
            # wired), and no router forwards a broadcast across that line.
            for host in ("255.255.255.255", "127.0.0.1") + self.announce:
                try:
                    sock.sendto(payload, (host, DISCOVERY_PORT))
                except OSError:
                    pass
            await asyncio.sleep(1.0)

    async def run(self):
        server = await asyncio.start_server(self.handle, "0.0.0.0", self.port)
        for ip in local_ips():
            print(f"   friends connect to:  {ip}:{self.port}")
        print(f"   (or just hit Scan in the client -- '{self.name}' is "
              f"broadcasting on this network)")
        if self.announce:
            print(f"   also announcing to {len(self.announce)} address(es) "
                  f"off this subnet")
        print()
        async with server:
            await asyncio.gather(server.serve_forever(),
                                 self.game_loop(), self.beacon())


def local_ips():
    """Best-effort list of this machine's LAN addresses, docker bridges last."""
    found = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))  # no packets sent; just picks a route
        found.append(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            ip = info[4][0]
            if ip not in found and not ip.startswith("127."):
                found.append(ip)
    except OSError:
        pass
    return found or ["127.0.0.1"]


def expand_targets(spec, limit=1024):
    """Turn an --announce string into a flat tuple of addresses to beacon to.

    Accepts plain IPs and CIDR subnets; a subnet becomes every host address in
    it, because the point of the flag is reaching players whose IPs you do not
    know. Capped so a fat-fingered /8 cannot turn the beacon into a flood.
    """
    targets = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "/" in part:
            try:
                net = ipaddress.ip_network(part, strict=False)
            except ValueError as exc:
                raise ValueError(f"--announce: {exc}")
            hosts = list(net.hosts()) or [net.network_address]
            if len(hosts) > limit:
                raise ValueError(f"--announce: {part} covers {len(hosts)} "
                                 f"addresses, over the {limit} cap -- use a "
                                 f"smaller subnet")
            targets.extend(str(h) for h in hosts)
        else:
            targets.append(part)
    # Dedupe, keeping the order the host typed.
    return tuple(dict.fromkeys(targets))


def main():
    ap = argparse.ArgumentParser(description="Host a sacmagame LAN match.")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--name", default=None,
                    help="Name shown in the client's server list.")
    ap.add_argument("--announce", default="",
                    help="Comma-separated addresses to beacon directly, for "
                         "players a broadcast cannot reach (different wifi "
                         "subnet, AP client isolation). Takes plain IPs and "
                         "whole subnets: --announce 10.166.120.0/24")
    ap.add_argument("--mode", default="ffa", choices=sorted(MODES),
                    help="Starting mode. ffa: last player standing, with the "
                         "occasional boss round. boss: every round is a boss "
                         "round. ctf: capture the flag, two teams of two. "
                         "Players can change it in-game with [M].")
    ap.add_argument("--no-mode-vote", action="store_true",
                    help="Stop players changing the mode with [M]. The --mode "
                         "you started with is then the only one.")
    ap.add_argument("--no-debug", action="store_true",
                    help="Stop clients cycling maps with [ and ]. Worth "
                         "setting once people are actually competing.")
    args = ap.parse_args()

    # Keep join/leave lines appearing live even when piped to a log file.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, OSError):
        pass

    name = args.name or f"{socket.gethostname()}'s game"
    debug = not args.no_debug
    mode = MODES[args.mode]
    try:
        announce = expand_targets(args.announce)
    except ValueError as exc:
        ap.error(str(exc))
    mode_vote = not args.no_mode_vote
    print(f"\n== sacmagame server '{name}' on port {args.port} ==")
    print(f"   mode: {MODE_LABEL[mode]}"
          + ("  (2v2, four players max)" if mode == MODE_CTF else "")
          + ("" if mode_vote else "  (fixed -- players cannot change it)"))
    if mode_vote:
        print("   any player can switch modes with [M]; it takes effect on "
              "the next round")
    if debug:
        print("   debug map cycling is ON -- any player can press [ or ]")
    try:
        asyncio.run(Server(args.port, name, debug, announce, mode,
                           mode_vote).run())
    except KeyboardInterrupt:
        print("\nserver stopped.")


if __name__ == "__main__":
    main()
