"""Exercise our real Windows popup menu on an interactive desktop.

No MCP/tunnel processes or credentials are used. This briefly moves the cursor
over this test application's own menu and restores its previous position.
"""

from pathlib import Path
import os
import tempfile

TEST_OUTPUT = Path(__file__).resolve().parents[1] / ".test-tmp"
TEST_OUTPUT.mkdir(exist_ok=True)
import threading
import time

import win32api
import win32con
import win32gui
import win32process

from local_files_mcp.tray import TrayApplication
from pystray import Menu
from pystray._util import win32


def main():
    original_cursor = win32gui.GetCursorPos()
    with tempfile.TemporaryDirectory(dir=TEST_OUTPUT, prefix="local-files-mcp-menu-") as folder:
        app = TrayApplication(Path(folder))
        icon_thread = threading.Thread(target=app.icon.run, daemon=True)
        icon_thread.start()
        app.root.after(500, app._tick)

        def pump_until(predicate, timeout=5):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                app.root.update()
                if predicate():
                    return
                time.sleep(0.02)
            raise AssertionError("Native tray menu check timed out")

        def pause(seconds):
            deadline = time.monotonic() + seconds
            pump_until(lambda: time.monotonic() >= deadline, timeout=seconds + 2)

        def menu_window():
            handles = []

            def inspect(hwnd, _):
                if (win32gui.GetClassName(hwnd) == "#32768" and
                        win32process.GetWindowThreadProcessId(hwnd)[1] == os.getpid()):
                    handles.append(hwnd)

            win32gui.EnumWindows(inspect, None)
            return handles[0] if handles else None

        try:
            pump_until(lambda: app.icon._hwnd and app.icon._menu_handle)
            # Put the menu inside the visible primary monitor, independent of
            # whether Explorer currently hides the test notification icon.
            work = win32api.GetMonitorInfo(win32api.MonitorFromPoint((0, 0)))["Work"]
            win32api.SetCursorPos((work[2] - 100, work[3] - 100))
            win32gui.PostMessage(app.icon._hwnd, win32.WM_NOTIFY, 0, win32.WM_RBUTTONUP)
            pump_until(menu_window)
            menu_handle = app.icon._menu_handle[0]
            app.snapshot = {
                "mcp": {"status": "ready", "ready": True},
                "tunnel": {"status": "connecting", "ready": False},
                "proxy": {"status": "ready", "ready": True},
            }
            positions = [index for index, item in enumerate(app.icon.menu)
                         if item.enabled and item is not Menu.SEPARATOR]
            # Crossing multiple 500 ms UI ticks reproduces the original bug:
            # old code destroys this handle while the popup is still open.
            for position in positions[:3] + positions[-3:-1] + positions[:1]:
                found, rectangle = win32gui.GetMenuItemRect(None, menu_handle, position)
                assert found, "Windows did not expose the popup menu item rectangle"
                left, top, right, bottom = rectangle
                win32api.SetCursorPos(((left + right) // 2, (top + bottom) // 2))
                pump_until(lambda: win32gui.GetMenuState(menu_handle, position, win32con.MF_BYPOSITION)
                           & win32con.MF_HILITE)
                pause(0.6)
                assert app.icon._menu_handle[0] == menu_handle, "Polling replaced an open HMENU"
                assert win32gui.GetMenuState(menu_handle, position, win32con.MF_BYPOSITION) & win32con.MF_HILITE
                app.snapshot["tunnel"] = {"status": "ready", "ready": True}
            print("Native hover follows multiple menu items across status polling: PASS")

            # The final hovered item is the real Show Status callback.
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0)
            pump_until(lambda: app.status_window and app.status_window.winfo_exists())
            assert "MCP Server: ready" in app.status_text.get("1.0", "end")
            assert "OpenAI Tunnel: ready" in app.status_text.get("1.0", "end")
            assert "Tunnel: ready" in app.icon.title
            print("Native click opens status window; updated component states visible: PASS")
        finally:
            if app.icon._menu_hwnd:
                win32gui.PostMessage(app.icon._menu_hwnd, win32con.WM_CANCELMODE, 0, 0)
            app.icon.stop()
            icon_thread.join(5)
            app.events.close()
            app.root.destroy()
            win32api.SetCursorPos(original_cursor)


if __name__ == "__main__":
    main()
