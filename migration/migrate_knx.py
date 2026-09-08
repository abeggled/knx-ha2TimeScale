"""KNX migration: db_knx_LTS (MariaDB) -> knx_data.knx_measurements (TimescaleDB).

Rules agreed with Daniel:
  * timestamps read as UTC (MariaDB session tz '+00:00') -> DST-safe
  * rows at or after the target's existing minimum are skipped (no overlap)
  * source ID column is dropped (no counterpart in target)
  * knxunit derived from dpt; unmapped dpts -> 'unknown', logged
  * rows violating target NOT NULL constraints go to quarantine, not into the table

Restartable: per source table, the last copied source ID is committed with the batch.
"""
import datetime as dt
import sys
import time

from conn import mysql_conn, pg_conn
from dpt_units import unit_for

BATCH = 25_000
CUTOFF_UTC = dt.datetime(2025, 10, 18, 23, 0, 0, 439827, tzinfo=dt.timezone.utc)
COLS = "time, source, dpt, description, knxvalue, destination, knxunit"


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def strip_nul(v):
    """PostgreSQL text cannot hold 0x00. DPT 16.x pads its 14-byte strings with
    NUL, so the padding is not payload — remove it and keep the row."""
    if isinstance(v, str) and "\x00" in v:
        return v.replace("\x00", ""), True
    return v, False


def source_tables(mc) -> list[str]:
    mc.execute(
        """select table_name from information_schema.tables
           where table_schema='db_knx_LTS' and table_name like 'tbl_TelegramArchive\\_%'
           order by table_name"""
    )
    return [r["table_name"] for r in mc.fetchall()]


def migrate_table(m, p, table: str, unmapped: dict[str, int]) -> None:
    pc = p.cursor()
    pc.execute(
        """insert into knx_migration_progress (src_table, started_at)
           values (%s, now()) on conflict (src_table) do nothing""",
        (table,),
    )
    p.commit()
    pc.execute(
        "select last_id, rows_copied, finished from knx_migration_progress where src_table=%s",
        (table,),
    )
    last_id, copied, finished = pc.fetchone()
    if finished:
        log(f"{table}: already finished ({copied:,} rows) — skipping")
        return

    if last_id:
        log(f"{table}: resuming after ID {last_id:,} ({copied:,} rows already copied)")
    else:
        log(f"{table}: starting")

    mc = m.cursor()
    t_table = time.time()
    quarantined = after_cutoff = nul_rows = 0

    while True:
        mc.execute(
            f"""select ID, source, dpt, description, knxvalue, destination, timestamp
                from `{table}` where ID > %s order by ID limit {BATCH}""",
            (last_id,),
        )
        rows = mc.fetchall()
        if not rows:
            break

        good, bad, nul_log = [], [], []
        for r in rows:
            ts = r["timestamp"]
            if ts is None:
                bad.append((r, "timestamp is null"))
                continue
            ts = ts.replace(tzinfo=dt.timezone.utc)
            if ts >= CUTOFF_UTC:
                after_cutoff += 1
                continue
            missing = [
                c for c in ("source", "dpt", "destination") if r[c] is None
            ]
            if missing:
                bad.append((r, "null: " + ",".join(missing)))
                continue
            unit, mapped = unit_for(r["dpt"])
            if not mapped:
                unmapped[r["dpt"]] = unmapped.get(r["dpt"], 0) + 1

            vals, had_nul = {}, False
            for col in ("source", "dpt", "description", "knxvalue", "destination"):
                vals[col], hit = strip_nul(r[col])
                had_nul = had_nul or hit
            if had_nul:
                nul_rows += 1
                nul_log.append((r, "nul_stripped"))

            good.append(
                (ts, vals["source"], vals["dpt"], vals["description"],
                 vals["knxvalue"], vals["destination"], unit)
            )

        if good:
            with pc.copy(f"COPY knx_measurements ({COLS}) FROM STDIN") as cp:
                for rec in good:
                    cp.write_row(rec)
        # NUL-affected rows are logged with the original (repr-escaped, since the
        # raw bytes cannot be stored in PG text either) and still inserted above.
        for r, reason in nul_log:
            ts = r["timestamp"]
            pc.execute(
                """insert into knx_migration_quarantine
                   (src_table, src_id, time, source, dpt, description,
                    knxvalue, destination, reason)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (table, r["ID"], ts.replace(tzinfo=dt.timezone.utc) if ts else None,
                 repr(r["source"]), repr(r["dpt"]), repr(r["description"]),
                 repr(r["knxvalue"]), repr(r["destination"]), reason),
            )
        for r, reason in bad:
            ts = r["timestamp"]
            pc.execute(
                """insert into knx_migration_quarantine
                   (src_table, src_id, time, source, dpt, description,
                    knxvalue, destination, reason)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (table, r["ID"],
                 ts.replace(tzinfo=dt.timezone.utc) if ts else None,
                 r["source"], r["dpt"], r["description"],
                 r["knxvalue"], r["destination"], reason),
            )
        quarantined += len(bad)
        copied += len(good)
        last_id = rows[-1]["ID"]

        pc.execute(
            """update knx_migration_progress
               set last_id=%s, rows_copied=%s, rows_quarantined=%s,
                   rows_after_cutoff=%s, updated_at=now()
               where src_table=%s""",
            (last_id, copied, quarantined, after_cutoff, table),
        )
        p.commit()

    pc.execute(
        "update knx_migration_progress set finished=true, updated_at=now() where src_table=%s",
        (table,),
    )
    p.commit()
    dur = time.time() - t_table
    rate = copied / dur if dur else 0
    log(f"{table}: done — {copied:,} rows in {dur/60:.1f} min ({rate:,.0f}/s), "
        f"quarantined {quarantined}, nul-stripped {nul_rows}, "
        f"skipped-after-cutoff {after_cutoff}")


