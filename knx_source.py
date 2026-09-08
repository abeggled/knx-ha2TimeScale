"""KNX source: listens on the bus and hands rows to the writer.

Decoding needs the DPT from knx_ga. Without one the raw payload is stored as
hex and the address is registered, so the value can be decoded retroactively
once the DPT is known — nothing is silently discarded.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import enum
import logging
import os
import struct

from xknx import XKNX
from xknx.dpt import DPTArray, DPTBase, DPTBinary
from xknx.io import ConnectionConfig, ConnectionType, SecureConfig
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
    if isinstance(value, enum.Enum):
        # DPT 1.x subtypes decode to enums (State.ACTIVE, ...). The archive
        # stores plain booleans, so keep that.
        value = value.value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        # Three decimals, trailing zeros trimmed — matches the archive.
        # (%g would cap at six significant digits and truncate large
        # meter readings: 21997.842 -> 21997.8)
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value)


def raw_hex(payload) -> str:
    if isinstance(payload, DPTBinary):
        return f"0x{payload.value:02x}"
    if isinstance(payload, DPTArray):
        return "0x" + payload.value.hex() if isinstance(payload.value, bytes) \
            else "0x" + bytes(payload.value).hex()
    return str(payload)


def decode_4byte_float(payload) -> float | None:
    """xknx rounds DPT 14.x to seven significant digits to match the ETS
    group monitor. At meter-reading magnitudes that drops the third decimal,
    which is genuinely present in the float32 — and which the previous
    collector wrote for three and a half years. Decode it ourselves to keep
    the series continuous.
    """
    if not isinstance(payload, DPTArray) or len(payload.value) != 4:
        return None
    try:
        return struct.unpack(">f", bytes(payload.value))[0]
    except struct.error:
        return None


def decode(dpt: str | None, payload) -> tuple[str, bool]:
    """Return (value, decoded). Two lenient fallbacks reproduce what the
    previous collector accepted, without weakening the primary path:

    * DPT 1.x sent as a full byte instead of a 6-bit payload — some devices
      do this; the least significant bit carries the state.
    * Subtypes with a range check rejecting a legitimate value, e.g. a
      pressure *tendency* on DPT 9.006, which is signed in practice. The
      main type (DPT 9) decodes it without the range restriction.
    """
    transcoder = transcoder_for(dpt)

    if (dpt or "").split(".")[0] == "14":
        raw = decode_4byte_float(payload)
        if raw is not None:
            return format_value(raw), True

    if transcoder is not None:
        try:
            return format_value(transcoder.from_knx(payload)), True
        except Exception:  # noqa: BLE001 — fall through to the lenient paths
            pass

    main = (dpt or "").split(".")[0]

    if main == "1" and isinstance(payload, DPTArray) and len(payload.value) == 1:
        return ("true" if payload.value[0] & 1 else "false"), True

    if main:
        base = transcoder_for(main)
        if base is not None and base is not transcoder:
            try:
                return format_value(base.from_knx(payload)), True
            except Exception:  # noqa: BLE001
                pass

    return raw_hex(payload), False


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

        if mode in ("tunnel_secure", "routing_secure"):
            # Keys come from the ETS keyring export. Its password is a secret
            # and therefore lives in the environment, not in settings.
            import keyring_store
            keyfile = s.get("knx.keyring_path") or str(keyring_store.KEYRING_FILE)
            keypass = keyring_store.keyring_password()
            if not os.path.exists(keyfile):
                raise RuntimeError(
                    f"keyring file not found: {keyfile} — upload it in the UI"
                )
            if not keypass:
                raise RuntimeError(
                    "keyring password missing — upload the keyring in the UI "
                    "or set KNX_KEYRING_PASSWORD"
                )
            secure = SecureConfig(
                knxkeys_file_path=keyfile,
                knxkeys_password=keypass,
                user_id=int(s["knx.secure_user_id"])
                if s.get("knx.secure_user_id") else None,
            )
            if mode == "routing_secure":
                return ConnectionConfig(
                    connection_type=ConnectionType.ROUTING_SECURE,
                    individual_address=own,
                    secure_config=secure,
                )
            return ConnectionConfig(
                connection_type=ConnectionType.TUNNELING_TCP_SECURE,
                gateway_ip=s.get("knx.gateway_host"),
                gateway_port=int(s.get("knx.gateway_port") or 3671),
                individual_address=own,
                secure_config=secure,
                auto_reconnect=True,
            )

        if mode == "routing":
            return ConnectionConfig(
                connection_type=ConnectionType.ROUTING, individual_address=own
            )
        if mode == "tunnel_tcp":
            return ConnectionConfig(
                connection_type=ConnectionType.TUNNELING_TCP,
                gateway_ip=s.get("knx.gateway_host"),
                gateway_port=int(s.get("knx.gateway_port") or 3671),
                individual_address=own,
                auto_reconnect=True,
            )
        return ConnectionConfig(
            connection_type=ConnectionType.TUNNELING,
            gateway_ip=s.get("knx.gateway_host"),
            gateway_port=int(s.get("knx.gateway_port") or 3671),
            individual_address=own,
            auto_reconnect=True,
        )

    def _on_telegram(self, telegram: Telegram) -> None:
        """Called synchronously by xknx's TelegramQueue — must not be a
        coroutine (xknx invokes it without awaiting). submit() never blocks."""
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
        value, decoded = decode(dpt, payload.value)
        if not decoded:
            self.undecoded += 1
            log.debug("no decode for %s (dpt %s), storing raw", destination, dpt)
        transcoder = transcoder_for(dpt)
        unit = self.registry.unit_for(dpt, getattr(transcoder, "unit", None))

        self.writer.submit((
            dt.datetime.now(dt.UTC),
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
            await asyncio.Event().wait()      # run until cancelled
        finally:
            await self.xknx.stop()
