from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

FILE_RE = re.compile(r"^kalshi_(?P<stamp>\d{8}T\d{6}\.\d{6}Z)_(?P<pid>\d+)_(?P<seq>\d{6})\.jsonl\.gz$")
BOOK_TYPES = {"orderbook_snapshot", "orderbook_delta"}


def dec(value: object) -> Decimal | None:
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return out if out.is_finite() else None


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def atomic_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def issue(store: dict[str, Any], severity: str, code: str, **detail: Any) -> None:
    counts = store[f"{severity}_counts"]
    samples = store[f"{severity}_samples"]
    counts[code] = counts.get(code, 0) + 1
    bucket = samples.setdefault(code, [])
    if len(bucket) < 20:
        bucket.append(detail)


def find_chunks(raw_root: Path, pid: int, issues: dict[str, Any]) -> list[tuple[int, datetime, Path]]:
    rows: list[tuple[int, datetime, Path]] = []
    for path in raw_root.rglob("*.jsonl.gz"):
        m = FILE_RE.match(path.name)
        if not m or int(m.group("pid")) != pid:
            continue
        try:
            stamp = datetime.strptime(m.group("stamp"), "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc)
        except ValueError:
            issue(issues, "failure", "filename_timestamp_invalid", path=str(path))
            continue
        rows.append((int(m.group("seq")), stamp, path))
    rows.sort(key=lambda x: x[0])
    if not rows:
        issue(issues, "failure", "no_chunks_found", raw_root=str(raw_root), pid=pid)
        return []

    seqs = [x[0] for x in rows]
    missing = sorted(set(range(seqs[0], seqs[-1] + 1)) - set(seqs))
    duplicates = sorted(k for k, v in Counter(seqs).items() if v > 1)
    if missing:
        issue(issues, "failure", "chunk_sequence_missing", count=len(missing), sample=missing[:100])
    if duplicates:
        issue(issues, "failure", "chunk_sequence_duplicate", count=len(duplicates), sample=duplicates[:100])
    if seqs[0] != 1:
        issue(issues, "warning", "chunk_sequence_does_not_start_at_one", first=seqs[0])
    for prior, cur in zip(rows, rows[1:]):
        if cur[1] < prior[1]:
            issue(issues, "failure", "chunk_filename_time_regression", prior=str(prior[2]), current=str(cur[2]))
    return rows


def snapshot_levels(msg: dict[str, Any], side: str) -> list[Any] | None:
    for key in (f"{side}_dollars_fp", side):
        value = msg.get(key)
        if isinstance(value, list):
            return value
    return None


def set_snapshot(books: dict[str, dict[str, dict[Decimal, Decimal]]], ticker: str, msg: dict[str, Any], issues: dict[str, Any], ctx: dict[str, Any]) -> None:
    book: dict[str, dict[Decimal, Decimal]] = {"yes": {}, "no": {}}
    for side in ("yes", "no"):
        levels = snapshot_levels(msg, side)
        if levels is None:
            issue(issues, "failure", "snapshot_levels_missing", ticker=ticker, side=side, **ctx)
            continue
        for i, level in enumerate(levels):
            if not isinstance(level, (list, tuple)) or len(level) < 2:
                issue(issues, "failure", "snapshot_level_malformed", ticker=ticker, side=side, level=i, **ctx)
                continue
            price, qty = dec(level[0]), dec(level[1])
            if price is None or qty is None:
                issue(issues, "failure", "snapshot_number_invalid", ticker=ticker, side=side, level=i, **ctx)
                continue
            if qty < 0:
                issue(issues, "failure", "snapshot_negative_quantity", ticker=ticker, side=side, price=str(price), quantity=str(qty), **ctx)
                continue
            if qty:
                book[side][price] = qty
    books[ticker] = book


def apply_delta(books: dict[str, dict[str, dict[Decimal, Decimal]]], ticker: str, msg: dict[str, Any], issues: dict[str, Any], ctx: dict[str, Any]) -> None:
    side = str(msg.get("side") or "")
    price = dec(msg.get("price_dollars", msg.get("price")))
    delta = dec(msg.get("delta_fp", msg.get("delta")))
    if side not in {"yes", "no"}:
        issue(issues, "failure", "delta_side_invalid", ticker=ticker, side=side, **ctx)
        return
    if price is None or delta is None:
        issue(issues, "failure", "delta_number_invalid", ticker=ticker, side=side, **ctx)
        return
    if ticker not in books:
        issue(issues, "failure", "delta_before_snapshot", ticker=ticker, **ctx)
        return
    levels = books[ticker][side]
    prior = levels.get(price, Decimal(0))
    result = prior + delta
    if result < 0:
        issue(issues, "failure", "book_negative_after_delta", ticker=ticker, side=side, price=str(price), prior=str(prior), delta=str(delta), result=str(result), **ctx)
        return
    if result == 0:
        levels.pop(price, None)
    else:
        levels[price] = result


def new_epoch(connection_id: str, index: int) -> dict[str, Any]:
    return {
        "index": index,
        "connection_id": connection_id,
        "records": 0,
        "message_types": Counter(),
        "subscribed_channels": {},
        "snapshot_markets": set(),
        "delta_markets": set(),
        "trade_markets": set(),
        "first_book_type": {},
        "first_recv_ts_ns": None,
        "last_recv_ts_ns": None,
        "first_recv_mono_ns": None,
        "last_recv_mono_ns": None,
    }


def epoch_json(e: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": e["index"],
        "connection_id": e["connection_id"],
        "records": e["records"],
        "message_types": dict(e["message_types"]),
        "subscribed_channels": e["subscribed_channels"],
        "snapshot_market_count": len(e["snapshot_markets"]),
        "delta_market_count": len(e["delta_markets"]),
        "trade_market_count": len(e["trade_markets"]),
        "snapshot_markets": sorted(e["snapshot_markets"]),
        "first_recv_ts_ns": e["first_recv_ts_ns"],
        "last_recv_ts_ns": e["last_recv_ts_ns"],
        "first_recv_mono_ns": e["first_recv_mono_ns"],
        "last_recv_mono_ns": e["last_recv_mono_ns"],
    }


def load_health(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def audit(raw_root: Path, pid: int, health_path: Path | None, expected_markets: int | None) -> tuple[dict[str, Any], dict[str, Any]]:
    start = time.monotonic()
    issues: dict[str, Any] = {"failure_counts": {}, "warning_counts": {}, "failure_samples": {}, "warning_samples": {}}
    chunks = find_chunks(raw_root, pid, issues)
    health = load_health(health_path)
    if health_path is not None and health is None:
        issue(issues, "warning", "health_file_unreadable", path=str(health_path))

    total = 0
    type_counts: Counter[str] = Counter()
    market_counts: dict[str, Counter[str]] = defaultdict(Counter)
    epochs: list[dict[str, Any]] = []
    by_connection: dict[str, dict[str, Any]] = {}
    closed_connections: set[str] = set()
    current_connection: str | None = None
    seq_last: dict[tuple[str, int], int] = {}
    books: dict[str, dict[str, dict[Decimal, Decimal]]] = {}
    trade_ids: set[str] = set()
    duplicate_trade_ids = 0
    prior_mono: int | None = None
    prior_wall: int | None = None
    prior_ctx: dict[str, Any] | None = None
    max_gap: dict[str, Any] = {"seconds": 0.0}
    first_recv: int | None = None
    last_recv: int | None = None
    manifest_files: list[dict[str, Any]] = []

    for chunk_seq, stamp, path in chunks:
        st = path.stat()
        file_info: dict[str, Any] = {
            "sequence": chunk_seq,
            "path": str(path),
            "filename_timestamp": stamp.isoformat(),
            "compressed_bytes": st.st_size,
            "sha256": sha256(path),
            "records": 0,
            "first_recv_ts_ns": None,
            "last_recv_ts_ns": None,
        }
        line_no = 0
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                for line_no, line in enumerate(f, 1):
                    ctx = {"chunk_sequence": chunk_seq, "path": str(path), "line": line_no}
                    if not line.strip():
                        issue(issues, "failure", "blank_jsonl_line", **ctx)
                        continue
                    try:
                        env = json.loads(line)
                    except json.JSONDecodeError as exc:
                        issue(issues, "failure", "json_decode_error", error=str(exc), **ctx)
                        continue
                    if not isinstance(env, dict):
                        issue(issues, "failure", "envelope_not_object", **ctx)
                        continue

                    total += 1
                    file_info["records"] += 1
                    recv_ts = env.get("recv_ts_ns")
                    recv_mono = env.get("recv_mono_ns")
                    connection_id = env.get("connection_id")
                    payload = env.get("payload")
                    if env.get("schema_version") != 1:
                        issue(issues, "failure", "envelope_schema_version", value=env.get("schema_version"), **ctx)
                    if not isinstance(recv_ts, int):
                        issue(issues, "failure", "recv_ts_invalid", value=recv_ts, **ctx)
                        recv_ts = None
                    if not isinstance(recv_mono, int):
                        issue(issues, "failure", "recv_mono_invalid", value=recv_mono, **ctx)
                        recv_mono = None
                    if not isinstance(connection_id, str) or not connection_id:
                        issue(issues, "failure", "connection_id_invalid", value=connection_id, **ctx)
                        connection_id = "<invalid>"
                    if not isinstance(payload, dict):
                        issue(issues, "failure", "payload_not_object", **ctx)
                        continue

                    msg_type = str(payload.get("type") or "unknown")
                    msg = payload.get("msg")
                    ticker = str(msg.get("market_ticker") or "") if isinstance(msg, dict) else ""
                    type_counts[msg_type] += 1
                    if ticker:
                        market_counts[ticker][msg_type] += 1

                    if current_connection != connection_id:
                        if current_connection is not None:
                            closed_connections.add(current_connection)
                        if connection_id in closed_connections:
                            issue(issues, "failure", "connection_id_reappeared_noncontiguously", connection_id=connection_id, **ctx)
                        if connection_id not in by_connection:
                            e = new_epoch(connection_id, len(epochs) + 1)
                            epochs.append(e)
                            by_connection[connection_id] = e
                        current_connection = connection_id
                        books = {}
                    e = by_connection[connection_id]
                    e["records"] += 1
                    e["message_types"][msg_type] += 1

                    if isinstance(recv_ts, int):
                        file_info["first_recv_ts_ns"] = file_info["first_recv_ts_ns"] or recv_ts
                        file_info["last_recv_ts_ns"] = recv_ts
                        first_recv = first_recv or recv_ts
                        last_recv = recv_ts
                        e["first_recv_ts_ns"] = e["first_recv_ts_ns"] or recv_ts
                        e["last_recv_ts_ns"] = recv_ts
                        if prior_wall is not None and recv_ts < prior_wall:
                            issue(issues, "warning", "wall_clock_regression", prior=prior_wall, current=recv_ts, **ctx)
                        prior_wall = recv_ts

                    if isinstance(recv_mono, int):
                        e["first_recv_mono_ns"] = e["first_recv_mono_ns"] or recv_mono
                        e["last_recv_mono_ns"] = recv_mono
                        if prior_mono is not None:
                            if recv_mono < prior_mono:
                                issue(issues, "failure", "monotonic_time_regression", prior=prior_mono, current=recv_mono, **ctx)
                            else:
                                gap = (recv_mono - prior_mono) / 1e9
                                if gap > max_gap["seconds"]:
                                    max_gap = {"seconds": gap, "before": prior_ctx, "after": {"connection_id": connection_id, "type": msg_type, "market_ticker": ticker or None, **ctx}}
                        prior_mono = recv_mono
                    prior_ctx = {"connection_id": connection_id, "type": msg_type, "market_ticker": ticker or None, **ctx}

                    if msg_type == "subscribed":
                        if isinstance(msg, dict) and isinstance(msg.get("sid"), int) and msg.get("channel"):
                            e["subscribed_channels"][str(msg["channel"])] = int(msg["sid"])
                        else:
                            issue(issues, "failure", "subscribed_message_malformed", **ctx)

                    sid, seq = payload.get("sid"), payload.get("seq")
                    if isinstance(sid, int) and isinstance(seq, int):
                        key = (connection_id, sid)
                        if key in seq_last and seq != seq_last[key] + 1:
                            issue(issues, "failure", "websocket_sequence_discontinuity", connection_id=connection_id, sid=sid, prior=seq_last[key], current=seq, type=msg_type, ticker=ticker or None, **ctx)
                        seq_last[key] = seq

                    if msg_type in BOOK_TYPES:
                        if not isinstance(msg, dict) or not ticker:
                            issue(issues, "failure", "book_message_malformed", type=msg_type, **ctx)
                        else:
                            e["first_book_type"].setdefault(ticker, msg_type)
                            if msg_type == "orderbook_snapshot":
                                e["snapshot_markets"].add(ticker)
                                set_snapshot(books, ticker, msg, issues, ctx)
                            else:
                                e["delta_markets"].add(ticker)
                                apply_delta(books, ticker, msg, issues, ctx)
                    elif msg_type == "trade":
                        if ticker:
                            e["trade_markets"].add(ticker)
                        if isinstance(msg, dict) and msg.get("trade_id"):
                            tid = str(msg["trade_id"])
                            if tid in trade_ids:
                                duplicate_trade_ids += 1
                                issue(issues, "warning", "duplicate_trade_id", trade_id=tid, **ctx)
                            else:
                                trade_ids.add(tid)
                    elif msg_type not in {"subscribed", "error", "ok", "decode_error"}:
                        issue(issues, "warning", "unknown_message_type", type=msg_type, **ctx)
        except (OSError, EOFError, UnicodeDecodeError, gzip.BadGzipFile) as exc:
            issue(issues, "failure", "gzip_or_utf8_error", error=f"{type(exc).__name__}: {exc}", path=str(path), line=line_no)
        manifest_files.append(file_info)

    baseline = set(epochs[0]["snapshot_markets"]) if epochs else set()
    if epochs and not baseline:
        issue(issues, "failure", "first_epoch_has_no_snapshots", connection_id=epochs[0]["connection_id"])
    if expected_markets is not None and len(baseline) != expected_markets:
        issue(issues, "failure", "baseline_market_count_mismatch", expected=expected_markets, actual=len(baseline))

    for e in epochs:
        for channel in ("orderbook_delta", "trade"):
            if channel not in e["subscribed_channels"]:
                issue(issues, "failure", "missing_subscription_ack", epoch=e["index"], connection_id=e["connection_id"], channel=channel)
        if baseline:
            missing = sorted(baseline - e["snapshot_markets"])
            extra = sorted(e["snapshot_markets"] - baseline)
            if missing:
                issue(issues, "failure", "epoch_missing_market_snapshots", epoch=e["index"], count=len(missing), markets=missing[:100])
            if extra:
                issue(issues, "warning", "epoch_extra_snapshot_markets", epoch=e["index"], count=len(extra), markets=extra[:100])
            bad_first = sorted(k for k, v in e["first_book_type"].items() if v != "orderbook_snapshot")
            if bad_first:
                issue(issues, "failure", "epoch_first_book_message_not_snapshot", epoch=e["index"], count=len(bad_first), markets=bad_first[:100])

    if health is not None:
        if isinstance(health.get("messages_total"), int) and health["messages_total"] != total:
            issue(issues, "failure", "health_total_mismatch", health=health["messages_total"], raw=total)
        if isinstance(health.get("message_types"), dict):
            htypes = {str(k): int(v) for k, v in health["message_types"].items() if isinstance(v, int)}
            if htypes != dict(type_counts):
                issue(issues, "failure", "health_message_type_counts_mismatch", health=htypes, raw=dict(type_counts))
        if isinstance(health.get("reconnects"), int) and len(epochs) != health["reconnects"] + 1:
            issue(issues, "failure", "health_reconnect_epoch_mismatch", reconnects=health["reconnects"], epochs=len(epochs))

    boundaries = []
    for a, b in zip(epochs, epochs[1:]):
        gap = None
        if a["last_recv_mono_ns"] is not None and b["first_recv_mono_ns"] is not None:
            gap = (b["first_recv_mono_ns"] - a["last_recv_mono_ns"]) / 1e9
        boundaries.append({"from_epoch": a["index"], "to_epoch": b["index"], "receive_gap_seconds": gap, "next_epoch_snapshot_market_count": len(b["snapshot_markets"]), "next_epoch_subscribed_channels": b["subscribed_channels"]})

    per_market = {ticker: {"total": sum(c.values()), "message_types": dict(c)} for ticker, c in sorted(market_counts.items())}
    report = {
        "audit_schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "result": "FAIL" if issues["failure_counts"] else "PASS",
        "source": {"raw_root": str(raw_root), "pid": pid, "health_path": str(health_path) if health_path else None},
        "runtime_seconds": time.monotonic() - start,
        "chunks": {"count": len(chunks), "first_sequence": chunks[0][0] if chunks else None, "last_sequence": chunks[-1][0] if chunks else None, "compressed_bytes": sum(x[2].stat().st_size for x in chunks)},
        "records": {"total": total, "message_types": dict(type_counts), "first_recv_ts_ns": first_recv, "last_recv_ts_ns": last_recv, "duration_seconds": (last_recv - first_recv) / 1e9 if first_recv is not None and last_recv is not None else None, "max_inter_record_gap": max_gap},
        "connections": {"epoch_count": len(epochs), "reconnect_boundaries": max(0, len(epochs) - 1), "epochs": [epoch_json(e) for e in epochs], "boundaries": boundaries},
        "universe": {"baseline_snapshot_market_count": len(baseline), "baseline_snapshot_markets": sorted(baseline), "all_observed_market_count": len(per_market), "markets": per_market},
        "trades": {"unique_trade_ids": len(trade_ids), "duplicate_trade_ids": duplicate_trade_ids},
        "issues": issues,
        "health_snapshot": health,
    }
    manifest = {"manifest_schema_version": 1, "generated_at": report["generated_at"], "raw_root": str(raw_root), "pid": pid, "files": manifest_files}
    return report, manifest


def main() -> int:
    p = argparse.ArgumentParser(description="Read-only integrity/replay audit for Kalshi recorder raw gzip JSONL chunks.")
    p.add_argument("--raw-root", type=Path, default=Path("/srv/kalshi/data/kalshi-recorder/raw"))
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--health", type=Path, default=Path("/srv/kalshi/state/recorder_health.json"))
    p.add_argument("--expected-markets", type=int)
    p.add_argument("--output-dir", type=Path, default=Path("/srv/kalshi/state/audits"))
    a = p.parse_args()
    report, manifest = audit(a.raw_root, a.pid, a.health, a.expected_markets)
    report_path = a.output_dir / f"burnin_audit_pid{a.pid}.json"
    manifest_path = a.output_dir / f"burnin_manifest_pid{a.pid}.json"
    atomic_json(report_path, report)
    atomic_json(manifest_path, manifest)
    print(f"BURN-IN RAW AUDIT: {report['result']}")
    print(f"chunks={report['chunks']['count']} records={report['records']['total']} epochs={report['connections']['epoch_count']}")
    print("message_types=" + json.dumps(report["records"]["message_types"], sort_keys=True))
    print(f"baseline_markets={report['universe']['baseline_snapshot_market_count']} unique_trade_ids={report['trades']['unique_trade_ids']} duplicate_trade_ids={report['trades']['duplicate_trade_ids']}")
    print("failure_counts=" + json.dumps(report["issues"]["failure_counts"], sort_keys=True))
    print("warning_counts=" + json.dumps(report["issues"]["warning_counts"], sort_keys=True))
    print(f"report={report_path}")
    print(f"manifest={manifest_path}")
    return 1 if report["result"] == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
