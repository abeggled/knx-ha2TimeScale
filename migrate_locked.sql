-- Split the overloaded origin column.
--
-- origin described where name and DPT came from, but was also misused as a
-- protection flag against ETS imports. Toggling archiving in the UI therefore
-- froze a group address's metadata as a side effect. Protection now lives in
-- its own column.

ALTER TABLE knx_ga ADD COLUMN IF NOT EXISTS locked boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN knx_ga.locked IS
    'true = name and DPT are kept as they are; a .knxproj import reports the '
    'difference but does not overwrite. Set deliberately, e.g. where ETS '
    'carries a wrong datapoint type.';

-- Carry the existing protection over: everything that was pinned on purpose
-- had a note explaining why the DPT deviates.
UPDATE knx_ga SET locked = true
WHERE origin = 'manual' AND note ILIKE '%DPT%';

-- Restore the real provenance. The nine VISU addresses are not linked in the
-- ETS project and came from the archive seed; the rest is from ETS.
UPDATE knx_ga SET origin = 'seed'
WHERE origin = 'manual'
  AND address IN ('17/6/2', '20/6/2', '20/6/7', '22/6/12', '25/6/2',
                  '27/6/2', '28/6/2', '29/6/2', '4/6/22');

UPDATE knx_ga SET origin = 'ets' WHERE origin = 'manual';

ALTER TABLE knx_ga DROP CONSTRAINT IF EXISTS knx_ga_origin_check;
ALTER TABLE knx_ga ADD CONSTRAINT knx_ga_origin_check
    CHECK (origin IN ('ets', 'seed', 'auto'));

COMMENT ON COLUMN knx_ga.origin IS
    'ets = from a .knxproj import | seed = derived from historic measurements '
    '| auto = first seen on the bus. Never set by hand.';
