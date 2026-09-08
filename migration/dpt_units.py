"""dpt -> knxunit mapping.

PRIMARY: reconstructed from the live convention already present in
knx_measurements (majority non-empty unit per dpt).
FALLBACK: KNX standard units for dpts that no longer occur in the live data.
Anything else -> 'unknown' (logged).
"""

# Reconstructed from the target table (authoritative — matches production).
DPT_UNITS = {
    "1.001": "unknown", "1.002": "unknown", "1.003": "unknown", "1.005": "unknown",
    "1.007": "unknown", "1.008": "unknown", "1.011": "unknown", "1.017": "unknown",
    "1.019": "unknown", "3.007": "unknown", "12.001": "unknown", "16.001": "unknown",
    "18.001": "unknown", "20.105": "unknown", "237.600": "unknown",
    "5.001": "%", "5.003": "\u00b0", "5.010": "pulses",
    "6.001": "%",
    "7.001": "pulses", "7.006": "minutes", "7.007": "hours",
    "9.001": "\u00b0C", "9.002": "\u00b0C", "9.004": "lux", "9.005": "m/s",
    "9.006": "Pa", "9.007": "%", "9.008": "ppm", "9.021": "mA",
    "9.022": "W/m\u00b2", "9.025": "l/h",
    "13.001": "pulses",
    "14.019": "A", "14.027": "V", "14.033": "Hz", "14.056": "W",
    "14.058": "Pa", "14.076": "m3",
}

# KNX standard fallback for dpts absent from the live data.
DPT_UNITS_FALLBACK = {
    "5.004": "%", "5.005": "ratio", "5.006": "tariff",
    "6.010": "counter pulses",
    "7.002": "ms", "7.003": "ms", "7.004": "ms", "7.005": "s",
    "7.011": "mm", "7.012": "mA", "7.013": "lux",
    "8.001": "pulses", "8.002": "ms", "8.003": "ms", "8.004": "ms",
    "8.005": "s", "8.010": "%", "8.011": "\u00b0",
    "9.003": "ms", "9.010": "s", "9.011": "ms", "9.020": "mV",
    "9.023": "K/%", "9.024": "kW", "9.026": "l/h", "9.027": "\u00b0F",
    "9.028": "km/h", "9.029": "g/m3", "9.030": "\u00b5g/m3",
    "12.100": "s", "12.101": "min", "12.102": "h",
    "13.002": "m3/h", "13.010": "Wh", "13.011": "VAh", "13.012": "VARh",
    "13.013": "kWh", "13.014": "kVAh", "13.015": "kVARh", "13.100": "s",
    "14.000": "m/s2", "14.007": "\u00b0", "14.017": "kg/m3", "14.031": "J",
    "14.032": "N", "14.037": "J", "14.038": "\u03a9", "14.039": "m",
    "14.051": "kg", "14.055": "\u00b0", "14.057": "cos\u03c6", "14.065": "m/s",
    "14.068": "\u00b0C", "14.069": "K", "14.070": "K", "14.077": "V",
    "14.078": "N", "14.080": "VA",
}


def unit_for(dpt: str | None) -> tuple[str, bool]:
    """Return (unit, was_mapped). Unmapped dpts get 'unknown' and are flagged."""
    if dpt is None:
        return "unknown", False
    d = dpt.strip()
    if d in DPT_UNITS:
        return DPT_UNITS[d], True
    if d in DPT_UNITS_FALLBACK:
        return DPT_UNITS_FALLBACK[d], True
    return "unknown", False
