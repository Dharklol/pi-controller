from __future__ import annotations

import json
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class HealthState:
    state_path: Path
    started_at: str = field(default_factory=utc_now)
    started_mono: float = field(default_factory=time.monotonic, repr=False)
    connected: bool = False
    connection_id: str | None = None
    last_connected_at: str | None = None
    last_message_at: str | None = None
    last_error: str | None = None
    messages_total: int = 0
    reconnects: int = 0
    sequence_gaps: int = 0
    snapshots_requested: int = 0
    queue_high_watermark: int = 0
    selected_events: tuple[str, ...] = ()
    selected_markets: tuple[str, ...] = ()
    message_types: Counter[str] = field(default_factory=Counter)
    current_raw_chunk: str | None = None

    def note_message(self, msg_type: str) -> None:
        self.messages_total += 1
        self.message_types[msg_type or "unknown"] += 1
        self.last_message_at = utc_now()

    def snapshot(self, queue_depth: int) -> dict[str, Any]:
        self.queue_high_watermark = max(self.queue_high_watermark, queue_depth)
        return {
            "schema_version": 1,
            "generated_at": utc_now(),
            "started_at": self.started_at,
            "uptime_seconds": max(0.0, time.monotonic() - self.started_mono),
            "status": "running" if self.connected else "degraded",
            "connected": self.connected,
            "connection_id": self.connection_id,
            "last_connected_at": self.last_connected_at,
            "last_message_at": self.last_message_at,
            "last_error": self.last_error,
            "messages_total": self.messages_total,
            "message_types": dict(self.message_types),
            "reconnects": self.reconnects,
            "sequence_gaps": self.sequence_gaps,
            "snapshots_requested": self.snapshots_requested,
            "queue_depth": queue_depth,
            "queue_high_watermark": self.queue_high_watermark,
            "selected_event_count": len(self.selected_events),
            "selected_market_count": len(self.selected_markets),
            "selected_events": list(self.selected_events),
            "selected_markets": list(self.selected_markets),
            "current_raw_chunk": self.current_raw_chunk,
        }

    def write_atomic(self, queue_depth: int) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        target = self.state_path
        temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        temp.write_text(json.dumps(self.snapshot(queue_depth), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp, target)
