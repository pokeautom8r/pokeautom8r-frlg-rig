"""
PokeAutomator Shiny Detector v0.4 — LOOP-SEAM + APG ABORT
==========================================================
Built on v0.3 (hardware-PWM + MG90S). Two behavioural changes (2026-05-24):

  1. On shiny detection, trigger_abort() now holds APG ~3.3s to interrupt
     the macro via GuliKit's recording-mode threshold. The shiny is
     preserved on the summary screen instead of being soft-reset away by
     the macro's in-cycle reset step. The PressWatchdog is DELIBERATELY
     not armed during the abort hold (the long hold IS the abort; the
     watchdog would otherwise force REST at 1.0s and prevent it).

  2. Inter-cycle random delay widened from 0–500ms to 100–2100ms. This is
     the "loop seam" variance source per the 5/16 design — wider delay
     spread = more variance in the GBA Timer1 value locked at title-press
     time = wider seed coverage = fewer duplicate seeds per hunt.

  New CLI flags for the dry-run validation in apg_abort_test_procedure.md:
    --abort-test-cycle N     force is_shiny=True on reset #N, exercising
                             the abort pipeline without needing a real
                             shiny. Destroys Auto.apg per rep — use Part B
                             of the procedure (≤5 reps).
    --timing-test            log the 4 abort latency timestamps but fire a
                             NO-OP instead of the real 3.3s hold. Used in
                             Part A — non-destructive, can run 20–30
                             cycles cheaply.
    --seam-delay-min FLOAT   override RANDOM_DELAY_MIN (seconds)
    --seam-delay-max FLOAT   override RANDOM_DELAY_MAX (seconds)

v0.3 base notes — Pi 5 hardware PWM (via rpi-hardware-pwm + RP1 chip)
instead of software PWM (gpiozero + lgpio). Built 2026-05-11 in
preparation for the MG90S + hardware-PWM upgrade session.

Prerequisites before launching this version:
  1. /boot/firmware/config.txt has: dtoverlay=pwm-pio,gpio=18
  2. Pi rebooted after the overlay change
  3. /sys/class/pwm/pwmchip0 exists (or chip2 — see HWPWM_CHIP constant)
  4. pip3 install rpi-hardware-pwm  (already installed on Pi from 5/4)
  5. SERVO_REST / RELEASE_POINT / SERVO_PRESS recalibrated for the new
     servo and PWM backend — the gpiozero values DO NOT transfer cleanly

If hardware-PWM init fails, the script logs an error and exits. Fall back
to the original shiny_detector.py to keep the rig running.

(Original v0.2 docstring preserved below.)

==================================
Works with video files for testing (no hardware needed)
or with USB capture dongle on the Pi for live detection.

Detection Methods:
  1. Sparkle Detection (frame differencing) - for static/wild encounters
  2. Star Icon Detection (pixel counting) - for summary screen (gift Pokemon)

Features:
  - Push notifications via ntfy (free, no account needed)
  - Random delay injection to avoid FRLG duplicate seed problem
  - Screenshot logging every cycle for retroactive review
  - Rolling video buffer saves last ~90s on shiny detection

Usage:
  Testing with video file:
    python shiny_detector.py --mode sparkle --source path/to/battle_video.mp4
    python shiny_detector.py --mode star --source path/to/summary_screenshot.png

  Live on Pi with capture dongle:
    python shiny_detector.py --mode sparkle --source 0 --live
    python shiny_detector.py --mode star --source 0 --live

  With notifications:
    python shiny_detector.py --mode star --source 0 --live --notify your-topic-name

Setup ntfy notifications:
  1. Install ntfy app on your phone (free, iOS + Android)
  2. Subscribe to a topic name (e.g. "your-topic-name")
  3. Use --notify with that same topic name
  4. That's it! No account needed.

Dependencies:
  pip install opencv-python numpy requests
"""

import cv2
import numpy as np
import argparse
import json
import os
import time
import random
import signal
import threading
import requests
from datetime import datetime

# ═══════════════════════════════════════════════════════
# NOTIFICATION SYSTEM (via ntfy.sh - free, no account)
# ═══════════════════════════════════════════════════════

def send_notification(topic, title, message, priority="high", tags=None):
    """
    Send a push notification via ntfy.sh.

    Uses ntfy's JSON publishing endpoint so non-ASCII characters in the title
    (e.g. the shiny star ★) round-trip cleanly. The legacy header-based API
    encodes Title as latin-1 and rejects U+2605.

    Setup:
      1. Install the ntfy app on your phone (free)
      2. Subscribe to your chosen topic name
      3. Pass that topic name to --notify when running the script
    """
    if not topic:
        return False

    # ntfy's JSON API requires priority as an int 1-5; the legacy header
    # API accepted string aliases. Translate so callers can keep using
    # the readable strings ("urgent", "high", "default", "low", "min").
    priority_map = {"max": 5, "urgent": 5, "high": 4, "default": 3,
                    "low": 2, "min": 1}
    if isinstance(priority, str):
        priority_int = priority_map.get(priority.lower(), 3)
    else:
        priority_int = int(priority)

    payload = {
        "topic": topic,
        "title": title,
        "message": message,
        "priority": priority_int,
    }
    if tags:
        # ntfy accepts a list of tag strings via JSON; the legacy header was a
        # comma-joined string. Accept either input shape from callers.
        if isinstance(tags, str):
            payload["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
        else:
            payload["tags"] = list(tags)

    try:
        response = requests.post(
            "https://ntfy.sh/",
            json=payload,
            timeout=10,
        )
        if response.status_code == 200:
            print(f"[NOTIFY] Sent: {title}")
            return True
        else:
            print(f"[NOTIFY] Failed (HTTP {response.status_code}): {response.text[:200]}")
            return False
    except Exception as e:
        print(f"[NOTIFY] Error: {e}")
        return False


# ═══════════════════════════════════════════════════════
# SERVO CALIBRATION
# ═══════════════════════════════════════════════════════
# Values calibrated 2026-05-08 after horn re-tighten following the 5/6 slip.
# Press direction reverted to NEGATIVE-relative-to-rest (matches original
# 4/27 orientation; the brief 5/5 +0.55 inversion was a slipped-horn artifact).
# Travel = 0.22 (REST -0.10 → PRESS -0.32). Used with the smooth-descent
# + immediate-detach pattern in press_button() below — that pattern is what
# makes -0.32 land softly. A bare `servo.value = -0.32` slams the button.
# ⚠️ ALL FOUR SERVO POSITION CONSTANTS BELOW NEED RECALIBRATION ⚠️
# Inherited from the SG90 + software-PWM rig (5/10 final values). With:
#   - New MG90S servo (different mechanical response, slightly different
#     idle/active angles per unit)
#   - Hardware PWM (different jitter profile; same value semantics if
#     HWPWM_MIN_PULSE_MS/HWPWM_MAX_PULSE_MS below match the prior gpiozero
#     min/max_pulse_width values, but the actual contact position may shift)
# Use the SG90 numbers as a starting point only. Expect to iterate:
#   1. Drive to SERVO_REST. Is the arm just above the button? Adjust until yes.
#   2. Slowly drive toward 0 (more negative? depends on horn orientation).
#      Find the value where arm just kisses the button. That's "contact".
#   3. SERVO_PRESS = contact - 0.005 to -0.01 (just past contact for clean tap).
#   4. RELEASE_POINT = contact (or contact + 0.005, just above) — smooth
#      descent ends here, brief-touch step jumps to SERVO_PRESS from this.
SERVO_REST = -0.200           # 8/23 spare-servo remount calibration (was -0.150)
                              # (arm at ~3-4 o'clock with current horn angle).
                              # NOTE: gravity pulls arm onto button at this
                              # REST when PWM is detached — script keeps PWM
                              # actively holding REST between presses (see
                              # press_button() — no detach-at-REST anymore).
SERVO_PRESS = -0.400          # 8/23 spare-servo remount calibration (was -0.320)
                              # the arm wasn't REACHING the button (drift); but
                              # 6/8 Steve confirmed the OPPOSITE — horn stable,
                              # arm SLAMMING through the button. The hard slam
                              # stalls the motor → current spike that browns out
                              # the USB capture card (froze 3.5s into every run).
                              # Backed off to -0.320 to TAP not slam. Tune: if the
                              # press stops registering (macro doesn't advance),
                              # nudge toward -0.33/-0.34; if still slamming/brownout,
                              # go to -0.31. (prior value -0.350 below.)
                              # --- prior: 5/26 drift-compensation update. Original 5/12
                              # value was -0.300 (5/5 presses registered when
                              # fresh), but by 5/26 morning the smooth-descent
                              # press_button() at -0.30/-0.295 stopped reaching
                              # the APG button. Verified via diagnostic
                              # (REST->PRESS direct held -0.30 still pressed,
                              # but press_button smooth descent missed). Tested
                              # -0.32 (failed), -0.34 (worked). Set to -0.35
                              # for safety margin against continued drift.
                              # If physical drift is fixed (loose horn / rig
                              # shift), revert toward -0.30. See task #11.

# 5/10 v2: BRIEF-COMMANDED-CONTACT + DETACH.
#
# Background: 5/8 (1284) and 5/9 (757) hunts both ended in APG macro-wipe
# (button held >2s by Python pause mid-descent → controller entered
# record-new-macro mode → soft-reset macro erased → hunt bricked).
#
# 5/10 v1 attempt — pure momentum coast: descend to RELEASE_POINT above
# contact, detach, let arm free-fall the last 0.01-0.03 into the button.
# Tested at -0.27, -0.28, -0.29 on the live rig — all left arm short of
# contact. The 600ms descent (50 × 12ms) is too slow to build enough
# end-of-travel velocity for the arm to coast that distance.
#
# 5/10 v2 fix: descend smoothly to RELEASE_POINT (above contact, safe),
# then a SINGLE commanded step drives to SERVO_PRESS (~0.01 past contact)
# and holds for ONE step interval (~12ms) before detach. Compared to the
# prior 5/8 pattern that drove to -0.31 across the last ~16 descent steps
# (~192ms total in the past-contact zone), this concentrates the
# vulnerable window into a single 12ms step — ~16× smaller blast radius
# for a Python pause to land on a past-contact step. Not perfectly safe
# (a multi-second pause that lands EXACTLY on the touch step is still
# possible) but a clear improvement on top of the COAST_HOLD reduction.
RELEASE_POINT = -0.395        # 8/24: 0.005 above SERVO_PRESS (-0.400). The 8/23
                              # spare-servo recal updated REST/PRESS but left this
                              # at -0.315, so the "single 0.005 step" was a 0.085
                              # full-speed jump — the loud/jittery slam Steve heard.
                              # Must always track SERVO_PRESS + 0.005.
                              # --- prior: 6/8: 0.005 above softened SERVO_PRESS (-0.320).
                              # --- prior: 5/26: 0.005 above SERVO_PRESS (-0.350).
                              # Original 5/12 value -0.295 (paired with PRESS
                              # -0.300). Smooth descent does ~99% of REST→PRESS
                              # travel (-0.150 → RELEASE_POINT), then a single
                              # 0.005 step to SERVO_PRESS is the actual button
                              # tap. Updated alongside SERVO_PRESS — see that
                              # constant's comment for the drift-compensation
                              # rationale.
                              # Exact contact point on the new arm angle
                              # wasn't probed — but 5/5 interactive jumps from
                              # REST to PRESS registered, so contact is
                              # somewhere within this descent range.

# Press motion params
PRESS_DESCENT_STEPS = 50      # finer steps = gentler arm motion
PRESS_STEP_INTERVAL = 0.012   # ~600ms total descent (50 × 12ms)
PRESS_TOUCH_HOLD = 0.020      # held-torque window at SERVO_PRESS. Bumped
                              # 5/10 from 0.012 → 0.020 to compensate for
                              # the lighter drive (-0.305 vs prior -0.31).
                              # Still 100× under 2s macro-record threshold.
PRESS_COAST_HOLD = 0.10       # detached coast at PRESS for press to register
PRESS_RETURN_SETTLE = 0.4     # seconds to settle at REST after return


# ═══════════════════════════════════════════════════════
# HARDWARE PWM SERVO WRAPPER
# ═══════════════════════════════════════════════════════
# Drop-in replacement for gpiozero.Servo, backed by Pi 5 hardware PWM via
# the rpi-hardware-pwm package. Exposes the same .value / .detach() /
# .close() API so the rest of this script (press_button, watchdog, init,
# cleanup) is unchanged from shiny_detector.py.
#
# Why: software PWM (gpiozero + lgpio) is generated by a Python-driven
# thread and is subject to kernel scheduling jitter that visibly affected
# 5/10 hunt's press consistency. Hardware PWM is generated by the RP1
# chip independent of CPU load — no jitter.

# ⚠️ Verify these against your overlay BEFORE first run on the new rig.
HWPWM_CHIP = 0                # /sys/class/pwm/pwmchip<N>. 5/4 attempt with
                              # dtoverlay=pwm-2chan exposed pwmchip0; the
                              # newer dtoverlay=pwm-pio path is untested —
                              # if init throws FileNotFoundError, try
                              # HWPWM_CHIP = 2.
HWPWM_CHANNEL = 2             # 5/12: verified GPIO 18 = PWM0_CHAN2 on this
                              # Pi 5 via `pinctrl get 18`. Default-0 was
                              # silently routing PWM to GPIO 12 (chan 0)
                              # while the servo was wired to GPIO 18.
HWPWM_FREQUENCY_HZ = 50       # Standard servo frequency. MG90S spec sheet
                              # lists 50Hz — don't change without reason.

# Pulse-width range — matches the existing gpiozero call site's
# min_pulse_width=0.5ms / max_pulse_width=2.5ms so the value=-1..+1 maps
# to the same physical pulse widths as the SG90 setup did.
HWPWM_MIN_PULSE_MS = 0.5
HWPWM_MAX_PULSE_MS = 2.5


class HardwarePWMServo:
    """gpiozero.Servo-compatible wrapper around rpi-hardware-pwm.

    Mapping (50Hz, 0.5-2.5ms pulse range):
        value -1.0 → 2.5%  duty (0.5ms pulse)
        value  0.0 → 7.5%  duty (1.5ms pulse, center)
        value +1.0 → 12.5% duty (2.5ms pulse)
        detach()   → 0%    duty (no pulses, servo released)

    .value, .detach(), .close() match gpiozero.Servo semantics so the rest
    of the codebase doesn't need to know which backend is active.
    """
    def __init__(self, channel=HWPWM_CHANNEL, chip=HWPWM_CHIP,
                 frequency_hz=HWPWM_FREQUENCY_HZ,
                 min_pulse_ms=HWPWM_MIN_PULSE_MS,
                 max_pulse_ms=HWPWM_MAX_PULSE_MS):
        from rpi_hardware_pwm import HardwarePWM
        self._pwm = HardwarePWM(pwm_channel=channel, hz=frequency_hz, chip=chip)
        self._pwm.start(0)  # start with no pulses (servo released)
        self._value = None
        # Precompute the value→duty mapping
        period_ms = 1000.0 / frequency_hz
        min_duty = (min_pulse_ms / period_ms) * 100.0
        max_duty = (max_pulse_ms / period_ms) * 100.0
        self._duty_mid = (min_duty + max_duty) / 2.0
        self._duty_swing = (max_duty - min_duty) / 2.0

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, v):
        if v is None:
            self.detach()
            return
        v_clipped = max(-1.0, min(1.0, float(v)))
        duty = self._duty_mid + self._duty_swing * v_clipped
        self._pwm.change_duty_cycle(duty)
        self._value = v_clipped

    def detach(self):
        """Stop sending PWM pulses — servo releases (same as gpiozero.detach())."""
        self._pwm.change_duty_cycle(0)

    def close(self):
        """Tear down the PWM peripheral. Idempotent."""
        try:
            self._pwm.stop()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════
