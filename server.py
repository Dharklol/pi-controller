#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

CONFIG_PATH = Path(os.environ.get("PI_CONTROLLER_CONFIG", "/etc/pi-controller/config.json"))
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

ROOTS = {name: Path(path).resolve() for name, path in CONFIG["roots"].items()}
PROJECT_ROOT = ROOTS["project"]
RECORDER_REPO = Path(CONFIG["recorder_repo"]).resolve()
RECORDER_HEALTH = Path(CONFIG["recorder_health_file"]).resolve()
ALLOWED_SERVICES = set(CONFIG["allowed_services"])

MAX_READ_BYTES = int(CONFIG.get("max_read_bytes", 2_000_000))
MAX_WRITE_BYTES = int(CONFIG.get("max_write_bytes", 2_000_000))
MAX_LIST_ENTRIES = int(CONFIG.get("max_list_entries", 500))

mcp = MCPServer(
    "Kalshi Pi Controller",
    instructions=(
        "Operate only inside the configured Kalshi project boundary. "
        "Prefer inspection before mutations. Never attempt to administer "
        "the Raspberry Pi outside the exposed bounded tools."
    ),
)


def run_process(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout after {timeout}s", "argv": argv}

    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout[-100_000:],
        "stderr": proc.stderr[-100_000:],
        "argv": argv,
    }


def resolve_path(scope: str, relative_path: str = "") -> Path:
    if scope not in ROOTS:
        raise ValueError(f"Unknown scope. Allowed scopes: {sorted(ROOTS)}")

    rel = Path(relative_path)
    if rel.is_absolute():
        raise ValueError("Paths must be relative to the selected scope.")

    root = ROOTS[scope]
    candidate = (root / rel).resolve(strict=False)

    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Path escapes the configured Kalshi scope.") from exc

    return candidate


def writable_project_path(relative_path: str) -> Path:
    path = resolve_path("project", relative_path)
    relative = path.relative_to(PROJECT_ROOT)

    if relative.parts and relative.parts[0] == ".git":
        raise ValueError("Direct writes inside .git are blocked; use the Git tools.")

    return path


def recorder_repo() -> Path:
    try:
        RECORDER_REPO.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise RuntimeError("Configured recorder repo escapes the project root.") from exc

    if not (RECORDER_REPO / ".git").exists():
        raise RuntimeError(f"Recorder repository is not present at {RECORDER_REPO}")

    return RECORDER_REPO


def allowed_service(name: str) -> str:
    if name not in ALLOWED_SERVICES:
        raise ValueError(f"Service is not allowlisted. Allowed: {sorted(ALLOWED_SERVICES)}")
    return name


