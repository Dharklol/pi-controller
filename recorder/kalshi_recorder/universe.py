from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from .config import RecorderConfig
from .health import HealthState

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Universe:
    event_tickers: tuple[str, ...]
    market_tickers: tuple[str, ...]


def _number(value: object) -> Decimal:
    try:
        number = Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal(0)
    return number if number.is_finite() else Decimal(0)


def _cache_path(cfg: RecorderConfig) -> Path:
    return cfg.state_dir / "universe_cache.json"


def _cache_signature(cfg: RecorderConfig) -> dict[str, object]:
    return {
        "environment": cfg.environment,
        "min_siblings": cfg.discovery.min_siblings,
        "max_events": cfg.discovery.max_events,
        "max_markets": cfg.discovery.max_markets,
        "min_event_volume_24h": cfg.discovery.min_event_volume_24h,
    }


def _load_cached_universe(cfg: RecorderConfig) -> tuple[Universe, float] | None:
    path = _cache_path(cfg)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1:
            return None
        if payload.get("signature") != _cache_signature(cfg):
            return None
        generated_unix = float(payload["generated_unix"])
        age = max(0.0, time.time() - generated_unix)
        event_tickers = tuple(str(x) for x in payload.get("event_tickers", []) if x)
        market_tickers = tuple(str(x) for x in payload.get("market_tickers", []) if x)
        if not market_tickers:
            return None
        return Universe(event_tickers, market_tickers), age
    except (FileNotFoundError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _write_cached_universe(cfg: RecorderConfig, universe: Universe) -> None:
    path = _cache_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "generated_unix": time.time(),
        "signature": _cache_signature(cfg),
        "event_tickers": list(universe.event_tickers),
        "market_tickers": list(universe.market_tickers),
    }
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


async def _get_page(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, object],
    *,
    attempts: int = 4,
) -> dict[str, object]:
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            response = await client.get(url, params=params)
            if response.status_code == 429 or response.status_code >= 500:
                response.raise_for_status()
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("Kalshi markets response was not a JSON object")
            return payload
        except (httpx.TransportError, httpx.HTTPStatusError, ValueError, RuntimeError) as exc:
            last_exc = exc
            if attempt + 1 >= attempts:
                break
            delay = min(8.0, 0.75 * (2**attempt))
            LOG.warning("market discovery page failed (%s); retrying in %.2fs", exc, delay)
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise RuntimeError(f"market discovery request failed after {attempts} attempts: {last_exc}") from last_exc


async def discover_universe(cfg: RecorderConfig, health: HealthState | None = None) -> Universe:
    """Discover sibling-rich events, reusing a fresh selection across restarts."""
    if cfg.market_tickers:
        return Universe(event_tickers=(), market_tickers=cfg.market_tickers)
    if not cfg.discovery.enabled:
        raise RuntimeError("no market_tickers configured and discovery is disabled")

    cached = _load_cached_universe(cfg)
    if cached is not None:
        universe, age = cached
        if age <= cfg.discovery.cache_ttl_seconds:
            if health is not None:
                health.phase = "using_cached_universe"
                health.write_atomic(0)
            LOG.info(
                "using cached universe age_seconds=%.1f selected_events=%d selected_markets=%d",
                age,
                len(universe.event_tickers),
                len(universe.market_tickers),
            )
            return universe
        LOG.info(
            "universe cache expired age_seconds=%.1f ttl_seconds=%.1f; refreshing",
            age,
            cfg.discovery.cache_ttl_seconds,
        )

    by_event: dict[str, list[dict[str, object]]] = defaultdict(list)
    cursor = ""
    seen_cursors: set[str] = set()
    page_number = 0
    total_markets = 0

    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "kalshi-recorder/0.1"}) as client:
        while True:
            page_number += 1
            if page_number > 1000:
                raise RuntimeError("market discovery exceeded 1000 pages; refusing runaway pagination")

            params: dict[str, object] = {
                "status": "open",
                "mve_filter": "exclude",
                "limit": 1000,
            }
            if cursor:
                params["cursor"] = cursor

            payload = await _get_page(client, f"{cfg.rest_base_url}/markets", params)
            markets = payload.get("markets", [])
            if not isinstance(markets, list):
                raise RuntimeError("Kalshi markets response contained a non-list 'markets' field")

            for market in markets:
                if not isinstance(market, dict):
                    continue
                event_ticker = str(market.get("event_ticker") or "")
                ticker = str(market.get("ticker") or "")
                if not event_ticker or not ticker:
                    continue
                by_event[event_ticker].append(market)

            total_markets += len(markets)
            if health is not None:
                health.phase = "discovering"
                health.discovery_pages = page_number
                health.discovery_markets_seen = total_markets
                health.discovery_events_seen = len(by_event)
                health.write_atomic(0)

            LOG.info(
                "discovery page=%d markets_this_page=%d markets_total=%d events_seen=%d",
                page_number,
                len(markets),
                total_markets,
                len(by_event),
            )

            next_cursor = str(payload.get("cursor") or "")
            if not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise RuntimeError("market discovery received a repeated pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    candidates: list[tuple[Decimal, str, list[str]]] = []
    for event_ticker, markets in by_event.items():
        if len(markets) < cfg.discovery.min_siblings:
            continue
        score = sum((_number(m.get("volume_24h_fp")) for m in markets), Decimal(0))
        if score < Decimal(str(cfg.discovery.min_event_volume_24h)):
            continue
        markets.sort(key=lambda m: _number(m.get("volume_24h_fp")), reverse=True)
        tickers = [str(m["ticker"]) for m in markets if m.get("ticker")]
        if len(tickers) >= cfg.discovery.min_siblings:
            candidates.append((score, event_ticker, tickers))

    candidates.sort(key=lambda row: (row[0], row[1]), reverse=True)
    selected_events: list[str] = []
    selected_markets: list[str] = []
    for _, event_ticker, tickers in candidates[: cfg.discovery.max_events]:
        if len(selected_markets) >= cfg.discovery.max_markets:
            break
        room = cfg.discovery.max_markets - len(selected_markets)
        chosen = tickers[:room]
        if len(chosen) < cfg.discovery.min_siblings:
            continue
        selected_events.append(event_ticker)
        selected_markets.extend(chosen)

    if not selected_markets:
        sibling_counts = sorted((len(markets) for markets in by_event.values()), reverse=True)
        largest = sibling_counts[:10]
        raise RuntimeError(
            "discovery returned no sibling-rich open markets "
            f"(markets_seen={total_markets}, events_seen={len(by_event)}, "
            f"min_siblings={cfg.discovery.min_siblings}, largest_event_sizes={largest})"
        )

    universe = Universe(tuple(selected_events), tuple(selected_markets))
    _write_cached_universe(cfg, universe)
    LOG.info(
        "discovery complete pages=%d markets_seen=%d events_seen=%d candidates=%d selected_events=%d selected_markets=%d",
        page_number,
        total_markets,
        len(by_event),
        len(candidates),
        len(selected_events),
        len(selected_markets),
    )
    return universe
