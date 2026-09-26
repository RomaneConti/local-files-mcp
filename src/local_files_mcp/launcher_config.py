"""Non-secret desktop configuration and the shared official tunnel command."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import io
import os
from pathlib import Path
import re
import sys
import time
import tomllib
from urllib.parse import urlsplit

import yaml

from .credentials import CredentialError, load_key, prompt_key


class LauncherConfigError(ValueError):
    pass


@dataclass(frozen=True)
class LauncherConfig:
    project_dir: Path
    tunnel_id: str
    proxy_url: str
    mcp_port: int
    tunnel_port: int
    profile_path: Path
    log_dir: Path
    no_proxy: str = "127.0.0.1,localhost"
    bundle_dir: Path | None = None

    @property
    def tunnel_executable(self) -> Path:
        """Read-only application assets are separate from local runtime settings."""
        return (self.bundle_dir or self.project_dir) / "vendor" / "tunnel-client" / "tunnel-client.exe"

    @property
    def mcp_url(self) -> str:
        return f"http://127.0.0.1:{self.mcp_port}/mcp"

    @property
    def admin_url(self) -> str:
        return f"http://127.0.0.1:{self.tunnel_port}/ui"


def _read_profile(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        if path.stat().st_size > 65536:
            raise LauncherConfigError("The tunnel profile is too large.")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise LauncherConfigError("The tunnel profile must be a YAML mapping.")
        return data
    except (OSError, UnicodeError, yaml.YAMLError):
        # YAML exception messages can include file contents, including secrets.
        raise LauncherConfigError("The tunnel profile could not be read as valid YAML.") from None


def _validate_profile(data: dict) -> None:
    """Keep the existing HTTP-only profile; reject hidden alternative transports."""
    fields = {
        "control_plane": {"base_url", "tunnel_id", "api_key", "http_proxy"},
        "health": {"listen_addr", "show_details"},
        "admin_ui": {"open_browser", "log_buffer_events"},
        "log": {"level", "format"},
        "mcp": {"server_urls"},
    }
    if set(data) - (set(fields) | {"config_version"}):
        raise LauncherConfigError("The tunnel profile contains unsupported settings; use the HTTP-only sample profile.")
    for name, allowed in fields.items():
        section = data.get(name, {})
        if not isinstance(section, dict) or set(section) - allowed:
            raise LauncherConfigError("The tunnel profile contains unsupported settings; use the HTTP-only sample profile.")
    control = data.get("control_plane", {})
    base_url = control.get("base_url", "https://api.openai.com")
    if not isinstance(base_url, str) or base_url.rstrip("/") != "https://api.openai.com":
        raise LauncherConfigError("The tunnel control plane must be https://api.openai.com.")
    if control.get("api_key", "env:CONTROL_PLANE_API_KEY") != "env:CONTROL_PLANE_API_KEY":
        raise LauncherConfigError("The tunnel profile API key must be the env:CONTROL_PLANE_API_KEY reference.")
    targets = data.get("mcp", {}).get("server_urls", [])
    if not isinstance(targets, list) or len(targets) > 1:
        raise LauncherConfigError("The tunnel profile must contain at most one local HTTP MCP target.")
    for target in targets:
        if not isinstance(target, dict) or set(target) - {"channel", "url"}:
            raise LauncherConfigError("The tunnel profile contains unsupported MCP target settings.")
        if target.get("channel", "main") != "main":
            raise LauncherConfigError("The tunnel profile MCP channel must be main.")


def _port(value: object) -> int:
    if type(value) is not int or not 1024 <= value <= 65535:
        raise LauncherConfigError("Launcher ports must be integers between 1024 and 65535.")
    return value


def validate_launcher_values(tunnel_id: object, proxy_url: object,
                             mcp_port: object, tunnel_port: object) -> None:
    """Validate form input with the same rules used when launching services."""
    if not isinstance(tunnel_id, str) or not re.fullmatch(r"tunnel_[A-Za-z0-9_-]+", tunnel_id):
        raise LauncherConfigError("Enter a valid OpenAI tunnel ID beginning with tunnel_.")
    if not isinstance(proxy_url, str):
        raise LauncherConfigError("The proxy must be an HTTP(S) URL or empty for direct access.")
    if proxy_url:
        try:
            parsed = urlsplit(proxy_url)
            valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                     and parsed.port is not None and 1 <= parsed.port <= 65535
                     and parsed.username is None and parsed.password is None
                     and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise LauncherConfigError("The HTTP(S) proxy URL must have a port and contain no credentials.")
    if _port(mcp_port) == _port(tunnel_port):
        raise LauncherConfigError("MCP and tunnel ports must be different.")


def load_launcher_config(project: Path, tunnel_id_override: str | None = None,
                         *, bundle_dir: Path | None = None) -> LauncherConfig:
    project = Path(project).resolve()
    try:
        with (project / "launcher.toml").open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        raise LauncherConfigError("Could not read launcher.toml. Open the launcher configuration dialog.") from None
    if set(data) - {"tunnel", "network", "ports"}:
        raise LauncherConfigError("launcher.toml contains an unknown section.")
    for name, keys in {"tunnel": {"id"}, "network": {"proxy_url", "no_proxy"},
                       "ports": {"mcp", "tunnel"}}.items():
        if not isinstance(data.get(name), dict) or set(data[name]) - keys:
            raise LauncherConfigError("launcher.toml contains a missing or unknown setting.")
    profile_path = project / ".runtime" / "tunnel" / "local-files.yaml"
    profile = _read_profile(profile_path)
    _validate_profile(profile)
    tunnel_id = data["tunnel"].get("id") or profile.get("control_plane", {}).get("tunnel_id", "")
    if tunnel_id_override:
        if tunnel_id and tunnel_id != tunnel_id_override:
            raise LauncherConfigError("A different tunnel is already configured. Edit launcher.toml to change it explicitly.")
        tunnel_id = tunnel_id_override
    proxy_url = data["network"].get("proxy_url")
    mcp_port, tunnel_port = data["ports"].get("mcp"), data["ports"].get("tunnel")
    validate_launcher_values(tunnel_id, proxy_url, mcp_port, tunnel_port)
    no_proxy = data["network"].get("no_proxy", "127.0.0.1,localhost")
    if not isinstance(no_proxy, str) or not {"127.0.0.1", "localhost"} <= {
        item.strip().lower() for item in no_proxy.split(",")
    }:
        raise LauncherConfigError("network.no_proxy must include 127.0.0.1 and localhost.")
    return LauncherConfig(project, tunnel_id, proxy_url, mcp_port, tunnel_port,
                          profile_path, project / ".runtime" / "logs", no_proxy,
                          Path(bundle_dir).resolve() if bundle_dir is not None else None)


def build_tunnel_command(config: LauncherConfig, action: str = "run") -> list[str]:
    if action not in {"run", "doctor"}:
        raise LauncherConfigError("Unsupported tunnel action.")
    # Component-only proxy: global --http-proxy would also proxy loopback MCP.
    command = [str(config.tunnel_executable), action]
    if config.profile_path.exists():
        _validate_profile(_read_profile(config.profile_path))
        command += ["--profile-file", str(config.profile_path)]
    command += [
        "--control-plane.base-url", "https://api.openai.com",
        "--control-plane.tunnel-id", config.tunnel_id,
        "--control-plane.api-key", "env:CONTROL_PLANE_API_KEY",
        "--control-plane.http-proxy", config.proxy_url,
        "--health.listen-addr", f"127.0.0.1:{config.tunnel_port}",
        "--mcp.server-url", f"url={config.mcp_url},channel=main",
        # Empty means the OS stdout stream. The string "stdout" is treated as
        # a literal filename by v0.0.15 and would bypass our bounded/redacted log.
        "--log.level", "info", "--log.format", "json", "--log.file=",
        "--log.http-raw-unsafe=false", "--allow-remote-ui=false", "--open-web-ui=false",
    ]
    if action == "doctor":
        command.append("--explain")
    return command


def build_child_environment(config: LauncherConfig, key: str | None = None,
                            for_tunnel: bool = True) -> dict[str, str]:
    prefixes = ("CONTROL_PLANE_", "OPENAI_", "MCP_", "HARPOON_", "CLOUDFLARED_",
                "HEALTH_", "LOG_", "TUNNEL_CLIENT_", "ADMIN_UI_", "PYTHON")
    excluded = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "ALLOW_REMOTE_UI",
                "OPEN_WEB_UI", "PID_FILE", "CA_BUNDLE", "PROXY_CHECK_INTERVAL"}
    environment = {name: value for name, value in os.environ.items()
                   if name.upper() not in excluded and not name.upper().startswith(prefixes)}
    environment.update(NO_PROXY=config.no_proxy, no_proxy=config.no_proxy,
                       PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    if for_tunnel and key:
        environment["CONTROL_PLANE_API_KEY"] = key
    return environment


def _console_run(command: list[str], environment: dict[str, str], key: str,
                 project: Path) -> int:
    from .windows_process import ManagedProcess

    # The same kill-on-close job used by the tray also prevents orphaned tunnel
    # clients when a user closes the older PowerShell entrypoint.
    child = ManagedProcess(command, cwd=project, env=environment)
    try:
        assert child.stdout is not None
        stream = io.BufferedReader(child.stdout)
        while line := stream.readline(65537):
            if len(line) > 65536:
                # Drop oversized lines whole, including their continuation, so a
                # credential split at the size limit cannot leak in fragments.
                while not line.endswith(b"\n"):
                    line = stream.readline(65537)
                    if not line:
                        break
                print("[Oversized tunnel output line omitted]", flush=True)
                continue
            print(line.decode("utf-8", errors="replace").replace(key, "[REDACTED]"),
                  end="", flush=True)
        while (code := child.poll()) is None:
            time.sleep(0.05)
        return code
    except KeyboardInterrupt:
        return 130
    finally:
        child.close()


def main() -> int:
    from .user_config import default_data_dir

    parser = argparse.ArgumentParser(description="Start the official tunnel with the shared local configuration.")
    parser.add_argument("--data-dir", "--project-dir", dest="project_dir", type=Path, default=default_data_dir())
    parser.add_argument("--bundle-dir", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--tunnel-id")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    try:
        config = load_launcher_config(args.project_dir, args.tunnel_id, bundle_dir=args.bundle_dir)
        if args.tunnel_id:
            # Store only this non-secret ID, preserving comments and other settings.
            path = config.project_dir / "launcher.toml"
            content = path.read_text(encoding="utf-8")
            content = re.sub(r"(?m)^id\s*=.*$", f"id = '{config.tunnel_id}'", content, count=1)
            path.write_text(content, encoding="utf-8")
        if not config.tunnel_executable.is_file():
            raise LauncherConfigError("Run scripts/setup-tunnel.ps1 to install the official tunnel client first.")
        key = load_key(config.project_dir) or prompt_key(config.project_dir)
        environment = build_child_environment(config, key)
        code = _console_run(build_tunnel_command(config, "doctor"), environment, key, config.project_dir)
        if code or args.check_only:
            return code
        print(f"Tunnel admin UI: {config.admin_url}. Press Ctrl+C to stop.", flush=True)
        return _console_run(build_tunnel_command(config), environment, key, config.project_dir)
    except (LauncherConfigError, CredentialError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, EOFError, KeyboardInterrupt):
        print("Tunnel startup was cancelled or failed. Check the local configuration and executable.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
