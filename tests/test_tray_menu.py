import queue
import threading
from types import SimpleNamespace

from local_files_mcp.tray import TrayApplication


class StaticMenuIcon:
    def __init__(self):
        self.updates = []

    def __setattr__(self, name, value):
        if name in {"title", "icon"}:
            self.updates.append((name, value))
        object.__setattr__(self, name, value)

    def update_menu(self):
        raise AssertionError("Polling must never destroy the open native menu")


def test_polling_preserves_native_menu_and_updates_changed_status(tmp_path, monkeypatch):
    from local_files_mcp import tray

    monkeypatch.setattr(tray, "icon_image", lambda color: color)
    icon = StaticMenuIcon()
    app = SimpleNamespace(
        events=SimpleNamespace(poll=lambda: []),
        requests=queue.Queue(),
        exiting=False,
        closed=threading.Event(),
        snapshot={"mcp": {"status": "starting", "ready": False},
                  "tunnel": {"status": "starting", "ready": False}},
        color="", title="", icon=icon, problem="", last_serialized="",
        project=tmp_path, _update_status_window=lambda: None,
    )
    for _ in range(10):
        TrayApplication._update_tick(app)
    assert [name for name, _ in icon.updates] == ["icon", "title"]

    app.snapshot = {name: {"status": "ready", "ready": True}
                    for name in ("mcp", "tunnel")}
    TrayApplication._update_tick(app)
    assert icon.title == "Local Files MCP | MCP: ready | Tunnel: ready"
    assert icon.icon == "#22c55e"
    assert [name for name, _ in icon.updates] == ["icon", "title", "icon", "title"]


def test_status_window_redraws_only_when_displayed_text_changes():
    inserted = []
    app = SimpleNamespace(
        status_window=SimpleNamespace(winfo_exists=lambda: True),
        status_text=SimpleNamespace(configure=lambda **_: None,
                                    delete=lambda *_: None,
                                    insert=lambda _, text: inserted.append(text)),
        last_status_summary="",
        _status_summary=lambda: "MCP: ready\nTunnel: connecting",
    )
    for _ in range(10):
        TrayApplication._update_status_window(app)
    assert inserted == ["MCP: ready\nTunnel: connecting"]
    app._status_summary = lambda: "MCP: ready\nTunnel: ready"
    TrayApplication._update_status_window(app)
    assert inserted[-1] == "MCP: ready\nTunnel: ready"
    assert len(inserted) == 2
