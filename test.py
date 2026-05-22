import asyncio, aiohttp

async def check():
    url = 'https://api-fxpractice.oanda.com/v3/instruments/XAU_USD/candles'
    headers = {'Authorization': 'Bearer c0f5b5df69c77e8bf35dcfd2fbde72da-a4c6cbadba7ae39d21143f65e2c2b8ba'}
    params = {
        'granularity': 'M5',
        'from': '2026-05-21T23:40:00Z',
        'to':   '2026-05-22T00:05:00Z',
        'price': 'M'
    }
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=headers, params=params) as r:
            d = await r.json()
            for c in d['candles']:
                t = c['time'][:16]
                o = c['mid']['o']
                h = c['mid']['h']
                l = c['mid']['l']
                cl = c['mid']['c']
                print(f"{t}  O:{o}  H:{h}  L:{l}  C:{cl}")

asyncio.run(check())
