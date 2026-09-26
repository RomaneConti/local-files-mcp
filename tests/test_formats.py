import io
import json
import struct
import zipfile
import zlib

import numpy as np
import pytest

from local_files_mcp import formats


def npy(array):
    file = io.BytesIO()
    np.save(file, array)
    file.seek(0)
    return file


def npz(**arrays):
    file = io.BytesIO()
    np.savez_compressed(file, **arrays)
    file.seek(0)
    return file


def test_npy_metadata_and_seek_pagination():
    file = npy(np.arange(24, dtype=np.int32).reshape(4, 6))
    metadata = formats.inspect_numpy(file, ".npy")
    assert metadata["shape"] == [4, 6]
    assert metadata["element_count"] == 24
    result = formats.read_numpy(file, ".npy", offset=20, count=3)
    assert result["values"] == [20, 21, 22]
    assert result["next_offset"] == 23
    assert formats.read_numpy(file, ".npy", offset=24)["values"] == []


def test_large_npy_offset_does_not_read_prefix_or_whole_array():
    class CountingFile(io.BytesIO):
        bytes_read = 0

        def read(self, count=-1):
            value = super().read(count)
            self.bytes_read += len(value)
            return value

    file = CountingFile(npy(np.arange(100000, dtype=np.int64)).getvalue())
    assert formats.read_numpy(file, ".npy", offset=99998, count=2)["values"] == [99998, 99999]
    assert file.bytes_read < 1024


def test_fortran_order_sample_is_explicit():
    array = np.asfortranarray([[1, 2, 3], [4, 5, 6]])
    result = formats.read_numpy(npy(array), ".npy")
    assert result["order"] == "F"
    assert result["flattening"] == "storage_order"
    assert result["values"] == [1, 4, 2, 5, 3, 6]


def test_numpy_special_and_structured_values_are_json_safe():
    array = np.array([(3, complex(1, float("nan")), b"hi")], dtype=[("index", "i4"), ("z", "c16"), ("tag", "S4")])
    result = formats.read_numpy(npy(array), ".npy")
    assert result["values"][0] == {"index": 3, "z": {"real": 1.0, "imag": "NaN"}, "tag": {"encoding": "base64", "data": "aGk=", "byte_length": 2, "truncated": False}}
    json.dumps(result, allow_nan=False)
    assert formats.read_numpy(npy(np.array(["2026-09-27"], dtype="datetime64[D]")), ".npy")["values"] == ["2026-09-27"]


def test_extended_precision_float_does_not_silently_downcast():
    if np.dtype(np.longdouble).itemsize <= 8:
        pytest.skip("This NumPy platform has no extended-precision floating dtype")
    value = np.longdouble("1.234567890123456789")
    assert formats.read_numpy(npy(np.array([value])), ".npy")["values"] == [str(value)]


def test_npz_inspection_and_member_selection():
    file = npz(left=np.arange(8), right=np.array([True, False]))
    result = formats.inspect_numpy(file, ".npz")
    assert result["member_count"] == 2
    assert [item["member"] for item in result["members"]] == ["left.npy", "right.npy"]
    with pytest.raises(ValueError, match="Specify a member"):
        formats.read_numpy(file, ".npz")
    assert formats.read_numpy(file, ".npz", member="left", offset=3, count=2)["values"] == [3, 4]
    assert formats.read_numpy(file, ".npz", member="right.npy")["values"] == [True, False]


@pytest.mark.parametrize("suffix", [".npy", ".npz"])
def test_pickle_arrays_rejected(suffix):
    array = np.array([{"untrusted": 1}], dtype=object)
    file = npy(array) if suffix == ".npy" else npz(data=array)
    with pytest.raises(ValueError, match="Object/pickle"):
        formats.inspect_numpy(file, suffix)
    with pytest.raises(ValueError, match="Object/pickle"):
        formats.read_numpy(file, suffix)


