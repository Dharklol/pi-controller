#!/bin/bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo ./install.sh" >&2
  exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

apt-get update
apt-get install -y python3 python3-venv python3-pip git curl ca-certificates sudo

python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10+ is required.")
print("Python:", sys.version.split()[0])
PY

if ! id kalshi-mcp >/dev/null 2>&1; then
  useradd \
    --system \
    --create-home \
    --home-dir /var/lib/pi-controller \
    --shell /usr/sbin/nologin \
    kalshi-mcp
fi

if getent group systemd-journal >/dev/null 2>&1; then
  usermod -aG systemd-journal kalshi-mcp
fi

install -d -o root -g root -m 0755 /opt/pi-controller
install -d -o root -g root -m 0755 /etc/pi-controller
install -d -o kalshi-mcp -g kalshi-mcp -m 0700 /var/lib/pi-controller

install -d -o kalshi-mcp -g kalshi-mcp -m 0750 /srv/kalshi
install -d -o kalshi-mcp -g kalshi-mcp -m 0750 /srv/kalshi/data
install -d -o kalshi-mcp -g kalshi-mcp -m 0750 /srv/kalshi/logs
install -d -o kalshi-mcp -g kalshi-mcp -m 0750 /srv/kalshi/state

install -o root -g root -m 0644 "$HERE/server.py" /opt/pi-controller/server.py
install -o root -g root -m 0644 "$HERE/requirements.txt" /opt/pi-controller/requirements.txt

if [[ ! -e /etc/pi-controller/config.json ]]; then
  install -o root -g root -m 0644 \
    "$HERE/config.example.json" \
    /etc/pi-controller/config.json
fi

install -o root -g root -m 0755 \
  "$HERE/scripts/kalshi-recorder-control" \
  /usr/local/sbin/kalshi-recorder-control

install -o root -g root -m 0440 \
  "$HERE/sudoers/pi-controller" \
  /etc/sudoers.d/pi-controller

visudo -cf /etc/sudoers.d/pi-controller

if [[ ! -d /opt/pi-controller/.venv ]]; then
  python3 -m venv /opt/pi-controller/.venv
fi

/opt/pi-controller/.venv/bin/python -m pip install --upgrade pip
/opt/pi-controller/.venv/bin/python -m pip install -r /opt/pi-controller/requirements.txt

install -o root -g root -m 0644 \
  "$HERE/systemd/pi-controller.service" \
  /etc/systemd/system/pi-controller.service

install -o root -g root -m 0644 \
  "$HERE/systemd/openai-kalshi-tunnel.service" \
  /etc/systemd/system/openai-kalshi-tunnel.service

systemctl daemon-reload
systemctl enable --now pi-controller.service

sleep 2

echo
echo "=== pi-controller status ==="
systemctl --no-pager --full status pi-controller.service || true

echo
echo "=== MCP local socket ==="
ss -ltnp | grep ':8765' || true

echo
echo "Installed successfully."
echo "MCP endpoint: http://127.0.0.1:8765/mcp"
echo "Project root: /srv/kalshi"
echo
echo "Next step: create the OpenAI Secure MCP Tunnel and then run:"
echo "  sudo ./setup_tunnel.sh tunnel_..."
