#!/bin/bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo ./setup_tunnel.sh tunnel_..." >&2
  exit 1
fi

if [[ $# -ne 1 ]]; then
  echo "Usage: sudo ./setup_tunnel.sh tunnel_0123456789abcdef0123456789abcdef" >&2
  exit 1
fi

TUNNEL_ID="$1"
ENV_FILE="/etc/pi-controller/tunnel.env"

if ! command -v tunnel-client >/dev/null 2>&1; then
  echo "tunnel-client is not installed at /usr/local/bin/tunnel-client or PATH." >&2
  echo "Download the current Linux ARM64 build from OpenAI Platform tunnel settings." >&2
  exit 2
fi

if [[ ! -f "$ENV_FILE" ]]; then
  install -o root -g root -m 0600 /dev/null "$ENV_FILE"
  cat >&2 <<EOF
Created $ENV_FILE.

Open it with:
  sudo nano $ENV_FILE

Add exactly:
  CONTROL_PLANE_API_KEY=YOUR_RUNTIME_KEY

Do not put the key in GitHub or ChatGPT.
Then rerun:
  sudo ./setup_tunnel.sh $TUNNEL_ID
EOF
  exit 3
fi

chmod 0600 "$ENV_FILE"
chown root:root "$ENV_FILE"

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

if [[ -z "${CONTROL_PLANE_API_KEY:-}" ]]; then
  echo "$ENV_FILE does not define CONTROL_PLANE_API_KEY." >&2
  exit 4
fi

sudo -u kalshi-mcp \
  env \
  HOME=/var/lib/pi-controller \
  CONTROL_PLANE_API_KEY="$CONTROL_PLANE_API_KEY" \
  tunnel-client init \
    --profile kalshi-pi \
    --tunnel-id "$TUNNEL_ID" \
    --mcp-server-url http://127.0.0.1:8765/mcp

sudo -u kalshi-mcp \
  env \
  HOME=/var/lib/pi-controller \
  CONTROL_PLANE_API_KEY="$CONTROL_PLANE_API_KEY" \
  tunnel-client doctor \
    --profile kalshi-pi \
    --explain

systemctl enable --now openai-kalshi-tunnel.service

echo
echo "Tunnel service enabled."
echo "Check with:"
echo "  sudo systemctl status openai-kalshi-tunnel --no-pager"
echo "  sudo journalctl -u openai-kalshi-tunnel -n 100 --no-pager"
