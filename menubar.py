import asyncio, os, threading, rumps
from miraie_ac import MirAIeHub, MirAIeBroker

loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()
ac = None

async def connect():
    global ac
    broker, hub = MirAIeBroker(), MirAIeHub()
    await hub.init(os.environ["MIRAIE_MOBILE"], os.environ["MIRAIE_PASSWORD"], broker)
    while getattr(broker, "client", None) is None:
        await asyncio.sleep(1)
    ac = hub.home.devices[0]

def send(make_command):
    if ac:
        asyncio.run_coroutine_threadsafe(make_command(), loop)

class ACApp(rumps.App):
    def __init__(self):
        super().__init__("AC")
        self.menu = ["Turn On", "Turn Off", None] + [f"{t}°C" for t in range(16, 31)]
        for t in range(16, 31):
            self.menu[f"{t}°C"].set_callback(self.set_temp)

    @rumps.clicked("Turn On")
    def on(self, _): send(lambda: ac.turn_on())

    @rumps.clicked("Turn Off")
    def off(self, _): send(lambda: ac.turn_off())

    def set_temp(self, item):
        temp = int(item.title.replace("°C", ""))
        send(lambda: ac.set_temperature(temp))
        self.title = f"AC {temp}°"

asyncio.run_coroutine_threadsafe(connect(), loop)
ACApp().run()
