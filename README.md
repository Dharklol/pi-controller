# pi-controller

Bounded MCP control plane for the Raspberry Pi running the Kalshi live-recorder project.

The design goal is **full control over the Kalshi project, not full control over the Raspberry Pi**.

## Security boundary

The MCP service is allowed to:

- inspect Pi health relevant to recording;
- read/write files under `/srv/kalshi` only;
- inspect recorder data/log/state directories;
- clone/fetch/pull the configured recorder Git repository;
- inspect `kalshi-recorder.service` and its journal logs;
- start/stop/restart only `kalshi-recorder.service`;
- read the recorder's future health JSON.

It deliberately cannot:

- run arbitrary shell commands;
- use arbitrary `sudo`;
- change SSH, Raspberry Pi Connect, Tailscale, networking, packages, boot config, or disks;
- read/write arbitrary OS/home paths;
- Git reset/clean/push through the Pi control surface;
- rewrite its own root-owned MCP policy files through MCP tools.

## Layout

```text
/opt/pi-controller/        root-owned MCP implementation
/etc/pi-controller/        root-owned policy/config + tunnel env
/var/lib/pi-controller/    tunnel-client state

/srv/kalshi/               MCP-writable project boundary
  recorder/                future Kalshi live-recorder Git checkout
  data/
  logs/
  state/
```

The MCP server binds only to `127.0.0.1:8765` and exposes Streamable HTTP at `/mcp`.

## 1. Install the bootstrap on the Pi

```bash
git clone https://github.com/Dharklol/pi-controller.git
cd pi-controller
chmod +x install.sh setup_tunnel.sh scripts/kalshi-recorder-control
sudo ./install.sh
```

Then verify:

```bash
sudo systemctl status pi-controller --no-pager
sudo journalctl -u pi-controller -n 50 --no-pager
ss -ltnp | grep ':8765'
```

The local MCP endpoint is:

```text
http://127.0.0.1:8765/mcp
```

The installer uses Python 3.10+ and the stable MCP Python SDK v2 line.

## 2. Create an OpenAI Secure MCP Tunnel

In OpenAI Platform tunnel settings:

1. Create a Secure MCP Tunnel.
2. Associate the Platform organization that owns it.
3. Associate the ChatGPT workspace/account that should use it.
4. Keep the resulting `tunnel_id`.

Creating/editing a tunnel needs **Tunnels Read + Manage**. Running `tunnel-client` or selecting the tunnel in ChatGPT needs **Tunnels Read + Use**.

## 3. Install `tunnel-client` on the Pi

Use the current Linux ARM64/aarch64 download from OpenAI Platform tunnel settings or the latest public `openai/tunnel-client` release.

For v0.0.13, the ARM64 archive is:

```bash
wget -O tunnel-client-v0.0.13-linux-arm64.zip \
  https://github.com/openai/tunnel-client/releases/download/v0.0.13/tunnel-client-v0.0.13-linux-arm64.zip

rm -rf tunnel-client-dist
mkdir tunnel-client-dist
unzip tunnel-client-v0.0.13-linux-arm64.zip -d tunnel-client-dist

sudo install -m 0755 tunnel-client-dist/tunnel-client /usr/local/bin/tunnel-client
sudo install -m 0755 tunnel-client-dist/cloudflared /usr/local/bin/cloudflared
sudo install -m 0644 tunnel-client-dist/cloudflared-manifest.json /usr/local/bin/cloudflared-manifest.json

tunnel-client --version
tunnel-client cloudflared version
tunnel-client help quickstart
```

The companion `cloudflared` binary and manifest should remain adjacent to `tunnel-client`; supported release archives are built that way intentionally.

The Pi needs outbound HTTPS to OpenAI; no public inbound port is required.

## 4. Store the tunnel runtime key locally

Create/use a runtime API key with tunnel-use permission.

**Do not put it in GitHub and do not paste it into ChatGPT.**

Run the tunnel setup once:

```bash
sudo ./setup_tunnel.sh tunnel_0123456789abcdef0123456789abcdef
```

On first run, the script creates:

```text
/etc/pi-controller/tunnel.env
```

and asks you to edit it:

```bash
sudo nano /etc/pi-controller/tunnel.env
```

Put exactly:

```text
CONTROL_PLANE_API_KEY=YOUR_RUNTIME_KEY
```

Save and exit, then rerun:

```bash
sudo ./setup_tunnel.sh tunnel_0123456789abcdef0123456789abcdef
```

That initializes the `kalshi-pi` tunnel profile, runs:

```bash
tunnel-client doctor --profile kalshi-pi --explain
```

and enables the persistent `openai-kalshi-tunnel.service`.

Check it with:

```bash
sudo systemctl status openai-kalshi-tunnel --no-pager
sudo journalctl -u openai-kalshi-tunnel -n 100 --no-pager
```

## 5. Connect from ChatGPT

Create a developer-mode app/plugin in ChatGPT:

- Connection: **Tunnel**
- select the available tunnel, or paste its `tunnel_id`.

Initial smoke tests:

- `system_info`
- `disk_usage`
- `list_files`
- `service_status`

The recorder service does not exist yet, so recorder-specific service status can report `not-found` until we build it.

## Bootstrap MCP tools

Read/inspection:

- `system_info`
- `disk_usage`
- `list_files`
- `read_file`
- `git_status`
- `git_log`
- `git_fetch`
- `service_status`
- `recent_logs`
- `recorder_health`

Bounded mutations:

- `create_directory`
- `write_file`
- `replace_text`
- `git_clone_recorder`
- `git_pull`
- `start_service`
- `stop_service`
- `restart_service`

There is intentionally no generic `shell(command)` tool.

## Notes

- `/opt/pi-controller` and `/etc/pi-controller` are root-owned so the MCP tools cannot modify their own authority.
- `/srv/kalshi` is the project boundary the MCP account can operate.
- Git pulls are fast-forward-only.
- Direct writes into `.git` are blocked.
- Raspberry Pi Connect remains the human/admin recovery path.
