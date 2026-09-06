"""KNX source: listens on the bus and hands rows to the writer.

Decoding needs the DPT from knx_ga. Without one the raw payload is stored as
hex and the address is registered, so the value can be decoded retroactively
once the DPT is known — nothing is silently discarded.
"""
from __future__ import annotations

import datetime as dt
import logging

from xknx import XKNX
from xknx.dpt import DPTArray, DPTBase, DPTBinary
from xknx.io import ConnectionConfig, ConnectionType
from xknx.telegram import Telegram
from xknx.telegram.apci import GroupValueResponse, GroupValueWrite

log = logging.getLogger(__name__)

COLUMNS = ["time", "source", "dpt", "description", "knxvalue",
           "destination", "knxunit"]

_transcoders: dict[str, type | None] = {}


def transcoder_for(dpt: str | None):
    """'9.001' -> DPTTemperature. Main-only entries such as '16.*' fall back
    to the main type, then to subtype 000. Result is cached."""
    if not dpt:
        return None
    if dpt in _transcoders:
        return _transcoders[dpt]
    candidates = [dpt]
    if dpt.endswith(".*"):
        main = dpt[:-2]
        candidates += [main, f"{main}.000", f"{main}.001"]
    found = None
    for cand in candidates:
        try:
            found = DPTBase.parse_transcoder(cand)
        except Exception:  # noqa: BLE001
            found = None
        if found is not None:
            break
    if found is None:
        log.warning("no transcoder for DPT %s — raw values will be stored", dpt)
    _transcoders[dpt] = found
    return found


def format_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{round(value, 3):g}"
    return str(value)


def raw_hex(payload) -> str:
    if isinstance(payload, DPTBinary):
        return f"0x{payload.value:02x}"
    if isinstance(payload, DPTArray):
        return "0x" + payload.value.hex() if isinstance(payload.value, bytes) \
            else "0x" + bytes(payload.value).hex()
    return str(payload)


class KNXSource:
    def __init__(self, registry, writer, include_responses: bool = False) -> None:
        self.registry = registry
        self.writer = writer
        self.include_responses = include_responses
        self.xknx: XKNX | None = None
        self.received = 0
        self.skipped = 0
        self.undecoded = 0

    def _connection_config(self) -> ConnectionConfig:
        s = self.registry.settings
        mode = (s.get("knx.connection") or "tunnel").lower()
        own = s.get("knx.individual_address") or None
        if mode == "routing":
            return ConnectionConfig(
                connection_type=ConnectionType.ROUTING, individual_address=own
            )
        return ConnectionConfig(
            connection_type=ConnectionType.TUNNELING,
            gateway_ip=s.get("knx.gateway_host"),
            gateway_port=int(s.get("knx.gateway_port") or 3671),
            individual_address=own,
            auto_reconnect=True,
        )

    async def _on_telegram(self, telegram: Telegram) -> None:
        payload = telegram.payload
        if not isinstance(payload, GroupValueWrite) and not (
            self.include_responses and isinstance(payload, GroupValueResponse)
        ):
            return

        destination = str(telegram.destination_address)
        self.received += 1
        if not self.registry.knx_archive(destination):
            self.skipped += 1
            return

        dpt = self.registry.dpt_for(destination)
        transcoder = transcoder_for(dpt)
        unit = "unknown"
        if transcoder is not None:
            try:
                value = format_value(transcoder.from_knx(payload.value))
                unit = getattr(transcoder, "unit", None) or "unknown"
            except Exception as exc:  # noqa: BLE001 — wrong DPT, keep the raw value
                log.debug("decode failed for %s (%s): %s", destination, dpt, exc)
                value = raw_hex(payload.value)
                self.undecoded += 1
        else:
            value = raw_hex(payload.value)
            self.undecoded += 1

        self.writer.submit((
            dt.datetime.now(dt.timezone.utc),
            str(telegram.source_address),
            dpt or "",
            self.registry.name_for(destination),
            value,
            destination,
            unit,
        ))

    async def run(self) -> None:
        self.xknx = XKNX(connection_config=self._connection_config())
        self.xknx.telegram_queue.register_telegram_received_cb(self._on_telegram)
        await self.xknx.start()
        log.info("KNX source connected (%s)",
                 self.registry.settings.get("knx.connection"))
        try:
            await self.xknx.stop_event.wait()
        finally:
            await self.xknx.stop()
