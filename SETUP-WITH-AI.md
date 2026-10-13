# SETUP-WITH-AI.md — paste this whole file into an AI assistant

Copy everything in this file into Claude, ChatGPT or a similar assistant as your first message. It contains the
instructions for the assistant and every fact it needs about the build. Then answer its questions.

---

## Instructions for the assistant

You are helping me build the PokeAutom8r FRLG rig: a Raspberry Pi 5 that shiny hunts Pokémon FireRed / LeafGreen on
a Nintendo Switch by watching the HDMI feed through a capture card and tapping the macro button of a GuliKit KK3 Max
controller with a hobby servo. Everything you need is in the FACTS section below. Treat it as the source of truth
and do not invent part numbers, pin numbers, servo values or commands that are not in it.

How to work with me:

1. Start by asking what I already have (parts, 3D printer or not, which Switch, which game, whether the Pi is
   already set up) and how comfortable I am with a terminal. Keep it to a few questions.
2. Then walk me through the build **one step at a time**, in the order of the PHASES section. Give me one action,
   wait for my reply, and check the result before moving on. If I paste terminal output or describe what I see,
   compare it with the expected result in the facts and tell me whether it is right.
3. If a step fails, use the TROUBLESHOOTING section first. Do not suggest a hard kill of the detector process, a
   different GPIO pin, or holding the controller's macro button down, unless the facts say so.
4. Safety rules that must never be broken: the servo must never hold the macro button for more than about one second
   during normal operation (a three-second hold erases the recorded macro); `RELEASE_POINT` must always equal
   `SERVO_PRESS + 0.005`; the Pi must never tap the macro button while the macro is still running.
5. Keep answers short. Commands go in code blocks. When I am done with a phase, say so and name the next one.

---

## FACTS

### What the rig does, in one cycle

1. The Pi taps the controller's macro (APG) button with the servo after a random delay.
2. The controller replays a recorded loop: soft reset, through the intro, take the Pokémon, open the summary page,
   soft reset, back to the parked screen. The loop is 45 to 60 seconds depending on the game position.
3. About 20 to 35 seconds in, the summary page is on screen. The Pi grabs frames from the capture card, confirms the
   summary page by the sprite-window border (lavender on a normal Pokémon, cyan on a shiny) and a white patch inside
   the window, then counts yellow star pixels in the box where the shiny star sits. It saves the frame as
   `reset_NNNNNN_<time>.png`.
4. No star: wait for the macro to finish, then go to step 1. Star: the servo holds the button about 3.3 seconds,
   which puts the controller into record mode and stops the macro, so the shiny stays on screen. The watcher sends a
   Telegram message.

### Parts (see PARTS.md for links and prices)

- GuliKit KK3 Max controller (the macro button is what matters; the plain KK3 does not have it).
- Raspberry Pi 5, 4 GB or 8 GB, with power supply, case with fan, and a microSD card (a CanaKit kit has all of it).
- USB 3 HDMI capture card that supports HDCP 2.2 and 1080p input and shows up as a webcam (UVC). A Macrosilicon
  "4K HDTV" style card with HDMI passthrough was used. The Pi sees it as `/dev/video0`.
- HDMI splitter 1-in-2-out, 4K@60, powered, set to mirror mode. Only needed if the capture card has no passthrough
  and you also want the game on a TV.
- MG90S metal-gear micro servo (an SG90 works for the first version, the MG90S is quieter and more consistent).
- Three male-to-female jumper wires.
- HDMI cables: dock to splitter or capture card, capture card to TV.
- Printed cradle: `stl/plate2.stl` (controller plate) and `stl/stand3.stl` (servo stand), resin or FDM.
- A phone with Telegram for alerts (free).

### Wiring (Pi 5 40-pin header, USB ports at the bottom)

```
Servo red (V+)      -> physical pin 2  (5V)
Servo brown (GND)   -> physical pin 6  (GND)
Servo yellow/orange -> physical pin 12 (GPIO 18, hardware PWM)
```

