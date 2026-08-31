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
    creds = KalshiCredentials.from_environment()
    universe = await discover_universe(cfg)
    LOG.info("selected %d events / %d markets", len(universe.event_tickers), len(universe.market_tickers))

    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=cfg.queue_max)
    health = HealthState(cfg.state_dir / "recorder_health.json")
    health.selected_events = universe.event_tickers
    health.selected_markets = universe.market_tickers
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
    LOG.info("recorder started")
    await stop.wait()
    LOG.info("shutdown requested")
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    writer.close()
    health.connected = False
    health.write_atomic(queue.qsize())


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
