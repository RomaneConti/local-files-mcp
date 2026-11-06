"""Bounded Parquet and Feather V2 previews on approved handles.

These functions never open paths, deserialize Python objects, or load a complete
table. IPC compressed-buffer sizes are checked before any batch is decoded.
Parquet row groups are checked against metadata limits before selected columns decode.
"""

from __future__ import annotations

import base64
import datetime
import decimal
import math
import struct
from typing import BinaryIO

import pyarrow as pa
import pyarrow.parquet as pq

from .common import BudgetIO, integer as _integer, response as _response, safe_value


MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_DECODE_BYTES = 32 * 1024 * 1024
MAX_REQUEST_DECODE_BYTES = 64 * 1024 * 1024
MAX_BATCH_ROWS = 1_000_000
MAX_COLUMNS = 1024
MAX_BLOCKS = 10_000
MAX_CELLS = 4096
MAX_NAME_LENGTH = 1024
MAX_BUFFERS = 4096


def _read_at(stream: BinaryIO, offset: int, size: int) -> bytes:
    if offset < 0 or size < 0:
        raise ValueError("Invalid columnar file offset or length")
    stream.seek(offset)
    value = stream.read(size)
    if len(value) != size:
        raise ValueError("Truncated columnar file")
    return value


def _size(stream: BinaryIO) -> int:
    stream.seek(0, 2)
    return stream.tell()


def _no_extensions(metadata: bytes) -> None:
    # Reject before invoking native schema readers: a registered Arrow custom
    # extension type can execute its Python deserializer while reading a schema.
    if b"ARROW:extension:" in metadata:
        raise ValueError("Arrow extension types are not supported; export plain storage types")


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 35, 7):
        if pos >= len(data):
            break
        part = data[pos]
        pos += 1
        value |= (part & 127) << shift
        if not part & 128:
            return value, pos
    raise ValueError("Invalid Parquet schema metadata length")


def _parquet_footer(stream: BinaryIO) -> bytes:
    size = _size(stream)
    if size < 12 or _read_at(stream, 0, 4) != b"PAR1":
        raise ValueError("Only ordinary, unencrypted Parquet files are supported")
    tail = _read_at(stream, size - 8, 8)
    length = struct.unpack_from("<I", tail)[0]
    if tail[4:] != b"PAR1" or not 0 < length <= min(MAX_METADATA_BYTES, size - 12):
        raise ValueError("Invalid or oversized Parquet footer (limit 2 MiB)")
    footer = _read_at(stream, size - 8 - length, length)
    _no_extensions(footer)
    # ARROW:schema is base64 IPC in a Thrift KeyValue. Only the canonical
    # key-then-value encoding is accepted, so extension metadata cannot hide
    # behind the serialized schema. Unknown/reordered encodings fail closed.
    marker = b"ARROW:schema"
    start = 0
    while (at := footer.find(marker, start)) >= 0:
        pos = at + len(marker)
        if footer[pos:pos + 1] != b"\x18":
            raise ValueError("Unsupported Parquet ARROW:schema metadata encoding")
        length, pos = _varint(footer, pos + 1)
        if length > MAX_METADATA_BYTES or pos + length > len(footer):
            raise ValueError("Invalid Parquet serialized schema")
        try:
            schema = base64.b64decode(footer[pos:pos + length], validate=True)
        except ValueError as exc:
            raise ValueError("Invalid Parquet serialized schema") from exc
        _no_extensions(schema)
        start = pos + length
    return footer


def _parquet(stream: BinaryIO):
    _parquet_footer(stream)
    stream.seek(0)
    reader = pq.ParquetFile(
        stream, pre_buffer=False, memory_map=False,
        thrift_string_size_limit=MAX_METADATA_BYTES,
        thrift_container_size_limit=100_000, arrow_extensions_enabled=False,
    )
    if reader.metadata.num_row_groups > MAX_BLOCKS:
        raise ValueError("Parquet has too many row groups for a bounded preview")
    schema = reader.schema_arrow
    _check_schema(schema)
    return reader, schema, reader.metadata.num_rows


