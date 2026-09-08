"""MQTT mapping: topic matching, JSON paths, scaling and timestamps."""
import datetime as dt

import pytest

from mqtt_source import dig, parse_time, topic_matches

PAYLOAD = {
    "Time": "2026-09-08T09:29:44",
    "z": {"SMid": 11046456, "Pi": 0.000, "Po": 0.907,
          "P1o": 252, "P2o": 505, "P3o": 150,
          "V1": 231.0, "I1": 1.78, "Ei": 104.067, "rPo": 912},
}


@pytest.mark.parametrize("pattern,topic,expected", [
    ("tele/sm/SENSOR", "tele/sm/SENSOR", True),
    ("tele/+/SENSOR", "tele/sm/SENSOR", True),
    ("tele/+/SENSOR", "tele/a/b/SENSOR", False),
    ("tele/#", "tele/a/b/c", True),
    ("tele/sm/SENSOR", "tele/sm/STATE", False),
    ("tele/+/SENSOR", "tele/sm", False),
])
def test_topic_matches(pattern, topic, expected):
    assert topic_matches(pattern, topic) is expected


def test_dig_nested_and_missing():
    assert dig(PAYLOAD, "z.Po") == 0.907
    assert dig(PAYLOAD, "Time") == "2026-09-08T09:29:44"
    assert dig(PAYLOAD, "z.gibtsnicht") is None
    assert dig(PAYLOAD, "z.Po.tiefer") is None


def test_phase_sum_matches_total():
    """The feed reports totals in kW and phases in W — the scale factor in the
    mapping is what keeps one unit per column."""
    phases = dig(PAYLOAD, "z.P1o") + dig(PAYLOAD, "z.P2o") + dig(PAYLOAD, "z.P3o")
    assert phases == pytest.approx(dig(PAYLOAD, "z.Po") * 1000)


def test_parse_time_without_offset_is_local():
    stamp = parse_time("2026-09-08T09:29:44", local=True)
    assert stamp.tzinfo is not None
    assert stamp.hour == 9


def test_parse_time_as_utc():
    stamp = parse_time("2026-09-08T09:29:44", local=False)
    assert stamp.tzinfo is dt.UTC


def test_parse_time_falls_back_to_now():
    assert parse_time("kein Datum", local=True).tzinfo is not None
    assert parse_time(None, local=True).tzinfo is not None
