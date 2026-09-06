"""Batched writer: buffers rows and flushes them with COPY.

One writer per target table. Flushing happens when the buffer reaches
max_rows or max_seconds have passed, whichever comes first. Values are
passed as tuples — never interpolated into SQL.
"""
from __future__ import annotations

import asyncio
import logging

import psycopg

log = logging.getLogger(__name__)


class BatchWriter:
    def __init__(
        self,
        dsn: str,
        table: str,
        columns: list[str],
        max_rows: int = 500,
        max_seconds: float = 2.0,
    ) -> None:
        self.dsn = dsn
        self.table = table
        self.columns = columns
        self.max_rows = max_rows
        self.max_seconds = max_seconds
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=100_000)
        self._conn: psycopg.AsyncConnection | None = None
        self.written = 0
        self.dropped = 0

    def submit(self, row: tuple) -> None:
        """Non-blocking. Drops the row if the queue is full rather than
        stalling the KNX/HA reader — a stalled reader loses more."""
        try:
            self._queue.put_nowait(row)
        except asyncio.QueueFull:
            self.dropped += 1
            if self.dropped % 1000 == 1:
                log.error("%s: queue full, dropped %d rows", self.table, self.dropped)

    async def _connect(self) -> psycopg.AsyncConnection:
        if self._conn is None or self._conn.closed:
            self._conn = await psycopg.AsyncConnection.connect(
                self.dsn, autocommit=True
            )
            log.info("%s: database connection established", self.table)
        return self._conn

    async def _flush(self, rows: list[tuple]) -> None:
        cols = ", ".join(self.columns)
        stmt = f"COPY {self.table} ({cols}) FROM STDIN"
        while True:
            try:
                conn = await self._connect()
                async with conn.cursor() as cur:
                    async with cur.copy(stmt) as cp:
                        for row in rows:
                            await cp.write_row(row)
                self.written += len(rows)
                return
            except Exception as exc:  # noqa: BLE001 — retry on any DB error
                log.warning("%s: flush failed (%s), retrying in 5s", self.table, exc)
                if self._conn is not None:
                    await self._conn.close()
                    self._conn = None
                await asyncio.sleep(5)

    async def run(self) -> None:
        buf: list[tuple] = []
        while True:
            try:
                row = await asyncio.wait_for(self._queue.get(), self.max_seconds)
                buf.append(row)
            except TimeoutError:
                pass
            if buf and (len(buf) >= self.max_rows or self._queue.empty()):
                batch, buf = buf, []
                await self._flush(batch)
