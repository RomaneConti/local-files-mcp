"""Windows tray UI. Lifecycle work runs off the UI thread; MCP code stays unchanged."""

import argparse
import json
import os
from pathlib import Path
import queue
import sys
import threading


def project_directory(explicit: Path | None = None) -> Path:
    """Runtime state stays beside the project or standalone application bundle."""
    if explicit:
        return explicit.resolve()
    from .user_config import default_data_dir
    return default_data_dir()


def asset_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parents[2]


def server_command(project: Path, port: int) -> list[str]:
    if getattr(sys, "frozen", False):
        base = [sys.executable, "--mcp-server"]
    else:
        base = [sys.executable, "-B", "-m", "local_files_mcp.server"]
    return base + ["--config", str(project / "config.toml"), "--port", str(port)]


def icon_image(color: str):
    from PIL import Image, ImageDraw
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((5, 7, 59, 57), 11, fill="#1e293b")
    draw.rectangle((16, 21, 45, 43), fill="#e2e8f0")
    draw.rectangle((16, 17, 29, 23), fill="#e2e8f0")
    draw.ellipse((38, 36, 60, 58), fill=color, outline="#ffffff", width=2)
    return image


class TrayApplication:
    def __init__(self, project: Path):
        import tkinter as tk
        import pystray
        from .tray_control import TrayEvents

        self.project = project
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Local Files MCP")
        self.requests = queue.Queue()
        self.operations = queue.Queue()
        self.events = TrayEvents(project)
        self.supervisor = None
        self.config = None
        self.problem = ""
        self.exiting = False
        self.dialog_active = False
        self.closed = threading.Event()
        self.status_window = None
        self.status_text = None
        self.last_serialized = ""
        self.color = ""
        self.title = ""
        self.last_status_summary = ""
        self.snapshot = {"mcp": {"status": "stopped", "ready": False},
                         "tunnel": {"status": "stopped", "ready": False},
                         "proxy": {"status": "unknown", "ready": False}}
        item = pystray.MenuItem
        self.icon = pystray.Icon("LocalFilesMCP", icon_image("#94a3b8"), "Local Files MCP",
            pystray.Menu(
                item("查看当前状态", lambda *_: self.requests.put("show"), default=True),
                item("设置 / 允许目录", lambda *_: self.requests.put("configure")),
                pystray.Menu.SEPARATOR,
                item("打开 tunnel 管理页", lambda *_: self.requests.put("admin")),
                item("打开日志目录", lambda *_: self.requests.put("logs")),
                item("设置 / 更新 runtime key", lambda *_: self.requests.put("credentials")),
                pystray.Menu.SEPARATOR,
                item("重启", lambda *_: self.requests.put("restart")),
                item("停止", lambda *_: self.requests.put("stop")),
                item("退出", lambda *_: self.requests.put("exit"))))
        self.worker = threading.Thread(target=self._worker, name="service-manager", daemon=True)

    def _worker(self):
        from .launcher_config import load_launcher_config
        from .credentials import load_key
        from .supervisor import Supervisor
        try:
            while True:
                try:
                    command = self.operations.get(timeout=3)
                except queue.Empty:
                    command = "refresh"
                try:
                    if command == "exit":
                        break
                    if command in {"start", "restart"}:
                        if self.supervisor:
                            self.supervisor.close()
                        self.config = load_launcher_config(self.project, bundle_dir=asset_directory())
                        self.supervisor = Supervisor(self.config, lambda: load_key(self.project, allow_environment=False),
                            server_command=server_command(self.project, self.config.mcp_port))
                        self.supervisor.start()
                        self.problem = ""
                    elif command == "stop" and self.supervisor:
                        self.supervisor.stop()
                    elif command == "refresh" and self.supervisor:
                        self.supervisor.refresh()
                    if self.supervisor:
                        self.snapshot = self.supervisor.snapshot()
                except Exception as exc:
                    # Exception text can contain configuration values. Do not persist it.
                    self.problem = f"启动器操作失败 ({type(exc).__name__})。请检查配置、凭据或日志。"
                    self.snapshot = {"mcp": {"status": "error", "ready": False},
                                     "tunnel": {"status": "error", "ready": False},
                                     "proxy": {"status": "unknown", "ready": False}}
        finally:
            try:
                if self.supervisor:
                    self.supervisor.close()
                    self.snapshot = self.supervisor.snapshot()
            finally:
                self.closed.set()

    def _credential_prompt(self):
        from .credential_dialog import prompt_key
        saved = prompt_key(self.project, parent=self.root)
        if saved and not self.exiting:
            self.operations.put("restart")
        return saved

    def _configure(self):
        from .setup_dialog import configure
        saved = configure(self.root, self.project)
        if saved and not self.exiting:
            self.operations.put("restart")
        return saved

    def _schedule_dialog(self, action, *, initial=False):
        if self.exiting or self.dialog_active:
            return
        self.dialog_active = True

        def open_dialog():
            try:
                if self.exiting:
                    return
                saved = self._configure() if action == "configure" else self._credential_prompt()
                if initial and not saved and not self.exiting:
                    self.problem = "尚未完成配置；通过托盘‘设置 / 允许目录’继续。"
            except Exception as exc:
                self.problem = f"无法打开设置窗口 ({type(exc).__name__})；请从托盘重试。"
            finally:
                self.dialog_active = False

        # Tk modal dialogs enter wait_window(), a nested event loop. Open only
        # after the current tick has returned and scheduled its next callback,
        # so Stop/Exit and health updates remain responsive while editing.
        self.root.after_idle(open_dialog)

    def _close_dialogs(self):
        import tkinter as tk
        for window in self.root.winfo_children():
            if isinstance(window, tk.Toplevel):
                try:
                    # quit() alone does not end a nested Tk wait_window().
                    window.destroy()
                except tk.TclError:
                    pass

    def _show_status(self):
        import tkinter as tk
        from tkinter import ttk
        if self.status_window and self.status_window.winfo_exists():
            self.status_window.deiconify()
            self.status_window.lift()
            return
        window = self.status_window = tk.Toplevel(self.root)
        window.title("Local Files MCP — 状态")
        window.geometry("680x400")
        window.minsize(570, 330)
        self.status_text = tk.Text(window, wrap="word", padx=16, pady=16, font=("Microsoft YaHei UI", 10))
        self.last_status_summary = ""
        self.status_text.pack(fill="both", expand=True)
        bar = ttk.Frame(window, padding=10)
        bar.pack(fill="x")
        for label, action in (("重启", "restart"), ("停止", "stop"), ("日志", "logs"), ("关闭", "hide")):
            ttk.Button(bar, text=label, command=lambda a=action: self.requests.put(a)).pack(side="left", padx=4)
        window.protocol("WM_DELETE_WINDOW", window.destroy)
        self._update_status_window()

    def _status_summary(self):
        lines = ["Local Files MCP · 只读本地文件", ""]
        for component, title in (("mcp", "MCP Server"), ("tunnel", "OpenAI Tunnel"), ("proxy", "HTTP(S) 代理")):
            data = self.snapshot.get(component, {})
            lines.append(f"{title}: {data.get('status', 'unknown')}")
            if data.get("detail"):
                lines.append(str(data["detail"]))
            if data.get("pid"):
                lines.append(f"PID: {data['pid']}")
            lines.append("")
        if self.problem:
            lines.append(self.problem)
        lines.append("关闭本窗口后继续驻留托盘；使用托盘‘退出’停止本程序启动的所有后台进程。")
        return "\n".join(lines)

    def _update_status_window(self):
        if self.status_window and self.status_window.winfo_exists():
            summary = self._status_summary()
            if summary == self.last_status_summary:
                return
            self.status_text.configure(state="normal")
            self.status_text.delete("1.0", "end")
            self.status_text.insert("1.0", summary)
            self.status_text.configure(state="disabled")
            self.last_status_summary = summary

    def _tick(self):
        # Explorer, a status window, or a file association may disappear between
        # callbacks. One failed UI operation must not disable Stop/Exit polling.
        try:
            self._update_tick()
        except Exception as exc:
            self.problem = f"状态更新失败 ({type(exc).__name__})，启动器仍在运行；将自动重试。"
        finally:
            # Keep polling if the worker finishes after _update_tick checks it.
            # Once quit() runs, mainloop ends and root destruction cancels this.
            try:
                self.root.after(500, self._tick)
            except Exception:
                # The Tk root is being destroyed; run() owns child cleanup.
                pass

    def _update_tick(self):
        import webbrowser
        from .launcher_config import load_launcher_config
        try:
            for action in self.events.poll():
                self.requests.put(action)
        except Exception:
            self.problem = "本地控制事件读取失败；请通过托盘菜单重试操作。"
        while not self.requests.empty():
            action = self.requests.get_nowait()
            if self.exiting:
                continue
            try:
                if action == "show":
                    self._show_status()
                elif action == "hide" and self.status_window:
                    self.status_window.destroy()
                elif action == "admin":
                    config = self.config or load_launcher_config(self.project, bundle_dir=asset_directory())
                    if not webbrowser.open(config.admin_url):
                        self.problem = "无法打开默认浏览器；请检查浏览器设置。"
                elif action == "logs":
                    logs = self.project / ".runtime" / "logs"
                    logs.mkdir(parents=True, exist_ok=True)
                    os.startfile(logs)
                elif action == "credentials":
                    self._schedule_dialog("credentials")
                elif action == "configure":
                    self._schedule_dialog("configure")
                elif action in {"restart", "stop", "exit"}:
                    if action == "exit":
                        self.exiting = True
                        self._close_dialogs()
                    self.operations.put(action)
            except Exception as exc:
                # Never include exception text: it may echo a user-entered key.
                labels = {"show": "打开状态窗口", "hide": "关闭状态窗口", "admin": "打开管理页",
                          "logs": "打开日志目录", "credentials": "保存凭据", "configure": "保存设置",
                          "restart": "重启", "stop": "停止", "exit": "退出"}
                self.problem = f"{labels.get(action, '操作')}失败 ({type(exc).__name__})；请重试。"
        status = self.snapshot
        ready = status["mcp"].get("ready") and status["tunnel"].get("ready")
        stopped = all(status[name].get("status") == "stopped" for name in ("mcp", "tunnel"))
        color = "#22c55e" if ready else "#94a3b8" if stopped else "#f59e0b"
        try:
            if color != self.color:
                self.icon.icon = icon_image(color)
                self.color = color
            title = ("Local Files MCP | MCP: " + status["mcp"].get("status", "?") +
                     " | Tunnel: " + status["tunnel"].get("status", "?"))[:127]
            if title != self.title:
                self.icon.title = title
                self.title = title
            # Keep menu labels static. On Windows, pystray.update_menu()
            # destroys the HMENU even while TrackPopupMenuEx is tracking it,
            # interrupting hover selection. Health polling only updates the
            # tooltip/icon and status window; native menu callbacks refresh
            # their own menu after the popup has closed.
        except Exception:
            self.problem = "托盘图标更新失败；后台服务和控制事件继续运行，将自动重试。"
        try:
            self._update_status_window()
        except Exception:
            self.status_window = self.status_text = None
            self.problem = "状态窗口已不可用；可从托盘菜单重新打开。"
        report = {**status, "launcher_pid": os.getpid(), "exiting": self.exiting, "problem": self.problem}
        serialized = json.dumps(report, ensure_ascii=False)
        if serialized != self.last_serialized:
            try:
                folder = self.project / ".runtime"
                folder.mkdir(parents=True, exist_ok=True)
                temp = folder / "tray-status.tmp"
                temp.write_text(serialized, encoding="utf-8")
                temp.replace(folder / "tray-status.json")
                self.last_serialized = serialized
            except OSError:
                self.problem = "无法写入本地状态文件，请检查 .runtime 目录权限。"
        if self.exiting and self.closed.is_set():
            self.root.quit()

    def run(self):
        from .user_config import is_configured
        tray_thread = threading.Thread(target=self.icon.run, name="tray-icon", daemon=True)
        tray_thread.start()
        self.worker.start()

        def startup():
            if self.exiting:
                return
            if not is_configured(self.project):
                self._schedule_dialog("configure", initial=True)
            else:
                self.operations.put("start")

        self.root.after(250, startup)
        self.root.after(500, self._tick)
        try:
            self.root.mainloop()
        finally:
            if not self.closed.is_set():
                self.operations.put("exit")
                self.closed.wait(20)
            self.icon.stop()
            self.events.close()
            self.root.destroy()


def main():
    # A frozen copy runs the already-tested MCP entrypoint as its child mode.
    if "--mcp-server" in sys.argv:
        sys.argv.remove("--mcp-server")
        from .server import main as server_main
        server_main()
        return
    parser = argparse.ArgumentParser(description="Local Files MCP Windows tray")
    parser.add_argument("--data-dir", "--project-dir", dest="data_dir", type=Path,
                        help="Override project-local configuration storage (advanced/testing)")
    parser.add_argument("--action", choices=["show", "restart", "stop", "exit", "credentials", "configure"])
    args = parser.parse_args()
    from .tray_control import instance_name, signal
    from .windows_process import WindowsNamedMutex
    project = project_directory(args.data_dir)
    if args.action:
        raise SystemExit(0 if signal(project, args.action) else 1)
    mutex = WindowsNamedMutex(instance_name(project))
    if not mutex.acquire():
        signal(project, "show")
        mutex.close()
        return
    try:
        TrayApplication(project).run()
    finally:
        mutex.close()


if __name__ == "__main__":
    main()
