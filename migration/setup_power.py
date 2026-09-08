"""Create power_data: bkw_measurements hypertable + compression + hourly CAgg.

PV is intentionally out of scope (decision: not needed).
Run once, on the database host, as the postgres superuser via ~/.pgpass.
"""
import psycopg

ADMIN_DSN = "host=<db-host> dbname=postgres user=postgres"
POWER_DSN = "host=<db-host> dbname=power_data user=postgres"

METRICS = [
    "energy_consumed_tariff_1", "energy_consumed_tariff_2", "energy_produced_tariff_1",
    "power_consumed", "power_consumed_phase_1", "power_consumed_phase_2",
    "power_consumed_phase_3", "power_produced", "power_produced_phase_1",
    "power_produced_phase_2", "power_produced_phase_3",
    "voltage_phase_1", "voltage_phase_2", "voltage_phase_3",
    "current_phase_1", "current_phase_2", "current_phase_3",
    "electricity_failures", "electricity_long_failures",
]

TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS bkw_measurements (\n"
    "    time timestamptz NOT NULL,\n"
    + "".join(f"    {m} real,\n" for m in METRICS).rstrip(",\n")
    + "\n);"
)

SETUP = f"""
{TABLE_DDL}

SELECT create_hypertable('bkw_measurements', 'time',
                         chunk_time_interval => INTERVAL '1 month',
                         if_not_exists => TRUE);

ALTER TABLE bkw_measurements SET (
    timescaledb.compress,
    timescaledb.compress_orderby = 'time DESC'
);
"""

# Hourly continuous aggregate: averages/extremes for the power channels,
# last reading for the monotonic energy counters (a mean of a counter is
# meaningless; the last value per hour is what you bill and plot).
CAGG = """
CREATE MATERIALIZED VIEW IF NOT EXISTS bkw_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket(INTERVAL '1 hour', time)      AS bucket,
    avg(power_consumed)::real                 AS power_consumed_avg,
    max(power_consumed)::real                 AS power_consumed_max,
    min(power_consumed)::real                 AS power_consumed_min,
    avg(power_produced)::real                 AS power_produced_avg,
    max(power_produced)::real                 AS power_produced_max,
    avg(voltage_phase_1)::real                AS voltage_phase_1_avg,
    avg(voltage_phase_2)::real                AS voltage_phase_2_avg,
    avg(voltage_phase_3)::real                AS voltage_phase_3_avg,
    avg(current_phase_1)::real                AS current_phase_1_avg,
    avg(current_phase_2)::real                AS current_phase_2_avg,
    avg(current_phase_3)::real                AS current_phase_3_avg,
    last(energy_consumed_tariff_1, time)      AS energy_consumed_tariff_1_last,
    last(energy_consumed_tariff_2, time)      AS energy_consumed_tariff_2_last,
    last(energy_produced_tariff_1, time)      AS energy_produced_tariff_1_last,
    last(electricity_failures, time)          AS electricity_failures_last,
    count(*)                                  AS sample_count
FROM bkw_measurements
GROUP BY 1
WITH NO DATA;
"""

POLICIES = """
SELECT add_compression_policy('bkw_measurements', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_continuous_aggregate_policy('bkw_hourly',
    start_offset => INTERVAL '3 days',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE);
"""

PROGRESS = """
CREATE TABLE IF NOT EXISTS bkw_migration_progress (
    src_table   text PRIMARY KEY,
    last_id     bigint  NOT NULL DEFAULT 0,
    rows_copied bigint  NOT NULL DEFAULT 0,
    finished    boolean NOT NULL DEFAULT false,
    note        text,
    started_at  timestamptz,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
"""


def main() -> None:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as ac:
        exists = ac.execute(
            "select 1 from pg_database where datname='power_data'"
        ).fetchone()
        if not exists:
            ac.execute("CREATE DATABASE power_data")
            print("created database power_data")
        else:
            print("database power_data already exists")

    with psycopg.connect(POWER_DSN, autocommit=True) as pc:
        pc.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        pc.execute(SETUP)
        print("bkw_measurements hypertable ready")
        pc.execute(CAGG)
        print("bkw_hourly continuous aggregate created (no data yet)")
        pc.execute(POLICIES)
        print("compression + refresh policies installed")
        pc.execute(PROGRESS)
        print("bookkeeping table ready")


if __name__ == "__main__":
    main()
