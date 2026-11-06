"""Supervise existing MCP/tunnel entrypoints; health probes never use a proxy."""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import socket
import sys
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

from . import __version__
from .launcher_config import LauncherConfig, build_child_environment, build_tunnel_command
from .windows_process import ManagedProcess

from .tool_catalog import EXPECTED_TOOLS
MAX_PROBE_BYTES = 256 * 1024


class SafeLog:
    """Redact before the file handler sees a record, and keep disk usage bounded."""

    def __init__(self, path: Path):
        self._secrets: set[str] = set()
        self._lock = threading.Lock()
        self._handler = RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
        self._handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))

    def add_secret(self, secret: str) -> None:
        if secret:
            with self._lock:
                self._secrets.add(secret)

    def write(self, text: str) -> None:
        with self._lock:
            for secret in self._secrets:
                text = text.replace(secret, "[REDACTED]")
            text = re.sub(r"(?i)\bsk-[A-Za-z0-9_-]+", "[REDACTED]", text)
            text = re.sub(r"(?i)(authorization[\s\"':=]+(?:bearer\s+)?)[^\s,}\]]+", r"\1[REDACTED]", text)
            text = re.sub(r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token)[\s\"':=]+)[^\s,}\]]+",
                          r"\1[REDACTED]", text)
            text = "".join(c if c >= " " or c == "\t" else " " for c in text)
            self._handler.handle(logging.LogRecord("tray", logging.INFO, "", 0, text[:8192], (), None))

    def close(self) -> None:
        with self._lock:
            self._handler.close()


