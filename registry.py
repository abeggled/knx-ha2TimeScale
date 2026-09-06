"""Registry: settings, group address metadata and archiving decisions.

Everything is read from the database and reloaded periodically, so changes
take effect without a restart. Newly seen addresses and entities are
registered automatically instead of being dropped.
"""
from __future__ import annotations

import asyncio
import fnmatch
import logging
from dataclasses import dataclass

import psycopg

log = logging.getLogger(__name__)


@dataclass
class GA:
    name: str | None
    dpt: str | None
    archive: bool


class Registry:
    def __init__(self, knx_dsn: str, ha_dsn: str) -> None:
        self.knx_dsn = knx_dsn
        self.ha_dsn = ha_dsn
        self.settings: dict[str, str] = {}
        self.ga: dict[str, GA] = {}
        self.ha_entities: dict[str, bool] = {}          # entity_id -> archive
        self.units: dict[str, str] = {}                 # dpt -> stored unit
        self.knx_patterns: list[str] = []
        self.ha_patterns: list[str] = []
        self._new_ga: set[str] = set()
        self._new_entities: set[str] = set()

    # ── loading ───────────────────────────────────────────────────────────
    async def load(self) -> None:
        async with await psycopg.AsyncConnection.connect(self.knx_dsn) as conn:
            cur = conn.cursor()
            await cur.execute("SELECT key, value FROM settings")
            self.settings = {k: v for k, v in await cur.fetchall()}

            await cur.execute("SELECT address, name, dpt, archive FROM knx_ga")
            self.ga = {
                a: GA(name=n, dpt=d, archive=ar) for a, n, d, ar in await cur.fetchall()
            }

            await cur.execute("SELECT dpt, unit FROM knx_dpt_unit")
            self.units = {d: u for d, u in await cur.fetchall()}

            await cur.execute(
                "SELECT kind, pattern FROM archive_exclude_pattern"
            )
            self.knx_patterns, self.ha_patterns = [], []
            for kind, pattern in await cur.fetchall():
                (self.knx_patterns if kind == "knx" else self.ha_patterns).append(
                    pattern
                )

        async with await psycopg.AsyncConnection.connect(self.ha_dsn) as conn:
            cur = conn.cursor()
            await cur.execute("SELECT entity_id, archive FROM ha_entity")
            self.ha_entities = {e: a for e, a in await cur.fetchall()}

        log.info(
            "registry: %d group addresses, %d HA entities, "
            "%d KNX / %d HA exclusion patterns",
            len(self.ga), len(self.ha_entities),
            len(self.knx_patterns), len(self.ha_patterns),
        )

    async def reload_loop(self) -> None:
        interval = int(self.settings.get("ga_reload_seconds", "600"))
        while True:
            await asyncio.sleep(interval)
            try:
                await self._register_new()
                await self.load()
            except Exception as exc:  # noqa: BLE001
                log.warning("registry reload failed: %s", exc)

    # ── decisions ─────────────────────────────────────────────────────────
    @staticmethod
    def _matches(value: str, patterns: list[str]) -> bool:
        # Patterns use SQL LIKE syntax in the table; '%' maps to fnmatch '*'.
        return any(fnmatch.fnmatch(value, p.replace("%", "*")) for p in patterns)

    def knx_archive(self, address: str) -> bool:
        if self._matches(address, self.knx_patterns):
            return False
        entry = self.ga.get(address)
        if entry is None:
            self._new_ga.add(address)      # unknown: archive, register later
            return True
        return entry.archive

    def ha_archive(self, entity_id: str) -> bool:
        if self._matches(entity_id, self.ha_patterns):
            return False
        known = self.ha_entities.get(entity_id)
        if known is None:
            self._new_entities.add(entity_id)
            return True
        return known

    def unit_for(self, dpt: str | None, fallback: str | None) -> str:
        """Stored convention wins over the transcoder symbol."""
        if dpt and dpt in self.units:
            return self.units[dpt]
        return fallback or "unknown"

    def dpt_for(self, address: str) -> str | None:
        entry = self.ga.get(address)
        return entry.dpt if entry else None

    def name_for(self, address: str) -> str | None:
        entry = self.ga.get(address)
        return entry.name if entry else None

    # ── registration of newly seen items ──────────────────────────────────
    async def _register_new(self) -> None:
        if self._new_ga:
            addresses, self._new_ga = list(self._new_ga), set()
            async with await psycopg.AsyncConnection.connect(
                self.knx_dsn, autocommit=True
            ) as conn:
                await conn.cursor().executemany(
                    """INSERT INTO knx_ga (address, origin, last_seen)
                       VALUES (%s, 'auto', now())
                       ON CONFLICT (address) DO UPDATE SET last_seen = now()""",
                    [(a,) for a in addresses],
                )
            log.info("registered %d new group addresses", len(addresses))

        if self._new_entities:
            entities, self._new_entities = list(self._new_entities), set()
            async with await psycopg.AsyncConnection.connect(
                self.ha_dsn, autocommit=True
            ) as conn:
                await conn.cursor().executemany(
                    """INSERT INTO ha_entity (entity_id, last_seen)
                       VALUES (%s, now())
                       ON CONFLICT (entity_id) DO UPDATE SET last_seen = now()""",
                    [(e,) for e in entities],
                )
            log.info("registered %d new HA entities", len(entities))
