from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

from .auth import KalshiCredentials
from .client import RecorderClient
from .config import load_config
from .health import HealthState
from .universe import discover_universe
from .writer import RawChunkWriter

LOG = logging.getLogger(__name__)


def configure_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


async def writer_loop(queue: asyncio.Queue[dict], writer: RawChunkWriter, health: HealthState) -> None:
    try:
        while True:
            try:
                record = await asyncio.wait_for(queue.get(), timeout=5.0)
            except TimeoutError:
                writer.flush(durable=True)
                continue
            try:
                writer.write(record)
                health.current_raw_chunk = writer.current_path
            finally:
                queue.task_done()
            if queue.empty():
                writer.flush(durable=False)
    finally:
        writer.flush(durable=True)
        writer.close()


async def health_loop(queue: asyncio.Queue[dict], health: HealthState, interval: float) -> None:
    while True:
        health.write_atomic(queue.qsize())
        await asyncio.sleep(interval)


async def run() -> None:
    cfg = load_config()
    configure_logging(cfg.logs_dir)

    health = HealthState(cfg.state_dir / "recorder_health.json")
    health.phase = "starting"
    health.write_atomic(0)

    queue: asyncio.Queue[dict] | None = None
    writer: RawChunkWriter | None = None
    tasks: list[asyncio.Task[object]] = []

    try:
        health.phase = "loading_credentials"
        health.write_atomic(0)
        creds = KalshiCredentials.from_environment()

        health.phase = "discovering"
        health.write_atomic(0)
        universe = await discover_universe(cfg, health)
        LOG.info("selected %d events / %d markets", len(universe.event_tickers), len(universe.market_tickers))

        queue = asyncio.Queue(maxsize=cfg.queue_max)
        health.selected_events = universe.event_tickers
        health.selected_markets = universe.market_tickers
        health.last_error = None
        health.phase = "initializing_writer"
        health.write_atomic(queue.qsize())

        writer = RawChunkWriter(cfg.raw_dir, cfg.chunk_seconds, cfg.chunk_max_bytes)
        client = RecorderClient(cfg, creds, universe, queue, health)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

        tasks = [
            asyncio.create_task(writer_loop(queue, writer, health), name="writer"),
            asyncio.create_task(health_loop(queue, health, cfg.health_interval_seconds), name="health"),
            asyncio.create_task(client.run_forever(), name="websocket"),
        ]
        stop_task = asyncio.create_task(stop.wait(), name="shutdown-signal")
        health.phase = "connecting"
        health.write_atomic(queue.qsize())
        LOG.info("recorder started")

        done, _ = await asyncio.wait([stop_task, *tasks], return_when=asyncio.FIRST_COMPLETED)
        if stop_task in done:
            LOG.info("shutdown requested")
        else:
            for task in done:
                if task is stop_task:
                    continue
                if task.cancelled():
                    raise RuntimeError(f"worker task {task.get_name()} was cancelled unexpectedly")
                exc = task.exception()
                if exc is not None:
                    raise RuntimeError(f"worker task {task.get_name()} failed: {type(exc).__name__}: {exc}") from exc
                raise RuntimeError(f"worker task {task.get_name()} exited unexpectedly")
    except Exception as exc:
        health.connected = False
        health.phase = "failed"
        health.last_error = f"{type(exc).__name__}: {exc}"
        health.write_atomic(queue.qsize() if queue is not None else 0)
        LOG.exception("recorder failed")
        raise
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if writer is not None:
            writer.close()
        if health.phase != "failed":
            health.connected = False
            health.phase = "stopped"
            health.write_atomic(queue.qsize() if queue is not None else 0)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
