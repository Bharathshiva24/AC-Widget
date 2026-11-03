"""A simulated two-room home. No hardware, no account, no network.

Useful for trying the panel, for development, and for seeing how the UI adapts
when a brand supports fewer controls:

    AC_DEMO_CAPS=minimal     only power, temperature and two modes
"""

import hashlib
import os
from datetime import timedelta

from .base import ACDriver, Capabilities, Device, Field

FULL = Capabilities(
    modes=[("cool", "Cool"), ("dry", "Dry"), ("fan", "Fan"), ("auto", "Auto")],
    fan=[("auto", "Auto"), ("quiet", "Quiet"), ("low", "Low"),
         ("medium", "Medium"), ("high", "High")],
    swing=[(0, "Auto"), (1, "Level 1"), (2, "Level 2"),
           (3, "Level 3"), (4, "Level 4"), (5, "Level 5")],
    hswing=[(0, "Auto"), (1, "Far left"), (2, "Left"), (3, "Centre"),
            (4, "Right"), (5, "Far right")],
    presets=[("eco", "Eco"), ("boost", "Powerful")],
    toggles=[("display", "Display")],
    energy=True,
)

# A deliberately plain unit: the panel should hide both knobs and the presets.
MINIMAL = Capabilities(
    modes=[("cool", "Cool"), ("fan", "Fan")],
    fan=[], swing=[], hswing=[], presets=[], toggles=[],
    temp={"min": 18, "max": 28, "step": 1}, room_temp=False,
)


class DemoDevice(Device):
    def __init__(self, id, name, caps, on, temp, room_temp):
        super().__init__(id, name, caps)
        self.state = {
            "online": True,
            "power": on,
            "temp": float(temp),
            "room_temp": float(room_temp) if caps.room_temp else None,
            "mode": caps.modes[0][0] if caps.modes else "cool",
            "fan": caps.fan[2][0] if len(caps.fan) > 2 else None,
            "swing": caps.swing[1][0] if len(caps.swing) > 1 else None,
            "hswing": caps.hswing[3][0] if len(caps.hswing) > 3 else None,
            "preset": "none",
            "toggles": {name: True for name, _ in caps.toggles},
        }

    def snapshot(self):
        return dict(self.state, toggles=dict(self.state["toggles"]))


class DemoDriver(ACDriver):
    id = "demo"
    label = "Demo (no real AC)"
    connectivity = "Simulated"
    notes = "Two pretend units, for trying the panel without any hardware."
    fields = ()

    async def connect(self):
        caps = MINIMAL if os.environ.get("AC_DEMO_CAPS") == "minimal" else FULL
        return [DemoDevice("demo-1", "Bedroom AC", caps, True, 24, 25),
                DemoDevice("demo-2", "Hall AC", caps, False, 26, 29)]

    async def command(self, device, action, value):
        st = device.state
        if action == "power":
            st["power"] = bool(value)
        elif action == "temp":
            st["temp"] = float(device.caps.clamp(value))
        elif action in ("mode", "fan", "swing", "hswing"):
            st[action] = value
        elif action == "preset":
            st["preset"] = value
            if value == "eco":
                st["temp"] = float(device.caps.clamp(26))
        elif action in st["toggles"]:
            st["toggles"][action] = bool(value)
        else:
            raise ValueError("The demo AC has no '%s' control" % action)

    async def energy_daily(self, device, start, end):
        """Invented but stable readings, so the chart looks like a real month.

        Derived from the date and the unit's id rather than randomly, so the
        same day always reads the same and the panel does not flicker. A
        couple of days are left out on purpose: gaps are a real thing the
        service does, and the chart has to survive them.
        """
        readings, day = {}, start
        while day <= end:
            seed = hashlib.md5(("%s%s" % (device.id, day)).encode()).digest()
            if seed[0] < 12:                     # roughly one day in twenty
                day += timedelta(days=1)
                continue
            weekend = 1.25 if day.weekday() >= 5 else 1.0
            readings[day] = round((1.8 + seed[1] / 255.0 * 4.5) * weekend, 2)
            day += timedelta(days=1)
        return readings
