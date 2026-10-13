#!/usr/bin/env python3
"""
Single-cycle TIMELINE capture — diagnostic for the "29% blocked / overnight
stall" question (2026-06-08).

The 6/3 conclusion was "the macro gets stuck at the Oak's-lab dialogue and
never reaches the summary." But that rested on a 14s forward-poll and a single
grab per cycle, neither of which can tell a STUCK macro apart from one that's
just running LATE — in both cases the frame at t≈30-44s shows dialogue. Steve's
counter-hypothesis: it's a timing/capture-window problem, not a macro error.

This tool settles it. Instead of one grab per cycle, it presses APG to anchor a
clean cycle at t=0, then grabs every INTERVAL seconds for the full DURATION and
records WHETHER and WHEN the summary screen actually appears. Run it for a few
cycles and you get the real summary-appearance-time distribution:

  • summary appears every cycle, some past 14s  → WINDOW DRIFT (Steve is right);
    the fix is widening/re-centering the capture window, not the macro.
  • summary genuinely never appears within DURATION → the macro really stalls.

It reuses the live script's servo init, press watchdog, and summary gate, so the
press behaves byte-for-byte like a real hunt cycle.

PRECONDITION: game on the LeafGreen TITLE screen (same as launching a hunt).
SAFETY: with DURATION ≥ the macro length (~45-50s), the inter-cycle APG press
only ever lands at the title screen — never mid-macro. The tool refuses to run
with DURATION < 60s for that reason.

NOTE: this is an OBSERVER — it does NOT abort on a shiny. Vanishingly unlikely in
a handful of cycles, but if star detection ever fires it prints a loud warning so
you can grab the encounter manually.

Run on the Pi:
  python3 timeline_capture.py                 # 3 cycles, 90s each, 1.5s cadence
  python3 timeline_capture.py --cycles 5 --duration 90
  python3 timeline_capture.py --no-press      # passive: macro driven elsewhere
"""
import argparse
import glob
import importlib.util
import os
import random
import sys
import time
from datetime import datetime

import cv2

HERE = os.path.dirname(os.path.abspath(__file__))

# Import the live detector as a module so we reuse its exact servo/gate code
# (its CLI is __main__-guarded, so importing it runs no hunt).
spec = importlib.util.spec_from_file_location(
    "sd", os.path.join(HERE, "shiny_detector_loop_seam.py"))
sd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sd)


def init_servo():
    """Bring up the hardware-PWM servo + press watchdog exactly as run_live does.

    Returns (servo, watchdog) or (None, None) if the Pi hardware isn't present
    (e.g. running on the Mac) — caller falls back to passive mode.
    """
    try:
        servo = sd.HardwarePWMServo()
        servo.value = sd.SERVO_REST
        time.sleep(0.5)  # let the arm settle at REST before the first press

        def _force_rest():
            try:
                servo.value = sd.SERVO_REST
                time.sleep(0.05)
            except Exception as e:
                print(f"[WATCHDOG] _force_rest exception: {e}")

        watchdog = sd.PressWatchdog(_force_rest)
        watchdog.start()
        print(f"[TL] Servo + watchdog up (chip={sd.HWPWM_CHIP}, "
              f"channel={sd.HWPWM_CHANNEL})")
        return servo, watchdog
    except Exception as e:
        print(f"[TL] Servo init failed ({e}) — falling back to PASSIVE mode "
              f"(no APG press; anchor = tool start).")
        return None, None


