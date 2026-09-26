"""Small Windows-only helpers for owned, hidden process trees and one tray instance."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import threading
import time

if os.name == "nt":
    import msvcrt
    import pywintypes
    import win32api
    import win32con
    import win32event
    import win32file
    import win32job
    import win32pipe
    import win32process
    import winerror

_launch_lock = threading.Lock()


class WindowsNamedMutex:
    """The kernel releases this handle even when the launcher is forcibly terminated."""

    def __init__(self, name: str):
        self.name = name
        self._handle = None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        handle = win32event.CreateMutex(None, False, self.name)
        if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
            handle.Close()
            return False
        self._handle = handle
        return True

    def close(self) -> None:
        if self._handle is not None:
            self._handle.Close()
            self._handle = None


class ManagedProcess:
    """Create suspended, put in a kill-on-close job, then resume without a console.

    Starting suspended prevents children escaping the job before assignment. The
    job handle is never inherited; closing it or losing the launcher kills the
    whole owned tree. No process is selected by port, name, or an external PID.
    """

    def __init__(self, command: list[str], *, cwd: Path, env: dict[str, str]):
        if os.name != "nt":
            raise OSError("The tray process manager requires Windows")
        self._job = self._process = None
        self.stdout = None
        self.pid = 0
        read_pipe = write_pipe = null_handle = thread_handle = None
        with _launch_lock:
            try:
                self._job = win32job.CreateJobObject(None, "")
                limits = win32job.QueryInformationJobObject(
                    self._job, win32job.JobObjectExtendedLimitInformation)
                limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                win32job.SetInformationJobObject(
                    self._job, win32job.JobObjectExtendedLimitInformation, limits)
                security = pywintypes.SECURITY_ATTRIBUTES()
                security.bInheritHandle = True
                read_pipe, write_pipe = win32pipe.CreatePipe(security, 0)
                win32api.SetHandleInformation(read_pipe, win32con.HANDLE_FLAG_INHERIT, 0)
                null_handle = win32file.CreateFile(
                    "NUL", win32con.GENERIC_READ, win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                    security, win32con.OPEN_EXISTING, 0, None)
                startup = win32process.STARTUPINFO()
                startup.dwFlags = win32con.STARTF_USESTDHANDLES | win32con.STARTF_USESHOWWINDOW
                startup.wShowWindow = win32con.SW_HIDE
                startup.hStdInput = null_handle
                startup.hStdOutput = startup.hStdError = write_pipe
                flags = (win32process.CREATE_NO_WINDOW | win32process.CREATE_SUSPENDED |
                         win32process.CREATE_UNICODE_ENVIRONMENT)
                self._process, thread_handle, self.pid, _ = win32process.CreateProcess(
                    None, subprocess.list2cmdline(command), None, None, True,
                    flags, env, str(cwd), startup)
                win32job.AssignProcessToJobObject(self._job, self._process)
                descriptor = msvcrt.open_osfhandle(read_pipe.Detach(), os.O_RDONLY | os.O_BINARY)
                read_pipe = None
                self.stdout = os.fdopen(descriptor, "rb", buffering=0)
                win32process.ResumeThread(thread_handle)
            except BaseException:
                if self._process is not None:
                    win32process.TerminateProcess(self._process, 1)
                self.close()
                raise
            finally:
                for handle in (read_pipe, write_pipe, null_handle, thread_handle):
                    if handle is not None:
                        handle.Close()

    def poll(self) -> int | None:
        if self._process is None:
            return 0
        if win32event.WaitForSingleObject(self._process, 0) == win32event.WAIT_TIMEOUT:
            return None
        return win32process.GetExitCodeProcess(self._process)

    def stop(self, timeout: float = 5) -> None:
        if self._job is not None:
            job = self._job
            self._job = None
            try:
                win32job.TerminateJobObject(job, 1)
                deadline = time.monotonic() + timeout
                while win32job.QueryInformationJobObject(
                        job, win32job.JobObjectBasicAccountingInformation)["ActiveProcesses"]:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Owned process tree did not exit after job termination")
                    time.sleep(0.02)
            finally:
                # Closing the last job handle is an independent cleanup guarantee.
                job.Close()
        if self._process is not None:
            result = win32event.WaitForSingleObject(self._process, int(timeout * 1000))
            if result == win32event.WAIT_TIMEOUT:
                raise TimeoutError("Owned process did not exit after job termination")

    def close(self) -> None:
        try:
            self.stop()
        finally:
            if self._process is not None:
                self._process.Close()
                self._process = None
            if self.stdout is not None:
                self.stdout.close()
                self.stdout = None
