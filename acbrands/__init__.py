"""Brand registry, configuration and credential loading.

Importing this package pulls in no vendor library: each driver is imported only
when its brand is actually selected, so the app runs with one brand installed
and setup works before anything is installed at all.
"""

import importlib
import json
import os
import subprocess

from .base import ACDriver, AuthError, Capabilities, Device, Field  # noqa: F401

# id -> module:class. Add a line here to register a new brand.
BRANDS = {
    "miraie": "acbrands.miraie:MirAIeDriver",
    "demo": "acbrands.demo:DemoDriver",
}

DEFAULT_BRAND = "miraie"          # what an install from before brand support used
KEYCHAIN_SERVICE = "ac-panel"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "config.json")

# Credentials saved before brands existed, kept working.
LEGACY_KEYCHAIN = {("miraie", "mobile"): "mobile", ("miraie", "password"): "password"}
LEGACY_ENV = {("miraie", "mobile"): "MIRAIE_MOBILE", ("miraie", "password"): "MIRAIE_PASSWORD"}


def driver_class(brand_id):
    """Import and return the driver class for a brand id."""
    target = BRANDS.get(brand_id)
    if target is None:
        raise KeyError("Unknown AC brand: %s" % brand_id)
    module_name, class_name = target.split(":")
    return getattr(importlib.import_module(module_name), class_name)


def describe(brand_id):
    """Metadata for one brand, without importing its vendor library."""
    cls = driver_class(brand_id)
    return {"id": cls.id, "label": cls.label, "connectivity": cls.connectivity,
            "notes": cls.notes, "requires": list(cls.requires),
            "fields": [f.as_dict() for f in cls.fields]}


def catalogue():
    """Every registered brand, for the setup flow."""
    return [describe(b) for b in BRANDS]


# ----------------------------------------------------------------- config

def read_config():
    try:
        with open(CONFIG_PATH) as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_config(**values):
    data = read_config()
    data.update(values)
    with open(CONFIG_PATH, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    return data


def selected_brand():
    """The configured brand, falling back to the one this app shipped with."""
    brand = os.environ.get("AC_BRAND") or read_config().get("brand") or DEFAULT_BRAND
    return brand if brand in BRANDS else DEFAULT_BRAND


# ------------------------------------------------------------ credentials

def _keychain(account):
    """Read one secret from the macOS Keychain. Returns None when absent."""
    try:
        done = subprocess.run(
            ["/usr/bin/security", "find-generic-password",
             "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    value = done.stdout.strip() if done.returncode == 0 else ""
    return value or None


def credentials(brand_id):
    """Collect a brand's credentials: environment first, then the Keychain.

    Secrets are never written to disk by this app — setup.sh puts them in the
    macOS Keychain and they are read back into memory here.
    """
    found = {}
    for field in driver_class(brand_id).fields:
        key = (brand_id, field.name)
        value = (os.environ.get("AC_%s_%s" % (brand_id.upper(), field.name.upper()))
                 or os.environ.get(LEGACY_ENV.get(key, ""), "")
                 or _keychain("%s.%s" % (brand_id, field.name))
                 or (_keychain(LEGACY_KEYCHAIN[key]) if key in LEGACY_KEYCHAIN else None))
        if value:
            found[field.name] = value
    return found


def build(brand_id=None):
    """The driver for the configured brand, loaded with its credentials."""
    brand_id = brand_id or selected_brand()
    return driver_class(brand_id)(credentials(brand_id))
