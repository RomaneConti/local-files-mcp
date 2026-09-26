from pathlib import Path

import pytest

from local_files_mcp.launcher_config import (
    LauncherConfigError, build_child_environment, build_tunnel_command,
    load_launcher_config,
)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "launcher.toml").write_text(
        "[tunnel]\nid = ''\n[network]\nproxy_url = 'http://127.0.0.1:8080'\n"
        "no_proxy = '127.0.0.1,localhost'\n[ports]\nmcp = 8765\ntunnel = 8766\n",
        encoding="utf-8")
    profile = tmp_path / ".runtime" / "tunnel" / "local-files.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        "config_version: 1\ncontrol_plane:\n  tunnel_id: tunnel_test\n"
        "  api_key: env:CONTROL_PLANE_API_KEY\n"
        "  base_url: https://api.openai.com\n"
        "health:\n  listen_addr: 127.0.0.1:9999\n"
        "mcp:\n  server_urls:\n    - channel: main\n      url: http://127.0.0.1:9998/mcp\n",
        encoding="utf-8",
    )
    return tmp_path


def test_proxy_applies_only_to_control_plane_and_ports_override_legacy(project):
    config = load_launcher_config(project)
    command = build_tunnel_command(config)
    assert command[command.index("--control-plane.http-proxy") + 1] == config.proxy_url
    assert "--http-proxy" not in command
    assert "--mcp.http-proxy" not in command
    assert command[command.index("--health.listen-addr") + 1] == "127.0.0.1:8766"
    assert command[command.index("--mcp.server-url") + 1] == "url=http://127.0.0.1:8765/mcp,channel=main"
    assert command[command.index("--control-plane.api-key") + 1] == "env:CONTROL_PLANE_API_KEY"
    assert "--log.http-raw-unsafe=false" in command
    assert "--log.file=" in command
    assert "--log.file" not in command
    assert "stdout" not in command


def test_inherited_overrides_cannot_redirect_key_or_proxy_local_mcp(project, monkeypatch):
    values = {"HTTPS_PROXY": "http://untrusted.invalid:8", "http_proxy": "http://untrusted.invalid:8",
              "ALL_PROXY": "http://untrusted.invalid:8", "CONTROL_PLANE_BASE_URL": "https://untrusted.invalid",
              "LOG_HTTP_RAW_UNSAFE": "true", "MCP_COMMAND": "execute something",
              "HEALTH_UNIX_SOCKET": "unexpected", "TUNNEL_CLIENT_CONFIG": "unexpected.yaml",
              "OPENAI_API_KEY": "other-test-secret", "PYTHONPATH": "untrusted", "ALLOW_REMOTE_UI": "true"}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    config = load_launcher_config(project)
    environment = build_child_environment(config, "test-runtime-secret")
    assert all(name not in environment for name in values)
    assert environment["NO_PROXY"] == environment["no_proxy"] == "127.0.0.1,localhost"
    assert environment["CONTROL_PLANE_API_KEY"] == "test-runtime-secret"
    assert "CONTROL_PLANE_API_KEY" not in build_child_environment(config, "test-runtime-secret", for_tunnel=False)


@pytest.mark.parametrize("unsafe", [
    "log:\n  http_raw_unsafe: true\n",
    "harpoon:\n  capture_payloads: true\n",
    "http_proxy: http://untrusted.invalid:8\n",
    "health:\n  unix_socket: unexpected\n",
    "mcp:\n  command: [something]\n",
])
def test_unsafe_profile_rejected(project, unsafe):
    profile = project / ".runtime" / "tunnel" / "local-files.yaml"
    profile.write_text("control_plane:\n  tunnel_id: tunnel_test\n" + unsafe, encoding="utf-8")
    with pytest.raises(LauncherConfigError):
        load_launcher_config(project)


def test_plaintext_profile_key_not_reported_in_error(project):
    profile = project / ".runtime" / "tunnel" / "local-files.yaml"
    profile.write_text("control_plane:\n  tunnel_id: tunnel_test\n  api_key: sentinel-secret\n", encoding="utf-8")
    with pytest.raises(LauncherConfigError) as failure:
        load_launcher_config(project)
    assert "sentinel-secret" not in str(failure.value)


@pytest.mark.parametrize("replacement", [
    ("mcp = 8765", "mcp = 8766"),
    ("mcp = 8765", "mcp = 80"),
    ("127.0.0.1,localhost", "localhost"),
    ("http://127.0.0.1:8080", "http://user:secret@127.0.0.1:8080"),
])
def test_invalid_config_rejected_without_echoing_values(project, replacement):
    path = project / "launcher.toml"
    path.write_text(path.read_text().replace(*replacement), encoding="utf-8")
    with pytest.raises(LauncherConfigError) as failure:
        load_launcher_config(project)
    assert "secret@" not in str(failure.value)


def test_explicit_id_works_without_legacy_profile(project):
    (project / ".runtime" / "tunnel" / "local-files.yaml").unlink()
    config = load_launcher_config(project, "tunnel_new")
    assert config.tunnel_id == "tunnel_new"
    assert "--profile-file" not in build_tunnel_command(config, "doctor")


def test_existing_id_cannot_be_silently_replaced(project):
    with pytest.raises(LauncherConfigError):
        load_launcher_config(project, "tunnel_different")


def test_assets_are_separate_from_user_config(project, tmp_path):
    bundle = tmp_path / "application" / "_internal"
    config = load_launcher_config(project, bundle_dir=bundle)
    assert config.project_dir == project
    assert config.tunnel_executable == bundle / "vendor" / "tunnel-client" / "tunnel-client.exe"
    assert build_tunnel_command(config)[0] == str(config.tunnel_executable)


def test_console_redacts_and_discards_huge_lines_before_output(tmp_path, monkeypatch, capsys):
    import io
    from local_files_mcp import windows_process
    from local_files_mcp.launcher_config import _console_run

    key = "test-runtime-sentinel"
    output = f"normal {key}\n".encode() + b"x" * 65530 + key.encode() + b"\nfinal\n"

    class FakeProcess:
        def __init__(self, *args, **kwargs):
            self.stdout = io.BytesIO(output)
            self.closed = False

        def poll(self):
            return 0

        def close(self):
            self.closed = True

    child = FakeProcess()
    monkeypatch.setattr(windows_process, "ManagedProcess", lambda *a, **kw: child)
    assert _console_run(["unused"], {}, key, tmp_path) == 0
    written = capsys.readouterr().out
    assert key not in written
    assert "normal [REDACTED]" in written
    assert "Oversized" in written
    assert "final" in written
    assert child.closed


def test_console_closes_managed_job_on_output_error(tmp_path, monkeypatch):
    import io
    from local_files_mcp import windows_process
    from local_files_mcp.launcher_config import _console_run

    class FakeProcess:
        stdout = io.BytesIO(b"a line\n")
        closed = False

        def close(self):
            self.closed = True

    child = FakeProcess()
    monkeypatch.setattr(windows_process, "ManagedProcess", lambda *a, **kw: child)

    def broken_output(*args, **kwargs):
        raise OSError("output unavailable")

    monkeypatch.setattr("builtins.print", broken_output)
    with pytest.raises(OSError):
        _console_run(["unused"], {}, "test-key", tmp_path)
    assert child.closed