@mcp.tool()
def system_info() -> dict[str, Any]:
    """Use this when you need Raspberry Pi health relevant to the Kalshi recorder."""
    uptime_seconds = None
    temperature_c = None

    try:
        uptime_seconds = float(Path("/proc/uptime").read_text().split()[0])
    except Exception:
        pass

    thermal = Path("/sys/class/thermal/thermal_zone0/temp")
    try:
        if thermal.exists():
            temperature_c = int(thermal.read_text().strip()) / 1000.0
    except Exception:
        pass

    return {
        "hostname": platform.node(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "uptime_seconds": uptime_seconds,
        "load_average": list(os.getloadavg()),
        "temperature_c": temperature_c,
    }


@mcp.tool()
def disk_usage(scope: str = "project") -> dict[str, Any]:
    """Use this when you need storage capacity for an allowed Kalshi filesystem scope."""
    path = resolve_path(scope)
    total, used, free = shutil.disk_usage(path)
    return {
        "scope": scope,
        "path": str(path),
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "used_percent": round((used / total) * 100, 2) if total else None,
    }


@mcp.tool()
def list_files(
    scope: str = "project",
    path: str = "",
    recursive: bool = False,
    max_entries: int = 200,
) -> dict[str, Any]:
    """Use this when you need to inspect files or directories in an allowed Kalshi scope."""
    base = resolve_path(scope, path)

    if not base.exists():
        return {"ok": False, "error": "path does not exist", "path": path}

    if base.is_file():
        return {
            "ok": True,
            "entries": [{
                "path": str(base.relative_to(ROOTS[scope])),
                "type": "file",
                "bytes": base.stat().st_size,
            }],
        }

    limit = max(1, min(int(max_entries), MAX_LIST_ENTRIES))
    iterator = base.rglob("*") if recursive else base.iterdir()
    entries: list[dict[str, Any]] = []

    for item in iterator:
        if len(entries) >= limit:
            break

        try:
            resolved = item.resolve(strict=False)
            resolved.relative_to(ROOTS[scope])
            stat = item.stat()
        except (ValueError, FileNotFoundError):
            continue

        entries.append({
            "path": str(item.relative_to(ROOTS[scope])),
            "type": "directory" if item.is_dir() else "file" if item.is_file() else "other",
            "bytes": stat.st_size if item.is_file() else None,
            "modified_ns": stat.st_mtime_ns,
        })

    return {
        "ok": True,
        "scope": scope,
        "base": path,
        "entries": entries,
        "truncated": len(entries) >= limit,
    }


@mcp.tool()
def read_file(
    scope: str,
    path: str,
    start_line: int = 1,
    max_lines: int = 400,
) -> dict[str, Any]:
    """Use this when you need to read a UTF-8 text file in an allowed Kalshi scope."""
    file_path = resolve_path(scope, path)

    if not file_path.is_file():
        return {"ok": False, "error": "file does not exist", "path": path}

    size = file_path.stat().st_size
    if size > MAX_READ_BYTES:
        return {
            "ok": False,
            "error": f"file exceeds direct-read cap of {MAX_READ_BYTES} bytes",
            "bytes": size,
        }

    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return {"ok": False, "error": "binary/non-UTF8 direct read is blocked"}

    first = max(1, int(start_line))
    count = max(1, min(int(max_lines), 2000))
    selected = lines[first - 1 : first - 1 + count]

    return {
        "ok": True,
        "scope": scope,
        "path": path,
        "start_line": first,
        "end_line": first + len(selected) - 1 if selected else first - 1,
        "total_lines": len(lines),
        "content": "\n".join(selected),
    }


@mcp.tool()
def create_directory(path: str) -> dict[str, Any]:
    """Use this when you need to create a directory inside the Kalshi project root."""
    target = writable_project_path(path)
    target.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "path": path}


@mcp.tool()
def write_file(path: str, content: str, overwrite: bool = False) -> dict[str, Any]:
    """Use this when you need to create or intentionally replace a text file inside the Kalshi project."""
    payload = content.encode("utf-8")
    if len(payload) > MAX_WRITE_BYTES:
        raise ValueError(f"Write exceeds {MAX_WRITE_BYTES} bytes.")

    target = writable_project_path(path)

    if target.exists() and not overwrite:
        return {"ok": False, "error": "file exists; set overwrite=true to replace it", "path": path}

    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + f".tmp-{os.getpid()}-{time.time_ns()}")
    temp.write_text(content, encoding="utf-8")
    os.replace(temp, target)

    return {"ok": True, "path": path, "bytes": len(payload)}


@mcp.tool()
def replace_text(
    path: str,
    old: str,
    new: str,
    replace_all: bool = False,
) -> dict[str, Any]:
    """Use this when you need a narrow text replacement inside a Kalshi project file."""
    target = writable_project_path(path)

    if not target.is_file():
        return {"ok": False, "error": "file does not exist", "path": path}

    if target.stat().st_size > MAX_READ_BYTES:
        return {"ok": False, "error": "file is too large for replace_text"}

    text = target.read_text(encoding="utf-8")
    matches = text.count(old)

    if matches == 0:
        return {"ok": False, "error": "old text was not found", "matches": 0}

    if matches > 1 and not replace_all:
        return {
            "ok": False,
            "error": "old text is not unique; set replace_all=true only if intended",
            "matches": matches,
        }

    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)

    if len(updated.encode("utf-8")) > MAX_WRITE_BYTES:
        return {"ok": False, "error": "updated file exceeds write cap"}

    temp = target.with_name(target.name + f".tmp-{os.getpid()}-{time.time_ns()}")
    temp.write_text(updated, encoding="utf-8")
    os.replace(temp, target)

    return {"ok": True, "path": path, "matches_replaced": matches if replace_all else 1}


