# AC Widget

A macOS menubar panel for smart air conditioners. Click the menubar item and
you step into a small 3D model of the room, with the AC on the wall and the
controls floating beside it. The theme follows the time of day, and the fan and
swing settings are rotary knobs you can turn.

Built for Panasonic MirAIe units, with a driver layer so other brands can be
added without touching the interface.

| Morning | Evening | Night |
|---|---|---|
| ![Morning](docs/theme-morning.png) | ![Evening](docs/theme-evening.png) | ![Night](docs/theme-night.png) |

## What it does

* **Rooms.** When the AC is off you see the whole home from above; turn it on
  and the view zooms into that room. Several units are supported, one per room.
* **Time of day.** Bright white in the morning, warm amber in the evening, deep
  black at night. It changes on its own and cross-fades.
* **Controls.** Temperature dial, mode, rotary fan-speed and swing knobs, and
  whatever presets the unit supports. Only the controls the selected brand
  reports are drawn.
* **Remote friendly.** Arrow keys move between controls, Enter picks one up,
  arrows then turn it, Enter puts it down.
* **On the TV.** An optional token-guarded endpoint on the home Wi-Fi.

## Requirements

* macOS 11 or newer
* Python 3.9+
* An air conditioner supported by one of the drivers below

## Setup

```bash
git clone https://github.com/Bharathshiva24/AC-Widget.git ~/ac-control
cd ~/ac-control
python3 -m venv venv && source venv/bin/activate
pip install aiohttp pyobjc-framework-WebKit miraie-ac-in
bash setup.sh
```

`setup.sh` asks which brand you have, asks for that brand's credentials, saves
them in the **macOS Keychain**, and installs `AC.app` into Applications. Look
for **AC** in your menubar.

To try it without any hardware:

```bash
python ac_panel.py --demo
```

## Supported brands

| Brand | Connectivity | Status |
|---|---|---|
| Panasonic MirAIe | Cloud (MQTT) | Tested against real units |
| Demo | Simulated | Built in, no hardware needed |

MirAIe support uses the community [`miraie-ac-in`](https://pypi.org/project/miraie-ac-in/)
library. It is unofficial: Panasonic can change their service at any time. It
does not work with Panasonic units that use the global Comfort Cloud app.

## Adding a brand

Every brand lives in one file under `acbrands/` and declares what the unit can
do. The panel reads that declaration and draws only those controls, so a unit
with no swing control simply never gets that knob — no interface code changes.

1. Copy `acbrands/template.py` to `acbrands/yourbrand.py`.
2. Fill in `CAPS` (modes, fan speeds, swing levels, presets, temperature range)
   and implement `connect()`, `command()` and `snapshot()`.
3. Add one line to the `BRANDS` table in `acbrands/__init__.py`.

`setup.sh` then offers the brand and collects the credentials it declares.
`acbrands/template.py` has notes for cloud accounts, local-network devices and
infrared blasters.

## Credentials

Credentials are kept in the macOS Keychain under the service `ac-panel`. The
app reads them into memory at startup and never writes them to disk; nothing
secret is in this repository, and `.gitignore` keeps local tokens and logs out
of it. The panel's own web server binds to a random loopback port behind a
per-run token.

## Built with

Python, aiohttp, PyObjC/WebKit for the menubar popover, and plain HTML and CSS
for the room — the 3D is CSS transforms, no WebGL.

## License

MIT — see [LICENSE](LICENSE).