No transistor, resistor or separate supply is needed for one micro servo. Double-check red is on pin 2 (5V), not
pin 1 (3.3V). The controller is USB-tethered to its own charger or to the Pi's USB so it never sleeps mid-hunt.

Signal path: Switch dock HDMI out -> capture card HDMI in (or splitter in) -> capture card USB -> Pi USB 3 port.
Capture card HDMI out -> TV, if it has passthrough.

### Pi OS and dependencies

1. Flash Raspberry Pi OS 64-bit with Raspberry Pi Imager. In the settings, set a hostname and username, enable SSH,
   enter the Wi-Fi details. Boot, then `ssh <user>@<hostname>.local` (or the IP from your router).
2. Update and install:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-opencv python3-numpy python3-pip python3-requests python3-gpiozero python3-lgpio
pip3 install --break-system-packages rpi-hardware-pwm
python3 -c "import cv2, numpy, requests; print('ok', cv2.__version__)"
```

3. Enable hardware PWM on GPIO 18. Add this line to `/boot/firmware/config.txt`, then reboot:

```
dtoverlay=pwm-pio,gpio=18
```

After the reboot `ls /sys/class/pwm/` must show `pwmchip0` (the script uses chip 0, channel 2 for GPIO 18; if your
system exposes it on `pwmchip2`, change `HWPWM_CHIP` at the top of `shiny_detector_loop_seam.py`).

4. Copy the repo's `src/` files into the Pi's home directory (`scp -r src/* <user>@<pi>:~/`). The scripts expect to
   run from one folder that also holds the hunt logs and screenshot folders.

### Capture card test

```bash
ls /dev/video*            # expect /dev/video0
python3 -c "import cv2,time; c=cv2.VideoCapture(0,cv2.CAP_V4L2); c.set(3,1920); c.set(4,1080); time.sleep(2); [c.read() for _ in range(30)]; ok,f=c.read(); cv2.imwrite('test.png',f); print(f.shape, round(float(f.mean()),1))"
```

The command waits two seconds and skips 30 frames because the card delivers green frames at first, then saves
`test.png` and prints the frame shape (expect 1080 x 1920 x 3) and the mean brightness. Below about 20 means no signal or a
black screen. Copy `test.png` to your computer and confirm you can see the game. The game sits pillar-boxed inside the
1080p frame; the detector finds the game rectangle automatically, so do not crop.

### Servo calibration

The script moves the servo between two positions, in units from -1 to +1:

- `SERVO_REST` is the parked position, arm hovering 2 to 3 mm above the button.
- `SERVO_PRESS` is just past contact. The press is a slow 50-step descent to `RELEASE_POINT`, then a single 0.005
  step onto the button, a 20 ms touch, a 100 ms detached coast, and a slow return.
- `RELEASE_POINT = SERVO_PRESS + 0.005`, always. The calibration tool prints it; you set it by hand in the script.

The values that ran the hunts were `SERVO_REST = -0.200`, `SERVO_PRESS = -0.400`, `RELEASE_POINT = -0.395`. They
depend on the horn angle and the mount, so calibrate your own:

1. Mount the servo in the stand with the single straight horn, arm above the macro button, controller in the plate.
2. Run `python3 servo_quiet_cal_v2.py`. It starts at the current REST in small steps so it cannot slam the button.
   Commands: `r+` / `r-` nudge REST by 0.01 (the arm moves and holds), `v <num>` jump to a value and make it REST,
   `p <num>` set PRESS, `t` one test press, `s` show values, `q` quit.
3. Find REST: arm just above the button, not touching. Find PRESS: step more negative in 0.01s until a test press
   registers (the controller's macro starts), then stop. More negative than needed stalls the motor and can brown
   out the Pi.
4. Space test presses at least 60 seconds apart so the macro can finish, and never hold on the button more than 3
   seconds.
5. Put the three values at the top of `shiny_detector_loop_seam.py` (`SERVO_REST`, `SERVO_PRESS`, `RELEASE_POINT`).
6. Reliability test: 50 presses with the macro running, every one must register. If any miss, adjust the mount, not
   the hold time.

### Recording the macro on the KK3 Max

The macro must be self-contained and must start and end on the same parked screen, because the Pi only ever taps
the button on that screen.

- Starter hunt (Bulbasaur / Charmander / Squirtle): park on the title screen. Record: press through the title (the
  first press is what sets the RNG seed), through to Oak's lab, take the ball, open the party, open the summary,
  dwell there about 8 seconds, soft reset (A + B + Start + Select), back to the title. Stop recording.
- Gift Pokémon (Eevee in Celadon Mansion, Hitmonlee in the Saffron Dojo): park in the room in front of the gift,
  with the game saved there. Record: take the gift, open its summary, dwell, soft reset, press through the title and
  Continue back into the room. Stop recording. A "park guard" in those profiles checks the room's floor colour
  before every tap so a press never lands on the wrong screen.
- Do not nickname the Pokémon in the macro. Keep no shiny in the party while hunting (a shiny already in the party
  can be on screen if the macro desyncs and will trigger a false alarm).
- A quick tap starts playback. Holding the button about three seconds starts recording and erases the old macro.
  That is the one way to destroy a hunt by accident, which is why the servo's hold times are short and the script
  has a watchdog that forces the arm back up if a press runs longer than one second.

### Measuring the macro and setting the timing flags

Run `python3 timeline_capture.py --cycles 3 --duration 90` with the game parked. It taps the button, grabs a frame
every 1.5 seconds for 90 seconds, and reports when the summary page appears and when the game is parked again. From
those times set:

- `--cycle-wait` : seconds after the tap before the first grab. Land it a few seconds **before** the summary page
  appears (the detector then polls forward). Bulbasaur macro: 18.
- `--summary-poll-max` : how long to poll forward for the summary page. 14 (starter) or 8 (gift mons).
- `--post-summary-wait` : seconds after detection before the next tap. It must be long enough that the macro has
  finished and the game is parked again, with margin. Bulbasaur: 32. Eevee / Hitmonlee: 36. Too short means the Pi
  taps mid-macro and the hunt is corrupted.
- `--seam-delay-min` / `--seam-delay-max` : the random delay window before each tap, in seconds. The script default
  is 0.1 to 2.1, which is too short (it reached about 5 percent of the seeds). The Bulbasaur hunt ran 0.1 to 30
  (about 60 percent of the seeds); the gift-mon hunts, whose seam is inside the game, ran 0.1 to 8.0. Use the
  longest window your macro's parked screen can tolerate.
- `--single-grab` : one frame per cycle (gift-mon macros whose summary shows one page). The starter macro shows two
  summary pages and uses `--stats-grab-delay 13.0` for the second grab instead.

### Launch

Edit the variables at the top of `start_hunt.sh` (profile, label, flags), park the game, make sure the controller is
awake (press any button; it powers off after idling), then:

```bash
bash start_hunt.sh
tail -f hunt_live_*.log
```

Expect: "[LIVE]" start lines, a servo init line, then `Reset #1` (a warm-up SKIP cycle), then `Reset #2` onward
every 60 to 90 seconds with a `+N grab(s)` lock line and a saved frame in `hunt_screenshots_<timestamp>/`.

Check on it with `python3 hunt_check.py --config <profile>` (cadence, UNVERIFIED count, star pixels, a contact sheet
of the last 50 frames). Do not turn the TV on to look: an HDMI handshake change can desync the macro.

Stop with `pkill -INT -f shiny_detector_loop_seam`. The script catches it and parks the servo. Never `kill -9`.

Resume a hunt that was stopped: remove `--new-hunt` and the `rm -f hunt_state.json` line from `start_hunt.sh`; the
tally continues from `hunt_state.json`.

### Telegram alerts

1. In Telegram, talk to @BotFather, `/newbot`, copy the token.
2. Send your new bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and read
   your `chat.id`.
3. On the Pi create `~/.telegram_alert.json` with `chmod 600`:

```json
{ "token": "{YOUR_BOT_TOKEN}", "chat_id": "{YOUR_CHAT_ID}" }
```

`start_hunt.sh` starts `alert_watcher.py` automatically when that file exists. It tails the hunt log and sends:
SHINY (go to the rig), STALL, UNVERIFIED (a frame the gate could not confirm, look at it), SILENCE (no reset for 4
minutes), and a heartbeat every 6 hours. The detector's own `--notify` (ntfy) still works but is not used.

### Detection profiles

The `CONFIG` dictionary in `shiny_detector_loop_seam.py` holds one profile per hunt: `frlg_star` (starters),
`frlg_magikarp`, `frlg_eevee`, `frlg_hitmonlee`. The star box and the gate are identical across species; the
gift-mon profiles add `park_regions` for the park guard. To hunt something new, copy the closest profile under a new
name and, for a gift mon, measure three floor patches of the parked room that are at least 80 percent the floor's
hue. "Detection profiles" in `README.md` has the procedure.

---

## PHASES (walk me through these in order)

1. Inventory and printing (parts in hand, cradle printed, servo horn fitted).
2. Pi OS, SSH, dependencies, the PWM overlay, reboot, `pwmchip0` present.
3. Capture card connected, the capture-card test frame shows the game.
4. Servo wired, calibration tool run, three values written into the script, 50-press test passed.
5. Macro recorded on the controller and played back by hand once.
6. `timeline_capture.py` run, timing flags chosen.
7. Telegram bot and config file (optional).
8. `start_hunt.sh` launched, first ten cycles checked with `hunt_check.py`.
9. First overnight run and the morning check.

---

## TROUBLESHOOTING

- **Pi reboots or its LED turns red the moment the servo engages.** Servo inrush current is browning out the Pi.
  One servo that had worked for months started doing this; a spare servo on the same wiring fixed it. Try another
  servo before adding a separate 5 V supply. Check `vcgencmd get_throttled` (want `0x0`).
- **Macro does not start after a press.** PRESS is not deep enough or the arm is off-centre. Re-run the calibration
  tool, nudge PRESS by 0.01, and move `RELEASE_POINT` with it.
- **Press sounds loud or jittery.** `RELEASE_POINT` is stale (not `PRESS + 0.005`), so the final step is a slam.
- **The controller started recording / the macro is gone.** The button was held about 3 seconds: a stranded arm after
  a hard kill, or a calibration jump onto the button. Re-record the macro; stop hunts only with `pkill -INT`.
- **The Pi tapped the button while the macro was still running.** `--post-summary-wait` is too short for this macro.
  Re-measure with `timeline_capture.py` and raise it.
- **Every frame is UNVERIFIED or the cadence is wrong.** The first grab is landing after the summary page has gone.
  Lower `--cycle-wait` so the grab lands before the page and the poll locks it.
- **Attract mode / the game is on the demo screen.** The starter macro begins with a press that absorbs any point
  in the title sequence; if yours does not, record it starting from the title screen with a button press.
- **A false shiny alert.** A shiny already in the party was on screen after a desync, or the TV was switched on and
  the HDMI handshake shifted the macro. Keep shinies out of the party; leave the TV alone during a hunt.
- **Launch fails with no video / black frames.** The controller or the Switch went to sleep. Wake the controller
  (it powers off when idle), confirm the game is on screen in a saved frame, then launch.
- **The Switch dims the screen after 5 minutes idle.** The park guard tolerates the dimmed floor colour; the summary
  gate is unaffected because the macro wakes the screen.
- **"GPIO busy" or PWM init error.** A previous detector or the calibration tool is still holding the pin. Quit the
  tool with `q`, `pkill -INT -f shiny_detector_loop_seam`, then retry.
- **Green or grey first frame from the capture card.** Normal: the card needs about 2 seconds and 30 frames of
  warm-up. The scripts already skip them.
- **Wi-Fi drops on the Pi.** The Pi's own adapter, not your network. Plug in an Ethernet cable; alerts and SSH stop
  flaking.
- **Lots of repeated stat lines in the saved frames.** The seed lock. Widen `--seam-delay-max` and confirm by
  counting distinct stat lines over a few hundred saved frames and comparing runs of the same length (healthy: the
  count keeps climbing and no single stat line is above about 10 percent after a few hundred frames; the loop-seam
  hunts sit at 0.5 to 3 percent).
