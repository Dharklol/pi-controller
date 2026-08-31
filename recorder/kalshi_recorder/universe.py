from __future__ import annotations

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
    if cfg.market_tickers:
        return Universe(event_tickers=(), market_tickers=cfg.market_tickers)
    if not cfg.discovery.enabled:
        raise RuntimeError("no market_tickers configured and discovery is disabled")

    candidates: list[tuple[Decimal, str, list[str]]] = []
    cursor = ""
    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "kalshi-recorder/0.1"}) as client:
        while True:
            params: dict[str, object] = {
                "status": "open",
                "with_nested_markets": "true",
                "limit": 200,
            }
            if cursor:
                params["cursor"] = cursor
            response = await client.get(f"{cfg.rest_base_url}/events", params=params)
            response.raise_for_status()
            payload = response.json()
            for event in payload.get("events", []):
                open_markets = [m for m in event.get("markets", []) if m.get("status") == "open"]
                if len(open_markets) < cfg.discovery.min_siblings:
                    continue
                score = sum((_number(m.get("volume_24h_fp")) for m in open_markets), Decimal(0))
                if score < Decimal(str(cfg.discovery.min_event_volume_24h)):
                    continue
                tickers = [str(m["ticker"]) for m in open_markets if m.get("ticker")]
                if len(tickers) >= cfg.discovery.min_siblings:
                    candidates.append((score, str(event.get("event_ticker", "")), tickers))
            cursor = str(payload.get("cursor") or "")
            if not cursor:
                break

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
        raise RuntimeError("discovery returned no sibling-rich open markets")
    return Universe(tuple(selected_events), tuple(selected_markets))
