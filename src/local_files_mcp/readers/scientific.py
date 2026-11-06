"""Bounded HDF5 and MAT previews over already-approved read-only handles.

HDF5 never follows soft/external links, external storage, virtual datasets, or
references. Dynamic HDF5 plugins are disabled before importing its library.
Classic MAT numeric arrays are parsed directly: no object deserialization or
whole-variable scipy.loadmat call. Compressed MAT payloads have a strict cap.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
import io
import itertools
import math
import os
import struct
import threading
from typing import BinaryIO
import zlib

import numpy as np

from ..formats import _numpy_value
from .common import integer as _integer, response as _response


MAX_MEMBERS = 256
MAX_PAGE = 100
MAX_NAME = 1024
MAX_DEPTH = 32
MAX_ELEMENTS = 4096
MAX_SAMPLE_BYTES = 1024 * 1024
MAX_READ_BYTES = 32 * 1024 * 1024
MAX_CHUNK_BYTES = 8 * 1024 * 1024
MAX_DECODE_BYTES = 32 * 1024 * 1024
MAX_MAT_EXPANDED = 16 * 1024 * 1024
HDF_SIGNATURE = b"\x89HDF\r\n\x1a\n"
_HDF_INITIALIZATION_LOCK = threading.Lock()
_BUILTIN_FILTERS = {1, 2, 3, 4, 5, 6, 32000}

# The value is documented by HDF Group as disabling every dynamic plugin type.
# Do not use setdefault: an inherited plugin configuration is not trusted.
os.environ["HDF5_PLUGIN_PRELOAD"] = "::"


class _ReadBudget:
    """File-object driver with no filename/fileno and no writable operations."""

    def __init__(self, stream: BinaryIO):
        self.stream = stream
        self.remaining = MAX_READ_BYTES

    def read(self, size=-1):
        if size < 0 or size > self.remaining:
            raise ValueError("Scientific-file read exceeds the 32 MiB per-request budget")
        value = self.stream.read(size)
        self.remaining -= len(value)
        return value

    def readinto(self, buffer):
        value = self.read(len(buffer))
        buffer[:len(value)] = value
        return len(value)

    def seek(self, offset, whence=0):
        return self.stream.seek(offset, whence)

    def tell(self):
        return self.stream.tell()

    def flush(self):
        pass


def _size(stream):
    position = stream.tell()
    stream.seek(0, 2)
    size = stream.tell()
    stream.seek(position)
    return size


def _exact(stream, size):
    if size < 0 or size > MAX_READ_BYTES:
        raise ValueError("Scientific-file element exceeds the read budget")
    data = stream.read(size)
    if len(data) != size:
        raise ValueError("Truncated scientific data file")
    return data


def _hdf_signature(stream):
    for position in (0, *(2 ** power for power in range(9, 17))):
        stream.seek(position)
        if stream.read(8) == HDF_SIGNATURE:
            return True
    return False


@contextmanager
def _hdf_file(stream):
    # h5py may have been imported earlier by an unrelated parser/test. Empty the
    # plugin search list too; only built-in filter IDs are accepted below.
    os.environ["HDF5_PLUGIN_PRELOAD"] = "::"
    import h5py

    with _HDF_INITIALIZATION_LOCK:
        while h5py.h5pl.size():
            h5py.h5pl.remove(0)
    stream.seek(0)
    try:
        with h5py.File(_ReadBudget(stream), "r", driver="fileobj", rdcc_nbytes=MAX_CHUNK_BYTES) as file:
            yield file
    except (OSError, RuntimeError, KeyError, TypeError) as exc:
        # HDF5 errors can quote external filenames stored in malicious files.
        raise ValueError("Invalid or unsupported HDF5 data; external references and unsafe storage are not read") from None


def _components(selector):
    if not isinstance(selector, str) or len(selector) > MAX_NAME or "\0" in selector:
        raise ValueError("HDF5 selector must be a short dataset/group path")
    if selector in {"", "/"}:
        return []
    parts = selector.lstrip("/").split("/")
    if len(parts) > MAX_DEPTH or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Invalid or excessively deep HDF5 selector")
    return parts


def _hard_object(group, name):
    """Inspect each link before opening; reject unsafe storage before shape IO."""
    import h5py

    link = group.get(name, getlink=True)
    if not isinstance(link, h5py.HardLink):
        raise ValueError("HDF5 soft/external links are not followed")
    object_id = h5py.h5o.open(group.id, name.encode("utf-8"))
    kind = h5py.h5i.get_type(object_id)
    if kind == h5py.h5i.DATASET:
        properties = object_id.get_create_plist()
        try:
            # Getting a virtual dataset's extent can open its source files, so
            # check layout before constructing the high-level Dataset wrapper.
            if properties.get_layout() == h5py.h5d.VIRTUAL:
                object_id.close()
                raise ValueError("HDF5 virtual datasets are not read")
            if properties.get_external_count():
                object_id.close()
                raise ValueError("HDF5 external dataset storage is not read")
        finally:
            properties.close()
        return h5py.Dataset(object_id)
    if kind == h5py.h5i.GROUP:
        return h5py.Group(object_id)
    object_id.close()
    raise ValueError("Only HDF5 groups and datasets are supported")


def _select_hdf(file, selector):
    import h5py

    current = file
    for part in _components(selector):
        if not isinstance(current, h5py.Group):
            raise ValueError("HDF5 selector traverses a non-group")
        current = _hard_object(current, part)
    return current


def _dtype_supported(dtype, depth=0):
    if dtype.hasobject or dtype.itemsize > 4096 or depth > 4:
        return False
    if dtype.fields:
        if len(dtype.names) > 32:
            return False
        return all(len(name) <= 128 and _dtype_supported(dtype.fields[name][0], depth + 1)
                   for name in dtype.names)
    if dtype.subdtype:
        scalar, shape = dtype.subdtype
        return math.prod(shape) <= MAX_ELEMENTS and _dtype_supported(scalar, depth + 1)
    return dtype.kind in "biufcS" and dtype.itemsize > 0


def _hdf_metadata(dataset, name):
    dtype, shape = dataset.dtype, dataset.shape
    with closing(dataset.id.get_create_plist()) as properties:
        filters = [properties.get_filter(index)[0] for index in range(properties.get_nfilters())]
    chunks = dataset.chunks
    readable = (_dtype_supported(dtype) and set(filters) <= _BUILTIN_FILTERS
                and (not chunks or math.prod(chunks) * dtype.itemsize <= MAX_CHUNK_BYTES))
    return {"member": name, "kind": "dataset", "shape": list(shape) if shape is not None else None,
            "dtype": str(dtype)[:2048], "element_count": math.prod(shape) if shape is not None else 0,
            "order": "C", "chunks": list(chunks) if chunks else None, "filters": filters,
            "readable": readable, "attributes_read": False,
            "note": "Variable-length/reference values, third-party filters, and chunks above 8 MiB are not read"}


def _inspect_hdf(stream, suffix, selector, offset, limit):
    import h5py

    with _hdf_file(stream) as file:
        selected = _select_hdf(file, selector)
        format_name = "mat-v7.3" if suffix == ".mat" else "hdf5"
        if isinstance(selected, h5py.Dataset):
            return _response({"format": format_name, **_hdf_metadata(selected, selector)})
        total = len(selected)
        if offset > total:
            raise ValueError("offset exceeds the HDF5 group's member count")
        members = []
        # No recursive walk: hard-link cycles remain ordinary paged group items.
        for name in itertools.islice(selected.keys(), offset, offset + limit):
            if len(name) > MAX_NAME:
                raise ValueError("HDF5 member name exceeds the metadata limit")
            full_name = selector.rstrip("/") + "/" + name
            link = selected.get(name, getlink=True)
            if not isinstance(link, h5py.HardLink):
                members.append({"member": full_name, "kind": "link", "readable": False,
                                "reason": "Soft/external links are not followed"})
                continue
            try:
                obj = _hard_object(selected, name)
            except ValueError as exc:
                members.append({"member": full_name, "kind": "unsupported", "readable": False,
                                "reason": str(exc)})
                continue
            if isinstance(obj, h5py.Dataset):
                members.append(_hdf_metadata(obj, full_name))
            else:
                members.append({"member": full_name, "kind": "group", "member_count": len(obj)})
        following = offset + len(members)
        return _response({"format": format_name, "selector": selector or "/", "kind": "group",
                          "member_count": total, "members": members, "offset": offset,
                          "returned": len(members), "next_offset": following if following < total else None,
                          "dimension_order": "HDF5 storage axes; MAT v7.3 may reverse MATLAB axes"})


def _indices(flat, dimensions, order):
    axes = range(len(dimensions) - 1, -1, -1) if order == "C" else range(len(dimensions))
    result = [0] * len(dimensions)
    for axis in axes:
        result[axis] = flat % dimensions[axis]
        flat //= dimensions[axis]
    return tuple(result)


def _selection(dimensions, offset, count, start, shape):
    total = math.prod(dimensions)
    if start is None and shape is None:
        if offset > total:
            raise ValueError("offset exceeds the array's element count")
        return min(count, total - offset), None
    if offset:
        raise ValueError("Use either flat offset/count or start/shape, not both")
    if not isinstance(start, (list, tuple)) or not isinstance(shape, (list, tuple)):
        raise ValueError("A rectangular selection requires both start and shape lists")
    if len(start) != len(dimensions) or len(shape) != len(dimensions):
        raise ValueError("start and shape must match the array's number of dimensions")
    for begin, length, bound in zip(start, shape, dimensions):
        _integer(begin, "start", 0, 2**63 - 1)
        _integer(length, "shape", 0, 2**63 - 1)
        if begin > bound or length > bound - begin:
            raise ValueError("Rectangular selection exceeds array bounds")
    actual = math.prod(shape)
    if actual > MAX_ELEMENTS:
        raise ValueError("Array selection exceeds the 4096-element sample limit")
    return actual, tuple(slice(begin, begin + length) for begin, length in zip(start, shape))


def _check_hdf_reads(dataset, actual, offset, selection, start, shape):
    dtype = dataset.dtype
    if not _dtype_supported(dtype):
        raise ValueError("HDF5 object/reference, variable-length, or oversized values are not read")
    if actual * dtype.itemsize > MAX_SAMPLE_BYTES:
        raise ValueError("Array sample exceeds 1 MiB; request a smaller count or shape")
    properties = dataset.id.get_create_plist()
    try:
        # Deflate, shuffle, Fletcher32, SZIP, N-bit, scale-offset are HDF5
        # built-ins; LZF is statically registered by the bundled h5py module.
        if any(properties.get_filter(index)[0] not in _BUILTIN_FILTERS
               for index in range(properties.get_nfilters())):
            raise ValueError("HDF5 third-party filter plugins are not allowed")
    finally:
        properties.close()
    chunks = dataset.chunks
    if chunks and actual:
        chunk_bytes = math.prod(chunks) * dtype.itemsize
        if chunk_bytes > MAX_CHUNK_BYTES:
            raise ValueError("HDF5 chunk exceeds the 8 MiB decompression limit")
        if selection is not None:
            touched = math.prod(((begin + length - 1) // chunk - begin // chunk + 1)
                                for begin, length, chunk in zip(start, shape, chunks))
        else:
            touched = len({tuple(index // chunk for index, chunk in zip(
                _indices(flat, dataset.shape, "C"), chunks)) for flat in range(offset, offset + actual)})
        if touched * chunk_bytes > MAX_DECODE_BYTES:
            raise ValueError("Selected HDF5 chunks exceed the 32 MiB decompression budget")


def _read_hdf(stream, suffix, member, offset, count, start, shape):
    import h5py

    with _hdf_file(stream) as file:
        if member is None:
            if len(file) != 1:
                raise ValueError("Specify a dataset member; inspect the HDF5 groups first")
            member = next(iter(file.keys()))
        dataset = _select_hdf(file, member)
        if not isinstance(dataset, h5py.Dataset):
            raise ValueError("member must select an HDF5 dataset, not a group")
        metadata = _hdf_metadata(dataset, member)
        if dataset.shape is None:
            if offset or start is not None or shape is not None:
                raise ValueError("An empty HDF5 dataspace cannot be sliced")
            return {"format": "mat-v7.3" if suffix == ".mat" else "hdf5", **metadata,
                    "values": [], "returned": 0, "next_offset": None}
        actual, selection = _selection(dataset.shape, offset, count, start, shape)
        _check_hdf_reads(dataset, actual, offset, selection, start, shape)
        values = []
        budget = [0]
        if actual:
            if selection is not None:
                sample = np.asarray(dataset[selection]).reshape(-1)
                values = [_numpy_value(value, budget) for value in sample]
            elif not dataset.shape:
                values = [_numpy_value(dataset[()], budget)]
            else:
                # One HDF5 request groups selected points by chunk. Repeated
                # per-row reads could otherwise repeatedly inflate the same
                # chunk and escape the aggregate decompression budget.
                coordinates = np.asarray([_indices(flat, dataset.shape, "C")
                                          for flat in range(offset, offset + actual)], dtype=np.uint64)
                sample = np.empty(actual, dtype=dataset.dtype)
                with closing(dataset.id.get_space()) as disk_space, closing(h5py.h5s.create_simple((actual,))) as memory_space:
                    disk_space.select_elements(coordinates)
                    dataset.id.read(memory_space, disk_space, sample)
                values = [_numpy_value(value, budget) for value in sample]
        result = {"format": "mat-v7.3" if suffix == ".mat" else "hdf5", **metadata,
                  "flattening": "HDF5 storage order", "values": values, "returned": len(values)}
        if selection is not None:
            result.update(selection={"start": list(start), "shape": list(shape)}, next_offset=None)
        else:
            following = offset + actual
            result.update(offset=offset, next_offset=following if following < metadata["element_count"] else None)
        return _response(result)


@dataclass
class _MatVariable:
    name: str
    shape: tuple[int, ...]
    dtype: np.dtype | None
    source: BinaryIO
    real: int | None
    imaginary: int | None = None
    matlab_class: str = "numeric"
    logical: bool = False
    reason: str = ""

    def metadata(self):
        return {"member": self.name, "kind": "array", "shape": list(self.shape),
                "dtype": str(self.dtype) if self.dtype is not None else None,
                "element_count": math.prod(self.shape), "order": "F",
                "matlab_class": self.matlab_class, "complex": self.imaginary is not None,
                "logical": self.logical, "readable": self.real is not None and self.dtype is not None,
                **({"reason": self.reason} if self.reason else {})}


_MI_DTYPES = {1: "i1", 2: "u1", 3: "i2", 4: "u2", 5: "i4", 6: "u4", 7: "f4", 9: "f8",
              12: "i8", 13: "u8", 16: "u1", 17: "u2", 18: "u4"}
_MX_CLASSES = {1: "cell", 2: "struct", 3: "object", 4: "char", 5: "sparse", 6: "double",
               7: "single", 8: "int8", 9: "uint8", 10: "int16", 11: "uint16",
               12: "int32", 13: "uint32", 14: "int64", 15: "uint64", 16: "function", 17: "opaque"}


def _tag(source, position, end, endian, *, compressed_top=False):
    if position + 8 > end:
        raise ValueError("Truncated MAT data-element tag")
    source.seek(position)
    word, length = struct.unpack(endian + "II", _exact(source, 8))
    small_length = word >> 16
    if small_length:
        kind = word & 0xFFFF
        if not 1 <= small_length <= 4:
            raise ValueError("Invalid small MAT data-element length")
        return kind, small_length, position + 4, position + 8
    if position + 8 + length > end:
        raise ValueError("MAT data element exceeds its declared container")
    following = position + 8 + length
    if not (compressed_top and word == 15):
        following += -length % 8
    if following > end:
        raise ValueError("Truncated MAT element padding")
    return word, length, position + 8, following


def _matrix(source, start, end, endian):
    kind, length, position, following = _tag(source, start, end, endian)
    if kind != 6 or length != 8:
        raise ValueError("Invalid MAT array flags")
    source.seek(position)
    flags, _ = struct.unpack(endian + "II", _exact(source, 8))
    class_id = flags & 0xFF
    kind, length, position, following = _tag(source, following, end, endian)
    if kind != 5 or length < 4 or length % 4 or length > 4 * MAX_DEPTH:
        raise ValueError("Invalid MAT dimensions")
    source.seek(position)
    shape = struct.unpack(endian + "i" * (length // 4), _exact(source, length))
    if any(value < 0 for value in shape):
        raise ValueError("Invalid negative MAT dimension")
    kind, length, position, following = _tag(source, following, end, endian)
    if kind not in {1, 16} or not 1 <= length <= MAX_NAME:
        raise ValueError("Invalid or oversized MAT variable name")
    source.seek(position)
    name = _exact(source, length).decode("utf-8", errors="replace")
    if "\0" in name:
        raise ValueError("Invalid MAT variable name")
    variable = _MatVariable(name, shape, None, source, None,
                            matlab_class=_MX_CLASSES.get(class_id, "unknown"), logical=bool(flags & 0x200))
    if class_id not in {4, *range(6, 16)}:
        variable.reason = "Only dense numeric/logical and fixed-width character arrays are sampled"
        return variable
    kind, length, position, following = _tag(source, following, end, endian)
    if kind not in _MI_DTYPES:
        variable.reason = "Unsupported MAT numeric storage type"
        return variable
    dtype = np.dtype(endian + _MI_DTYPES[kind])
    if length != math.prod(shape) * dtype.itemsize:
        raise ValueError("MAT array dimensions do not match the stored payload")
    variable.dtype, variable.real = dtype, position
    if flags & 0x800:
        imag_kind, imag_length, imag_position, following = _tag(source, following, end, endian)
        if imag_kind != kind or imag_length != length:
            raise ValueError("MAT complex-array real and imaginary payloads differ")
        variable.imaginary = imag_position
    if following != end:
        raise ValueError("Unexpected data in a dense MAT array")
    return variable


def _decompress_mat(source, position, length, expanded_budget):
    if length > MAX_READ_BYTES:
        raise ValueError("Compressed MAT payload exceeds the 32 MiB read limit")
    source.seek(position)
    decoder = zlib.decompressobj()
    chunks = bytearray()
    remaining = length
    while remaining:
        chunk = _exact(source, min(65536, remaining))
        remaining -= len(chunk)
        output = decoder.decompress(chunk, expanded_budget[0] + 1)
        expanded_budget[0] -= len(output)
        if expanded_budget[0] < 0:
            raise ValueError("Compressed MAT data exceeds the 16 MiB decompression budget; use uncompressed MAT or v7.3")
        chunks.extend(output)
        if decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError("Invalid compressed MAT payload")
    if not decoder.eof:
        raise ValueError("Truncated compressed MAT payload")
    return io.BytesIO(chunks)


def _scan_mat5(source, size):
    source.seek(124)
    header = _exact(source, 4)
    endian = "<" if header[2:] == b"IM" else ">" if header[2:] == b"MI" else None
    if endian is None or struct.unpack(endian + "H", header[:2])[0] != 0x100:
        raise ValueError("Unsupported MAT file header")
    variables, expanded = [], [MAX_MAT_EXPANDED]
    position = 128
    while position < size:
        if len(variables) >= MAX_MEMBERS:
            raise ValueError("MAT file exceeds the 256-variable inspection limit")
        kind, length, payload, following = _tag(source, position, size, endian, compressed_top=True)
        if kind == 14:
            variable = _matrix(source, payload, payload + length, endian)
        elif kind == 15:
            decompressed = _decompress_mat(source, payload, length, expanded)
            inner_size = _size(decompressed)
            inner_kind, inner_length, inner_payload, inner_end = _tag(decompressed, 0, inner_size, endian)
            if inner_kind != 14 or inner_end != inner_size:
                raise ValueError("A compressed MAT element must contain one matrix")
            variable = _matrix(decompressed, inner_payload, inner_payload + inner_length, endian)
        else:
            raise ValueError("Unsupported top-level MAT data element")
        variables.append(variable)
        position = following
    return variables, "mat-v5"


def _scan_mat4(source, size):
    variables, position = [], 0
    while position < size:
        if len(variables) >= MAX_MEMBERS or position + 20 > size:
            raise ValueError("Invalid MAT v4 header or too many variables")
        source.seek(position)
        header = _exact(source, 20)
        choices = []
        for endian, machine in (("<", 0), (">", 1)):
            mopt, rows, cols, imag, name_length = struct.unpack(endian + "5i", header)
            if (mopt // 1000 == machine and (mopt // 100) % 10 == 0
                    and (mopt // 10) % 10 in range(6) and mopt % 10 in {0, 1, 2}
                    and rows >= 0 and cols >= 0 and imag in {0, 1} and 1 <= name_length <= MAX_NAME):
                choices.append((endian, mopt, rows, cols, imag, name_length))
        if len(choices) != 1:
            raise ValueError("Unsupported or corrupt MAT v4 file")
        endian, mopt, rows, cols, imag, name_length = choices[0]
        name_bytes = _exact(source, name_length)
        if not name_bytes.endswith(b"\0") or b"\0" in name_bytes[:-1]:
            raise ValueError("Invalid MAT v4 variable name")
        name = name_bytes[:-1].decode("utf-8", errors="replace")
        dtype = np.dtype(endian + {0: "f8", 1: "f4", 2: "i4", 3: "i2", 4: "u2", 5: "u1"}[(mopt // 10) % 10])
        real = position + 20 + name_length
        data_bytes = rows * cols * dtype.itemsize
        following = real + data_bytes * (1 + imag)
        if following > size:
            raise ValueError("Truncated MAT v4 array")
        variable = _MatVariable(name, (rows, cols), dtype, source, real,
                                real + data_bytes if imag else None,
                                matlab_class="char" if mopt % 10 == 1 else "numeric")
        if mopt % 10 == 2:
            variable.real, variable.reason, variable.matlab_class = None, "Sparse MAT v4 arrays are metadata-only", "sparse"
        variables.append(variable)
        position = following
    return variables, "mat-v4"


def _mat_variables(stream):
    source = _ReadBudget(stream)
    size = _size(source)
    if size < 20:
        raise ValueError("MAT header is missing or truncated")
    source.seek(0)
    header = source.read(min(size, 128))
    try:
        if size >= 128 and header[126:128] in {b"IM", b"MI"}:
            variables, format_name = _scan_mat5(source, size)
        else:
            variables, format_name = _scan_mat4(source, size)
    except (zlib.error, struct.error, OverflowError):
        raise ValueError("Invalid or corrupt MAT data") from None
    names = [variable.name for variable in variables]
    if len(set(names)) != len(names):
        raise ValueError("Duplicate MAT variable names are ambiguous")
    return variables, format_name


def _choose_mat(variables, member):
    if member is None:
        if len(variables) != 1:
            raise ValueError("Specify a MAT variable member; inspect variable names first")
        return variables[0]
    for variable in variables:
        if variable.name == member:
            return variable
    raise ValueError("MAT variable member was not found")


def _read_mat(stream, member, offset, count, start, shape):
    variables, format_name = _mat_variables(stream)
    variable = _choose_mat(variables, member)
    if variable.real is None or variable.dtype is None:
        raise ValueError(variable.reason or "Unsupported MAT variable")
    actual, selection = _selection(variable.shape, offset, count, start, shape)
    if actual * variable.dtype.itemsize * (2 if variable.imaginary is not None else 1) > MAX_SAMPLE_BYTES:
        raise ValueError("MAT sample exceeds 1 MiB; request a smaller sample")
    if selection is None:
        positions = range(offset, offset + actual)
    else:
        strides = [math.prod(variable.shape[:axis]) for axis in range(len(variable.shape))]
        # Fortran storage order: the first dimension changes fastest.
        axes = [range(begin, begin + length) for begin, length in zip(start, shape)]
        positions = [sum(index * stride for index, stride in zip(indices[::-1], strides))
                     for indices in itertools.product(*axes[::-1])]
    positions = list(positions)
    values, budget = [], [0]
    for position in positions:
        variable.source.seek(variable.real + position * variable.dtype.itemsize)
        raw = _exact(variable.source, variable.dtype.itemsize)
        value = np.frombuffer(raw, dtype=variable.dtype, count=1)[0]
        if variable.imaginary is not None:
            variable.source.seek(variable.imaginary + position * variable.dtype.itemsize)
            imaginary = np.frombuffer(_exact(variable.source, variable.dtype.itemsize), dtype=variable.dtype, count=1)[0]
            result = {"real": _numpy_value(value, budget), "imag": _numpy_value(imaginary, budget)}
        else:
            result = bool(value) if variable.logical else _numpy_value(value, budget)
        values.append(result)
    result = {"format": format_name, **variable.metadata(), "values": values,
              "returned": len(values), "flattening": "MATLAB column-major storage order"}
    if selection is not None:
        result.update(selection={"start": list(start), "shape": list(shape)}, next_offset=None)
    else:
        following = offset + actual
        result.update(offset=offset, next_offset=following if following < math.prod(variable.shape) else None)
    return _response(result)


def inspect(stream: BinaryIO, suffix: str, selector: str = "", offset: int = 0, limit: int = 50) -> dict:
    """Inspect one HDF5 group/dataset or a bounded page of classic MAT variables."""
    _integer(offset, "offset", 0, 10000)
    _integer(limit, "limit", 1, MAX_PAGE)
    suffix = suffix.lower()
    if suffix in {".h5", ".hdf5", ".hdf"} or (suffix == ".mat" and _hdf_signature(stream)):
        return _inspect_hdf(stream, suffix, selector, offset, limit)
    if suffix != ".mat":
        raise ValueError("Expected .h5, .hdf5, .hdf, or .mat")
    variables, format_name = _mat_variables(stream)
    if selector:
        return _response({"format": format_name, **_choose_mat(variables, selector).metadata()})
    if offset > len(variables):
        raise ValueError("offset exceeds the MAT variable count")
    page = variables[offset:offset + limit]
    following = offset + len(page)
    return _response({"format": format_name, "member_count": len(variables),
                      "members": [variable.metadata() for variable in page], "offset": offset,
                      "returned": len(page), "next_offset": following if following < len(variables) else None})


def read_array(stream: BinaryIO, suffix: str, member: str | None = None, offset: int = 0,
               count: int = 100, start: list[int] | None = None, shape: list[int] | None = None) -> dict:
    """Read at most 4096 values by flat pagination or a bounded rectangular slice."""
    _integer(offset, "offset", 0, 2**63 - 1)
    _integer(count, "count", 0, MAX_ELEMENTS)
    suffix = suffix.lower()
    if suffix in {".h5", ".hdf5", ".hdf"} or (suffix == ".mat" and _hdf_signature(stream)):
        return _read_hdf(stream, suffix, member, offset, count, start, shape)
    if suffix == ".mat":
        return _read_mat(stream, member, offset, count, start, shape)
    raise ValueError("Expected .h5, .hdf5, .hdf, or .mat")
