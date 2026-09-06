"""Seed knx_ga from the metadata already present in knx_measurements.

Every group address that has ever sent carries its own name and DPT in the
archive. That is a complete mapping without touching ETS at all.

Only recent chunks are scanned (default 90 days): chunk exclusion keeps this
cheap, and recent rows carry the *current* naming. Older group addresses that
no longer send are picked up by --full.
"""
from __future__ import annotations

import argparse

from db import knx_conn

SEED_SQL = """
INSERT INTO knx_ga (address, name, dpt, origin, last_seen)
SELECT DISTINCT ON (destination)
       destination,
       nullif(description, ''),
       nullif(dpt, ''),
       'seed',
       time
FROM knx_measurements
WHERE {where}
ORDER BY destination, time DESC
ON CONFLICT (address) DO NOTHING
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--days", type=int, default=90,
        help="how far back to scan (default 90)",
    )
    ap.add_argument(
        "--full", action="store_true",
        help="scan the entire hypertable — slow, only needed once",
    )
    args = ap.parse_args()

    since = None if args.full else f"{args.days} days"
    scope = "full history" if args.full else f"last {args.days} days"
    print(f"seeding knx_ga from knx_measurements ({scope}) ...", flush=True)

    where = "true" if args.full else "time >= now() - (%(since)s)::interval"

    with knx_conn() as conn:
        cur = conn.cursor()
        cur.execute("SET statement_timeout = '30min'")
        cur.execute(SEED_SQL.format(where=where), {"since": since})
        inserted = cur.rowcount
        conn.commit()

        cur.execute("SELECT count(*) FROM knx_ga")
        total = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM knx_ga WHERE dpt IS NULL")
        no_dpt = cur.fetchone()[0]

    print(f"  inserted: {inserted}")
    print(f"  knx_ga total: {total}  (without DPT: {no_dpt})")


if __name__ == "__main__":
    main()
