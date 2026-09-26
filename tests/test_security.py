"""Boundary tests create and alter only pytest-owned temporary fixtures."""

import os
from pathlib import Path
import stat

import pytest

from local_files_mcp.security import PathAccessError, PathPolicy


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows native boundary tests")


@pytest.fixture
def files(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    (allowed / "nested").mkdir()
    (allowed / "nested" / "hello.txt").write_bytes(b"hello\n")
    (outside / "secret.txt").write_bytes(b"outside secret")
    return PathPolicy({"test": allowed}), allowed, outside


def test_normal_read_directory_and_metadata(files):
    policy, allowed, _ = files
    with policy.directory("test", ".") as path:
        assert [entry.name for entry in path.iterdir()] == ["nested"]
    with policy.open_file("test", "nested/hello.txt") as stream:
        assert stream.read(3) == b"hel"
        stream.seek(0)
        assert stream.read() == b"hello\n"
        assert not stream.writable()
        with pytest.raises(OSError):
            os.write(stream.fileno(), b"changed")
    assert stream.closed
    assert policy.stat("test", "nested/hello.txt").st_size == 6
    assert stat.S_ISDIR(policy.stat("test", ".").st_mode)
    assert (allowed / "nested" / "hello.txt").read_bytes() == b"hello\n"


@pytest.mark.parametrize("path", [
    "..", "../outside/secret.txt", "nested/../../outside/secret.txt",
    "nested\\..\\hello.txt", "/outside/secret.txt", "\\outside\\secret.txt",
    "C:\\Windows\\win.ini", "C:Windows\\win.ini", "\\\\server\\share\\secret.txt",
    "\\\\?\\C:\\Windows\\win.ini", "\\\\.\\PhysicalDrive0", "nested/hello.txt:secret",
    "nested/hello.txt::$DATA", "NUL", "nul.txt", "COM1.log", "LPT².txt", "CONOUT$",
    "nested/hello.txt.", "nested/hello.txt ", "nested//hello.txt", "nested/*.txt",
    "nested/hello?.txt", "nested/bad\x00name", "nested/bad\nname", "nested/bad\x85name",
    "nested/<name>", "nested/\"name\"", "nested/pipe|name",
])
def test_forbidden_path_syntax(files, path):
    policy, _, _ = files
    with pytest.raises(PathAccessError):
        with policy.open_file("test", path):
            pytest.fail("Unsafe path was opened")


def test_unknown_root_and_wrong_type_fail_closed(files):
    policy, _, _ = files
    for root, path in [("missing", "nested/hello.txt"), ("test", "nested"), ("test", "."), ("test", "absent")]:
        with pytest.raises(PathAccessError):
            with policy.open_file(root, path):
                pytest.fail("Invalid file access was accepted")
    with pytest.raises(PathAccessError):
        with policy.directory("test", "nested/hello.txt"):
            pytest.fail("File accepted as directory")


def test_hard_link_to_outside_is_rejected(files):
    policy, allowed, outside = files
    os.link(outside / "secret.txt", allowed / "hard.txt")
    for operation in (lambda: policy.stat("test", "hard.txt"),
                      lambda: policy.open_file("test", "hard.txt").__enter__()):
        with pytest.raises(PathAccessError, match="Hard-linked"):
            operation()


def test_junction_to_outside_and_junction_root_are_rejected(files):
    import _winapi

    policy, allowed, outside = files
    junction = allowed / "junction"
    _winapi.CreateJunction(str(outside), str(junction))
    try:
        with pytest.raises(PathAccessError, match="Reparse"):
            with policy.open_file("test", "junction/secret.txt"):
                pytest.fail("Junction was followed")
        with pytest.raises(PathAccessError, match="Reparse"):
            PathPolicy({"bad": junction})
        with pytest.raises(PathAccessError, match="Reparse"):
            PathPolicy({"bad": junction / "subdir"})
    finally:
        # Unlink this fixture junction itself; never traverse/delete its target.
        os.rmdir(junction)
    assert (outside / "secret.txt").read_bytes() == b"outside secret"


def test_symbolic_link_to_outside_is_rejected(files):
    policy, allowed, outside = files
    target = allowed / "symlink.txt"
    try:
        os.symlink(outside / "secret.txt", target)
    except OSError as exc:
        pytest.skip(f"This Windows account cannot create symlinks: {exc.winerror}")
    with pytest.raises(PathAccessError, match="Reparse"):
        with policy.open_file("test", "symlink.txt"):
            pytest.fail("Symlink was followed")


def test_ancestor_and_file_cannot_be_renamed_or_written_during_read(files):
    policy, allowed, _ = files
    folder = allowed / "nested"
    target = folder / "hello.txt"
    with policy.open_file("test", "nested/hello.txt") as stream:
        with pytest.raises(OSError):
            folder.rename(allowed / "renamed")
        with pytest.raises(OSError):
            target.rename(folder / "renamed.txt")
        with pytest.raises(OSError):
            target.open("wb")
        assert stream.read() == b"hello\n"
    # Native handles must be released after the context.
    target.rename(folder / "renamed.txt")
    folder.rename(allowed / "renamed")


def test_directory_context_pins_directory(files):
    policy, allowed, _ = files
    with policy.directory("test", "nested") as path:
        with pytest.raises(OSError):
            (allowed / "nested").rename(allowed / "renamed")
        assert next(path.iterdir()).name == "hello.txt"


def test_read_lock_does_not_prevent_unrelated_child_writes(files):
    policy, allowed, _ = files
    with policy.open_file("test", "nested/hello.txt") as stream:
        unrelated = allowed / "new-file.txt"
        unrelated.write_bytes(b"independent")
        (allowed / "new-directory").mkdir()
        assert stream.read() == b"hello\n"
    assert unrelated.read_bytes() == b"independent"


def test_config_validation(tmp_path):
    for roots in ({}, {"bad/id": tmp_path}, {"bad": Path("relative")},
                  {"bad": Path(r"\\server\share")}, {"bad": tmp_path / ".."}):
        with pytest.raises(PathAccessError):
            PathPolicy(roots)


def test_failed_final_handle_validation_does_not_read(files, monkeypatch):
    import local_files_mcp.security as security

    policy, _, _ = files
    original = security._final_path

    def spoof_final(handle):
        path = original(handle)
        if path.endswith("\\hello.txt"):
            return path.replace("\\allowed\\nested\\", "\\outside\\")
        return path

    monkeypatch.setattr(security, "_final_path", spoof_final)
    with pytest.raises(PathAccessError, match="outside"):
        with policy.open_file("test", "nested/hello.txt"):
            pytest.fail("Final handle containment failure was ignored")
