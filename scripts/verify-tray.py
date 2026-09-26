"""Exercise the built tray through its real UI command queue, with no secret output."""

import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
import time

from local_files_mcp.launcher_config import load_launcher_config
from local_files_mcp.tray_control import signal
from local_files_mcp.user_config import default_data_dir

PROJECT = Path(__file__).resolve().parents[1]
EXECUTABLE = PROJECT / "dist" / "LocalFilesMCP" / "LocalFilesMCP.exe"
DATA = default_data_dir()


def port_free(port):
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def wait_state(process, condition, timeout=70):
    until = time.monotonic() + timeout
    latest = {}
    while time.monotonic() < until:
        if process.poll() is not None:
            raise AssertionError(f"Tray exited unexpectedly: {process.returncode}")
        try:
            latest = json.loads((DATA / ".runtime" / "tray-status.json").read_text(encoding="utf-8"))
            if latest.get("launcher_pid") == process.pid and condition(latest):
                return latest
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    print(json.dumps(latest, indent=2, ensure_ascii=True), flush=True)
    raise AssertionError("Tray did not reach the expected health state")


def main():
    global DATA, EXECUTABLE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA)
    parser.add_argument("--exe", type=Path, default=EXECUTABLE)
    args = parser.parse_args()
    DATA, EXECUTABLE = args.data_dir.resolve(), args.exe.resolve()
    config = load_launcher_config(DATA, bundle_dir=EXECUTABLE.parent / "_internal")
    assert port_free(config.mcp_port) and port_free(config.tunnel_port), "Existing services must be stopped first"
    assert not signal(DATA, "show"), "An existing tray instance is already running"
    command = [str(EXECUTABLE), "--data-dir", str(DATA)]
    process = subprocess.Popen(command, cwd=EXECUTABLE.parent, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        ready = lambda state: state["mcp"].get("ready") and state["tunnel"].get("ready")
        initial = wait_state(process, ready)
        print("EXE auto-start and actual MCP/control-plane readiness: PASS", flush=True)
        subprocess.run([sys.executable, "-B", str(PROJECT / "scripts" / "smoke-test.py"),
                        "--url", config.mcp_url], check=True)
        duplicate = subprocess.Popen(command, cwd=EXECUTABLE.parent, creationflags=subprocess.CREATE_NO_WINDOW)
        assert duplicate.wait(timeout=10) == 0
        same = wait_state(process, ready, timeout=5)
        assert same["mcp"]["pid"] == initial["mcp"]["pid"]
        assert same["tunnel"]["pid"] == initial["tunnel"]["pid"]
        print("Duplicate instance prevented: PASS", flush=True)
        assert signal(DATA, "stop")
        wait_state(process, lambda state: all(state[name]["status"] == "stopped" for name in ("mcp", "tunnel")), 15)
        assert port_free(config.mcp_port) and port_free(config.tunnel_port)
        print("Tray Stop releases both ports: PASS", flush=True)
        assert signal(DATA, "restart")
        restarted = wait_state(process, ready)
        assert restarted["mcp"]["pid"] != initial["mcp"]["pid"]
        assert restarted["tunnel"]["pid"] != initial["tunnel"]["pid"]
        print("Tray Restart with new healthy children: PASS", flush=True)
        assert signal(DATA, "exit")
        assert process.wait(timeout=20) == 0
        assert port_free(config.mcp_port) and port_free(config.tunnel_port)
        final = json.loads((DATA / ".runtime" / "tray-status.json").read_text(encoding="utf-8"))
        assert all(final[name]["status"] == "stopped" for name in ("mcp", "tunnel"))
        assert (DATA / ".runtime" / "logs" / "tunnel.log").stat().st_size > 0
        assert not (DATA / "stdout").exists(), "Tunnel log output bypassed the managed logger"
        print("Tray Exit and owned process-tree/port cleanup: PASS", flush=True)
    finally:
        if process.poll() is None:
            signal(DATA, "exit")
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=10)


if __name__ == "__main__":
    main()
