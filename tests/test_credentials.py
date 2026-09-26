import os

import pytest

from local_files_mcp.credentials import (
    CredentialError, credential_path, delete_key, load_key, save_key,
)


@pytest.fixture(autouse=True)
def isolated_credential_environment(monkeypatch):
    monkeypatch.delenv("CONTROL_PLANE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_dpapi_roundtrip_contains_no_plaintext(tmp_path):
    secret = "test-runtime-credential-only-1234567890"
    save_key(tmp_path, secret)
    path = credential_path(tmp_path)
    assert path.is_file()
    assert secret.encode() not in path.read_bytes()
    assert list(path.parent.iterdir()) == [path]
    assert load_key(tmp_path) == secret
    save_key(tmp_path, secret + "-replacement")
    assert load_key(tmp_path) == secret + "-replacement"
    delete_key(tmp_path)
    assert load_key(tmp_path) is None


def test_prefer_official_env_without_persisting(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-fallback")
    assert load_key(tmp_path) == "test-fallback"
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "test-preferred")
    assert load_key(tmp_path) == "test-preferred"
    assert not credential_path(tmp_path).exists()
    assert load_key(tmp_path, allow_environment=False) is None


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_corrupt_ciphertext_raises_secret_free_error(tmp_path):
    path = credential_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"sentinel-secret-corrupt-data")
    with pytest.raises(CredentialError) as failure:
        load_key(tmp_path)
    assert "sentinel-secret" not in str(failure.value)


@pytest.mark.parametrize("secret", ["", "  ", "test-key\ninjection", "x" * 8193],
                         ids=["empty", "whitespace", "newline", "oversized"])
def test_invalid_key_never_reaches_disk(tmp_path, secret):
    with pytest.raises(CredentialError):
        save_key(tmp_path, secret)
    assert not credential_path(tmp_path).exists()
