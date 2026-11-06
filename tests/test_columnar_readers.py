"""Real columnar fixtures, plus checks that unsafe inputs fail before decoding."""
import datetime
import io
import json
import struct
from decimal import Decimal

import pyarrow as pa
import pyarrow.feather as feather
import pyarrow.parquet as pq
import pytest

from local_files_mcp.readers import columnar


def parquet(table, **kwargs):
    sink = io.BytesIO()
    pq.write_table(table, sink, **kwargs)
    sink.seek(0)
    return sink


def ipc(table, **kwargs):
    sink = io.BytesIO()
    feather.write_feather(table, sink, **kwargs)
    sink.seek(0)
    return sink


@pytest.fixture
def table():
    return pa.table({"number": range(12), "label": [f"row {i}" for i in range(12)], "value": [None, float("nan"), float("inf")] * 4})


@pytest.mark.parametrize("suffix,writer,kwargs", [
    (".parquet", parquet, {"row_group_size": 4}),
    (".feather", ipc, {"compression": "uncompressed", "chunksize": 4}),
])
def test_schema_and_cross_batch_selected_rows(table, suffix, writer, kwargs):
    source = writer(table, **kwargs)
    info = columnar.inspect(source, suffix, offset=1, limit=1)
    assert info["row_count"] == 12
    assert info["column_count"] == 3
    assert info["columns"][0]["name"] == "label"
    assert info["next_offset"] == 2
    result = columnar.read_table(source, suffix, columns=["label", "number"], offset=3, limit=4)
    assert result["rows"] == [[f"row {i}", i] for i in range(3, 7)]
    assert result["columns"] == ["label", "number"]
    assert result["next_offset"] == 7
    assert not source.closed


@pytest.mark.parametrize("suffix,writer", [(".parquet", parquet), (".feather", lambda t: ipc(t, compression="uncompressed"))])
def test_json_safe_scalars_and_empty_page(suffix, writer):
    source = writer(pa.table({
        "float": [float("nan"), float("inf")], "text": ["x" * 4000, "okay"],
        "binary": [b"a" * 4000, b"b"], "date": [datetime.date(2026, 1, 1), None],
        "decimal": [Decimal("1.25"), Decimal("2.50")],
    }))
    result = columnar.read_table(source, suffix)
    json.dumps(result, allow_nan=False)
    assert result["rows"][0][0] == "NaN"
    assert result["rows"][0][1]["truncated"]
    assert result["rows"][0][2]["truncated"]
    assert result["rows"][0][3] == "2026-01-01"
    assert result["rows"][0][4] == "1.25"
    assert columnar.read_table(source, suffix, offset=100)["rows"] == []


def test_column_paging_and_bad_input(table):
    source = parquet(table)
    result = columnar.read_table(source, ".parquet", column_offset=1, column_limit=1)
    assert result["columns"] == ["label"]
    assert result["next_column_offset"] == 2
    for kwargs in ({"columns": ["missing"]}, {"columns": ["number", "number"]}, {"columns": "number"},
                   {"offset": -1}, {"limit": 0}, {"limit": True}, {"selector": "other"},
                   {"columns": ["number"], "column_offset": 1}):
        with pytest.raises(ValueError):
            columnar.read_table(source, ".parquet", **kwargs)


def test_metadata_page_and_cell_limits():
    source = parquet(pa.table({f"c{i}": [i] for i in range(60)}))
    assert columnar.inspect(source, ".parquet")["next_offset"] == 50
    with pytest.raises(ValueError, match="4096-cell"):
        columnar.read_table(source, ".parquet", columns=[f"c{i}" for i in range(50)], limit=100)
    with pytest.raises(ValueError, match="64 KiB"):
        source = parquet(pa.table({"x": ["x" * 2000] * 100}))
        columnar.read_table(source, ".parquet", limit=100)


def test_parquet_rejects_large_group_before_iterating(table, monkeypatch):
    source = parquet(table)
    monkeypatch.setattr(columnar, "MAX_DECODE_BYTES", 1)
    class GuardedReader:
        def __init__(self, reader):
            self.metadata = reader.metadata
        def iter_batches(self, **kwargs):
            pytest.fail("Oversized row group must not decode")
    original = columnar._parquet
    def wrap(stream):
        reader, schema, count = original(stream)
        return GuardedReader(reader), schema, count
    monkeypatch.setattr(columnar, "_parquet", wrap)
    with pytest.raises(ValueError, match="row group exceeds"):
        columnar.read_table(source, ".parquet")


def test_parquet_does_not_decode_unselected_large_column(monkeypatch):
    source = parquet(pa.table({"small": [1, 2], "large": ["x" * 10000] * 2}))
    monkeypatch.setattr(columnar, "MAX_DECODE_BYTES", 1024)
    assert columnar.read_table(source, ".parquet", columns=["small"])["rows"] == [[1], [2]]
    with pytest.raises(ValueError, match="row group exceeds"):
        columnar.read_table(source, ".parquet", columns=["large"])


