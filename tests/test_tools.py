import asyncio
import gzip
import io
import json
from pathlib import Path

import numpy as np
import pytest

from local_files_mcp import browse
from local_files_mcp.config import Config, load_config
from local_files_mcp.security import PathPolicy
from local_files_mcp.server import create_server
from local_files_mcp.text import MAX_GZIP_PREFIX, read_window


@pytest.fixture
def fixture_root(tmp_path):
    (tmp_path / "code").mkdir()
    (tmp_path / "code" / "solver.py").write_text("# 中文\ndef solve(x):\n    return x + 1\n", encoding="utf-8")
    (tmp_path / "code" / "fold.py").write_text("ß" * 300 + "TARGET\n", encoding="utf-8")
    (tmp_path / "payload.json").write_text(json.dumps({"values": list(range(10))}), encoding="utf-8")
    (tmp_path / "events.jsonl.gz").write_bytes(gzip.compress(b'{"ok": 1}\n{"ok": 2}\n'))
    np.save(tmp_path / "array.npy", np.arange(12).reshape(3, 4))
    np.savez_compressed(tmp_path / "bundle.npz", sample=np.arange(20))
    return tmp_path


def test_read_windows_no_utf8_loss():
    data = ("中文abc" * 15).encode()
    offset, pieces = 0, []
    while offset is not None:
        result = read_window(io.BytesIO(data), byte_offset=offset, max_bytes=16)
        pieces.append(result["text"])
        offset = result["next_byte_offset"]
    assert "".join(pieces) == data.decode()


def test_gzip_line_and_byte_windows():
    data = gzip.compress(b"alpha\nbeta\ngamma\n")
    assert read_window(io.BytesIO(data), compressed=True, start_line=2)["text"] == "beta\ngamma\n"
    assert read_window(io.BytesIO(data), compressed=True, byte_offset=6)["text"] == "beta\ngamma\n"
    with pytest.raises(ValueError, match="8 MiB"):
        read_window(io.BytesIO(data), compressed=True, byte_offset=MAX_GZIP_PREFIX)


def test_huge_single_line_scan_is_bounded():
    with pytest.raises(ValueError, match="scan budget"):
        read_window(io.BytesIO(b"x" * (MAX_GZIP_PREFIX + 16)), start_line=2)


def test_binary_and_bad_encoding():
    with pytest.raises(ValueError, match="Binary"):
        read_window(io.BytesIO(b"\x00\x01binary"))
    with pytest.raises(ValueError, match="encoding"):
        read_window(io.BytesIO(b"abc"), encoding="unicode_escape")


def test_tools_metadata_and_calls(fixture_root):
    server = create_server(Config({"test": fixture_root}, frozenset({".git"})))

    async def run():
        definitions = await server.list_tools()
        assert {tool.name for tool in definitions} == {
            "list_roots", "list_directory", "find_files", "search_text", "file_info",
            "read_text", "read_json", "inspect_array", "read_array"}
        for tool in definitions:
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.destructive_hint is False
            assert tool.annotations.open_world_hint is False

        async def call(name, **arguments):
            result = await server.call_tool(name, arguments)
            assert not result.is_error
            return result.structured_content

        roots = await call("list_roots")
        assert roots["read_only"]
        listing = await call("list_directory", root="test", limit=2)
        assert len(listing["entries"]) == 2 and listing["next_offset"] == 2
        files = await call("find_files", root="test", glob="*.py")
        assert set(files["paths"]) == {"code/solver.py", "code/fold.py"}
        matches = await call("search_text", root="test", query="def solve")
        assert matches["matches"][0]["line"] == 2
        content = await call("read_text", root="test", path="code/solver.py", start_line=2)
        assert content["text"].startswith("def solve")
        fold = await call("search_text", root="test", query="target")
        assert "TARGET" in fold["matches"][0]["text"]
        document = await call("read_json", root="test", path="payload.json", pointer="/values", limit=2)
        assert document["next_offset"] == 2
        gz = await call("read_text", root="test", path="events.jsonl.gz")
        assert '{"ok": 2}' in gz["text"]
        metadata = await call("inspect_array", root="test", path="array.npy")
        assert "error" not in metadata
        page = await call("read_array", root="test", path="array.npy", offset=4, count=3)
        assert page["values"] == [4, 5, 6]
        archive = await call("read_array", root="test", path="bundle.npz", member="sample", count=3)
        assert archive["values"] == [0, 1, 2]
        for name in ("read_text", "read_json", "read_array", "inspect_array", "file_info", "list_directory"):
            assert "error" in await call(name, root="test", path="../outside")

    asyncio.run(run())


def test_search_unavailable_not_complete(fixture_root, monkeypatch):
    policy = PathPolicy({"test": fixture_root})
    def deny(*args):
        raise OSError("denied")
    monkeypatch.setattr(policy, "open_file", deny)
    result = browse.search_text(policy, frozenset(), "test", "anything")
    assert result["skipped_entries"] > 0
    assert not result["complete_within_scope"]


def test_config_rejects_unknown_fields(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[rootz]\na="x"')
    with pytest.raises(ValueError):
        load_config(config)
