from __future__ import annotations

import gzip
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RawChunkWriter:
    """Append-only gzip JSONL writer. Closed chunks are never reopened."""

    def __init__(self, raw_dir: Path, chunk_seconds: int, chunk_max_bytes: int):
        self.raw_dir = raw_dir
        self.chunk_seconds = chunk_seconds
        self.chunk_max_bytes = chunk_max_bytes
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self._file: gzip.GzipFile | None = None
        self._path: Path | None = None
        self._opened_mono = 0.0
        self._uncompressed_bytes = 0
        self._sequence = 0

    @property
    def current_path(self) -> str | None:
        return str(self._path) if self._path else None

    def _open(self) -> None:
        now = datetime.now(timezone.utc)
        day_dir = self.raw_dir / now.strftime("%Y/%m/%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        stamp = now.strftime("%Y%m%dT%H%M%S.%fZ")
        self._sequence += 1
        self._path = day_dir / f"kalshi_{stamp}_{os.getpid()}_{self._sequence:06d}.jsonl.gz"
        self._file = gzip.open(self._path, mode="xb", compresslevel=3)
        self._opened_mono = time.monotonic()
        self._uncompressed_bytes = 0

    def _should_rotate(self, next_len: int) -> bool:
        if self._file is None:
            return True
        if time.monotonic() - self._opened_mono >= self.chunk_seconds:
            return True
        return self._uncompressed_bytes + next_len > self.chunk_max_bytes

    def write(self, record: dict[str, Any]) -> None:
        line = (json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        if self._should_rotate(len(line)):
            self.close()
            self._open()
        assert self._file is not None
        self._file.write(line)
        self._uncompressed_bytes += len(line)

    def flush(self, durable: bool = False) -> None:
        if self._file is not None:
            self._file.flush()
            if durable and self._file.fileobj is not None:
                self._file.fileobj.flush()
                os.fsync(self._file.fileobj.fileno())

    def close(self) -> None:
        if self._file is not None:
            self._file.flush()
            self._file.close()
            self._file = None
            self._path = None
