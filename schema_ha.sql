-- Home Assistant side. Applied to the ha_data database.
-- The control tables (settings, exclusion patterns) live in knx_data;
-- this database only holds the entity inventory next to its measurements.

CREATE TABLE IF NOT EXISTS ha_entity (
    entity_id   text PRIMARY KEY,
    domain      text GENERATED ALWAYS AS (split_part(entity_id, '.', 1)) STORED,
    archive     boolean     NOT NULL DEFAULT true,
    note        text,
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ha_entity_domain_idx ON ha_entity (domain);
