import asyncio, os
from miraie_ac import MirAIeHub, MirAIeBroker

async def main():
    broker, hub = MirAIeBroker(), MirAIeHub()
    await hub.init(os.environ["MIRAIE_MOBILE"], os.environ["MIRAIE_PASSWORD"], broker)
    while getattr(broker, "client", None) is None:
        await asyncio.sleep(1)

    ac = hub.home.devices[0]
    print("Found:", ac.friendly_name)
    await ac.turn_on()
    await ac.set_temperature(24)
    await asyncio.sleep(3)
    await hub.close()

asyncio.run(main())
