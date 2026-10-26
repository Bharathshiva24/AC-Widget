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

WIDTH, HEIGHT = 320, 520
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
        STATE["height"] = max(300, min(760, int(body.get("height", HEIGHT))))
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
    from AppKit import (NSApplication, NSPopover, NSStatusBar,
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
            # No forced appearance: the panel follows the system light or dark
            # setting, like every other menu bar popover.

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


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AC</title>
<style>
/* ---------------------------------------------------------------- tokens
   The panel sits inside an NSPopover, so the window material shows through
   and every surface here is a translucent wash over it, the way a native
   menu bar panel is built. Colours follow the system appearance. */
:root{
  --accent:#007AFF; --accent-press:#0062CC;
  --label:rgba(0,0,0,.85); --label2:rgba(0,0,0,.5); --label3:rgba(0,0,0,.26);
  --on-accent:#fff; --raised:#fff; --knob:#fff;
  --control:rgba(0,0,0,.05); --control-h:rgba(0,0,0,.09); --control-p:rgba(0,0,0,.14);
  --well:rgba(0,0,0,.06); --track:rgba(0,0,0,.10); --sep:rgba(0,0,0,.10);
  --surface:#F2F2F4;
  --glow:rgba(0,122,255,.42);
  --ease:cubic-bezier(.32,.72,0,1);
}
@media (prefers-color-scheme:dark){
  :root{
    --accent:#0A84FF; --accent-press:#0969CC;
    --label:rgba(255,255,255,.85); --label2:rgba(255,255,255,.55); --label3:rgba(255,255,255,.26);
    --raised:#3A3A3C; --knob:#F2F2F4;
    --control:rgba(255,255,255,.09); --control-h:rgba(255,255,255,.15); --control-p:rgba(255,255,255,.22);
    --well:rgba(255,255,255,.07); --track:rgba(255,255,255,.13); --sep:rgba(255,255,255,.13);
    --surface:#2C2C2E;
    --glow:rgba(10,132,255,.5);
  }
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{
  background:var(--surface);
  font:13px/1.25 -apple-system,"SF Pro Text","Helvetica Neue",system-ui,sans-serif;
  color:var(--label);
  -webkit-font-smoothing:antialiased;
  user-select:none; -webkit-user-select:none;
  overflow:hidden;
}
button{font:inherit;color:inherit;background:none;border:0;cursor:default;-webkit-appearance:none}
button:focus{outline:none}
body.kb button:focus-visible,body.kb #dial:focus-visible{outline:2.5px solid var(--accent);outline-offset:2px;border-radius:8px}
svg{fill:none;stroke:currentColor;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
[hidden]{display:none!important}

.panel{padding:14px 16px 12px;display:flex;flex-direction:column;gap:13px;position:relative}

/* ---------------------------------------------------------------- header */
.header{display:flex;align-items:flex-start;justify-content:space-between;gap:8px}
.title{display:flex;align-items:center;gap:3px;font-size:15px;font-weight:590;letter-spacing:-.1px;
  margin:-3px -5px 0;padding:3px 5px;border-radius:6px;transition:background .15s}
.title:hover{background:var(--control)}
.title:active{background:var(--control-p)}
.title svg{width:12px;height:12px;stroke-width:2.1;color:var(--label3);transition:transform .22s var(--ease)}
.title[aria-expanded=true] svg{transform:rotate(180deg)}
.title.solo{pointer-events:none}
.title.solo svg{display:none}
.sub{font-size:11px;color:var(--label2);margin-top:2px;letter-spacing:.05px}

.power{width:28px;height:28px;flex:none;border-radius:50%;background:var(--control);color:var(--label2);
  display:grid;place-items:center;transition:background .22s,color .22s,box-shadow .35s,transform .1s}
.power svg{width:15px;height:15px;stroke-width:2}
.power:hover{background:var(--control-h);color:var(--label)}
.power:active{transform:scale(.93)}
.power[aria-pressed=true]{background:var(--accent);color:var(--on-accent);box-shadow:0 3px 11px -2px var(--glow)}
.power[aria-pressed=true]:hover{box-shadow:0 4px 14px -2px var(--glow)}
.power[aria-pressed=true]:active{background:var(--accent-press)}

/* device switcher */
.menu{position:absolute;top:40px;left:14px;z-index:9;min-width:168px;padding:4px;border-radius:10px;
  background:var(--raised);box-shadow:0 8px 26px -6px rgba(0,0,0,.3),0 0 0 .5px rgba(0,0,0,.14);
  opacity:0;transform:translateY(-5px) scale(.97);transform-origin:top left;pointer-events:none;
  transition:opacity .16s,transform .16s var(--ease)}
.menu.show{opacity:1;transform:none;pointer-events:auto}
.menu button{display:flex;align-items:center;gap:7px;width:100%;padding:5px 8px;border-radius:6px;font-size:12.5px;text-align:left}
.menu button:hover{background:var(--accent);color:var(--on-accent)}
.menu button::before{content:"";width:12px;height:12px;flex:none;opacity:0;
  background:currentColor;-webkit-mask:var(--tick) center/12px no-repeat;mask:var(--tick) center/12px no-repeat}
.menu button[aria-pressed=true]::before{opacity:1}

/* ---------------------------------------------------------------- dial */
.dialrow{display:flex;align-items:center;justify-content:center;gap:6px}
.step{width:30px;height:30px;flex:none;border-radius:50%;background:var(--control);color:var(--label);
  display:grid;place-items:center;transition:background .18s,transform .1s}
.step svg{width:14px;height:14px;stroke-width:2}
.step:hover{background:var(--control-h)}
.step:active{background:var(--control-p);transform:scale(.92)}

.dial{position:relative;width:168px;flex:none;aspect-ratio:200/172;touch-action:none}
.dial svg.gauge{width:100%;height:100%;overflow:visible}
.rail{stroke:var(--track);stroke-width:10}
.arc{stroke:var(--accent);stroke-width:10;stroke-dasharray:0 100;
  filter:drop-shadow(0 2px 7px var(--glow));
  transition:stroke-dasharray .45s var(--ease),opacity .4s}
.knob{transform-origin:100px 92px;transition:transform .45s var(--ease),opacity .4s}
.knob circle{fill:var(--knob);stroke:none;filter:drop-shadow(0 1px 3px rgba(0,0,0,.28))}
.drag .arc,.drag .knob{transition:none}
.hit{stroke:transparent;stroke-width:34;cursor:grab}
.drag .hit{cursor:grabbing}

.read{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:1px;pointer-events:none;padding-bottom:6px}
.num{display:flex;align-items:flex-start;font-weight:270;font-size:42px;letter-spacing:-1.4px;line-height:1}
.num sup{font-size:15px;font-weight:500;color:var(--label2);margin-top:5px;margin-left:1px;letter-spacing:0}
.state{display:flex;align-items:center;gap:4px;font-size:11px;color:var(--label2);margin-top:3px}
.state svg{width:13px;height:13px;color:var(--accent);transition:color .3s}

/* ---------------------------------------------------------------- modes */
.modes{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(0,1fr)}
.modes button{display:flex;flex-direction:column;align-items:center;gap:6px;padding:4px 0}
.chip{width:34px;height:34px;border-radius:50%;background:var(--control);color:var(--label2);
  display:grid;place-items:center;transition:background .25s,color .25s,box-shadow .35s,transform .1s}
.chip svg{width:17px;height:17px}
.modes button:hover .chip{background:var(--control-h);color:var(--label)}
.modes button:active .chip{transform:scale(.93)}
.modes em{font-style:normal;font-size:10px;color:var(--label2);letter-spacing:.06px;transition:color .25s}
.modes button[aria-pressed=true] .chip{background:var(--accent);color:var(--on-accent);box-shadow:0 4px 12px -3px var(--glow)}
.modes button[aria-pressed=true] em{color:var(--label);font-weight:590}

.rule{height:1px;background:var(--sep);margin:1px 0}

/* -------------------------------------------------- fan speed and swing */
.hgroup{display:flex;flex-direction:column;gap:6px}
.hrow{display:flex;align-items:baseline;justify-content:space-between;margin:0 2px}
.hrow span{font-size:11px;color:var(--label2);letter-spacing:.05px}
.hrow b{font-size:11px;font-weight:590;color:var(--label);font-variant-numeric:tabular-nums}
.hsel{position:relative;display:grid;grid-auto-flow:column;grid-auto-columns:minmax(0,1fr);
  padding:3px;border-radius:10px;background:var(--well)}
.hsel::before{content:"";position:absolute;top:3px;bottom:3px;left:3px;width:calc((100% - 6px)/var(--n,5));
  border-radius:8px;background:var(--accent);box-shadow:0 3px 10px -3px var(--glow);
  transform:translateX(calc(var(--i,0) * 100%));
  transition:transform .34s var(--ease),background .25s,opacity .3s}
.hsel button{position:relative;height:30px;display:grid;place-items:center;border-radius:8px;
  color:var(--label2);transition:color .22s}
.hsel button svg{width:17px;height:17px}
.hsel button:hover{color:var(--label)}
.hsel button[aria-pressed=true]{color:var(--on-accent)}

/* ---------------------------------------------------------------- presets */
.pills{display:grid;gap:10px}
.tile{display:flex;align-items:center;gap:8px;height:46px;padding:0 10px;border-radius:10px;
  background:var(--control);transition:background .18s}
.tile:hover{background:var(--control-h)}
.tile:active{background:var(--control-p)}
.tile i{flex:none;color:var(--label2);display:grid;place-items:center;transition:color .22s}
.tile i svg{width:16px;height:16px}
.tile[aria-pressed=true] i{color:var(--accent)}
.tile div{flex:1;min-width:0;text-align:left}
.tile b{display:block;font-size:10.5px;font-weight:590;letter-spacing:.05px}
.tile small{display:block;font-size:10px;color:var(--label2);margin-top:1px}
.sw{position:relative;flex:none;width:28px;height:17px;border-radius:9px;background:var(--track);transition:background .22s}
.sw i{position:absolute;top:2px;left:2px;width:13px;height:13px;border-radius:50%;background:var(--knob);
  box-shadow:0 1px 2.5px rgba(0,0,0,.2);transition:transform .24s var(--ease)}
.tile[aria-pressed=true] .sw{background:var(--accent)}
.tile[aria-pressed=true] .sw i{transform:translateX(11px)}

.quit{align-self:center;font-size:11px;color:var(--label3);padding:3px 10px;border-radius:6px;transition:color .15s,background .15s}
.quit:hover{color:var(--label2);background:var(--control)}

/* ------------------------------------------------------------ off state */
.off .modes,.off .hgroup,.off .pills,.off .step{opacity:.42;pointer-events:none}
.off .arc,.off .knob{opacity:0}
.off .hsel::before{opacity:0}
.off .num{color:var(--label3)}
.off .state{color:var(--label3)}
.off .state svg{color:var(--label3)}
.off .dial{pointer-events:none}

/* ---------------------------------------------------------------- veil */
.veil{position:absolute;inset:0;z-index:20;display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:10px;padding:28px;text-align:center;opacity:0;pointer-events:none;transition:opacity .3s;
  background:var(--surface)}
.veil.show{opacity:1;pointer-events:auto}
.veil b{font-size:13px;font-weight:590}
.veil p{font-size:11px;color:var(--label2);line-height:1.45;max-width:250px}
.spin{width:20px;height:20px;border-radius:50%;border:2px solid var(--track);border-top-color:var(--accent);animation:sp .9s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}.spin{animation:none}}
</style></head>

<body><div class="panel off" id="panel">

<div class="header">
  <div>
    <button class="title" id="title" aria-haspopup="true" aria-expanded="false">
      <span id="hud-name">Air conditioner</span>
      <svg viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>
    </button>
    <div class="sub" id="sub">Connecting</div>
  </div>
  <button class="power" id="power" aria-label="Power" aria-pressed="false">
    <svg viewBox="0 0 24 24"><path d="M12 4v8M7.200 7.200a7 7 0 1 0 9.600 0"/></svg>
  </button>
</div>
<div class="menu" id="menu" role="menu"></div>

<div class="dialrow">
  <button class="step" id="minus" aria-label="Lower temperature"><svg viewBox="0 0 14 14"><path d="M2.500 7h9"/></svg></button>
  <div class="dial" id="dial" role="slider" tabindex="0" aria-label="Temperature" aria-valuemin="16" aria-valuemax="30">
    <svg class="gauge" viewBox="0 0 200 172">
      <path class="rail" d="M47.670 144.330A74 74 0 1 1 152.330 144.330"/>
      <path class="arc" id="arc" pathLength="100" d="M47.670 144.330A74 74 0 1 1 152.330 144.330"/>
      <g class="knob" id="knob"><circle cx="174" cy="92" r="8"/></g>
      <path class="hit" d="M47.670 144.330A74 74 0 1 1 152.330 144.330"/>
    </svg>
    <div class="read">
      <div class="num"><span id="temp">--</span><sup>°C</sup></div>
      <div class="state"><svg viewBox="0 0 24 24"><path id="state-icon" d=""/></svg><span id="state-t"></span></div>
    </div>
  </div>
  <button class="step" id="plus" aria-label="Raise temperature"><svg viewBox="0 0 14 14"><path d="M2.500 7h9M7 2.500v9"/></svg></button>
</div>

<div class="modes" id="seg-mode" role="group" aria-label="Mode"></div>
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
<button class="quit" id="quit">Quit</button>

<div class="veil show" id="veil"><div class="spin" id="spin"></div><b id="veil-t">Connecting to your AC</b><p id="veil-p"></p></div>
</div>

<script>
const TOKEN="__TOKEN__", $=id=>document.getElementById(id);
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
document.documentElement.style.setProperty("--tick",
  "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'%3E%3Cpath d='M2 6.3 4.6 9 10 3.2' fill='none' stroke='%23000' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E\")");

/* Every option list comes from the selected AC's capabilities, so a brand with
   no swing control simply never gets that selector. */
let SEGS={mode:[],fan:[],swing:[]}, capsKey="", TMIN=16, TMAX=30, TSPAN=14;
let s=null, sel=null, hold=0, tempTimer=null, dragging=false, lastUi="", choiceTimer={}, menuKey="";
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
const titleOf=d=>d.name||(d.room?d.room[0].toUpperCase()+d.room.slice(1)+" AC":"Air conditioner");

function choose(id){
  if(!s||!s.connected||id===sel) return;
  sel=id; try{ localStorage.setItem("ac-selected",sel); }catch(e){}
  render();
}
function closeMenu(){ $("menu").classList.remove("show"); $("title").setAttribute("aria-expanded","false"); }

/* The device list is whatever the driver reports, so one AC or five all work. */
function buildMenu(devices){
  const key=devices.map(d=>d.id).join("|"); if(key===menuKey) return; menuKey=key;
  const menu=$("menu"); menu.innerHTML="";
  devices.forEach(d=>{
    const b=document.createElement("button");
    b.setAttribute("aria-pressed","false"); b.dataset.id=d.id; b.textContent=titleOf(d);
    b.onclick=()=>{ choose(d.id); closeMenu(); };
    menu.appendChild(b);
  });
  $("title").classList.toggle("solo",devices.length<2);
}

/* ---- controls ---- */
function setSeg(key,value){
  const box=$("seg-"+key); if(!box) return;
  const i=SEGS[key].findIndex(o=>o[0]===value);
  [...box.children].forEach((b,n)=>b.setAttribute("aria-pressed",n===i));
}
/* Horizontal icon selectors for fan speed and swing. Options come from the
   AC's capabilities: --n sizes the sliding highlight, --i positions it. */
const SELECTORS={fan:{opts:[]},swing:{opts:[]}};

/* Fan speed reads as a four-bar meter: bars up to this speed are solid and the
   rest stay faint, so every option shares a silhouette and only the fill moves. */
function fanIcon(value,index){
  if(value==="auto") return '<path d="'+ICON.auto+'"/>';
  const known={quiet:1,low:2,medium:3,high:4};
  const bars=known[value]||Math.max(1,Math.min(4,index||1));
  let lit="",dim="";
  for(let i=0;i<4;i++){
    const segment="M"+(6.4+i*3.7).toFixed(1)+" 18.6V"+(14.6-i*3.6).toFixed(1);
    if(i<bars) lit+=segment; else dim+=segment;
  }
  return '<path d="'+lit+'"/>'+(dim?'<path d="'+dim+'" opacity=".3"/>':"");
}
/* Swing shows the louver itself: the unit's lip stays put while the vane below
   tilts from flat to steeply down. "auto" is the vane sweeping both ways. */
function swingIcon(value,index,total){
  const lip='<path d="M5.5 5.5h13"/>';
  if(value===0||value==="auto"||value==="0")
    return lip+'<path d="M6 13.5h12"/><path d="M9.4 9.6 12 7l2.6 2.6M9.4 17.4 12 20l2.6-2.6"/>';
  const levels=Math.max(2,total-1), angle=10+(index-1)*(62/(levels-1));
  return lip+'<g transform="rotate('+angle.toFixed(1)+' 12 13.5)"><path d="M6 13.5h12"/></g>';
}
const SEL_ICON={fan:fanIcon,swing:swingIcon};

function buildSelector(key,opts){
  const box=$("sel-"+key), st=SELECTORS[key];
  st.opts=opts; st.n=opts.length; box.innerHTML="";
  box.style.setProperty("--n",Math.max(1,st.n));
  opts.forEach(([value,label],i)=>{
    const b=document.createElement("button");
    b.type="button"; b.setAttribute("aria-pressed","false");
    b.setAttribute("aria-label",label); b.title=label;
    b.innerHTML='<svg viewBox="0 0 24 24">'+SEL_ICON[key](value,i,st.n)+"</svg>";
    b.onclick=()=>applyChoice(key,value);
    box.appendChild(b);
  });
  st.buttons=[...box.children];
}
function setSelector(key,value){
  const st=SELECTORS[key]; if(!st.n) return;
  let i=st.opts.findIndex(o=>o[0]===value);
  if(i<0) i=0;
  $("sel-"+key).style.setProperty("--i",i);
  st.buttons.forEach((b,n)=>b.setAttribute("aria-pressed",n===i));
  $("v-"+key).textContent=st.opts[i][1];
}

/* Rebuild every control from what the selected AC reports it can do. */
function applyCaps(caps){
  const key=JSON.stringify(caps); if(key===capsKey) return; capsKey=key;
  SEGS={mode:caps.modes||[],fan:caps.fan||[],swing:caps.swing||[]};
  TMIN=caps.temp.min; TMAX=caps.temp.max; TSPAN=Math.max(1,TMAX-TMIN);
  $("dial").setAttribute("aria-valuemin",TMIN); $("dial").setAttribute("aria-valuemax",TMAX);

  const modes=$("seg-mode"); modes.innerHTML="";
  SEGS.mode.forEach(([value,label])=>{
    const b=document.createElement("button"); b.setAttribute("aria-pressed","false"); b.title=label;
    b.innerHTML='<span class="chip"><svg viewBox="0 0 24 24"><path d="'+(ICON[value]||ICON.auto)+'"/></svg></span><em>'+label+"</em>";
    b.onclick=()=>send("mode",value,{mode:value});
    modes.appendChild(b);
  });
  modes.hidden=!SEGS.mode.length;

  buildSelector("fan",SEGS.fan); buildSelector("swing",SEGS.swing);
  $("g-fan").hidden=!SEGS.fan.length; $("g-swing").hidden=!SEGS.swing.length;

  const pills=$("pills"); pills.innerHTML="";
  const add=(id,label,icon,onclick)=>{
    const b=document.createElement("button");
    b.className="tile"; b.id=id; b.setAttribute("aria-pressed","false"); b.title=label;
    b.innerHTML='<i><svg viewBox="0 0 24 24"><path d="'+icon+'"/></svg></i>'+
      '<div><b>'+label+'</b><small></small></div><span class="sw"><i></i></span>';
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
  document.querySelector(".rule").hidden=!SEGS.fan.length&&!SEGS.swing.length&&!count;
}
function applyChoice(key,val){
  const d=cur(); if(!d||d[key]===val) return;
  d[key]=val; hold=Date.now()+4000; render();
  clearTimeout(choiceTimer[key]);
  const id=d.id;
  choiceTimer[key]=setTimeout(()=>{hold=Date.now()+4000; api("/cmd",{id,action:key,value:val}).catch(()=>{hold=0;});},300);
}
const labelOf=(key,value)=>{const o=(SEGS[key]||[]).find(x=>x[0]===value); return o?o[1]:"";};
const whole=t=>t==null?null:Math.min(TMAX,Math.max(TMIN,Math.round(t)));

/* The popover resizes to whatever the panel currently needs. */
function fit(){
  const h=Math.max(300,Math.min(760,Math.ceil($("panel").offsetHeight)));
  const key=h+"|"+sel;
  if(key!==lastUi){lastUi=key; api("/ui",{height:h,selected:sel}).catch(()=>{lastUi="";});}
}

function render(){
  const veil=$("veil");
  if(!s||!s.connected){
    veil.classList.add("show");
    const err=s&&s.error;
    $("spin").style.display=err?"none":"";
    $("veil-t").textContent=err?"Can't reach your AC":"Connecting to your AC";
    $("veil-p").textContent=err||"";
    fit();
    return;
  }
  veil.classList.remove("show");
  const d=cur(); if(sel!==d.id) sel=d.id;
  buildMenu(s.devices);
  [...$("menu").children].forEach(b=>b.setAttribute("aria-pressed",b.dataset.id===sel));
  applyCaps(d.caps);

  const t=whole(d.temp), pct=t==null?0:(t-TMIN)/TSPAN;
  $("panel").classList.toggle("off",!d.power);
  $("power").setAttribute("aria-pressed",d.power);
  $("hud-name").textContent=titleOf(d);
  const room=(!d.caps.room_temp||d.room_temp==null)?"":"Room "+Math.round(d.room_temp)+"°";
  const bits=d.power?[room,d.online?"Online":"Offline"]:["Off",room];
  $("sub").textContent=bits.filter(Boolean).join("  ·  ");
  $("temp").textContent=t==null?"--":t;
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
  $("state-t").textContent=d.power?(WORD[d.mode]||labelOf("mode",d.mode)||"On"):"Off";
  $("arc").style.strokeDasharray=(pct*100)+" 100";
  $("knob").style.transform="rotate("+(135+pct*270)+"deg)";
  $("dial").setAttribute("aria-valuenow",t==null?"":t);
  fit();
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
$("minus").onclick=()=>setTemp(((cur()||{}).temp||24)-1);
$("plus").onclick=()=>setTemp(((cur()||{}).temp||24)+1);
$("title").onclick=e=>{
  e.stopPropagation();
  const open=$("menu").classList.toggle("show");
  $("title").setAttribute("aria-expanded",open);
};
$("quit").onclick=()=>api("/quit",{});
document.addEventListener("pointerdown",e=>{ if(!e.target.closest(".menu,.title")) closeMenu(); });

const dial=$("dial");
function polar(e){
  const r=dial.getBoundingClientRect(), k=r.width/200, dx=e.clientX-(r.left+100*k), dy=e.clientY-(r.top+92*k);
  let rel=(Math.atan2(dy,dx)*180/Math.PI-135+720)%360;
  if(rel>270) rel=rel>315?0:270;
  return {dist:Math.hypot(dx,dy)/k, temp:TMIN+TSPAN*rel/270};
}
dial.onpointerdown=e=>{
  const d=cur(); if(!d||!d.power) return;
  const p=polar(e); if(p.dist<52||p.dist>96) return;
  dragging=true; dial.classList.add("drag"); dial.setPointerCapture(e.pointerId); setTemp(p.temp);
};
dial.onpointermove=e=>{if(dragging) setTemp(polar(e).temp);};
dial.onpointerup=dial.onpointercancel=()=>{
  if(!dragging) return;
  dragging=false; dial.classList.remove("drag"); setTemp(cur().temp,true);
};

/* ---- remote / keyboard navigation: arrows move between controls, Enter selects ---- */
function focusables(){
  return [...document.querySelectorAll("button,#dial")].filter(b=>{
    if(b.hidden||b.closest("[hidden]")) return false;
    const r=b.getBoundingClientRect(); if(r.width<4||r.height<4) return false;
    if(b.closest(".menu")&&!$("menu").classList.contains("show")) return false;
    if(b===dial&&$("panel").classList.contains("off")) return false;
    const cs=getComputedStyle(b);
    if(cs.visibility==="hidden"||cs.pointerEvents==="none") return false;
    if(b.closest(".panel")&&parseFloat(getComputedStyle(b.parentElement).opacity)<.6&&b.closest(".off")) return false;
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
    if(document.activeElement===dial&&(e.key==="ArrowUp"||e.key==="ArrowDown")){
      setTemp(((cur()||{}).temp||24)+(e.key==="ArrowUp"?1:-1)); e.preventDefault(); return;
    }
    move(e.key); e.preventDefault();
  }else if(e.key==="Enter"||e.key===" "){
    document.body.classList.add("kb");
    const el=document.activeElement;
    if(el&&el.tagName==="BUTTON"){ el.click(); e.preventDefault(); }
  }else if(e.key==="Escape"){
    if($("menu").classList.contains("show")){ closeMenu(); e.preventDefault(); return; }
    const d=cur(); if(d&&d.power){ send("power",false,{power:false}); e.preventDefault(); }
  }
});
document.addEventListener("pointerdown",()=>document.body.classList.remove("kb"));

refresh(); setInterval(refresh,1500);
</script></body></html>
"""


if __name__ == "__main__":
    main()
