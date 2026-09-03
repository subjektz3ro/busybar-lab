"""5 Hz radio keepalive — OFF by default; opt in with BARKEEP_KEEPALIVE=1.

The bar's Wi-Fi radio naps between packets, and at 1am against an idle bar a
5 Hz ping flattened HTTP p95 from 124 ms to 18 ms. A midday re-measurement
against a bar actually serving an app's draw loop reversed the verdict — max
latency 10.3 s WITH the ping vs 475 ms without — which is why skystrip
unplugged it. It is kept here only so the A/B can be re-run;
do not switch it back on without repeating that measurement under load.
"""

from __future__ import annotations

import asyncio


async def radio_keepalive(host: str | None) -> None:
    if not host:
        return
    while True:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-i", "0.2", "-q", host,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        try:
            await proc.wait()
        except asyncio.CancelledError:
            proc.terminate()
            raise
        await asyncio.sleep(5)   # ping exited (network blip); rearm