class _FlatBuffer:
    """Small bounds-checked metadata lookup, not an Arrow data decoder.

    Field layouts are from Apache Arrow format/File.fbs and Message.fbs.
    Only integer fields, table offsets and vectors of Block structs are read.
    """

    def __init__(self, data: bytes):
        self.data = data

    def number(self, fmt: str, pos: int) -> int:
        if pos < 0 or pos + struct.calcsize(fmt) > len(self.data):
            raise ValueError("Invalid Arrow IPC metadata offset")
        return struct.unpack_from(fmt, self.data, pos)[0]

    def root(self) -> int:
        return self.number("<I", 0)

    def field(self, table: int, index: int) -> int | None:
        vtable = table - self.number("<i", table)
        size = self.number("<H", vtable)
        entry = vtable + 4 + index * 2
        if entry + 2 > vtable + size:
            return None
        offset = self.number("<H", entry)
        return table + offset if offset else None

    def indirect(self, pos: int) -> int:
        return pos + self.number("<I", pos)

    def blocks(self, table: int, field: int) -> list[tuple[int, int, int]]:
        pos = self.field(table, field)
        if pos is None:
            return []
        vector = self.indirect(pos)
        count = self.number("<I", vector)
        if count > MAX_BLOCKS:
            raise ValueError("Arrow IPC has too many record batches")
        result = []
        for index in range(count):
            entry = vector + 4 + index * 24
            result.append((self.number("<q", entry), self.number("<i", entry + 8), self.number("<q", entry + 16)))
        return result


def _ipc_buffers(stream, flat, header, body_start, body_size, compressed):
    """Read expansion prefixes, never compressed payloads or decoded buffers.

    Arrow IPC BUFFER compression puts a signed int64 decompressed length ahead
    of every nonempty buffer; -1 means the remaining bytes are already raw.
    The native decoder uses these exact prefixes for its buffer allocations.
    """
    field = flat.field(header, 2)
    if field is None:
        return 0
    vector = flat.indirect(field)
    count = flat.number('<I', vector)
    if count > MAX_BUFFERS:
        raise ValueError('Arrow IPC buffer count exceeds the 4096-buffer limit')
    decoded = 0
    previous_end = 0
    for index in range(count):
        entry = vector + 4 + index * 16
        offset = flat.number('<q', entry)
        length = flat.number('<q', entry + 8)
        if offset < previous_end or offset % 8 or length < 0 or offset + length > body_size:
            raise ValueError('Invalid, overlapping or out-of-range Arrow IPC buffer')
        previous_end = offset + length
        expanded = length
        if compressed and length:
            if length < 8:
                raise ValueError('Truncated Arrow IPC compressed-buffer prefix')
            expanded = struct.unpack('<q', _read_at(stream, body_start + offset, 8))[0]
            if expanded == -1:
                expanded = length - 8
            elif expanded < 0:
                raise ValueError('Invalid Arrow IPC compressed-buffer expansion length')
        if expanded > MAX_DECODE_BYTES:
            raise ValueError('Arrow IPC buffer exceeds the 32 MiB decode limit')
        decoded += expanded
        if decoded > MAX_DECODE_BYTES:
            raise ValueError('Arrow IPC buffers exceed the 32 MiB batch decode limit')
    return decoded


