"""Home Assistant source: websocket subscription on state_changed."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os

import aiohttp

log = logging.getLogger(__name__)

COLUMNS = ["time", "sensorid", "havalue"]


class HASource:
    def __init__(self, registry, writer) -> None:
        self.registry = registry
        self.writer = writer
        self.received = 0
        self.skipped = 0

    def _token(self) -> str:
        env_name = self.registry.settings.get("ha.token_env") or "HA_TOKEN"
        token = os.environ.get(env_name)
        if not token:
            raise RuntimeError(
                f"Home Assistant token missing: environment variable {env_name} is unset"
            )
        return token

    def _handle(self, message: dict) -> None:
        event = message.get("event") or {}
        data = event.get("data") or {}
        new_state = data.get("new_state")
        if not new_state:
            return                                   # entity removed
        entity_id = data.get("entity_id") or new_state.get("entity_id")
        if not entity_id:
            return

        self.received += 1
        if not self.registry.ha_archive(entity_id):
            self.skipped += 1
            return

        when = new_state.get("last_updated") or event.get("time_fired")
        try:
            ts = dt.datetime.fromisoformat(when)
        except (TypeError, ValueError):
            ts = dt.datetime.now(dt.UTC)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.UTC)

        self.writer.submit((ts, entity_id, new_state.get("state")))

    async def _session(self) -> None:
        url = self.registry.settings.get("ha.websocket_url")
        if not url:
            raise RuntimeError("setting ha.websocket_url is empty")
        async with (aiohttp.ClientSession() as session,
                    session.ws_connect(url, heartbeat=30) as ws):
                await ws.receive_json()              # auth_required
                await ws.send_json({"type": "auth", "access_token": self._token()})
                auth = await ws.receive_json()
                if auth.get("type") != "auth_ok":
                    raise RuntimeError(f"Home Assistant rejected the token: {auth}")
                await ws.send_json(
                    {"id": 1, "type": "subscribe_events",
                     "event_type": "state_changed"}
                )
                log.info("HA source subscribed to state_changed")
                async for msg in ws:
                    if msg.type is aiohttp.WSMsgType.TEXT:
                        payload = json.loads(msg.data)
                        if payload.get("type") == "event":
                            self._handle(payload)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                      aiohttp.WSMsgType.ERROR):
                        break
        raise ConnectionError("Home Assistant websocket closed")

    async def run(self) -> None:
        backoff = 5
        while True:
            try:
                await self._session()
            except Exception as exc:  # noqa: BLE001
                log.warning("HA source: %s — reconnecting in %ds", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)
