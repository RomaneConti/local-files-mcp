"""Lifecycle checks use isolated HTTP fixtures, never the user's two repositories."""

import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from local_files_mcp import supervisor as module
from local_files_mcp.supervisor import SafeLog, Supervisor, probe_mcp, probe_tunnel
from local_files_mcp.windows_process import ManagedProcess, WindowsNamedMutex


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    assert predicate(), "Condition did not become true before timeout"


@pytest.fixture
def proxy_listener():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.2)
    stop = threading.Event()

    def accept_connections():
        while not stop.is_set():
            try:
                client, _ = listener.accept()
                client.close()
            except OSError:
                pass

    thread = threading.Thread(target=accept_connections, daemon=True)
    thread.start()
    yield listener.getsockname()[1]
    stop.set()
    listener.close()
    thread.join(timeout=1)


@pytest.fixture
def child_script(tmp_path):
    path = tmp_path / "fixture_server.py"
    path.write_text('''
import json, os, sys
from http.server import HTTPServer, BaseHTTPRequestHandler
tools = "list_roots list_directory find_files search_text file_info read_text read_json inspect_array read_array".split()
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def reply(self, data):
        body = json.dumps(data).encode() if isinstance(data, dict) else data.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        result = {"protocolVersion":"2025-11-25"}
        if payload["method"] == "tools/list":
            result = {"tools":[{"name":name,"annotations":{"readOnlyHint":True,"destructiveHint":False,"openWorldHint":False}} for name in tools]}
        self.reply({"jsonrpc":"2.0","id":payload["id"],"result":result})
    def do_GET(self):
        if self.path == "/healthz": self.reply("live")
        elif self.path == "/readyz": self.reply("ready")
        else:
            self.reply({"schema_version":1,"live":True,"ready":True,"components":{"control-plane":{
                "status":"ok","state":"polling","details":{"last_success":"2026-09-27T00:00:00Z",
                "consecutive_failures":0,"current_poll_age_seconds":0,"deadline_seconds":35}}}})
print("fixture key=" + os.environ.get("CONTROL_PLANE_API_KEY", "none"), flush=True)
HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
''', encoding="utf-8")
    return path


def configuration(tmp_path, proxy_port):
    return SimpleNamespace(project_dir=tmp_path, mcp_port=free_port(), tunnel_port=free_port(),
                           proxy_url=f"http://127.0.0.1:{proxy_port}", no_proxy="127.0.0.1,localhost",
                           tunnel_id="tunnel_fixture", log_dir=tmp_path / "logs")


def make_supervisor(tmp_path, proxy_port, child_script, monkeypatch):
    config = configuration(tmp_path, proxy_port)
    monkeypatch.setattr(module, "build_tunnel_command", lambda config: [sys.executable, "-B", str(child_script), str(config.tunnel_port)])
    return Supervisor(config, lambda: "sk-fixture-secret-should-never-appear",
                      [sys.executable, "-B", str(child_script), str(config.mcp_port)])


@pytest.mark.skipif(os.name != "nt", reason="Windows job objects")
def test_supervisor_start_stop_restart_and_secrets(tmp_path, proxy_listener, child_script, monkeypatch):
    supervisor = make_supervisor(tmp_path, proxy_listener, child_script, monkeypatch)
    try:
        supervisor.start()
        wait_until(lambda: supervisor.refresh()["tunnel"]["ready"])
        first = supervisor.snapshot()
        assert first["mcp"]["ready"]
        assert first["proxy"]["ready"]
        assert supervisor.start()["mcp"]["pid"] == first["mcp"]["pid"]
        supervisor.stop()
        assert module._port_available(supervisor.config.mcp_port)
        assert module._port_available(supervisor.config.tunnel_port)
        assert supervisor.snapshot()["mcp"]["status"] == "stopped"
        supervisor.restart()
        wait_until(lambda: supervisor.refresh()["tunnel"]["ready"])
        assert supervisor.snapshot()["mcp"]["pid"] != first["mcp"]["pid"]
        assert supervisor.snapshot()["tunnel"]["pid"] != first["tunnel"]["pid"]
    finally:
        supervisor.close()
    assert module._port_available(supervisor.config.mcp_port)
    assert module._port_available(supervisor.config.tunnel_port)
    logs = "".join(path.read_text(encoding="utf-8") for path in supervisor.config.log_dir.iterdir())
    assert "sk-fixture-secret-should-never-appear" not in logs
    assert "[REDACTED]" in logs
    assert "fixture key=none" in (supervisor.config.log_dir / "mcp.log").read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="Windows job objects")
def test_unreachable_proxy_preserves_ready_mcp(tmp_path, child_script, monkeypatch):
    supervisor = make_supervisor(tmp_path, free_port(), child_script, monkeypatch)
    try:
        state = supervisor.start()
        assert state["mcp"]["ready"]
        assert state["proxy"]["status"] == "failed"
        assert state["tunnel"]["status"] == "blocked"
        assert state["tunnel"]["pid"] is None
    finally:
        supervisor.close()