def _ipc_batch_info(stream: BinaryIO, block: tuple[int, int, int], footer_start: int) -> dict:
    offset, metadata_size, body_size = block
    if (offset < 8 or not 8 <= metadata_size <= MAX_METADATA_BYTES
            or body_size < 0 or offset + metadata_size + body_size > footer_start):
        raise ValueError("Invalid or oversized Arrow IPC record batch metadata")
    metadata = _read_at(stream, offset, metadata_size)
    prefix = 8 if metadata[:4] == b"\xff\xff\xff\xff" else 4
    declared = struct.unpack_from("<i", metadata, prefix - 4)[0]
    if declared <= 0 or declared + prefix > metadata_size:
        raise ValueError("Invalid Arrow IPC message length")
    metadata = metadata[prefix:prefix + declared]
    _no_extensions(metadata)
    flat = _FlatBuffer(metadata)
    message = flat.root()
    type_field, header_field = flat.field(message, 1), flat.field(message, 2)
    if type_field is None or flat.number("<B", type_field) != 3 or header_field is None:
        raise ValueError("Only Arrow IPC record batches are supported")
    body_field = flat.field(message, 3)
    if (flat.number("<q", body_field) if body_field is not None else 0) != body_size:
        raise ValueError("Inconsistent Arrow IPC body length")
    header = flat.indirect(header_field)
    rows_field = flat.field(header, 0)
    rows = flat.number("<q", rows_field) if rows_field is not None else 0
    if not 0 <= rows <= MAX_BATCH_ROWS:
        raise ValueError("Invalid Arrow IPC row count")
    compression = flat.field(header, 3)
    codec_name = None
    if compression is not None:
        compression = flat.indirect(compression)
        codec_field, method_field = flat.field(compression, 0), flat.field(compression, 1)
        codec = flat.number('<b', codec_field) if codec_field is not None else 0
        method = flat.number('<b', method_field) if method_field is not None else 0
        if codec not in (0, 1) or method != 0:
            raise ValueError('Only Arrow IPC LZ4_FRAME/ZSTD buffer compression is supported')
        codec_name = ('lz4', 'zstd')[codec]
    if body_size > MAX_DECODE_BYTES:
        raise ValueError('Feather batch exceeds the 32 MiB stored-byte limit; export smaller batches')
    decoded = _ipc_buffers(stream, flat, header, offset + metadata_size, body_size, compression is not None)
    return {"rows": rows, "bytes": body_size, "decoded_bytes": decoded,
            "compressed": compression is not None, "codec": codec_name}


def _feather(stream: BinaryIO):
    size = _size(stream)
    if size < 18 or _read_at(stream, 0, 6) != b"ARROW1":
        raise ValueError("Only Feather V2 / Arrow IPC file format is supported (not Feather V1 or IPC streams)")
    tail = _read_at(stream, size - 10, 10)
    length = struct.unpack_from("<I", tail)[0]
    if tail[4:] != b"ARROW1" or not 0 < length <= min(MAX_METADATA_BYTES, size - 18):
        raise ValueError("Invalid or oversized Arrow IPC footer (limit 2 MiB)")
    start = size - 10 - length
    footer = _read_at(stream, start, length)
    _no_extensions(footer)
    flat = _FlatBuffer(footer)
    root = flat.root()
    if flat.blocks(root, 2):
        raise ValueError("Dictionary-encoded Feather is not supported; export plain columns")
    blocks = flat.blocks(root, 3)
    infos = [_ipc_batch_info(stream, block, start) for block in blocks]
    stream.seek(0)
    reader = pa.ipc.open_file(stream, options=pa.ipc.IpcReadOptions(use_threads=False))
    schema = reader.schema
    _check_schema(schema)
    if reader.num_record_batches != len(infos):
        raise ValueError("Inconsistent Arrow IPC batch count")
    return reader, schema, sum(info["rows"] for info in infos), infos


def _check_schema(schema) -> None:
    if len(schema) > MAX_COLUMNS:
        raise ValueError("Column count exceeds the 1024-column preview limit")
    names = schema.names
    if len(set(names)) != len(names) or any(len(name) > MAX_NAME_LENGTH for name in names):
        raise ValueError("Duplicate or oversized column names are not supported")
    for field in schema:
        if isinstance(field.type, pa.BaseExtensionType):
            raise ValueError("Arrow extension types are not supported")


def _format(suffix: str, selector: str) -> str:
    if selector not in ("", "table"):
        raise ValueError("Columnar files have one table; selector must be empty or 'table'")
    suffix = suffix.lower()
    if suffix in (".parquet", ".pq"):
        return "parquet"
    if suffix in (".feather", ".arrow", ".ipc"):
        return "feather"
    raise ValueError("Unsupported columnar suffix")


