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
/etc/pi-controller/        root-owned policy/config
/var/lib/pi-controller/    tunnel-client state / runtime secret

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
chmod +x install.sh scripts/kalshi-recorder-control
sudo ./install.sh
```

Then verify:

```bash
sudo systemctl status pi-controller --no-pager
sudo journalctl -u pi-controller -n 50 --no-pager
```

The local MCP endpoint is:

```text
http://127.0.0.1:8765/mcp
```

## 2. Create an OpenAI Secure MCP Tunnel

In OpenAI Platform tunnel settings, create a tunnel and associate it with the Platform organization and ChatGPT workspace/account that should use it. Keep the resulting `tunnel_id`.

Install the current Linux ARM64 `tunnel-client` from OpenAI Platform tunnel settings or the latest public `openai/tunnel-client` release:

```bash
sudo install -m 0755 ./tunnel-client /usr/local/bin/tunnel-client
tunnel-client version
```

Create a runtime API key with tunnel-use permission. **Do not put it in this repository or paste it into ChatGPT.** For the first bootstrap, export it only in the shell where you initialize the tunnel:

```bash
export CONTROL_PLANE_API_KEY='YOUR_KEY_HERE'
```

Then initialize the HTTP tunnel profile:

```bash
tunnel-client init \
  --profile kalshi-pi \
  --tunnel-id tunnel_0123456789abcdef0123456789abcdef \
  --mcp-server-url http://127.0.0.1:8765/mcp

tunnel-client doctor --profile kalshi-pi --explain
```

Keep `tunnel-client run --profile kalshi-pi` healthy while testing. Once the profile works, install the included systemd unit after setting the runtime-key environment securely outside Git.

## 3. Connect from ChatGPT

Create a developer-mode app/plugin in ChatGPT, choose **Tunnel** as the connection type, then select this tunnel or paste its `tunnel_id`.

Initial smoke tests in ChatGPT:

- `system_info`
- `disk_usage`
- `list_files`
- `service_status`

The recorder service does not exist yet, so recorder-specific status can report `not-found` until we build it.

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
