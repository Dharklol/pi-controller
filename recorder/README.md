# Kalshi live recorder v0

A deliberately boring first-stage recorder for the F → liquidity → repricing → C research program.

## What v0 records

- Authenticated Kalshi WebSocket session.
- `orderbook_delta` snapshots and deltas.
- Public `trade` messages.
- Local wall-clock receive time (`recv_ts_ns`) and monotonic receive time (`recv_mono_ns`) for every message.
- WebSocket connection ID for reconnect boundaries.
- Sequence-gap counts and automatic `get_snapshot` requests for orderbook gaps.
- Atomic `/srv/kalshi/state/recorder_health.json` status.
- Immutable gzip JSONL chunks under `/srv/kalshi/data/raw/YYYY/MM/DD/`.

This version intentionally does **not** implement F/C labeling or derived liquidity features yet. The first priority is preserving replayable raw evidence without silent loss. F/C annotations will be added only after the capture path survives burn-in.

## Universe

Two modes are supported:

1. Explicit `market_tickers` in `config.json`.
2. Automatic discovery when that list is empty. Discovery fetches open Kalshi events with nested markets, requires at least `min_siblings` open markets in the event, ranks events by summed 24-hour volume, and caps selected events/markets.

The discovery ranking is an engineering sampling rule, **not** a research signal and must not be treated as part of F/C logic.

## Secrets

Never commit Kalshi credentials. The service expects:

`/etc/kalshi-recorder/kalshi.env`

```text
KALSHI_API_KEY_ID=your-key-id
KALSHI_PRIVATE_KEY_PATH=/etc/kalshi-recorder/kalshi_private.key
```

The private key belongs at the path above. Recommended ownership/permissions for the current Pi layout:

```bash
sudo chown root:kalshi-mcp /etc/kalshi-recorder/kalshi.env /etc/kalshi-recorder/kalshi_private.key
sudo chmod 0640 /etc/kalshi-recorder/kalshi.env /etc/kalshi-recorder/kalshi_private.key
```

Do not paste the key into ChatGPT.

## Pi layout

The current controller clones the repository to `/srv/kalshi/recorder`. In the controller monorepo bootstrap, the recorder package lives in the nested `recorder/` directory, so the service uses:

```text
/srv/kalshi/recorder/recorder/
```

## Initial install after clone

```bash
cd /srv/kalshi/recorder
python3 -m venv .venv
.venv/bin/pip install -r recorder/requirements.txt
cp recorder/config.example.json recorder/config.json
```

Install the unit once through the human/admin path:

```bash
sudo install -m 0644 recorder/systemd/kalshi-recorder.service /etc/systemd/system/kalshi-recorder.service
sudo systemctl daemon-reload
sudo systemctl enable kalshi-recorder.service
```

Do not start the service until credentials and `config.json` are present.

## Burn-in acceptance criteria

The first 72 hours are engineering validation, not hypothesis testing. At minimum review:

- reconnect count and downtime,
- sequence gaps and snapshot recoveries,
- queue high-water mark (zero overflows),
- raw chunk rotation and disk growth,
- last-message freshness,
- selected event/market counts,
- malformed/error messages,
- system clock/chrony health separately at the OS level.

Any silent-loss condition invalidates the burn-in window.
