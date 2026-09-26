"""Exercise the real Tk form with synthetic data; never use a user's key."""

import os

import pytest

from local_files_mcp.credentials import load_key
from local_files_mcp.user_config import is_configured


@pytest.fixture(scope="module")
def tk_root():
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


@pytest.mark.skipif(os.name != "nt", reason="Windows Tk and DPAPI")
def test_setup_form_saves_and_preserves_existing_configuration(tmp_path, monkeypatch, tk_root):
    import tkinter as tk
    from local_files_mcp.setup_dialog import ConfigurationDialog

    monkeypatch.delenv("CONTROL_PLANE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Do not raise a test window above the user's current application.
    monkeypatch.setattr(tk.Toplevel, "lift", lambda *_: None)
    monkeypatch.setattr(tk.Toplevel, "focus_force", lambda *_: None)
    main = tk_root
    readable = tmp_path / "readable"
    readable.mkdir()
    data = tmp_path / "user"
    try:
        dialog = ConfigurationDialog(main, data)
        dialog.window.withdraw()
        dialog.roots.insert("", "end", values=("work", str(readable)))
        dialog.tunnel.set("tunnel_form_test")
        assert dialog.proxy.get() == ""
        dialog.key.set("test-form-runtime-credential")
        dialog.save()
        assert dialog.saved
        assert is_configured(data)
        assert dialog.key.get() == ""

        reopened = ConfigurationDialog(main, data)
        reopened.window.withdraw()
        assert reopened.has_key
        assert reopened.key.get() == ""
        assert reopened.tunnel.get() == "tunnel_form_test"
        assert len(reopened.roots.get_children()) == 1
        reopened.save()
        assert reopened.saved
        assert load_key(data, allow_environment=False) == "test-form-runtime-credential"
    finally:
        for window in main.winfo_children():
            window.destroy()


@pytest.mark.skipif(os.name != "nt", reason="Windows Tk")
def test_cancel_first_run_never_creates_configuration(tmp_path, monkeypatch, tk_root):
    import tkinter as tk
    from local_files_mcp.setup_dialog import ConfigurationDialog

    monkeypatch.setattr(tk.Toplevel, "lift", lambda *_: None)
    monkeypatch.setattr(tk.Toplevel, "focus_force", lambda *_: None)
    main = tk_root
    try:
        dialog = ConfigurationDialog(main, tmp_path / "unused")
        dialog.window.withdraw()
        dialog.key.set("test-cancelled-runtime-credential")
        dialog.cancel()
        assert not dialog.saved
        assert dialog.key.get() == ""
        assert list(tmp_path.iterdir()) == []
    finally:
        for window in main.winfo_children():
            window.destroy()