def observe_cycle(cap, cycle_idx, out_dir, config_name, interval, duration):
    """Grab a frame every `interval`s for `duration`s, anchored at call time.

    Returns a dict:
      first_summary_s — elapsed seconds at the FIRST gate-pass, or None if the
                        summary never appeared inside the window
      grabs           — list of (t, gate_passed, purple, star_conf) per grab
      shiny_seen      — True if star detection ever fired (warn-only)
    """
    cdir = os.path.join(out_dir, f"cycle_{cycle_idx:02d}")
    os.makedirs(cdir, exist_ok=True)

    t0 = time.time()
    deadline = t0 + duration
    first_summary_s = None
    shiny_seen = False
    grabs = []

    print(f"\n[TL] ── Cycle {cycle_idx}: observing {duration:.0f}s "
          f"@ {interval:.1f}s cadence ──")
    while time.time() < deadline:
        # Flush the capture buffer so we read a FRESH frame, not a stale one
        # sitting in the driver queue (same 5-read flush the live grab uses).
        for _ in range(5):
            cap.read()
        ok, frame = cap.read()
        t = time.time() - t0
        if not ok or frame is None:
            print(f"  t={t:5.1f}s  [WARN] grab failed")
            time.sleep(interval)
            continue

        gate_passed, purple = sd.is_summary_screen(frame, config_name)
        is_shiny, conf, _ = sd.detect_star_frame(frame, config_name)
        grabs.append((t, gate_passed, purple, conf))

        cv2.imwrite(os.path.join(cdir, f"t{t:05.1f}.png"), frame)

        flag = ""
        if gate_passed and first_summary_s is None:
            first_summary_s = t
            flag = "  ← FIRST SUMMARY"
        if is_shiny:
            shiny_seen = True
            flag += "  ★★ STAR DETECTED ★★"
        print(f"  t={t:5.1f}s  gate={'PASS' if gate_passed else 'block'}  "
              f"purple={purple:>3}  star_conf={conf:>4.0%}{flag}")

        time.sleep(interval)

    if first_summary_s is None:
        print(f"  → summary NEVER appeared in {duration:.0f}s")
    else:
        late = first_summary_s > sd.SUMMARY_POLL_MAX_S
        print(f"  → first summary at t={first_summary_s:.1f}s"
              + (f"  (LATE — past the {sd.SUMMARY_POLL_MAX_S:.0f}s default poll!)"
                 if late else ""))
    if shiny_seen:
        print(f"  ★★ WARNING: star detection fired this cycle — this tool does "
              f"NOT abort. Check the rig if a real shiny is on screen. ★★")

    return {"first_summary_s": first_summary_s, "grabs": grabs,
            "shiny_seen": shiny_seen, "dir": cdir}


