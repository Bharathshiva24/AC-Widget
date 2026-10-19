"""The contract every AC brand driver implements.

A driver has two jobs: connect to the manufacturer's service (or device) and
turn the panel's generic actions into whatever that brand understands. It also
declares what the air conditioner can actually do, through `Capabilities`, so
the panel can render only the controls that brand supports.

Nothing here imports a vendor library, so this module stays importable during
setup, before any brand's dependencies are installed.
"""

DEFAULT_TEMP = {"min": 16, "max": 30, "step": 1}


class AuthError(Exception):
    """The service rejected the saved credentials. Re-running setup is the fix."""


class Field:
    """One credential the setup flow asks for."""

    def __init__(self, name, label, secret=False, example="", required=True):
        self.name = name
        self.label = label
        self.secret = secret
        self.example = example
        self.required = required

    def as_dict(self):
        return {"name": self.name, "label": self.label, "secret": self.secret,
                "example": self.example, "required": self.required}


class Capabilities:
    """What one air conditioner can do.

    The panel renders only what is listed here: a brand with no swing control
    leaves `swing` empty and that knob disappears, a brand with no eco mode
    leaves `presets` empty and that row disappears.

    Each list holds (value, label) pairs. `value` is what comes back to
    `ACDriver.command()`; `label` is what the person reads on screen. Order is
    the order shown, so list fan speeds from gentlest to strongest.
    """

    def __init__(self, modes=(), fan=(), swing=(), presets=(), toggles=(),
                 temp=None, room_temp=True):
        self.modes = [tuple(x) for x in modes]
        self.fan = [tuple(x) for x in fan]
        self.swing = [tuple(x) for x in swing]
        self.presets = [tuple(x) for x in presets]
        self.toggles = [tuple(x) for x in toggles]
        self.temp = dict(temp or DEFAULT_TEMP)
        self.room_temp = bool(room_temp)

    def supports(self, action, value=None):
        """True when this unit accepts `action` (and `value`, when given)."""
        if action in ("power", "temp"):
            return True
        group = {"mode": self.modes, "fan": self.fan,
                 "swing": self.swing, "preset": self.presets}.get(action)
        if group is None:
            group = self.toggles
            if action not in [v for v, _ in self.toggles]:
                return False
            return True
        if value is None:
            return bool(group)
        return any(v == value for v, _ in group)

    def clamp(self, celsius):
        lo, hi = self.temp["min"], self.temp["max"]
        return max(lo, min(hi, round(float(celsius))))

    def as_dict(self):
        return {"modes": [list(x) for x in self.modes],
                "fan": [list(x) for x in self.fan],
                "swing": [list(x) for x in self.swing],
                "presets": [list(x) for x in self.presets],
                "toggles": [list(x) for x in self.toggles],
                "temp": self.temp, "room_temp": self.room_temp}


class Device:
    """One air conditioner, as the panel sees it."""

    def __init__(self, id, name, caps):
        self.id = str(id)
        self.name = name or "Air conditioner"
        self.caps = caps

    def snapshot(self):
        """Current state as plain JSON-safe values.

        Returns keys: online, power, temp, room_temp, mode, fan, swing,
        preset, toggles. Anything the unit does not have may be None (or an
        empty dict for toggles).
        """
        raise NotImplementedError


class ACDriver:
    """Base class for a brand integration.

    Subclasses set the class attributes, then implement `connect()` and
    `command()`. See template.py for a worked skeleton.
    """

    id = "generic"
    label = "Generic"
    connectivity = "Cloud"          # shown in setup: Cloud, Local network, Infrared…
    fields = ()                     # credentials setup should collect
    notes = ""                      # one line shown under the brand in setup
    requires = ()                   # pip packages this driver needs
    retry_seconds = 15              # wait before retrying a failed connect

    def __init__(self, credentials=None):
        self.credentials = dict(credentials or {})

    def missing_fields(self):
        return [f.name for f in self.fields
                if f.required and not str(self.credentials.get(f.name, "")).strip()]

    async def connect(self):
        """Sign in and return a list of Device objects. Raise AuthError if the
        credentials are refused, any other exception for a temporary failure."""
        raise NotImplementedError

    async def command(self, device, action, value):
        """Apply one change. `action` is power, temp, mode, fan, swing, preset,
        or the name of one of this unit's toggles."""
        raise NotImplementedError

    async def close(self):
        """Release sockets and background tasks. Safe to call twice."""
