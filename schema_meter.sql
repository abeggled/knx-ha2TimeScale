-- MQTT meter feed and a consumption series that spans both meters.
--
-- The old BKW meter reports HT/NT separately, the Kamstrup OMNIPOWER reports
-- tariff-free. Summing the two old registers reproduces the tariff-free value,
-- so the join happens in a view — the 35 million archived rows stay untouched.

-- ── target table for the MQTT meter ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS meter_measurements (
    time              timestamptz NOT NULL,
    meter_id          text,             -- SMid
    energy_consumed   double precision, -- Ei   kWh
    energy_produced   double precision, -- Eo   kWh
    reactive_in       double precision, -- rEi  kvarh
    reactive_out      double precision, -- rEo  kvarh
    power_consumed    double precision, -- Pi   kW
    power_produced    double precision, -- Po   kW
    power_in_l1       real,             -- P1i  W
    power_in_l2       real,
    power_in_l3       real,
    power_out_l1      real,             -- P1o  W
    power_out_l2      real,
    power_out_l3      real,
    reactive_power_in  real,            -- rPi  var
    reactive_power_out real,            -- rPo  var
    voltage_l1        real,
    voltage_l2        real,
    voltage_l3        real,
    current_l1        real,
    current_l2        real,
    current_l3        real,
    power_factor_l1   real
);

SELECT create_hypertable('meter_measurements', 'time',
                         chunk_time_interval => INTERVAL '1 month',
                         if_not_exists => TRUE);

ALTER TABLE meter_measurements SET (
    timescaledb.compress,
    timescaledb.compress_orderby = 'time DESC'
);
SELECT add_compression_policy('meter_measurements', INTERVAL '7 days',
                              if_not_exists => TRUE);

-- ── epochs: which table holds which period, and with what offset ──────────
ALTER TABLE bkw_meter_epochs
    ADD COLUMN IF NOT EXISTS source_table text NOT NULL DEFAULT 'bkw_measurements',
    ADD COLUMN IF NOT EXISTS offset_consumed double precision NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS offset_produced double precision NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS ct_factor double precision NOT NULL DEFAULT 1;

COMMENT ON COLUMN bkw_meter_epochs.ct_factor IS
    'Transformer ratio from the meter label. 1 = direct meter, no conversion.';

-- The first epoch starts at zero; its tariff-free offsets stay 0.
UPDATE bkw_meter_epochs
   SET offset_consumed = offset_consumed_t1 + offset_consumed_t2,
       offset_produced = offset_produced_t1
 WHERE source_table = 'bkw_measurements' AND offset_consumed = 0;

-- ── one series across both meters ────────────────────────────────────────
CREATE OR REPLACE VIEW energy_readings AS
SELECT b.time,
       'bkw_measurements'::text AS source_table,
       (b.energy_consumed_tariff_1::double precision
        + b.energy_consumed_tariff_2::double precision) AS consumed_raw,
       b.energy_produced_tariff_1::double precision     AS produced_raw,
       b.power_consumed::double precision               AS power_consumed,
       b.power_produced::double precision               AS power_produced
FROM bkw_measurements b
UNION ALL
SELECT m.time, 'meter_measurements',
       m.energy_consumed, m.energy_produced,
       m.power_consumed, m.power_produced
FROM meter_measurements m;

CREATE OR REPLACE VIEW energy_cumulative AS
SELECT r.time, e.meter_label, e.source_table,
       r.consumed_raw, r.produced_raw,
       r.consumed_raw * e.ct_factor + e.offset_consumed AS cum_consumed,
       r.produced_raw * e.ct_factor + e.offset_produced AS cum_produced,
       r.power_consumed * e.ct_factor AS power_consumed,
       r.power_produced * e.ct_factor AS power_produced
FROM energy_readings r
JOIN bkw_meter_epochs e
  ON r.source_table = e.source_table
 AND r.time >= e.valid_from
 AND (e.valid_to IS NULL OR r.time < e.valid_to);