def test_truncated_and_giant_headers_rejected():
    file = npy(np.arange(8))
    with pytest.raises(ValueError, match="more array data"):
        formats.inspect_numpy(io.BytesIO(file.getvalue()[:-8]), ".npy")
    oversized_header = b"\x93NUMPY\x02\x00" + struct.pack("<I", formats.MAX_HEADER_BYTES + 1)
    with pytest.raises(ValueError, match="header exceeds"):
        formats.inspect_numpy(io.BytesIO(oversized_header), ".npy")


def test_compressed_prefix_skip_has_budget(monkeypatch):
    file = npz(data=np.zeros(2000, dtype=np.uint8))
    monkeypatch.setattr(formats, "MAX_DECOMPRESSED_BYTES", 1000)
    with pytest.raises(ValueError, match="decompression budget"):
        formats.read_numpy(file, ".npz", offset=1100, count=1)


def test_npz_raw_read_budget_blocks_many_empty_deflate_blocks(monkeypatch):
    payload = npy(np.array([1], dtype=np.uint8)).getvalue()
    compressor = zlib.compressobj(wbits=-15)
    # Each nonfinal stored block produces zero output; 1 MiB of compressed
    # input must be consumed to reach this otherwise ordinary 129-byte NPY.
    compressed = b"\x00\x00\x00\xff\xff" * 200000 + compressor.compress(payload) + compressor.flush()
    name = b"data.npy"
    crc = zlib.crc32(payload)
    local = struct.pack("<4s5H3I2H", b"PK\x03\x04", 20, 0, 8, 0, 0, crc, len(compressed), len(payload), len(name), 0) + name + compressed
    central = struct.pack("<4s6H3I5H2I", b"PK\x01\x02", 20, 20, 0, 8, 0, 0, crc, len(compressed), len(payload), len(name), 0, 0, 0, 0, 0, 0) + name
    end = struct.pack("<4s4H2IH", b"PK\x05\x06", 0, 0, 1, 1, len(central), len(local), 0)
    fixture = local + central + end
    with zipfile.ZipFile(io.BytesIO(fixture)) as archive:
        assert archive.read("data.npy") == payload

    class CountingFile(io.BytesIO):
        bytes_read = 0

        def read(self, count=-1):
            value = super().read(count)
            self.bytes_read += len(value)
            return value

    monkeypatch.setattr(formats, "MAX_ARCHIVE_READ_BYTES", 256 * 1024)
    file = CountingFile(fixture)
    with pytest.raises(ValueError, match="raw input"):
        formats.inspect_numpy(file, ".npz")
    assert file.bytes_read <= 256 * 1024


def test_archive_and_declared_member_limits(monkeypatch):
    file = npz(data=np.arange(100))
    monkeypatch.setattr(formats, "MAX_ARCHIVE_BYTES", 20)
    with pytest.raises(ValueError, match="archive limit"):
        formats.inspect_numpy(file, ".npz")
    monkeypatch.setattr(formats, "MAX_ARCHIVE_BYTES", 1024 * 1024)
    monkeypatch.setattr(formats, "MAX_MEMBER_BYTES", 20)
    with pytest.raises(ValueError, match="declared-size limit"):
        formats.inspect_numpy(file, ".npz")


def test_long_string_values_are_explicit_previews():
    result = formats.read_numpy(npy(np.array(["x" * 3000])), ".npy")
    assert result["values"][0]["length"] == 3000
    assert result["values"][0]["truncated"]
    assert len(result["values"][0]["preview"]) == 2048


def test_zip_directory_limit_is_checked_before_zipfile(monkeypatch):
    file = npz(a=np.arange(2), b=np.arange(2))
    monkeypatch.setattr(formats, "MAX_ARCHIVE_MEMBERS", 1)
    monkeypatch.setattr(formats.zipfile, "ZipFile", lambda *_: pytest.fail("ZipFile called before directory bounds"))
    with pytest.raises(ValueError, match="central directory"):
        formats.inspect_numpy(file, ".npz")


