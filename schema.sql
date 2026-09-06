-- knx-ha2TimeScale — configuration and metadata schema.
-- Applied to the knx_data database (KNX side) and ha_data (HA side).
-- Idempotent: safe to re-run.

-- ── KNX group address inventory ───────────────────────────────────────────
-- Drives both decoding (dpt) and archiving (archive flag).
CREATE TABLE IF NOT EXISTS knx_ga (
    address     text PRIMARY KEY,               -- '6/6/52'
    name        text,                           -- group address name
    dpt         text,                           -- '9.001', '16.*', NULL = unknown
    description text,
    archive     boolean     NOT NULL DEFAULT true,
    origin      text        NOT NULL DEFAULT 'auto'
                CHECK (origin IN ('ets', 'seed', 'auto', 'manual')),
    note        text,                           -- why a value was pinned/excluded
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE knx_ga ADD COLUMN IF NOT EXISTS note text;

COMMENT ON COLUMN knx_ga.origin IS
    'ets = from .knxproj import | seed = derived from historic measurements | '
    'auto = first seen on the bus | manual = pinned by hand, never overwritten '
    'by an import';

-- ── Home Assistant entity inventory ───────────────────────────────────────
-- Auto-populated on first sighting; excluding an entity is a flag, not a rule.
CREATE TABLE IF NOT EXISTS ha_entity (
    entity_id   text PRIMARY KEY,
    domain      text GENERATED ALWAYS AS (split_part(entity_id, '.', 1)) STORED,
    archive     boolean     NOT NULL DEFAULT true,
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ha_entity_domain_idx ON ha_entity (domain);

-- ── Pattern based exclusions ──────────────────────────────────────────────
-- For whole families ('sensor.flightradar24%'); single items use the
-- archive flag above. Patterns win over the flag.
CREATE TABLE IF NOT EXISTS archive_exclude_pattern (
    id         int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind       text NOT NULL CHECK (kind IN ('knx', 'ha')),
    pattern    text NOT NULL,                   -- SQL LIKE syntax
    note       text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kind, pattern)
);

-- ── Runtime settings ──────────────────────────────────────────────────────
-- Connection parameters and tuning. Secrets stay in the systemd
-- EnvironmentFile and are referenced here by name, never stored.
CREATE TABLE IF NOT EXISTS settings (
    key        text PRIMARY KEY,
    value      text,
    note       text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO settings (key, value, note) VALUES
    ('knx.connection',      'tunnel',        'tunnel | routing'),
    ('knx.gateway_host',    '',              'KNXnet/IP gateway IP (tunnel mode)'),
    ('knx.gateway_port',    '3671',          NULL),
    ('knx.individual_address', '',           'own address on the bus, e.g. 1.1.250'),
    ('ha.websocket_url',    '',              'ws://host:8123/api/websocket'),
    ('ha.token_env',        'HA_TOKEN',      'name of the env var holding the token'),
    ('batch.max_rows',      '500',           'flush after N rows'),
    ('batch.max_seconds',   '2',             'flush after N seconds'),
    ('ga_reload_seconds',   '600',           'reload knx_ga / exclusions interval')
ON CONFLICT (key) DO NOTHING;

-- ── Import audit ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS knx_import_log (
    id          int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    imported_at timestamptz NOT NULL DEFAULT now(),
    source_file text,
    added       int NOT NULL DEFAULT 0,
    changed     int NOT NULL DEFAULT 0,
    vanished    int NOT NULL DEFAULT 0,
    note        text
);
