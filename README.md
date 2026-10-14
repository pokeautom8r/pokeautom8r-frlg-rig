# PokeAutom8r FRLG rig — a servo pressing one real button shiny hunted Pokémon FireRed / LeafGreen on a stock Switch

A Raspberry Pi watches the Switch's HDMI feed through a capture card. When it sees the Pokémon's summary page it
checks for the shiny star. A hobby servo then taps the macro button on a GuliKit KK3 Max controller, which replays a
recorded soft-reset loop. Nothing is plugged into the Switch that Nintendo didn't make. It ran for about seven
months and 78,000 soft resets, missed two shinies because of a bug in my own detection, and then found a shiny
Bulbasaur at reset 21,266 and a shiny Eevee at reset 2,171.

Video: https://youtu.be/eNQQcBJI0eQ

Everything here is free: the scripts, the detection profiles, the cradle STLs, the parts list, and a setup file you
can hand to an AI assistant and be walked through the build one step at a time (`SETUP-WITH-AI.md`). If it helps
you, subscribe: https://www.youtube.com/channel/UCDPMqobhTDp3NIQ9cLcjcUA?sub_confirmation=1

---

## Why this design

Every other FRLG-on-Switch bot I found emulates a controller over Bluetooth or USB. I wanted the console to see only
a real controller. So the automation is split in two:

- **The controller does the inputs.** The KK3 Max has a macro button (GuliKit calls it APG). You record a loop once
  by hand: soft reset, mash through the intro, take the Pokémon, open its summary, soft reset again, back to the
  parked screen. The controller replays it every time its button is tapped.
- **The Pi does the looking and the tapping.** A capture card gives the Pi the HDMI picture. OpenCV finds the game
  area inside the pillar-boxed 1080p frame, confirms the summary page is on screen, counts star-coloured pixels in
  the box where the shiny star appears, and saves a frame of every reset. If there is no star, a servo taps the
  macro button for the next cycle. If there is a star, the servo holds the button for about three seconds, which
  drops the controller into record mode and kills the running macro, so the shiny stays on screen until I get there.

A cycle is 60 to 90 seconds depending on the macro, which is 1,000 to 1,500 resets a day.

## The two things that were harder than the hardware

**1. The seed lock.** FireRed and LeafGreen seed their RNG from a hardware timer that is read when you press through
the title screen. A macro presses at the same instant every cycle, so a fixed-timing loop samples a tiny slice of
the 65,536 possible seeds. I measured it: a fixed-timing macro produced only about 240 distinct stat lines in 1,700
rolls, when random seeding should have produced around 800. About 86 percent of the rolls were repeats, against a
floor of about half that level-5 stats alone would show even with perfect seeding. Papa Jefé's PSA for
macro-controller hunters explains the mechanism; retailrng.com has the maths.

The fix is a random delay inserted by the Pi before it taps the button, at a point in the loop where the game is
idle, so the title-screen press lands on a different timer value each cycle. The first version waited 0.1 to 2.1
seconds. That broke the lock: the count of distinct stat lines kept climbing instead of flattening, and at equal
sample sizes the repeat rate fell from about half to about one in eight. But a later level-25 hunt showed it was
still reaching only about 5 percent of the seeds. Widening the wait to 30 seconds on the Bulbasaur loop took that to
roughly 40,000 of the 65,536 seeds, about 60 percent, measured four times. The gift-Pokémon macros use an in-game
seam and wait up to 8 seconds; they sit at roughly 10 to 20 percent repeats over the first few thousand resets (a
pool of about 10,000 seeds per recording), so use the longest window your macro can tolerate. The way to check your
own loop is to count distinct stat lines across a few hundred saved frames and compare runs at the same length: if
the count stops climbing, the loop is stuck on a handful of seeds.

**2. My gate was blind to shinies.** To avoid counting star-coloured pixels on the wrong screen, the detector first
confirms the summary page by sampling the lavender border of the sprite window. On a shiny, FRLG draws that border
cyan. So the gate rejected every shiny frame and the star was never counted. The detector reset past two shiny
Bulbasaur, at resets 13,102 and 15,104, and logged them as clean. I found them weeks later by replaying 13,000 saved
frames through a fixed gate. The shipped script accepts both border colours, treats any gate failure as UNVERIFIED
and pages me. The detector also saves a frame of every reset, so the whole hunt can be checked by eye afterwards,
which is the only check that does not share the detector's assumptions.

## Results

