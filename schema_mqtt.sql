-- MQTT: broker settings and the topic/field mapping the UI edits.
--
-- Deliberately generic: a topic points at a target table, and each field maps
-- a JSON path to a column with an optional scale factor. A second device then
-- needs configuration, not code.

CREATE TABLE IF NOT EXISTS mqtt_topic (
    id            int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    topic         text NOT NULL UNIQUE,       -- may contain + and #
    target_db     text NOT NULL,              -- knx_data | ha_data | power_data
    target_table  text NOT NULL,
    time_path     text,                       -- JSON path to the timestamp
    time_is_local boolean NOT NULL DEFAULT true,
    enabled       boolean NOT NULL DEFAULT true,
    note          text,
    last_seen     timestamptz,
    last_error    text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON COLUMN mqtt_topic.time_path IS
    'Dotted path to the timestamp inside the payload, e.g. "Time". Empty means '
    'the time of reception is used.';
COMMENT ON COLUMN mqtt_topic.time_is_local IS
    'Tasmota sends local time without an offset. false = the value is UTC.';

CREATE TABLE IF NOT EXISTS mqtt_field (
    id         int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    topic_id   int NOT NULL REFERENCES mqtt_topic(id) ON DELETE CASCADE,
    json_path  text NOT NULL,                 -- e.g. "z.Pi"
    column_name text NOT NULL,
    scale      double precision NOT NULL DEFAULT 1,
    note       text,
    UNIQUE (topic_id, column_name)
);

COMMENT ON COLUMN mqtt_field.scale IS
    'Value is multiplied by this before storing. The Kamstrup feed reports '
    'totals in kW/kWh and phase values in W — scale keeps one unit per column.';

INSERT INTO settings (key, value, note) VALUES
    ('mqtt.enabled',      'false', 'MQTT-Quelle aktivieren'),
    ('mqtt.host',         '',      'Broker-Adresse'),
    ('mqtt.port',         '1883',  'ohne TLS meist 1883, mit TLS 8883'),
    ('mqtt.tls',          'false', 'TLS verwenden'),
    ('mqtt.tls_insecure', 'false', 'Zertifikat nicht prüfen (nur für Tests)'),
    ('mqtt.ca_cert',      '',      'Pfad zu einem CA-Zertifikat, optional'),
    ('mqtt.username',     '',      'leer = anonym'),
    ('mqtt.client_id',    'homearchive', 'Client-ID am Broker'),
    ('mqtt.qos',          '0',     '0, 1 oder 2')
ON CONFLICT (key) DO NOTHING;
