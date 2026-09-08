-- Heartbeat: the UI runs as a separate service and cannot see the collector's
-- process state, so the collector reports into the database.

CREATE TABLE IF NOT EXISTS collector_status (
    id            int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    started_at    timestamptz,
    knx_connected boolean,
    ha_connected  boolean,
    knx_received  bigint DEFAULT 0,
    knx_written   bigint DEFAULT 0,
    knx_skipped   bigint DEFAULT 0,
    knx_undecoded bigint DEFAULT 0,
    knx_dropped   bigint DEFAULT 0,
    ha_received   bigint DEFAULT 0,
    ha_written    bigint DEFAULT 0,
    ha_skipped    bigint DEFAULT 0,
    ha_dropped    bigint DEFAULT 0,
    mqtt_connected boolean,
    mqtt_received bigint DEFAULT 0,
    mqtt_mapped   bigint DEFAULT 0,
    mqtt_unmatched bigint DEFAULT 0,
    last_error    text
);

INSERT INTO collector_status (id) VALUES (1) ON CONFLICT DO NOTHING;