def inspect(stream: BinaryIO, suffix: str, selector: str = "", offset: int = 0, limit: int = 50) -> dict:
    """Inspect schema/row count without reading table data. offset pages columns."""
    kind = _format(suffix, selector)
    _integer(offset, "offset", 0, 2**53 - 1)
    _integer(limit, "limit", 1, 100)
    stream = BudgetIO(stream)
    extra = {}
    if kind == "parquet":
        reader, schema, count = _parquet(stream)
        extra["row_groups"] = reader.metadata.num_row_groups
    else:
        _, schema, count, infos = _feather(stream)
        extra = {"record_batches": len(infos), "compressed_batches": sum(info["compressed"] for info in infos),
                 "read_constraint": "Feather V2 plain, LZ4 or ZSTD batches: each stored/expanded batch <=32 MiB, <=1000000 rows; dictionary encoding unsupported"}
    fields = list(schema)[offset:offset + limit]
    return _response({"format": kind, "selector": "", "row_count": count, "column_count": len(schema),
                      "columns": [{"name": field.name, "type": str(field.type), "nullable": field.nullable} for field in fields],
                      "offset": offset, "returned": len(fields),
                      "next_offset": offset + len(fields) if offset + len(fields) < len(schema) else None, **extra})


def _columns(schema, columns, column_offset: int, column_limit: int) -> tuple[list[str], int | None]:
    _integer(column_offset, "column_offset", 0, MAX_COLUMNS)
    _integer(column_limit, "column_limit", 1, 50)
    if columns is None:
        selected = schema.names[column_offset:column_offset + column_limit]
        end = column_offset + len(selected)
        next_column = end if end < len(schema) else None
    else:
        if (not isinstance(columns, list) or not 1 <= len(columns) <= 50
                or any(not isinstance(name, str) for name in columns) or len(set(columns)) != len(columns)):
            raise ValueError("columns must be a list of 1 to 50 distinct column names")
        selected, next_column = columns, None
        if column_offset:
            raise ValueError("column_offset must be zero when columns are selected by name")
    for name in selected:
        if name not in schema.names:
            raise ValueError("Unknown selected column")
        dtype = schema.field(name).type
        if (isinstance(dtype, pa.BaseExtensionType) or pa.types.is_nested(dtype)
                or pa.types.is_dictionary(dtype) or pa.types.is_union(dtype)):
            raise ValueError("Selected nested, dictionary or extension columns are not supported; select plain scalar columns")
    return selected, next_column


