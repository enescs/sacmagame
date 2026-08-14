# sacmagame

A small top-down shooter for one office and one wifi network.

Last player standing wins the round. **One bullet kills anybody**, including you,
so it's mostly about angles, corners and not being where someone is looking.
Five maps rotate automatically, some of them with obstacles that move while you
are trying to hide behind them.

No accounts, no internet, no port forwarding. One person hosts, everyone else
picks the game off a list.

Runs on Linux, macOS and Windows, and they can all play in the same match.

## Setup (once per machine)

```bash
git clone https://github.com/enescs/sacmagame.git
cd sacmagame
./setup.sh          # Windows: setup.bat
```

That builds a local `.venv/` and installs pygame into it. Nothing is installed
system-wide. You need Python 3.10 or newer.

## Playing

One person hosts:

| | |
|---|---|
| Linux / macOS | `./host.sh --name "Office FFA"` |
| Windows | `host.bat --name "Office FFA"` |

Everyone else, on the same wifi:

| | |
|---|---|
| Linux / macOS | `./play.sh` |
| Windows | `play.bat` |

The client scans the network, lists whatever games it finds, and you hit Enter.
Nobody needs to know an IP address. If discovery is blocked on your network,
press `M` in the menu and type the host's address, or skip the menu entirely:

```bash
./play.sh --host 10.0.0.5 --name enes
```

The host can play too — just run the client alongside the server.

The `.sh` and `.bat` files are one-line wrappers, so you can always call Python
directly instead: `.venv/bin/python -m sacma.server` on Linux and macOS,
`.venv\Scripts\python -m sacma.client` on Windows.

## Controls

| | |
|---|---|
| `WASD` / arrows | move |
| mouse | aim |
| left click / `Space` | shoot (hold to keep firing) |
| `R` | reload |
| `T` / `Enter` | chat — type, `Enter` to send, `Esc` to cancel |
| `F1` / `F2` | debug: jump to the previous/next map (or `PgUp`/`PgDn`) |
| `Esc` | back to the menu |

Map jumping restarts the round, so host with `--no-debug` once people are
actually competing. Durations and fire rates are all constants at the top of
[shared.py](sacma/shared.py) if something feels off.

Six rounds in the magazine, 1.5s reload, and it auto-reloads when you run dry —
firing wildly is how you get caught reloading.

## Rounds

With two or more players connected the server cycles: 3s countdown, then the
round runs until one player is left (or 100s passes, which is a draw), then 4s
on the scoreboard, then the next map.

Get killed and you sit out the rest of the round and spectate — there are no
respawns mid-round. Join mid-round and you spectate until the next one starts.

Alone on a server you're in free roam: instant respawns, obstacles running, good
for getting a feel for the maps.

## Loot

Every ~9 seconds a crate drops, biased towards the middle of the map, so
grabbing one usually means giving up your cover. Touch it to pick it up.

| | |
|---|---|
| **Shield** | absorbs exactly one bullet, then pops — expires after 7s |
| **Inf ammo** | no reloading for 10s |
| **Rapid fire** | ~2.6x fire rate for 10s |
| **Hot rounds** | 1.7x bullet speed for 12s |
| **Sprint** | 1.45x move speed for 10s |
| **Scatter** | three bullets per shot for 10s |
| **Invisible** | 3s — nobody can see you, but your bullets still show |
| **Golden gun** | 5s — one round in the mag, slow reload, very fast, ignores shields |
| **Reflect** | 5s — parries incoming bullets straight back, and they become yours |
| **Homing** | 5s — your bullets curve into whoever is nearest |

Powerups are cleared when you die, so being loaded up isn't a guaranteed round.

A few interactions worth knowing:

- **Invisibility is enforced on the server** — your position is never sent to
  other clients, so it can't be defeated by fiddling with the game. Firing
  doesn't produce a muzzle flash while cloaked, but the bullets themselves are
  visible, so shooting still gives away roughly where you are.
- **Golden gun beats shield, reflect beats golden gun.** A parry sends the
  round back with the parrier as its owner, so a good parry scores the kill.
- **Golden gun overrides scatter** — it stays a single precise round.
- **Homing bullets ignore their own shooter** and only track within 460px, so
  breaking line of sight and distance are both real counters.

## Maps

| | |
|---|---|
| **Crossfire** | plain cover, nothing moves |
| **Millstone** | two long rotating bars, thin cover |
| **Pistons** | blocks sliding on fixed timings |
| **Carousel** | four rotors, hardest to hold an angle on |
| **Gridlock** | pillar grid, all corner-peeking |

Everything is laid out in rotationally symmetric pairs, so no spawn is better
than another. Maps are in [maps.py](sacma/maps.py) as plain tuples — walls,
sliding blocks and rotors — if you want to add one.

## If nobody can connect

- **Firewall.** The most common cause by far. On Linux:
  `sudo ufw allow 50500/tcp && sudo ufw allow 50505/udp`. Windows pops a dialog
  the first time you host — tick **Private networks** and allow it. If you
  dismissed it, the rule is under Windows Defender Firewall → Allow an app.
- **Two clients on one Windows box** may not both see the server list, because
  sharing the discovery port relies on `SO_REUSEPORT`, which Windows lacks.
  Start the second one with `play.bat --host 127.0.0.1` instead.
- **Guest wifi.** A lot of corporate and guest networks isolate clients from
  each other, so no amount of configuration will let two laptops talk. You need
  a normal network, or a phone hotspot.
- **Discovery finds nothing but you know the IP.** Press `M` and type it. The
  host prints its addresses on startup.

## How it works

- `sacma/game.py` — the entire simulation, no networking or rendering in it
- `sacma/server.py` — asyncio TCP server, 60 ticks/s, ships a snapshot per tick
- `sacma/client.py` — pygame; sends input, draws the last snapshot, predicts nothing
- `sacma/net.py` — the client's socket thread and the LAN beacon listener
- `sacma/maps.py`, `sacma/shared.py` — map data and the constants both sides agree on

The server is authoritative: clients send key states and an aim angle, and get
back positions. There's no client-side prediction because on a LAN the round
trip is 1–2 ms, well under a frame. Snapshots run about 40 KB/s per player.

Map geometry never goes over the wire — every client has `maps.py`, and moving
obstacles are a pure function of a map clock the server sends, so all screens
stay in lockstep.
