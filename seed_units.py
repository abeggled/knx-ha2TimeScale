"""Unit convention: keep the archive consistent across the Node-RED cutover.

xknx reports SI-style unit symbols ('min', 'h', 'lx', 'm³'), while the
existing rows in knx_measurements use the convention the previous collector
wrote ('minutes', 'hours', 'lux', 'm3'). Switching collectors must not change
the unit of a series, so the stored convention wins; xknx's symbol is only a
fallback for DPTs that have never occurred.

Populated from the archive with:
    python seed_units.py
"""
from __future__ import annotations

from db import knx_conn

DDL = """
CREATE TABLE IF NOT EXISTS knx_dpt_unit (
    dpt        text PRIMARY KEY,
    unit       text NOT NULL,
    origin     text NOT NULL DEFAULT 'archive'
               CHECK (origin IN ('archive', 'manual')),
    samples    bigint,
    updated_at timestamptz NOT NULL DEFAULT now()
);
"""

QUERY = """
SELECT DISTINCT ON (dpt) dpt, knxunit, count(*) OVER (PARTITION BY dpt, knxunit)
FROM knx_measurements
WHERE time >= now() - interval '%s days' AND dpt <> '' AND knxunit IS NOT NULL
ORDER BY dpt, count(*) OVER (PARTITION BY dpt, knxunit) DESC
"""


def main(days: int = 14) -> None:
    with knx_conn(autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute("SET statement_timeout = '15min'")
        cur.execute(DDL)
        cur.execute(
            """SELECT dpt, knxunit, count(*) AS n
               FROM knx_measurements
               WHERE time >= now() - (%s || ' days')::interval
                 AND dpt <> '' AND knxunit IS NOT NULL
               GROUP BY 1, 2 ORDER BY dpt, n DESC""",
            (days,),
        )
        best: dict[str, tuple[str, int]] = {}
        for dpt, unit, n in cur.fetchall():
            if dpt not in best:
                best[dpt] = (unit, n)

        cur.executemany(
            """INSERT INTO knx_dpt_unit (dpt, unit, origin, samples)
               VALUES (%s, %s, 'archive', %s)
               ON CONFLICT (dpt) DO UPDATE SET
                   unit = excluded.unit, samples = excluded.samples,
                   updated_at = now()
               WHERE knx_dpt_unit.origin <> 'manual'""",
            [(d, u, n) for d, (u, n) in best.items()],
        )
        print(f"{len(best)} DPT/unit pairs stored from the last {days} days")


if __name__ == "__main__":
    main()
