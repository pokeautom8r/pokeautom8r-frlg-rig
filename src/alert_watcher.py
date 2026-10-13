#!/usr/bin/env python3
"""
alert_watcher.py — independent shiny/stall alert watcher for the FRLG hunts.

Runs SEPARATELY from shiny_detector_loop_seam.py so the alert layer is
decoupled from the detector (survives detector restarts, and doesn't require
touching / restarting a live hunt to add alerting). Watches a hunt log and
sends a Telegram push when:

  1. SHINY   — the log shows "SHINY FOUND after" or a "★ SHINY!" reset line.
  2. STALL   — the detector's graceful stall guard wrote a "[STALL]" line.
  3. SILENCE — no new "Reset #" line for > --silence-secs (default 240s, ~4x
               the ~63s cadence). Catches freezes / crashes / console-off /
               capture death that DON'T trip the graceful stall.

Also sends a startup ping and a periodic heartbeat so absence-of-heartbeat
itself signals the watcher (or Wi-Fi) is down.

Secrets live in a config file (default ~/.telegram_alert.json) so the bot
token never appears in `ps`/argv:
    { "token": "123456789:AAE...", "chat_id": "123456789" }

Usage:
    python3 alert_watcher.py --glob "hunt_*.log" --label Bulbasaur
    python3 alert_watcher.py --log ~/hunt_frlg_20260101_120000.log
"""

import argparse
import glob as globmod
import json
import os
import re
import sys
import time
from datetime import datetime

try:
    import requests
except ImportError:
    print("ERROR: requests not installed (pip3 install requests)", file=sys.stderr)
    sys.exit(1)

RESET_RE = re.compile(r"Reset #\s*([\d,]+)")
SHINY_RE = re.compile(r"SHINY FOUND after|★ SHINY!")
STALL_RE = re.compile(r"\[STALL\]")
UNVERIFIED_RE = re.compile(r"\[UNVERIFIED\]")   # 9/19: gate-failed cycle, needs a human look


def load_config(path):
    path = os.path.expanduser(path)
    with open(path) as f:
        cfg = json.load(f)
    if not cfg.get("token") or not cfg.get("chat_id"):
        raise ValueError(f"{path} must contain 'token' and 'chat_id'")
    return cfg["token"], str(cfg["chat_id"])


