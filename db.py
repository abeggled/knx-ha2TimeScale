"""Shared helpers: database connections and DPT normalisation."""
from __future__ import annotations

import os

import psycopg

KNX_DSN = os.environ.get("KNX_DSN", "dbname=knx_data")
HA_DSN = os.environ.get("HA_DSN", "dbname=ha_data")
POWER_DSN = os.environ.get("POWER_DSN", "dbname=power_data")


def knx_conn(**kw) -> psycopg.Connection:
    return psycopg.connect(KNX_DSN, **kw)


def ha_conn(**kw) -> psycopg.Connection:
    return psycopg.connect(HA_DSN, **kw)


def power_conn(**kw) -> psycopg.Connection:
    return psycopg.connect(POWER_DSN, **kw)


def normalise_dpt(dpt) -> str | None:
    """xknxproject yields {'main': 9, 'sub': 1}; sub may be None.

    9/1   -> '9.001'   (matches the convention already used in knx_measurements)
    16/None -> '16.*'  (main type only, no subtype defined in ETS)
    """
    if not dpt:
        return None
    if isinstance(dpt, str):
        return dpt
    main = dpt.get("main")
    if main is None:
        return None
    sub = dpt.get("sub")
    return f"{main}.{sub:03d}" if sub is not None else f"{main}.*"
