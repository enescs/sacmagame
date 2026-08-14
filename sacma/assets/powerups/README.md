# Powerup icons

Drop a 16x16 PNG here named after the powerup and the crate will show it
instead of the powerup's initial. Anything missing keeps the lettered
fallback, so you can add them one at a time.

| file | crate |
|---|---|
| `shield.png` | Shield |
| `ammo.png` | Inf ammo |
| `rapid.png` | Rapid fire |
| `velocity.png` | Hot rounds |
| `swift.png` | Sprint |
| `scatter.png` | Scatter |
| `invis.png` | Invisible |
| `golden.png` | Golden gun |
| `reflect.png` | Reflect |
| `homing.png` | Homing |
| `bounce.png` | Ricochet |
| `frost.png` | Frost |
| `hold.png` | Time stop |

Each file also loads under the name the art was drawn with (`infammo.png`,
`hotrounds.png`, `sprint.png`, `invisible.png`, `goldengun.png`,
`recochet.png`, `rapidfire.png`, `timestop.png`) — see `ICON_ALIASES` in
`client.py`.

Drawing notes:

- A crate with an icon is a **dark plate** ringed and lit in the powerup's
  colour, so icons may use their own colours freely; only very dark art risks
  vanishing. (The lettered fallback crate is still filled with the powerup
  colour, but nothing is drawn over it but the letter.)
- The full 16x16 is usable — the plate is a square and does not rotate.
- Icons are scaled to 20px and never rotate, so keep them chunky and readable
  rather than detailed. Other sizes load fine (they get smoothscaled), but 16x16
  drawn at 16x16 stays crispest.