| Hunt | Reset | Date | Note |
|---|---|---|---|
| Bulbasaur (starter) | 13,102 | 2026-08 | shiny, missed by the gate bug |
| Bulbasaur (starter) | 15,104 | 2026-09 | shiny, missed by the gate bug |
| Bulbasaur (starter) | 21,266 | 2026-09-25 | caught |
| Eevee (Celadon gift) | 2,171 | 2026-10-01 | caught, first with the fixed gate |

Lifetime resets across all hunts at the time of writing: about 78,000. The rig is hunting a Hitmonlee now.

## What is in this repo

```
README.md               this file
SETUP-WITH-AI.md        paste into Claude / ChatGPT: a guided build, one step at a time
PARTS.md                parts list with prices and links
src/
  shiny_detector_loop_seam.py   the detector + servo driver (the production script)
  alert_watcher.py              Telegram alerts: shiny, stall, silence, heartbeat
  hunt_check.py                 health check from logs + saved frames (never the TV)
  start_hunt.sh                 one-shot launcher for detector + watcher
  servo_quiet_cal_v2.py         interactive servo calibration (REST / PRESS)
  timeline_capture.py           measure when the summary page appears in a new macro
stl/                    plate2.stl (controller plate) + stand3.stl (servo stand)
LICENSE                 MIT (code)
LICENSE-STL.md          CC BY-NC-SA 4.0 (the printed parts)
CREDITS.md
```

## Quick start

1. Buy the parts in `PARTS.md` (about $350 if you have none of it; the controller and the Pi are most of that).
2. Print `stl/plate2.stl` and `stl/stand3.stl` (see "Printing the cradle" below).
3. Open `SETUP-WITH-AI.md`, paste the whole file into an AI assistant, and tell it what you have. It will walk you
   through the OS install, wiring, capture-card test, servo calibration, macro recording and the first launch, and
   check each step before moving on. It is also readable on its own if you would rather not use an assistant.
4. Record your macro on the controller, measure it with `src/timeline_capture.py`, set the timing flags, and launch
   with `src/start_hunt.sh`.

## Which games and consoles

Built and run on the Nintendo Switch Online GBA release of FireRed / LeafGreen on a Switch 2. A Switch 1 should
behave the same; the game output and the controller are identical. Detection regions are anchored to the auto-detected game rectangle, so the pillar-box width does not matter.
The star box and the summary gate are the same for every FRLG species; "Detection profiles" below shows how to clone a
profile for a new hunt and how the in-game "park guard" works for gift Pokémon whose loop never returns to the title
screen. Ruby / Sapphire / Emerald are not supported: with a dead or frozen clock those games reseed to a constant,
so a blind reset loop would walk the same handful of frames forever.

## Printing the cradle

Two printed parts hold the controller still and put the servo arm over its macro button: `stl/plate2.stl`
(the GuliKit KK3 Max sits in it, grips clear of the table) and `stl/stand3.stl` (holds an MG90S / SG90 with the arm
over the macro button).

### Printing
- Mine were printed in standard resin on an Elegoo Saturn, 0.05 mm layers, light supports under the plate's
  overhangs. The plate has done about 80,000 presses with no wear.
- FDM is fine: PLA or PETG, 0.2 mm layers, 3 walls, 20 percent infill, plate flat side down, stand upright.
- The stand's servo pocket is sized for the MG90S / SG90 body (22.5 x 12 mm). Fix the servo with its own two screws
  or a drop of hot glue.

### Fitting

