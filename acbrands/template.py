"""Starting point for a new brand. Copy this file, fill it in, register it.

Three steps to add a manufacturer:

  1. Copy this file to acbrands/<yourbrand>.py and implement connect(),
     command() and Device.snapshot().
  2. Declare what the unit can do in CAPS. Leave a list empty and the panel
     hides that control, so you never have to touch the UI code.
  3. Add one line to the BRANDS table in acbrands/__init__.py.

Then `bash setup.sh` offers the brand, collects the fields declared below,
stores them in the macOS Keychain and the panel uses them. No other file in
the project needs to change.

Connectivity notes for common cases:
  * Cloud account (MirAIe, SmartThings, Comfort Cloud…) — ask for the login in
    `fields`, sign in inside connect().
  * Local network (ESPHome, a LAN API, Tuya local keys) — ask for the host or
    device key, then talk to it directly; set connectivity = "Local network".
  * Infrared blaster (Broadlink and friends) — the unit cannot report back, so
    track the last command you sent and return it from snapshot(); set
    room_temp=False unless the blaster has a thermometer.
"""

from .base import ACDriver, AuthError, Capabilities, Device, Field

CAPS = Capabilities(
    modes=[("cool", "Cool"), ("heat", "Heat"), ("fan", "Fan")],
    fan=[("low", "Low"), ("high", "High")],
    swing=[],                      # empty: this unit has no swing control
    presets=[],                    # empty: no eco/powerful modes
    toggles=[],                    # e.g. [("display", "Display")]
    temp={"min": 16, "max": 30, "step": 1},
    room_temp=True,                # False if the unit cannot report the room
)


class TemplateDevice(Device):
    def __init__(self, raw):
        super().__init__(raw["id"], raw["name"], CAPS)
        self.raw = raw

    def snapshot(self):
        """Return the unit's current state, using the values declared in CAPS."""
        raise NotImplementedError("Report the current state here")


class TemplateDriver(ACDriver):
    id = "template"
    label = "Example brand"
    connectivity = "Cloud"
    notes = "Not a real integration — a copy-me starting point."
    requires = ()                  # pip packages, e.g. ("some-vendor-sdk",)
    fields = (
        Field("username", "Account email"),
        Field("password", "Account password", secret=True),
    )

    async def connect(self):
        """Sign in and return a list of Device objects.

        Raise AuthError when the credentials are refused (the panel then tells
        the person to run setup again and stops retrying for a while). Raise
        anything else for a temporary problem, which is retried.
        """
        raise NotImplementedError("Sign in and return devices here")

    async def command(self, device, action, value):
        """Apply one change: power, temp, mode, fan, swing, preset, or a toggle
        name from CAPS. Values arrive exactly as declared in CAPS."""
        raise NotImplementedError("Send the command to the unit here")

    async def close(self):
        """Close sockets and cancel background tasks."""
