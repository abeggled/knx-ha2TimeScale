"""Decoding rules — every case here was a real bug or a deliberate decision."""
import pytest
from xknx.dpt import DPTArray, DPTBinary

from knx_source import decode, format_value, raw_hex, transcoder_for


def test_dpt_lookup():
    assert transcoder_for("9.001").__name__ == "DPTTemperature"
    assert transcoder_for("5.001").__name__ == "DPTScaling"
    assert transcoder_for(None) is None
    assert transcoder_for("99.999") is None


def test_main_type_fallback():
    """'16.*' has no transcoder for the main type; 16.000 must be found."""
    assert transcoder_for("16.*").__name__ == "DPTString"


@pytest.mark.parametrize("value,expected", [
    (21997.842, "21997.842"),   # %g would have cut this to 21997.8
    (21.0, "21"),
    (7.8, "7.8"),
    (-0.06, "-0.06"),
    (True, "true"),
    (False, "false"),
    (42, "42"),
])
def test_format_value(value, expected):
    assert format_value(value) == expected


def test_dpt1_sent_as_full_byte():
    """Some devices send DPT 1.x as a byte instead of a 6 bit payload."""
    assert decode("1.011", DPTArray((0x00,))) == ("false", True)
    assert decode("1.011", DPTArray((0x01,))) == ("true", True)


def test_dpt1_enum_becomes_boolean():
    """xknx decodes DPT 1 subtypes to enums; the archive stores booleans."""
    assert decode("1.011", DPTBinary(1)) == ("true", True)
    assert decode("1.001", DPTBinary(0)) == ("false", True)


def test_range_check_falls_back_to_main_type():
    """9.006 is pressure and rejects negatives, but a pressure *tendency*
    is signed — the main type decodes it."""
    assert decode("9.006", DPTArray((0x87, 0xFA))) == ("-0.06", True)


def test_four_byte_float_keeps_full_precision():
    """xknx rounds DPT 14 to seven significant digits; we decode ourselves."""
    assert decode("14.056", DPTArray((0x46, 0xAB, 0xDB, 0xAF))) == ("21997.842", True)


def test_undecodable_falls_back_to_hex():
    value, decoded = decode("219.001", DPTArray((0x05, 0x04, 0x03)))
    assert decoded is False
    assert value.startswith("0x")


def test_raw_hex():
    assert raw_hex(DPTArray((0x0A, 0x0B))) == "0x0a0b"
    assert raw_hex(DPTBinary(1)) == "0x01"
