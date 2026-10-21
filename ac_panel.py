"""AC - a macOS menubar control panel for smart air conditioners.

Run:     python ac_panel.py           the configured brand (see config.json)
         python ac_panel.py --demo    a simulated home, no hardware needed
Set up:  bash setup.sh                pick a brand and save its credentials

Brands live in the acbrands package, one module each; the panel renders only
the controls the selected brand reports that it supports.
"""
import asyncio
import json
import os
import secrets
import socket
import sys
import threading
import time

from aiohttp import web

import acbrands

WIDTH, HEIGHT = 700, 520
TOKEN = secrets.token_urlsafe(24)
STATE = {"driver": None, "devices": [], "error": None, "quit": False,
         "height": HEIGHT, "selected": None}
ROOMS = ("bedroom", "hall")
ROOM_WORDS = {"bedroom": ("bed", "master"), "hall": ("hall", "living", "drawing", "lounge")}


# ---------------------------------------------------------------- AC state

def assign_rooms(devices):
    """Decide which AC sits in which room: by name if it says so, otherwise in order."""
    rooms = {}
    for device in devices:
        name = (device.name or "").lower()
        for room in ROOMS:
            if room not in rooms and any(word in name for word in ROOM_WORDS[room]):
                rooms[room] = device
    left = [d for d in devices if d not in rooms.values()]
    free = [r for r in ROOMS if r not in rooms]
    if os.environ.get("AC_SWAP_ROOMS"):
        free.reverse()
    for room, device in zip(free, left):
        rooms[room] = device
    return rooms


def device_state(device, room):
    """One AC as the panel sees it: its state plus what it is able to do."""
    state = dict(device.snapshot())
    state.setdefault("toggles", {})
    state.update(id=device.id, room=room, name=device.name, caps=device.caps.as_dict())
    return state


def snapshot():
    if not STATE["devices"]:
        return {"connected": False, "error": STATE["error"]}
    rooms = assign_rooms(STATE["devices"])
    return {"connected": True, "devices": [device_state(rooms[r], r) for r in ROOMS if r in rooms]}


def menubar_title():
    snap = snapshot()
    if not snap["connected"]:
        return "AC !" if snap["error"] else "AC …"
    chosen = [d for d in snap["devices"] if d["id"] == STATE["selected"]] or snap["devices"]
    current = chosen[0]
    if not current["power"]:
        return "AC Off"
    temp = current["temp"]
    return "AC %d°" % round(temp) if temp is not None else "AC On"


async def run_command(device_id, action, value):
    """Hand one change to the brand driver, after checking the unit supports it."""
    matches = [d for d in STATE["devices"] if d.id == device_id]
    if not matches:
        raise RuntimeError("That AC is not connected")
    device = matches[0]
    if action == "preset" and value == "none":
        pass                       # clearing a preset is always allowed
    elif not device.caps.supports(action, value):
        raise ValueError("%s does not support %s" % (device.name, action))
    await STATE["driver"].command(device, action, value)


def log(message):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


async def connect(brand_id):
    """Keep trying to reach the configured brand until the ACs answer."""
    while not STATE["devices"]:
        driver = acbrands.build(brand_id)
        STATE["driver"] = driver
        missing = driver.missing_fields()
        if missing:
            STATE["error"] = ("%s is not set up yet. Run setup to add your %s: "
                              "bash ~/ac-control/setup.sh"
                              % (driver.label, " and ".join(missing)))
            log("Not configured: missing %s for %s" % (", ".join(missing), brand_id))
            await asyncio.sleep(300)
            continue
        try:
            STATE["devices"] = await driver.connect()
            STATE["error"] = None
            log("Connected to %s: %d AC(s) found" % (driver.label, len(STATE["devices"])))
        except acbrands.AuthError as exc:
            log("Could not connect: %s" % exc)
            explain = getattr(driver, "explain_rejection", None)
            if explain is not None:
                await explain(log)
            STATE["error"] = ("%s Run setup again to re-enter them: "
                              "bash ~/ac-control/setup.sh" % exc)
            await driver.close()
            await asyncio.sleep(600)
        except Exception as exc:   # temporary: show it in the panel and retry
            reason = str(exc) or exc.__class__.__name__
            log("Could not connect: %s" % reason)
            STATE["error"] = reason + ". Trying again in a few seconds."
            await driver.close()
            await asyncio.sleep(driver.retry_seconds)


# ------------------------------------------------------------ local server

def build_app(port):
    allowed_hosts = {"127.0.0.1:%d" % port}

    @web.middleware
    async def guard(request, handler):
        if request.headers.get("Host") not in allowed_hosts:
            return web.Response(status=403)
        if request.path != "/" and request.headers.get("X-Token") != TOKEN:
            return web.Response(status=403)
        return await handler(request)

    async def index(request):
        return web.Response(text=PAGE.replace("__TOKEN__", TOKEN), content_type="text/html")

    async def state(request):
        return web.json_response(snapshot())

    async def cmd(request):
        body = await request.json()
        try:
            await run_command(body.get("id"), body.get("action"), body.get("value"))
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": True})

    async def ui(request):
        body = await request.json()
        STATE["height"] = max(360, min(760, int(body.get("height", HEIGHT))))
        STATE["selected"] = body.get("selected")
        return web.json_response({"ok": True})

    async def quit_app(request):
        STATE["quit"] = True
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[guard])
    app.add_routes([
        web.get("/", index),
        web.get("/state", state),
        web.post("/cmd", cmd),
        web.post("/ui", ui),
        web.post("/quit", quit_app),
    ])
    return app


def build_lan_app(token):
    """Same /state and /cmd as the local panel, reachable from the TV on the home Wi-Fi.
    Guarded by AC_LAN_TOKEN (sent as X-Token or ?token=). Read-only /state, /cmd for control; nothing else."""
    @web.middleware
    async def guard(request, handler):
        if request.method == "OPTIONS":
            return cors(web.Response(status=204))
        sent = request.headers.get("X-Token") or request.query.get("token")
        if sent != token:
            return cors(web.Response(status=403, text="bad token"))
        return cors(await handler(request))

    def cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "X-Token, Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return resp

    async def state(request):
        return web.json_response(snapshot())

    async def cmd(request):
        body = await request.json()
        try:
            await run_command(body.get("id"), body.get("action"), body.get("value"))
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[guard])
    app.add_routes([web.get("/state", state), web.post("/cmd", cmd),
                    web.options("/state", state), web.options("/cmd", cmd)])
    return app


async def start_lan_server(port, token):
    runner = web.AppRunner(build_lan_app(token))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log("TV access on port %d" % port)


async def start_server(sock, port):
    runner = web.AppRunner(build_app(port))
    await runner.setup()
    await web.SockSite(runner, sock).start()


# ----------------------------------------------------------- macOS menubar

def run_menubar(url):
    from AppKit import (NSApplication, NSAppearance, NSPopover, NSStatusBar,
                        NSVariableStatusItemLength, NSViewController)
    from Foundation import NSMakeRect, NSObject, NSTimer, NSURL, NSURLRequest
    from PyObjCTools import AppHelper
    from WebKit import WKWebView, WKWebViewConfiguration

    class Delegate(NSObject):
        def applicationDidFinishLaunching_(self, note):
            self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
            button = self.item.button()
            button.setTitle_(menubar_title())
            button.setTarget_(self)
            button.setAction_("toggle:")

            self.web = WKWebView.alloc().initWithFrame_configuration_(
                NSMakeRect(0, 0, WIDTH, HEIGHT), WKWebViewConfiguration.alloc().init())
            try:
                self.web.setValue_forKey_(False, "drawsBackground")
            except Exception:
                pass
            controller = NSViewController.alloc().init()
            controller.setView_(self.web)

            self.popover = NSPopover.alloc().init()
            self.popover.setContentSize_((WIDTH, HEIGHT))
            self.popover.setBehavior_(1)  # close when clicking elsewhere
            self.popover.setContentViewController_(controller)
            light = NSAppearance.appearanceNamed_("NSAppearanceNameAqua")
            if light is not None:
                self.popover.setAppearance_(light)

            self.web.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))
            self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.2, self, "tick:", None, True)
            self.last_height = HEIGHT
            self.last_title = None

        def toggle_(self, sender):
            if self.popover.isShown():
                self.popover.performClose_(sender)
                return
            button = self.item.button()
            self.popover.showRelativeToRect_ofView_preferredEdge_(button.bounds(), button, 1)
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            self.web.evaluateJavaScript_completionHandler_("window.refresh && window.refresh()", None)

        def tick_(self, timer):
            if STATE["quit"]:
                NSApplication.sharedApplication().terminate_(None)
                return
            title = menubar_title()
            if title != self.last_title:
                self.last_title = title
                self.item.button().setTitle_(title)
            if STATE["height"] != self.last_height:
                self.last_height = STATE["height"]
                self.popover.setContentSize_((WIDTH, self.last_height))

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)  # menubar only, no Dock icon
    delegate = Delegate.alloc().init()
    app.setDelegate_(delegate)
    AppHelper.runEventLoop()


