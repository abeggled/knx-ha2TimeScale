"""homearchive — collector service.

Replaces the Node-RED flow: KNX telegrams into knx_data.knx_measurements and
Home Assistant state changes into ha_data.ha_measurements.

  python service.py                 run both sources
  python service.py --only knx      KNX only
  python service.py --dry-run       write to *_dryrun tables instead
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import signal
import time

import psycopg

import db
import ha_source
import knx_source
from registry import Registry
from writer import BatchWriter

log = logging.getLogger("collector")


async def status_loop(knx, ha, knx_writer, ha_writer, started: str) -> None:
    """Log a summary and publish a heartbeat the UI can read."""
    while True:
        await asyncio.sleep(30)
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
        try:
            async with await psycopg.AsyncConnection.connect(
                db.KNX_DSN, autocommit=True
            ) as conn:
                await conn.cursor().execute(
                    """UPDATE collector_status SET
                         updated_at = now(), started_at = %s,
                         knx_connected = %s, ha_connected = %s,
                         knx_received = %s, knx_written = %s, knx_skipped = %s,
                         knx_undecoded = %s, knx_dropped = %s,
                         ha_received = %s, ha_written = %s, ha_skipped = %s,
                         ha_dropped = %s
                       WHERE id = 1""",
                    (
                        started,
                        bool(knx and knx.xknx is not None),
                        bool(ha and ha.received >= 0),
                        knx.received if knx else 0,
                        knx_writer.written if knx_writer else 0,
                        knx.skipped if knx else 0,
                        knx.undecoded if knx else 0,
                        knx_writer.dropped if knx_writer else 0,
                        ha.received if ha else 0,
                        ha_writer.written if ha_writer else 0,
                        ha.skipped if ha else 0,
                        ha_writer.dropped if ha_writer else 0,
                    ),
                )
        except Exception as exc:  # noqa: BLE001 — heartbeat must never kill the run
            log.debug("heartbeat failed: %s", exc)

        if parts and int(time.monotonic()) % 300 < 30:
            log.info(" | ".join(parts))


def _watch(task: asyncio.Task) -> asyncio.Task:
    """Surface exceptions: an unobserved task dies silently otherwise."""
    def done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        if exc := t.exception():
            log.error("task %s died: %r", t.get_name(), exc, exc_info=exc)
    task.add_done_callback(done)
    return task


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

    tasks = [_watch(asyncio.create_task(registry.reload_loop(), name="registry"))]
    knx = ha = None
    knx_writer = ha_writer = None

    if args.only != "ha":
        knx_writer = BatchWriter(db.KNX_DSN, f"knx_measurements{suffix}",
                                 knx_source.COLUMNS, max_rows, max_secs)
        knx = knx_source.KNXSource(registry, knx_writer)
        tasks += [_watch(asyncio.create_task(knx_writer.run(), name="knx-writer")),
                  _watch(asyncio.create_task(knx.run(), name="knx-source"))]

    if args.only != "knx":
        ha_writer = BatchWriter(db.HA_DSN, f"ha_measurements{suffix}",
                                ha_source.COLUMNS, max_rows, max_secs)
        ha = ha_source.HASource(registry, ha_writer)
        tasks += [_watch(asyncio.create_task(ha_writer.run(), name="ha-writer")),
                  _watch(asyncio.create_task(ha.run(), name="ha-source"))]

    tasks.append(_watch(asyncio.create_task(
        status_loop(knx, ha, knx_writer, ha_writer,
                    dt.datetime.now(dt.UTC).isoformat()), name="status")))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    # SIGHUP reloads registry immediately (after an ETS import, for example).
    loop.add_signal_handler(
        signal.SIGHUP, lambda: _watch(asyncio.create_task(registry.load()))
    )

    log.info("collector started%s", " (dry run)" if args.dry_run else "")
    await stop.wait()
    log.info("shutting down")
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