def _cell(scalar):
    if not scalar.is_valid:
        return None
    value = scalar.as_py()
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    if isinstance(value, str):
        return safe_value(value)
    if isinstance(value, bytes):
        return {"type": "binary", "base64": base64.b64encode(value[:1024]).decode("ascii"),
                "length": len(value), "truncated": len(value) > 1024}
    if isinstance(value, (datetime.date, datetime.time, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, (datetime.timedelta, decimal.Decimal)):
        return str(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    # Never repr() unknown Python objects. Extension and nested types have
    # already been refused, and unsupported scalar types fail closed here.
    raise ValueError("Unsupported scalar type; select ordinary numeric, text, binary or temporal columns")


def _append_rows(rows: list, batch, names: list[str], offset: int, count: int) -> None:
    arrays = [batch.column(batch.schema.get_field_index(name)) for name in names]
    for row in range(offset, offset + count):
        rows.append([_cell(array[row]) for array in arrays])


def _parquet_rows(reader, schema, names, offset, limit):
    selected = set(names)
    groups = []
    cursor = 0
    total_bytes = 0
    for index in range(reader.metadata.num_row_groups):
        group = reader.metadata.row_group(index)
        end = cursor + group.num_rows
        if cursor < offset + limit and end > offset:
            decoded = 0
            for column in range(group.num_columns):
                chunk = group.column(column)
                if chunk.file_path:
                    raise ValueError("External Parquet column chunks are forbidden")
                if chunk.path_in_schema in selected:
                    if chunk.total_uncompressed_size < 0 or chunk.total_compressed_size < 0:
                        raise ValueError("Invalid Parquet column size")
                    decoded += chunk.total_uncompressed_size
            # Encoded dictionaries/RLE may be tiny despite a huge row count.
            if group.num_rows > MAX_BATCH_ROWS or max(decoded, group.num_rows * max(len(names), 1) * 8) > MAX_DECODE_BYTES:
                raise ValueError("Selected Parquet row group exceeds the 32 MiB / 1000000-row decode limit; export smaller row groups")
            total_bytes += decoded
            if total_bytes > MAX_REQUEST_DECODE_BYTES or len(groups) >= 128:
                raise ValueError("Requested Parquet preview crosses too many bytes or row groups; request fewer rows")
            groups.append((index, max(offset - cursor, 0), min(end, offset + limit) - max(cursor, offset)))
        cursor = end
        if cursor >= offset + limit:
            break
    rows = []
    for index, skip, remaining in groups:
        for batch in reader.iter_batches(batch_size=min(limit, 1024), row_groups=[index], columns=names, use_threads=False, use_pandas_metadata=False):
            if skip >= batch.num_rows:
                skip -= batch.num_rows
                continue
            take = min(remaining, batch.num_rows - skip)
            _append_rows(rows, batch, names, skip, take)
            remaining -= take
            skip = 0
            if not remaining:
                break
    return rows


def _feather_rows(reader, infos, names, offset, limit):
    rows = []
    cursor = 0
    total_bytes = 0
    for index, info in enumerate(infos):
        end = cursor + info["rows"]
        if cursor < offset + limit and end > offset:
            if max(info["bytes"], info["decoded_bytes"]) > MAX_DECODE_BYTES or info["rows"] > MAX_BATCH_ROWS:
                raise ValueError("Feather batch exceeds the 32 MiB / 1000000-row decode limit; export smaller batches")
            total_bytes += max(info["bytes"], info["decoded_bytes"])
            if total_bytes > MAX_REQUEST_DECODE_BYTES:
                raise ValueError("Requested Feather preview crosses too many bytes; request fewer rows")
            batch = reader.get_batch(index)
            if batch.num_rows != info["rows"]:
                raise ValueError("Inconsistent Arrow IPC row count")
            start = max(offset - cursor, 0)
            take = min(end, offset + limit) - max(cursor, offset)
            _append_rows(rows, batch, names, start, take)
        cursor = end
        if cursor >= offset + limit:
            break
    return rows


def read_table(stream: BinaryIO, suffix: str, selector: str = "", columns: list[str] | None = None,
               offset: int = 0, limit: int = 100, column_offset: int = 0, column_limit: int = 20) -> dict:
    """Return a row/column window; data is never loaded as a complete table."""
    kind = _format(suffix, selector)
    _integer(offset, "offset", 0, 2**53 - 1)
    _integer(limit, "limit", 1, 1000)
    stream = BudgetIO(stream)
    if kind == "parquet":
        reader, schema, count = _parquet(stream)
    else:
        reader, schema, count, infos = _feather(stream)
    names, next_column = _columns(schema, columns, column_offset, column_limit)
    if limit * len(names) > MAX_CELLS:
        raise ValueError("Preview exceeds the 4096-cell limit; request fewer rows or columns")
    if not names or offset >= count:
        rows = []
    elif kind == "parquet":
        rows = _parquet_rows(reader, schema, names, offset, limit)
    else:
        rows = _feather_rows(reader, infos, names, offset, limit)
    return _response({"format": kind, "selector": "", "row_count": count, "column_count": len(schema),
                      "columns": names, "rows": rows, "offset": offset, "returned": len(rows),
                      "next_offset": offset + len(rows) if rows and offset + len(rows) < count else None,
                      "column_offset": column_offset, "next_column_offset": next_column})