# --------------------------------------------------------------------- main

def load_env_file(path=".env"):
    """Read KEY=value lines from .env, if present.

    Variables already set in the environment win, so the launcher and the
    shell keep precedence. Nothing is written back to disk.
    """
    try:
        with open(path) as handle:
            lines = handle.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def main():
    load_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    brand = "demo" if "--demo" in sys.argv else acbrands.selected_brand()

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    url = "http://127.0.0.1:%d/" % port

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    asyncio.run_coroutine_threadsafe(start_server(sock, port), loop).result(10)
    lan_port, lan_token = os.environ.get("AC_LAN_PORT"), os.environ.get("AC_LAN_TOKEN")
    if lan_port and lan_token:
        try:
            asyncio.run_coroutine_threadsafe(start_lan_server(int(lan_port), lan_token), loop).result(10)
        except Exception as exc:
            log("TV access could not start: %s" % exc)

    asyncio.run_coroutine_threadsafe(connect(brand), loop)
    if brand == "demo":
        print("Demo panel at " + url)

    log("AC started (%s)" % brand)
    if brand == "demo" and "--browser" in sys.argv:
        threading.Event().wait()
    else:
        run_menubar(url)


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AC</title>
<style>
@property --pbg1{syntax:"<color>";inherits:true;initial-value:#2a2c34}
@property --pbg2{syntax:"<color>";inherits:true;initial-value:#16181e}
@property --wall1{syntax:"<color>";inherits:true;initial-value:#555863}
@property --wall2{syntax:"<color>";inherits:true;initial-value:#30323b}
@property --floor1{syntax:"<color>";inherits:true;initial-value:#5a4634}
@property --floor2{syntax:"<color>";inherits:true;initial-value:#6e5641}
@property --hallf1{syntax:"<color>";inherits:true;initial-value:#3a3b44}
@property --hallf2{syntax:"<color>";inherits:true;initial-value:#4b4c56}
@property --boardline{syntax:"<color>";inherits:true;initial-value:rgba(0,0,0,.22)}
@property --win1{syntax:"<color>";inherits:true;initial-value:#1b2b4a}
@property --win2{syntax:"<color>";inherits:true;initial-value:#0a1226}
@property --winglow{syntax:"<color>";inherits:true;initial-value:rgba(80,120,200,.18)}
@property --lampa{syntax:"<number>";inherits:true;initial-value:0.95}
@property --vig{syntax:"<color>";inherits:true;initial-value:rgba(0,0,0,.45)}
@property --glass{syntax:"<color>";inherits:true;initial-value:rgba(18,19,24,.88)}
@property --gtext{syntax:"<color>";inherits:true;initial-value:#ffffff}
@property --gsub{syntax:"<color>";inherits:true;initial-value:#aeb3c2}
@property --gfill{syntax:"<color>";inherits:true;initial-value:rgba(255,255,255,.08)}
@property --gfill2{syntax:"<color>";inherits:true;initial-value:rgba(255,255,255,.14)}
@property --gline{syntax:"<color>";inherits:true;initial-value:rgba(255,255,255,.10)}
@property --ac{syntax:"<color>";inherits:true;initial-value:#2f8cff}
@property --airc{syntax:"<color>";inherits:true;initial-value:rgba(110,190,255,.85)}
@property --ink{syntax:"<color>";inherits:true;initial-value:#f2f3f7}
@property --soft{syntax:"<color>";inherits:true;initial-value:#aeb3c2}
@property --faint{syntax:"<color>";inherits:true;initial-value:#7d8294}
@property --gnd{syntax:"<color>";inherits:true;initial-value:rgba(0,0,0,.45)}

:root{--ease:cubic-bezier(.3,.7,.2,1);--air:2.4s;--hair:var(--gline);--well:var(--gfill);
  transition:--pbg1 .9s,--pbg2 .9s,--wall1 .9s,--wall2 .9s,--floor1 .9s,--floor2 .9s,--hallf1 .9s,--hallf2 .9s,
    --boardline .9s,--win1 .9s,--win2 .9s,--winglow .9s,--lampa .9s,--vig .9s,--glass .9s,--gtext .9s,--gsub .9s,
    --gfill .9s,--gfill2 .9s,--gline .9s,--ac .9s,--airc .9s,--ink .9s,--soft .9s,--faint .9s,--gnd .9s}

:root[data-tod=morning]{--pbg1:#ffffff;--pbg2:#eef1f7;--wall1:#fdfdff;--wall2:#e7eaf2;
  --floor1:#d7bd9b;--floor2:#ecd9bf;--hallf1:#e8ebf3;--hallf2:#f7f8fc;--boardline:rgba(120,84,48,.16);
  --win1:#cfe9ff;--win2:#9ec9f7;--winglow:rgba(150,200,255,.55);--lampa:0;--vig:rgba(40,54,96,.10);
  --glass:rgba(255,255,255,.80);--gtext:#15171d;--gsub:#5b6070;--gfill:rgba(18,24,45,.06);--gfill2:rgba(18,24,45,.11);
  --gline:rgba(18,24,45,.12);--ac:#0a84ff;--airc:rgba(46,140,240,.70);
  --ink:#1a1c22;--soft:#5b6070;--faint:#8b90a0;--gnd:rgba(60,70,110,.20)}

:root[data-tod=evening]{--pbg1:#4a3729;--pbg2:#241a14;--wall1:#6b4f3c;--wall2:#3b2b21;
  --floor1:#5a3a24;--floor2:#71492e;--hallf1:#453126;--hallf2:#574034;--boardline:rgba(0,0,0,.26);
  --win1:#ff9a52;--win2:#5b2b35;--winglow:rgba(255,150,80,.45);--lampa:1;--vig:rgba(30,10,0,.42);
  --glass:rgba(44,28,18,.86);--gtext:#fff3e6;--gsub:#d6b094;--gfill:rgba(255,214,170,.10);--gfill2:rgba(255,214,170,.17);
  --gline:rgba(255,214,170,.16);--ac:#ff9038;--airc:rgba(140,200,255,.80);
  --ink:#ffeedd;--soft:#d6b094;--faint:#a3836a;--gnd:rgba(40,18,0,.45)}

:root[data-tod=night]{--pbg1:#2a2c34;--pbg2:#14161b;--wall1:#555863;--wall2:#30323b;
  --floor1:#5a4634;--floor2:#6e5641;--hallf1:#3a3b44;--hallf2:#4b4c56;--boardline:rgba(0,0,0,.22);
  --win1:#1b2b4a;--win2:#0a1226;--winglow:rgba(80,120,200,.20);--lampa:.95;--vig:rgba(0,0,0,.45);
  --glass:rgba(18,19,24,.88);--gtext:#ffffff;--gsub:#aeb3c2;--gfill:rgba(255,255,255,.08);--gfill2:rgba(255,255,255,.14);
  --gline:rgba(255,255,255,.10);--ac:#2f8cff;--airc:rgba(110,190,255,.85);
  --ink:#f2f3f7;--soft:#aeb3c2;--faint:#7d8294;--gnd:rgba(0,0,0,.45)}
*{box-sizing:border-box;margin:0;-webkit-user-select:none;user-select:none}
[hidden]{display:none!important}
html,body{height:100%;background:transparent;overflow:hidden}
body{font:13px/1.25 -apple-system,BlinkMacSystemFont,"SF Pro Text","Helvetica Neue",Helvetica,sans-serif;color:var(--ink);
  -webkit-font-smoothing:antialiased;cursor:default}
button{font:inherit;color:inherit;border:0;background:none;cursor:pointer;padding:0}
button:focus-visible{outline:2px solid var(--ac);outline-offset:3px}
svg{fill:none;stroke:currentColor;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
/* layout */
.panel{position:relative;height:100%;overflow:hidden;background:radial-gradient(95% 85% at 70% 15%,var(--pbg1) 0%,var(--pbg2) 70%,color-mix(in srgb,var(--pbg2) 72%,#000) 100%)}
.panel:not(.off) .head{opacity:0;pointer-events:none}
.head{position:absolute;left:20px;right:20px;top:14px;display:flex;align-items:center;z-index:6;pointer-events:none}
.head>*{pointer-events:auto}
.name{font-size:16px;font-weight:650;letter-spacing:-.25px}
.status{display:flex;align-items:center;gap:6px;color:var(--soft);font-size:12px;margin-top:2px}
.status i{width:6px;height:6px;border-radius:50%;background:var(--faint)}.status.on i{background:#30d158}
.roomsw{margin-left:auto;margin-right:12px;display:flex;gap:2px;padding:2px;border-radius:10px;background:var(--gfill);
  box-shadow:0 0 0 1px var(--hair);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px)}
.roomsw button{padding:5px 11px;border-radius:8px;font-size:12px;color:var(--soft)}
.roomsw button[aria-pressed=true]{background:var(--gfill2);color:var(--ink);font-weight:600}
.power{width:40px;height:40px;border-radius:50%;display:grid;place-items:center;color:var(--soft);background:var(--gfill);
  box-shadow:0 0 0 1px var(--hair),0 3px 8px rgba(50,60,130,.1);transition:color .3s,box-shadow .3s,transform .12s;position:relative;overflow:hidden}
.power::before{content:"";position:absolute;inset:0;background:var(--accent);opacity:0;transition:opacity .35s}
.power svg{width:18px;height:18px;stroke-width:2;position:relative}
.power.on{color:#fff;box-shadow:0 8px 18px rgba(96,104,250,.38)}.power.on::before{opacity:1}
.power:active{transform:scale(.93)}
.scene{position:absolute;inset:0}


/* the room, seen from inside */
.box{position:absolute;transform-style:preserve-3d}
.f{position:absolute}
.interior{position:absolute;inset:0;perspective:720px;perspective-origin:50% 42%;transition:opacity .7s ease .25s,transform 1.1s var(--ease)}
.off .hud-only{pointer-events:none}
.roomdim{position:absolute;inset:0;pointer-events:none;opacity:0;transition:opacity .7s;
  background:color-mix(in srgb,var(--pbg2) 55%,transparent)}
.off .roomdim{opacity:1}
.off .acair{opacity:0}
.off .acglow{opacity:0}
.off .aclcd{opacity:0}
.off .acvent::after{transform:scaleY(1)}
.acair,.acglow,.aclcd{transition:opacity .6s}
.iw{position:absolute;left:50%;top:50%;width:0;height:0;transform-style:preserve-3d;transform:scale3d(.62,.62,.62)}
.iw>div{position:absolute;transform-origin:0 0}
.wall{background:linear-gradient(180deg,var(--wall1) 0%,var(--wall2) 70%)}
.bwall{left:-520px;top:-340px;width:1040px;height:680px;transform:translateZ(-400px);
  background:radial-gradient(70% 90% at 78% 30%,var(--wall1) 0%,color-mix(in srgb,var(--wall1) 45%,var(--wall2)) 45%,var(--wall2) 100%);box-shadow:inset 0 -14px 0 color-mix(in srgb,var(--wall2) 72%,#000)}
.lwall{left:-520px;top:-340px;width:400px;height:680px;transform:translateZ(-400px) rotateY(90deg) scaleX(-1);transform-origin:0 0;
  background:linear-gradient(90deg,color-mix(in srgb,var(--wall2) 88%,#000),color-mix(in srgb,var(--wall2) 55%,var(--wall1)));box-shadow:inset 0 -14px 0 color-mix(in srgb,var(--wall2) 72%,#000)}
.rwall{left:520px;top:-340px;width:400px;height:680px;transform:translateZ(-400px) rotateY(-90deg);transform-origin:0 0;
  background:linear-gradient(90deg,color-mix(in srgb,var(--wall1) 70%,var(--wall2)) 0%,var(--wall2) 60%,color-mix(in srgb,var(--wall2) 82%,#000) 100%);box-shadow:inset 0 -14px 0 color-mix(in srgb,var(--wall2) 72%,#000)}
.rwall::after{content:"";position:absolute;left:0;top:0;bottom:0;width:120px;opacity:var(--lampa);background:linear-gradient(90deg,rgba(255,196,120,.55),rgba(255,196,120,.18) 50%,transparent)}
.ceil{left:-520px;top:-340px;width:1040px;height:400px;transform:translateZ(-400px) rotateX(90deg);transform-origin:0 0;background:linear-gradient(180deg,color-mix(in srgb,var(--wall1) 72%,var(--wall2)),var(--wall2))}
.floor3{left:-520px;top:340px;width:1040px;height:400px;transform:translateZ(-400px) rotateX(90deg);transform-origin:0 0;transform-style:preserve-3d}
.bedroom-i .floor3{background:repeating-linear-gradient(90deg,var(--boardline) 0 1.5px,transparent 1.5px 60px),linear-gradient(180deg,var(--floor1),var(--floor2))}
.hall-i .floor3{background:repeating-linear-gradient(0deg,var(--boardline) 0 1.5px,transparent 1.5px 90px),
  repeating-linear-gradient(90deg,var(--boardline) 0 1.5px,transparent 1.5px 90px),linear-gradient(180deg,var(--hallf1),var(--hallf2))}
.floor3::before{content:"";position:absolute;inset:0;background:radial-gradient(55% 50% at 62% 25%,color-mix(in srgb,var(--airc) 35%,transparent),transparent 70%),radial-gradient(40% 60% at 100% 50%,rgba(255,190,110,calc(var(--lampa)*.24)),transparent 70%);pointer-events:none}
.window{left:-120px;top:-250px;width:150px;height:210px;transform:translateZ(-398px);border-radius:4px;background:linear-gradient(160deg,var(--win1),color-mix(in srgb,var(--win1) 40%,var(--win2)) 55%,var(--win2));
  box-shadow:0 0 0 8px color-mix(in srgb,var(--wall2) 80%,#000),0 0 0 9px rgba(0,0,0,.3),0 0 70px 14px var(--winglow)}
.window::after{content:"";position:absolute;inset:0;background:linear-gradient(color-mix(in srgb,var(--wall2) 80%,#000) 0 0) 50%/8px 100% no-repeat,linear-gradient(color-mix(in srgb,var(--wall2) 80%,#000) 0 0) 50% 45%/100% 8px no-repeat}
.frame{left:-250px;top:-190px;width:120px;height:90px;transform:translateZ(-398px);background:linear-gradient(135deg,#6a5a4a,#3b3229);box-shadow:0 0 0 6px color-mix(in srgb,var(--wall2) 80%,#000),0 0 0 7px rgba(0,0,0,.3)}
.lamp{left:470px;top:-340px;width:50px;height:680px;transform:translateZ(-399px);background:linear-gradient(90deg,transparent,rgba(255,200,130,calc(var(--lampa)*.55)) 70%,rgba(255,214,160,var(--lampa)));filter:blur(2px)}
.shade{position:absolute;inset:0;pointer-events:none;background:radial-gradient(75% 65% at 55% 40%,transparent 45%,var(--vig) 100%)}
.box .f{position:absolute}

/* the AC on the back wall */
.acunit{left:120px;top:-270px;width:300px;height:88px;border-radius:14px 14px 22px 22px;transform:translateZ(-386px);
  background:linear-gradient(180deg,#fdfdff 0%,#eef0f5 50%,#d7dae3 100%);
  box-shadow:0 0 0 1px rgba(0,0,0,.25),0 30px 40px -14px rgba(0,0,0,.7),0 4px 8px rgba(0,0,0,.25)}
.acunit::before{content:"";position:absolute;left:0;right:0;top:56px;height:1px;background:rgba(0,0,0,.12)}
.aclcd{position:absolute;right:26px;top:22px;display:flex;align-items:flex-start;gap:2px;font-size:20px;font-weight:600;color:var(--ac);letter-spacing:1px;
  font-variant-numeric:tabular-nums;text-shadow:0 0 8px color-mix(in srgb,var(--ac) 80%,transparent)}
.aclcd i{display:none}
.acvent{position:absolute;left:22px;right:22px;bottom:9px;height:13px;border-radius:5px;background:#2a2d36;overflow:hidden;box-shadow:inset 0 2px 4px rgba(0,0,0,.5)}
.acvent::after{content:"";position:absolute;inset:0;background:linear-gradient(180deg,#f6f7fb,#dfe2ea);transform-origin:50% 0;transform:scaleY(.18);transition:transform .7s var(--ease)}
.acshadow{left:130px;top:-190px;width:280px;height:30px;transform:translateZ(-399px);border-radius:50%;background:rgba(0,0,0,.45);filter:blur(10px)}
.acglow{left:60px;top:-200px;width:420px;height:260px;transform:translateZ(-397px);border-radius:50%;background:radial-gradient(closest-side,color-mix(in srgb,var(--airc) 45%,transparent),transparent);filter:blur(14px);pointer-events:none}
.acair{left:120px;top:-186px;width:300px;height:190px;transform:translateZ(-384px);pointer-events:none;
  -webkit-mask-image:linear-gradient(#000 5%,transparent 92%);mask-image:linear-gradient(#000 5%,transparent 92%)}
.acair svg{width:100%;height:100%;overflow:visible}
.acair path{stroke:var(--airc); stroke-width:5;stroke-dasharray:22 30;filter:blur(1.6px);animation:air var(--air) linear infinite;animation-delay:var(--d)}
.acair path.thin{stroke-width:2;filter:blur(.4px);stroke:color-mix(in srgb,var(--airc) 55%,#fff)}
@keyframes air{to{stroke-dashoffset:-52}}

/* controls that float in the room: dark glass */
.hud{position:absolute;left:28px;top:22px;width:300px;padding:15px 18px 14px;border-radius:24px;z-index:4;color:var(--gtext);
  background:var(--glass);backdrop-filter:blur(20px) saturate(1.3);-webkit-backdrop-filter:blur(20px) saturate(1.3);
  box-shadow:0 0 0 1px var(--gline),0 30px 60px -20px rgba(0,0,0,.55);
  transform:perspective(900px) rotateY(9deg);transform-origin:0 50%;
  transition:background .9s,box-shadow .9s,color .9s,opacity .6s ease .45s,transform 1s var(--ease) .1s}
.off .hud{opacity:0;transform:perspective(900px) rotateY(9deg) translateX(-40px) scale(.96);pointer-events:none;transition:opacity .25s ease,transform .4s ease}
.hud-top{display:flex;align-items:center;gap:6px}
.title{display:flex;align-items:center;gap:6px;font-size:17px;font-weight:650;letter-spacing:-.2px;color:var(--gtext);padding:4px;border-radius:8px}
.title svg{width:14px;height:14px;stroke-width:2;color:var(--gsub);transition:transform .25s}
.title[aria-expanded=true] svg{transform:rotate(180deg)}
.menu{position:absolute;left:18px;top:54px;z-index:5;display:none;flex-direction:column;min-width:150px;padding:6px;border-radius:14px;
  background:color-mix(in srgb,var(--glass) 92%,var(--gtext) 8%);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);box-shadow:0 0 0 1px var(--gline),0 16px 30px rgba(0,0,0,.4)}
.menu.show{display:flex}
.menu button{text-align:left;padding:9px 12px;border-radius:9px;font-size:13px;color:var(--gtext)}
.menu button[aria-pressed=true]{background:color-mix(in srgb,var(--ac) 20%,transparent);color:var(--gtext);font-weight:600}
.hud .power{margin-left:auto;width:40px;height:40px;background:var(--gfill);color:var(--ac);box-shadow:none}
.hud .power.on{color:var(--ac);box-shadow:0 0 0 1px color-mix(in srgb,var(--ac) 45%,transparent),0 0 18px color-mix(in srgb,var(--ac) 35%,transparent)}.hud .power.on::before{opacity:0}
.hud .power::before{display:none}
.dialrow{display:flex;align-items:center;gap:4px;margin-top:6px}
.dial{position:relative;width:170px;height:144px;margin:0 auto;border-radius:24px;touch-action:none;flex:none}
.dial>svg{position:absolute;inset:0;width:100%;height:100%;overflow:visible}
.rail{stroke:var(--gline);stroke-width:11}
.arc{stroke:var(--ac);stroke-width:11;stroke-dasharray:0 100;filter:drop-shadow(0 0 6px color-mix(in srgb,var(--ac) 55%,transparent));transition:stroke-dasharray .5s var(--ease),stroke .9s}
.knob{transition:transform .5s var(--ease);transform-origin:100px 92px;cursor:grab}
.dial .knob circle{fill:color-mix(in srgb,var(--gtext) 55%,var(--ac));stroke:none;filter:drop-shadow(0 2px 6px rgba(0,0,0,.45))}
.knob .pip{display:none}
.drag .arc,.drag .knob{transition:none}.drag .knob{cursor:grabbing}
.read{position:absolute;left:0;right:0;top:44px;text-align:center;pointer-events:none}
.num{font-family:-apple-system,"SF Pro Display","Helvetica Neue",sans-serif;font-size:40px;font-weight:500;line-height:1;letter-spacing:-1.5px;
  font-variant-numeric:tabular-nums;display:inline-block;position:relative;padding-right:22px}
.num sup{position:absolute;right:0;top:4px;font-size:15px;font-weight:500;letter-spacing:0;color:var(--gsub)}
.state{display:flex;align-items:center;justify-content:center;gap:5px;color:var(--gsub);font-size:12px;margin-top:4px}
.state svg{width:14px;height:14px;color:var(--ac)}
.roomline{text-align:center;font-size:12px;color:var(--gsub);margin-top:2px;min-height:14px}
.nudge{width:42px;height:42px;border-radius:50%;background:var(--gfill);color:var(--gtext);display:grid;place-items:center;flex:none;transition:transform .12s,background .3s}
.nudge:hover{background:var(--gfill2)}.nudge svg{width:14px;height:14px;stroke-width:2}.nudge:active{transform:scale(.9)}
.seg{position:relative;display:grid;grid-auto-flow:column;grid-auto-columns:1fr;margin-top:6px}
.seg::before{display:none}
.seg button{display:flex;flex-direction:column;align-items:center;gap:6px;font-size:11.5px;color:var(--gsub);padding:2px 0;border-radius:12px;transition:color .25s}
.seg button span{width:40px;height:40px;border-radius:50%;display:grid;place-items:center;background:var(--gfill);color:var(--gtext);
  box-shadow:0 0 0 1.5px transparent;transition:background .3s,box-shadow .3s,color .3s,transform .2s}
.seg button:active span{transform:scale(.92)}
.seg svg{width:18px;height:18px}
.seg button[aria-pressed=true]{color:var(--gtext);font-weight:600}
.seg button[aria-pressed=true] span{background:var(--ac);color:#fff;box-shadow:0 0 0 1.5px var(--gline),0 0 0 5px color-mix(in srgb,var(--ac) 25%,transparent),0 6px 16px color-mix(in srgb,var(--ac) 45%,transparent)}
.rule{height:1px;background:var(--gline);margin:11px 0 9px}
.hgroup{margin-top:13px}
.hrow{display:flex;align-items:baseline;justify-content:space-between;margin:0 3px 6px}
.hrow span{font-size:11.5px;color:var(--gsub);letter-spacing:.1px}
.hrow b{font-size:12.5px;font-weight:650;color:var(--gtext);font-variant-numeric:tabular-nums}
.hsel{position:relative;display:grid;grid-auto-flow:column;grid-auto-columns:minmax(0,1fr);
  padding:3px;border-radius:13px;background:var(--gfill);box-shadow:inset 0 0 0 1px var(--gline)}
.hsel::before{content:"";position:absolute;top:3px;bottom:3px;left:3px;width:calc((100% - 6px)/var(--n,5));
  border-radius:10px;background:var(--ac);box-shadow:0 4px 12px -3px color-mix(in srgb,var(--ac) 60%,transparent);
  transform:translateX(calc(var(--i,0) * 100%));transition:transform .34s var(--ease),background .9s,box-shadow .9s}
.hsel button{position:relative;height:34px;display:grid;place-items:center;border-radius:10px;
  color:var(--gsub);transition:color .25s,transform .12s}
.hsel button svg{width:19px;height:19px;stroke-width:1.9}
.hsel button:hover:not([aria-pressed=true]){color:var(--gtext)}
.hsel button[aria-pressed=true]{color:#fff}
.hsel button:active{transform:scale(.9)}
@media (max-width:360px){.hsel button{height:30px}.hsel button svg{width:17px;height:17px}}
.pills{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:15px}
.pills>:last-child:nth-child(odd){grid-column:1/-1}
.tile{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:14px;background:var(--gfill);text-align:left;transition:background .3s,box-shadow .3s}
.tile:hover{background:var(--gfill2)}
.tile i{flex:none;width:28px;height:28px;border-radius:50%;background:var(--gfill);display:grid;place-items:center;color:var(--gtext);transition:background .3s,color .3s}
.tile i svg{width:16px;height:16px}
.tile b{display:block;font-size:12px;font-weight:600;color:var(--gtext);white-space:nowrap}
.tile small{display:block;font-size:11px;color:var(--gsub);margin-top:1px}
.tile[aria-pressed=true]{background:color-mix(in srgb,var(--ac) 18%,var(--gfill));box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--ac) 40%,transparent)}
.tile[aria-pressed=true] i{background:var(--ac);color:#fff}

.offnote{position:absolute;left:0;right:0;bottom:30px;text-align:center;color:var(--soft);font-size:12px;opacity:0;transition:opacity .4s;pointer-events:none}
.off .offnote{opacity:1;transition-delay:.3s}
.quit{position:absolute;left:50%;bottom:8px;transform:translateX(-50%);padding:3px 10px;color:var(--faint);font-size:11px;border-radius:6px;z-index:6}
.quit:hover{color:var(--soft)}

/* remote / keyboard navigation */
.kb button:focus,.kb [tabindex]:focus{outline:3px solid var(--ac);outline-offset:3px;box-shadow:0 0 0 7px color-mix(in srgb,var(--ac) 22%,transparent)}
.kb .seg button:focus{outline-offset:-2px}
button:focus{outline:none}
.hint{position:absolute;right:20px;bottom:10px;color:var(--faint);font-size:11px;z-index:6;opacity:0;transition:opacity .3s}
.kb .hint{opacity:1}

.veil{position:absolute;inset:0;z-index:9;display:none;flex-direction:column;align-items:center;justify-content:center;gap:10px;
  padding:30px;text-align:center;background:color-mix(in srgb,var(--pbg2) 94%,#000);color:var(--ink)}
.veil.show{display:flex}.veil b{font-size:15px;font-weight:600}
.veil p{color:var(--soft);-webkit-user-select:text;user-select:text}
.spin{width:22px;height:22px;border-radius:50%;border:2px solid var(--well);border-top-color:var(--ac);animation:sp .9s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}.spin{animation:none}.air path,.acair path{animation:none}.knob.spin .kface svg{animation:none}}
</style></head>
<body><div class="panel off" id="panel">

<div class="head">
  <div><div class="name" id="name">Air conditioner</div><div class="status" id="status"><i></i><span id="status-t">Connecting</span></div></div>
  <div class="roomsw" id="roomsw"><button id="sw-bedroom" aria-pressed="false">Bedroom</button><button id="sw-hall" aria-pressed="false">Hall</button></div>
  <button class="power" id="power2" aria-label="Power"><svg viewBox="0 0 24 24"><path d="M12 4v8M7.200 7.200a7 7 0 1 0 9.600 0"/></svg></button>
</div>

<div class="scene">
  <div class="offnote" id="offnote"></div>

  <div class="interior" id="interior">
    <div class="iw" id="iw"></div>
    <div class="shade"></div>
    <div class="roomdim"></div>
    <div class="hud" id="hud">
      <div class="hud-top">
        <button class="title" id="title" aria-haspopup="true" aria-expanded="false"><span id="hud-name">Bedroom AC</span><svg viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg></button>
        <button class="power" id="power" aria-label="Power"><svg viewBox="0 0 24 24"><path d="M12 4v8M7.200 7.200a7 7 0 1 0 9.600 0"/></svg></button>
      </div>
      <div class="menu" id="menu"><button id="m-bedroom" aria-pressed="false">Bedroom AC</button><button id="m-hall" aria-pressed="false">Hall AC</button></div>
      <div class="dialrow">
        <button class="nudge" id="minus" aria-label="Lower temperature"><svg viewBox="0 0 14 14"><path d="M2.500 7h9"/></svg></button>
        <div class="dial" id="dial" role="slider" tabindex="0" aria-label="Temperature" aria-valuemin="16" aria-valuemax="30">
          <svg viewBox="0 0 200 172">
            <path class="rail" d="M47.670 144.330A74 74 0 1 1 152.330 144.330"/>
            <path class="arc" id="arc" pathLength="100" d="M47.670 144.330A74 74 0 1 1 152.330 144.330"/>
            <g class="knob" id="knob"><circle cx="174" cy="92" r="10"/></g>
          </svg>
          <div class="read"><div class="num"><span id="temp">--</span><sup>°C</sup></div><div class="state"><svg viewBox="0 0 24 24"><path id="state-icon" d=""/></svg><span id="state-t"></span></div></div>
        </div>
        <button class="nudge" id="plus" aria-label="Raise temperature"><svg viewBox="0 0 14 14"><path d="M2.500 7h9M7 2.500v9"/></svg></button>
      </div>
      <div class="roomline" id="room-t"></div>
      <div class="seg" id="seg-mode" role="group" aria-label="Mode"></div>
      <div class="rule"></div>
      <div class="hgroup" id="g-fan">
        <div class="hrow"><span>Fan Speed</span><b id="v-fan">--</b></div>
        <div class="hsel" id="sel-fan" role="group" aria-label="Fan speed"></div>
      </div>
      <div class="hgroup" id="g-swing">
        <div class="hrow"><span>Swing</span><b id="v-swing">--</b></div>
        <div class="hsel" id="sel-swing" role="group" aria-label="Swing"></div>
      </div>
      <div class="pills" id="pills"></div>
    </div>
  </div>
</div>

<div class="hint" id="hint">Arrows to move · Enter to select</div>
<button class="quit" id="quit">Quit</button>
<div class="veil show" id="veil"><div class="spin" id="spin"></div><b id="veil-t">Connecting to your AC</b><p id="veil-p"></p></div>
</div>

<script>
const TOKEN="__TOKEN__", $=id=>document.getElementById(id);
const ROOMS=["bedroom","hall"], TITLE={bedroom:"Bedroom",hall:"Hall"};
const ICON={cool:"M12 3v18M4.200 7.500l15.600 9M19.800 7.500l-15.600 9M9.500 4.500 12 7l2.500-2.500M9.500 19.500 12 17l2.500 2.500",
  dry:"M12 3v2M12 19v2M3 12h2M19 12h2M5.600 5.600 7 7M17 17l1.400 1.400M18.400 5.600 17 7M7 17l-1.400 1.400M15.500 12a3.500 3.500 0 1 1-7 0 3.500 3.500 0 0 1 7 0z",
  fan:"M12 10.400C10 4 15 2.500 16 6c.700 2.500-2 4-4 4.400M13.600 12c6.400-2 7.900 3 4.400 4-2.500.700-4-2-4.400-4M12 13.600c2 6.400-3 7.900-4 4.400-.700-2.500 2-4 4-4.400M10.400 12c-6.400 2-7.900-3-4.400-4 2.500-.700 4 2 4.400 4",
  auto:"M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0zM9 16l3-8 3 8M10 13.500h4"};
const PILL_ICON={eco:"M5 19c0-9 5-14 15-14 0 10-5 15-13 14M5 19c3-5 6-8 10-10",
  boost:"M13 3 5 13.500h6L10 21l8-10.500h-6z",
  clean:"M10 4l1.600 4.400L16 10l-4.400 1.600L10 16l-1.600-4.400L4 10l4.400-1.600zM18 14l.800 2.200L21 17l-2.200.800L18 20l-.800-2.200L15 17l2.200-.800z",
  display:"M9 18h6M10 21h4M8.500 14.500a6 6 0 1 1 7 0c-.600.500-1 1.200-1 2v.500h-5v-.500c0-.800-.400-1.500-1-2z",
  sleep:"M20.500 14.500A8.500 8.500 0 1 1 9.500 3.500a7 7 0 0 0 11 11z",
  "":"M12 8v4l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z"};
const WORD={cool:"Cooling",dry:"Drying",fan:"Fan only",auto:"Auto",heat:"Heating"};
/* Every option list comes from the selected AC's capabilities, so a brand with
   no swing control simply never gets that knob. */
let SEGS={mode:[],fan:[],swing:[]}, capsKey="", TMIN=16, TMAX=30, TSPAN=14;
const airFor=(fan)=>{
  const list=SEGS.fan, i=list.findIndex(o=>o[0]===fan);
  if(i<0||list.length<2) return "1.2s";
  return (2.1-i*(1.35/(list.length-1))).toFixed(2)+"s";
};
let s=null, sel=null, hold=0, tempTimer=null, dragging=false, lastUi="", choiceTimer={};
const todOf=h=>h>=6&&h<17?"morning":h<20?"evening":"night";
const forcedTod=new URLSearchParams(location.search).get("tod");
let tod=forcedTod||todOf(new Date().getHours());
document.documentElement.dataset.tod=tod;
function checkTod(){
  if(forcedTod) return;
  const t=todOf(new Date().getHours());
  if(t===tod) return;
  tod=t; document.documentElement.dataset.tod=t;
  $("iw").dataset.room=""; if(s&&s.connected) render();
}
setInterval(checkTod,60000);
try{ sel=localStorage.getItem("ac-selected"); }catch(e){}

async function api(path,body){
  const r=await fetch(path,{method:body?"POST":"GET",headers:{"X-Token":TOKEN,"Content-Type":"application/json"},
    body:body?JSON.stringify(body):undefined});
  return r.json();
}
async function refresh(){
  if(Date.now()<hold||dragging) return;
  try{ const n=await api("/state"); if(Date.now()<hold||dragging) return; s=n; render(); }catch(e){}
}
window.refresh=refresh;
const cur=()=>s&&s.connected?(s.devices.find(d=>d.id===sel)||s.devices[0]):null;

/* ---- build the 3D home ---- */
const el=(cls,css)=>{const d=document.createElement("div"); d.className=cls; if(css) d.style.cssText=css; return d;};
function box(parent,x,y,z,w,d,h,c,cls){
  const g=el("box "+(cls||""),`left:${x}px;top:${y}px;width:${w}px;height:${d}px;transform:translateZ(${z}px)`);
  g.appendChild(el("f top",`inset:0;background:${c[0]};transform:translateZ(${h}px)`));
  g.appendChild(el("f front",`left:0;top:${d-h}px;width:${w}px;height:${h}px;background:${c[1]};transform-origin:50% 100%;transform:rotateX(-90deg)`));
  g.appendChild(el("f",`left:${-h}px;top:0;width:${h}px;height:${d}px;background:${c[2]};transform-origin:100% 50%;transform:rotateY(90deg)`));
  g.appendChild(el("f",`left:${w-h}px;top:0;width:${h}px;height:${d}px;background:${c[2]};transform-origin:100% 50%;transform:rotateY(90deg)`));
  parent.appendChild(g); return g;
}
function choose(room){
  if(!s||!s.connected) return;
  const d=s.devices.find(x=>x.room===room); if(!d||d.id===sel) return;
  sel=d.id; try{ localStorage.setItem("ac-selected",sel); }catch(e){}
  render();
}


/* ---- the room seen from inside (built once per room, swapped on selection) ---- */
const BASE={wood:"#8a6a4c",white:"#e8e3da",linen:"#ddd5c8",blue:"#6b7d99",sofa:"#cfc5b4",
  green:"#5aa06a",pot:"#9a7c62",dark:"#3a3d48",glow:"#ffd98a"};
const TOD_MODEL={morning:{f:1.16,mix:[255,255,255],amt:.20},evening:{f:1.02,mix:[255,150,70],amt:.30},night:{f:.58,mix:[42,52,80],amt:.18}};
function pal(name){
  const m=TOD_MODEL[tod]||TOD_MODEL.night, h=BASE[name]||name;
  const rgb=[0,1,2].map(k=>parseInt(h.slice(1+k*2,3+k*2),16));
  const sc=(c,f)=>c.map(v=>Math.max(0,Math.min(255,Math.round(v*f))));
  const b=sc(rgb,m.f).map((v,i)=>Math.round(v+(m.mix[i]-v)*m.amt));
  const css=c=>"rgb("+c.join(",")+")";
  return [css(sc(b,1.10)),css(b),css(sc(b,.84))];
}
function buildInterior(room){
  const iw=$("iw"); iw.innerHTML=""; iw.className="iw "+room+"-i";
  iw.appendChild(el("wall bwall")); iw.appendChild(el("lamp"));
  iw.appendChild(el("wall lwall")); iw.appendChild(el("wall rwall")); iw.appendChild(el("ceil"));
  const floor=el("floor3"); iw.appendChild(floor);
  if(room==="bedroom"){
    iw.appendChild(el("window")); iw.appendChild(el("frame"));
    // bed on the right, headboard against the back wall, under the AC
    box(floor,600,20,0,330,250,34,pal("wood")); box(floor,606,24,34,318,242,26,pal("linen")); box(floor,600,8,0,330,16,120,pal("wood"));
    box(floor,690,30,60,230,230,10,pal("blue")); box(floor,620,36,60,120,66,18,pal("white")); box(floor,620,110,60,120,66,18,pal("white"));
    box(floor,510,20,0,70,70,62,pal("wood")); box(floor,530,40,62,30,30,20,pal("glow"));
    const rug=el("rug","left:120px;top:120px;width:360px;height:220px;border-radius:8px;background:#eef0fb;box-shadow:inset 0 0 0 5px #dfe3f7"); rug.style.position="absolute"; rug.style.transform="translateZ(1px)"; floor.appendChild(rug);
  }else{
    const rug=el("rug","left:320px;top:120px;width:620px;height:250px;border-radius:8px;background:#e9e6fb;box-shadow:inset 0 0 0 6px #d6d0f5"); rug.style.position="absolute"; rug.style.transform="translateZ(1px)"; floor.appendChild(rug);
    box(floor,380,60,0,500,130,46,pal("sofa")); box(floor,380,50,46,500,40,60,pal("sofa")); box(floor,360,50,0,30,140,80,pal("sofa")); box(floor,870,50,0,30,140,80,pal("sofa"));
    box(floor,480,230,0,300,110,36,pal("wood")); box(floor,900,260,0,80,80,70,pal("pot")); box(floor,886,246,70,108,108,110,pal("green"));
    box(floor,120,200,0,120,120,60,pal("dark"));
    iw.appendChild(el("frame","left:-60px;top:-140px;width:230px;height:130px;background:linear-gradient(135deg,#2b2f4a,#4b5078)"));
  }
  // the air conditioner, mounted high on the back wall to the right
  const unit=el("acunit"); unit.innerHTML='<div class="aclcd"><span id="aclcd">--°C</span><i></i></div><div class="acvent"></div>';
  iw.appendChild(el("acshadow")); iw.appendChild(el("acglow")); iw.appendChild(unit);
  const air=el("acair");
  air.innerHTML='<svg viewBox="0 0 300 190">'+[30,75,120,165,210,255].map((x,i)=>'<path style="--d:-'+(i*.37).toFixed(2)+'s" d="M'+x+' 0Q'+x+' 95 '+(x+(x-150)*.7)+' 190"/>'+
    '<path class="thin" style="--d:-'+(i*.53+.2).toFixed(2)+'s" d="M'+(x+12)+' 0Q'+(x+12)+' 95 '+(x+12+(x-150)*.6)+' 185"/>').join("")+'</svg>';
  iw.appendChild(air);
  iw.dataset.room=room;
}

/* ---- controls ---- */
function setSeg(key,value){
  const box=$("seg-"+key); if(!box) return;
  const i=Math.max(0,SEGS[key].findIndex(o=>o[0]===value));
  [...box.children].forEach((b,n)=>b.setAttribute("aria-pressed",n===i));
}
/* Horizontal icon selectors for fan speed and swing.

   Options come from the AC's declared capabilities, so the control works for
   any number of them: --n sizes the sliding highlight, --i positions it. */
const SELECTORS={fan:{opts:[]},swing:{opts:[]}};

/* Fan speed reads as bars that grow with the airflow; "auto" keeps the circled
   A used by the mode row. */
function fanIcon(value,index,total){
  if(value==="auto") return '<path d="'+ICON.auto+'"/>';
  const known={quiet:1,low:2,medium:3,high:4};
  const bars=known[value]||Math.max(1,Math.min(4,Math.round((index/Math.max(1,total-1))*4)||1));
  let d="";
  for(let i=0;i<bars;i++){ const x=(6+i*4.4).toFixed(1), h=5+i*4.2; d+="M"+x+" 19V"+(19-h).toFixed(1); }
  return '<path d="'+d+'" stroke-linecap="round"/>';
}

/* Swing shows the louver angle: a fixed unit above, an arrow that rotates from
   almost horizontal to straight down. "auto" is the sweeping double arrow. */
function swingIcon(value,index,total){
  if(value===0||value==="auto"||value==="0")
    return '<path d="M4 4h16"/><path d="M12 8v11M8.500 11.500 12 8l3.500 3.500M8.500 15.500 12 19l3.500-3.500"/>';
  const levels=Math.max(1,total-1), step=60/Math.max(1,levels-1), angle=-60+(index-1)*step;
  return '<path d="M4 4h16"/><g transform="rotate('+angle.toFixed(1)+' 12 8)">'+
         '<path d="M12 8v10M8.500 14.500 12 18l3.500-3.500"/></g>';
}
const SEL_ICON={fan:fanIcon,swing:swingIcon};

function buildSelector(key,opts){
  const sel=SELECTORS[key], box=$("sel-"+key);
  sel.opts=opts; sel.n=opts.length; box.innerHTML="";
  box.style.setProperty("--n",Math.max(1,sel.n));
  opts.forEach(([value,label],i)=>{
    const b=document.createElement("button");
    b.type="button"; b.setAttribute("aria-pressed","false");
    b.setAttribute("aria-label",label); b.title=label;
    b.innerHTML='<svg viewBox="0 0 24 24" fill="none">'+SEL_ICON[key](value,i,sel.n)+"</svg>";
    b.onclick=()=>applyChoice(key,value);
    box.appendChild(b);
  });
  sel.buttons=[...box.children];
}
function setSelector(key,value){
  const sel=SELECTORS[key]; if(!sel.n) return;
  let i=sel.opts.findIndex(o=>o[0]===value);
  if(i<0) i=0;
  $("sel-"+key).style.setProperty("--i",i);
  sel.buttons.forEach((b,n)=>b.setAttribute("aria-pressed",n===i));
  $("v-"+key).textContent=sel.opts[i][1];
}

/* Rebuild every control from what the selected AC reports it can do. */
function applyCaps(caps){
  const key=JSON.stringify(caps); if(key===capsKey) return; capsKey=key;
  SEGS={mode:caps.modes||[],fan:caps.fan||[],swing:caps.swing||[]};
  TMIN=caps.temp.min; TMAX=caps.temp.max; TSPAN=Math.max(1,TMAX-TMIN);
  $("dial").setAttribute("aria-valuemin",TMIN); $("dial").setAttribute("aria-valuemax",TMAX);

  const modes=$("seg-mode"); modes.innerHTML="";
  SEGS.mode.forEach(([value,label])=>{
    const b=document.createElement("button"); b.setAttribute("aria-pressed","false");
    b.innerHTML='<span><svg viewBox="0 0 24 24"><path d="'+(ICON[value]||ICON.auto)+'"/></svg></span>'+label;
    b.onclick=()=>send("mode",value,{mode:value}); modes.appendChild(b);
  });
  modes.hidden=!SEGS.mode.length;

  buildSelector("fan",SEGS.fan); buildSelector("swing",SEGS.swing);
  $("g-fan").hidden=!SEGS.fan.length; $("g-swing").hidden=!SEGS.swing.length;
  document.querySelector(".rule").hidden=!SEGS.fan.length&&!SEGS.swing.length&&
    !((caps.presets||[]).length+(caps.toggles||[]).length);

  const pills=$("pills"); pills.innerHTML="";
  const add=(id,label,icon,onclick)=>{
    const b=document.createElement("button");
    b.className="tile"; b.id=id; b.setAttribute("aria-pressed","false");
    b.innerHTML='<i><svg viewBox="0 0 24 24"><path d="'+icon+'"/></svg></i><div><b>'+label+'</b><small></small></div>';
    b.onclick=onclick; pills.appendChild(b);
  };
  (caps.presets||[]).forEach(([value,label])=>
    add("p-"+value,label,PILL_ICON[value]||PILL_ICON[""],()=>{
      const d=cur(); if(!d) return; const on=d.preset!==value;
      send("preset",on?value:"none",on&&value==="eco"?{preset:value,temp:Math.min(TMAX,26)}:{preset:on?value:"none"});
    }));
  (caps.toggles||[]).forEach(([value,label])=>
    add("t-"+value,label,PILL_ICON[value]||PILL_ICON[""],()=>{
      const d=cur(); if(!d) return; const on=!d.toggles[value];
      send(value,on,{toggles:Object.assign({},d.toggles,{[value]:on})});
    }));
  const count=(caps.presets||[]).length+(caps.toggles||[]).length;
  pills.hidden=!count;
  pills.style.gridTemplateColumns=count===1?"1fr":"1fr 1fr";
}
function applyChoice(key,val){
  const d=cur(); if(!d||d[key]===val) return;
  d[key]=val; hold=Date.now()+4000; render();
  clearTimeout(choiceTimer[key]);
  const id=d.id;
  choiceTimer[key]=setTimeout(()=>{hold=Date.now()+4000; api("/cmd",{id,action:key,value:val}).catch(()=>{hold=0;});},300);
}
const labelOf=(key,value)=>{const o=(SEGS[key]||[]).find(x=>x[0]===value); return o?o[1]:"";};
const STOPS=[[47,140,255],[122,92,250],[240,106,140]];
function tint(p){
  const x=Math.min(.9999,Math.max(0,p))*2, i=Math.floor(x), f=x-i;
  return STOPS[i].map((v,k)=>Math.round(v+(STOPS[i+1][k]-v)*f)).join(",");
}
const whole=t=>t==null?null:Math.min(TMAX,Math.max(TMIN,Math.round(t)));
function fit(){
  const hud=$("hud");
  const h=Math.max(470,Math.min(700,Math.ceil(hud.offsetTop+hud.offsetHeight+24)));
  const key=h+"|"+sel;
  if(key!==lastUi){lastUi=key; api("/ui",{height:h,selected:sel}).catch(()=>{lastUi="";});}
}

function render(){
  const veil=$("veil");
  if(!s||!s.connected){
    veil.classList.add("show");
    const err=s&&s.error;
    $("spin").style.display=err?"none":"";
    $("veil-t").textContent=err?"Can't reach your ACs":"Connecting to your ACs";
    $("veil-p").textContent=err||"";
    fit(false);
    return;
  }
  veil.classList.remove("show");
  const d=cur(); if(sel!==d.id) sel=d.id;
  ROOMS.forEach(name=>{
    const dev=s.devices.find(x=>x.room===name);
    $("sw-"+name).hidden=!dev;
    $("sw-"+name).setAttribute("aria-pressed",!!dev&&dev.id===sel);
  });
  applyCaps(d.caps);
  if($("iw").dataset.room!==d.room) buildInterior(d.room);

  const t=whole(d.temp), pct=t==null?0:(t-TMIN)/TSPAN, c=tint(pct), root=document.documentElement.style;
  root.setProperty("--tc","rgb("+c+")"); root.setProperty("--tg","rgba("+c+",.14)");
  root.setProperty("--air",airFor(d.fan));
  const wasOff=$("panel").classList.contains("off");
  $("panel").classList.toggle("off",!d.power);
  $("power").classList.toggle("on",d.power); $("power").setAttribute("aria-pressed",d.power);
  $("name").textContent=TITLE[d.room];
  $("status").classList.toggle("on",d.online); $("status-t").textContent=d.online?"Online":"Offline";
  $("temp").textContent=t==null?"--":t;
  $("aclcd").innerHTML=(t==null?"--":t)+'<sup style="font-size:9px">°</sup>';
  const room=(!d.caps.room_temp||d.room_temp==null)?"":"Room "+Math.round(d.room_temp)+"°";
  $("room-t").textContent=room;
  $("offnote").textContent=TITLE[d.room]+" AC is off."+(room?" "+room.replace("Room","Room is")+".":"");
  setSeg("mode",d.mode);
  if(SEGS.fan.length) setSelector("fan",d.fan);
  if(SEGS.swing.length) setSelector("swing",d.swing);
  (d.caps.presets||[]).forEach(([value])=>{
    const b=$("p-"+value); if(!b) return;
    const on=d.preset===value;
    b.setAttribute("aria-pressed",on); b.querySelector("small").textContent=on?"On":"Off";
  });
  (d.caps.toggles||[]).forEach(([value])=>{
    const b=$("t-"+value); if(!b) return;
    const on=!!(d.toggles||{})[value];
    b.setAttribute("aria-pressed",on); b.querySelector("small").textContent=on?"On":"Off";
  });
  $("state-icon").setAttribute("d",ICON[d.mode]||ICON.auto);
  $("state-t").textContent=WORD[d.mode]||labelOf("mode",d.mode)||"On";
  $("hud-name").textContent=TITLE[d.room]+" AC";
  $("arc").style.strokeDasharray=(pct*100)+" 100";
  $("knob").style.transform="rotate("+(135+pct*270)+"deg)";
  $("dial").setAttribute("aria-valuenow",t==null?"":t);
  $("power2").classList.toggle("on",d.power);
  ROOMS.forEach(name=>{const dev=s.devices.find(x=>x.room===name); $("m-"+name).hidden=!dev; $("m-"+name).setAttribute("aria-pressed",!!dev&&dev.id===sel);});
  if(wasOff&&d.power&&document.body.classList.contains("kb")) setTimeout(()=>$("plus").focus(),700);
  fit(d.power);
}

function send(action,value,patch){
  const d=cur(); if(!d) return;
  Object.assign(d,patch); hold=Date.now()+4000; render();
  api("/cmd",{id:d.id,action,value}).catch(()=>{hold=0;});
}
function setTemp(t,now){
  const d=cur(); if(!d) return;
  t=Math.min(TMAX,Math.max(TMIN,Math.round(t)));
  d.temp=t; hold=Date.now()+4000; render();
  clearTimeout(tempTimer);
  const id=d.id;
  tempTimer=setTimeout(()=>{hold=Date.now()+4000; api("/cmd",{id,action:"temp",value:t}).catch(()=>{hold=0;});},now?0:450);
}
$("power").onclick=()=>{const d=cur(); if(d) send("power",!d.power,{power:!d.power});};
$("minus").onclick=()=>setTemp((cur().temp||24)-1);
$("plus").onclick=()=>setTemp((cur().temp||24)+1);
$("power2").onclick=$("power").onclick;
$("title").onclick=()=>{const open=$("menu").classList.toggle("show"); $("title").setAttribute("aria-expanded",open);};
$("m-bedroom").onclick=()=>{choose("bedroom"); $("menu").classList.remove("show"); $("title").setAttribute("aria-expanded",false);};
$("m-hall").onclick=()=>{choose("hall"); $("menu").classList.remove("show"); $("title").setAttribute("aria-expanded",false);};

const dial=$("dial");
function polar(e){
  const r=dial.getBoundingClientRect(), k=r.width/200, dx=e.clientX-(r.left+100*k), dy=e.clientY-(r.top+92*k);
  let rel=(Math.atan2(dy,dx)*180/Math.PI-135+720)%360;
  if(rel>270) rel=rel>315?0:270;
  return {dist:Math.hypot(dx,dy)/k, temp:TMIN+TSPAN*rel/270};
}
dial.onpointerdown=e=>{
  const p=polar(e); if(p.dist<50||p.dist>98) return;
  dragging=true; dial.classList.add("drag"); dial.setPointerCapture(e.pointerId); setTemp(p.temp);
};
dial.onpointermove=e=>{if(dragging) setTemp(polar(e).temp);};
dial.onpointerup=dial.onpointercancel=()=>{if(!dragging) return; dragging=false; dial.classList.remove("drag"); setTemp(cur().temp,true);};
$("sw-bedroom").onclick=()=>choose("bedroom"); $("sw-hall").onclick=()=>choose("hall");
$("quit").onclick=()=>api("/quit",{});

/* ---- remote / keyboard navigation: arrows move between controls, Enter selects ---- */
function focusables(){
  return [...document.querySelectorAll("button,#dial")].filter(b=>{
    if(b.hidden||b.closest("[hidden]")) return false;
    const r=b.getBoundingClientRect(); if(r.width<4||r.height<4) return false;
    const off=$("panel").classList.contains("off");
    if(off&&b.closest(".hud")) return false;
    if(!off&&b.classList.contains("pin")) return false;
    if(b.closest(".menu")&&!$("menu").classList.contains("show")) return false;
    if(b.closest(".head")&&!off) return false;
    const cs=getComputedStyle(b); if(cs.visibility==="hidden"||cs.pointerEvents==="none") return false;
    return true;
  });
}
function move(dir){
  const list=focusables(), from=document.activeElement&&list.includes(document.activeElement)?document.activeElement:null;
  if(!from){ (list[0]||{}).focus&&list[0].focus(); return; }
  const a=from.getBoundingClientRect(), ax=a.left+a.width/2, ay=a.top+a.height/2;
  let best=null, score=1e9;
  list.forEach(b=>{
    if(b===from) return;
    const r=b.getBoundingClientRect(), dx=r.left+r.width/2-ax, dy=r.top+r.height/2-ay;
    const fwd=dir==="ArrowRight"?dx:dir==="ArrowLeft"?-dx:dir==="ArrowDown"?dy:-dy;
    const side=Math.abs(dir==="ArrowRight"||dir==="ArrowLeft"?dy:dx);
    if(fwd<4) return;
    const sc=fwd+side*2.2; if(sc<score){score=sc; best=b;}
  });
  if(best) best.focus();
}
document.addEventListener("keydown",e=>{
  if(["ArrowLeft","ArrowRight","ArrowUp","ArrowDown"].includes(e.key)){
    document.body.classList.add("kb");
    if(document.activeElement===$("dial")&&(e.key==="ArrowUp"||e.key==="ArrowDown")){ setTemp((cur().temp||24)+(e.key==="ArrowUp"?1:-1)); e.preventDefault(); return; }
    move(e.key); e.preventDefault();
  }else if(e.key==="Enter"||e.key===" "){
    document.body.classList.add("kb");
    const el=document.activeElement;
    if(el&&el.tagName==="BUTTON"){ el.click(); e.preventDefault(); }
  }else if(e.key==="Backspace"||e.key==="Escape"){
    const d=cur(); if(d&&d.power&&!$("panel").classList.contains("off")){ send("power",false,{power:false}); e.preventDefault(); }
  }
});
document.addEventListener("pointerdown",()=>document.body.classList.remove("kb"));

refresh(); setInterval(refresh,1500);
</script></body></html>
"""


if __name__ == "__main__":
    main()
