"""DPT normalisation, password hashing and the DPT description in the UI."""
import pytest

from db import normalise_dpt


@pytest.mark.parametrize("raw,expected", [
    ({"main": 9, "sub": 1}, "9.001"),
    ({"main": 14, "sub": 76}, "14.076"),
    ({"main": 16, "sub": None}, "16.*"),
    ({"main": 237, "sub": 600}, "237.600"),
    ("9.001", "9.001"),
    (None, None),
])
def test_normalise_dpt(raw, expected):
    assert normalise_dpt(raw) == expected


def test_password_roundtrip():
    import ui
    stored = ui.hash_password("korrektes Passwort")
    assert ui.verify_password("korrektes Passwort", stored)
    assert not ui.verify_password("falsches Passwort", stored)


def test_malformed_hash_denies_access():
    import ui
    assert not ui.verify_password("egal", "kaputt")
    assert not ui.verify_password("egal", "")


def test_describe_dpt():
    import ui
    text, unit = ui.describe_dpt("9.001")
    assert "DPTTemperature" in text and unit == "°C"
    text, unit = ui.describe_dpt("99.999")
    assert text.startswith("unbekannt") and unit is None
    text, _ = ui.describe_dpt(None)
    assert "kein DPT" in text
