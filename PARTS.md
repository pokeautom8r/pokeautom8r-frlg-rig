# Parts list

What I actually used, with the prices I paid or saw in October 2026. Prices move; the search terms will get you the
current listing.

Nothing in this project was sponsored or supplied free, and none of the links below pay me anything.

| Part | What it is for | Where | Approx. price |
|---|---|---|---|
| GuliKit KK3 Max controller | Records and replays the soft-reset loop; its macro (APG) button is what the servo taps. The plain KK3 has no macro button. | https://www.amazon.com/dp/B0CGWZBPQQ (the B0GKNG7RK8 listing is the same controller) | $80 |
| Raspberry Pi 5 kit, 8 GB (CanaKit PRO, 128 GB card) | Runs the detector. A 4 GB Pi 5 kit works the same for this job. | https://www.amazon.com/dp/B0CT6F25MN | $215 (4 GB kit about $150) |
| USB 3 HDMI capture card, 1080p/4K in, HDCP 2.2, UVC | Turns the Switch's HDMI into a webcam feed for the Pi. Mine is a Macrosilicon "4K HDTV" card with HDMI passthrough, so the TV stays connected without a splitter. | Amazon, search the part name | $25–40 |
| HDMI splitter 1-in-2-out, 4K@60, powered, mirror mode | Only if your capture card has no passthrough and you want the game on a TV while hunting. | Amazon, search the part name | $12–20 |
| MG90S metal-gear micro servo (2–4 pack) | Taps the button. An SG90 works for a first build; the MG90S is quieter and more consistent, and a spare is worth having (one servo went bad after months and started browning out the Pi). | Amazon, search the part name | $8–12 |
| Jumper wires, male-to-female | Three wires from the servo plug to the Pi header. | Amazon, search the part name | $5–7 |
| HDMI cables (2) | Dock to capture card, capture card to TV. | Amazon, search the part name | $6–10 each |
| microSD card, Pi power supply, case with fan | Included in the CanaKit kit. Buy separately if you have a bare Pi 5. | — | — |
| Resin or filament for the cradle | Two parts, `stl/plate2.stl` and `stl/stand3.stl`. Mine were resin on an Elegoo Saturn; FDM PLA is fine. | — | $3–5 of material |
| Servo screws / horn | The MG90S comes with a horn and screws; use the single straight arm. | — | — |

Total if you own none of it: about $350 with the 8 GB Pi kit, about $290 with the 4 GB kit. If you already have a
Pi, the controller plus capture card plus servo is about $120.

Not needed: a separate 5 V supply for the servo, a breadboard, transistors or relays. One micro servo runs happily
from the Pi's 5 V pin. (An early plan used a solenoid with a transistor driver; the servo replaced it before the
first hunt.)

Console: the hunts ran on a Nintendo Switch 2 with the Nintendo Switch Online GBA release of FireRed / LeafGreen.
A Switch 1 works the same. The game is not modified and nothing is plugged into the console except the dock's HDMI
cable and, if you tether it, the controller's USB cable.
