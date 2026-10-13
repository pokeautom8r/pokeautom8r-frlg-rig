#!/usr/bin/env python3
"""
hunt_check.py — health check for a running (or finished) hunt, without touching
the TV or the console.

Reads the newest hunt log and screenshot folder in the current directory and
prints:
  * reset count, time of the last reset, cycle cadence (min / median / p95 / max)
  * counts of UNVERIFIED, POLL-retry, PARK-GUARD, STALL, SHINY and 2ND-GRAB lines
  * a re-run of the summary gate + star-pixel count over every saved reset frame
  * consecutive identical stat panels (a crude duplicate-seed check)
  * a contact sheet (JPEG) of the last 50 frames so you can eyeball them

Run it from the folder that holds shiny_detector_loop_seam.py, the hunt logs and
the hunt_screenshots_* folders (on the Pi that is usually your home directory):

    python3 hunt_check.py                                   # defaults
    python3 hunt_check.py --config frlg_star --glob "hunt_live_*.log" --out check.jpg

Why this exists: turning the TV on to look at the game can change the HDMI
handshake and desync the macro mid-cycle. Checking the saved frames is safe.
"""
import argparse
import glob
import importlib.util
import os
import re
import sys
from datetime import datetime

import cv2
import numpy as np


def load_detector():
    """Import the detector from this folder (or ../src) so we reuse its gate."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(here, "shiny_detector_loop_seam.py"),
                  os.path.join(os.getcwd(), "shiny_detector_loop_seam.py"),
                  os.path.join(here, "..", "src", "shiny_detector_loop_seam.py")]
    for path in candidates:
        if os.path.exists(path):
            spec = importlib.util.spec_from_file_location("sd", path)
            sd = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(sd)
            return sd
    sys.exit("shiny_detector_loop_seam.py not found next to this script, in the "
             "current directory, or in ../src")


def main():
    ap = argparse.ArgumentParser(description="Hunt health check from logs + frames.")
    ap.add_argument("--glob", default="hunt_*.log",
                    help="Glob for the hunt log; the newest match is used (default hunt_*.log)")
    ap.add_argument("--frames", default="hunt_screenshots_*",
                    help="Glob for the screenshot folder; newest match is used")
    ap.add_argument("--config", default="frlg_star",
                    help="Detection profile name in the detector's CONFIG (default frlg_star)")
    ap.add_argument("--out", default="hunt_check_sheet.jpg",
                    help="Contact-sheet output path (JPEG)")
    ap.add_argument("--sheet-frames", type=int, default=50,
                    help="How many of the newest frames go on the contact sheet")
    args = ap.parse_args()

    sd = load_detector()
    if args.config not in sd.CONFIG:
        sys.exit(f"profile '{args.config}' not in CONFIG; choices: {', '.join(sd.CONFIG)}")

    logs = sorted(glob.glob(args.glob), key=os.path.getmtime)
    dirs = sorted(glob.glob(args.frames), key=os.path.getmtime)
    if not logs or not dirs:
        sys.exit(f"no log matching {args.glob} or no folder matching {args.frames} here")
    log, fdir = logs[-1], dirs[-1]

    # ---- log statistics -------------------------------------------------
    lines = open(log, errors="replace").read().splitlines()
    resets = []
    for l in lines:
        m = re.match(r"\[(\d\d:\d\d:\d\d)\] Reset #\s*(\d+)", l)
        if m:
            resets.append((datetime.strptime(m.group(1), "%H:%M:%S"), int(m.group(2))))
    # Skip the first cycle (the detector's warm-up SKIP cycle) when measuring cadence.
    gaps = [(b[0] - a[0]).total_seconds() for a, b in zip(resets[1:], resets[2:])]
    gaps = [g for g in gaps if g >= 0]  # ignore the midnight wrap

    def count(pat):
        return sum(1 for l in lines if pat in l)

    print(f"LOG {log}: resets {resets[-1][1] if resets else 0}, "
          f"last {resets[-1][0].strftime('%H:%M:%S') if resets else '-'}")
    if gaps:
        print(f"cadence s: min {min(gaps):.0f} / median {np.median(gaps):.0f} / "
              f"p95 {np.percentile(gaps, 95):.0f} / max {max(gaps):.0f}  (n={len(gaps)})")
    print(f"UNVERIFIED {count('[UNVERIFIED]')} | POLL retries {count('[POLL]')} | "
          f"PARK-GUARD waits {count('[PARK-GUARD]')} | STALL {count('[STALL]')} | "
          f"SHINY {count('SHINY!')} | 2ND-GRAB {count('[2ND-GRAB]')}")
    for l in lines:
        if any(k in l for k in ("[UNVERIFIED]", "[PARK-GUARD]", "[STALL]", "SHINY!", "[POLL]")):
            print("   ", l.strip()[:150])

    # ---- re-run the gate + star count on every saved frame ---------------
    frames = sorted(glob.glob(os.path.join(fdir, "reset_*.png")))[1:]  # drop the SKIP-cycle frame
    bad, stars = [], []
    rx, ry, rw, rh = sd.CONFIG[args.config]["detect_region"]
    for p in frames:
        f = cv2.imread(p)
        if f is None:
            bad.append(os.path.basename(p) + " (unreadable)")
            continue
        gate_ok, _ = sd.is_summary_screen(f, args.config)
        gx, gy, gw, gh = sd.find_game_bounds(f)
        roi = f[gy + int(ry * gh):gy + int((ry + rh) * gh), gx + int(rx * gw):gx + int((rx + rw) * gw)]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        stars.append(cv2.countNonZero(cv2.inRange(hsv, np.array([20, 80, 150]), np.array([32, 255, 255]))))
        if not gate_ok:
            bad.append(os.path.basename(p))
    print(f"frames {len(frames)}: gate PASS {len(frames) - len(bad)}, FAIL {len(bad)} {bad[:5]}; "
          f"star px max {max(stars) if stars else 0}")

    # ---- duplicate check on the stats panel ------------------------------
    sig = []
    for p in frames:
        f = cv2.imread(p)
        if f is None:
            continue
        gx, gy, gw, gh = sd.find_game_bounds(f)
        panel = cv2.resize(f[gy + int(0.12 * gh):gy + int(0.62 * gh), gx + int(0.55 * gw):gx + int(0.98 * gw)], (64, 32))
        sig.append(cv2.imencode(".png", panel)[1].tobytes())
    dups = sum(1 for a, b in zip(sig, sig[1:]) if a == b)
    print(f"consecutive identical stat panels: {dups}; distinct panels {len(set(sig))}/{len(sig)}")

    # ---- contact sheet of the newest frames ------------------------------
    sel = frames[-args.sheet_frames:]
    tiles = []
    for p in sel:
        f = cv2.imread(p)
        if f is None:
            continue
        gx, gy, gw, gh = sd.find_game_bounds(f)
        t = cv2.resize(f[gy:gy + gh, gx:gx + gw], (240, 160))
        num = re.search(r"reset_(\d+)", p).group(1).lstrip("0") or "0"
        cv2.putText(t, num, (4, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
        tiles.append(t)
    if tiles:
        while len(tiles) % 10:
            tiles.append(np.zeros_like(tiles[0]))
        rows = [np.hstack(tiles[i:i + 10]) for i in range(0, len(tiles), 10)]
        cv2.imwrite(args.out, np.vstack(rows), [cv2.IMWRITE_JPEG_QUALITY, 75])
        print("sheet", args.out, len(sel), "frames")


if __name__ == "__main__":
    main()
