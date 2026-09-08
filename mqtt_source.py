"""MQTT source: subscribes to configured topics and maps JSON payloads to rows.

The mapping lives in the database (mqtt_topic / mqtt_field), not in code: a
topic points at a target table and every field maps a dotted JSON path to a
column with a scale factor. Adding a second device is configuration.

The most recent payload per topic is kept in memory so the UI can show a real
telegram while the mapping is being built.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import pathlib
import ssl
from typing import Any

import aiomqtt
import psycopg

log = logging.getLogger(__name__)

STATE_DIR = pathlib.Path(
    os.environ.get("STATE_DIRECTORY", "/var/lib/homearchive").split(":")[0]
)
PASSWORD_FILE = STATE_DIR / "mqtt.pass"


def broker_password() -> str | None:
    if pw := os.environ.get("MQTT_PASSWORD"):
        return pw
    try:
        return PASSWORD_FILE.read_text().strip() or None
    except OSError:
        return None


def dig(payload: dict, path: str) -> Any:
    """'z.Pi' -> payload['z']['Pi']; missing keys yield None, not an error."""
    node: Any = payload
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def topic_matches(pattern: str, topic: str) -> bool:
    """MQTT wildcards: + one level, # the rest."""
    p, t = pattern.split("/"), topic.split("/")
    for i, part in enumerate(p):
        if part == "#":
            return True
        if i >= len(t):
            return False
        if part != "+" and part != t[i]:
            return False
    return len(p) == len(t)


def parse_time(value, local: bool) -> dt.datetime:
    """Tasmota sends '2026-09-08T09:29:44' without an offset."""
    if not value:
        return dt.datetime.now(dt.UTC)
    try:
        stamp = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return dt.datetime.now(dt.UTC)
    if stamp.tzinfo is not None:
        return stamp
    if local:
        return stamp.astimezone()          # interpret in the host's zone
    return stamp.replace(tzinfo=dt.UTC)


