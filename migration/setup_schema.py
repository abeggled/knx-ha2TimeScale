"""One-time schema objects for the KNX migration (bookkeeping + quarantine)."""
from conn import pg_conn

DDL = """
CREATE TABLE IF NOT EXISTS knx_migration_progress (
    src_table       text PRIMARY KEY,
    last_id         bigint  NOT NULL DEFAULT 0,
    rows_copied     bigint  NOT NULL DEFAULT 0,
    rows_quarantined bigint NOT NULL DEFAULT 0,
    rows_after_cutoff bigint NOT NULL DEFAULT 0,
    finished        boolean NOT NULL DEFAULT false,
    started_at      timestamptz,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Rows that violate the NOT NULL constraints of knx_measurements.
-- Nothing is discarded; the disposition can be decided after the run.
CREATE TABLE IF NOT EXISTS knx_migration_quarantine (
    src_table   text,
    src_id      bigint,
    time        timestamptz,
    source      text,
    dpt         text,
    description text,
    knxvalue    text,
    destination text,
    reason      text
);

-- dpts encountered in the source that no mapping covers.
CREATE TABLE IF NOT EXISTS knx_migration_unmapped_dpt (
    dpt   text PRIMARY KEY,
    n     bigint NOT NULL DEFAULT 0
);
"""

if __name__ == "__main__":
    p = pg_conn()
    with p.cursor() as c:
        c.execute(DDL)
    p.commit()
    print("bookkeeping schema ready")
    p.close()
