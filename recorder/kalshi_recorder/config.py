from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DiscoveryConfig:
    enabled: bool
    min_siblings: int
    max_events: int
    max_markets: int
    min_event_volume_24h: float


@dataclass(frozen=True)
class RecorderConfig:
    environment: str
    market_tickers: tuple[str, ...]
    channels: tuple[str, ...]
    discovery: DiscoveryConfig
    raw_dir: Path
    state_dir: Path
    logs_dir: Path
    chunk_seconds: int
    chunk_max_bytes: int
    health_interval_seconds: float
    queue_max: int
    reconnect_min_seconds: float
    reconnect_max_seconds: float
    ws_ping_interval_seconds: float
    ws_ping_timeout_seconds: float

    @property
    def ws_url(self) -> str:
        if self.environment == "production":
            return "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
        if self.environment == "demo":
            return "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
        raise ValueError(f"unsupported environment: {self.environment}")

    @property
    def rest_base_url(self) -> str:
        if self.environment == "production":
            return "https://external-api.kalshi.com/trade-api/v2"
        if self.environment == "demo":
            return "https://external-api.demo.kalshi.co/trade-api/v2"
        raise ValueError(f"unsupported environment: {self.environment}")


def _require_positive(name: str, value: float) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be > 0")


def load_config(path: str | Path | None = None) -> RecorderConfig:
    config_path = Path(path or os.environ.get("KALSHI_RECORDER_CONFIG", "/srv/kalshi/recorder/recorder/config.json"))
    raw: dict[str, Any] = json.loads(config_path.read_text(encoding="utf-8"))
    discovery_raw = raw.get("discovery", {})
    discovery = DiscoveryConfig(
        enabled=bool(discovery_raw.get("enabled", True)),
        min_siblings=int(discovery_raw.get("min_siblings", 3)),
        max_events=int(discovery_raw.get("max_events", 30)),
        max_markets=int(discovery_raw.get("max_markets", 250)),
        min_event_volume_24h=float(discovery_raw.get("min_event_volume_24h", 0.0)),
    )
    cfg = RecorderConfig(
        environment=str(raw.get("environment", "production")),
        market_tickers=tuple(dict.fromkeys(str(x) for x in raw.get("market_tickers", []))),
        channels=tuple(dict.fromkeys(str(x) for x in raw.get("channels", ["orderbook_delta", "trade"]))),
        discovery=discovery,
        raw_dir=Path(raw.get("raw_dir", "/srv/kalshi/data/raw")),
        state_dir=Path(raw.get("state_dir", "/srv/kalshi/state")),
        logs_dir=Path(raw.get("logs_dir", "/srv/kalshi/logs")),
        chunk_seconds=int(raw.get("chunk_seconds", 300)),
        chunk_max_bytes=int(raw.get("chunk_max_bytes", 64 * 1024 * 1024)),
        health_interval_seconds=float(raw.get("health_interval_seconds", 5)),
        queue_max=int(raw.get("queue_max", 100000)),
        reconnect_min_seconds=float(raw.get("reconnect_min_seconds", 1.0)),
        reconnect_max_seconds=float(raw.get("reconnect_max_seconds", 30.0)),
        ws_ping_interval_seconds=float(raw.get("ws_ping_interval_seconds", 20)),
        ws_ping_timeout_seconds=float(raw.get("ws_ping_timeout_seconds", 20)),
    )
    if cfg.environment not in {"production", "demo"}:
        raise ValueError("environment must be production or demo")
    if not cfg.channels:
        raise ValueError("at least one channel is required")
    if "orderbook_delta" not in cfg.channels:
        raise ValueError("v0 requires orderbook_delta so book state and gap recovery are recorded")
    _require_positive("chunk_seconds", cfg.chunk_seconds)
    _require_positive("chunk_max_bytes", cfg.chunk_max_bytes)
    _require_positive("health_interval_seconds", cfg.health_interval_seconds)
    _require_positive("queue_max", cfg.queue_max)
    if discovery.min_siblings < 2:
        raise ValueError("discovery.min_siblings must be >= 2")
    if discovery.max_events < 1 or discovery.max_markets < 1:
        raise ValueError("discovery caps must be >= 1")
    return cfg