@pytest.mark.parametrize("compression", [None, "lz4", "zstd"])
def test_compressed_feather_real_default_and_explicit_codecs(table, compression):
    options = {"compression": compression} if compression else {}
    source = ipc(table, chunksize=4, **options)
    assert columnar.inspect(source, ".feather")["compressed_batches"] == 3
    result = columnar.read_table(source, ".feather", columns=["label", "number"], offset=3, limit=4)
    assert result["rows"] == [[f"row {i}", i] for i in range(3, 7)]
    assert result["next_offset"] == 7


def _first_ipc_batch(source):
    """Locate encoded fields in fixtures to create malformed-file regressions."""
    data = bytearray(source.getvalue())
    footer_length = struct.unpack_from('<I', data, len(data) - 10)[0]
    footer_start = len(data) - 10 - footer_length
    footer = columnar._FlatBuffer(bytes(data[footer_start:-10]))
    offset, metadata_size, body_size = footer.blocks(footer.root(), 3)[0]
    prefix = 8 if data[offset:offset + 4] == b'\xff\xff\xff\xff' else 4
    flat = columnar._FlatBuffer(bytes(data[offset + prefix:offset + metadata_size]))
    header = flat.indirect(flat.field(flat.root(), 2))
    vector = flat.indirect(flat.field(header, 2))
    buffers = []
    for index in range(flat.number('<I', vector)):
        entry = vector + 4 + index * 16
        buffer_offset, buffer_size = flat.number('<q', entry), flat.number('<q', entry + 8)
        if buffer_size:
            buffers.append((offset + metadata_size + buffer_offset, buffer_size,
                            offset + prefix + entry))
    return data, buffers, flat, header, offset + prefix


@pytest.mark.parametrize('length', [-2, 32 * 1024**2 + 1, 2**62])
@pytest.mark.parametrize('compression', ['lz4', 'zstd'])
def test_compression_prefix_bombs_rejected_before_arrow_reader(table, length, compression, monkeypatch):
    data, buffers, *_ = _first_ipc_batch(ipc(table, compression=compression))
    struct.pack_into('<q', data, buffers[0][0], length)
    monkeypatch.setattr(pa.ipc, 'open_file', lambda *args, **kwargs: pytest.fail('Bad expansion prefix reached native reader'))
    with pytest.raises(ValueError, match='expansion length|decode limit'):
        columnar.read_table(io.BytesIO(data), '.feather')


