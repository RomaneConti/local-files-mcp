"""A small, local Tk form for first-run setup and later configuration changes."""

from __future__ import annotations

from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from .config import load_config
from .credentials import CredentialError, load_key
from .launcher_config import load_launcher_config
from .user_config import DEFAULT_SKIPPED, UserConfigError, save_configuration


class ConfigurationDialog:
    def __init__(self, parent, data_dir: Path):
        self.data_dir = data_dir
        self.saved = False
        self.has_key = False
        self.mcp_port, self.tunnel_port = 8765, 8766
        self.skip_directories = DEFAULT_SKIPPED
        self.window = tk.Toplevel(parent)
        self.window.title("Local Files MCP — 本机配置")
        self.window.geometry("740x590")
        self.window.minsize(620, 540)
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(1, weight=1)
        ttk.Label(self.window, text="选择允许只读访问的目录，并填写 OpenAI Tunnel 连接信息。\n"
                  "配置保存在当前项目或应用目录内；密钥使用当前 Windows 用户的 DPAPI 加密。",
                  wraplength=690).grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 10))
        folders = ttk.LabelFrame(self.window, text="允许读取的文件夹（1–32 个，包含子目录）")
        folders.grid(row=1, column=0, sticky="nsew", padx=18, pady=4)
        folders.columnconfigure(0, weight=1)
        folders.rowconfigure(0, weight=1)
        self.roots = ttk.Treeview(folders, columns=("alias", "path"), show="headings", height=6)
        self.roots.heading("alias", text="别名")
        self.roots.heading("path", text="本地目录")
        self.roots.column("alias", width=110, stretch=False)
        self.roots.column("path", width=450)
        self.roots.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        scroll = ttk.Scrollbar(folders, orient="vertical", command=self.roots.yview)
        scroll.grid(row=0, column=1, sticky="ns", padx=(0, 8), pady=8)
        self.roots.configure(yscrollcommand=scroll.set)
        folder_buttons = ttk.Frame(folders)
        folder_buttons.grid(row=1, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 8))
        ttk.Button(folder_buttons, text="添加文件夹…", command=self.add_folder).pack(side="left")
        ttk.Button(folder_buttons, text="移除选中", command=self.remove_folder).pack(side="left", padx=8)

        fields = ttk.Frame(self.window)
        fields.grid(row=2, column=0, sticky="ew", padx=18, pady=10)
        fields.columnconfigure(1, weight=1)
        self.tunnel = tk.StringVar()
        self.proxy = tk.StringVar()
        self.key = tk.StringVar()
        ttk.Label(fields, text="OpenAI Tunnel ID").grid(row=0, column=0, sticky="w", pady=5, padx=(0, 12))
        ttk.Entry(fields, textvariable=self.tunnel).grid(row=0, column=1, sticky="ew", pady=5)
        ttk.Label(fields, text="HTTP / HTTPS proxy").grid(row=1, column=0, sticky="w", pady=5, padx=(0, 12))
        ttk.Entry(fields, textvariable=self.proxy).grid(row=1, column=1, sticky="ew", pady=5)
        ttk.Label(fields, text="可留空以直连；使用代理时填写 http(s)://主机:端口。",
                  wraplength=490).grid(row=2, column=1, sticky="w")
        ttk.Label(fields, text="Runtime API key").grid(row=3, column=0, sticky="w", pady=5, padx=(0, 12))
        ttk.Entry(fields, textvariable=self.key, show="*").grid(row=3, column=1, sticky="ew", pady=5)
        self.key_note = ttk.Label(fields, text="请输入仅用于此 Tunnel 的 runtime key。", wraplength=490)
        self.key_note.grid(row=4, column=1, sticky="w")
        ttk.Label(self.window, text=f"项目 / 应用配置目录：{data_dir}", wraplength=690).grid(
            row=3, column=0, sticky="w", padx=18, pady=8)
        buttons = ttk.Frame(self.window)
        buttons.grid(row=4, column=0, sticky="e", padx=18, pady=(4, 16))
        ttk.Button(buttons, text="取消", command=self.cancel).pack(side="left", padx=8)
        ttk.Button(buttons, text="保存并启动", command=self.save).pack(side="left")
        self._load_existing()
        self.window.grab_set()
        self.window.lift()
        self.window.focus_force()

    def _load_existing(self):
        try:
            config = load_config(self.data_dir / "config.toml")
            for alias, path in config.roots.items():
                self.roots.insert("", "end", values=(alias, str(path)))
            self.skip_directories = tuple(sorted(config.skip_directories))
        except (ValueError, OSError):
            pass
        try:
            launcher = load_launcher_config(self.data_dir)
            self.tunnel.set(launcher.tunnel_id)
            self.proxy.set(launcher.proxy_url)
            self.mcp_port, self.tunnel_port = launcher.mcp_port, launcher.tunnel_port
        except (ValueError, OSError):
            pass
        try:
            self.has_key = load_key(self.data_dir, allow_environment=False) is not None
        except CredentialError:
            pass
        if self.has_key:
            self.key_note.configure(text="已安全保存密钥。留空保留现有密钥；输入新密钥即可更新。")

    def add_folder(self):
        if len(self.roots.get_children()) >= 32:
            messagebox.showerror("目录数量已达上限", "最多允许配置 32 个文件夹。", parent=self.window)
            return
        path = filedialog.askdirectory(title="选择允许 MCP 读取的本地文件夹", parent=self.window, mustexist=True)
        if not path:
            return
        aliases = {self.roots.item(item, "values")[0] for item in self.roots.get_children()}
        index = 1
        while f"folder{index}" in aliases:
            index += 1
        alias = simpledialog.askstring("文件夹别名", "使用字母、数字、下划线或连字符，作为 ChatGPT 调用时的目录名。",
                                       initialvalue=f"folder{index}", parent=self.window)
        if not alias:
            return
        alias = alias.strip()
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", alias) or alias in aliases:
            messagebox.showerror("别名无效", "别名需唯一，且只能包含 1–64 个字母、数字、下划线或连字符。", parent=self.window)
            return
        self.roots.insert("", "end", values=(alias, str(Path(path))))

    def remove_folder(self):
        for item in self.roots.selection():
            self.roots.delete(item)

    def save(self):
        key = self.key.get().strip()
        roots = dict(self.roots.item(item, "values") for item in self.roots.get_children())
        try:
            save_configuration(self.data_dir, roots, self.tunnel.get(), self.proxy.get(),
                               key if key else (None if self.has_key else ""),
                               mcp_port=self.mcp_port, tunnel_port=self.tunnel_port,
                               skip_directories=self.skip_directories)
        except (ValueError, CredentialError, OSError) as exc:
            # Only our controlled validation messages are shown. No traceback or
            # unexpected OS message (potentially containing a value) is logged.
            text = str(exc) if isinstance(exc, (UserConfigError, CredentialError)) else "请检查 Tunnel ID、代理地址、目录和端口配置。"
            messagebox.showerror("配置未保存", text, parent=self.window)
            return
        finally:
            key = None
        self.saved = True
        self.key.set("")
        self.window.destroy()

    def cancel(self):
        self.key.set("")
        self.window.destroy()


def configure(parent, data_dir: Path) -> bool:
    """Display a modal form on the existing Tk main thread; return save success."""
    dialog = ConfigurationDialog(parent, Path(data_dir))
    parent.wait_window(dialog.window)
    return dialog.saved
