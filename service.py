"""knx-ha2TimeScale — collector service.

Replaces the Node-RED flow: KNX telegrams into knx_data.knx_measurements and
Home Assistant state changes into ha_data.ha_measurements.

  python service.py                 run both sources
  python service.py --only knx      KNX only
  python service.py --dry-run       write to *_dryrun tables instead
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal

import db
import ha_source
import knx_source
from registry import Registry
from writer import BatchWriter

log = logging.getLogger("collector")


async def status_loop(knx, ha, knx_writer, ha_writer) -> None:
    while True:
        await asyncio.sleep(300)
        parts = []
        if knx:
            parts.append(
                f"KNX rx={knx.received} skip={knx.skipped} raw={knx.undecoded} "
                f"written={knx_writer.written} dropped={knx_writer.dropped}"
            )
        if ha:
            parts.append(
                f"HA rx={ha.received} skip={ha.skipped} "
                f"written={ha_writer.written} dropped={ha_writer.dropped}"
            )
        log.info(" | ".join(parts))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["knx", "ha"], help="run a single source")
    ap.add_argument("--dry-run", action="store_true",
                    help="write to knx_measurements_dryrun / ha_measurements_dryrun")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    suffix = "_dryrun" if args.dry_run else ""
    registry = Registry(db.KNX_DSN, db.HA_DSN)
    await registry.load()

    max_rows = int(registry.settings.get("batch.max_rows", "500"))
    max_secs = float(registry.settings.get("batch.max_seconds", "2"))

    tasks = [asyncio.create_task(registry.reload_loop(), name="registry")]
    knx = ha = None
    knx_writer = ha_writer = None

    if args.only != "ha":
        knx_writer = BatchWriter(db.KNX_DSN, f"knx_measurements{suffix}",
                                 knx_source.COLUMNS, max_rows, max_secs)
        knx = knx_source.KNXSource(registry, knx_writer)
        tasks += [asyncio.create_task(knx_writer.run(), name="knx-writer"),
                  asyncio.create_task(knx.run(), name="knx-source")]

    if args.only != "knx":
        ha_writer = BatchWriter(db.HA_DSN, f"ha_measurements{suffix}",
                                ha_source.COLUMNS, max_rows, max_secs)
        ha = ha_source.HASource(registry, ha_writer)
        tasks += [asyncio.create_task(ha_writer.run(), name="ha-writer"),
                  asyncio.create_task(ha.run(), name="ha-source")]

    tasks.append(asyncio.create_task(
        status_loop(knx, ha, knx_writer, ha_writer), name="status"))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    # SIGHUP reloads registry immediately (after an ETS import, for example).
    loop.add_signal_handler(
        signal.SIGHUP, lambda: asyncio.create_task(registry.load())
    )

    log.info("collector started%s", " (dry run)" if args.dry_run else "")
    await stop.wait()
    log.info("shutting down")
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
