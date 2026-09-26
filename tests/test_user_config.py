import os
from pathlib import Path
import sys

import pytest

from local_files_mcp.config import load_config
from local_files_mcp.credentials import CredentialError, credential_path, load_key
from local_files_mcp.launcher_config import LauncherConfigError, load_launcher_config
from local_files_mcp.user_config import default_data_dir, is_configured, save_configuration, UserConfigError


@pytest.fixture(autouse=True)
def no_inherited_credentials(monkeypatch):
    monkeypatch.delenv("CONTROL_PLANE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def test_source_default_stays_in_checkout_and_ignores_user_profile(tmp_path, monkeypatch):
    from local_files_mcp import user_config

    checkout = tmp_path / "source with spaces"
    monkeypatch.setattr(user_config, "__file__", str(checkout / "src" / "local_files_mcp" / "user_config.py"))
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "must-not-use-profile"))
    assert default_data_dir() == checkout / "local-config"
    assert not default_data_dir().exists()


def test_frozen_checkout_uses_project_local_settings(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout with spaces"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text("", encoding="utf-8")
    (checkout / "build.ps1").write_text("", encoding="utf-8")
    executable = checkout / "dist" / "LocalFilesMCP" / "LocalFilesMCP.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "must-not-use-profile"))
    assert default_data_dir() == checkout / "local-config"
    assert not default_data_dir().exists()


def test_standalone_bundle_keeps_settings_next_to_executable(tmp_path, monkeypatch):
    # Keep the test runner's enclosing checkout outside the four-parent search.
    application = tmp_path / "isolated" / "portable" / "standalone application"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(application / "LocalFilesMCP.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "must-not-use-profile"))
    assert default_data_dir() == application / "local-config"
    assert not default_data_dir().exists()


@pytest.mark.parametrize("only_marker", ["pyproject.toml", "build.ps1"])
def test_frozen_checkout_search_requires_both_markers(tmp_path, monkeypatch, only_marker):
    unrelated = tmp_path / "unrelated project"
    unrelated.mkdir()
    (unrelated / only_marker).write_text("", encoding="utf-8")
    executable = unrelated / "application" / "LocalFilesMCP.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    assert default_data_dir() == executable.parent / "local-config"


def test_frozen_checkout_search_is_bounded(tmp_path, monkeypatch):
    checkout = tmp_path / "unrelated project"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text("", encoding="utf-8")
    executable = checkout / "a" / "b" / "c" / "d" / "LocalFilesMCP.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    assert default_data_dir() == executable.parent / "local-config"
    (checkout / "build.ps1").write_text("", encoding="utf-8")
    # The unrelated project is five ancestors above the EXE, outside the search.
    assert default_data_dir() == executable.parent / "local-config"


def test_unconfigured_directory_does_not_create_any_files(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "test-environment-only")
    assert not is_configured(tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows directories and DPAPI")
def test_first_run_roundtrip_and_edit_preserves_encrypted_key(tmp_path):
    data, root = tmp_path / "user", tmp_path / "read only"
    root.mkdir()
    secret = "test-first-run-runtime-key"
    save_configuration(data, {"work": root}, "tunnel_example", "", secret)
    assert is_configured(data)
    assert load_config(data / "config.toml").roots == {"work": root}
    assert load_launcher_config(data).proxy_url == ""
    assert load_key(data) == secret
    encrypted = credential_path(data).read_bytes()
    for path in data.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()
    save_configuration(data, {"work": root}, "tunnel_changed", "https://127.0.0.1:8080", None,
                       mcp_port=9000, tunnel_port=9001)
    assert credential_path(data).read_bytes() == encrypted
    updated = load_launcher_config(data)
    assert updated.mcp_port == 9000
    assert updated.tunnel_id == "tunnel_changed"
    assert updated.no_proxy == "127.0.0.1,localhost"
    assert not list(data.rglob("*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows directories and DPAPI")
@pytest.mark.parametrize("field,value", [
    ("roots", {}), ("roots", {"bad/name": "."}), ("roots", {"work": "."}),
    ("tunnel_id", "not-a-tunnel"), ("proxy_url", "http://user:secret@127.0.0.1:8080"),
    ("mcp_port", 80), ("runtime_key", ""),
])
def test_invalid_first_run_inputs_leave_no_settings(tmp_path, field, value):
    data, root = tmp_path / "user", tmp_path / "readable"
    root.mkdir()
    arguments = dict(data_dir=data, roots={"work": root}, tunnel_id="tunnel_example",
                     proxy_url="", runtime_key="test-never-persisted")
    arguments[field] = value
    with pytest.raises((UserConfigError, LauncherConfigError, CredentialError)):
        save_configuration(**arguments)
    assert not data.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows directories and DPAPI")
def test_environment_credential_does_not_replace_saved_configuration_requirement(tmp_path, monkeypatch):
    data, root = tmp_path / "user", tmp_path / "readable"
    root.mkdir()
    monkeypatch.setenv("OPENAI_API_KEY", "test-environment-only")
    with pytest.raises(CredentialError):
        save_configuration(data, {"work": root}, "tunnel_example", "", None)
    assert not data.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows directories and DPAPI")
def test_invalid_reconfiguration_does_not_replace_previous_settings(tmp_path):
    data, root = tmp_path / "user", tmp_path / "readable"
    root.mkdir()
    save_configuration(data, {"work": root}, "tunnel_example", "", "test-original")
    before = {str(path.relative_to(data)): path.read_bytes() for path in data.rglob("*") if path.is_file()}
    with pytest.raises(LauncherConfigError):
        save_configuration(data, {"work": root}, "tunnel_example", "ftp://invalid:8080", "test-replacement")
    after = {str(path.relative_to(data)): path.read_bytes() for path in data.rglob("*") if path.is_file()}
    assert before == after


@pytest.mark.skipif(os.name != "nt", reason="Windows directories and DPAPI")
def test_corrupt_saved_key_requires_configuration_even_with_valid_env(tmp_path, monkeypatch):
    data, root = tmp_path / "user", tmp_path / "readable"
    root.mkdir()
    save_configuration(data, {"work": root}, "tunnel_example", "", "test-original")
    credential_path(data).write_bytes(b"invalid-ciphertext")
    monkeypatch.setenv("OPENAI_API_KEY", "test-environment-only")
    assert not is_configured(data)


@pytest.mark.skipif(os.name != "nt", reason="Windows directories and DPAPI")
@pytest.mark.parametrize("filename,content", [
    ("config.toml", b"[roots"), ("launcher.toml", b"\xff"),
    ("config.toml", b"roots = []"), ("launcher.toml", b"network = []"),
])
def test_malformed_settings_reopen_setup_without_crashing(tmp_path, filename, content):
    data, root = tmp_path / "user", tmp_path / "readable"
    root.mkdir()
    save_configuration(data, {"work": root}, "tunnel_example", "", "test-original")
    (data / filename).write_bytes(content)
    assert not is_configured(data)


@pytest.mark.skipif(os.name != "nt", reason="Windows native directory junction")
def test_setup_rejects_junction_roots_without_writing_settings_or_target(tmp_path):
    import _winapi

    target = tmp_path / "target"
    target.mkdir()
    marker = target / "unchanged.txt"
    marker.write_bytes(b"preserve target contents")
    junction = tmp_path / "junction"
    data = tmp_path / "user"
    _winapi.CreateJunction(str(target), str(junction))
    try:
        with pytest.raises(UserConfigError):
            save_configuration(data, {"work": junction}, "tunnel_example", "", "test-never-persisted")
        assert not data.exists()
        assert marker.read_bytes() == b"preserve target contents"
    finally:
        # Remove only this fixture link, never recursively traverse its target.
        os.rmdir(junction)
