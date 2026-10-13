#!/bin/bash
# start_hunt.sh — one-shot hunt launcher: refuse to start if anything is still
# running, then launch the detector and the Telegram alert watcher, detached.
#
# Edit the four variables below for your hunt, then:  bash start_hunt.sh
# Stop a hunt with:  pkill -INT -f shiny_detector_loop_seam   (NEVER kill -9 — a
# hard kill can strand the servo arm on the button, which after ~3 s puts the
# controller into record mode and wipes your macro.)
#
# Preconditions (see SETUP-WITH-AI.md): the game is parked where your macro
# expects it (title screen for the starter macro, the gift-mon room for the
# gift-mon macros), the controller is awake and tethered, the capture card is
# showing the game, and ~/.telegram_alert.json exists if you want alerts.

PROFILE="frlg_star"        # detection profile: frlg_star | frlg_eevee | frlg_hitmonlee | your own
LABEL="Bulbasaur"          # name used in Telegram messages
LOGPREFIX="hunt_live"      # log file becomes hunt_live_<timestamp>.log
# Timing flags. These are the values that ran the Bulbasaur hunt on a ~54 s
# macro. Re-measure with timeline_capture.py when you record a new macro.
# The seam window (0.1 to 30 s) is what beats the seed lock; without it the
# script falls back to 0.1 to 2.1 s, which reaches only ~5 % of the seeds.
DETECTOR_FLAGS="--cycle-wait 18 --summary-poll-max 14 --post-summary-wait 32 --stats-grab-delay 13.0 --seam-delay-min 0.1 --seam-delay-max 30.0"
# Gift-mon (in-game seam) hunts used instead:
#   DETECTOR_FLAGS="--single-grab --cycle-wait 18 --summary-poll-max 8 --post-summary-wait 36 --seam-delay-min 0.1 --seam-delay-max 8.0"

cd "$(dirname "$0")" || exit 1
{
echo "START $(date)"
# Wait for a timeline_capture run to finish if one is still going.
for k in $(seq 1 10); do pgrep -f timeline_capture.py >/dev/null || break; sleep 3; done
if pgrep -af "shiny_detector|alert_watcher|timeline_capture" | grep -v pgrep; then
    echo "ABORT: something is still running (stop it with pkill -INT first)"; exit 2
fi
rm -f hunt_state.json   # --new-hunt below starts the tally at 1; drop this line + --new-hunt to resume
nohup python3 -u shiny_detector_loop_seam.py --mode star --source 0 --live \
    --config "$PROFILE" --new-hunt $DETECTOR_FLAGS \
    > "${LOGPREFIX}_$(date +%Y%m%d_%H%M%S).log" 2>&1 < /dev/null &
disown
sleep 5
if [ -f ~/.telegram_alert.json ]; then
    nohup python3 -u alert_watcher.py --glob "${LOGPREFIX}_*.log" --label "$LABEL" \
        > "alert_watcher_$(date +%Y%m%d_%H%M%S).log" 2>&1 < /dev/null &
    disown
else
    echo "no ~/.telegram_alert.json — running without phone alerts"
fi
sleep 2
echo "LAUNCHED $(date)"
pgrep -af "shiny_detector|alert_watcher" | grep -v pgrep | cut -c1-90
ls -t "${LOGPREFIX}"_*.log | head -1
} > start_hunt.out 2>&1
cat start_hunt.out