class MQTTSource:
    def __init__(self, registry, dsn_for_db) -> None:
        self.registry = registry
        self.dsn_for_db = dsn_for_db      # name -> DSN
        self.writers: dict[int, Any] = {}  # topic id -> BatchWriter
        self._writer_tasks: list[asyncio.Task] = []
        self.received = 0
        self.mapped = 0
        self.unmatched = 0
        self._topics: list[dict] = []
        self._pending: dict[int, tuple[str, str]] = {}   # topic id -> payload

    # ── configuration ─────────────────────────────────────────────────────
    async def load_mapping(self, dsn: str) -> None:
        async with await psycopg.AsyncConnection.connect(dsn) as conn:
            cur = conn.cursor()
            await cur.execute(
                "SELECT id, topic, target_db, target_table, time_path,"
                " time_is_local FROM mqtt_topic WHERE enabled ORDER BY id")
            topics = []
            for tid, topic, tdb, table, tpath, tlocal in await cur.fetchall():
                await cur.execute(
                    "SELECT json_path, column_name, scale FROM mqtt_field"
                    " WHERE topic_id = %s ORDER BY column_name", (tid,))
                fields = await cur.fetchall()
                topics.append({"id": tid, "topic": topic, "db": tdb,
                               "table": table, "time_path": tpath,
                               "time_local": tlocal, "fields": fields})
        self._topics = topics
        self._build_writers()
        log.info("MQTT mapping: %d topics, %d fields",
                 len(topics), sum(len(t["fields"]) for t in topics))

    def _build_writers(self) -> None:
        """One writer per topic, with the column list fixed by the mapping.
        A field missing from a payload becomes NULL — COPY needs every row to
        have the same shape."""
        from writer import BatchWriter
        settings = self.registry.settings
        for cfg in self._topics:
            columns = ["time"] + [c for _p, c, _s in cfg["fields"]]
            dsn = self.dsn_for_db(cfg["db"])
            if dsn is None:
                log.error("topic %s: unknown database %s", cfg["topic"], cfg["db"])
                continue
            existing = self.writers.get(cfg["id"])
            if existing is not None and existing.columns == columns:
                continue                      # unchanged, keep it running
            writer = BatchWriter(
                dsn, cfg["table"], columns,
                int(settings.get("batch.max_rows", "500")),
                float(settings.get("batch.max_seconds", "2")))
            self.writers[cfg["id"]] = writer
            self._writer_tasks.append(
                asyncio.create_task(writer.run(), name=f"mqtt-writer-{cfg['id']}"))

    # ── message handling ──────────────────────────────────────────────────
    def handle(self, topic: str, payload: bytes) -> None:
        self.received += 1
        try:
            data = json.loads(payload)
        except ValueError:
            self.unmatched += 1
            log.debug("no JSON on %s", topic)
            return
        for cfg in self._topics:
            if not topic_matches(cfg["topic"], topic):
                continue
            writer = self.writers.get(cfg["id"])
            if writer is None:
                continue
            when = parse_time(dig(data, cfg["time_path"]) if cfg["time_path"]
                              else None, cfg["time_local"])
            values = [when]
            for path, _column, scale in cfg["fields"]:
                raw = dig(data, path)
                if isinstance(raw, (int, float)) and not isinstance(raw, bool) \
                        and scale != 1:
                    raw = raw * scale
                values.append(raw)          # None stays None -> NULL
            writer.submit(tuple(values))
            self.mapped += 1
            self._pending[cfg["id"]] = (topic, payload.decode("utf-8", "replace"))
            return
        self.unmatched += 1

    # ── connection ────────────────────────────────────────────────────────
    def _tls_context(self) -> ssl.SSLContext | None:
        s = self.registry.settings
        if (s.get("mqtt.tls") or "false").lower() != "true":
            return None
        ctx = ssl.create_default_context(cafile=s.get("mqtt.ca_cert") or None)
        if (s.get("mqtt.tls_insecure") or "false").lower() == "true":
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            log.warning("MQTT: certificate verification disabled")
        return ctx

    async def run(self, dsn: str) -> None:
        backoff = 5
        while True:
            s = self.registry.settings
            if (s.get("mqtt.enabled") or "false").lower() != "true":
                await asyncio.sleep(30)
                continue
            host = s.get("mqtt.host")
            if not host:
                log.warning("MQTT enabled but mqtt.host is empty")
                await asyncio.sleep(60)
                continue
            try:
                await self.load_mapping(dsn)
                async with aiomqtt.Client(
                    hostname=host,
                    port=int(s.get("mqtt.port") or 1883),
                    username=s.get("mqtt.username") or None,
                    password=broker_password(),
                    identifier=s.get("mqtt.client_id") or "homearchive",
                    tls_context=self._tls_context(),
                ) as client:
                    qos = int(s.get("mqtt.qos") or 0)
                    for cfg in self._topics:
                        await client.subscribe(cfg["topic"], qos=qos)
                        log.info("MQTT subscribed to %s", cfg["topic"])
                    backoff = 5
                    async for message in client.messages:
                        self.handle(str(message.topic), message.payload)
                        if self.received % 20 == 0:
                            await self._publish_samples(dsn)
            except Exception as exc:  # noqa: BLE001 — reconnect on anything
                log.warning("MQTT: %s — retrying in %ds", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)

    async def _publish_samples(self, dsn: str) -> None:
        """Store the newest payload per topic so the UI — a separate process —
        can offer its fields for mapping. Throttled to every 20th message."""
        if not self._pending:
            return
        pending, self._pending = self._pending, {}
        try:
            async with await psycopg.AsyncConnection.connect(
                dsn, autocommit=True
            ) as conn:
                await conn.cursor().executemany(
                    "UPDATE mqtt_topic SET last_seen = now(), last_error = NULL,"
                    " sample_payload = %s::jsonb, sample_at = now(),"
                    " sample_source = 'broker' WHERE id = %s",
                    [(payload, tid) for tid, (_topic, payload) in pending.items()],
                )
        except Exception as exc:  # noqa: BLE001 — never kill the subscription
            log.debug("could not store MQTT samples: %s", exc)
