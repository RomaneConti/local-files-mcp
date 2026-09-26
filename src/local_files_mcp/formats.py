"""Bounded data previews over binary handles already approved by PathPolicy.

Nothing here opens a filesystem path, extracts an archive, or enables pickle.
NumPy samples follow the file's storage order (C or Fortran), not always C order.
"""

from __future__ import annotations

import ast
import base64
from contextlib import contextmanager
import io
import json
import math
import struct
from typing import BinaryIO, Iterator
import zipfile
import zlib

import numpy as np


MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_JSON_PAGE = 100
MAX_HEADER_BYTES = 64 * 1024
MAX_SAMPLE_BYTES = 1024 * 1024
MAX_SAMPLE_COUNT = 4096
MAX_RESPONSE_BYTES = 128 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_CENTRAL_DIRECTORY_BYTES = 1024 * 1024
MAX_ARCHIVE_MEMBERS = 128
MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_READ_BYTES = 16 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 16 * 1024 * 1024
MAX_VALUE_CHARACTERS = 2048
MAX_DTYPE_NAME_CHARACTERS = 1024


def _integer(value: int, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _response(value: dict) -> dict:
    size = 0
    encoder = json.JSONEncoder(ensure_ascii=True, allow_nan=False)
    # Checking a completed dumps() string could allocate hundreds of MiB when
    # a long structured-dtype field name is repeated for every sampled record.
    for chunk in encoder.iterencode(value):
        size += len(chunk.encode("utf-8"))
        if size > MAX_RESPONSE_BYTES:
            raise ValueError("Preview exceeds the 128 KiB response limit; request a smaller count/limit or a narrower JSON pointer")
    return value


def _short_string(value: str):
    if len(value) <= MAX_VALUE_CHARACTERS:
        return value
    return {"type": "string", "preview": value[:MAX_VALUE_CHARACTERS], "length": len(value), "truncated": True}


def _float(value: float):
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    return value


def _json_preview(value, depth: int = 0):
    if isinstance(value, str):
        return _short_string(value)
    if isinstance(value, float):
        return _float(value)
    if isinstance(value, (dict, list)):
        size = len(value)
        result = {"type": "object" if isinstance(value, dict) else "array", "length": size}
        if depth >= 3:
            result["truncated"] = size > 0
            return result
        if isinstance(value, dict):
            result["items"] = [
                {"key": _short_string(key), "value": _json_preview(item, depth + 1)}
                for key, item in list(value.items())[:10]
            ]
        else:
            result["items"] = [_json_preview(item, depth + 1) for item in value[:10]]
        result["truncated"] = size > 10
        return result
    return value


def _pointer(document, pointer: str):
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise ValueError("JSON pointer must be empty or start with '/' (RFC 6901)")
    current = document
    if not pointer:
        return current
    for part in pointer[1:].split("/"):
        i = 0
        while i < len(part):
            if part[i] == "~":
                if i + 1 >= len(part) or part[i + 1] not in "01":
                    raise ValueError("Invalid JSON pointer escape; use ~0 for '~' and ~1 for '/'")
                i += 1
            i += 1
        token = part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                raise ValueError(f"JSON pointer key does not exist: {token[:100]!r}")
            current = current[token]
        elif isinstance(current, list):
            if not token.isascii() or not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                raise ValueError("JSON pointer array index must be a nonnegative integer without leading zeroes")
            try:
                index = int(token)
            except ValueError as exc:
                raise ValueError("JSON pointer array index is too large") from exc
            if index >= len(current):
                raise ValueError("JSON pointer array index is out of range")
            current = current[index]
        else:
            raise ValueError("JSON pointer attempts to traverse a scalar")
    return current


def read_json(file: BinaryIO, pointer: str = "", offset: int = 0, limit: int = 50) -> dict:
    """Parse at most 4 MiB; page the selected array/object and summarize children.

    Larger documents should be inspected with the server's bounded text reader.
    ``pointer`` follows RFC 6901. Object pagination preserves file key order.
    """
    _integer(offset, "offset", 0, 2**63 - 1)
    _integer(limit, "limit", 1, MAX_JSON_PAGE)
    content = file.read(MAX_JSON_BYTES + 1)
    if len(content) > MAX_JSON_BYTES:
        raise ValueError("JSON document exceeds 4 MiB; use read_text for bounded line-based access or JSONL")
    try:
        document = json.loads(content.decode("utf-8-sig"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError(f"Invalid or excessively nested UTF-8 JSON: {str(exc)[:200]}") from exc
    selected = _pointer(document, pointer)
    result = {"pointer": pointer, "source_bytes": len(content), "offset": offset}
    if isinstance(selected, (dict, list)):
        length = len(selected)
        result.update(type="object" if isinstance(selected, dict) else "array", length=length)
        if isinstance(selected, dict):
            result["items"] = [
                {"key": _short_string(key), "value": _json_preview(value)}
                for key, value in list(selected.items())[offset:offset + limit]
            ]
        else:
            result["items"] = [_json_preview(value) for value in selected[offset:offset + limit]]
        result["count"] = len(result["items"])
        result["next_offset"] = offset + result["count"] if offset + result["count"] < length else None
    else:
        if offset:
            raise ValueError("offset must be zero when the JSON pointer selects a scalar")
        result.update(type="scalar", value=_json_preview(selected), next_offset=None)
    return _response(result)


def _read_exact(file: BinaryIO, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = file.read(remaining)
        if not chunk:
            raise ValueError("Truncated NumPy data")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _file_size(file: BinaryIO) -> int:
    position = file.tell()
    file.seek(0, io.SEEK_END)
    size = file.tell()
    file.seek(position)
    return size


def _validate_dtype(dtype: np.dtype, depth: int = 0, budget: list[int] | None = None) -> None:
    if budget is None:
        budget = [0]
    budget[0] += 1
    if depth > 16 or budget[0] > 256:
        raise ValueError("NumPy dtype is too deeply nested or has too many fields")
    if dtype.hasobject:
        raise ValueError("Object/pickle NumPy arrays are not supported")
    if not 1 <= dtype.itemsize <= 64 * 1024:
        raise ValueError("NumPy dtype item size must be between 1 and 65536 bytes")
    if dtype.subdtype:
        _validate_dtype(dtype.subdtype[0], depth + 1, budget)
    elif dtype.fields:
        for name in dtype.names or ():
            field = dtype.fields[name]
            title = field[2] if len(field) > 2 else None
            if len(name) > MAX_DTYPE_NAME_CHARACTERS or (title is not None and (not isinstance(title, str) or len(title) > MAX_DTYPE_NAME_CHARACTERS)):
                raise ValueError("NumPy field names/titles must be strings of at most 1024 characters")
            _validate_dtype(field[0], depth + 1, budget)


def _header(file: BinaryIO) -> tuple[dict, np.dtype, int]:
    if _read_exact(file, 6) != b"\x93NUMPY":
        raise ValueError("Not a NumPy .npy file")
    version = tuple(_read_exact(file, 2))
    if version == (1, 0):
        header_size = struct.unpack("<H", _read_exact(file, 2))[0]
        prefix_size = 10
    elif version in ((2, 0), (3, 0)):
        header_size = struct.unpack("<I", _read_exact(file, 4))[0]
        prefix_size = 12
    else:
        raise ValueError(f"Unsupported NumPy header version: {version}")
    if not 1 <= header_size <= MAX_HEADER_BYTES:
        raise ValueError("NumPy header exceeds the 64 KiB limit")
    raw = _read_exact(file, header_size)
    try:
        header = ast.literal_eval(raw.decode("utf-8" if version == (3, 0) else "latin1"))
    except (ValueError, TypeError, OverflowError, SyntaxError, UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid NumPy header") from exc
    if not isinstance(header, dict) or set(header) != {"descr", "fortran_order", "shape"}:
        raise ValueError("Invalid NumPy header fields")
    shape = header["shape"]
    if not isinstance(shape, tuple) or len(shape) > 32:
        raise ValueError("NumPy shape must be a tuple with at most 32 dimensions")
    elements = 1
    for dimension in shape:
        _integer(dimension, "shape dimension", 0, 2**63 - 1)
        elements *= dimension
        if elements > 2**63 - 1:
            raise ValueError("NumPy array dimensions exceed the supported size")
    if not isinstance(header["fortran_order"], bool):
        raise ValueError("Invalid NumPy fortran_order flag")
    try:
        dtype = np.dtype(header["descr"])
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise ValueError("Invalid NumPy dtype") from exc
    _validate_dtype(dtype)
    data_bytes = elements * dtype.itemsize
    if data_bytes > 2**63 - 1:
        raise ValueError("NumPy array byte size exceeds the supported size")
    metadata = {
        "shape": list(shape), "dtype": str(dtype),
        "dtype_descriptor": dtype.descr if dtype.fields else dtype.str,
        "order": "F" if header["fortran_order"] else "C",
        "element_count": elements, "data_bytes": data_bytes,
        "header_version": list(version),
    }
    return metadata, dtype, prefix_size + header_size


class _BudgetReader:
    def __init__(self, file: BinaryIO):
        self.file = file
        self.used = 0

    def read(self, size: int) -> bytes:
        if size < 0 or self.used + size > MAX_DECOMPRESSED_BYTES:
            raise ValueError("NPZ preview exceeds the 16 MiB decompression budget; request an earlier offset or use .npy")
        result = self.file.read(size)
        self.used += len(result)
        return result


class _ArchiveReadBudget:
    """Bound raw ZIP input as well as decoded output, including repeated reads.

    Deflate streams may contain arbitrarily many empty blocks, consuming large
    compressed input while producing almost no bytes for _BudgetReader to count.
    Seeking does not reset this per-operation budget.
    """

    def __init__(self, file: BinaryIO):
        self.file = file
        self.remaining = MAX_ARCHIVE_READ_BYTES

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            raise ValueError("NPZ raw input exceeds the 16 MiB read budget; request a smaller/earlier sample or use .npy")
        size = self.remaining if size < 0 else min(size, self.remaining)
        data = self.file.read(size)
        self.remaining -= len(data)
        return data

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        return self.file.seek(offset, whence)

    def tell(self) -> int:
        return self.file.tell()

    def seekable(self) -> bool:
        return self.file.seekable()


def _check_zip_directory(file: BinaryIO) -> None:
    """Bound zipfile's central-directory allocation before constructing ZipFile."""
    size = _file_size(file)
    if size > MAX_ARCHIVE_BYTES:
        raise ValueError("NPZ archive exceeds the 512 MiB archive limit")
    tail_size = min(size, 65557)
    file.seek(size - tail_size)
    tail = file.read(tail_size)
    end = len(tail)
    location = -1
    while True:
        candidate = tail.rfind(b"PK\x05\x06", 0, end)
        if candidate < 0:
            break
        if candidate + 22 <= len(tail):
            comment_size = struct.unpack_from("<H", tail, candidate + 20)[0]
            if candidate + 22 + comment_size == len(tail):
                location = candidate
                break
        end = candidate
    if location < 0:
        raise ValueError("Invalid NPZ ZIP directory")
    _, disk, start_disk, disk_count, count, directory_size, directory_offset, _ = struct.unpack_from("<4s4H2IH", tail, location)
    absolute_end = size - tail_size + location
    if disk or start_disk or disk_count != count:
        raise ValueError("Multi-disk NPZ archives are not supported")
    if count == 0xFFFF or directory_size == 0xFFFFFFFF or directory_offset == 0xFFFFFFFF:
        raise ValueError("ZIP64 NPZ directories are not supported")
    if absolute_end >= 20:
        file.seek(absolute_end - 20)
        if file.read(4) == b"PK\x06\x07":
            raise ValueError("ZIP64 NPZ directories are not supported")
    if count > MAX_ARCHIVE_MEMBERS or directory_size > MAX_CENTRAL_DIRECTORY_BYTES:
        raise ValueError("NPZ central directory exceeds the 128-member / 1 MiB limit")
    if directory_offset + directory_size != absolute_end:
        raise ValueError("Invalid or unsupported NPZ directory layout")
    file.seek(0)


@contextmanager
def _archive(file: BinaryIO) -> Iterator[tuple[zipfile.ZipFile, list[zipfile.ZipInfo]]]:
    file = _ArchiveReadBudget(file)
    _check_zip_directory(file)
    try:
        with zipfile.ZipFile(file) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ValueError("NPZ archive has too many members")
            if sum(item.file_size for item in members) > MAX_ARCHIVE_EXPANDED_BYTES:
                raise ValueError("NPZ declared uncompressed size exceeds 2 GiB")
            names = set()
            arrays = []
            for item in members:
                if item.filename in names:
                    raise ValueError("NPZ archive has duplicate member names")
                names.add(item.filename)
                if item.flag_bits & 1:
                    raise ValueError("Encrypted NPZ members are not supported")
                if item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise ValueError("Only stored/deflate NPZ members are supported")
                if item.file_size > MAX_MEMBER_BYTES:
                    raise ValueError("NPZ member exceeds the 512 MiB declared-size limit")
                if item.is_dir():
                    continue
                if not item.filename.endswith(".npy"):
                    raise ValueError("NPZ archive contains a non-.npy member")
                arrays.append(item)
            yield archive, arrays
    except (zipfile.BadZipFile, EOFError, RuntimeError, NotImplementedError, zlib.error) as exc:
        raise ValueError(f"Invalid or unsupported NPZ archive: {str(exc)[:200]}") from exc


def _find_member(members: list[zipfile.ZipInfo], member: str | None) -> zipfile.ZipInfo:
    if member is None:
        if len(members) != 1:
            raise ValueError("Specify a member name for an NPZ containing zero or multiple arrays; use inspect_numpy first")
        return members[0]
    exact = [item for item in members if item.filename == member]
    if not exact and not member.endswith(".npy"):
        exact = [item for item in members if item.filename == member + ".npy"]
    if len(exact) != 1:
        raise ValueError("NPZ member does not exist; use inspect_numpy to list member names")
    return exact[0]


def _check_payload(metadata: dict, start: int, file_size: int) -> None:
    if start + metadata["data_bytes"] > file_size:
        raise ValueError("NumPy header declares more array data than the file contains")


def inspect_numpy(file: BinaryIO, suffix: str, member: str | None = None) -> dict:
    """Read headers only; NPZ inspection lists at most 128 bounded members."""
    suffix = suffix.lower()
    if suffix == ".npy":
        if member is not None:
            raise ValueError("member applies only to .npz files")
        file.seek(0)
        metadata, _, start = _header(file)
        _check_payload(metadata, start, _file_size(file))
        return _response({"format": "npy", **metadata})
    if suffix != ".npz":
        raise ValueError("Expected .npy or .npz")
    with _archive(file) as (archive, members):
        selected = [_find_member(members, member)] if member is not None else members
        results = []
        for item in selected:
            with archive.open(item) as source:
                metadata, _, start = _header(_BudgetReader(source))
            _check_payload(metadata, start, item.file_size)
            results.append({"member": item.filename, "compressed_bytes": item.compress_size, "uncompressed_bytes": item.file_size, **metadata})
        return _response({"format": "npz", "member_count": len(members), "members": results})


def _numpy_value(value, budget: list[int]):
    budget[0] += 1
    if budget[0] > 8192:
        raise ValueError("NumPy sample has too many nested values; request a smaller count")
    if isinstance(value, np.ndarray):
        return [_numpy_value(item, budget) for item in value]
    if isinstance(value, np.void):
        if value.dtype.fields:
            return {name: _numpy_value(value[name], budget) for name in value.dtype.names or ()}
        value = value.tobytes()
    if isinstance(value, (np.datetime64, np.timedelta64)):
        return str(value)
    if isinstance(value, (np.complexfloating, complex)):
        return {"real": _numpy_value(value.real, budget), "imag": _numpy_value(value.imag, budget)}
    if isinstance(value, (np.floating, float)):
        if isinstance(value, np.floating) and value.dtype.itemsize > 8:
            return str(value)
        return _float(float(value))
    if isinstance(value, (np.integer, int)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, (np.bytes_, bytes)):
        raw = bytes(value)
        return {"encoding": "base64", "data": base64.b64encode(raw[:MAX_VALUE_CHARACTERS]).decode("ascii"), "byte_length": len(raw), "truncated": len(raw) > MAX_VALUE_CHARACTERS}
    if isinstance(value, (np.str_, str)):
        return _short_string(str(value))
    raise ValueError(f"Unsupported NumPy scalar type: {type(value).__name__}")


def _sample(file: BinaryIO, metadata: dict, dtype: np.dtype, start: int, offset: int, count: int, *, compressed: bool) -> dict:
    total = metadata["element_count"]
    if offset > total:
        raise ValueError("offset exceeds the number of array elements")
    actual_count = min(count, total - offset)
    data_size = actual_count * dtype.itemsize
    if data_size > MAX_SAMPLE_BYTES:
        raise ValueError("NumPy sample exceeds 1 MiB; request a smaller count")
    skip = offset * dtype.itemsize
    if compressed:
        if start + skip + data_size > MAX_DECOMPRESSED_BYTES:
            raise ValueError("NPZ offset/sample exceeds the 16 MiB decompression budget; request an earlier offset or use .npy")
        while skip:
            chunk = min(skip, 64 * 1024)
            _read_exact(file, chunk)
            skip -= chunk
    else:
        file.seek(start + skip)
    raw = _read_exact(file, data_size)
    array = np.frombuffer(raw, dtype=dtype, count=actual_count)
    budget = [0]
    values = [_numpy_value(value, budget) for value in array]
    return {
        **metadata, "flattening": "storage_order", "offset": offset,
        "count": actual_count, "values": values,
        "next_offset": offset + actual_count if offset + actual_count < total else None,
        "nonfinite_float_encoding": "NaN, Infinity, -Infinity are strings",
        "extended_precision_float_encoding": "Floating components wider than 64 bits are strings",
    }


def read_numpy(file: BinaryIO, suffix: str, member: str | None = None, offset: int = 0, count: int = 100) -> dict:
    """Read a bounded flattened sample without loading the complete array.

    .npy uses direct seeks. .npz decompresses the prefix sequentially, refusing
    offsets that would consume more than 16 MiB including header and sample.
    """
    _integer(offset, "offset", 0, 2**63 - 1)
    _integer(count, "count", 1, MAX_SAMPLE_COUNT)
    suffix = suffix.lower()
    if suffix == ".npy":
        if member is not None:
            raise ValueError("member applies only to .npz files")
        file.seek(0)
        metadata, dtype, start = _header(file)
        _check_payload(metadata, start, _file_size(file))
        return _response({"format": "npy", **_sample(file, metadata, dtype, start, offset, count, compressed=False)})
    if suffix != ".npz":
        raise ValueError("Expected .npy or .npz")
    with _archive(file) as (archive, members):
        selected = _find_member(members, member)
        with archive.open(selected) as source:
            reader = _BudgetReader(source)
            metadata, dtype, start = _header(reader)
            _check_payload(metadata, start, selected.file_size)
            return _response({"format": "npz", "member": selected.filename, **_sample(reader, metadata, dtype, start, offset, count, compressed=True)})