def report(results, duration):
    """Print the verdict: window-drift vs. genuine stall."""
    print(f"\n{'='*60}\nTIMELINE SUMMARY ({len(results)} cycles)\n{'='*60}")
    times = []
    nevers = 0
    late = 0
    for i, r in enumerate(results, 1):
        ts = r["first_summary_s"]
        if ts is None:
            nevers += 1
            print(f"  cycle {i:>2}: summary NEVER appeared (≥{duration:.0f}s)")
        else:
            times.append(ts)
            tag = f"  ← LATE (> {sd.SUMMARY_POLL_MAX_S:.0f}s)" if ts > sd.SUMMARY_POLL_MAX_S else ""
            if ts > sd.SUMMARY_POLL_MAX_S:
                late += 1
            print(f"  cycle {i:>2}: first summary @ {ts:5.1f}s{tag}")

    print(f"\n  summaries seen : {len(times)}/{len(results)}")
    print(f"  never appeared : {nevers}/{len(results)}")
    print(f"  appeared LATE  : {late}/{len(results)}  "
          f"(past the {sd.SUMMARY_POLL_MAX_S:.0f}s default poll window)")
    if times:
        times_sorted = sorted(times)
        med = times_sorted[len(times_sorted) // 2]
        print(f"  appearance time: min={min(times):.1f}s  "
              f"median={med:.1f}s  max={max(times):.1f}s")

    print(f"\n  VERDICT:")
    if nevers == 0 and late > 0:
        print(f"    → WINDOW DRIFT confirmed. The summary appears every cycle, but "
              f"{late} landed past the 14s default poll — the old single-grab/14s-poll "
              f"would log those as 'stuck dialogue' when the macro was just running "
              f"late. FIX is on the CAPTURE side: widen --summary-poll-max (and/or "
              f"re-center the grab), not the macro.")
    elif nevers == 0 and late == 0:
        print(f"    → No drift, no stall in this sample — every summary landed inside "
              f"14s. If the live hunt still blocks ~29%, the blocked cycles are rarer "
              f"than this sample caught; run more --cycles to provoke one.")
    elif nevers > 0 and len(times) > 0:
        print(f"    → MIXED: {len(times)} cycles reached the summary (some maybe late) "
              f"but {nevers} never did within {duration:.0f}s. Both a window-drift tax "
              f"AND a genuine-stall tail exist. Widen the window first (cheap), then "
              f"harden whatever's stalling the {nevers} dead cycles.")
    else:
        print(f"    → GENUINE STALL. The summary never appeared in any cycle within "
              f"{duration:.0f}s — this is NOT a capture-window problem. The macro is "
              f"truly desyncing before the summary; the fix is macro/input reliability "
              f"(dialogue-dismissal hardening, controller USB cable).")
    print(f"{'='*60}")


def main():
    ap = argparse.ArgumentParser(
        description="Single-cycle summary-appearance timeline (stuck vs. late diagnostic)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("--source", default="0",
                    help="Capture device index or path (default 0 = the dongle).")
    ap.add_argument("--config", default="frlg_star",
                    help="Detection config (default frlg_star).")
    ap.add_argument("--cycles", type=int, default=3,
                    help="How many cycles to observe (default 3).")
    ap.add_argument("--interval", type=float, default=1.5,
                    help="Seconds between grabs (default 1.5 — matches the live poll cadence).")
    ap.add_argument("--duration", type=float, default=90.0,
                    help="Seconds to observe per cycle (default 90). Must be ≥60 so the "
                         "inter-cycle APG press lands at the title screen, never mid-macro.")
    ap.add_argument("--no-press", action="store_true",
                    help="PASSIVE mode: don't touch the servo. Use only if the macro is "
                         "being driven by something else; the anchor is tool start, not a "
                         "clean APG press, so first-summary times are approximate.")
    args = ap.parse_args()

    if args.duration < 60 and not args.no_press:
        ap.error(f"--duration {args.duration} < 60s is unsafe when pressing: the macro "
                 f"is ~45-50s, so a short window would press APG mid-macro and corrupt "
                 f"the cycle. Use ≥60, or --no-press for passive observation.")

    # Open the capture device (persistent) and warm it up.
    src = int(args.source) if args.source.isdigit() else args.source
    cap = cv2.VideoCapture(src)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    time.sleep(2)
    for _ in range(30):
        cap.read()
    if not cap.isOpened():
        print(f"[TL] ERROR: could not open capture source {args.source}")
        return 1
    print(f"[TL] Capture open on source {args.source}")

    # NOTE: datetime.now() is fine here — this is a one-shot diagnostic, not a
    # resumable workflow; we just need a unique output folder name.
    runstamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(HERE, "timeline_capture", runstamp)
    os.makedirs(out_dir, exist_ok=True)
    print(f"[TL] Frames → {out_dir}")
    print(f"[TL] Gate default poll window for reference: "
          f"SUMMARY_POLL_MAX_S = {sd.SUMMARY_POLL_MAX_S:.0f}s")

    servo, watchdog = (None, None)
    if not args.no_press:
        servo, watchdog = init_servo()
        if servo is None:
            print("[TL] No servo — continuing in PASSIVE mode.")

    results = []
    try:
        for c in range(1, args.cycles + 1):
            # Anchor the cycle. With a servo, press APG (clean t=0 at macro
            # start). Passive mode just starts observing where the macro already
            # is. The press happens BEFORE observing, and the prior cycle's full
            # DURATION (≥60s) guarantees the macro is back at title by now.
            if servo is not None:
                print(f"\n[TL] Cycle {c}: pressing APG (macro start = t=0)...")
                sd.press_button(servo, watchdog=watchdog)
            else:
                print(f"\n[TL] Cycle {c}: PASSIVE — observing from here (no press).")

            results.append(
                observe_cycle(cap, c, out_dir, args.config,
                              args.interval, args.duration))

            # Inject the same loop-seam variance the live hunt uses, so the
            # next press reproduces real conditions (irrelevant to summary
            # timing, but keeps the cycle honest). Skip after the last cycle.
            if servo is not None and c < args.cycles:
                delay = random.uniform(sd.RANDOM_DELAY_MIN, sd.RANDOM_DELAY_MAX)
                time.sleep(delay)
    except KeyboardInterrupt:
        print("\n[TL] Interrupted — reporting what we have.")
    finally:
        if watchdog is not None:
            watchdog.stop()
        if servo is not None:
            try:
                servo.value = sd.SERVO_REST
                time.sleep(0.1)
                servo.close()
            except Exception:
                pass
        cap.release()

    if results:
        report(results, args.duration)
    return 0


if __name__ == "__main__":
    sys.exit(main())
