# sacmagame

A small top-down shooter for one office and one wifi network.

Last player standing wins the round. **One bullet kills anybody**, including you,
so it's mostly about angles, corners and not being where someone is looking.
Five maps rotate automatically, some of them with obstacles that move while you
are trying to hide behind them.

No accounts, no internet, no port forwarding. One person hosts, everyone else
picks the game off a list.

## Setup (once per machine)

```bash
git clone <this repo>
cd sacmagame
./setup.sh
```

That builds a local `.venv/` and installs pygame into it. Nothing is installed
system-wide.

On Windows, without the shell scripts:

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

## Playing

One person hosts:

```bash
./host.sh --name "Office FFA"
```

Everyone else, on the same wifi:

```bash
./play.sh
```

The client scans the network, lists whatever games it finds, and you hit Enter.
Nobody needs to know an IP address. If discovery is blocked on your network,
press `M` in the menu and type the host's address, or skip the menu entirely:

```bash
./play.sh --host 10.0.0.5 --name enes
```

The host can play too — just run `./play.sh` alongside `./host.sh`.

Windows equivalents: `.venv\Scripts\python -m sacma.server` and
`.venv\Scripts\python -m sacma.client`.

## Controls

| | |
|---|---|
| `WASD` / arrows | move |
| mouse | aim |
| left click / `Space` | shoot (hold to keep firing) |
| `R` | reload |
| `Esc` | back to the menu |

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
| **Shield** | absorbs exactly one bullet, then pops |
| **Inf ammo** | no reloading for 10s |
| **Rapid fire** | ~2.6x fire rate for 10s |
| **Hot rounds** | 1.7x bullet speed for 12s |
| **Sprint** | 1.45x move speed for 10s |
| **Scatter** | three bullets per shot for 10s |

Powerups are cleared when you die, so being loaded up isn't a guaranteed round.

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
  `sudo ufw allow 50500/tcp && sudo ufw allow 50505/udp`. Windows will pop a
  dialog the first time you host — allow it on private networks.
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
