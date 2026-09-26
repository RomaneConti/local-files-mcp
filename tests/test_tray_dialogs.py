import queue
import threading
from types import SimpleNamespace

import pytest

from local_files_mcp.tray import TrayApplication


def test_modal_opening_is_deferred_single_instance_and_cancelled_on_exit():
    scheduled, opened = [], []
    app = SimpleNamespace(
        exiting=False, dialog_active=False,
        root=SimpleNamespace(after_idle=scheduled.append),
        _configure=lambda: opened.append("configure"),
    )
    TrayApplication._schedule_dialog(app, "configure")
    TrayApplication._schedule_dialog(app, "credentials")
    assert app.dialog_active and len(scheduled) == 1 and not opened
    app.exiting = True
    scheduled[0]()
    assert not app.dialog_active and not opened


@pytest.mark.parametrize("method,module,function", [
    ("_configure", "local_files_mcp.setup_dialog", "configure"),
    ("_credential_prompt", "local_files_mcp.credential_dialog", "prompt_key"),
])
def test_dialog_save_finishing_during_shutdown_cannot_restart(tmp_path, monkeypatch, method, module, function):
    app = SimpleNamespace(project=tmp_path, root=object(), exiting=False, operations=queue.Queue())

    def saved_during_exit(*args, **kwargs):
        app.exiting = True
        return True

    monkeypatch.setattr(f"{module}.{function}", saved_during_exit)
    assert getattr(TrayApplication, method)(app)
    assert app.operations.empty()


def test_tray_worker_uses_only_persisted_credential(tmp_path, monkeypatch):
    credential_options = []
    operations = queue.Queue()
    operations.put("start")
    operations.put("exit")
    monkeypatch.setattr("local_files_mcp.launcher_config.load_launcher_config",
                        lambda *args, **kwargs: SimpleNamespace(mcp_port=8765))

    def key_provider(path, **kwargs):
        credential_options.append(kwargs)
        return "test-saved-credential"

    def supervisor(config, get_key, **kwargs):
        assert get_key() == "test-saved-credential"
        return SimpleNamespace(start=lambda: None, close=lambda: None, snapshot=lambda: {})

    monkeypatch.setattr("local_files_mcp.credentials.load_key", key_provider)
    monkeypatch.setattr("local_files_mcp.supervisor.Supervisor", supervisor)
    app = SimpleNamespace(project=tmp_path, supervisor=None, operations=operations, closed=threading.Event())
    TrayApplication._worker(app)
    assert credential_options == [{"allow_environment": False}]
    assert app.closed.is_set()