def test_occupied_port_is_not_killed(tmp_path, child_script, monkeypatch):
    supervisor = make_supervisor(tmp_path, free_port(), child_script, monkeypatch)
    with socket.socket() as unrelated:
        unrelated.bind(("127.0.0.1", supervisor.config.mcp_port))
        unrelated.listen()
        try:
            assert supervisor.start()["mcp"]["status"] == "failed"
            assert not supervisor._children
            with socket.create_connection(unrelated.getsockname(), timeout=1):
                pass
        finally:
            supervisor.close()


def test_mcp_failed_entrypoint_is_reported(tmp_path, proxy_listener, child_script, monkeypatch):
    supervisor = make_supervisor(tmp_path, proxy_listener, child_script, monkeypatch)
    supervisor.server_command = [sys.executable, "-c", "raise SystemExit(17)"]
    try:
        state = supervisor.start()
        assert state["mcp"]["status"] == "failed"
        assert "17" in state["mcp"]["detail"]
        assert state["tunnel"]["pid"] is None
    finally:
        supervisor.close()


@pytest.mark.parametrize("plane,expected", [
    ({"status": "degraded", "state": "backoff", "reason_code": "network_error", "details": {"last_success": "earlier"}}, False),
    ({"status": "unknown", "state": "polling", "details": {}}, False),
    ({"status": "ok", "state": "polling", "details": {"last_success": "recent", "consecutive_failures": 0}}, True),
    ({"status": "ok", "state": "polling", "details": {"last_success": "recent", "current_poll_age_seconds": 100, "deadline_seconds": 35}}, False),
    ({"status": "ok", "state": "backpressured", "details": {"last_success": "earlier"}}, True),
])
def test_tunnel_control_plane_evidence(monkeypatch, plane, expected):
    def request(port, path):
        return 200, {"schema_version": 1, "components": {"control-plane": plane}}
    monkeypatch.setattr(module, "_request", request)
    assert probe_tunnel(1234)[0] == expected


def test_mcp_malformed_health_is_not_a_crash(monkeypatch):
    monkeypatch.setattr(module, "_request", lambda *args: (200, {"result": []}))
    assert probe_mcp(1234)[0] is False


def test_explicit_direct_proxy_mode():
    assert module.probe_proxy("")[0]


def test_stop_error_does_not_skip_other_owned_tree(tmp_path, child_script, monkeypatch):
    supervisor = make_supervisor(tmp_path, free_port(), child_script, monkeypatch)
    stopped = []

    class Child:
        def __init__(self, name): self.name = name
        def stop(self):
            stopped.append(self.name)
            if self.name == "tunnel": raise OSError("fixture failure")
        def close(self): stopped.append(self.name + "-closed")

    reader = SimpleNamespace(join=lambda **kwargs: None)
    supervisor._children = {name: SimpleNamespace(process=Child(name), reader=reader) for name in ("mcp", "tunnel")}
    try:
        result = supervisor.stop()
        assert stopped == ["tunnel", "tunnel-closed", "mcp", "mcp-closed"]
        assert result["tunnel"]["status"] == "failed"
        assert result["mcp"]["status"] == "stopped"
    finally:
        supervisor.close()


def test_logs_redact_split_secret_and_oversized_lines(tmp_path):
    log = SafeLog(tmp_path / "safe.log")
    secret = "runtime-secret-unique-123456789"
    log.add_secret(secret)
    stream = io.BytesIO(b"x" * 4090 + secret.encode() + b"\n" + b"x" * 65536 + secret.encode() + b"\n" +
                        b"Authorization: Bearer bearer-test-secret\napi_key=another-secret\nsk-other-secret\n")
    module._pump_output(SimpleNamespace(stdout=stream), log)
    log.close()
    saved = (tmp_path / "safe.log").read_text(encoding="utf-8")
    for value in (secret, "bearer-test-secret", "another-secret", "sk-other-secret"):
        assert value not in saved
    assert "[REDACTED]" in saved and "Oversized" in saved


@pytest.mark.skipif(os.name != "nt", reason="Windows named mutex")
def test_mutex_is_released_and_second_instance_rejected():
    first = WindowsNamedMutex("Local\\LocalFilesMCP-test-" + str(os.getpid()))
    second = WindowsNamedMutex(first.name)
    try:
        assert first.acquire()
        assert not second.acquire()
        first.close()
        assert second.acquire()
    finally:
        first.close()
        second.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows job objects")
def test_forcibly_closed_launcher_kills_descendants(tmp_path):
    port = free_port()
    grandchild = "import socket,time,os; s=socket.socket(); s.bind(('127.0.0.1'," + str(port) + ")); s.listen(); print(os.getpid(),flush=True); time.sleep(60)"
    child = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-u','-c'," + repr(grandchild) + "]); time.sleep(60)"
    parent = ("from local_files_mcp.windows_process import ManagedProcess; from pathlib import Path; import os,sys,time; "
              "p=ManagedProcess([sys.executable,'-u','-c'," + repr(child) + "],cwd=Path.cwd(),env=dict(os.environ)); "
              "print(p.stdout.readline().decode().strip(),flush=True); time.sleep(60)")
    process = subprocess.Popen([sys.executable, "-u", "-c", parent], cwd=tmp_path,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        wait_until(lambda: not module._port_available(port))
        process.terminate()
        process.wait(timeout=5)
        wait_until(lambda: module._port_available(port))
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
