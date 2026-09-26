"""Windows-only, fail-closed access to configured read-only directory trees.

Each request holds native handles for every ancestor, with neither write nor
delete sharing. Reparse points are opened as objects and rejected; reads use
the checked handle itself. Configured roots must be ordinary local paths.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import ctypes
from ctypes import wintypes
import ntpath
import os
from pathlib import Path
import re
from typing import BinaryIO, Iterator
import unicodedata


class PathAccessError(ValueError):
    """The requested path cannot be accessed safely under the allowlist."""


_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])$", re.I)
_INVALID = set('<>:"|?*')


def _relative_parts(path: str) -> tuple[str, ...]:
    if not isinstance(path, str):
        raise PathAccessError("Path must be a root-relative string.")
    if len(path) > 16000 or path.startswith(("/", "\\")) or ntpath.splitdrive(path)[0]:
        raise PathAccessError("Absolute, drive, UNC and device paths are forbidden.")
    if path in ("", "."):
        return ()
    parts = path.replace("\\", "/").split("/")
    for part in parts:
        if part in ("", ".."):
            raise PathAccessError("Empty path components and parent traversal are forbidden.")
        if part == ".":
            continue
        if part.endswith((" ", ".")) or any(unicodedata.category(c) == "Cc" or c in _INVALID for c in part):
            raise PathAccessError("Unsafe Windows path component.")
        if _RESERVED.fullmatch(part.split(".", 1)[0].rstrip(" ")):
            raise PathAccessError("Reserved Windows device names are forbidden.")
    return tuple(part for part in parts if part != ".")


if os.name == "nt":
    import msvcrt

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel.CreateFileW.restype = wintypes.HANDLE
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel.CloseHandle.restype = wintypes.BOOL
    _kernel.GetFileType.argtypes = [wintypes.HANDLE]
    _kernel.GetFileType.restype = wintypes.DWORD
    _kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR,
                                               wintypes.DWORD, wintypes.DWORD]
    _kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD

    class _FileInfo(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("creation", wintypes.FILETIME),
                    ("access", wintypes.FILETIME), ("write", wintypes.FILETIME),
                    ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
                    ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
                    ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]

    _kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FileInfo)]
    _kernel.GetFileInformationByHandle.restype = wintypes.BOOL


def _windows_error() -> PathAccessError:
    return PathAccessError(f"Path is unavailable or cannot be locked safely (Windows error {ctypes.get_last_error()}).")


def _final_path(handle: int) -> str:
    # A volume GUID avoids drive-letter/SUBST remapping during a request.
    buffer = ctypes.create_unicode_buffer(32768)
    length = _kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 1)
    if not length or length >= len(buffer):
        raise _windows_error()
    result = buffer.value
    if not result.startswith("\\\\?\\Volume{"):
        raise PathAccessError("Only local volume paths are supported.")
    return result


class _Handle:
    def __init__(self, value: int, info: _FileInfo):
        self.value: int | None = value
        self.info = info

    def close(self) -> None:
        if self.value is not None:
            _kernel.CloseHandle(self.value)
            self.value = None

    def take_fd(self) -> int:
        # open_osfhandle transfers ownership only when it succeeds.
        fd = msvcrt.open_osfhandle(self.value, os.O_RDONLY | os.O_BINARY)
        self.value = None
        return fd


@contextmanager
def _open_handle(path: str, directory: bool | None) -> Iterator[_Handle]:
    # OPEN_EXISTING, FILE_SHARE_READ, BACKUP_SEMANTICS | OPEN_REPARSE_POINT.
    # GENERIC_READ is needed even for ancestors: metadata-only access does not
    # participate in Windows share-access checks and cannot pin a directory.
    value = _kernel.CreateFileW(path, 0x80000000, 1, None, 3, 0x02200000, None)
    if value == ctypes.c_void_p(-1).value:
        raise _windows_error()
    native = _Handle(value, _FileInfo())
    try:
        if _kernel.GetFileType(value) != 1:
            raise PathAccessError("Only regular disk files and directories are allowed.")
        if not _kernel.GetFileInformationByHandle(value, ctypes.byref(native.info)):
            raise _windows_error()
        if native.info.attributes & 0x400:
            raise PathAccessError("Reparse points, symlinks and junctions are forbidden.")
        is_directory = bool(native.info.attributes & 0x10)
        if directory is not None and is_directory != directory:
            raise PathAccessError("Expected a directory." if directory else "Expected a regular file.")
        if not is_directory and native.info.links != 1:
            raise PathAccessError("Hard-linked files are forbidden.")
        yield native
    finally:
        native.close()


class PathPolicy:
    """Allowlist policy. All callers must use these contexts for filesystem IO.

    directory() yields an internal canonical Path for enumeration only. Its
    protection lasts only inside the context; each child needs its own policy
    call before metadata inspection or reading. A concurrent writer can cause
    a sharing violation; this is intentional and never falls back to open().
    """

    def __init__(self, roots: dict[str, Path]):
        if os.name != "nt":
            raise PathAccessError("This security policy requires Windows.")
        if not roots:
            raise PathAccessError("At least one allowed root is required.")
        self.roots: dict[str, Path] = {}
        self._root_parts: dict[str, tuple[str, tuple[str, ...]]] = {}
        for root_id, root_path in roots.items():
            if not isinstance(root_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", root_id):
                raise PathAccessError("Root IDs may contain letters, digits, underscore and hyphen only.")
            raw = str(root_path).replace("/", "\\")
            drive, tail = ntpath.splitdrive(raw)
            if not re.fullmatch(r"[A-Za-z]:", drive) or not tail.startswith("\\"):
                raise PathAccessError("Configured roots must be absolute local drive paths.")
            components = _relative_parts(tail[1:])
            self.roots[root_id] = Path(drive + "\\", *components)
            self._root_parts[root_id] = (drive + "\\", components)
        # Validate roots and every ancestor now, and again under locks on each use.
        for root_id in self.roots:
            with self.directory(root_id):
                pass

    @contextmanager
    def _target(self, root: str, path: str, directory: bool | None) -> Iterator[tuple[_Handle, Path]]:
        if not isinstance(root, str) or root not in self._root_parts:
            raise PathAccessError("Unknown allowed root ID.")
        requested = _relative_parts(path)
        drive, root_parts = self._root_parts[root]
        try:
            with ExitStack() as stack:
                current = stack.enter_context(_open_handle("\\\\?\\" + drive, True))
                canonical = _final_path(current.value)
                for part in root_parts:
                    current = stack.enter_context(_open_handle(ntpath.join(canonical, part), True))
                    canonical = _final_path(current.value)
                root_canonical = ntpath.normcase(canonical)
                if not requested and directory is False:
                    raise PathAccessError("Expected a regular file.")
                for index, part in enumerate(requested):
                    final = index == len(requested) - 1
                    current = stack.enter_context(_open_handle(
                        ntpath.join(canonical, part), directory if final else True))
                    canonical = _final_path(current.value)
                    if ntpath.commonpath((root_canonical, ntpath.normcase(canonical))) != root_canonical:
                        raise PathAccessError("Final handle is outside the allowed root.")
                yield current, Path(canonical)
        except OSError as exc:
            raise PathAccessError(f"Path could not be accessed safely (OS error {exc.errno}).") from exc

    @contextmanager
    def directory(self, root: str, path: str = ".") -> Iterator[Path]:
        with self._target(root, path, directory=True) as (_, checked_path):
            yield checked_path

    @contextmanager
    def open_file(self, root: str, path: str) -> Iterator[BinaryIO]:
        with self._target(root, path, directory=False) as (native, _):
            fd = native.take_fd()
            try:
                stream = os.fdopen(fd, "rb")
            except BaseException:
                os.close(fd)
                raise
            with stream:
                yield stream

    def stat(self, root: str, path: str) -> os.stat_result:
        with self._target(root, path, directory=None) as (native, _):
            fd = native.take_fd()
            try:
                return os.fstat(fd)
            finally:
                os.close(fd)
