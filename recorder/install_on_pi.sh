#!/bin/bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash recorder/install_on_pi.sh" >&2
  exit 1
fi

ROOT=/srv/kalshi/recorder
APP=$ROOT/recorder
VENV=$ROOT/.venv
SECRETS=/etc/kalshi-recorder

if [[ ! -f "$APP/requirements.txt" ]]; then
  echo "Expected recorder checkout at $ROOT; requirements missing." >&2
  exit 2
fi

python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$APP/requirements.txt"

if [[ ! -f "$APP/config.json" ]]; then
  install -o kalshi-mcp -g kalshi-mcp -m 0644 "$APP/config.example.json" "$APP/config.json"
  echo "Created $APP/config.json from example."
fi

install -d -o root -g kalshi-mcp -m 0750 "$SECRETS"
if [[ ! -f "$SECRETS/kalshi.env" ]]; then
  install -o root -g kalshi-mcp -m 0640 /dev/null "$SECRETS/kalshi.env"
fi

install -o root -g root -m 0644 "$APP/systemd/kalshi-recorder.service" /etc/systemd/system/kalshi-recorder.service
systemctl daemon-reload
systemctl enable kalshi-recorder.service

cat <<'MSG'
Recorder software and service unit are installed, but the service was NOT started.

Before starting it, create/store the Kalshi production credentials locally:

  /etc/kalshi-recorder/kalshi.env
    KALSHI_API_KEY_ID=YOUR_KEY_ID
    KALSHI_PRIVATE_KEY_PATH=/etc/kalshi-recorder/kalshi_private.key

  /etc/kalshi-recorder/kalshi_private.key
    your downloaded Kalshi RSA private key

Then enforce:
  sudo chown root:kalshi-mcp /etc/kalshi-recorder/kalshi.env /etc/kalshi-recorder/kalshi_private.key
  sudo chmod 0640 /etc/kalshi-recorder/kalshi.env /etc/kalshi-recorder/kalshi_private.key

Do not paste the private key into ChatGPT or commit it.

Also verify /srv/kalshi/data is on the intended SSD before beginning the 72-hour burn-in.
MSG
