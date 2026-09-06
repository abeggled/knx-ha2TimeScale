"""Shared helpers: database connections and DPT normalisation."""
from __future__ import annotations

import os

import psycopg

KNX_DSN = os.environ.get(
    "KNX_DSN", "host=iqsrv36.a38.ch dbname=knx_data user=postgres"
)
HA_DSN = os.environ.get(
    "HA_DSN", "host=iqsrv36.a38.ch dbname=ha_data user=postgres"
)


def knx_conn(**kw) -> psycopg.Connection:
    return psycopg.connect(KNX_DSN, **kw)


def ha_conn(**kw) -> psycopg.Connection:
    return psycopg.connect(HA_DSN, **kw)


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