1. Seat the controller in the plate. It should not rock.
2. Fit the single straight horn to the servo so that at the REST value (about -0.20 in the script's -1..+1 units)
   the arm tip hovers 2 to 3 mm above the macro button, roughly at the 4 o'clock position.
3. Put a small piece of foam or rubber tape on the arm tip for grip.
4. The arm tip should come down nearly vertically on the centre of the button. If it slides off, shift the stand, not
   the servo values.
5. Run the 50-press reliability test from `SETUP-WITH-AI.md` before the first overnight hunt.

## Detection profiles

The profiles are not separate files. They are entries in the `CONFIG` dictionary near the top of
`src/shiny_detector_loop_seam.py`, selected at launch with `--config <name>`. Four ship with the script:

| Profile | Hunt | Parked screen | Park guard |
|---|---|---|---|
| `frlg_star` | starters (Bulbasaur / Charmander / Squirtle), Oak's lab | title screen | no |
| `frlg_magikarp` | Magikarp salesman, Mt. Moon Pokémon Center | title screen | no |
| `frlg_eevee` | Eevee gift, Celadon Mansion | in the room, in front of the NPC | yes |
| `frlg_hitmonlee` | Hitmonlee gift, Saffron Fighting Dojo | in the dojo, in front of the left ball | yes |

### What a profile contains

All four share the same detection numbers, because the summary page looks the same for every FRLG species. The
coordinates are fractions of the auto-detected game rectangle (x, y, width, height), not of the full 1080p frame.

```python
"detect_region": (0.41, 0.21, 0.08, 0.08),   # the box where the shiny star appears, next to the name
"star_hue_range": (20, 32),                  # yellow-gold, OpenCV hue (0-179)
"star_sat_range": (80, 255),
"star_val_range": (150, 255),
"star_pixel_threshold": 50,                  # a real star is ~900 px in the box; normals are 0

"gate_region": (0.02, 0.10, 0.06, 0.55),     # left edge of the sprite window border
"gate_hue_range": (130, 165),                # lavender border = normal Pokémon
"gate_hue_range_alt": (80, 95),              # cyan border = SHINY. Without this the gate rejects every shiny.
"gate_sat_range": (30, 180),
"gate_val_range": (160, 255),
"gate_pixel_threshold": 500,
"gate_white_region": (0.025, 0.22, 0.06, 0.06),  # inside the sprite window is pure white on every summary page
"gate_white_min_frac": 0.80,                     # and never on the overworld, menus or title
```

The gate exists so the star box is only read on the summary page. The yellow floor of the Celadon room, for example,
would otherwise count as a star. The white-patch test was added after the first gate let an overworld frame through.

### Park guard (gift-Pokémon profiles only)

The starter macro ends on the title screen, and pressing the macro button there is always safe. A gift-Pokémon macro
ends parked in a room, and a press on any other screen replays the inputs against the wrong state (one stray press
on the title menu once walked into NEW GAME). So those profiles add:

```python
"park_regions": [(0.06, 0.62, 0.10, 0.10), (0.84, 0.62, 0.10, 0.10), (0.30, 0.80, 0.08, 0.06)],
"park_hue_range": (20, 32),     # the room's floor colour (Celadon: yellow; the Dojo is khaki, hue 30)
"park_min_frac": 0.80,          # each patch must be at least 80 % floor-coloured
"park_val_min": 25,             # Hitmonlee only: tolerate the Switch's 5-minute idle dimming
```

Before every tap the script grabs a frame and checks all patches. If the room is not on screen it waits up to 20
seconds and then stops cleanly instead of pressing.

### Adding a species

1. Copy the closest profile (a title-screen hunt from `frlg_star`, a gift mon from `frlg_hitmonlee`) under a new
   name inside `CONFIG`. Leave the star and gate numbers alone.
2. For a gift mon, save the game parked in front of the gift, grab a frame (the capture-card test command in `SETUP-WITH-AI.md`, saved as `parked.png`), open it, and
   pick three patches of plain floor that are clear of NPCs and your own sprite. Express them as fractions of the game
   rectangle. Check the patches read at least 0.80 on the parked frame and under 0.40 on the title screen, the
   summary pages and the reset screen (`is_parked()` in the script prints the fractions).
3. Record the macro, measure it with `timeline_capture.py`, set `--cycle-wait` and `--post-summary-wait`.
4. Launch with `--config <your_name>` and run `hunt_check.py --config <your_name>` after the first ten cycles: gate
   PASS on every frame, star pixels 0, no UNVERIFIED lines.

Hunts measured so far: Magikarp 4,110 resets, Bulbasaur 21,266, Eevee 2,171, all on these numbers with zero false
positives after the white-patch gate was added.

## Credits

- Papa Jefé, whose PSA Short explained the macro seed problem: https://youtube.com/shorts/MF8Amtvzp2A
- retailrng.com for the Gen 3 RNG reference.
- Benbenshow's "Gulikit KK3 Max model" (Printables 1091236, CC BY-SA 4.0) was the controller body in my Fusion
  assembly while designing the cradle. It is not included in this repo; download it from Printables if you want the
  assembly to render.
- The Raspberry Pi, OpenCV and rpi-hardware-pwm projects.

## Licence

Code: MIT (see `LICENSE`). Printed parts in `stl/`: CC BY-NC-SA 4.0 (see `LICENSE-STL.md`). Pokémon, FireRed,
LeafGreen and Nintendo Switch are trademarks of their owners; this is an unaffiliated hobby project that runs
local, single-player automation and modifies nothing on the console.
