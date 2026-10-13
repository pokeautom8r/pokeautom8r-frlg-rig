"""
Servo Quiet Calibration v2 — HARDWARE PWM (Pi 5, GPIO 18 = PWM0 chan 2)
=======================================================================
Written 2026-09-08 for the horn replacement. Same job as servo_quiet_cal_hwpwm.py
but with the traps from 8/23 and 8/24 designed out:

  * Starts at the CURRENT hunt values (REST -0.200 / PRESS -0.400) instead of
    the stale 5/12 guesses, and moves to REST in small steps rather than one
    full-speed jump — so starting the tool with a horn attached can't slam APG.
  * Every command that moves the arm (r+, r-, v) ALSO updates the internal
    rest value, so a later test press returns to where the arm actually is.
    (The 8/23 wipe happened because "v" moved the arm but not the variable.)
  * Fine 0.01 steps by default; 0.05 steps on the double-sign commands.
  * "p <num>" sets PRESS directly, no stepping needed.
  * Final printout includes RELEASE_POINT = PRESS + 0.005 — the value that was
    forgotten on 8/23 and caused the noisy 0.085 slam-step found on 8/24.

Commands (Enter after each):
  r+ / r-     nudge REST by 0.01 (arm moves and HOLDS)      r++ / r--  by 0.05
  p+ / p-     nudge PRESS by 0.01 (no movement)              p++ / p--  by 0.05
  v <num>     move arm to <num> and make it the new REST, e.g. "v -0.21"
  p <num>     set PRESS directly, e.g. "p -0.41"
  t           one test press: REST -> PRESS (0.15s) -> REST
  s           show values (REST, PRESS, swing, RELEASE_POINT)
  q           quit: settle at REST, release PWM (arm hovers — MG90S doesn't drop)

⚠ On this rig a real press on APG starts the GuliKit macro (~55s). Space
  test presses at least 60s apart so the macro finishes and re-parks the game,
  and NEVER hold the arm on APG for more than ~3s (record-mode = macro wipe).

Run:  python3 servo_quiet_cal_v2.py            (defaults -0.200 / -0.400)
      python3 servo_quiet_cal_v2.py -0.21 -0.42 (custom start REST PRESS)
"""
import sys
import time

from rpi_hardware_pwm import HardwarePWM

HWPWM_CHIP = 0
HWPWM_CHANNEL = 2
HWPWM_FREQUENCY_HZ = 50

# Current hunt calibration (shiny_detector_loop_seam.py, 8/23-8/24).
DEFAULT_REST = -0.200
DEFAULT_PRESS = -0.400
RELEASE_GAP = 0.005          # RELEASE_POINT = PRESS + RELEASE_GAP, always

PRESS_HOLD_S = 0.15          # bare press hold for the "t" test (same as v1 — this
                             # is what the -0.400 value was calibrated with on 8/23)
RETURN_SETTLE_S = 0.30
MOVE_STEP = 0.02             # step size for smooth moves
MOVE_STEP_INTERVAL = 0.012   # seconds per step (~0.6s for a 0.4 swing)


def value_to_duty(v):
    # 50Hz, 0.5-2.5ms pulse range -> duty % = 7.5 + 5.0 * v
    return 7.5 + 5.0 * v


def clamp(v):
    return max(-1.0, min(1.0, v))


class Arm:
    """Tracks where the arm actually is and moves it in small steps."""

    def __init__(self, pwm):
        self.pwm = pwm
        self.pos = None      # unknown until the first move

    def jump(self, v):
        self.pwm.change_duty_cycle(value_to_duty(v))
        self.pos = v

    def glide(self, target):
        """Move to target in MOVE_STEP increments so nothing gets slammed."""
        target = clamp(target)
        if self.pos is None:
            # First move after start: we don't know where the shaft is, so we
            # can't glide from anywhere. Applying the target directly is the
            # only option — this is why the START value must be the real REST.
            self.jump(target)
            time.sleep(0.5)
            return
        step = MOVE_STEP if target > self.pos else -MOVE_STEP
        v = self.pos
        while abs(target - v) > MOVE_STEP:
            v += step
            self.jump(v)
            time.sleep(MOVE_STEP_INTERVAL)
        self.jump(target)
        time.sleep(0.2)


