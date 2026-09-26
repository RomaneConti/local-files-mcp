"""Build safety checks run without downloads, installations or services."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows build")
SUPPORT = Path(__file__).resolve().parents[1] / "scripts" / "build-support.ps1"


def powershell(tmp_path, command, **values):
    environment = dict(os.environ)
    environment.update(BUILD_TEST_ROOT=str(tmp_path), BUILD_TEST_SUPPORT=str(SUPPORT), **values)
    return subprocess.run(
        [shutil.which("powershell.exe") or "powershell.exe", "-NoProfile", "-Command",
         "$ErrorActionPreference='Stop'; . $env:BUILD_TEST_SUPPORT; " + command],
        env=environment, capture_output=True, text=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def test_build_refuses_path_escape(tmp_path):
    result = powershell(tmp_path, "Assert-BuildPath $env:BUILD_TEST_ROOT (Join-Path $env:BUILD_TEST_ROOT '..\\outside')")
    assert result.returncode != 0
    assert "inside the project" in result.stderr


def test_build_refuses_reparse_parent_and_descendant(tmp_path):
    import _winapi
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "output" / "redirect"
    link.parent.mkdir()
    _winapi.CreateJunction(str(real), str(link))
    try:
        for command in (
            "Assert-BuildPath $env:BUILD_TEST_ROOT (Join-Path $env:BUILD_TEST_ROOT 'output\\redirect\\new')",
            "Assert-BuildTree $env:BUILD_TEST_ROOT (Join-Path $env:BUILD_TEST_ROOT 'output')",
        ):
            result = powershell(tmp_path, command)
            assert result.returncode != 0
            assert "reparse" in result.stderr
    finally:
        link.rmdir()


def test_pinned_python_alias_removed_without_touching_interpreter(tmp_path):
    import _winapi
    folder = tmp_path / ".cache" / "python"
    real = folder / "cpython-3.14.6-windows-x86_64-none"
    real.mkdir(parents=True)
    (real / "python.exe").write_bytes(b"preserved interpreter")
    alias = folder / "cpython-3.14-windows-x86_64-none"
    _winapi.CreateJunction(str(real), str(alias))
    try:
        result = powershell(
            tmp_path,
            "Remove-PinnedPythonAlias $env:BUILD_TEST_ROOT '3.14.6' 'amd64'; "
            "Assert-BuildTree $env:BUILD_TEST_ROOT (Join-Path $env:BUILD_TEST_ROOT '.cache\\python')",
        )
        assert result.returncode == 0, result.stderr
        assert not alias.exists()
        assert (real / "python.exe").read_bytes() == b"preserved interpreter"
    finally:
        if alias.exists():
            alias.rmdir()


def test_pinned_python_alias_rejects_other_target(tmp_path):
    import _winapi
    folder = tmp_path / ".cache" / "python"
    (folder / "cpython-3.14.6-windows-x86_64-none").mkdir(parents=True)
    external = tmp_path / "unrelated"
    external.mkdir()
    marker = external / "preserve.txt"
    marker.write_text("preserved", encoding="utf-8")
    alias = folder / "cpython-3.14-windows-x86_64-none"
    _winapi.CreateJunction(str(external), str(alias))
    try:
        result = powershell(tmp_path, "Remove-PinnedPythonAlias $env:BUILD_TEST_ROOT '3.14.6' 'amd64'")
        assert result.returncode != 0
        assert "expected pinned interpreter" in result.stderr
        assert alias.exists()
        assert marker.read_text(encoding="utf-8") == "preserved"
    finally:
        alias.rmdir()


def test_verified_archive_cache_works_without_network(tmp_path):
    archive = tmp_path / ".cache" / "downloads" / "fixture.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"verified fixture bytes")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    result = powershell(
        tmp_path,
        "function Invoke-WebRequest { throw 'Unexpected network request' }; "
        "Get-VerifiedBuildArchive $env:BUILD_TEST_ROOT 'https://example.invalid/fixture.zip' 'fixture.zip' $env:BUILD_TEST_HASH",
        BUILD_TEST_HASH=digest,
    )
    assert result.returncode == 0, result.stderr
    assert str(archive) in result.stdout


def test_download_hash_and_extract_without_module_imports(tmp_path):
    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w") as stream:
        stream.writestr("nested/uv.exe", b"fixture executable bytes")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    result = powershell(
        tmp_path,
        "function Invoke-WebRequest { param($Uri,$OutFile,$UseBasicParsing,$ErrorAction,$Proxy); "
        "[IO.File]::Copy($env:BUILD_TEST_SOURCE,$OutFile) }; "
        "$archive = Get-VerifiedBuildArchive $env:BUILD_TEST_ROOT 'https://example.invalid/uv.zip' 'uv.zip' $env:BUILD_TEST_HASH; "
        "Expand-BuildArchive $env:BUILD_TEST_ROOT $archive (Join-Path $env:BUILD_TEST_ROOT 'output')",
        BUILD_TEST_HASH=digest, BUILD_TEST_SOURCE=str(source),
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "output" / "nested" / "uv.exe").read_bytes() == b"fixture executable bytes"
    assert not list(tmp_path.rglob("*.download"))


def test_archive_traversal_rejected_before_extraction(tmp_path):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("../escape.txt", "unexpected")
    result = powershell(
        tmp_path,
        "Expand-BuildArchive $env:BUILD_TEST_ROOT (Join-Path $env:BUILD_TEST_ROOT 'unsafe.zip') (Join-Path $env:BUILD_TEST_ROOT 'output')",
    )
    assert result.returncode != 0
    assert "Unsafe path" in result.stderr
    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path / "output").exists()
