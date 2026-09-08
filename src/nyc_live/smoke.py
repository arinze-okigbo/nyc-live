"""`just smoke`: one live fetch per registered feed, one line per feed, exit 1 on any failure.

Key-gated feeds that are not configured print SKIP and do not fail the run.
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

from nyc_live.config import get_settings
from nyc_live.contracts import ErrorKind, FeedAdapter, FeedUnavailable
from nyc_live.feeds import load_adapters
from nyc_live.http import make_client


async def _one(adapter: FeedAdapter[Any]) -> tuple[str, bool]:
    name = adapter.name.value
    if not adapter.is_configured():
        return f"SKIP  {name:<22} not configured (key missing)", True
    started = time.perf_counter()
    try:
        snap = await adapter.fetch()
    except FeedUnavailable as exc:
        ms = (time.perf_counter() - started) * 1000
        ok = exc.kind is ErrorKind.NOT_CONFIGURED
        tag = "SKIP" if ok else "FAIL"
        return f"{tag}  {name:<22} {exc.kind.value} {exc.message} ({ms:.0f} ms)", ok
    ms = (time.perf_counter() - started) * 1000
    return (
        f"OK    {name:<22} n={len(snap.records):<6} fetched={snap.fetched_at:%H:%M:%SZ} "
        f"stale_after={snap.stale_after:%H:%M:%SZ} ({ms:.0f} ms)",
        True,
    )


async def run() -> int:
    settings = get_settings()
    async with make_client(settings) as client:
        adapters = load_adapters(client, settings)
        if not adapters:
            print("FAIL  no feed adapters are registered yet (Phase 1 not started)")
            return 1
        results = await asyncio.gather(*(_one(a) for a in adapters))
    all_ok = True
    for line, ok in results:
        print(line)
        all_ok &= ok
    return 0 if all_ok else 1


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