def show(rest, press):
    print(f"  REST={rest:+.3f}  PRESS={press:+.3f}  swing={abs(press - rest):.3f}"
          f"  RELEASE_POINT={press + RELEASE_GAP:+.3f}")


def main():
    rest = DEFAULT_REST
    press = DEFAULT_PRESS
    if len(sys.argv) >= 2:
        rest = clamp(float(sys.argv[1]))
    if len(sys.argv) >= 3:
        press = clamp(float(sys.argv[2]))

    pwm = HardwarePWM(pwm_channel=HWPWM_CHANNEL, hz=HWPWM_FREQUENCY_HZ, chip=HWPWM_CHIP)
    pwm.start(0)
    arm = Arm(pwm)

    print("Servo Quiet Calibration v2 — hardware PWM chan 2 / GPIO 18")
    print("=" * 62)
    print("Commands: r+ r- r++ r-- p+ p- p++ p-- v <n> p <n> t s q")
    print(f"Moving to REST {rest:+.3f} and holding.")
    arm.glide(rest)
    show(rest, press)
    print()

    try:
        while True:
            try:
                cmd = input("> ").strip().lower()
            except EOFError:
                break
            if not cmd:
                continue

            if cmd in ("r+", "r-", "r++", "r--"):
                delta = 0.05 if len(cmd) == 3 else 0.01
                if "-" in cmd:
                    delta = -delta
                rest = clamp(rest + delta)
                arm.glide(rest)
                print(f"  REST={rest:+.3f} (holding)")

            elif cmd in ("p+", "p-", "p++", "p--"):
                delta = 0.05 if len(cmd) == 3 else 0.01
                if "-" in cmd:
                    delta = -delta
                press = clamp(press + delta)
                print(f"  PRESS={press:+.3f}  RELEASE_POINT={press + RELEASE_GAP:+.3f}")

            elif cmd.startswith("v "):
                try:
                    rest = clamp(float(cmd[2:].strip()))
                except ValueError:
                    print(f"  bad number: {cmd[2:]!r}")
                    continue
                arm.glide(rest)
                print(f"  moved — REST is now {rest:+.3f} (holding)")

            elif cmd.startswith("p "):
                try:
                    press = clamp(float(cmd[2:].strip()))
                except ValueError:
                    print(f"  bad number: {cmd[2:]!r}")
                    continue
                print(f"  PRESS={press:+.3f}  RELEASE_POINT={press + RELEASE_GAP:+.3f}")

            elif cmd == "t":
                if press >= rest:
                    print("  refusing: PRESS must be MORE NEGATIVE than REST on this rig")
                    continue
                print(f"  test: {rest:+.3f} -> {press:+.3f} -> {rest:+.3f}")
                arm.jump(press)
                time.sleep(PRESS_HOLD_S)
                arm.jump(rest)
                time.sleep(RETURN_SETTLE_S)
                print("  done — did the macro start on the TV? (wait ~60s before the next t)")

            elif cmd == "s":
                show(rest, press)

            elif cmd == "q":
                break

            else:
                print(f"  ?? unknown: {cmd!r}")
    finally:
        print("\nFINAL VALUES for shiny_detector_loop_seam.py:")
        print(f"  SERVO_REST    = {rest:+.3f}")
        print(f"  SERVO_PRESS   = {press:+.3f}")
        print(f"  RELEASE_POINT = {press + RELEASE_GAP:+.3f}   # = SERVO_PRESS + 0.005, ALWAYS")
        arm.glide(rest)
        time.sleep(0.5)
        pwm.change_duty_cycle(0)
        time.sleep(0.3)
        pwm.stop()
        print("PWM released — arm should stay hovering at REST.")


if __name__ == "__main__":
    main()