def compress_backlog(p) -> None:
    """Compress every uncompressed chunk that lies entirely before the cutoff."""
    c = p.cursor()
    c.execute(
        """select chunk_schema||'.'||chunk_name
           from timescaledb_information.chunks
           where hypertable_name='knx_measurements'
             and not is_compressed and range_end <= %s""",
        (CUTOFF_UTC,),
    )
    chunks = [r[0] for r in c.fetchall()]
    if not chunks:
        return
    log(f"compressing {len(chunks)} chunk(s)")
    for ch in chunks:
        try:
            c.execute("select compress_chunk(%s)", (ch,))
            p.commit()
        except Exception as e:  # keep going; recompress at the end
            p.rollback()
            log(f"  compress failed for {ch}: {e}")


def main() -> None:
    p = pg_conn()
    m = mysql_conn(db="db_knx_LTS")
    mc = m.cursor()
    mc.execute("SET time_zone='+00:00'")
    mc.execute("SET SESSION net_read_timeout=600, net_write_timeout=600")

    pc = p.cursor()
    pc.execute("select alter_job(1000, scheduled => false)")
    p.commit()
    log("compression policy paused")

    unmapped: dict[str, int] = {}
    tables = source_tables(mc)
    log(f"{len(tables)} source tables, cutoff {CUTOFF_UTC.isoformat()}")

    try:
        for i, t in enumerate(tables, 1):
            log(f"--- [{i}/{len(tables)}] {t} ---")
            migrate_table(m, p, t, unmapped)
            compress_backlog(p)
    finally:
        # The connection may be in a failed transaction if we got here via an
        # exception — clear it before the bookkeeping writes.
        try:
            p.rollback()
        except Exception:
            pass
        for d, n in unmapped.items():
            pc.execute(
                """insert into knx_migration_unmapped_dpt (dpt, n) values (%s,%s)
                   on conflict (dpt) do update set n = knx_migration_unmapped_dpt.n + excluded.n""",
                (d, n),
            )
        p.commit()
        pc.execute("select alter_job(1000, scheduled => true)")
        p.commit()
        log("compression policy re-enabled")
        m.close()
        p.close()

    log("MIGRATION COMPLETE")


if __name__ == "__main__":
    sys.exit(main())
