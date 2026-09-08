"""BKW power migration: db_power_LTS (MariaDB) -> power_data.bkw_measurements.

Scope decisions:
  * BKW only — PV is out of scope.
  * tbl_BKW_new (empty) and tbl_BKW_2026_01 (broken InnoDB tablespace) are skipped
    and recorded in bkw_migration_progress.note.
  * tbl_BKW_temp and tbl_BKW carry real data and are included.
  * timestamps read as UTC (MariaDB session tz '+00:00'), same rule as KNX.
  * no unique constraint on time — duplicates are preserved, restart is driven by
    the per-table ID watermark.

Migration order is chronological so the hypertable chunks fill in order:
    tbl_BKW_2023_03 ... tbl_BKW_2025_12, tbl_BKW_temp, tbl_BKW
"""
import datetime as dt
import time

import psycopg

from conn import mysql_conn

POWER_DSN = "host=<db-host> dbname=power_data user=postgres"
BATCH = 25_000
SKIP = {
    "tbl_BKW_new": "empty table",
    "tbl_BKW_2026_01": "broken: table doesn't exist in engine (InnoDB tablespace missing)",
    "tbl_PV": "out of scope",
}
COLS = [
    "energy_consumed_tariff_1", "energy_consumed_tariff_2", "energy_produced_tariff_1",
    "power_consumed", "power_consumed_phase_1", "power_consumed_phase_2",
    "power_consumed_phase_3", "power_produced", "power_produced_phase_1",
    "power_produced_phase_2", "power_produced_phase_3",
    "voltage_phase_1", "voltage_phase_2", "voltage_phase_3",
    "current_phase_1", "current_phase_2", "current_phase_3",
    "electricity_failures", "electricity_long_failures",
]
COPY_COLS = "time, " + ", ".join(COLS)
SELECT_COLS = "ID, `Timestamp`, " + ", ".join(f"`{c}`" for c in COLS)


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def ordered_tables(mc) -> list[str]:
    mc.execute(
        """select table_name from information_schema.tables
           where table_schema='db_power_LTS' and table_name like 'tbl\\_BKW%'
           order by table_name"""
    )
    names = [r["table_name"] for r in mc.fetchall()]
    monthly = sorted(n for n in names if n[8:].replace("_", "").isdigit() and len(n) == 15)
    tail = [n for n in ("tbl_BKW_temp", "tbl_BKW") if n in names]
    return monthly + tail


def migrate_table(m, p, table: str) -> None:
    pc = p.cursor()
    pc.execute(
        "insert into bkw_migration_progress (src_table, started_at) values (%s, now())"
        " on conflict (src_table) do nothing",
        (table,),
    )
    p.commit()
    pc.execute(
        "select last_id, rows_copied, finished from bkw_migration_progress where src_table=%s",
        (table,),
    )
    last_id, copied, finished = pc.fetchone()
    if finished:
        log(f"{table}: already finished ({copied:,} rows) — skipping")
        return
    if table in SKIP:
        pc.execute(
            "update bkw_migration_progress set finished=true, note=%s, updated_at=now()"
            " where src_table=%s",
            (SKIP[table], table),
        )
        p.commit()
        log(f"{table}: skipped — {SKIP[table]}")
        return

    log(f"{table}: {'resuming after ID %d' % last_id if last_id else 'starting'}")
    mc = m.cursor()
    t0 = time.time()

    while True:
        try:
            mc.execute(
                f"select {SELECT_COLS} from `{table}` where ID > %s order by ID limit {BATCH}",
                (last_id,),
            )
            rows = mc.fetchall()
        except Exception as e:
            pc.execute(
                "update bkw_migration_progress set note=%s, updated_at=now() where src_table=%s",
                (f"read error: {e}", table),
            )
            p.commit()
            log(f"{table}: READ ERROR — {e}")
            return
        if not rows:
            break

        recs = []
        for r in rows:
            ts = r["Timestamp"]
            if ts is None:
                continue
            recs.append(
                (ts.replace(tzinfo=dt.timezone.utc), *[r[c] for c in COLS])
            )
        if recs:
            with pc.copy(f"COPY bkw_measurements ({COPY_COLS}) FROM STDIN") as cp:
                for rec in recs:
                    cp.write_row(rec)
        copied += len(recs)
        last_id = rows[-1]["ID"]
        pc.execute(
            "update bkw_migration_progress set last_id=%s, rows_copied=%s, updated_at=now()"
            " where src_table=%s",
            (last_id, copied, table),
        )
        p.commit()

    pc.execute(
        "update bkw_migration_progress set finished=true, updated_at=now() where src_table=%s",
        (table,),
    )
    p.commit()
    dur = time.time() - t0
    log(f"{table}: done — {copied:,} rows in {dur/60:.1f} min ({copied/dur if dur else 0:,.0f}/s)")


def main() -> None:
    p = psycopg.connect(POWER_DSN)
    m = mysql_conn(db="db_power_LTS")
    mc = m.cursor()
    mc.execute("SET time_zone='+00:00'")

    tables = ordered_tables(mc)
    log(f"{len(tables)} BKW tables to process")
    try:
        for i, t in enumerate(tables, 1):
            log(f"--- [{i}/{len(tables)}] {t} ---")
            migrate_table(m, p, t)
    finally:
        try:
            p.rollback()
        except Exception:
            pass
        m.close()
        p.close()

    # Materialise the hourly aggregate over the whole imported range.
    with psycopg.connect(POWER_DSN, autocommit=True) as ac:
        log("refreshing bkw_hourly over the full range (this takes a few minutes)")
        ac.execute("CALL refresh_continuous_aggregate('bkw_hourly', NULL, NULL)")
        log("bkw_hourly materialised")
        ac.execute(
            """select chunk_schema||'.'||chunk_name from timescaledb_information.chunks
               where hypertable_name='bkw_measurements' and not is_compressed
                 and range_end < now() - interval '7 days'"""
        )
        chunks = [r[0] for r in ac.fetchall()]
        log(f"compressing {len(chunks)} chunk(s)")
        for ch in chunks:
            try:
                ac.execute("select compress_chunk(%s)", (ch,))
            except Exception as e:
                log(f"  compress failed for {ch}: {e}")
    log("POWER MIGRATION COMPLETE")


if __name__ == "__main__":
    main()
