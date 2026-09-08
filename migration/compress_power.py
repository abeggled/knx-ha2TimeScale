import psycopg
with psycopg.connect("host=<db-host> dbname=power_data user=postgres", autocommit=True) as p:
    cur = p.cursor()
    cur.execute("""select chunk_schema||\x27.\x27||chunk_name from timescaledb_information.chunks
                   where hypertable_name=\x27bkw_measurements\x27 and not is_compressed
                     and range_end < now() - interval \x277 days\x27 order by 1""")
    chunks=[r[0] for r in cur.fetchall()]
    print("compressing", len(chunks), "chunks", flush=True)
    for ch in chunks:
        try:
            cur.execute("select compress_chunk(%s)", (ch,))
        except Exception as e:
            print(" fail", ch, e, flush=True)
    cur.execute("select count(*), min(time), max(time) from bkw_measurements"); print("rows:", cur.fetchone())
    cur.execute("select pg_size_pretty(hypertable_size(\x27bkw_measurements\x27))"); print("size:", cur.fetchone()[0])
    cur.execute("select count(*) from bkw_hourly"); print("bkw_hourly buckets:", cur.fetchone()[0])
    cur.execute("select count(*) filter (where is_compressed) c, count(*) t from timescaledb_information.chunks where hypertable_name=\x27bkw_measurements\x27"); print("chunks compressed/total:", cur.fetchone())
