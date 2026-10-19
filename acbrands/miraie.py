"""Panasonic MirAIe — the air conditioners sold in India under the MirAIe app.

Connectivity: the app talks to Panasonic's cloud MQTT broker. The open-source
`miraie-ac-in` package handles the login and the MQTT plumbing; this driver
only maps it onto the panel's generic actions.

Not compatible with Panasonic units that use the global Comfort Cloud app.
"""

from .base import ACDriver, AuthError, Capabilities, Device, Field

# What the panel offers for these units. Fan speeds run gentlest to strongest
# so the rotary knob reads like a dial.
#
# The library also knows HVACMode.HEAT, PresetMode.CLEAN and a display toggle.
# They are left out because the cooling-only units these were tested against
# ignore them; to switch any of them on, add the pair to the matching list:
#     ("heat", "Heat") to MODES, ("clean", "Clean") to PRESETS,
#     ("display", "Display") to TOGGLES.
MODES = [("cool", "Cool"), ("dry", "Dry"), ("fan", "Fan"), ("auto", "Auto")]
FAN = [("auto", "Auto"), ("quiet", "Quiet"), ("low", "Low"),
       ("medium", "Medium"), ("high", "High")]
SWING = [(0, "Auto"), (1, "Level 1"), (2, "Level 2"),
         (3, "Level 3"), (4, "Level 4"), (5, "Level 5")]
PRESETS = [("eco", "Eco"), ("boost", "Powerful")]
TOGGLES = []

CAPS = Capabilities(modes=MODES, fan=FAN, swing=SWING, presets=PRESETS,
                    toggles=TOGGLES, temp={"min": 16, "max": 30, "step": 1},
                    room_temp=True)


def login_id(value):
    """A bare 10-digit Indian mobile number gets its +91 added."""
    value = (value or "").strip().replace(" ", "")
    if value.isdigit() and len(value) == 10:
        return "+91" + value
    if value.isdigit() and len(value) == 12 and value.startswith("91"):
        return "+" + value
    return value


class MirAIeDevice(Device):
    def __init__(self, raw):
        super().__init__(raw.id, raw.friendly_name, CAPS)
        self.raw = raw

    def snapshot(self):
        from miraie_ac import DisplayMode, PowerMode
        st = self.raw.status
        return {
            "online": bool(st.is_online),
            "power": st.power_mode == PowerMode.ON,
            "temp": st.temperature,
            "room_temp": st.room_temperature,
            "mode": st.hvac_mode.value,
            "fan": st.fan_mode.value,
            "swing": st.v_swing_mode.value,
            "preset": st.preset_mode.value,
            "toggles": {"display": st.display_mode == DisplayMode.ON},
        }


class MirAIeDriver(ACDriver):
    id = "miraie"
    label = "Panasonic MirAIe"
    connectivity = "Cloud (MQTT)"
    notes = "Indian-market Panasonic ACs set up in the MirAIe app."
    requires = ("miraie-ac-in",)
    fields = (
        Field("mobile", "MirAIe mobile number", example="+91XXXXXXXXXX"),
        Field("password", "MirAIe password", secret=True),
    )

    def __init__(self, credentials=None):
        super().__init__(credentials)
        self.hub = None

    async def connect(self):
        from miraie_ac import MirAIeBroker, MirAIeHub

        self.hub = MirAIeHub()
        try:
            await self.hub.init(login_id(self.credentials.get("mobile")),
                                self.credentials.get("password", ""),
                                MirAIeBroker())
        except Exception as exc:
            await self.close()
            if "Authentication failed" in str(exc):
                raise AuthError("MirAIe did not accept the saved mobile number "
                                "or password.") from exc
            raise
        devices = list(self.hub.home.devices)
        if not devices:
            await self.close()
            raise RuntimeError("No AC found on this MirAIe account")
        return [MirAIeDevice(d) for d in devices]

    async def command(self, device, action, value):
        from miraie_ac import DisplayMode, FanMode, HVACMode, PresetMode, SwingMode
        raw = device.raw
        if action == "power":
            await (raw.turn_on() if value else raw.turn_off())
        elif action == "temp":
            await raw.set_temperature(device.caps.clamp(value))
        elif action == "mode":
            await raw.set_hvac_mode(HVACMode(value))
        elif action == "fan":
            await raw.set_fan_mode(FanMode(value))
        elif action == "preset":
            await raw.set_preset_mode(PresetMode(value))
        elif action == "swing":
            await raw.set_v_swing_mode(SwingMode(int(value)))
        elif action == "display":
            await raw.set_display_mode(DisplayMode.ON if value else DisplayMode.OFF)
        else:
            raise ValueError("Panasonic MirAIe has no '%s' control" % action)

    async def close(self):
        hub, self.hub = self.hub, None
        if hub is not None:
            try:
                await hub.close()
            except Exception:
                pass

    async def explain_rejection(self, log):
        """Ask MirAIe why the login failed and write its answer to the log.
        Never logs the password itself, only its length."""
        import aiohttp
        from miraie_ac import constants
        mobile = login_id(self.credentials.get("mobile"))
        password = self.credentials.get("password", "")
        log("Login used: %s***%s, password of %d characters"
            % (mobile[:3], mobile[-4:], len(password)))
        try:
            async with aiohttp.ClientSession() as session:
                body = {"clientId": constants.httpClientId, "password": password,
                        "scope": "an_14214235325", "mobile": mobile}
                async with session.post(constants.loginUrl, json=body) as response:
                    text = (await response.text())[:300]
                    if "accessToken" in text:
                        text = "(login accepted)"
                    log("MirAIe answered %s: %s" % (response.status, text))
        except Exception as exc:
            log("Could not ask MirAIe: %s" % exc)
