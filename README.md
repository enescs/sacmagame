# sacmagame

A small top-down shooter for one office and one wifi network.

Last player standing wins the round. **One bullet kills anybody**, including you,
so it's mostly about angles, corners and not being where someone is looking.
Five maps rotate automatically, some of them with obstacles that move while you
are trying to hide behind them.

Every seventh round or so one of you is made the boss and the rest have to team
up and bring them down. There are two other modes — boss rush, where that
happens every round, and 2v2 capture the flag — and anyone can switch between
them mid-match with `M`.

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

`--mode` picks what you start on: `ffa` (default), `boss` or `ctf`. Nobody has
to get it right up front though — **press `M` in game to change modes**, and
the next round is played that way.

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
| `M` | game mode — `1`–`3` or `Enter` to pick, `M`/`Esc` to close |
| `F1` / `F2` | debug: jump to the previous/next map (or `PgUp`/`PgDn`) |
| `Esc` | back to the menu |

Map jumping restarts the round, so host with `--no-debug` once people are
actually competing. Durations and fire rates are all constants at the top of
[shared.py](sacma/shared.py) if something feels off.

Six rounds in the magazine, 1.5s reload, and it auto-reloads when you run dry —
firing wildly is how you get caught reloading.

## Modes

| | |
|---|---|
| **free-for-all** | last one standing, boss round now and then |
| **boss rush** | every round has a boss, everyone else teams up |
| **capture the flag** | 2v2, first to three captures (four players max) |

Press `M` in game, pick with `1`–`3` or the arrows, `Enter` to confirm. **The
change lands on the next round**, never the one being played — a banner at the
top of the arena says what's coming, and the feed says who asked for it.

Anyone can do it, same as the map keys; it's a game for people sitting in the
same room. If you'd rather it stayed put, host with `--no-mode-vote` and only
`--mode` decides.

Two things to know: switching to capture the flag is refused while more than
four people are connected (everyone gets told why), and while it's queued the
server holds the last seats empty so a fifth person doesn't join and then have
to be thrown out. Teams are assigned when the mode lands and cleared when you
leave it.

## Rounds

With two or more players connected the server cycles: 3s countdown, then the
round runs until one player is left (or 100s passes, which is a draw), then 4s
on the scoreboard, then the next map.

Get killed and you sit out the rest of the round and spectate — there are no
respawns mid-round. Join mid-round and you spectate until the next one starts.

Alone on a server you're in free roam: instant respawns, obstacles running, good
for getting a feel for the maps.

## Boss rounds

Roughly one round in seven, with three or more players, somebody is picked at
random and everybody else has to deal with them. It's announced during the
countdown, so you get three seconds to find out where they are. In **boss
rush** it's every round instead, and it'll run with as few as two players,
where free-for-all leaves it alone — a boss with one hunter is just a duel with
extra health.

The boss is the only thing in the game that doesn't die to one bullet. They get
`3 + 2 per opponent` health (capped at 15) shown as a bar over their head, plus
a bigger body, 1.22x speed, roughly double the fire rate, a 14-round magazine
and a quicker reload. The size cuts both ways — they're easier to hit, and they
fit through less.

Everyone else is on the same side for the round: **hunters can't shoot each
other**, bullets pass straight through teammates. Kill the boss and every
hunter gets the round win, including the ones who died drawing fire, because
that's usually why it worked. If the boss outlives all of them, the win is
theirs alone. If the clock runs out the hunters take it — the boss is the
stronger side and is expected to come and get them, not sit in a corner.

The same player is never picked twice in a row.

## Capture the flag

`--mode ctf`, or `M` in game — two teams of two, so four players and no more.
You're put on the thinner side as you join, blue on the left, red on the right.

Everyone is drawn in their team's colour with their usual colour as a dot in
the middle, and **teammates can't shoot each other** — bullets pass through.

Grab the enemy flag off its stand and carry it to your own stand to score.
**Carrying it halves your speed**, so the run home is a serious commitment —
anyone who spots you gains on you at 132 px/s and runs you down, and the flag
really only moves while your teammate is covering you. Sprint doesn't cancel
it (0.73x, still slower than a free jog) but it helps a lot, so a loose sprint
is the thing to fight over on the way out.
**Your own flag has to be home for that to count**, which is what stops both
teams simply running past each other; if it's away, you're standing on your
base holding theirs until somebody sorts it out. First to three captures wins
the round, or the higher score when the 180s runs out.