@mcp.tool()
def git_clone_recorder(repo_url: str) -> dict[str, Any]:
    """Use this only when the recorder repo has not been cloned yet; destination is fixed to /srv/kalshi/recorder."""
    if not (
        repo_url.startswith("https://github.com/")
        or repo_url.startswith("git@github.com:")
    ):
        raise ValueError("Only github.com repository URLs are accepted.")

    if RECORDER_REPO.exists() and any(RECORDER_REPO.iterdir()):
        return {"ok": False, "error": "recorder destination already exists and is not empty"}

    RECORDER_REPO.parent.mkdir(parents=True, exist_ok=True)
    return run_process(["git", "clone", "--", repo_url, str(RECORDER_REPO)], timeout=300)


@mcp.tool()
def git_status() -> dict[str, Any]:
    """Use this when you need recorder Git branch and working-tree status."""
    return run_process(["git", "-C", str(recorder_repo()), "status", "--short", "--branch"])


@mcp.tool()
def git_log(limit: int = 20) -> dict[str, Any]:
    """Use this when you need recent recorder Git commits."""
    count = max(1, min(int(limit), 100))
    return run_process([
        "git",
        "-C",
        str(recorder_repo()),
        "log",
        f"-{count}",
        "--date=iso",
        "--pretty=format:%h%x09%ad%x09%an%x09%s",
    ])


@mcp.tool()
def git_fetch() -> dict[str, Any]:
    """Use this when you need updated remote refs without modifying the recorder working tree."""
    return run_process(
        ["git", "-C", str(recorder_repo()), "fetch", "--prune"],
        timeout=120,
    )


@mcp.tool()
def git_pull() -> dict[str, Any]:
    """Use this when you need to update the recorder checkout; pulls are fast-forward-only."""
    return run_process(
        ["git", "-C", str(recorder_repo()), "pull", "--ff-only"],
        timeout=120,
    )


@mcp.tool()
def service_status(name: str = "kalshi-recorder.service") -> dict[str, Any]:
    """Use this when you need current status for the allowlisted Kalshi recorder service."""
    name = allowed_service(name)
    return run_process([
        "systemctl",
        "show",
        name,
        "--no-pager",
        "--property=Id,LoadState,ActiveState,SubState,MainPID,Result,NRestarts,ExecMainStartTimestamp",
    ])


@mcp.tool()
def recent_logs(
    name: str = "kalshi-recorder.service",
    lines: int = 200,
    since: str = "2 hours ago",
) -> dict[str, Any]:
    """Use this when you need recent journal logs for the allowlisted Kalshi recorder service."""
    name = allowed_service(name)
    count = max(1, min(int(lines), 1000))

    return run_process([
        "journalctl",
        "-u",
        name,
        "--no-pager",
        "-n",
        str(count),
        "--since",
        since,
        "-o",
        "short-iso-precise",
    ])


def control_service(action: str, name: str) -> dict[str, Any]:
    name = allowed_service(name)
    if action not in {"start", "stop", "restart"}:
        raise ValueError("Unsupported service action.")

    return run_process([
        "sudo",
        "/usr/local/sbin/kalshi-recorder-control",
        action,
        name,
    ])


@mcp.tool()
def start_service(name: str = "kalshi-recorder.service") -> dict[str, Any]:
    """Use this when the allowlisted Kalshi recorder service should be started."""
    return control_service("start", name)


@mcp.tool()
def stop_service(name: str = "kalshi-recorder.service") -> dict[str, Any]:
    """Use this when the allowlisted Kalshi recorder service should be intentionally stopped."""
    return control_service("stop", name)


@mcp.tool()
def restart_service(name: str = "kalshi-recorder.service") -> dict[str, Any]:
    """Use this when the allowlisted Kalshi recorder service needs a deliberate restart."""
    return control_service("restart", name)


@mcp.tool()
def recorder_health() -> dict[str, Any]:
    """Use this when you need the recorder's future self-reported health plus systemd status."""
    output: dict[str, Any] = {
        "health_available": False,
        "health_file": str(RECORDER_HEALTH),
        "service": service_status("kalshi-recorder.service"),
    }

    if RECORDER_HEALTH.exists():
        try:
            output["health"] = json.loads(RECORDER_HEALTH.read_text(encoding="utf-8"))
            output["health_available"] = True
        except Exception as exc:
            output["health_error"] = repr(exc)

    return output


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=8765,
        stateless_http=True,
        json_response=True,
    )