# PRESS WATCHDOG
# ═══════════════════════════════════════════════════════
# Background thread that forces the servo back to REST if any single
# press_button() invocation exceeds a hard time budget. Closes the
# macro-wipe vulnerability that ended the 5/8 (1284 cycles), 5/9 (757),
# and 5/10 (410) hunts — all three failed the same way: a single press
# got stuck with the arm on the button for >2s (kernel WiFi-driver pause,
# Python GC, etc.), the Switch Online APG controller interpreted that as
# "record new macro," wiped the soft-reset macro, and the hunt bricked.
#
# This is the only software-only fix that makes macro-wipe IMPOSSIBLE by
# construction regardless of where Python pauses, because it (a) runs on
# a separate daemon thread unaffected by main-thread pauses, and (b)
# actively commands REST (not just detach) so the servo torques in the
# opposite direction and physically retracts the arm — even if the arm
# was sitting depressed on the button under gravity rather than under
# motor force. A passive arm resting on a button cannot stay there if
# another thread is commanding "go to REST" with full motor torque.
PRESS_HARD_BUDGET_S = 1.0     # Max allowed elapsed time INSIDE THE DANGER
                              # WINDOW (descent + touch + coast = ~720ms
                              # normally) before the watchdog forces REST.
                              # The danger window is "any phase where the
                              # arm could be in contact with the button" —
                              # NOT the return-settle (400ms after that the
                              # arm is moving AWAY from the button under
                              # motor torque, no macro-wipe risk).
                              #
                              # Budget = 1.0s gives ~280ms pause-headroom on
                              # top of the 720ms normal danger window before
                              # firing. After firing, force_rest() commands
                              # REST and the arm retracts in ~200ms — so
                              # total contact time stays under ~1.5s even
                              # under a kernel pause that triggers the
                              # watchdog. That's 500ms clear of the 2s APG
                              # macro-record threshold → macro stays safe.
                              # (Pauses longer than ~1.7s during the danger
                              # window can still wipe; only hardware-level
                              # watchdog would close that completely.)
WATCHDOG_CHECK_INTERVAL_S = 0.05


class PressWatchdog:
    """Forces servo to REST if any armed press exceeds hard_budget_s.

    Lifecycle:
        wd = PressWatchdog(force_rest_callback, hard_budget_s=...)
        wd.start()                       # spawn daemon thread
        # In press_button():
        wd.arm()
        try:
            ...press logic...
        finally:
            wd.disarm()                  # ALWAYS disarm, even on exception
        wd.stop()                        # on shutdown
    """
    def __init__(self, force_rest_callback,
                 hard_budget_s=PRESS_HARD_BUDGET_S,
                 check_interval_s=WATCHDOG_CHECK_INTERVAL_S):
        self.force_rest = force_rest_callback
        self.hard_budget_s = hard_budget_s
        self.check_interval_s = check_interval_s
        self._press_start = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._fired_count = 0

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name='PressWatchdog')
        self._thread.start()
        print(f"[WATCHDOG] Started — hard budget {self.hard_budget_s:.2f}s, "
              f"check every {self.check_interval_s*1000:.0f}ms")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._fired_count > 0:
            print(f"[WATCHDOG] Stopped — fired {self._fired_count}× this session "
                  f"(each fire = a press that would otherwise have wiped the macro)")

    def arm(self):
        """Begin watching the current press. Call at the start of press_button()."""
        with self._lock:
            self._press_start = time.time()

    def disarm(self):
        """Stop watching — press completed normally. Call in press_button finally."""
        with self._lock:
            self._press_start = None

    def _run(self):
        while not self._stop_event.is_set():
            time.sleep(self.check_interval_s)
            fired = False
            elapsed = 0.0
            with self._lock:
                if self._press_start is None:
                    continue
                elapsed = time.time() - self._press_start
                if elapsed > self.hard_budget_s:
                    self._press_start = None   # prevent re-firing this press
                    self._fired_count += 1
                    fired = True
            if fired:
                # Outside the lock — callback can take time and shouldn't block arm/disarm
                try:
                    self.force_rest()
                    print(f"[WATCHDOG] FIRED #{self._fired_count} — "
                          f"forced REST after {elapsed:.2f}s "
                          f"(budget {self.hard_budget_s:.2f}s exceeded). "
                          f"Likely prevented a macro-wipe.")
                except Exception as e:
                    print(f"[WATCHDOG] ERROR in force_rest callback: {e}")


def press_button(servo, watchdog=None):
    """APG press: smooth descent + brief touch + detach (5/10 v2).

    1. Smooth descent from REST (-0.10) to RELEASE_POINT (-0.30) over
       ~600ms — visually fluid, no held torque past contact.
    2. Single commanded step to SERVO_PRESS (-0.305, just past contact)
       held for ~20ms — the actual button tap.
    3. detach() — motor stops driving, arm coasts at PRESS for COAST_HOLD
       (100ms) to ensure the press registers cleanly.
    4. Smooth return to REST (8/24 — same step rate as the descent; the
       old single full-speed return swing was audibly loud/jittery).

    If `watchdog` is provided, it's armed at function entry and disarmed
    on exit (try/finally — disarm runs even if an exception is raised).
    The watchdog thread will force the servo back to REST if any single
    press_button() call takes longer than PRESS_HARD_BUDGET_S — making
    macro-wipe impossible regardless of Python pauses mid-press.

    Calibration knobs:
    - SERVO_PRESS: lower to press harder (-0.31, -0.32), raise to lighten
    - PRESS_TOUCH_HOLD: raise if press doesn't register (extends held
      torque; watchdog still bounds total)
    - PRESS_COAST_HOLD: raise if press doesn't register (no motor torque
      during coast, only gravity — safe under the watchdog)
    """
    try:
        # ── DANGER WINDOW START ── arm could approach/contact button below
        if watchdog is not None:
            watchdog.arm()
        # Smooth descent — stops at RELEASE_POINT, above contact.
        for v in np.linspace(SERVO_REST, RELEASE_POINT, PRESS_DESCENT_STEPS)[1:]:
            servo.value = float(v)
            time.sleep(PRESS_STEP_INTERVAL)
        # Brief commanded touch — single step past contact, then detach.
        servo.value = SERVO_PRESS
        time.sleep(PRESS_TOUCH_HOLD)
        servo.detach()
        time.sleep(PRESS_COAST_HOLD)
        # Smooth return to REST — mirror of the descent (8/24: the bare
        # `servo.value = SERVO_REST` full-speed swing was the residual
        # press-moment noise/jitter after the RELEASE_POINT fix; same
        # step rate as the descent = same quiet motion).
        #
        # Watchdog stays ARMED until the arm clears the button. The old
        # bare assignment was pause-proof (full retraction already
        # commanded, so a Python hang couldn't strand the arm on APG),
        # but a stepped return CAN — a pause mid-loop leaves the arm at
        # the last commanded step. Disarm only once we're back above
        # RELEASE_POINT (~4 steps, ~50ms; total armed time ~760ms, still
        # inside the 1.0s watchdog budget).
        cleared = False
        for v in np.linspace(SERVO_PRESS, SERVO_REST, PRESS_DESCENT_STEPS)[1:]:
            servo.value = float(v)
            if (not cleared and watchdog is not None
                    and float(v) >= RELEASE_POINT + 0.010):
                watchdog.disarm()
                cleared = True
            time.sleep(PRESS_STEP_INTERVAL)
        if watchdog is not None:
            watchdog.disarm()
        # ── DANGER WINDOW END ──
        time.sleep(PRESS_RETURN_SETTLE)
        # 5/12: NO detach at REST. With the MG90S + new horn angle, gravity
        # pulls the arm onto the A button when PWM is detached at REST
        # (verified during calibration — arm consistently fell back onto the
        # button after every detach). Holding REST under active PWM keeps
        # the arm safely lifted. Servo will buzz softly; that's fine for the
        # hunt rig. The detach at line ~428 (after the brief PRESS touch) is
        # KEPT — that's a mid-cycle detach during coast and is essential
        # for clean button release.
    finally:
        # Safety-net disarm — idempotent. Runs even on exception in case
        # arm() ran but disarm() didn't reach (mid-descent error etc.).
        if watchdog is not None:
            watchdog.disarm()


# ═══════════════════════════════════════════════════════
# APG-ABORT (loop-seam exit on shiny detection)  — v0.4
# ═══════════════════════════════════════════════════════
# Added 5/24. On shiny detection, drive servo to PRESS and hold for
# ABORT_HOLD_S (3.3s). The GuliKit KK3 interprets a 3-second APG hold
# as "start a new recording" — which interrupts any running macro and
# leaves the game wherever it was when the macro stopped. With proper
# timing (abort fires DURING the recorded ~8s summary dwell), the macro
# halts before its in-cycle reset and the shiny is preserved on screen.
# See 40-Rig/apg_abort_test_procedure.md for the full design + validation.
#
# Why this is safe despite looking exactly like the macro-wipe failure
# mode the PressWatchdog was built to prevent: we deliberately do NOT
# arm the watchdog during the abort. The watchdog only flags presses
# that explicitly call watchdog.arm(). trigger_abort() bypasses arm()
# entirely. The dormant watchdog never sees a press in progress, so it
# never fires.
ABORT_HOLD_S = 3.3            # APG hold duration. GuliKit's recording-mode
                              # trigger is 3.0s; +0.3s margin ensures clean
                              # activation under PWM jitter + Python slack.


def trigger_abort(servo, hold_seconds=ABORT_HOLD_S):
    """Hold APG ~3.3s to interrupt the running macro on shiny detection.

    Direct jump to SERVO_PRESS (no smooth-descent ramp — speed matters
    here; the abort must fire well within the recorded ~8s summary dwell).
    Holds for `hold_seconds`, then returns to SERVO_REST.

    Returns a dict of monotonic timestamps for the per-cycle latency log
    called for in apg_abort_test_procedure.md Part A:
        abort_decision        — moment trigger_abort() was called
        servo_reaches_PRESS   — after `servo.value = SERVO_PRESS`
        servo_released        — after `servo.value = SERVO_REST`

    ⚠ DOES NOT ARM THE PressWatchdog. The 3.3s hold IS the thing the
    watchdog is built to prevent (it interprets long holds as accidental
    macro-wipes). For abort, the long hold is intentional, so we bypass
    the watchdog by simply not calling arm(). The watchdog thread checks
    _press_start every 50ms; we never set it, so the watchdog never
    fires.
    """
    t_abort_decision = time.time()
    print(f"[ABORT] Firing — driving APG to PRESS, holding {hold_seconds:.2f}s "
          f"(watchdog NOT armed; this is intentional)")

    # Direct jump to PRESS — no descent ramp. Latency matters more than
    # press-softness here; we're not registering a single button press,
    # we're triggering GuliKit's recording mode.
    servo.value = SERVO_PRESS
    t_servo_reaches_press = time.time()

    time.sleep(hold_seconds)

    # Return to REST. Same convention as press_button(): hold REST under
    # active PWM (no detach), otherwise gravity drops the arm back onto
    # the button.
    servo.value = SERVO_REST
    t_servo_released = time.time()

    press_reach_ms = (t_servo_reaches_press - t_abort_decision) * 1000
    hold_ms = (t_servo_released - t_servo_reaches_press) * 1000
    total_ms = (t_servo_released - t_abort_decision) * 1000
    print(f"[ABORT] Complete. Press-reach: {press_reach_ms:.0f}ms, "
          f"Hold: {hold_ms:.0f}ms, Total: {total_ms:.0f}ms")

    return {
        "abort_decision": t_abort_decision,
        "servo_reaches_PRESS": t_servo_reaches_press,
        "servo_released": t_servo_released,
    }


