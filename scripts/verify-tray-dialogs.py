"""Real Tk modal-dialog Stop/Exit regression; no servers, network or credentials.

Exercises startup setup, later configuration and the masked credential prompt.
Only the service worker is replaced; Windows events, tray polling and Tk modal
loops use the normal production implementations.
"""

from pathlib import Path
import subprocess
import sys
import tempfile

TEST_OUTPUT = Path(__file__).resolve().parents[1] / ".test-tmp"
TEST_OUTPUT.mkdir(exist_ok=True)
import threading
from unittest.mock import patch

from local_files_mcp.tray import TrayApplication
from local_files_mcp.tray_control import signal


def verify(action):
    with tempfile.TemporaryDirectory(dir=TEST_OUTPUT, prefix="local-files-mcp-dialog-") as folder:
        app = TrayApplication(Path(folder))
        observed, errors = [], []

        def worker():
            while True:
                command = app.operations.get()
                observed.append((command, app.dialog_active))
                if command == "exit":
                    app.closed.set()
                    return

        app.worker = threading.Thread(target=worker, daemon=True)
        app.root.report_callback_exception = lambda *args: errors.append("Tk callback failed")

        def require_single_dialog():
            import tkinter as tk
            dialogs = [window for window in app.root.winfo_children() if isinstance(window, tk.Toplevel)]
            if not app.dialog_active or len(dialogs) != 1:
                errors.append("Expected exactly one active modal dialog")

        def timeout():
            errors.append("Stop/Exit failed to finish during the modal dialog")
            app.exiting = True
            app._close_dialogs()
            app.operations.put("exit")
            app.root.quit()

        # First-run setup opens through startup(); other dialogs open through
        # the normal named event and request queue. Repeated requests must not
        # create nested duplicate dialogs or consume Stop/Exit.
        if action != "startup":
            app.root.after(100, lambda: signal(app.project, action))
        repeated = "configure" if action == "startup" else action
        app.root.after(700, lambda: signal(app.project, repeated))
        app.root.after(1200, require_single_dialog)
        app.root.after(1300, lambda: signal(app.project, "stop"))
        app.root.after(1900, lambda: signal(app.project, "exit"))
        app.root.after(6000, timeout)
        with patch("local_files_mcp.user_config.is_configured", return_value=action != "startup"):
            app.run()
        app.worker.join(2)
        assert not errors, errors
        assert ("stop", True) in observed, "Stop was not processed while the dialog was open"
        assert any(command == "exit" for command, _ in observed)
        assert all(command != "restart" for command, _ in observed)
        assert not app.dialog_active and app.closed.is_set()
        assert not (Path(folder) / "config.toml").exists()
        assert not (Path(folder) / ".runtime" / "secrets").exists()
    print(f"{action}: single modal, live Stop, Exit closes dialog, no late restart: PASS")


if __name__ == "__main__":
    if len(sys.argv) == 2:
        verify(sys.argv[1])
    else:
        # Match production's one Tk interpreter per process; pending Tcl
        # callbacks from a destroyed root must not spill into another case.
        for scenario in ("startup", "configure", "credentials"):
            subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), scenario], check=True)