Dying is a three-second respawn at your own base, not the end of your round.
Whoever kills the carrier makes them drop the flag exactly where they fell: run
over your own dropped flag to send it straight home, or leave it for twelve
seconds and it goes back on its own. The enemy can pick it up again in the
meantime.

Round wins go to the whole winning team. Everything else — loot, powerups, the
map rotation — works exactly as it does in free-for-all. There are no boss
rounds here.

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
| **Ricochet** | 8s — your bullets bounce off walls up to 3 times, and never hit you |
| **Frost** | 8s — your bullets leave a patch of ice where they land, slowing everyone but your side |
| **Time stop** | 1.5s — the whole arena freezes except you: nobody moves, nobody shoots, bullets hang in the air |
| **Ghost rounds** | 6s — your bullets go through walls; slightly slower |
| **Earthquake** | shakes everyone else's actual window for 3s |

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
- **Ricochet rounds can never come back at you.** No bullet in the game can hit
  the player who fired it, bounced or not, so you can safely bank shots off a
  wall right next to you. Each bounce sheds a little speed and the round dies
  after three, which keeps corridors from filling up with strays.
- **Frost is area denial, not damage.** A patch is 74px across, lasts 3.5s and
  cuts move speed to 55% for everyone standing in it except the side that laid
  it — its rim is drawn in the owner's colour, so you can see at a glance whose
  ice you are about to step into. At most 8 patches exist at once.
- **Frost stacks with ricochet.** A bounced frost round drops its patch
  wherever it finally stops, which is how you get ice around a corner.
- **Time stop fires the moment you touch the crate** — there is nothing to save
  it for. For 1.5s everyone else is locked in place and cannot turn, shoot or
  reload, rounds already in the air hang where they are, and the moving
  obstacles stop with them. Your own shots still travel, so the window is worth
  exactly one good angle. Every deadline that decides a round pauses with it —
  the round clock, respawn timers and the countdown on a dropped flag — so
  nobody loses anything to a timer that ran while they were frozen. It freezes
  teammates too: in a team mode, mind who you are stopping.
- **Bullets are drawn in their shooter's colour**, so in a crowded fight you can
  always tell your rounds from everyone else's. A parried round changes colour
  along with its new owner.
- **Ghost rounds still stop at the arena border**, and they are the one thing
  that makes cover worthless — if someone picks one up, stop hugging a wall
  and start moving. They fly a little slower, which is your warning.
- **Ghost beats ricochet where they meet.** A round that passes through walls
  never touches one, so there is nothing to bounce off — but with both running
  it still rebounds off the arena edge, which is the one thing ghost respects.
- **The earthquake is the only powerup that does nothing to the simulation.**
  It physically shakes the game window on every rival's desk for three
  seconds; the mouse stays put while the arena slides under it, so their aim
  wanders. Teammates are spared. It is deliberately mild — a nudge, not a
  seizure — and if the desktop refuses to let the game move its own window
  (Wayland, mostly) the picture shakes inside the window instead.

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
sliding blocks and rotors — if you want to add one. Every map also carries a
pair of flag stands for capture the flag, facing each other across the long
axis with the loot cluster in between; a new map needs to keep those two spots
clear.

## If nobody can connect

Run `python netcheck.py <host ip>` on the machine that cannot join. It reports
your subnet, whether any beacons arrive, and whether the host's port answers —
then tells you which of the causes below you have. Plain stdlib, so it runs on
a machine that never went through setup.

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
- **They see someone else's game but not yours.** Then discovery works fine and
  you are simply on a different subnet — typically you on ethernet, them on
  wifi. Broadcasts stop at that line; routed TCP usually does not, so `M` plus
  your IP still gets them in. To put yourself back in their list, beacon to
  their subnet directly:

  ```
  ./host.sh --name "Office FFA" --announce 10.166.120.0/24
  ```

  `--announce` takes plain IPs too, comma-separated. Compare the first three
  numbers of your IP against theirs to see whether this is your problem.

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
