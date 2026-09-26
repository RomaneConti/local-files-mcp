"""Local Windows events for one tray instance and its optional command-line controls."""

import ctypes
from ctypes import wintypes
import hashlib
from pathlib import Path

ACTIONS = ("show", "restart", "stop", "exit", "credentials", "configure")
_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_kernel.CreateEventW.restype = wintypes.HANDLE
_kernel.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_kernel.OpenEventW.restype = wintypes.HANDLE
_kernel.SetEvent.argtypes = [wintypes.HANDLE]
_kernel.SetEvent.restype = wintypes.BOOL
_kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel.WaitForSingleObject.restype = wintypes.DWORD
_kernel.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel.CloseHandle.restype = wintypes.BOOL


def instance_name(project: Path) -> str:
    tag = hashlib.sha256(str(project.resolve()).casefold().encode("utf-8")).hexdigest()[:24]
    return "Local\\LocalFilesMCP." + tag


def signal(project: Path, action: str) -> bool:
    if action not in ACTIONS:
        raise ValueError("Unknown tray action")
    handle = _kernel.OpenEventW(2, False, instance_name(project) + "." + action)
    if not handle:
        return False
    try:
        return bool(_kernel.SetEvent(handle))
    finally:
        _kernel.CloseHandle(handle)


class TrayEvents:
    def __init__(self, project: Path):
        self.handles = {}
        try:
            for action in ACTIONS:
                handle = _kernel.CreateEventW(None, False, False, instance_name(project) + "." + action)
                if not handle:
                    raise OSError("Cannot create local tray control event")
                self.handles[action] = handle
        except BaseException:
            self.close()
            raise

    def poll(self) -> list[str]:
        return [action for action, handle in self.handles.items()
                if _kernel.WaitForSingleObject(handle, 0) == 0]

    def close(self):
        for handle in self.handles.values():
            _kernel.CloseHandle(handle)
        self.handles.clear()
