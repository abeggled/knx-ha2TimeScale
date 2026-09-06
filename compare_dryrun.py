"""Compare the dry run against what the current collector wrote.

Runs every check that mattered during the Node-RED migration:
row counts, per group address counts, units, and value sets. Anything the
old collector did that the new one does not reproduce shows up here.

    python compare_dryrun.py            # KNX and HA
    python compare_dryrun.py --only ha
"""
from __future__ import annotations

import argparse

from db import ha_conn, knx_conn


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)))


def compare_knx(limit: int) -> None:
    with knx_conn() as conn:
        cur = conn.cursor()
        cur.execute("SET statement_timeout = '10min'")
        cur.execute("SELECT min(time), max(time), count(*) FROM knx_measurements_dryrun")
        lo, hi, n = cur.fetchone()
        if not n:
            print("dry run table is empty")
            return
        cur.execute(
            "SELECT count(*) FROM knx_measurements WHERE time BETWEEN %s AND %s",
            (lo, hi),
        )
        old = cur.fetchone()[0]
        section("KNX")
        print(f"window   {lo:%Y-%m-%d %H:%M:%S} .. {hi:%H:%M:%S}")
        print(f"new      {n:>8}")
        print(f"current  {old:>8}   delta {n - old:+d}")

        cur.execute(
            """WITH d AS (SELECT destination, count(*) n FROM knx_measurements_dryrun
                          GROUP BY 1),
                    o AS (SELECT destination, count(*) n FROM knx_measurements
                          WHERE time BETWEEN %s AND %s GROUP BY 1)
               SELECT coalesce(d.destination, o.destination),
                      coalesce(d.n, 0), coalesce(o.n, 0)
               FROM d FULL OUTER JOIN o USING (destination)
               WHERE abs(coalesce(d.n, 0) - coalesce(o.n, 0)) > 2
               ORDER BY abs(coalesce(d.n, 0) - coalesce(o.n, 0)) DESC LIMIT %s""",
            (lo, hi, limit),
        )
        rows = cur.fetchall()
        section("group addresses differing by more than two rows")
        print("\n".join(f"  {a:<10} new={b:<6} current={c}" for a, b, c in rows)
              or "  none")

        cur.execute(
            """WITH d AS (SELECT destination, knxunit u FROM knx_measurements_dryrun
                          GROUP BY 1, 2),
                    o AS (SELECT destination, knxunit u FROM knx_measurements
                          WHERE time BETWEEN %s AND %s GROUP BY 1, 2)
               SELECT d.destination, d.u, o.u FROM d JOIN o USING (destination)
               WHERE d.u IS DISTINCT FROM o.u LIMIT %s""",
            (lo, hi, limit),
        )
        rows = cur.fetchall()
        section("unit mismatches")
        print("\n".join(f"  {a:<10} new={b!r:<12} current={c!r}" for a, b, c in rows)
              or "  none")

        cur.execute(
            """WITH d AS (SELECT destination, array_agg(DISTINCT knxvalue) v
                          FROM knx_measurements_dryrun GROUP BY 1),
                    o AS (SELECT destination, array_agg(DISTINCT knxvalue) v
                          FROM knx_measurements WHERE time BETWEEN %s AND %s
                          GROUP BY 1)
               SELECT d.destination, d.v[1:4], o.v[1:4]
               FROM d JOIN o USING (destination) WHERE NOT (d.v <@ o.v) LIMIT %s""",
            (lo, hi, limit),
        )
        rows = cur.fetchall()
        section("value formats the current collector never produced")
        print("\n".join(f"  {a:<10} new={b} current={c}" for a, b, c in rows)
              or "  none")


def compare_ha(limit: int) -> None:
    with ha_conn() as conn:
        cur = conn.cursor()
        cur.execute("SET statement_timeout = '10min'")
        cur.execute("SELECT min(time), max(time), count(*) FROM ha_measurements_dryrun")
        lo, hi, n = cur.fetchone()
        if not n:
            print("dry run table is empty")
            return
        cur.execute(
            "SELECT count(*) FROM ha_measurements WHERE time BETWEEN %s AND %s",
            (lo, hi),
        )
        old = cur.fetchone()[0]
        section("Home Assistant")
        print(f"window   {lo:%Y-%m-%d %H:%M:%S} .. {hi:%H:%M:%S}")
        print(f"new      {n:>8}")
        print(f"current  {old:>8}   delta {n - old:+d}")

        cur.execute(
            """WITH d AS (SELECT sensorid, count(*) n FROM ha_measurements_dryrun
                          GROUP BY 1),
                    o AS (SELECT sensorid, count(*) n FROM ha_measurements
                          WHERE time BETWEEN %s AND %s GROUP BY 1)
               SELECT coalesce(d.sensorid, o.sensorid),
                      coalesce(d.n, 0), coalesce(o.n, 0)
               FROM d FULL OUTER JOIN o USING (sensorid)
               WHERE abs(coalesce(d.n, 0) - coalesce(o.n, 0)) > 2
               ORDER BY abs(coalesce(d.n, 0) - coalesce(o.n, 0)) DESC LIMIT %s""",
            (lo, hi, limit),
        )
        rows = cur.fetchall()
        section("entities differing by more than two rows")
        print("\n".join(f"  {a:<45} new={b:<6} current={c}" for a, b, c in rows)
              or "  none")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["knx", "ha"])
    ap.add_argument("--limit", type=int, default=15)
    args = ap.parse_args()
    if args.only != "ha":
        compare_knx(args.limit)
    if args.only != "knx":
        compare_ha(args.limit)


if __name__ == "__main__":
    main()
