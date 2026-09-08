"""Audit of all three TimescaleDB databases: tables, hypertables, policies, jobs.

Read-only. Prints what exists so leftovers and missing policies become visible
instead of being assumed.
"""
import psycopg

DBS = {
    "knx_data": "host=iqsrv36.a38.ch dbname=knx_data user=postgres",
    "ha_data": "host=iqsrv36.a38.ch dbname=ha_data user=postgres",
    "power_data": "host=iqsrv36.a38.ch dbname=power_data user=postgres",
}


def section(title):
    print(f"\n{'─' * 4} {title} {'─' * max(0, 60 - len(title))}")


for name, dsn in DBS.items():
    print(f"\n{'=' * 70}\n  {name}\n{'=' * 70}")
    with psycopg.connect(dsn) as conn:
        c = conn.cursor()
        c.execute("SET statement_timeout = '5min'")

        section("Tabellen")
        c.execute("""
            SELECT c.relname,
                   pg_size_pretty(pg_total_relation_size(c.oid)),
                   c.reltuples::bigint
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r'
            ORDER BY pg_total_relation_size(c.oid) DESC""")
        for rel, size, rows in c.fetchall():
            print(f"  {rel:<34} {size:>10}  ~{rows:>14,}")

        section("Sichten")
        c.execute("""SELECT table_name FROM information_schema.views
                     WHERE table_schema='public' ORDER BY 1""")
        print("  " + (", ".join(r[0] for r in c.fetchall()) or "keine"))

        section("Hypertables")
        c.execute("""
            SELECT h.hypertable_name, h.num_chunks, h.compression_enabled,
                   pg_size_pretty(hypertable_size(format('%I.%I', h.hypertable_schema,
                                                          h.hypertable_name)::regclass)),
                   d.time_interval
            FROM timescaledb_information.hypertables h
            JOIN timescaledb_information.dimensions d
              ON d.hypertable_name = h.hypertable_name
            ORDER BY 1""")
        for ht, chunks, comp, size, interval in c.fetchall():
            print(f"  {ht:<28} {chunks:>4} Chunks  {size:>9}  "
                  f"Intervall {interval}  Kompression {'an' if comp else 'AUS'}")

        section("Unkomprimierte Chunks (älter als 30 Tage)")
        c.execute("""
            SELECT hypertable_name, count(*), min(range_start)
            FROM timescaledb_information.chunks
            WHERE NOT is_compressed AND range_end < now() - interval '30 days'
            GROUP BY 1 ORDER BY 2 DESC""")
        rows = c.fetchall()
        for ht, n, oldest in rows:
            print(f"  {ht:<28} {n:>4} Chunks, ältester ab {oldest:%d.%m.%Y}")
        if not rows:
            print("  keine")

        section("Continuous Aggregates")
        c.execute("""SELECT view_name, materialized_only, compression_enabled,
                            finalized FROM timescaledb_information.continuous_aggregates
                     ORDER BY 1""")
        rows = c.fetchall()
        for v, mat, comp, fin in rows:
            print(f"  {v:<28} materialized_only={mat}  Kompression={comp}")
        if not rows:
            print("  keine")

        section("Jobs")
        c.execute("""
            SELECT j.job_id, j.proc_name, j.hypertable_name, j.schedule_interval,
                   j.config, s.last_run_status, s.last_successful_finish,
                   s.total_failures
            FROM timescaledb_information.jobs j
            LEFT JOIN timescaledb_information.job_stats s USING (job_id)
            WHERE j.job_id >= 1000 ORDER BY j.job_id""")
        rows = c.fetchall()
        for jid, proc, ht, sched, cfg, status, last_ok, fails in rows:
            print(f"  {jid} {proc:<28} {str(ht or ''):<22} alle {sched}")
            print(f"      Konfiguration: {cfg}")
            print(f"      Status {status or '—'}, zuletzt erfolgreich "
                  f"{last_ok.strftime('%d.%m. %H:%M') if last_ok else '—'}, "
                  f"Fehler gesamt {fails}")
        if not rows:
            print("  keine")
