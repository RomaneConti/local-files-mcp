"""Project-local first-run settings, created only when the application runs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile

from .config import load_config
from .credentials import CredentialError, load_key, save_key
from .launcher_config import load_launcher_config, validate_launcher_values
from .security import PathPolicy

DEFAULT_SKIPPED = (".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache")


class UserConfigError(ValueError):
    """An input or local settings operation failed, without echoing credentials."""


def default_data_dir() -> Path:
    """Keep settings beside the checkout, or beside a standalone EXE bundle.

    A built EXE normally lives in checkout/dist/LocalFilesMCP. Look only a few
    ancestors up for our checkout markers; moving the bundle elsewhere instead
    places its settings beside the EXE. Never fall back to a user-profile path.
    """
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parents[2] / "local-config"
    executable = Path(sys.executable).resolve()
    for candidate in list(executable.parents)[:4]:
        if (candidate / "pyproject.toml").is_file() and (candidate / "build.ps1").is_file():
            return candidate / "local-config"
    return executable.parent / "local-config"


def is_configured(data_dir: Path) -> bool:
    """A fresh EXE asks for setup unless both settings and a saved key are valid."""
    data_dir = Path(data_dir)
    try:
        load_config(data_dir / "config.toml")
        load_launcher_config(data_dir)
        return load_key(data_dir, allow_environment=False) is not None
    except (ValueError, OSError, CredentialError):
        return False


def _atomic_text(path: Path, text: str) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, prefix="settings-", suffix=".tmp",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_configuration(data_dir: Path, roots: dict[str, str | Path], tunnel_id: str,
                       proxy_url: str, runtime_key: str | None, *, mcp_port: int = 8765,
                       tunnel_port: int = 8766,
                       skip_directories: tuple[str, ...] = DEFAULT_SKIPPED) -> None:
    """Validate before writing; each settings file and the DPAPI blob is atomic.

    runtime_key=None preserves the already saved key. Environment credentials do
    not satisfy first-run setup, so the next double-click is also self-contained.
    """
    data_dir = Path(data_dir)
    if not isinstance(roots, dict) or not 1 <= len(roots) <= 32:
        raise UserConfigError("Choose between 1 and 32 folders.")
    checked_roots = {}
    for alias, value in roots.items():
        if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", alias):
            raise UserConfigError("Folder aliases need 1 to 64 letters, digits, underscores or hyphens.")
        if not isinstance(value, (str, Path)) or not Path(value).is_absolute():
            raise UserConfigError("Choose absolute local folder paths.")
        checked_roots[alias] = Path(value)
    # Reuse the server's native read-only validation, including rejection of
    # reparse points, network shares, device paths and unsafe path components.
    try:
        PathPolicy(checked_roots)
    except (ValueError, OSError):
        raise UserConfigError("A folder is unavailable or is not a supported ordinary local folder.") from None
    tunnel_id = tunnel_id.strip() if isinstance(tunnel_id, str) else tunnel_id
    proxy_url = proxy_url.strip() if isinstance(proxy_url, str) else proxy_url
    validate_launcher_values(tunnel_id, proxy_url, mcp_port, tunnel_port)
    if not isinstance(skip_directories, (list, tuple)) or not all(isinstance(x, str) for x in skip_directories):
        raise UserConfigError("Directory skip names must be a list of strings.")
    if runtime_key is None and load_key(data_dir, allow_environment=False) is None:
        raise CredentialError("Enter a runtime API key for first-time setup.")
    # JSON-escaped strings are also TOML basic strings: no executable templates,
    # interpolation, or hand-escaped Windows paths are involved.
    quote = lambda value: json.dumps(str(value), ensure_ascii=False)
    config_text = "# Local user settings. Restart the tray after editing.\n[roots]\n"
    config_text += "".join(f"{alias} = {quote(path)}\n" for alias, path in checked_roots.items())
    config_text += "\n[browse]\nskip_directories = " + json.dumps(list(skip_directories), ensure_ascii=False) + "\n"
    launcher_text = (
        "# Local non-secret launcher settings.\n[tunnel]\n"
        f"id = {quote(tunnel_id)}\n\n[network]\nproxy_url = {quote(proxy_url)}\n"
        'no_proxy = "127.0.0.1,localhost"\n\n[ports]\n'
        f"mcp = {mcp_port}\ntunnel = {tunnel_port}\n"
    )
    try:
        # DPAPI encryption completes before any replacement of configuration.
        # The key is never serialized in either ordinary settings file.
        if runtime_key is not None:
            save_key(data_dir, runtime_key)
        _atomic_text(data_dir / "config.toml", config_text)
        _atomic_text(data_dir / "launcher.toml", launcher_text)
    except OSError:
        raise UserConfigError("Could not save the local user settings. Check directory permissions.") from None
