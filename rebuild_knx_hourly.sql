-- Rebuild of knx_hourly.
--
-- The previous definition did avg(knxvalue::double precision) over a text
-- column that also holds 'true', 'false', 'null', DPT 16 strings and hex
-- values. Every refresh failed with "invalid input syntax for type double
-- precision", 809 times, and the view never held a single row.
--
-- The cast is now guarded: non-numeric values yield NULL for the numeric
-- aggregates while last_value and event_count still work for text points.

DROP MATERIALIZED VIEW IF EXISTS knx_hourly CASCADE;

CREATE MATERIALIZED VIEW knx_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket(INTERVAL '1 hour', time) AS bucket,
    destination,
    last(knxvalue, time)                 AS last_value,
    count(*)                             AS event_count,
    count(*) FILTER (
        WHERE knxvalue ~ '^-?[0-9]+\.?[0-9]*$')            AS numeric_count,
    avg(CASE WHEN knxvalue ~ '^-?[0-9]+\.?[0-9]*$'
             THEN knxvalue::double precision END)          AS avg_value,
    min(CASE WHEN knxvalue ~ '^-?[0-9]+\.?[0-9]*$'
             THEN knxvalue::double precision END)          AS min_value,
    max(CASE WHEN knxvalue ~ '^-?[0-9]+\.?[0-9]*$'
             THEN knxvalue::double precision END)          AS max_value
FROM knx_measurements
GROUP BY 1, 2
WITH NO DATA;

SELECT add_continuous_aggregate_policy('knx_hourly',
    start_offset      => INTERVAL '3 days',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists     => TRUE);

-- Aggregates were the only uncompressed chunks left in all three databases.
ALTER MATERIALIZED VIEW knx_hourly SET (timescaledb.compress);
SELECT add_compression_policy('knx_hourly', compress_after => INTERVAL '30 days',
                              if_not_exists => TRUE);