def test_duplicate_members_rejected():
    file = io.BytesIO()
    with pytest.warns(UserWarning), zipfile.ZipFile(file, "w") as archive:
        archive.writestr("data.npy", npy(np.arange(2)).getvalue())
        archive.writestr("data.npy", npy(np.arange(2)).getvalue())
    file.seek(0)
    with pytest.raises(ValueError, match="duplicate"):
        formats.inspect_numpy(file, ".npz")


def test_response_and_sample_bounds(monkeypatch):
    with pytest.raises(ValueError, match="count must"):
        formats.read_numpy(npy(np.arange(3)), ".npy", count=5000)
    with pytest.raises(ValueError, match="offset must"):
        formats.read_numpy(npy(np.arange(3)), ".npy", offset=-1)
    monkeypatch.setattr(formats, "MAX_RESPONSE_BYTES", 64)
    with pytest.raises(ValueError, match="response limit"):
        formats.read_numpy(npy(np.arange(3)), ".npy")


def test_response_limit_stops_serializing_repeated_large_field_names(monkeypatch):
    # The objects share the key string, but a full JSON string would be 246 MB.
    field_name = "f" * 60000
    value = {"values": [{field_name: 1} for _ in range(4096)]}
    original_iterencode = json.JSONEncoder.iterencode
    visited_chunks = 0

    def tracked_iterencode(self, item, **kwargs):
        nonlocal visited_chunks
        for chunk in original_iterencode(self, item, **kwargs):
            visited_chunks += 1
            if visited_chunks > 50:
                pytest.fail("Response guard continued serialization after exceeding its byte budget")
            yield chunk

    monkeypatch.setattr(json.JSONEncoder, "iterencode", tracked_iterencode)
    monkeypatch.setattr(json, "dumps", lambda *_args, **_kwargs: pytest.fail("Response guard allocated the complete JSON string"))
    with pytest.raises(ValueError, match="response limit"):
        formats._response(value)
    assert 0 < visited_chunks <= 50


@pytest.mark.parametrize("use_title", [False, True])
def test_oversized_dtype_field_name_or_title_rejected_before_sampling(monkeypatch, use_title):
    field_name = "f" * 60000
    descriptor = ((field_name, "name"), "u1") if use_title else (field_name, "u1")
    file = npy(np.zeros(4096, dtype=[descriptor]))
    monkeypatch.setattr(np, "frombuffer", lambda *_args, **_kwargs: pytest.fail("Oversized dtype reached sampling"))
    with pytest.raises(ValueError, match="field names/titles"):
        formats.read_numpy(file, ".npy", count=4096)


def test_json_pointer_and_pages():
    content = json.dumps({"a/b": {"~items": [0, 1, 2, 3]}, "name": "hello"}).encode()
    result = formats.read_json(io.BytesIO(content), "/a~1b/~0items", offset=1, limit=2)
    assert result["items"] == [1, 2]
    assert result["next_offset"] == 3
    root = formats.read_json(io.BytesIO(content), limit=1)
    assert root["type"] == "object"
    assert root["items"][0]["key"] == "a/b"
    assert root["next_offset"] == 1


def test_json_size_and_nested_previews(monkeypatch):
    result = formats.read_json(io.BytesIO(json.dumps({"nested": list(range(100))}).encode()))
    assert result["items"][0]["value"]["length"] == 100
    assert result["items"][0]["value"]["truncated"]
    monkeypatch.setattr(formats, "MAX_JSON_BYTES", 10)
    with pytest.raises(ValueError, match="read_text"):
        formats.read_json(io.BytesIO(b" " * 11))


@pytest.mark.parametrize("pointer", ["missing-leading-slash", "/~2", "/a/01", "/a/-", "/a/20"])
def test_invalid_json_pointers(pointer):
    with pytest.raises(ValueError):
        formats.read_json(io.BytesIO(b'{"a":[0,1]}'), pointer)
