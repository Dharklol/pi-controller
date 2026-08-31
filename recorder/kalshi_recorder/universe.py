from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import httpx

from .config import RecorderConfig


@dataclass(frozen=True)
class Universe:
    event_tickers: tuple[str, ...]
    market_tickers: tuple[str, ...]


def _number(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal(0)


async def discover_universe(cfg: RecorderConfig) -> Universe:
    """Discover sibling-rich events from the canonical open-markets feed.

    We deliberately group open markets by their event_ticker instead of relying
    on nested market objects returned by /events. That keeps discovery tied to
    the actual tradable market universe and avoids assumptions about how nested
    event payloads represent market lifecycle state.
    """
    if cfg.market_tickers:
        return Universe(event_tickers=(), market_tickers=cfg.market_tickers)
    if not cfg.discovery.enabled:
        raise RuntimeError("no market_tickers configured and discovery is disabled")

    by_event: dict[str, list[dict[str, object]]] = defaultdict(list)
    cursor = ""
    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "kalshi-recorder/0.1"}) as client:
        while True:
            params: dict[str, object] = {
                "status": "open",
                "mve_filter": "exclude",
                "limit": 1000,
            }
            if cursor:
                params["cursor"] = cursor
            response = await client.get(f"{cfg.rest_base_url}/markets", params=params)
            response.raise_for_status()
            payload = response.json()
            for market in payload.get("markets", []):
                event_ticker = str(market.get("event_ticker") or "")
                ticker = str(market.get("ticker") or "")
                if not event_ticker or not ticker:
                    continue
                by_event[event_ticker].append(market)
            cursor = str(payload.get("cursor") or "")
            if not cursor:
                break

    candidates: list[tuple[Decimal, str, list[str]]] = []
    for event_ticker, markets in by_event.items():
        if len(markets) < cfg.discovery.min_siblings:
            continue
        score = sum((_number(m.get("volume_24h_fp")) for m in markets), Decimal(0))
        if score < Decimal(str(cfg.discovery.min_event_volume_24h)):
            continue
        # Highest-activity siblings first in case max_markets truncates an event.
        markets.sort(key=lambda m: _number(m.get("volume_24h_fp")), reverse=True)
        tickers = [str(m["ticker"]) for m in markets if m.get("ticker")]
        if len(tickers) >= cfg.discovery.min_siblings:
            candidates.append((score, event_ticker, tickers))

    candidates.sort(key=lambda row: row[0], reverse=True)
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
        raise RuntimeError(
            "discovery returned no sibling-rich open markets "
            f"(open events seen={len(by_event)}, min_siblings={cfg.discovery.min_siblings})"
        )
    return Universe(tuple(selected_events), tuple(selected_markets))
