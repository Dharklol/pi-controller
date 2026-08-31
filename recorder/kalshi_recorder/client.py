from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from typing import Any

import websockets

from .auth import KalshiCredentials
from .config import RecorderConfig
from .health import HealthState, utc_now
from .universe import Universe

LOG = logging.getLogger(__name__)
WS_SIGN_PATH = "/trade-api/ws/v2"


class RecorderClient:
    def __init__(self, cfg: RecorderConfig, creds: KalshiCredentials, universe: Universe, queue: asyncio.Queue[dict[str, Any]], health: HealthState):
        self.cfg = cfg
        self.creds = creds
        self.universe = universe
        self.queue = queue
        self.health = health
        self._command_id = 1
        self._last_seq_by_sid: dict[int, int] = {}
        self._orderbook_sid: int | None = None

    def _next_id(self) -> int:
        value = self._command_id
        self._command_id += 1
        return value

    async def _emit(self, message: dict[str, Any], connection_id: str) -> None:
        record = {
            "schema_version": 1,
            "recv_ts_ns": time.time_ns(),
            "recv_mono_ns": time.monotonic_ns(),
            "connection_id": connection_id,
            "payload": message,
        }
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull as exc:
            self.health.last_error = "raw writer queue overflow"
            raise RuntimeError("raw writer queue overflow; refusing silent data loss") from exc

    async def _subscribe(self, ws: websockets.ClientConnection) -> None:
        market_tickers = list(self.universe.market_tickers)
        for channel in self.cfg.channels:
            msg = {
                "id": self._next_id(),
                "cmd": "subscribe",
                "params": {"channels": [channel], "market_tickers": market_tickers},
            }
            await ws.send(json.dumps(msg))

    async def _request_snapshot(self, ws: websockets.ClientConnection, ticker: str) -> None:
        if self._orderbook_sid is None:
            LOG.warning("sequence gap for %s before orderbook subscription sid was known", ticker)
            return
        command = {
            "id": self._next_id(),
            "cmd": "update_subscription",
            "params": {
                "sid": self._orderbook_sid,
                "market_tickers": [ticker],
                "action": "get_snapshot",
            },
        }
        await ws.send(json.dumps(command))
        self.health.snapshots_requested += 1
        LOG.warning("requested fresh snapshot after sequence gap: %s", ticker)

    async def _handle_message(self, ws: websockets.ClientConnection, data: dict[str, Any], connection_id: str) -> None:
        await self._emit(data, connection_id)
        msg_type = str(data.get("type") or "unknown")
        self.health.note_message(msg_type)

        if msg_type == "subscribed":
            msg = data.get("msg", {})
            if msg.get("channel") == "orderbook_delta" and isinstance(msg.get("sid"), int):
                self._orderbook_sid = msg["sid"]
            return

        sid = data.get("sid")
        seq = data.get("seq")
        if isinstance(sid, int) and isinstance(seq, int):
            prior = self._last_seq_by_sid.get(sid)
            if prior is not None and seq != prior + 1:
                self.health.sequence_gaps += 1
                ticker = str(data.get("msg", {}).get("market_ticker") or "")
                LOG.error("sequence gap sid=%s prior=%s current=%s ticker=%s", sid, prior, seq, ticker)
                if ticker and sid == self._orderbook_sid:
                    await self._request_snapshot(ws, ticker)
            self._last_seq_by_sid[sid] = seq

        if msg_type == "error":
            raise RuntimeError(f"Kalshi websocket error: {data.get('msg')!r}")

    async def run_forever(self) -> None:
        delay = self.cfg.reconnect_min_seconds
        first = True
        while True:
            if not first:
                self.health.reconnects += 1
            first = False
            connection_id = uuid.uuid4().hex
            self._last_seq_by_sid.clear()
            self._orderbook_sid = None
            headers = self.creds.headers("GET", WS_SIGN_PATH)
            try:
                LOG.info("connecting to %s for %d markets", self.cfg.ws_url, len(self.universe.market_tickers))
                async with websockets.connect(
                    self.cfg.ws_url,
                    additional_headers=headers,
                    ping_interval=self.cfg.ws_ping_interval_seconds,
                    ping_timeout=self.cfg.ws_ping_timeout_seconds,
                    max_queue=4096,
                    close_timeout=10,
                ) as ws:
                    self.health.connected = True
                    self.health.connection_id = connection_id
                    self.health.last_connected_at = utc_now()
                    self.health.last_error = None
                    delay = self.cfg.reconnect_min_seconds
                    await self._subscribe(ws)
                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            await self._emit({"type": "decode_error", "raw": str(raw)}, connection_id)
                            raise
                        await self._handle_message(ws, data, connection_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.health.connected = False
                self.health.last_error = f"{type(exc).__name__}: {exc}"
                LOG.exception("websocket session failed")
                sleep_for = min(self.cfg.reconnect_max_seconds, delay) * random.uniform(0.8, 1.2)
                await asyncio.sleep(sleep_for)
                delay = min(self.cfg.reconnect_max_seconds, max(self.cfg.reconnect_min_seconds, delay * 2))
            finally:
                self.health.connected = False