def trigger_abort_noop(servo, hold_seconds=ABORT_HOLD_S):
    """--timing-test variant of trigger_abort(). Logs timestamps but does
    NOT actually hold the servo (no APG hold = no recording-mode trigger
    = no Auto.apg overwrite). Used in apg_abort_test_procedure.md Part A
    to measure detector→Pi→decision latency across many cycles cheaply.

    Returns the same timing dict as trigger_abort() so the cycle log can
    compute the would-be latency math without paying the destruction
    cost.
    """
    t_abort_decision = time.time()
    print(f"[TIMING-TEST] Would fire abort here. No-op (servo untouched, "
          f"Auto.apg preserved).")
    # Mark the "would have held / would have released" timestamps at
    # exactly the same moment (no real motion). Latency math uses
    # abort_decision; the other two are placeholders for log shape.
    t_servo_reaches_press = t_abort_decision
    t_servo_released = t_abort_decision
    return {
        "abort_decision": t_abort_decision,
        "servo_reaches_PRESS": t_servo_reaches_press,
        "servo_released": t_servo_released,
    }


# ═══════════════════════════════════════════════════════
# STALL GUARD
# ═══════════════════════════════════════════════════════
# If the summary-screen gate blocks this many cycles in a row, assume the
# hunt is stuck (Switch off, controller battery dead, capture card unplugged,
# macro broken, etc.) and stop pressing the servo. Without this, the script
# would keep cycling indefinitely against a black "no signal" frame — exactly
# what happened on the 2026-04-27 overnight run, where the Switch went idle
# after the controller died and the servo kept firing for ~9 hours into a
# powered-off console.
STALL_GATE_BLOCK_THRESHOLD = 5


# ═══════════════════════════════════════════════════════
# SUMMARY-GATE POLLING  (mistimed-grab fix, 2026-06-03)
# ═══════════════════════════════════════════════════════
# The macro reaches the Bulbasaur summary at a VARIABLE moment (~28-36s after
# the APG press) because the loop-seam injects a random start delay and the
# macro itself jitters. The old cycle took a SINGLE blind grab at a fixed
# ~30s; when that snapshot landed before the summary (on Oak's-lab dialogue or
# the "nickname BULBASAUR? YES/NO" prompt) or mid slide-in animation, the
# summary-screen gate blocked and the whole cycle was wasted — and a shiny on
# that cycle would have been MISSED, since no abort fires on a blocked frame.
# Overnight 6/3: 149/520 cycles (29%) were lost this way.
#
# Fix: after the initial settle wait, POLL FORWARD — re-grab every
# SUMMARY_POLL_INTERVAL_S and re-check the gate until it passes or
# SUMMARY_POLL_MAX_S elapses, then lock onto that real-summary frame.
#
# SAFETY: polling only ever pushes detection (and therefore the next APG
# press) LATER, never earlier. The critical loop-seam invariant — never tap
# APG mid-macro (the macro has no soft-reset prefix) — depends on the press
# landing AFTER the macro returns to title, so a later press is strictly safe.
# POST_SUMMARY_WAIT is still applied AFTER the gate-passing grab, so press
# timing relative to the real summary is unchanged.
SUMMARY_POLL_MAX_S = 14.0       # poll forward up to this long for the summary
SUMMARY_POLL_INTERVAL_S = 1.5   # re-grab cadence while waiting for the gate

# Two-shot capture (2026-06-22): after the poll locks the NATURE page (first
# gate-pass, ~t33), wait this long before the SECOND grab so the macro has
# flipped to the STATS page (~t37 → ~44). Measured nature dwell ~3.5s; 5s lands
# the stats grab squarely mid-stats-dwell regardless of where in the nature
# window the lock happened. Only runs on non-shiny cycles (a shiny aborts on the
# nature page and breaks before this).
STATS_GRAB_DELAY_S = 5.0


# ═══════════════════════════════════════════════════════
# HUNT STATE (running tally persists across sessions until a shiny is found)
# ═══════════════════════════════════════════════════════

HUNT_STATE_FILE = "hunt_state.json"


def load_hunt_state(state_file=HUNT_STATE_FILE):
    if not os.path.exists(state_file):
        return None
    try:
        with open(state_file) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[STATE] Could not read {state_file}: {e}")
        return None


def save_hunt_state(reset_count, hunt_start_time, state_file=HUNT_STATE_FILE):
    try:
        with open(state_file, "w") as f:
            json.dump({
                "reset_count": reset_count,
                "hunt_start_time": hunt_start_time.isoformat(),
            }, f)
    except OSError as e:
        print(f"[STATE] Warning: could not save hunt state: {e}")


def clear_hunt_state(state_file=HUNT_STATE_FILE):
    if os.path.exists(state_file):
        os.remove(state_file)


# ═══════════════════════════════════════════════════════
# SCREENSHOT LOGGING
# ═══════════════════════════════════════════════════════

