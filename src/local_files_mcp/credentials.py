"""Current-user Windows DPAPI storage for the tunnel runtime credential.

Only DPAPI ciphertext reaches disk. Python strings cannot be reliably erased;
native DPAPI buffers are wiped before release and the key is never logged.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import getpass
import os
from pathlib import Path
import tempfile


class CredentialError(RuntimeError):
    """A credential operation failed; messages never contain the secret."""


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def credential_path(project: Path) -> Path:
    return Path(project) / ".runtime" / "secrets" / "runtime-key.dpapi"


def _validate_key(key: str) -> str:
    key = key.strip()
    if not key or len(key) > 8192 or any(c.isspace() or c == "\0" for c in key):
        raise CredentialError("The runtime API key is empty or has an invalid format.")
    return key


def _crypt(data: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise CredentialError("Saved credentials require Windows DPAPI.")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    raw = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = _Blob(len(data), ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte)))
    result = _Blob()
    try:
        if protect:
            operation = crypt32.CryptProtectData
            operation.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR,
                                  ctypes.POINTER(_Blob), ctypes.c_void_p,
                                  ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
            operation.restype = wintypes.BOOL
            ok = operation(ctypes.byref(source), "Local Files MCP runtime key", None,
                           None, None, 0x1, ctypes.byref(result))
        else:
            operation = crypt32.CryptUnprotectData
            operation.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p,
                                  ctypes.POINTER(_Blob), ctypes.c_void_p,
                                  ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
            operation.restype = wintypes.BOOL
            ok = operation(ctypes.byref(source), None, None, None, None, 0x1,
                           ctypes.byref(result))
        if not ok:
            raise CredentialError("Windows could not protect or unlock the saved runtime key.")
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.memset(raw, 0, len(data))
        if result.pbData:
            ctypes.memset(result.pbData, 0, result.cbData)
            kernel32.LocalFree(result.pbData)


def load_key(project: Path, *, allow_environment: bool = True) -> str | None:
    """Prefer official environment names unless persistent-only lookup is requested."""
    if allow_environment:
        for name in ("CONTROL_PLANE_API_KEY", "OPENAI_API_KEY"):
            if os.environ.get(name):
                return _validate_key(os.environ[name])
    path = credential_path(project)
    try:
        if not path.exists():
            return None
        if path.stat().st_size > 65536:
            raise CredentialError("The saved runtime credential is invalid.")
        return _validate_key(_crypt(path.read_bytes(), protect=False).decode("utf-8"))
    except CredentialError:
        raise
    except (OSError, UnicodeError):
        raise CredentialError("The saved runtime credential could not be read.") from None


def save_key(project: Path, key: str) -> None:
    """Atomically save ciphertext, bound by DPAPI to the current Windows user."""
    encrypted = _crypt(_validate_key(key).encode("utf-8"), protect=True)
    path = credential_path(project)
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix="runtime-key-",
                                         suffix=".dpapi.tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise CredentialError("The encrypted runtime credential could not be saved.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def delete_key(project: Path) -> None:
    try:
        credential_path(project).unlink(missing_ok=True)
    except OSError:
        raise CredentialError("The encrypted runtime credential could not be removed.") from None


def prompt_key(project: Path) -> str:
    """Console fallback used by start-tunnel.ps1; the tray uses a masked dialog."""
    key = _validate_key(getpass.getpass("OpenAI tunnel runtime API key (saved with Windows DPAPI): "))
    save_key(project, key)
    return key
