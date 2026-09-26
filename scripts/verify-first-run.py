"""Exercise the real Tk setup form and Windows DPAPI with disposable settings.

No MCP/tunnel process is started and no real credential is read or changed.
Run from a desktop session after installing the project's dependencies.
"""

from pathlib import Path
import tempfile

TEST_OUTPUT = Path(__file__).resolve().parents[1] / ".test-tmp"
TEST_OUTPUT.mkdir(exist_ok=True)
import tkinter as tk
from tkinter import ttk
from unittest.mock import patch

from local_files_mcp.config import load_config
from local_files_mcp.credentials import credential_path, load_key
from local_files_mcp.launcher_config import load_launcher_config
from local_files_mcp.setup_dialog import ConfigurationDialog
from local_files_mcp.user_config import is_configured


def widgets(parent):
    for child in parent.winfo_children():
        yield child
        yield from widgets(child)


def press(dialog, label):
    button = next(child for child in widgets(dialog.window)
                  if isinstance(child, ttk.Button) and child.cget("text") == label)
    button.invoke()


def main():
    root = tk.Tk()
    root.withdraw()
    errors = []
    root.report_callback_exception = lambda *args: errors.append("Tk callback failed")
    try:
        with tempfile.TemporaryDirectory(dir=TEST_OUTPUT, prefix="local-files-mcp-first-run-") as folder:
            base = Path(folder)
            data_dir = base / "settings"
            readable = base / "readable folder"
            readable.mkdir()
            fake_key = "test-only-not-a-real-runtime-credential"

            with patch("local_files_mcp.setup_dialog.messagebox.showerror",
                       side_effect=lambda *args, **kwargs: errors.append("Validation failed")):
                dialog = ConfigurationDialog(root, data_dir)
                root.update()
                assert not dialog.roots.get_children()
                assert dialog.tunnel.get() == dialog.proxy.get() == dialog.key.get() == ""
                assert not dialog.has_key
                # Cancellation clears even unsaved input and leaves no files.
                dialog.key.set(fake_key)
                press(dialog, "取消")
                root.update()
                assert not dialog.saved and dialog.key.get() == ""
                assert not data_dir.exists()
                print("Fresh form is blank; cancelling creates no configuration or credential: PASS")

                dialog = ConfigurationDialog(root, data_dir)
                masked = [child for child in widgets(dialog.window)
                          if isinstance(child, ttk.Entry) and child.cget("show") == "*"]
                assert len(masked) == 1, "The runtime key entry must remain masked"
                dialog.roots.insert("", "end", values=("example", str(readable)))
                dialog.tunnel.set("tunnel_example_test")
                dialog.proxy.set("http://127.0.0.1:8080")
                dialog.key.set(fake_key)
                press(dialog, "保存并启动")
                root.update()
                assert not errors and dialog.saved
                assert dialog.key.get() == ""
                assert is_configured(data_dir)
                assert load_key(data_dir, allow_environment=False) == fake_key
                assert load_config(data_dir / "config.toml").roots == {"example": readable}
                launcher = load_launcher_config(data_dir)
                assert launcher.tunnel_id == "tunnel_example_test"
                assert launcher.proxy_url == "http://127.0.0.1:8080"
                encrypted = credential_path(data_dir).read_bytes()
                for path in data_dir.rglob("*"):
                    if path.is_file():
                        contents = path.read_bytes()
                        assert fake_key.encode("utf-8") not in contents
                        assert fake_key.encode("utf-16-le") not in contents
                print("Real form saves local settings and masked key through DPAPI without plaintext: PASS")

                dialog = ConfigurationDialog(root, data_dir)
                root.update()
                assert dialog.has_key and dialog.key.get() == ""
                assert dialog.tunnel.get() == "tunnel_example_test"
                assert dialog.proxy.get() == "http://127.0.0.1:8080"
                assert [dialog.roots.item(item, "values") for item in dialog.roots.get_children()] == [
                    ("example", str(readable))]
                # Switching to direct access must preserve the encrypted key
                # when the masked input is left empty.
                dialog.proxy.set("")
                press(dialog, "保存并启动")
                root.update()
                assert not errors and dialog.saved
                assert load_launcher_config(data_dir).proxy_url == ""
                assert load_key(data_dir, allow_environment=False) == fake_key
                assert credential_path(data_dir).read_bytes() == encrypted
                assert not list(data_dir.rglob("*.tmp"))
                print("Reopening restores settings, leaves key input blank and preserves saved key: PASS")
        assert not base.exists()
        print("Disposable form, settings and encrypted test credential cleaned up: PASS")
    finally:
        root.destroy()


if __name__ == "__main__":
    main()
