-- Meter epochs + epoch-aware views for power_data.
-- Raw measurements stay untouched; everything here is derived at read time.

CREATE TABLE IF NOT EXISTS bkw_meter_epochs (
    epoch_id            int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    meter_label         text        NOT NULL,
    valid_from          timestamptz NOT NULL,
    valid_to            timestamptz,                 -- NULL = current epoch
    offset_consumed_t1  double precision NOT NULL DEFAULT 0,
    offset_consumed_t2  double precision NOT NULL DEFAULT 0,
    offset_produced_t1  double precision NOT NULL DEFAULT 0,
    note                text,
    CONSTRAINT bkw_meter_epochs_range CHECK (valid_to IS NULL OR valid_to > valid_from)
);

-- No two epochs may cover the same instant.
CREATE EXTENSION IF NOT EXISTS btree_gist;
ALTER TABLE bkw_meter_epochs DROP CONSTRAINT IF EXISTS bkw_meter_epochs_no_overlap;
ALTER TABLE bkw_meter_epochs ADD CONSTRAINT bkw_meter_epochs_no_overlap
    EXCLUDE USING gist (tstzrange(valid_from, valid_to) WITH &&);

INSERT INTO bkw_meter_epochs (meter_label, valid_from, valid_to, note)
SELECT 'meter-1 (original)', timestamptz '2000-01-01 00:00:00+01', NULL,
       'Erster Zaehler. Offsets 0 — Startpunkt der Zaehlung. valid_to setzen, '
       'sobald der Ersatzzaehler liefert.'
WHERE NOT EXISTS (SELECT 1 FROM bkw_meter_epochs);


-- Raw counters plus the cumulative series across meter changes.
CREATE OR REPLACE VIEW bkw_energy_cumulative AS
SELECT
    m.time,
    e.meter_label,
    m.energy_consumed_tariff_1 AS raw_consumed_t1,
    m.energy_consumed_tariff_2 AS raw_consumed_t2,
    m.energy_produced_tariff_1 AS raw_produced_t1,
    m.energy_consumed_tariff_1::double precision + e.offset_consumed_t1 AS cum_consumed_t1,
    m.energy_consumed_tariff_2::double precision + e.offset_consumed_t2 AS cum_consumed_t2,
    m.energy_produced_tariff_1::double precision + e.offset_produced_t1 AS cum_produced_t1
FROM bkw_measurements m
JOIN bkw_meter_epochs e
  ON m.time >= e.valid_from AND (e.valid_to IS NULL OR m.time < e.valid_to);


-- Hourly consumption as deltas. A delta is only emitted when the previous
-- bucket is the immediately preceding hour AND belongs to the same epoch —
-- so recording gaps and meter changes yield NULL instead of a bogus figure.
CREATE OR REPLACE VIEW bkw_hourly_consumption AS
WITH h AS (
    SELECT
        b.bucket,
        e.epoch_id,
        e.meter_label,
        b.sample_count,
        b.power_consumed_avg,
        b.power_consumed_max,
        b.power_produced_avg,
        b.energy_consumed_tariff_1_last::double precision + e.offset_consumed_t1 AS cum_t1,
        b.energy_consumed_tariff_2_last::double precision + e.offset_consumed_t2 AS cum_t2,
        b.energy_produced_tariff_1_last::double precision + e.offset_produced_t1 AS cum_p1
    FROM bkw_hourly b
    JOIN bkw_meter_epochs e
      ON b.bucket >= e.valid_from AND (e.valid_to IS NULL OR b.bucket < e.valid_to)
),
d AS (
    SELECT h.*,
           lag(bucket)   OVER w AS prev_bucket,
           lag(epoch_id) OVER w AS prev_epoch,
           lag(cum_t1)   OVER w AS prev_t1,
           lag(cum_t2)   OVER w AS prev_t2,
           lag(cum_p1)   OVER w AS prev_p1
    FROM h
    WINDOW w AS (ORDER BY bucket)
)
SELECT
    bucket,
    meter_label,
    sample_count,
    sample_count < 300                       AS incomplete_hour,
    power_consumed_avg,
    power_consumed_max,
    power_produced_avg,
    cum_t1, cum_t2, cum_p1,
    CASE WHEN continuous AND cum_t1 >= prev_t1 THEN cum_t1 - prev_t1 END AS consumed_t1_kwh,
    CASE WHEN continuous AND cum_t2 >= prev_t2 THEN cum_t2 - prev_t2 END AS consumed_t2_kwh,
    CASE WHEN continuous AND cum_p1 >= prev_p1 THEN cum_p1 - prev_p1 END AS produced_t1_kwh,
    continuous AND (cum_t1 < prev_t1 OR cum_t2 < prev_t2 OR cum_p1 < prev_p1)
                                             AS counter_backwards
FROM (
    SELECT d.*,
           prev_bucket = bucket - INTERVAL '1 hour' AND prev_epoch = epoch_id AS continuous
    FROM d
) x;


-- Watchdog: counters running backwards inside one epoch. Not corrected —
-- shown, so a meter change, a rollover and plain garbage stay tellable apart.
CREATE OR REPLACE VIEW bkw_counter_anomalies AS
SELECT bucket, meter_label, sample_count,
       cum_t1, cum_t2, cum_p1
FROM bkw_hourly_consumption
WHERE counter_backwards
ORDER BY bucket DESC;