def _pump_output(process: ManagedProcess, log: SafeLog) -> None:
    """Discard huge lines whole, avoiding secret leakage across chunk boundaries."""
    pending = bytearray()
    discard = False
    stream = process.stdout
    try:
        while chunk := stream.read(4096):
            for fragment in chunk.splitlines(keepends=True):
                ended = fragment.endswith((b"\n", b"\r"))
                if not discard:
                    pending.extend(fragment)
                    if len(pending) > 65536:
                        pending.clear()
                        discard = True
                        log.write("[Oversized child output line omitted]")
                if ended:
                    if not discard:
                        log.write(pending.decode("utf-8", errors="replace").rstrip("\r\n"))
                    pending.clear()
                    discard = False
        if pending and not discard:
            log.write(pending.decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        pass  # Pipe closure is normal during shutdown.


def _request(port: int, path: str, payload: dict | None = None) -> tuple[int, dict]:
    # http.client does not consume HTTP_PROXY/HTTPS_PROXY from the environment.
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.8)
    try:
        headers = {"Accept": "application/json, text/event-stream"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["MCP-Protocol-Version"] = "2025-11-25"
        connection.request("POST" if payload is not None else "GET", path, body, headers)
        response = connection.getresponse()
        data = response.read(MAX_PROBE_BYTES + 1)
        if len(data) > MAX_PROBE_BYTES:
            raise ValueError("Health response exceeded size limit")
        # The official daemon returns plain "live" / "ready" on these endpoints.
        parsed = {} if path in {"/healthz", "/readyz"} else json.loads(data) if data.strip() else {}
        if not isinstance(parsed, dict):
            raise ValueError("Health response is not an object")
        return response.status, parsed
    finally:
        connection.close()


def probe_mcp(port: int) -> tuple[bool, str]:
    try:
        status, initialized = _request(port, "/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "local-files-tray-health", "version": __version__}}})
        if status != 200 or "result" not in initialized:
            return False, f"MCP initialize failed (HTTP {status})"
        status, result = _request(port, "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        if status != 200 or "result" not in result:
            return False, f"MCP tools/list failed (HTTP {status})"
        tools = result["result"].get("tools", [])
        if len(tools) != len(EXPECTED_TOOLS) or {tool.get("name") for tool in tools} != EXPECTED_TOOLS:
            return False, f"MCP tool list differs from the {len(EXPECTED_TOOLS)} expected read-only tools"
        if not all(tool.get("annotations", {}).get("readOnlyHint") is True and
                   tool.get("annotations", {}).get("destructiveHint") is False and
                   tool.get("annotations", {}).get("openWorldHint") is False for tool in tools):
            return False, "MCP read-only tool annotations failed validation"
        version = initialized["result"].get("serverInfo", {}).get("version", "unknown")
        return True, f"MCP v{version}: initialize and all {len(EXPECTED_TOOLS)} read-only tool definitions verified"
    except (OSError, ValueError, http.client.HTTPException, KeyError, TypeError, AttributeError):
        return False, "MCP HTTP/protocol health check unavailable"


def probe_proxy(proxy_url: str) -> tuple[bool, str]:
    if not proxy_url:
        return True, "Direct mode configured; tunnel health verifies OpenAI connectivity"
    parsed = urlsplit(proxy_url)
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 80), timeout=0.6):
            return True, "Proxy TCP listener reachable; tunnel health verifies OpenAI connectivity"
    except (OSError, ValueError, TypeError):
        return False, "Proxy unreachable; start Clash or check the configured proxy address"


def probe_tunnel(port: int) -> tuple[bool, str]:
    """Readiness alone does not establish control-plane connectivity (v0.0.15)."""
    try:
        status, health = _request(port, "/healthz")
        ready_status, ready = _request(port, "/readyz")
        detail_status, details = _request(port, "/health?details=true")
        if status != 200:
            return False, f"Tunnel health check failed (HTTP {status})"
        if ready_status != 200:
            return False, f"Tunnel running but not ready (HTTP {ready_status}); see tunnel log"
        if detail_status != 200 or details.get("schema_version") != 1:
            return False, "Tunnel ready endpoint responds, but detailed health is unavailable"
        plane = details.get("components", {}).get("control-plane", {})
        evidence = plane.get("details", {})
        reason = plane.get("reason_code", "")
        known_reasons = {"network_error", "unauthorized", "forbidden", "not_found", "rate_limited",
                         "server_error", "request_timeout", "deadline_exceeded", "invalid_config"}
        if plane.get("status") != "ok" or not evidence.get("last_success"):
            suffix = f" ({reason})" if reason in known_reasons else ""
            return False, "Tunnel local readiness passed; OpenAI control plane has not recovered" + suffix
        if evidence.get("consecutive_failures", 0) != 0:
            return False, "OpenAI control plane reports consecutive failures; see tunnel log"
        state = plane.get("state")
        if state not in {"polling", "idle", "backpressured"}:
            return False, "OpenAI control plane is not in an active polling state"
        if state == "polling" and (float(evidence.get("current_poll_age_seconds", 0)) >
                                   float(evidence.get("deadline_seconds", 35)) + 5):
            return False, "OpenAI control-plane poll exceeded its reported deadline"
        if state == "backpressured":
            return True, "OpenAI control-plane success verified; queue is applying backpressure"
        return True, "Tunnel ready; successful OpenAI control-plane polling verified"
    except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
        return False, "Tunnel health endpoint unavailable; see tunnel log"


def _port_available(port: int) -> bool:
    try:
        with socket.socket() as listener:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(("127.0.0.1", port))
            return True
    except OSError:
        return False


@dataclass
class _Child:
    process: ManagedProcess
    reader: threading.Thread


class Supervisor:
    def __init__(self, config: LauncherConfig, key_provider: Callable[[], str | None],
                 server_command: list[str] | None = None):
        self.config = config
        self.key_provider = key_provider
        self.server_command = server_command or [
            sys.executable, "-B", "-m", "local_files_mcp.server",
            "--config", str(config.project_dir / "config.toml"), "--port", str(config.mcp_port)]
        self._lifecycle = threading.RLock()
        self._state_lock = threading.Lock()
        self._children: dict[str, _Child] = {}
        self._closed = False
        self._state = {"operation": "stopped", "mcp": self._entry("stopped", "Not started"),
                       "tunnel": self._entry("stopped", "Not started"),
                       "proxy": self._entry("unknown", "Not checked")}
        config.log_dir.mkdir(parents=True, exist_ok=True)
        self._logs = {name: SafeLog(config.log_dir / f"{name}.log") for name in ("launcher", "mcp", "tunnel")}

    @staticmethod
    def _entry(status: str, detail: str, pid: int | None = None) -> dict:
        return {"status": status, "ready": status == "ready", "detail": detail, "pid": pid}

    def _set(self, name: str, status: str, detail: str, pid: int | None = None) -> None:
        entry = self._entry(status, detail, pid)
        with self._state_lock:
            changed = self._state[name] != entry
            self._state[name] = entry
        if changed:
            self._logs["launcher"].write(f"{name}: {status}; {detail}" + (f" (owned PID {pid})" if pid else ""))

    def _operation(self, value: str) -> None:
        with self._state_lock:
            self._state["operation"] = value

    def snapshot(self) -> dict:
        with self._state_lock:
            return {name: value.copy() if isinstance(value, dict) else value for name, value in self._state.items()}

    def _spawn(self, name: str, command: list[str], key: str | None = None) -> None:
        process = ManagedProcess(command, cwd=self.config.project_dir,
                                 env=build_child_environment(self.config, key, for_tunnel=name == "tunnel"))
        reader = threading.Thread(target=_pump_output, args=(process, self._logs[name]), daemon=True,
                                  name=f"{name}-log-reader")
        self._children[name] = _Child(process, reader)
        reader.start()
        self._set(name, "starting", "Process started; waiting for application health", process.pid)

    def start(self) -> dict:
        with self._lifecycle:
            if self._closed:
                return self.snapshot()
            if self._children:
                return self.refresh()
            self._operation("starting")
            for name, port in (("mcp", self.config.mcp_port), ("tunnel", self.config.tunnel_port)):
                if not _port_available(port):
                    self._set(name, "failed", f"Port {port} is occupied; existing process was not changed")
                    self._operation("failed")
                    return self.snapshot()
            try:
                self._spawn("mcp", self.server_command)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    process = self._children["mcp"].process
                    if process.poll() is not None:
                        self._set("mcp", "failed", f"MCP process exited (code {process.poll()}); see mcp.log")
                        self._operation("failed")
                        return self.snapshot()
                    ready, detail = probe_mcp(self.config.mcp_port)
                    if ready:
                        self._set("mcp", "ready", detail, process.pid)
                        break
                    time.sleep(0.15)
                else:
                    self._set("mcp", "failed", "MCP did not become ready within 15 seconds; see mcp.log", process.pid)
                    self._operation("failed")
                    return self.snapshot()
                proxy_ready, proxy_detail = probe_proxy(self.config.proxy_url)
                self._set("proxy", "ready" if proxy_ready else "failed", proxy_detail)
                if not proxy_ready:
                    self._set("tunnel", "blocked", "Clash proxy unavailable; start Clash, then choose Restart")
                    self._operation("running")
                    return self.snapshot()
                key = self.key_provider()
                if not key:
                    self._set("tunnel", "blocked", "Runtime key unavailable or entry cancelled; choose Restart to retry")
                    self._operation("running")
                    return self.snapshot()
                for log in self._logs.values():
                    log.add_secret(key)
                self._spawn("tunnel", build_tunnel_command(self.config), key)
                key = None
                self._operation("running")
                return self.refresh()
            except Exception as exc:
                # Never include exception text: a third-party error may quote credentials.
                target = "tunnel" if self.snapshot()["mcp"]["ready"] else "mcp"
                self._set(target, "failed", f"Startup failed ({type(exc).__name__}); check configuration and installed files")
                self._operation("failed")
                return self.snapshot()

    def refresh(self) -> dict:
        if not self._lifecycle.acquire(blocking=False):
            return self.snapshot()
        try:
            if self._closed:
                return self.snapshot()
            ready, detail = probe_proxy(self.config.proxy_url)
            self._set("proxy", "ready" if ready else "failed", detail)
            for name, probe, port in (("mcp", probe_mcp, self.config.mcp_port),
                                      ("tunnel", probe_tunnel, self.config.tunnel_port)):
                child = self._children.get(name)
                if child is None:
                    continue
                exit_code = child.process.poll()
                if exit_code is not None:
                    self._set(name, "failed", f"Owned process exited (code {exit_code}); see {name}.log")
                    continue
                ready, detail = probe(port)
                self._set(name, "ready" if ready else "unavailable", detail, child.process.pid)
            return self.snapshot()
        finally:
            self._lifecycle.release()

    def stop(self) -> dict:
        with self._lifecycle:
            if self._closed:
                return self.snapshot()
            self._operation("stopping")
            failed = False
            for name in ("tunnel", "mcp"):
                child = self._children.pop(name, None)
                child_failed = False
                if child is not None:
                    try:
                        child.process.stop()
                    except Exception:
                        child_failed = True
                    finally:
                        child.reader.join(timeout=3)
                        try:
                            child.process.close()
                        except Exception:
                            child_failed = True
                if child_failed:
                    failed = True
                    self._set(name, "failed", "Cleanup error; job handles closed but process exit was not confirmed")
                else:
                    self._set(name, "stopped", "Stopped; all owned process-tree handles closed")
            self._operation("failed" if failed else "stopped")
            return self.snapshot()

    def restart(self) -> dict:
        with self._lifecycle:
            self.stop()
            return self.start()

    def close(self) -> None:
        with self._lifecycle:
            if not self._closed:
                self.stop()
                self._closed = True
                for log in self._logs.values():
                    log.close()
