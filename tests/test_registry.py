"""Archiving decisions and pattern matching."""
from registry import GA, Registry


def make() -> Registry:
    r = Registry("", "")
    r.ga = {"1/2/3": GA(name="Test", dpt="9.001", archive=True),
            "1/2/4": GA(name="Aus", dpt="9.001", archive=False)}
    r.ha_entities = {"sensor.a": True, "sensor.b": False}
    return r


def test_archive_flag_is_honoured():
    r = make()
    assert r.knx_archive("1/2/3") is True
    assert r.knx_archive("1/2/4") is False
    assert r.ha_archive("sensor.a") is True
    assert r.ha_archive("sensor.b") is False


def test_pattern_beats_flag():
    r = make()
    r.knx_patterns = ["1/2/%"]
    r.ha_patterns = ["sensor.%"]
    assert r.knx_archive("1/2/3") is False
    assert r.ha_archive("sensor.a") is False


def test_pattern_wildcard_in_the_middle():
    r = make()
    r.ha_patterns = ["sensor.awtrix%free_ram"]
    assert r.ha_archive("sensor.awtrix_b4a5cc_free_ram") is False
    assert r.ha_archive("sensor.awtrix_b4a5cc_uptime") is True


def test_unknown_is_archived_and_registered():
    r = make()
    assert r.knx_archive("9/9/9") is True
    assert "9/9/9" in r._new_ga
    assert r.ha_archive("sensor.neu") is True
    assert "sensor.neu" in r._new_entities


def test_excluded_entries_still_enter_the_inventory():
    """Otherwise a pattern appears to match nothing and cannot be reviewed."""
    r = make()
    r.ha_patterns = ["automation.%"]
    assert r.ha_archive("automation.x") is False
    assert "automation.x" in r._new_entities
