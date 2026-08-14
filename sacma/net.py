"""Client-side networking: one thread talking to the server, one listening for
LAN beacons. Both hand plain data to the pygame main loop, which never blocks.
"""

import json
import socket
import threading
import time
from collections import deque

from .shared import DISCOVERY_MAGIC, DISCOVERY_PORT


class NetClient(threading.Thread):
    """Blocking socket on its own thread; the render loop just reads fields.

    Attribute writes here are whole-object replacements, which are atomic under
    the GIL, so the main loop can read them without locking.
    """

    daemon = True

    def __init__(self, host, port, name):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.name = name

        self.sock = None
        self.connected = False
        self.error = None
        self.closing = False

        self.my_id = None
        self.my_color = 0
        self.server_name = ""
        self.state = None            # latest snapshot
        self.roster = {}             # pid -> {"name": str, "color": int}
        self.events = deque(maxlen=400)
        self.ping_ms = 0.0

    # -- outbound -------------------------------------------------------------

    def send(self, obj):
        sock = self.sock
        if not sock:
            return
        try:
            sock.sendall((json.dumps(obj, separators=(",", ":")) + "\n").encode())
        except OSError:
            pass

    def close(self):
        self.closing = True
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass

    # -- thread ---------------------------------------------------------------

    def run(self):
        try:
            self.sock = socket.create_connection((self.host, self.port),
                                                 timeout=5.0)
            self.sock.settimeout(None)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            self.error = f"Could not reach {self.host}:{self.port} -- {exc}"
            return

        self.send({"t": "join", "name": self.name})

        buf = b""
        try:
            while not self.closing:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line:
                        self._handle(line)
        except OSError:
            pass
        finally:
            if not self.closing and not self.error:
                self.error = "Disconnected from server."
            self.connected = False

    def _handle(self, line):
        try:
            msg = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return

        kind = msg.get("t")
        if kind == "s":
            self.state = msg
        elif kind == "welcome":
            self.my_id = msg.get("id")
            self.my_color = msg.get("color", 0)
            self.server_name = msg.get("server", "")
            self.connected = True
        elif kind == "roster":
            self.roster = {p["id"]: p for p in msg.get("players", [])}
        elif kind == "ev":
            for item in msg.get("items", []):
                self.events.append(item)
        elif kind == "pong":
            ts = msg.get("ts")
            if isinstance(ts, (int, float)):
                self.ping_ms = (time.perf_counter() - ts) * 1000.0
        elif kind == "error":
            self.error = msg.get("msg", "Server refused the connection.")
            self.closing = True


class Discovery(threading.Thread):
    """Listens for server beacons and keeps a list of games seen recently."""

    daemon = True
    STALE = 4.0  # seconds without a beacon before a game drops off the list

    def __init__(self):
        super().__init__(daemon=True)
        self._found = {}
        self._lock = threading.Lock()
        self.running = True
        self.error = None

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Lets several clients on one machine listen at once (Linux/macOS).
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        try:
            sock.bind(("", DISCOVERY_PORT))
        except OSError as exc:
            self.error = f"discovery unavailable ({exc})"
            return
        sock.settimeout(0.5)

        while self.running:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                continue
            if msg.get("magic") != DISCOVERY_MAGIC:
                continue
            key = (addr[0], int(msg.get("port", 0)))
            with self._lock:
                self._found[key] = {
                    "host": addr[0],
                    "port": key[1],
                    "name": msg.get("name", "game"),
                    "players": msg.get("players", 0),
                    "max": msg.get("max", 8),
                    "map": msg.get("map", "?"),
                    "round": msg.get("round", 0),
                    "seen": time.time(),
                }

    def servers(self):
        now = time.time()
        with self._lock:
            live = [s for s in self._found.values()
                    if now - s["seen"] < self.STALE]

        # The host's own machine hears its beacon on both the LAN address and
        # 127.0.0.1. Same game, so collapse them and keep the routable one --
        # that is the address worth showing.
        best = {}
        for s in live:
            key = (s["name"], s["port"])
            prev = best.get(key)
            if prev is None or (prev["host"].startswith("127.")
                                and not s["host"].startswith("127.")):
                best[key] = s
        return sorted(best.values(), key=lambda s: (s["name"], s["host"]))