def send_telegram(token, chat_id, text, retries=3):
    """Best-effort Telegram push. Returns True on success."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for attempt in range(retries):
        try:
            r = requests.post(
                url,
                json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
                timeout=10,
            )
            if r.status_code == 200 and r.json().get("ok"):
                return True
            print(f"[tg] non-ok response: {r.status_code} {r.text[:200]}", file=sys.stderr)
        except Exception as e:  # network hiccup / Wi-Fi outage — retry
            print(f"[tg] send attempt {attempt+1} failed: {e}", file=sys.stderr)
        time.sleep(2 * (attempt + 1))
    return False


def pick_log(args):
    if args.log:
        return args.log
    matches = globmod.glob(os.path.expanduser(os.path.join(args.dir, args.glob)))
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)  # newest


def scan_existing(path):
    """Scan the whole log once at startup. Returns (last_reset_str, saw_shiny,
    saw_stall) so we alert on a shiny/stall that happened BEFORE the watcher
    started, and seed the silence clock from the last reset line."""
    last_reset = None
    saw_shiny = saw_stall = False
    try:
        with open(path, errors="replace") as f:
            for line in f:
                if SHINY_RE.search(line):
                    saw_shiny = True
                if STALL_RE.search(line):
                    saw_stall = True
                m = RESET_RE.search(line)
                if m:
                    last_reset = m.group(1)
    except FileNotFoundError:
        pass
    return last_reset, saw_shiny, saw_stall


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="~/.telegram_alert.json")
    ap.add_argument("--dir", default=os.path.expanduser("~"),
                    help="Directory to search for --glob (default: home)")
    ap.add_argument("--glob", default="hunt_*.log",
                    help="Glob to auto-pick the newest matching hunt log")
    ap.add_argument("--log", default=None,
                    help="Explicit log path (overrides --glob)")
    ap.add_argument("--poll", type=float, default=20.0, help="Poll interval (s)")
    ap.add_argument("--silence-secs", type=float, default=240.0,
                    help="Alert if no new Reset # line for this long")
    ap.add_argument("--heartbeat-hours", type=float, default=6.0,
                    help="Send an 'alive' heartbeat every N hours (0=off)")
    ap.add_argument("--label", default="FRLG", help="Hunt label in messages (e.g. Bulbasaur)")
    args = ap.parse_args()

    token, chat_id = load_config(args.config)

    path = pick_log(args)
    if not path:
        print(f"No log matching {args.glob} in {args.dir}", file=sys.stderr)
        sys.exit(1)

    # Startup: scan existing content so a pre-existing shiny/stall still alerts.
    last_reset, saw_shiny, saw_stall = scan_existing(path)
    alerted_shiny = alerted_stall = False
    if saw_shiny:
        send_telegram(token, chat_id,
                      f"\U0001f31f {args.label}: SHINY already in the log at startup "
                      f"(reset #{last_reset}) — GO LOOK NOW.")
        alerted_shiny = True
    if saw_stall:
        send_telegram(token, chat_id,
                      f"⚠️ {args.label}: hunt already STALLED at startup "
                      f"(last reset #{last_reset}).")
        alerted_stall = True

    send_telegram(
        token, chat_id,
        f"✅ Alert watcher ONLINE for the {args.label} hunt.\n"
        f"Watching: {os.path.basename(path)} (reset #{last_reset or '?'})\n"
        f"Will ping on shiny, stall, or {int(args.silence_secs)}s of silence.")

    pos = os.path.getsize(path)  # follow new lines from here
    last_activity = time.time()
    alerted_silence = False
    last_heartbeat = time.time()

    while True:
        time.sleep(args.poll)

        # Re-pick in case the detector was relaunched into a newer log file.
        newest = pick_log(args)
        if newest and newest != path:
            path = newest
            pos = 0
            last_reset, s_shiny, s_stall = scan_existing(path)
            alerted_shiny = alerted_stall = alerted_silence = False
            last_activity = time.time()
            send_telegram(token, chat_id,
                          f"\U0001f504 {args.label}: now watching a NEW log "
                          f"{os.path.basename(path)} (reset #{last_reset or '?'}).")

        # Read appended lines.
        try:
            size = os.path.getsize(path)
            if size < pos:      # truncated/rotated
                pos = 0
            with open(path, errors="replace") as f:
                f.seek(pos)
                new = f.read()
                pos = f.tell()
        except FileNotFoundError:
            continue

        for line in new.splitlines():
            if SHINY_RE.search(line) and not alerted_shiny:
                m = RESET_RE.search(line)
                rst = m.group(1) if m else last_reset
                send_telegram(
                    token, chat_id,
                    f"\U0001f31f\U0001f31f SHINY {args.label.upper()}! \U0001f31f\U0001f31f\n"
                    f"Reset #{rst}. The macro is HALTED and the shiny is on "
                    f"screen. Go to the rig and save it before anything else!")
                alerted_shiny = True
            if STALL_RE.search(line) and not alerted_stall:
                m = RESET_RE.search(line)
                rst = m.group(1) if m else last_reset
                send_telegram(
                    token, chat_id,
                    f"⚠️ {args.label} hunt STALLED (reset #{rst}). "
                    f"Detector stopped gracefully — check Switch / controller / "
                    f"capture card, then relaunch.")
                alerted_stall = True
            if UNVERIFIED_RE.search(line):
                # 9/19: every gate-failed cycle gets paged (this is how two shinies were lost).
                send_telegram(
                    token, chat_id,
                    f"\U0001f50d {args.label}: a reset could NOT be verified by the summary gate.\n"
                    f"{line.strip()[:220]}\n"
                    f"Frame is saved on the Pi (reset_*.png) - eyeball it when you can.")
            m = RESET_RE.search(line)
            if m:
                last_reset = m.group(1)
                last_activity = time.time()
                if alerted_silence:  # activity resumed — re-arm silence alert
                    alerted_silence = False
                    send_telegram(token, chat_id,
                                  f"✅ {args.label}: resets resumed (now #{last_reset}).")

        # Silence detection (freeze/crash/console-off that didn't stall cleanly).
        if (time.time() - last_activity) > args.silence_secs and not alerted_silence:
            mins = int((time.time() - last_activity) / 60)
            send_telegram(
                token, chat_id,
                f"⚠️ {args.label}: NO new resets for ~{mins} min "
                f"(last #{last_reset}). Detector may have frozen/crashed or the "
                f"console is off. Go check — could also be a shiny that didn't "
                f"log cleanly.")
            alerted_silence = True

        # Heartbeat.
        if args.heartbeat_hours > 0 and \
                (time.time() - last_heartbeat) > args.heartbeat_hours * 3600:
            send_telegram(token, chat_id,
                          f"❤️ {args.label} watcher alive — reset #{last_reset}, "
                          f"{datetime.now():%H:%M %m/%d}.")
            last_heartbeat = time.time()


if __name__ == "__main__":
    main()