def test_combined_compressed_expansion_limit(table, monkeypatch):
    data, buffers, *_ = _first_ipc_batch(ipc(table, compression='lz4'))
    for location, *_ in buffers[:2]:
        struct.pack_into('<q', data, location, columnar.MAX_DECODE_BYTES // 2 + 1)
    monkeypatch.setattr(pa.ipc, 'open_file', lambda *args, **kwargs: pytest.fail('Combined expansion reached native reader'))
    with pytest.raises(ValueError, match='batch decode limit'):
        columnar.read_table(io.BytesIO(data), '.feather')


def test_compressed_raw_buffer_sentinel():
    table = pa.table({'binary': [bytes(range(251))]})
    data, buffers, *_ = _first_ipc_batch(ipc(table, compression='lz4'))
    raw_buffers = [buffer.to_pybytes() for buffer in table.column(0).chunk(0).buffers() if buffer is not None]
    for (location, length, descriptor), raw in zip(buffers, raw_buffers, strict=True):
        assert len(raw) + 8 <= length  # These tiny/incompressible fixtures grow under LZ4.
        struct.pack_into('<q', data, location, -1)
        data[location + 8:location + 8 + len(raw)] = raw
        struct.pack_into('<q', data, descriptor + 8, len(raw) + 8)
    result = columnar.read_table(io.BytesIO(data), '.feather')
    assert result['rows'][0][0]['base64'] == columnar.base64.b64encode(bytes(range(251))).decode()


@pytest.mark.parametrize('mutation', ['short', 'offset', 'codec'])
def test_malformed_compression_metadata_rejected_before_arrow_reader(table, mutation, monkeypatch):
    data, buffers, flat, header, metadata_start = _first_ipc_batch(ipc(table, compression='zstd'))
    if mutation == 'short':
        struct.pack_into('<q', data, buffers[0][2] + 8, 7)
    elif mutation == 'offset':
        struct.pack_into('<q', data, buffers[0][2], -1)
    else:
        compression = flat.indirect(flat.field(header, 3))
        data[metadata_start + flat.field(compression, 0)] = 99
    monkeypatch.setattr(pa.ipc, 'open_file', lambda *args, **kwargs: pytest.fail('Malformed compression reached native reader'))
    with pytest.raises(ValueError):
        columnar.read_table(io.BytesIO(data), '.feather')


@pytest.mark.parametrize('compression', ['lz4', 'zstd'])
def test_corrupt_compressed_payload_fails_cleanly(table, compression):
    data, buffers, *_ = _first_ipc_batch(ipc(table, compression=compression))
    location, length, _ = buffers[0]
    data[location + 8:location + length] = b'x' * (length - 8)
    with pytest.raises((ValueError, OSError)):
        columnar.read_table(io.BytesIO(data), '.feather')


def test_feather_batch_cap_precedes_decode(table, monkeypatch):
    source = ipc(table, compression="uncompressed")
    monkeypatch.setattr(columnar, "MAX_DECODE_BYTES", 1)
    with pytest.raises(ValueError, match="Feather batch exceeds"):
        columnar.read_table(source, ".feather")


def test_nested_schema_visible_but_plain_column_selection_required():
    source = parquet(pa.table({"plain": [1], "nested": [[1, 2]]}))
    assert "list" in columnar.inspect(source, ".parquet")["columns"][1]["type"]
    assert columnar.read_table(source, ".parquet", columns=["plain"])["rows"] == [[1]]
    with pytest.raises(ValueError, match="nested"):
        columnar.read_table(source, ".parquet", columns=["nested"])


@pytest.mark.parametrize("suffix,writer", [(".parquet", parquet), (".feather", lambda t: ipc(t, compression="uncompressed"))])
def test_extension_deserializer_never_called(suffix, writer):
    calls = []
    class TestExtension(pa.ExtensionType):
        def __init__(self):
            super().__init__(pa.int64(), "local-files-test")
        def __arrow_ext_serialize__(self):
            return b""
        @classmethod
        def __arrow_ext_deserialize__(cls, storage_type, serialized):
            calls.append(True)
            return cls()
    kind = TestExtension()
    values = pa.ExtensionArray.from_storage(kind, pa.array([1, 2]))
    source = writer(pa.table({"danger": values}))
    pa.register_extension_type(kind)
    try:
        with pytest.raises(ValueError, match="extension"):
            columnar.inspect(source, suffix)
        assert calls == []
    finally:
        pa.unregister_extension_type("local-files-test")


def test_dictionary_feather_rejected_before_decode():
    source = ipc(pa.table({"x": pa.array(["a", "b", "a"]).dictionary_encode()}), compression="uncompressed")
    with pytest.raises(ValueError, match="Dictionary-encoded"):
        columnar.inspect(source, ".feather")


@pytest.mark.parametrize("suffix,writer", [(".parquet", parquet), (".feather", lambda t: ipc(t, compression="uncompressed"))])
def test_empty_table_and_duplicate_names(suffix, writer):
    source = writer(pa.table({"x": pa.array([], type=pa.int64())}))
    assert columnar.inspect(source, suffix)["row_count"] == 0
    assert columnar.read_table(source, suffix)["rows"] == []
    source = writer(pa.table([pa.array([1]), pa.array([2])], names=["same", "same"]))
    with pytest.raises(ValueError, match="Duplicate"):
        columnar.inspect(source, suffix)


def test_external_parquet_chunks_are_never_followed():
    metadata = pq.read_metadata(parquet(pa.table({"x": [1]})))
    metadata.set_file_path("../outside.parquet")
    source = io.BytesIO()
    metadata.write_metadata_file(source)
    with pytest.raises(ValueError, match="External"):
        columnar.read_table(source, ".parquet")


@pytest.mark.parametrize("source,suffix", [
    (b"PAR1" + b"x" * 10 + struct.pack("<I", 3 * 1024 * 1024) + b"PAR1", ".parquet"),
    (b"ARROW1" + b"x" * 10 + struct.pack("<I", 3 * 1024 * 1024) + b"ARROW1", ".feather"),
    (b"FEA1" + b"x" * 100, ".feather"),
    (b"PAR1" + b"x" * 100, ".parquet"),
])
def test_invalid_and_oversized_footers(source, suffix):
    with pytest.raises(ValueError):
        columnar.inspect(io.BytesIO(source), suffix)


def test_metadata_only_inspection_does_not_read_table_body(table):
    source = parquet(table, row_group_size=4)
    class TrackedIO(io.BytesIO):
        def __init__(self, data):
            super().__init__(data)
            self.requests = []
        def read(self, size=-1):
            assert size >= 0, "Unbounded read forbidden"
            self.requests.append((self.tell(), size))
            return super().read(size)
    stream = TrackedIO(source.getvalue())
    assert columnar.inspect(stream, ".parquet")["row_count"] == 12
    assert not stream.closed


def test_feather_corrupt_record_batch_offsets_fail_closed(table):
    source = ipc(table, compression="uncompressed")
    data = bytearray(source.getvalue())
    footer_length = struct.unpack_from("<I", data, len(data) - 10)[0]
    footer_start = len(data) - 10 - footer_length
    flat = columnar._FlatBuffer(bytes(data[footer_start:-10]))
    vector_field = flat.field(flat.root(), 3)
    vector = flat.indirect(vector_field)
    struct.pack_into("<q", data, footer_start + vector + 4, -100)
    with pytest.raises(ValueError, match="metadata"):
        columnar.inspect(io.BytesIO(data), ".feather")
