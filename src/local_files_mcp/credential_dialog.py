"""Local password dialog; secrets never enter command arguments or logs."""

from pathlib import Path
import argparse
import json
import tkinter as tk
from tkinter import messagebox, simpledialog

from .credentials import CredentialError, save_key


def prompt_key(project: Path, parent=None) -> bool:
    own_root = parent is None
    window = parent or tk.Tk()
    if own_root:
        window.withdraw()
    try:
        key = simpledialog.askstring(
            "Local Files MCP — 隧道密钥",
            "请输入 OpenAI Tunnel runtime API key。\n"
            "仅在本机使用 Windows DPAPI 加密保存，不写入配置或日志。\n"
            "请不要将密钥发到聊天里。",
            show="*", parent=window)
        if not key:
            return False
        try:
            save_key(project, key)
        except CredentialError:
            messagebox.showerror("密钥未保存", "Windows 无法安全保存密钥。请检查当前用户权限后重试。", parent=window)
            return False
        finally:
            key = None
        return True
    finally:
        if own_root:
            window.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-dir", required=True, type=Path)
    args = parser.parse_args()
    saved = prompt_key(args.project_dir)
    report = args.project_dir / ".runtime" / "credential-input-status.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"saved": saved}), encoding="utf-8")


if __name__ == "__main__":
    main()
