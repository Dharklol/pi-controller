from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

import audit_burnin


def envelope(i: int, connection_id: str, payload: dict) -> dict:
    return {
        "schema_version": 1,
        "recv_ts_ns": 1_000_000_000 + i * 1_000_000,
        "recv_mono_ns": 2_000_000_000 + i * 1_000_000,
        "connection_id": connection_id,
        "payload": payload,
    }


def epoch(start: int, connection_id: str) -> tuple[list[dict], int]:
    payloads = [
        {"id": 1, "type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 1}},
        {"id": 2, "type": "subscribed", "msg": {"channel": "trade", "sid": 2}},
        {"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": {"market_ticker": "M1", "yes_dollars_fp": [["0.4", "10"]], "no_dollars_fp": [["0.5", "5"]]}},
        {"type": "orderbook_snapshot", "sid": 1, "seq": 2, "msg": {"market_ticker": "M2", "yes_dollars_fp": [["0.3", "3"]], "no_dollars_fp": [["0.6", "4"]]}},
        {"type": "orderbook_delta", "sid": 1, "seq": 3, "msg": {"market_ticker": "M1", "price_dollars": "0.4", "delta_fp": "-2", "side": "yes"}},
        {"type": "trade", "sid": 2, "msg": {"market_ticker": "M1", "trade_id": f"T-{connection_id}"}},
    ]
    rows = [envelope(start + i, connection_id, p) for i, p in enumerate(payloads)]
    return rows, start + len(rows)


class AuditTest(unittest.TestCase):
    def make_fixture(self, root: Path) -> tuple[Path, Path]:
        raw = root / "raw"
        raw.mkdir()
        rows: list[dict] = []
        n = 0
        first, n = epoch(n, "A")
        second, n = epoch(n, "B")
        rows.extend(first)
        rows.extend(second)
        for seq, part in ((1, rows[:6]), (2, rows[6:])):
            path = raw / f"kalshi_20260901T00000{seq}.000000Z_16910_{seq:06d}.jsonl.gz"
            with gzip.open(path, "wt", encoding="utf-8") as f:
                for row in part:
                    f.write(json.dumps(row) + "\n")
        health = root / "health.json"
        health.write_text(json.dumps({
            "messages_total": 12,
            "message_types": {"subscribed": 4, "orderbook_snapshot": 4, "orderbook_delta": 2, "trade": 2},
            "reconnects": 1,
        }), encoding="utf-8")
        return raw, health

    def test_clean_two_epoch_fixture_passes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            raw, health = self.make_fixture(Path(td))
            report, _ = audit_burnin.audit(raw, 16910, health, 2)
            self.assertEqual(report["result"], "PASS")
            self.assertEqual(report["connections"]["epoch_count"], 2)
            self.assertEqual(report["universe"]["baseline_snapshot_market_count"], 2)

    def test_sequence_gap_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            raw, health = self.make_fixture(Path(td))
            second = raw / "kalshi_20260901T000002.000000Z_16910_000002.jsonl.gz"
            with gzip.open(second, "rt", encoding="utf-8") as f:
                rows = [json.loads(line) for line in f]
            rows[3]["payload"]["seq"] = 4
            with gzip.open(second, "wt", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row) + "\n")
            report, _ = audit_burnin.audit(raw, 16910, health, 2)
            self.assertEqual(report["result"], "FAIL")
            self.assertGreater(report["issues"]["failure_counts"].get("websocket_sequence_discontinuity", 0), 0)


if __name__ == "__main__":
    unittest.main()