def save_cycle_screenshot(frame, reset_count, screenshot_dir="hunt_screenshots",
                          prefix="reset"):
    """
    Save a screenshot every detection cycle for retroactive review.

    This is critical for catching false negatives — if the script
    misses a shiny and resets over it, the screenshot is still on disk.
    You can run a batch re-analysis later at a lower threshold.

    `prefix` selects the filename stem: "reset" for the authoritative
    stats-page frame, "nature" for the redundant nature-page census frame
    (Option A, 2026-06-22). Both share the reset number so a cycle's two
    frames sort together.

    At 720p, each screenshot is ~200KB. At 120 resets/hour:
      ~24MB/hour, ~576MB/day, ~4GB/week
    The 128GB SD card can hold months of logging.
    """
    os.makedirs(screenshot_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{screenshot_dir}/{prefix}_{reset_count:06d}_{timestamp}.png"
    cv2.imwrite(filename, frame)
    return filename


# ═══════════════════════════════════════════════════════
# RANDOM DELAY (mitigates FRLG duplicate seed problem)
# ═══════════════════════════════════════════════════════

# THE PROBLEM:
# FRLG generates its initial RNG seed from a hardware timer value
# at the exact moment you press A on the title screen. A macro
# controller replays identical timing every cycle, causing the
# game to land on the same small set of seeds repeatedly.
# PapaJefe & Anubis tested 10,000-20,000 resets and found ~43%
# were duplicate checks — effectively wasted resets.
#
# HOW THE POKEAUTOMATOR WORKS:
# The KK3 Max stores ONE macro that does everything:
#   soft reset → mash through title → select starter → open summary
# The Pi's servo ONLY presses the APG button to start/replay that macro.
# The macro's internal timing is fixed — we can't change when the
# title screen A press happens within the macro.
#
# THE MITIGATION:
# By adding a random delay (0-500ms) BEFORE the servo presses
# the APG button, we shift when the entire macro sequence begins.
# The game's RNG advances continuously, even while sitting on the
# summary screen. Starting the macro 200ms later means the soft
# reset fires at a different RNG state, which shifts when the
# title screen loads, which shifts which hardware timer frame the
# macro's A press lands on.
#
# This won't eliminate duplicates entirely — the macro's fixed
# internal timing still constrains the seed pool more than fully
# random human input would. But it should significantly reduce
# the ~43% duplicate rate seen with pure macro controllers, since
# each cycle starts from a different offset.
#
# A pure macro controller CAN'T do even this — the KK3 replays
# with identical timing including the gap between loops. The Pi
# sitting in the middle provides the only source of variability.
#
# FUTURE IMPROVEMENT:
# A second servo pressing A independently during the title screen
# (separate from the macro) would give precise random control over
# the seed-determining input. But that would require a different
# macro design that pauses at the title screen and waits for input.

# 5/24 (v0.4): widened from 0.0–0.5s to 0.1–2.1s per the 5/16 design
# and Papa Jefé's retailrng.com docs. The wider range shifts the
# title-screen-press moment over a larger Timer1 window, accessing more
# distinct GBA seeds per cycle. The 86% duplicate rate observed on the
# 5/14 hunt (2322 resets → 319 distinct stat lines) was largely caused
# by the narrow 0–500ms window. CLI override:
#   --seam-delay-min 0.05 --seam-delay-max 3.0   # wider for sparser hunts
#   --seam-delay-min 0.0  --seam-delay-max 0.5   # restore old behavior
RANDOM_DELAY_MIN = 0.100  # seconds (100ms — faster than human reaction)
RANDOM_DELAY_MAX = 2.100  # seconds (2.1s — ~126 frames at 60fps)


# ═══════════════════════════════════════════════════════
# CONFIGURATION - Adjust these per game / encounter
# ═══════════════════════════════════════════════════════

CONFIG = {
    "frlg_sparkle": {
        # Region of screen to monitor for sparkle (x, y, w, h as ratios 0-1)
        # Focus on the area where the opposing Pokemon sprite appears
        "detect_region": (0.45, 0.05, 0.45, 0.45),  # right-center of screen (opponent side)
        
        # How many frames to capture during the detection window
        "capture_fps": 15,
        "detection_window_seconds": 2.0,
        
        # Frame difference threshold - brightness change that counts as "sparkle"
        # Sparkle adds bright white/yellow pixels; normal entrance is much calmer
        "diff_threshold": 40,        # pixel brightness change to count as "changed"
        "sparkle_pixel_ratio": 0.02, # % of pixels that must change (2% of region)
        "sparkle_frame_count": 3,    # number of frames that must show sparkle
        
        "game": "FRLG",
        "description": "FireRed/LeafGreen sparkle detection for static encounters"
    },
    
    "frlg_star": {
        # Region where the shiny star appears on the summary screen.
        # Coordinates are RELATIVE TO THE GAME-ACTIVE AREA (after letterbox
        # detection), not the full capture frame — the Switch outputs FRLG
        # via Switch Online with pillar-bars on a 1920x1080 capture.
        "detect_region": (0.41, 0.21, 0.08, 0.08),  # star at ~43-45% X, 23-26% Y per calibration

        # Color range for the star (yellow-gold in FRLG)
        # HSV ranges - tuned from pixel sampling of actual screenshots
        "star_hue_range": (20, 32),      # yellow-gold hue
        "star_sat_range": (80, 255),     # saturated
        "star_val_range": (150, 255),    # bright

        # How many matching pixels = star present
        "star_pixel_threshold": 50,

        # ── Summary-screen gate ─────────────────────────────────────────
        # Before counting star pixels, confirm we're actually on the
        # Pokémon Info / summary screen by sampling the lavender sprite-
        # window border on the left half of the game area. Prevents the
        # 2026-04-20 false positive where an NPC's blonde hair in Oak's
        # lab triggered the detector.
        "gate_region": (0.02, 0.10, 0.06, 0.55),  # left edge of sprite window
        "gate_hue_range": (130, 165),
        "gate_hue_range_alt": (80, 95),   # 9/19 SHINY FIX: FRLG draws the sprite-window border CYAN on a shiny
                                          # (#13,102/#15,104: 0 lavender / 7,880 cyan px). Without this the gate rejected every shiny.
        "gate_sat_range": (30, 180),
        "gate_val_range": (160, 255),
        "gate_pixel_threshold": 500,
        # 9/29 EEVEE FIX: the Celadon Mansion room (Eevee macro's parked screen) has
        # light-blue trim in the gate strip (passes the cyan range) AND a yellow floor
        # in the star box (hue 20-32) -> gate PASS + star 100% on an overworld frame.
        # Second, species-independent test: the top-left corner INSIDE the sprite
        # window is pure white on every real summary page (INFO + SKILLS, shiny or
        # not: 5766/5766 px) and <=20% white on the room / party menu / title.
        "gate_white_region": (0.025, 0.22, 0.06, 0.06),
        "gate_white_min_frac": 0.80,

        "game": "FRLG",
        "description": "FireRed/LeafGreen star icon on summary screen"
    },

    "frlg_magikarp": {
        # Species-agnostic clone of frlg_star. The shiny star appears in the
        # SAME summary-screen position for every FRLG species, so Magikarp
        # reuses the starter's proven star + gate detection. Kept as its own
        # named profile so it can be re-tuned on real Magikarp frames later
        # WITHOUT ever touching frlg_star's frozen numbers.
        "detect_region": (0.41, 0.21, 0.08, 0.08),
        "star_hue_range": (20, 32),
        "star_sat_range": (80, 255),
        "star_val_range": (150, 255),
        "star_pixel_threshold": 50,
        "gate_region": (0.02, 0.10, 0.06, 0.55),
        "gate_hue_range": (130, 165),
        "gate_hue_range_alt": (80, 95),   # 9/19 SHINY FIX: FRLG draws the sprite-window border CYAN on a shiny
                                          # (#13,102/#15,104: 0 lavender / 7,880 cyan px). Without this the gate rejected every shiny.
        "gate_sat_range": (30, 180),
        "gate_val_range": (160, 255),
        "gate_pixel_threshold": 500,
        # 9/29 EEVEE FIX: the Celadon Mansion room (Eevee macro's parked screen) has
        # light-blue trim in the gate strip (passes the cyan range) AND a yellow floor
        # in the star box (hue 20-32) -> gate PASS + star 100% on an overworld frame.
        # Second, species-independent test: the top-left corner INSIDE the sprite
        # window is pure white on every real summary page (INFO + SKILLS, shiny or
        # not: 5766/5766 px) and <=20% white on the room / party menu / title.
        "gate_white_region": (0.025, 0.22, 0.06, 0.06),
        "gate_white_min_frac": 0.80,
        "game": "FRLG",
        "description": "FRLG Magikarp salesman hunt - summary-screen star (clone of frlg_star)"
    },

    "frlg_eevee": {
        # 2026-07-21. Third species profile, cloned from frlg_star the same way
        # frlg_magikarp was. The shiny star sits in the SAME summary-screen slot
        # for every FRLG species (Magikarp proved the clone transfers cleanly
        # over 4,110 resets), so the star + gate numbers start identical.
        #
        # Kept as its own named profile so the Celadon Mansion Eevee hunt can be
        # re-tuned on real Eevee frames WITHOUT ever touching frlg_star's or
        # frlg_magikarp's frozen values.
        #
        # NOTE — Eevee is the Lv25 gift mon, which is the POINT of this hunt:
        # the stats page carries a far wider signature space (~11M vs Magikarp's
        # ~15k at Lv5), so seed uniqueness becomes provable. Detection itself is
        # unchanged — a star is a star.
        "detect_region": (0.41, 0.21, 0.08, 0.08),
        "star_hue_range": (20, 32),
        "star_sat_range": (80, 255),
        "star_val_range": (150, 255),
        "star_pixel_threshold": 50,
        "gate_region": (0.02, 0.10, 0.06, 0.55),
        "gate_hue_range": (130, 165),
        "gate_hue_range_alt": (80, 95),   # 9/19 SHINY FIX: FRLG draws the sprite-window border CYAN on a shiny
                                          # (#13,102/#15,104: 0 lavender / 7,880 cyan px). Without this the gate rejected every shiny.
        "gate_sat_range": (30, 180),
        "gate_val_range": (160, 255),
        "gate_pixel_threshold": 500,
        # 9/29 EEVEE FIX: the Celadon Mansion room (Eevee macro's parked screen) has
        # light-blue trim in the gate strip (passes the cyan range) AND a yellow floor
        # in the star box (hue 20-32) -> gate PASS + star 100% on an overworld frame.
        # Second, species-independent test: the top-left corner INSIDE the sprite
        # window is pure white on every real summary page (INFO + SKILLS, shiny or
        # not: 5766/5766 px) and <=20% white on the room / party menu / title.
        "gate_white_region": (0.025, 0.22, 0.06, 0.06),
        "gate_white_min_frac": 0.80,
        # 9/29 PARK GUARD: this macro's loop seam is IN-GAME (parked in the Celadon
        # Mansion room in front of the Eevee NPC), so the APG press is only safe when
        # that room is on screen. Pressing anywhere else replays the macro's inputs
        # against the wrong screen - on 9/29 a press that landed on the title menu
        # walked into NEW GAME. Signature = three floor patches that are >=99% yellow
        # (hue 20-32) on every room frame and 0% on title/menus/summary pages.
        "park_regions": [(0.06, 0.62, 0.10, 0.10), (0.84, 0.62, 0.10, 0.10),
                         (0.30, 0.80, 0.08, 0.06)],
        "park_hue_range": (20, 32),
        "park_min_frac": 0.80,
        "game": "FRLG",
        "description": "FRLG Celadon Mansion Eevee gift hunt - summary-screen star (clone of frlg_star)"
    },
    "frlg_hitmonlee": {
        # 2026-10-07. Fourth species profile, cloned from frlg_eevee (same gift-mon
        # design: in-game loop seam, fixed-level gift, park guard). Saffron Fighting
        # Dojo Hitmonlee hunt: the macro takes the LEFT Pokeball, opens the summary,
        # soft-resets and reloads the save parked in front of the ball.
        # Star + gate numbers identical to the other FRLG profiles (a star is a star).
        "detect_region": (0.41, 0.21, 0.08, 0.08),
        "star_hue_range": (20, 32),
        "star_sat_range": (80, 255),
        "star_val_range": (150, 255),
        "star_pixel_threshold": 50,
        "gate_region": (0.02, 0.10, 0.06, 0.55),
        "gate_hue_range": (130, 165),
        "gate_hue_range_alt": (80, 95),   # shiny = CYAN sprite-window border (9/19 fix)
        "gate_sat_range": (30, 180),
        "gate_val_range": (160, 255),
        "gate_pixel_threshold": 500,
        "gate_white_region": (0.025, 0.22, 0.06, 0.06),   # 9/29 white-background gate
        "gate_white_min_frac": 0.80,
        # PARK GUARD: the Dojo floor is khaki (hue 30, sat 92-124, val 156-232), inside
        # the same yellow signature the Celadon room used. Three floor patches measured
        # 1.00 on the parked frame (dojo_parked.png, 10/7): left + right lower floor
        # (clear of the two students at y>=0.78) and the left mid band.
        "park_regions": [(0.06, 0.62, 0.10, 0.10), (0.84, 0.62, 0.10, 0.10),
                         (0.06, 0.40, 0.10, 0.10)],
        # 10/7: the Switch dims the screen after 5 min idle (burn-in reduction);
        # dimmed floor = hue 31-32 / sat 118 / val 31-56, so the guard tolerates
        # it (hue to 36, val floor 25). Non-room screens (title, menus, INFO,
        # SKILLS, reset) stay <=0.39 on any patch at these floors (250 Eevee frames).
        "park_hue_range": (20, 36),
        "park_val_min": 25,
        "park_min_frac": 0.80,
        "game": "FRLG",
        "description": "FRLG Saffron Dojo Hitmonlee gift hunt - summary-screen star (clone of frlg_eevee)"
    }
}


# ═══════════════════════════════════════════════════════
# GAME-AREA DETECTION + SUMMARY-SCREEN GATE
# ═══════════════════════════════════════════════════════
# Switch outputs FRLG (via Switch Online) with letterbox/pillar-bars on the
# capture card's 1920x1080 frame. Detection regions need to be anchored to
# the game-active rectangle, not the full frame, or every relative coord
# is shifted by the bar width.

LETTERBOX_GRAY_THRESHOLD = 15


def find_game_bounds(frame):
    """Return (x, y, w, h) of the non-letterbox region of `frame`."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, LETTERBOX_GRAY_THRESHOLD, 255, cv2.THRESH_BINARY)
    coords = cv2.findNonZero(binary)
    if coords is None:
        return 0, 0, frame.shape[1], frame.shape[0]
    return cv2.boundingRect(coords)


PARK_GUARD_MAX_S = 20.0        # how long to wait for the parked screen before stopping
PARK_GUARD_INTERVAL_S = 1.0    # re-grab cadence while waiting


def is_parked(frame, config_name="frlg_star"):
    """9/29 PARK GUARD. Return (parked, fracs). A profile without "park_regions"
    is always "parked" (its macro seams at the title, where no guard applies).
    """
    cfg = CONFIG[config_name]
    regions = cfg.get("park_regions")
    if not regions:
        return True, []
    gx, gy, gw, gh = find_game_bounds(frame)
    h_lo, h_hi = cfg.get("park_hue_range", (20, 32))
    fracs = []
    for rx, ry, rw, rh in regions:
        roi = frame[gy + int(ry * gh):gy + int((ry + rh) * gh),
                    gx + int(rx * gw):gx + int((rx + rw) * gw)]
        if roi.size == 0:
            fracs.append(0.0)
            continue
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        # 10/7: floors are per-profile so a dim-tolerant signature (Switch idle
        # screen-dim after 5 min scales V to ~25%) can be used; defaults keep
        # the 9/29 Eevee behaviour byte-for-byte.
        s_min = cfg.get("park_sat_min", 80)
        v_min = cfg.get("park_val_min", 150)
        m = cv2.inRange(hsv, np.array([h_lo, s_min, v_min]), np.array([h_hi, 255, 255]))
        fracs.append(cv2.countNonZero(m) / float(roi.shape[0] * roi.shape[1]))
    return all(f >= cfg.get("park_min_frac", 0.80) for f in fracs), fracs


def wait_until_parked(cap, config_name, max_s=PARK_GUARD_MAX_S):
    """Poll the capture until the parked-room signature is on screen.
    Returns (parked, waited_s, fracs). Profiles without park_regions return
    immediately as parked so title-seam macros are unaffected.
    """
    if not CONFIG[config_name].get("park_regions"):
        return True, 0.0, []
    t0 = time.time()
    fracs = []
    while True:
        for _ in range(3):
            cap.read()
        ok, frame = cap.read()
        if ok and frame is not None:
            parked, fracs = is_parked(frame, config_name)
            if parked:
                return True, time.time() - t0, fracs
        if time.time() - t0 >= max_s:
            return False, time.time() - t0, fracs
        time.sleep(PARK_GUARD_INTERVAL_S)


def is_summary_screen(frame, config_name="frlg_star"):
    """
    Return (passed, purple_pixels) indicating whether `frame` looks like the
    FRLG summary screen. Counts lavender-purple pixels in the region where
    the sprite-window's left border appears on the summary screen — that
    border is unique to the Pokémon Info page in FRLG.
    """
    cfg = CONFIG[config_name]
    gx, gy, gw, gh = find_game_bounds(frame)
    rx, ry, rw, rh = cfg["gate_region"]
    x = gx + int(rx * gw)
    y = gy + int(ry * gh)
    rw_px = int(rw * gw)
    rh_px = int(rh * gh)
    roi = frame[y:y+rh_px, x:x+rw_px]
    if roi.size == 0:
        return False, 0

    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    h_lo, h_hi = cfg["gate_hue_range"]
    s_lo, s_hi = cfg["gate_sat_range"]
    v_lo, v_hi = cfg["gate_val_range"]
    mask = cv2.inRange(hsv,
                       np.array([h_lo, s_lo, v_lo]),
                       np.array([h_hi, s_hi, v_hi]))
    # 9/19 SHINY FIX: a shiny's sprite-window border is CYAN, not lavender.
    # Two shiny Bulbasaur (#13,102 and #15,104) scored 0 lavender / 7,880 cyan
    # here, failed the gate, and the star was never counted. Accept both.
    alt = cfg.get("gate_hue_range_alt")
    if alt:
        mask_alt = cv2.inRange(hsv,
                               np.array([alt[0], s_lo, v_lo]),
                               np.array([alt[1], s_hi, v_hi]))
        mask = cv2.bitwise_or(mask, mask_alt)
    purple = int(cv2.countNonZero(mask))
    passed = purple >= cfg["gate_pixel_threshold"]
    # 9/29 EEVEE FIX: second test - the sprite window's background must be white.
    # The border-colour test alone passes the Celadon Mansion room (light-blue
    # trim counts as "cyan"), and that room's yellow floor then trips the star
    # box. A real summary page is 100% white in this corner; the room, the party
    # menu and page transitions are <=20% white.
    wreg = cfg.get("gate_white_region")
    if passed and wreg:
        wx, wy, ww, wh = wreg
        wroi = frame[gy + int(wy * gh):gy + int((wy + wh) * gh),
                     gx + int(wx * gw):gx + int((wx + ww) * gw)]
        if wroi.size == 0:
            passed = False
        else:
            whsv = cv2.cvtColor(wroi, cv2.COLOR_BGR2HSV)
            wmask = cv2.inRange(whsv, np.array([0, 0, 200]), np.array([180, 40, 255]))
            wfrac = cv2.countNonZero(wmask) / float(wroi.shape[0] * wroi.shape[1])
            passed = wfrac >= cfg.get("gate_white_min_frac", 0.80)
    return passed, purple


# ═══════════════════════════════════════════════════════
# SPARKLE DETECTION (Frame Differencing)
# ═══════════════════════════════════════════════════════

def detect_sparkle(source, config_name="frlg_sparkle", debug=False, output_dir=None):
    """
    Detect shiny sparkle animation by measuring frame-to-frame brightness changes.
    
    The sparkle animation creates rapid bright flashes that produce large
    differences between consecutive frames. Normal encounters have smooth,
    minimal frame transitions.
    
    Returns: (is_shiny: bool, confidence: float, debug_data: dict)
    """
    cfg = CONFIG[config_name]
    
    # Open video source
    if isinstance(source, str) and not source.isdigit():
        cap = cv2.VideoCapture(source)  # video file
        print(f"[SPARKLE] Analyzing video file: {source}")
    else:
        cap = cv2.VideoCapture(int(source) if isinstance(source, str) else source)
        print(f"[SPARKLE] Reading from capture device: {source}")
    
    if not cap.isOpened():
        print("[ERROR] Could not open video source!")
        return False, 0.0, {}
    
    # Get video properties
    fps = cap.get(cv2.CAP_PROP_FPS) or cfg["capture_fps"]
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"[SPARKLE] Video: {width}x{height} @ {fps:.0f}fps, {total_frames} frames")
    
    # Calculate detection region in pixels
    rx, ry, rw, rh = cfg["detect_region"]
    region = (
        int(rx * width), int(ry * height),
        int(rw * width), int(rh * height)
    )
    print(f"[SPARKLE] Detection region: x={region[0]}, y={region[1]}, "
          f"w={region[2]}, h={region[3]}")
    
    # Read frames and compute differences
    prev_gray = None
    diff_scores = []
    sparkle_frames = []
    frame_count = 0
    frames_to_analyze = int(cfg["detection_window_seconds"] * fps)
    
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    
    while cap.isOpened() and frame_count < frames_to_analyze:
        ret, frame = cap.read()
        if not ret:
            break
        
        # Crop to detection region
        x, y, w, h = region
        roi = frame[y:y+h, x:x+w]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        
        if prev_gray is not None:
            # Compute absolute difference between consecutive frames
            diff = cv2.absdiff(prev_gray, gray)
            
            # Count pixels that changed significantly
            changed_pixels = np.sum(diff > cfg["diff_threshold"])
            total_pixels = gray.shape[0] * gray.shape[1]
            change_ratio = changed_pixels / total_pixels
            
            diff_scores.append(change_ratio)
            
            is_sparkle_frame = change_ratio > cfg["sparkle_pixel_ratio"]
            sparkle_frames.append(is_sparkle_frame)
            
            if debug:
                print(f"  Frame {frame_count:3d}: change={change_ratio:.4f} "
                      f"({'★ SPARKLE' if is_sparkle_frame else '  normal'})")
            
            # Save debug images
            if output_dir:
                # Colorize the diff for visibility
                diff_color = cv2.applyColorMap(diff * 4, cv2.COLORMAP_HOT)
                cv2.imwrite(f"{output_dir}/frame_{frame_count:03d}_roi.png", roi)
                cv2.imwrite(f"{output_dir}/frame_{frame_count:03d}_diff.png", diff_color)
        
        prev_gray = gray
        frame_count += 1
    
    cap.release()
    
    if not diff_scores:
        print("[SPARKLE] No frames analyzed!")
        return False, 0.0, {}
    
    # Analyze results
    sparkle_count = sum(sparkle_frames)
    max_diff = max(diff_scores)
    avg_diff = np.mean(diff_scores)
    
    # Detection: Did enough frames show sparkle-level changes?
    is_shiny = sparkle_count >= cfg["sparkle_frame_count"]
    
    # Confidence based on how strongly the sparkle signal appeared
    if sparkle_count > 0:
        confidence = min(1.0, sparkle_count / (cfg["sparkle_frame_count"] * 2))
    else:
        confidence = 0.0
    
    debug_data = {
        "frames_analyzed": frame_count,
        "sparkle_frames": sparkle_count,
        "max_change_ratio": max_diff,
        "avg_change_ratio": avg_diff,
        "diff_scores": diff_scores,
        "threshold_used": cfg["sparkle_pixel_ratio"],
    }
    
    print(f"\n[SPARKLE] ═══ RESULTS ═══")
    print(f"  Frames analyzed:  {frame_count}")
    print(f"  Sparkle frames:   {sparkle_count} / {cfg['sparkle_frame_count']} needed")
    print(f"  Max change ratio: {max_diff:.4f} (threshold: {cfg['sparkle_pixel_ratio']})")
    print(f"  Avg change ratio: {avg_diff:.4f}")
    
    if is_shiny:
        print(f"\n  ★★★ SHINY DETECTED! (confidence: {confidence:.0%}) ★★★")
    else:
        print(f"\n  No shiny. (confidence of shiny: {confidence:.0%})")
    
    return is_shiny, confidence, debug_data


# ═══════════════════════════════════════════════════════
# STAR ICON DETECTION (Pixel Counting)
# ═══════════════════════════════════════════════════════

def detect_star(source, config_name="frlg_star", debug=False, output_dir=None):
    """
    Detect shiny star icon on the Pokemon summary screen.
    
    Looks for a cluster of reddish-orange pixels in the known star location.
    The summary screen background is blue/green, making the star easy to isolate.
    
    Returns: (is_shiny: bool, confidence: float, debug_data: dict)
    """
    cfg = CONFIG[config_name]
    
    # Load image (screenshot or grab from video)
    if isinstance(source, str) and os.path.isfile(source):
        frame = cv2.imread(source)
        print(f"[STAR] Analyzing image: {source}")
    else:
        cap = cv2.VideoCapture(int(source) if isinstance(source, str) else source)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        # Skip initial frames — capture card needs warmup to lock signal
        time.sleep(2)
        for _ in range(30):
            cap.read()
        ret, frame = cap.read()
        cap.release()
        if not ret:
            print("[ERROR] Could not capture frame!")
            return False, 0.0, {}
        print(f"[STAR] Captured frame from device: {source}")
    
    height, width = frame.shape[:2]
    print(f"[STAR] Image: {width}x{height}")

    # Anchor to the game-active rectangle (handles pillar-bars on live capture).
    gx, gy, gw, gh = find_game_bounds(frame)
    if (gw, gh) != (width, height):
        print(f"[STAR] Game area: x={gx}, y={gy}, {gw}x{gh} (letterbox detected)")

    # Gate: only run star detection on actual summary frames.
    on_summary, purple = is_summary_screen(frame, config_name)
    print(f"[STAR] Summary-screen gate: {purple} purple pixels "
          f"(threshold {cfg['gate_pixel_threshold']}) -> "
          f"{'PASS' if on_summary else 'BLOCK'}")
    if not on_summary:
        debug_data = {
            "matching_pixels": 0,
            "threshold": cfg["star_pixel_threshold"],
            "gate_passed": False,
            "gate_purple_pixels": purple,
            "gate_threshold": cfg["gate_pixel_threshold"],
        }
        print(f"\n[STAR] ═══ RESULTS ═══")
        print(f"  Gate blocked detection (not a summary screen).")
        return False, 0.0, debug_data

    rx, ry, rw, rh = cfg["detect_region"]
    x = gx + int(rx * gw)
    y = gy + int(ry * gh)
    w = int(rw * gw)
    h = int(rh * gh)
    roi = frame[y:y+h, x:x+w]

    print(f"[STAR] Detection region: x={x}, y={y}, w={w}, h={h}")

    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    h_lo, h_hi = cfg["star_hue_range"]
    s_lo, s_hi = cfg["star_sat_range"]
    v_lo, v_hi = cfg["star_val_range"]
    mask = cv2.inRange(hsv,
                       np.array([h_lo, s_lo, v_lo]),
                       np.array([h_hi, s_hi, v_hi]))

    matching_pixels = int(cv2.countNonZero(mask))
    total_pixels = w * h

    is_shiny = matching_pixels >= cfg["star_pixel_threshold"]
    confidence = min(1.0, matching_pixels / (cfg["star_pixel_threshold"] * 3))

    debug_data = {
        "matching_pixels": matching_pixels,
        "total_pixels": total_pixels,
        "threshold": cfg["star_pixel_threshold"],
        "match_ratio": matching_pixels / total_pixels if total_pixels > 0 else 0,
        "gate_passed": True,
        "gate_purple_pixels": purple,
        "gate_threshold": cfg["gate_pixel_threshold"],
    }

    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        cv2.imwrite(f"{output_dir}/star_roi.png", roi)
        cv2.imwrite(f"{output_dir}/star_mask.png", mask)
        annotated = frame.copy()
        cv2.rectangle(annotated, (gx, gy), (gx+gw, gy+gh), (0, 255, 0), 2)
        cv2.rectangle(annotated, (x, y), (x+w, y+h), (0, 255, 255), 2)
        cv2.imwrite(f"{output_dir}/star_annotated.png", annotated)

    print(f"\n[STAR] ═══ RESULTS ═══")
    print(f"  Matching pixels: {matching_pixels} / {cfg['star_pixel_threshold']} needed")
    print(f"  Match ratio:     {debug_data['match_ratio']:.4f}")

    if is_shiny:
        print(f"\n  ★★★ SHINY STAR DETECTED! (confidence: {confidence:.0%}) ★★★")
    else:
        print(f"\n  No star found. (confidence: {confidence:.0%})")

    return is_shiny, confidence, debug_data


def unverified_probe(frame, config_name="frlg_star"):
    """9/19 SHINY FIX — second opinion for a cycle whose summary gate did NOT pass.

    Counts star pixels in the detect box with the gate bypassed, plus shiny- and
    normal-palette pixels inside the sprite window (same box on both summary
    pages). run_live() logs these as [UNVERIFIED] so a human/watcher sees every
    gate-failed cycle, and fires the abort only when star AND palette agree
    (a real shiny scored 923 star px / ~10k shiny-palette px; the 4/20 blonde-hair
    false positive had no shiny-palette pixels at all).
    """
    cfg = CONFIG[config_name]
    gx, gy, gw, gh = find_game_bounds(frame)
    rx, ry, rw, rh = cfg["detect_region"]
    roi = frame[gy + int(ry * gh):gy + int((ry + rh) * gh),
                gx + int(rx * gw):gx + int((rx + rw) * gw)]
    star = 0
    if roi.size:
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        star = int(cv2.countNonZero(cv2.inRange(
            hsv,
            np.array([cfg["star_hue_range"][0], cfg["star_sat_range"][0], cfg["star_val_range"][0]]),
            np.array([cfg["star_hue_range"][1], cfg["star_sat_range"][1], cfg["star_val_range"][1]]))))
    # Sprite window ≈ 1.3–50% X, 18–67% Y of the game area.
    sw = frame[gy + int(0.18 * gh):gy + int(0.67 * gh),
               gx + int(0.013 * gw):gx + int(0.50 * gw)]
    shiny_px = normal_px = 0

    def _count(img, bgr, tol=20):
        lo = np.clip(np.array(bgr) - tol, 0, 255).astype(np.uint8)
        hi = np.clip(np.array(bgr) + tol, 0, 255).astype(np.uint8)
        return int(cv2.countNonZero(cv2.inRange(img, lo, hi)))
    if sw.size:
        for bgr in ((0, 198, 110), (0, 255, 176)):        # shiny body / bulb (BGR)
            shiny_px += _count(sw, bgr)
        for bgr in ((155, 154, 100), (172, 243, 106)):    # normal body / bulb (BGR)
            normal_px += _count(sw, bgr)
    return {"star_px": star, "shiny_px": shiny_px, "normal_px": normal_px}


def detect_star_frame(frame, config_name="frlg_star"):
    """
    Run star detection on an already-captured frame.
    Used by run_live() to avoid reopening the capture device each cycle.
    Returns: (is_shiny: bool, confidence: float, debug_data: dict)
    """
    cfg = CONFIG[config_name]

    # Anchor to the game-active rectangle so pillar-bars on live captures
    # don't shift the star region off-target.
    gx, gy, gw, gh = find_game_bounds(frame)

    # Gate: skip star detection unless we're actually on the summary screen.
    # The sprite-window border is purple on the Pokémon Info page and
    # essentially nowhere else in FRLG, so this rejects overworld/lab
    # frames where yellow NPC pixels would otherwise false-positive.
    on_summary, purple = is_summary_screen(frame, config_name)
    if not on_summary:
        return False, 0.0, {
            "matching_pixels": 0,
            "threshold": cfg["star_pixel_threshold"],
            "gate_passed": False,
            "gate_purple_pixels": purple,
            "gate_threshold": cfg["gate_pixel_threshold"],
        }

    rx, ry, rw, rh = cfg["detect_region"]
    x = gx + int(rx * gw)
    y = gy + int(ry * gh)
    w = int(rw * gw)
    h = int(rh * gh)
    roi = frame[y:y+h, x:x+w]

    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    h_lo, h_hi = cfg["star_hue_range"]
    s_lo, s_hi = cfg["star_sat_range"]
    v_lo, v_hi = cfg["star_val_range"]
    mask = cv2.inRange(hsv,
                       np.array([h_lo, s_lo, v_lo]),
                       np.array([h_hi, s_hi, v_hi]))

    matching_pixels = int(cv2.countNonZero(mask))
    is_shiny = matching_pixels >= cfg["star_pixel_threshold"]
    confidence = min(1.0, matching_pixels / (cfg["star_pixel_threshold"] * 3))

    debug_data = {
        "matching_pixels": matching_pixels,
        "total_pixels": w * h,
        "threshold": cfg["star_pixel_threshold"],
        "gate_passed": True,
        "gate_purple_pixels": purple,
        "gate_threshold": cfg["gate_pixel_threshold"],
    }

    if is_shiny:
        print(f"\n  ★★★ SHINY STAR DETECTED! (confidence: {confidence:.0%}) ★★★")

    return is_shiny, confidence, debug_data


def grab_summary_frame(cap, config_name="frlg_star", poll=True):
    """
    Grab a frame from the capture device and, when `poll` is set, keep
    re-grabbing until the summary-screen gate passes or SUMMARY_POLL_MAX_S
    elapses (the mistimed-grab fix — see the SUMMARY-GATE POLLING block above).

    Each read flushes the capture buffer first so the gate always tests "now",
    never a stale buffered frame.

    Returns (frame, gate_passed, polls):
      frame       — the gate-passing frame if the poll succeeded, else the last
                    frame read (so the stall guard and screenshot review still
                    see a representative frame), or None if the read failed.
      gate_passed — whether the returned frame is a real summary screen.
      polls       — how many EXTRA grabs the poll spent waiting (0 = the very
                    first grab was already on the summary).

    NOTE (2026-06-22): the re-recorded macro shows TWO summary pages that both
    pass this gate — the NATURE page (POKéMON INFO, ~t33, ~3.5s) first, then the
    STATS page (POKéMON SKILLS, ~t37, ~7s). The poll locks onto the FIRST
    gate-pass, i.e. the nature page. run_live() takes a SECOND grab a few seconds
    later to capture the stats page (the two-shot stats+nature census).
    """
    def _read_fresh():
        # Drop buffered frames, keep the latest, so the gate sees the present.
        for _ in range(5):
            cap.read()
        ret, fr = cap.read()
        return fr if ret else None

    frame = _read_fresh()
    if frame is None:
        return None, False, 0

    gate_passed, _ = is_summary_screen(frame, config_name)
    if not poll:
        return frame, gate_passed, 0

    deadline = time.time() + SUMMARY_POLL_MAX_S
    polls = 0
    while not gate_passed and time.time() < deadline:
        time.sleep(SUMMARY_POLL_INTERVAL_S)
        polls += 1
        fr = _read_fresh()
        if fr is None:
            continue
        frame = fr
        gate_passed, _ = is_summary_screen(frame, config_name)

    return frame, gate_passed, polls


# ═══════════════════════════════════════════════════════
# CALIBRATION TOOL
# ═══════════════════════════════════════════════════════

def calibrate(source, output_dir="calibration_output"):
    """
    Interactive calibration helper.
    
    Takes a screenshot/video and helps you find the right detection regions
    and color thresholds. Saves annotated images showing what the detector sees.
    
    Usage:
      python shiny_detector.py --calibrate --source screenshot.png
    """
    if isinstance(source, str) and os.path.isfile(source):
        frame = cv2.imread(source)
    else:
        cap = cv2.VideoCapture(int(source) if isinstance(source, str) else source)
        ret, frame = cap.read()
        cap.release()
        if not ret:
            print("[ERROR] Could not load source!")
            return
    
    os.makedirs(output_dir, exist_ok=True)
    height, width = frame.shape[:2]
    
    print(f"\n═══ CALIBRATION TOOL ═══")
    print(f"Image size: {width}x{height}")
    print(f"\nSaving analysis to: {output_dir}/")
    
    # Save full frame with grid overlay
    grid = frame.copy()
    # Draw 10% gridlines
    for i in range(1, 10):
        x_pos = int(width * i / 10)
        y_pos = int(height * i / 10)
        cv2.line(grid, (x_pos, 0), (x_pos, height), (0, 255, 0), 1)
        cv2.line(grid, (0, y_pos), (width, y_pos), (0, 255, 0), 1)
        cv2.putText(grid, f"{i*10}%", (x_pos+2, 15), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1)
        cv2.putText(grid, f"{i*10}%", (2, y_pos-5), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1)
    cv2.imwrite(f"{output_dir}/grid_overlay.png", grid)
    
    # HSV analysis of the full frame
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    
    # Show different color channels
    cv2.imwrite(f"{output_dir}/channel_hue.png", hsv[:,:,0] * 2)  # scale for visibility
    cv2.imwrite(f"{output_dir}/channel_saturation.png", hsv[:,:,1])
    cv2.imwrite(f"{output_dir}/channel_value.png", hsv[:,:,2])
    
    # Show detection regions from all configs
    annotated = frame.copy()
    for name, cfg in CONFIG.items():
        rx, ry, rw, rh = cfg["detect_region"]
        x1, y1 = int(rx * width), int(ry * height)
        x2, y2 = int((rx+rw) * width), int((ry+rh) * height)
        color = (0, 255, 255) if "star" in name else (255, 0, 255)
        cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
        cv2.putText(annotated, name, (x1, y1-5), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
    cv2.imwrite(f"{output_dir}/detection_regions.png", annotated)
    
    print(f"\n  Saved grid_overlay.png         - Use to find pixel coordinates as %")
    print(f"  Saved channel_hue.png          - Hue values (for color matching)")
    print(f"  Saved channel_saturation.png   - Saturation (color intensity)")
    print(f"  Saved channel_value.png        - Brightness")
    print(f"  Saved detection_regions.png    - Current config regions overlaid")
    print(f"\n  Use the grid overlay to determine the correct detect_region values")
    print(f"  for your specific game and encounter type.")
    print(f"\n  Tip: Take screenshots of BOTH shiny and non-shiny summary screens")
    print(f"  and run calibration on each to compare the differences.")


# ═══════════════════════════════════════════════════════
# MAIN LOOP (for live operation on the Pi)
# ═══════════════════════════════════════════════════════

def run_live(mode, source=0, config_name=None, servo_gpio=18, notify_topic=None,
             new_hunt=False, abort_test_cycle=None, timing_test=False,
             post_summary_wait=25, summary_poll=True,
             cycle_wait_override=None, stats_grab_delay=None, single_grab=False):
    """
    Main detection loop for live operation.

    In production on the Pi, this runs continuously:
    1. Wait for macro to reach summary screen
    2. Analyze frame(s) + save screenshot for retroactive review
    3. If shiny: fire trigger_abort() — hold APG 3.3s, halting the macro
       mid-summary-dwell. The shiny is preserved on screen (not soft-reset
       away). Notify + exit. ← v0.4 inversion (was previously: do nothing).
    4. If not shiny: inject random delay (loop-seam variance), tap APG,
       restart macro
    5. Increment counter, log result
    6. Repeat

    v0.4 test-mode arguments:
      abort_test_cycle: int  — force is_shiny=True on this reset number,
                               firing the abort pipeline for dry-run testing
                               (apg_abort_test_procedure.md Part B).
      timing_test: bool      — replace real trigger_abort() with
                               trigger_abort_noop() so latency math can be
                               measured non-destructively (Part A).
    """
    # Determine config
    if config_name is None:
        config_name = f"frlg_{mode}"

    # ── Clean-shutdown signals (2026-06-22) ────────────────────────────────
    # When launched backgrounded over ssh (`nohup … &`), the shell sets SIGINT
    # to SIG_IGN and the script INHERITS it — so `pkill -INT` (the documented,
    # servo-safe stop) silently does nothing and the only way to stop becomes a
    # hard kill that skips the finally-block servo cleanup (arm can strand on
    # the APG button → macro wipe). Re-arm SIGINT to Python's default
    # KeyboardInterrupt handler regardless of what we inherited, and route
    # SIGTERM to the SAME clean path, so BOTH `pkill -INT` and `pkill -TERM`
    # unwind through `except KeyboardInterrupt` → `finally` (servo → REST,
    # detach, close). Must run in the main thread (it does — run_live is called
    # from __main__).
    def _clean_shutdown(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, _clean_shutdown)

    print(f"\n{'═'*50}")
    print(f"  PokeAutomator - LIVE MODE")
    print(f"  Detection: {mode}")
    print(f"  Config: {config_name}")
    print(f"  Source: {source}")
    print(f"  Notifications: {'ON → ' + notify_topic if notify_topic else 'OFF'}")
    print(f"  Random delay: {RANDOM_DELAY_MIN*1000:.0f}-{RANDOM_DELAY_MAX*1000:.0f}ms")
    print(f"  Summary poll: {'ON (≤%.0fs forward, %.1fs cadence)' % (SUMMARY_POLL_MAX_S, SUMMARY_POLL_INTERVAL_S) if summary_poll else 'OFF (single blind grab)'}")
    print(f"{'═'*50}\n")
    
    # Load prior hunt state — running tally persists across sessions
    # until a shiny is found (or the user passes --new-hunt).
    if new_hunt:
        clear_hunt_state()
    state = load_hunt_state()
    if state:
        reset_count = state["reset_count"]
        hunt_start_time = datetime.fromisoformat(state["hunt_start_time"])
        resumed = True
    else:
        reset_count = 0
        hunt_start_time = datetime.now()
        resumed = False

    session_start_time = datetime.now()
    log_file = f"hunt_log_{session_start_time.strftime('%Y%m%d_%H%M%S')}.txt"
    screenshot_dir = f"hunt_screenshots_{session_start_time.strftime('%Y%m%d_%H%M%S')}"

    with open(log_file, 'w') as f:
        f.write(f"PokeAutomator Hunt Log\n")
        f.write(f"Session started: {session_start_time}\n")
        f.write(f"Hunt started:    {hunt_start_time}\n")
        f.write(f"Starting count:  {reset_count} ({'resumed' if resumed else 'new hunt'})\n")
        f.write(f"Config: {config_name}\n")
        f.write(f"Notifications: {notify_topic or 'disabled'}\n")
        f.write(f"Random delay: {RANDOM_DELAY_MIN*1000:.0f}-{RANDOM_DELAY_MAX*1000:.0f}ms\n")
        f.write(f"Summary poll: {'ON (<=%.0fs, %.1fs cadence)' % (SUMMARY_POLL_MAX_S, SUMMARY_POLL_INTERVAL_S) if summary_poll else 'OFF'}\n")
        f.write(f"{'='*40}\n\n")

    print(f"[LIVE] Hunt log: {log_file}")
    print(f"[LIVE] Screenshots: {screenshot_dir}/")
    if resumed:
        print(f"[LIVE] Resumed hunt at reset #{reset_count:,}")
    else:
        print(f"[LIVE] Starting new hunt")
    print(f"[LIVE] Waiting for first detection window...\n")

    # Send session-started notification
    if notify_topic:
        if resumed:
            hunt_elapsed = session_start_time - hunt_start_time
            send_notification(
                notify_topic,
                "Session Started",
                f"Resumed hunt at reset #{reset_count:,}\n"
                f"Hunt running for: {str(hunt_elapsed).split('.')[0]}\n"
                f"Config: {config_name}",
                priority="default",
                tags="arrows_counterclockwise,video_game"
            )
        else:
            send_notification(
                notify_topic,
                "Session Started",
                f"New hunt — config: {config_name}\nMode: {mode}\nLet's go!",
                priority="default",
                tags="video_game,sparkles"
            )
    
    # Servo setup — MG90S on hardware PWM (RP1 chip via rpi-hardware-pwm).
    # NOTE: servo_gpio argument is unused in this variant — the GPIO pin
    # is bound by the dtoverlay=pwm-2chan line in /boot/firmware/config.txt,
    # NOT by Python. On this Pi 5, pwm-2chan exposes pwmchip0; GPIO 18 is
    # PWM0_CHAN2 (verify with `pinctrl get 18`). The script uses
    # HWPWM_CHIP=0, HWPWM_CHANNEL=2 to match.
    is_pi = False
    servo = None
    watchdog = None
    try:
        servo = HardwarePWMServo()  # uses HWPWM_CHIP/CHANNEL/FREQUENCY constants
        servo.value = SERVO_REST
        time.sleep(0.5)
        # 5/12: NO detach here. With MG90S + new horn angle, detach lets
        # gravity drop the arm onto the A button. Hold REST under active
        # PWM continuously (slight buzz acceptable). press_button() also
        # ends with REST held, not detached.
        is_pi = True
        print(f"[LIVE] Hardware PWM servo initialized "
              f"(chip={HWPWM_CHIP}, channel={HWPWM_CHANNEL}, "
              f"{HWPWM_FREQUENCY_HZ}Hz, {HWPWM_MIN_PULSE_MS}-{HWPWM_MAX_PULSE_MS}ms pulse)")
        print(f"[LIVE] (servo_gpio CLI arg = {servo_gpio} — IGNORED; pin is bound "
              f"by dtoverlay=pwm-pio in /boot/firmware/config.txt)")

        # Press watchdog — forces servo back to REST if any single press
        # exceeds PRESS_HARD_BUDGET_S in the danger window. Closes the
        # macro-wipe vulnerability that ended 5/8 (1284), 5/9 (757), and
        # 5/10 (410) hunts. See PressWatchdog class docstring + constants.
        def _force_rest():
            try:
                servo.value = SERVO_REST
                time.sleep(0.05)  # let the motor settle into REST
                # 5/12: NO detach in watchdog. Gravity would drop the arm
                # back onto the button at REST. The watchdog's job is to
                # actively retract the arm AWAY from the button; that
                # requires PWM holding REST, not detaching.
            except Exception as e:
                print(f"[WATCHDOG] _force_rest exception: {e}")
        watchdog = PressWatchdog(_force_rest)
        watchdog.start()
    except ImportError as e:
        print(f"[LIVE] ImportError on rpi-hardware-pwm: {e}")
        print("[LIVE] Install with: pip3 install rpi-hardware-pwm")
        print("[LIVE] Or fall back to the gpiozero version: ~/shiny_detector.py")
        raise
    except (FileNotFoundError, OSError) as e:
        print(f"[LIVE] Hardware PWM init failed: {e}")
        print("[LIVE] Possible causes:")
        print("[LIVE]   - dtoverlay=pwm-2chan not in /boot/firmware/config.txt")
        print("[LIVE]   - Pi not rebooted after overlay change")
        print(f"[LIVE]   - HWPWM_CHANNEL={HWPWM_CHANNEL} wrong for your GPIO pin")
        print(f"[LIVE]     (verify with: pinctrl get 18 — should report PWM0_CHAN2 for ch=2)")
        print("[LIVE] Falling back: re-run with the gpiozero version (~/shiny_detector.py)")
        raise

    # Open persistent capture — stays open for the entire hunt
    # This avoids the 2s warmup penalty on every cycle
    cap = None
    if not (isinstance(source, str) and not source.isdigit()):
        cap = cv2.VideoCapture(int(source) if isinstance(source, str) else source)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        time.sleep(2)
        for _ in range(30):
            cap.read()
        print(f"[LIVE] Capture card initialized (persistent)")

    first_cycle_of_session = True
    consecutive_blocks = 0  # for stall guard
    try:
        while True:
            reset_count += 1
            save_hunt_state(reset_count, hunt_start_time)
            timestamp = datetime.now().strftime("%H:%M:%S")

            # Capture whether THIS cycle is the intentional first-cycle skip
            # before the detection branch flips first_cycle_of_session to False.
            # Computed before the grab so the poll knows whether to wait for the
            # summary — the skip cycle's macro may not be at the summary yet.
            is_skip_cycle = first_cycle_of_session

            # ── Grab a fresh frame for both screenshot and detection ──
            # On real hardware, POLL FORWARD for the summary-screen gate so a
            # late summary is caught instead of missed (mistimed-grab fix). The
            # skip cycle takes a single grab — the macro isn't guaranteed to be
            # at the summary yet, so there's nothing to lock onto.
            # ── Two-shot capture (2026-06-22) ──────────────────────────────
            # Both summary pages pass the gate; the poll locks onto the FIRST
            # one = the NATURE page (POKéMON INFO, ~t33). That is the
            # AUTHORITATIVE shiny-detection frame — it has the sprite + star
            # region, and it appears ~4s before the stats page, so an abort here
            # finishes well before the macro's reset. The STATS page is grabbed
            # a few seconds later (see SECOND GRAB, below) for the census +
            # downstream tools that parse reset_*.png.
            frame = None
            if cap and cap.isOpened():
                frame, grab_gate_passed, polls = grab_summary_frame(
                    cap, config_name,
                    poll=(summary_poll and not is_skip_cycle),
                )
                if frame is None:
                    print(f"  [WARN] Frame grab failed on reset #{reset_count}")
                else:
                    if polls:
                        status = "nature/summary locked" if grab_gate_passed else "no summary in window"
                        print(f"  [POLL] +{polls} grab(s) "
                              f"(~{polls*SUMMARY_POLL_INTERVAL_S:.0f}s) → {status}")
                    # A real gate-pass is the nature page → save as nature_*.png.
                    # A blocked/skip grab is still saved (as reset_*.png) so the
                    # stall guard and retroactive review have a frame to inspect.
                    if grab_gate_passed and not single_grab:
                        save_cycle_screenshot(frame, reset_count, screenshot_dir,
                                              prefix="nature")
                    else:
                        save_cycle_screenshot(frame, reset_count, screenshot_dir)

            # ── Detect ──
            # Skip first cycle of THIS session — macro may not be at summary
            # screen yet (applies whether starting fresh or resuming).
            if first_cycle_of_session:
                is_shiny, confidence, data = False, 0.0, {}
                first_cycle_of_session = False
                print(f"  [SKIP] First cycle of session — waiting for macro to reach summary screen")
            elif frame is not None:
                # AUTHORITATIVE detection — runs on the NATURE page (the first
                # gate-pass). Sprite + star region are present here, ~4s before
                # the stats page, so an abort fires with plenty of reset margin.
                is_shiny, confidence, data = detect_star_frame(frame, config_name)
                # 9/19 SHINY FIX (belt and braces): a cycle whose summary gate did
                # not pass is NEVER silently cleared as "Not shiny" again. Probe it,
                # log [UNVERIFIED] (the watcher pages on that), and abort if the
                # star and the sprite palette BOTH say shiny.
                if not is_shiny and not data.get("gate_passed") and not is_skip_cycle:
                    ub = unverified_probe(frame, config_name)
                    ub_msg = (f"  [UNVERIFIED] reset #{reset_count}: summary gate did not pass"
                              f" - star px (gate bypassed)={ub['star_px']}"
                              f" shiny-palette px={ub['shiny_px']}"
                              f" normal-palette px={ub['normal_px']}; frame saved for review")
                    print(ub_msg)
                    with open(log_file, 'a') as f:
                        f.write(ub_msg + "\n")
                    if (ub["star_px"] >= CONFIG[config_name]["star_pixel_threshold"]
                            and ub["shiny_px"] >= 1000):
                        print("  \u2605\u2605\u2605 SHINY (gate bypassed: star + palette agree) \u2605\u2605\u2605")
                        is_shiny, confidence = True, 1.0
                        data = dict(data, gate_passed=True, unverified_override=True)
            elif mode == "sparkle":
                is_shiny, confidence, data = detect_sparkle(
                    source, config_name, debug=False
                )
            else:
                is_shiny, confidence, data = detect_star(
                    source, config_name, debug=False
                )

            # ── SECOND GRAB: the STATS page (two-shot census + redundant abort)
            # The first grab locked + checked the NATURE page (authoritative).
            # If it wasn't shiny, wait for the macro to flip to the stats page,
            # grab it as reset_*.png (the frame the montage / missed-shiny /
            # seed-diversity tools parse), and re-run the SAME star check as an
            # independent second look at the same sprite. If the stats page flags
            # a shiny the nature page somehow missed, escalate to a real abort
            # here — the stats grab (~t38) still completes its 3.3s hold (~t42)
            # before the macro's reset (~t45). `data` is left on the nature
            # result so the stall guard still counts this as a good cycle.
            if (not is_shiny and grab_gate_passed and not is_skip_cycle
                    and not single_grab
                    and cap and cap.isOpened()
                    and not (isinstance(source, str) and not source.isdigit())):
                time.sleep(stats_grab_delay if stats_grab_delay is not None
                           else STATS_GRAB_DELAY_S)
                stats_frame, stats_gate, _ = grab_summary_frame(
                    cap, config_name, poll=False)
                if stats_frame is not None:
                    save_cycle_screenshot(stats_frame, reset_count, screenshot_dir)
                    if stats_gate:
                        st_shiny, st_conf, _ = detect_star_frame(
                            stats_frame, config_name)
                        if st_shiny:
                            is_shiny = True
                            confidence = st_conf
                            st_msg = (f"  [STATS-STAR] reset #{reset_count}: shiny "
                                      f"flagged on the STATS page (conf {st_conf:.0%}) "
                                      f"that the nature page missed — aborting on the "
                                      f"stats page.")
                            print(st_msg)
                            with open(log_file, 'a') as f:
                                f.write(st_msg + "\n")
                    else:
                        print(f"  [2ND-GRAB] stats grab didn't pass the gate; "
                              f"saved reset_*.png for review.")

            # ── v0.4: timestamp the moment detection completed (the
            # "summary_detected" anchor for abort-latency math, per
            # apg_abort_test_procedure.md Part A). Only meaningful when
            # the gate passed; for skip/blocked cycles it's a no-op.
            t_summary_detected = time.time() if data.get("gate_passed") else None

            # ── v0.4: --abort-test-cycle N override. Forces is_shiny=True
            # on the specified reset so the abort pipeline fires without
            # needing a real shiny encounter.
            if abort_test_cycle is not None and reset_count == abort_test_cycle:
                print(f"  [ABORT-TEST] Cycle {reset_count} matches "
                      f"--abort-test-cycle {abort_test_cycle}; forcing "
                      f"is_shiny=True to exercise the abort pipeline.")
                is_shiny = True
                confidence = 1.0
                if t_summary_detected is None:
                    # Synthesize the timestamp so latency math doesn't divide by None
                    t_summary_detected = time.time()

            # ── v0.4: --timing-test forces is_shiny=True on EVERY
            # gate-passed cycle so we get latency measurements across the
            # full 20-30 cycles called for in Part A. Only fires on real
            # summary screens (gate_passed) — won't pollute timing data
            # with blocked/skip cycles. trigger_abort_noop() runs instead
            # of the real hold, so Auto.apg is never destroyed.
            elif timing_test and data.get("gate_passed") is True and not is_skip_cycle:
                is_shiny = True
                confidence = 1.0  # not meaningful in timing-test, but set for log shape
            
            # Log result
            elapsed = datetime.now() - hunt_start_time
            log_line = (f"[{timestamp}] Reset #{reset_count:>5d} | "
                       f"Elapsed: {str(elapsed).split('.')[0]} | "
                       f"{'★ SHINY!' if is_shiny else 'Not shiny'} "
                       f"(conf: {confidence:.0%})")
            
            print(log_line)
            with open(log_file, 'a') as f:
                f.write(log_line + "\n")

            # ── Stall guard ──
            # Track consecutive cycles where the summary-screen gate didn't
            # pass. A real summary-screen frame returns gate_passed=True; the
            # intentional first-cycle skip has no gate_passed key (ignored
            # below). Anything else (gate=False, or capture totally failed
            # with empty data dict) counts as a block.
            if not is_skip_cycle:
                if data.get("gate_passed") is True:
                    consecutive_blocks = 0
                else:
                    consecutive_blocks += 1
                    print(f"  [STALL?] Gate-block streak: {consecutive_blocks}/{STALL_GATE_BLOCK_THRESHOLD}")
                    if consecutive_blocks >= STALL_GATE_BLOCK_THRESHOLD:
                        stall_msg = (
                            f"Hunt stalled at reset #{reset_count:,}.\n\n"
                            f"The summary-screen gate has blocked "
                            f"{consecutive_blocks} cycles in a row, meaning "
                            f"the Pi hasn't seen a real Pokemon summary "
                            f"screen recently.\n\n"
                            f"Likely causes: Switch off / asleep, controller "
                            f"battery dead, capture card unplugged, or the "
                            f"KK3 macro is broken.\n\n"
                            f"Running tally is preserved — fix the issue, "
                            f"then resume."
                        )
                        print(f"\n[STALL] {stall_msg}")
                        with open(log_file, 'a') as f:
                            f.write(f"\n[STALL] gate-blocked {consecutive_blocks}x; "
                                    f"stopping at reset #{reset_count}\n")
                        if notify_topic:
                            send_notification(
                                notify_topic,
                                "Hunt stalled",
                                stall_msg,
                                priority="urgent",
                                tags="warning,stop_sign",
                            )
                        # Bail out of the loop without pressing the servo again.
                        break

            if is_shiny:
                # ── v0.4 ABORT PATH ──────────────────────────────────────
                # Fire trigger_abort() to halt the running macro mid-summary
                # dwell. Without this, the macro would continue to its
                # in-cycle reset and the shiny would be destroyed.
                # apg_abort_test_procedure.md Part A is the timing test;
                # Part B is the live-fire validation.
                banner = "★" * 50 if not (abort_test_cycle == reset_count) else "T" * 50
                print(f"\n{banner}")
                if abort_test_cycle == reset_count:
                    print(f"  ABORT-TEST CYCLE FIRED at reset {reset_count}")
                else:
                    print(f"  SHINY FOUND after {reset_count} resets!")
                print(f"  Total time: {str(elapsed).split('.')[0]}")
                print(f"  Confidence: {confidence:.0%}")
                print(f"  Mode: {'TIMING-TEST (no-op abort)' if timing_test else 'LIVE ABORT (3.3s APG hold)'}")
                print(f"{banner}\n")

                # Fire the abort (real or no-op depending on --timing-test).
                if is_pi:
                    if timing_test:
                        abort_timings = trigger_abort_noop(servo)
                    else:
                        abort_timings = trigger_abort(servo)
                else:
                    # Simulation mode (no Pi hardware) — fake the timings
                    print(f"  [SIM] *ABORT* (would hold APG {ABORT_HOLD_S}s)")
                    _t = time.time()
                    abort_timings = {
                        "abort_decision": _t,
                        "servo_reaches_PRESS": _t,
                        "servo_released": _t,
                    }

                # Compute the four per-cycle timing offsets per
                # apg_abort_test_procedure.md Part A pass criteria.
                if t_summary_detected is not None:
                    latency_ms = (abort_timings["abort_decision"] - t_summary_detected) * 1000
                    press_reach_ms = (abort_timings["servo_reaches_PRESS"] - abort_timings["abort_decision"]) * 1000
                    hold_ms = (abort_timings["servo_released"] - abort_timings["servo_reaches_PRESS"]) * 1000
                    total_ms = (abort_timings["servo_released"] - t_summary_detected) * 1000
                    timing_line = (
                        f"[TIMING] cycle={reset_count} "
                        f"summary→decision={latency_ms:.0f}ms "
                        f"decision→press={press_reach_ms:.0f}ms "
                        f"hold={hold_ms:.0f}ms "
                        f"TOTAL={total_ms:.0f}ms"
                    )
                    print(f"  {timing_line}")
                    with open(log_file, 'a') as f:
                        f.write(timing_line + "\n")

                # Banner-write: suppressed in --timing-test mode (would print
                # every cycle for 20-30 reps and bury the timing data). The
                # compact [TIMING] line above is enough log signal for Part A.
                if not timing_test:
                    tag_word = "ABORT-TEST" if abort_test_cycle == reset_count else "SHINY FOUND"
                    with open(log_file, 'a') as f:
                        f.write(f"\n{'★'*40}\n")
                        f.write(f"{tag_word}! Resets: {reset_count}\n")
                        f.write(f"Time: {str(elapsed).split('.')[0]}\n")
                        f.write(f"Mode: live abort (3.3s hold)\n")
                        f.write(f"{'★'*40}\n")

                # ── NOTIFY (suppressed in --timing-test to avoid spam) ──
                if notify_topic and not timing_test:
                    if abort_test_cycle == reset_count:
                        send_notification(
                            notify_topic,
                            "ABORT-TEST fired",
                            f"--abort-test-cycle {abort_test_cycle} triggered at "
                            f"reset #{reset_count:,}\n"
                            f"Mode: {'timing-test (no-op)' if timing_test else 'LIVE 3.3s APG hold'}\n"
                            f"Check rig — macro should be halted if live abort.",
                            priority="default",
                            tags="test_tube,gear",
                        )
                    else:
                        send_notification(
                            notify_topic,
                            "★ SHINY FOUND! ★",
                            f"Shiny detected after {reset_count:,} resets!\n"
                            f"Time: {str(elapsed).split('.')[0]}\n"
                            f"Confidence: {confidence:.0%}\n\n"
                            f"Abort fired — macro halted, shiny on screen!\n"
                            f"Get to the rig before the GuliKit recording auto-stops (10 min).",
                            priority="urgent",
                            tags="sparkles,tada,star",
                        )

                # In TIMING-TEST mode, skip break + clear — fall through to
                # the inter-cycle code so the loop keeps collecting
                # measurements. Otherwise (real shiny or one-shot
                # --abort-test-cycle), clear running tally + break.
                if not timing_test:
                    # Wipe the running tally only on a REAL shiny — abort-test
                    # cycles shouldn't reset the hunt counter.
                    if abort_test_cycle != reset_count:
                        clear_hunt_state()
                    break
            
            # ── v0.4 FIX (5/24): Wait for the macro to COMPLETE before
            # pressing APG to start the next cycle. The new loop-seam macro
            # is self-contained (title → encounter → summary → reset → title)
            # and lasts ≥45s. The OLD macro ended at summary (~31s), so the
            # old cycle_wait alone was enough. The new macro does NOT start
            # with a soft reset, so an APG press at any non-title screen
            # corrupts the cycle entirely.
            #
            # Sequencing (Press at t=0):
            #   t=0          press APG → macro starts
            #   t=0-45+      macro runs (summary at ~28-36s)
            #   t=cycle_wait+frame_grab+detect (~31s):  Pi grabs frame mid-dwell ✓
            #   t=31+POST_SUMMARY_WAIT_S+variance+press_time:  next APG press
            #   For next press to land AFTER macro completes (≥45s):
            #     POST_SUMMARY_WAIT_S ≥ 14 (with random=0.1 lower bound)
            #   Default 25 gives ~12s margin above the 45s minimum.
            # Tune via --post-summary-wait if your macro is longer.
            POST_SUMMARY_WAIT_S = post_summary_wait
            if is_pi and not is_skip_cycle and not (isinstance(source, str) and not source.isdigit()):
                print(f"  Waiting {POST_SUMMARY_WAIT_S}s for macro to finish post-summary phase "
                      f"(macro returns to title before next APG press)...")
                time.sleep(POST_SUMMARY_WAIT_S)

            # ── Random delay (loop-seam variance source) ──
            # Shifts when the macro starts relative to game RNG state
            delay = random.uniform(RANDOM_DELAY_MIN, RANDOM_DELAY_MAX)
            
            # Press the APG (macro) button to restart the hunt cycle
            # The KK3 Max macro handles: soft reset → title screen →
            # startup → select starter → summary screen
            if is_pi:
                # 9/29 PARK GUARD: never press unless the macro's parked screen is on
                # screen. If it isn't within PARK_GUARD_MAX_S, stop the hunt cleanly
                # ([STALL] pages the watcher) rather than replay the macro against
                # the wrong screen.
                if cap and cap.isOpened() and CONFIG[config_name].get("park_regions"):
                    parked, waited, pfr = wait_until_parked(cap, config_name)
                    if waited >= PARK_GUARD_INTERVAL_S or not parked:
                        print(f"  [PARK-GUARD] parked={parked} after {waited:.1f}s "
                              f"(floor fracs {', '.join(f'{x:.2f}' for x in pfr)})")
                    if not parked:
                        guard_msg = (f"[STALL] park-guard: parked screen not seen for "
                                     f"{waited:.0f}s before the press; stopping at reset "
                                     f"#{reset_count} WITHOUT pressing (floor fracs "
                                     f"{', '.join(f'{x:.2f}' for x in pfr)})")
                        print(f"\n{guard_msg}")
                        with open(log_file, 'a') as f:
                            f.write(f"\n{guard_msg}\n")
                        break
                time.sleep(delay)          # random offset before pressing APG
                press_button(servo, watchdog=watchdog)  # watchdog enforces hard time budget
            else:
                print(f"  [SIM] *APG press* (+{delay*1000:.0f}ms delay) - macro restarting")
            
            # Wait for macro cycle to complete before next detection.
            # Macro duration (APG press → summary screen visible) = ~31s
            # with the 2026-04-20 reprogram that navigates into SUMMARY.
            # 5/12: bumped 32→34 after observing that ~1 in 6 captures
            # were hitting the page slide-in animation (layout chrome
            # rendered but stat values not yet populated — pixel-identical
            # mid-animation frames at reset 416/421/422 = md5 878f7b...).
            # The sprite renders correctly in slide-in so detection still
            # worked, but a 3s buffer is cheap insurance and removes
            # variance from the screenshot review pile.
            cycle_wait = 30   # 6/22: set for the two-page nature-first macro.
                              # Measured (timeline_capture, 0.3s cadence, this
                              # macro): NATURE page t≈33→36.6, STATS page
                              # t≈37→44.3 — both pass the gate. The first grab
                              # must land BEFORE the stats page so the poll locks
                              # the NATURE page (first gate-pass). Grab at t≈30
                              # (party/pre-nature) → poll catches nature at ~t33;
                              # ~7s clear of the t≈37 stats page, so we never lock
                              # stats by mistake. STATS_GRAB_DELAY_S then grabs the
                              # stats page ~5s after the nature lock.
                              # 6/8 baseline (pre-nature macro): summary t=33.6s.
                              #
                              # 7/21: a NEW macro (e.g. the Celadon Eevee gift)
                              # will have a different length, so this is now
                              # overridable via --cycle-wait. Default None keeps
                              # the frozen 30 for starter + Magikarp.
            if cycle_wait_override is not None:
                cycle_wait = cycle_wait_override

            if isinstance(source, str) and not source.isdigit():
                # Testing with video file - don't wait
                pass
            else:
                print(f"  Waiting {cycle_wait}s for macro cycle...")
                time.sleep(cycle_wait)
    
    except KeyboardInterrupt:
        print(f"\n[LIVE] Stopped by user at reset #{reset_count}.")
        save_hunt_state(reset_count, hunt_start_time)
        if notify_topic:
            hunt_elapsed = datetime.now() - hunt_start_time
            session_elapsed = datetime.now() - session_start_time
            send_notification(
                notify_topic,
                "Session Ended",
                f"Stopped at reset #{reset_count:,}\n"
                f"Session length: {str(session_elapsed).split('.')[0]}\n"
                f"Total hunt time: {str(hunt_elapsed).split('.')[0]}\n\n"
                f"Run again to resume the hunt.",
                priority="default",
                tags="stop_sign"
            )

    except Exception as e:
        # ── NOTIFY: Error / crash ──
        print(f"\n[LIVE] ERROR: {e}")
        save_hunt_state(reset_count, hunt_start_time)
        if notify_topic:
            hunt_elapsed = datetime.now() - hunt_start_time
            send_notification(
                notify_topic,
                "⚠️ PokeAutomator Error!",
                f"Script crashed at reset #{reset_count:,}.\n"
                f"Total hunt time: {str(hunt_elapsed).split('.')[0]}\n"
                f"Error: {str(e)}\n\n"
                f"Check the Pi — the macro may have stopped.\n"
                f"State saved; run again to resume.",
                priority="urgent",
                tags="warning,x"
            )
        raise
    
    finally:
        if watchdog:
            watchdog.stop()
        if cap:
            cap.release()
            print("[LIVE] Capture released")
        if is_pi and servo:
            servo.value = SERVO_REST
            time.sleep(0.3)
            servo.detach()
            servo.close()
            print("[LIVE] Servo cleaned up")
    
    return reset_count


# ═══════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="PokeAutomator Shiny Detector",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Test sparkle detection on a video file
  python shiny_detector.py --mode sparkle --source battle_clip.mp4 --debug

  # Test star detection on a screenshot  
  python shiny_detector.py --mode star --source summary_screen.png --debug

  # Calibrate with a screenshot
  python shiny_detector.py --calibrate --source screenshot.png

  # Run live on the Pi (capture dongle = device 0)
  python shiny_detector.py --mode sparkle --source 0 --live

  # Run live with push notifications to your phone
  python shiny_detector.py --mode star --source 0 --live --notify your-topic-name
        """
    )
    
    parser.add_argument("--mode", choices=["sparkle", "star"], 
                       help="Detection mode")
    parser.add_argument("--source", default="0",
                       help="Video file path, image path, or device number (default: 0)")
    parser.add_argument("--config", default=None,
                       help="Config name (default: frlg_{mode})")
    parser.add_argument("--debug", action="store_true",
                       help="Show per-frame debug output")
    parser.add_argument("--save-debug", default=None,
                       help="Directory to save debug images")
    parser.add_argument("--calibrate", action="store_true",
                       help="Run calibration tool")
    parser.add_argument("--live", action="store_true",
                       help="Run in live detection loop (for Pi)")
    parser.add_argument("--gpio", type=int, default=18,
                       help="GPIO pin for servo (default: 18)")
    parser.add_argument("--notify", default=None,
                       help="ntfy.sh topic name for push notifications (e.g. your-topic-name)")
    parser.add_argument("--new-hunt", action="store_true",
                       help="Wipe any saved running tally and start a fresh hunt")
    # ── v0.4 (loop-seam + APG abort) test flags ──
    parser.add_argument("--abort-test-cycle", type=int, default=None,
                       help="(v0.4) Force is_shiny=True on this reset number, "
                            "firing the abort pipeline for dry-run testing. "
                            "Destroys Auto.apg per rep — keep test reps low "
                            "(apg_abort_test_procedure.md Part B).")
    parser.add_argument("--timing-test", action="store_true",
                       help="(v0.4) Replace the real 3.3s abort hold with a "
                            "no-op that still logs the latency timestamps. "
                            "Used for Part A (non-destructive timing test).")
    parser.add_argument("--seam-delay-min", type=float, default=None,
                       help="(v0.4) Override RANDOM_DELAY_MIN (seconds). "
                            "Default 0.100. Lower bound of the per-cycle "
                            "variance window.")
    parser.add_argument("--seam-delay-max", type=float, default=None,
                       help="(v0.4) Override RANDOM_DELAY_MAX (seconds). "
                            "Default 2.100. Upper bound of the per-cycle "
                            "variance window.")
    parser.add_argument("--post-summary-wait", type=float, default=25,
                       help="(v0.4) Seconds to wait AFTER detection completes "
                            "before pressing APG for the next cycle. Lets the "
                            "self-contained loop-seam macro finish its "
                            "post-summary reset + return to title screen. "
                            "Default 25 (good for ~47s macros). Increase if "
                            "your macro is longer (e.g. 35 for ~55s, 45 for "
                            "~65s). If the Pi presses APG mid-macro, "
                            "everything goes haywire.")
    parser.add_argument("--no-summary-poll", action="store_false",
                       dest="summary_poll", default=True,
                       help="Disable the summary-gate poll (revert to a single "
                            "blind grab per cycle). Polling forward for the "
                            "summary is on by default — it catches late "
                            "summaries instead of wasting the cycle, and only "
                            "ever delays the next APG press, never advances it.")
    parser.add_argument("--summary-poll-max", type=float, default=None,
                       help="(diagnostic) Override SUMMARY_POLL_MAX_S — how many "
                            "seconds the gate-poll waits forward for the summary. "
                            "Default 14. Bump to ~50 to test whether 'blocked' "
                            "cycles are really stuck or just running late: a "
                            "'summary locked' at a high poll count means the "
                            "macro is drifting past the window (timing problem), "
                            "not stalling. Safe-by-construction — a longer poll "
                            "only ever pushes the next APG press LATER, never "
                            "mid-macro.")
    parser.add_argument("--cycle-wait", type=float, default=None,
                       help="(2026-07-21) Override the fixed post-press wait "
                            "before the Pi grabs a frame. Default 30 — tuned for "
                            "the starter/Magikarp two-page macro (nature page at "
                            "t≈33). A different macro (e.g. the Celadon Eevee "
                            "gift) reaches its summary at a different time; set "
                            "this so the FIRST grab lands just BEFORE the nature "
                            "page, letting the poll lock nature rather than "
                            "stats. Measure with 40-Rig/timeline_capture.py.")
    parser.add_argument("--single-grab", action="store_true",
                       help="(2026-09-29) One capture per cycle: the first gate-pass "
                            "frame is the detection frame AND is saved as reset_*.png; "
                            "the second (stats-page) grab is skipped. Time --cycle-wait "
                            "so the first grab lands on the STATS page.")
    parser.add_argument("--stats-grab-delay", type=float, default=None,
                       help="(2026-07-21) Override STATS_GRAB_DELAY_S — seconds "
                            "after the nature-page lock before grabbing the STATS "
                            "page (the reset_*.png the montage / missed-shiny / "
                            "seed-diversity tools parse). Default 5.0. Retune if "
                            "a new macro's page dwells differ.")

    args = parser.parse_args()

    # ── Apply seam-delay overrides (v0.4) ──
    # Mutate the module-level constants so existing references in run_live's
    # loop still pick up the change without threading the values through.
    if args.seam_delay_min is not None:
        RANDOM_DELAY_MIN = args.seam_delay_min
        print(f"[CLI] Override: RANDOM_DELAY_MIN = {RANDOM_DELAY_MIN:.3f}s")
    if args.seam_delay_max is not None:
        RANDOM_DELAY_MAX = args.seam_delay_max
        print(f"[CLI] Override: RANDOM_DELAY_MAX = {RANDOM_DELAY_MAX:.3f}s")
    if RANDOM_DELAY_MIN > RANDOM_DELAY_MAX:
        parser.error(
            f"--seam-delay-min ({RANDOM_DELAY_MIN}) must be ≤ "
            f"--seam-delay-max ({RANDOM_DELAY_MAX})"
        )

    # ── Apply summary-poll-max override (diagnostic, 2026-06-08) ──
    # Same module-scope mutation pattern as the seam-delay overrides above.
    # Widening the poll window is how we distinguish a stuck macro from one
    # that's merely drifting past the 14s default — see project_open_bugs.
    if args.summary_poll_max is not None:
        if args.summary_poll_max <= 0:
            parser.error(f"--summary-poll-max must be > 0 (got {args.summary_poll_max})")
        SUMMARY_POLL_MAX_S = args.summary_poll_max
        print(f"[CLI] Override: SUMMARY_POLL_MAX_S = {SUMMARY_POLL_MAX_S:.1f}s")

    if args.calibrate:
        calibrate(args.source)
    elif args.live:
        if not args.mode:
            parser.error("--mode required for live operation")
        config = args.config or f"frlg_{args.mode}"
        run_live(args.mode, args.source, config, args.gpio, args.notify,
                 args.new_hunt, args.abort_test_cycle, args.timing_test,
                 args.post_summary_wait, args.summary_poll,
                 args.cycle_wait, args.stats_grab_delay, args.single_grab)
    elif args.mode:
        config = args.config or f"frlg_{args.mode}"
        if args.mode == "sparkle":
            detect_sparkle(args.source, config, args.debug, args.save_debug)
        else:
            detect_star(args.source, config, args.debug, args.save_debug)
    else:
        parser.print_help()
