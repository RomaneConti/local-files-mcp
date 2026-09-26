import subprocess
import sys
import time
import threading
from types import SimpleNamespace

from local_files_mcp.tray_control import TrayEvents, signal, instance_name
from local_files_mcp.windows_process import WindowsNamedMutex


def test_named_event_commands(tmp_path):
    events = TrayEvents(tmp_path)
    try:
        assert signal(tmp_path, "stop")
        assert events.poll() == ["stop"]
        assert events.poll() == []
        assert signal(tmp_path, "restart")
        assert signal(tmp_path, "exit")
        assert set(events.poll()) == {"restart", "exit"}
    finally:
        events.close()
    assert not signal(tmp_path, "stop")


def test_single_instance_mutex(tmp_path):
    mutex = WindowsNamedMutex(instance_name(tmp_path))
    assert mutex.acquire()
    second = WindowsNamedMutex(instance_name(tmp_path))
    try:
        assert not second.acquire()
    finally:
        second.close()
        mutex.close()
    third = WindowsNamedMutex(instance_name(tmp_path))
    assert third.acquire()
    third.close()


def test_exit_poll_continues_when_worker_finishes_between_callbacks():
    from local_files_mcp.tray import TrayApplication

    callbacks = []
    closed = threading.Event()
    app = SimpleNamespace(
        exiting=True,
        closed=closed,
        root=SimpleNamespace(after=lambda delay, callback: callbacks.append(callback)),
    )
    app._tick = lambda: None
    # Model the worker completing just after the update missed its quit check.
    app._update_tick = closed.set
    TrayApplication._tick(app)
    assert callbacks == [app._tick]
