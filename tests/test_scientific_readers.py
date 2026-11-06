"""Scientific-reader fixtures are in-memory or inside pytest-owned paths only."""

import io
import json
import os
import struct
import zlib

import h5py
import numpy as np
import pytest

from local_files_mcp.readers import scientific


def hdf(array=None, **options):
    stream = io.BytesIO()
    with h5py.File(stream, "w") as file:
        if array is not None:
            file.create_dataset("data", data=array, **options)
    return stream


def tag(kind, payload, endian="<", small=False):
    if small:
        assert 0 < len(payload) <= 4
        return struct.pack(endian + "I", kind | len(payload) << 16) + payload.ljust(4, b"\0")
    return struct.pack(endian + "II", kind, len(payload)) + payload + b"\0" * (-len(payload) % 8)


def matrix(name, array, endian="<", *, complex_values=None, logical=False, class_id=6):
    array = np.asarray(array, dtype=endian + "f8")
    flags = class_id | (0x800 if complex_values is not None else 0) | (0x200 if logical else 0)
    payload = tag(6, struct.pack(endian + "II", flags, 0), endian)
    payload += tag(5, struct.pack(endian + "i" * array.ndim, *array.shape), endian)
    payload += tag(1, name.encode(), endian, small=len(name) <= 4)
    if class_id == 6:
        payload += tag(9, array.tobytes(order="F"), endian)
        if complex_values is not None:
            payload += tag(9, np.asarray(complex_values, dtype=endian + "f8").tobytes(order="F"), endian)
    return tag(14, payload, endian)


def mat5(*elements, endian="<", compressed=False):
    header = b"MATLAB 5.0 MAT-file, test fixture".ljust(116, b" ") + b"\0" * 8
    header += struct.pack(endian + "H", 0x100) + (b"IM" if endian == "<" else b"MI")
    if compressed:
        # MAT miCOMPRESSED elements have no outer eight-byte alignment padding.
        elements = [struct.pack(endian + "II", 15, len(payload := zlib.compress(element))) + payload
                    for element in elements]
    return io.BytesIO(header + b"".join(elements))


def mat4(name, array, endian="<", imaginary=None):
    array = np.asarray(array, dtype=endian + "f8")
    header = struct.pack(endian + "5i", 0 if endian == "<" else 1000, *array.shape,
                         int(imaginary is not None), len(name.encode()) + 1)
    content = header + name.encode() + b"\0" + array.tobytes(order="F")
    if imaginary is not None:
        content += np.asarray(imaginary, dtype=endian + "f8").tobytes(order="F")
    return content


class CountingFile(io.BytesIO):
    read_bytes = 0

    def read(self, count=-1):
        result = super().read(count)
        self.read_bytes += len(result)
        return result


def test_hdf_metadata_pagination_and_flat_and_rectangular_slices():
    stream = hdf(np.arange(60).reshape(5, 12), chunks=(2, 4), compression="gzip", shuffle=True)
    metadata = scientific.inspect(stream, ".h5")
    assert metadata["members"][0]["shape"] == [5, 12]
    assert scientific.read_array(stream, ".h5", offset=10, count=5)["values"] == [10, 11, 12, 13, 14]
    result = scientific.read_array(stream, ".h5", start=[1, 2], shape=[2, 3])
    assert result["values"] == [14, 15, 16, 26, 27, 28]
    assert result["selection"] == {"start": [1, 2], "shape": [2, 3]}
    assert not stream.closed


def test_hdf_huge_contiguous_array_reads_only_a_small_sample():
    stream = CountingFile(hdf(np.arange(1000000, dtype=np.int32)).getvalue())
    result = scientific.read_array(stream, ".hdf5", offset=999997, count=2)
    assert result["values"] == [999997, 999998]
    assert stream.read_bytes < 128 * 1024


def test_hdf_scalar_empty_and_nonfinite_values_are_json_safe():
    assert scientific.read_array(hdf(np.array(3)), ".h5")["values"] == [3]
    assert scientific.read_array(hdf(np.empty((0, 3))), ".h5")["values"] == []
    result = scientific.read_array(hdf(np.array([complex(1, float("nan")), complex(float("inf"), 2)])), ".h5")
    assert result["values"][0] == {"real": 1.0, "imag": "NaN"}
    json.dumps(result, allow_nan=False)


def test_hdf_group_pagination_does_not_recurse_into_hard_link_cycles():
    stream = io.BytesIO()
    with h5py.File(stream, "w") as file:
        group = file.create_group("group")
        group["cycle"] = group
        group.create_dataset("value", data=[1, 2])
    first = scientific.inspect(stream, ".h5", "group", limit=1)
    assert first["members"][0]["member"] == "group/cycle"
    assert first["next_offset"] == 1
    second = scientific.inspect(stream, ".h5", "group", offset=1)
    assert second["members"][0]["kind"] == "dataset"


@pytest.mark.parametrize("link_kind", ["external", "soft"])
def test_hdf_links_never_follow_another_file_or_group(link_kind):
    stream = io.BytesIO()
    with h5py.File(stream, "w") as file:
        file["linked"] = (h5py.ExternalLink("MUST-NOT-OPEN-THIS-FILE.h5", "/secret")
                          if link_kind == "external" else h5py.SoftLink("/missing"))
    metadata = scientific.inspect(stream, ".h5")
    assert metadata["members"][0]["readable"] is False
    assert "MUST-NOT-OPEN" not in str(metadata)
    for member in ("linked", "linked/child"):
        with pytest.raises(ValueError, match="links are not followed"):
            scientific.read_array(stream, ".h5", member=member)


def test_hdf_virtual_and_external_storage_rejected_before_dataset_shape_reads():
    stream = io.BytesIO()
    with h5py.File(stream, "w") as file:
        virtual = h5py.VirtualLayout(shape=(10,), dtype="i4")
        virtual[:] = h5py.VirtualSource("MUST-NOT-OPEN.h5", "secret", shape=(10,))
        file.create_virtual_dataset("virtual", virtual)
        file.create_dataset("external", shape=(10,), dtype="i4", external=[("MUST-NOT-OPEN.raw", 0, 40)])
    metadata = scientific.inspect(stream, ".h5")
    assert all(item["readable"] is False for item in metadata["members"])
    for name in ("virtual", "external"):
        with pytest.raises(ValueError, match="not read"):
            scientific.read_array(stream, ".h5", member=name)


@pytest.mark.parametrize("kind", ["strings", "references"])
def test_hdf_object_and_variable_length_values_are_metadata_only(kind):
    stream = io.BytesIO()
    with h5py.File(stream, "w") as file:
        file.create_dataset("unsafe", shape=(2,), dtype=h5py.string_dtype() if kind == "strings" else h5py.ref_dtype)
    assert scientific.inspect(stream, ".h5")["members"][0]["readable"] is False
    with pytest.raises(ValueError, match="variable-length"):
        scientific.read_array(stream, ".h5")


def test_hdf_oversized_chunks_rejected_before_allocating_array_data():
    stream = io.BytesIO()
    with h5py.File(stream, "w") as file:
        file.create_dataset("data", shape=(2000000,), dtype="f8", chunks=(2000000,), compression="gzip")
    with pytest.raises(ValueError, match="chunk exceeds"):
        scientific.read_array(stream, ".h5", count=1)


def test_hdf_aggregate_chunk_budget_is_checked(monkeypatch):
    stream = hdf(np.arange(12).reshape(3, 4), chunks=(1, 4), compression="gzip")
    monkeypatch.setattr(scientific, "MAX_DECODE_BYTES", 40)
    with pytest.raises(ValueError, match="chunks exceed"):
        scientific.read_array(stream, ".h5", start=[0, 0], shape=[3, 1])


def test_hdf_unknown_filter_is_rejected_without_loading_plugin():
    stream = io.BytesIO()
    with h5py.File(stream, "w") as file:
        properties = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
        properties.set_chunk((4,))
        properties.set_filter(65000, h5py.h5z.FLAG_OPTIONAL)
        space = h5py.h5s.create_simple((4,))
        h5py.h5d.create(file.id, b"data", h5py.h5t.NATIVE_INT32, space, dcpl=properties)
    with pytest.raises(ValueError, match="filter plugins"):
        scientific.read_array(stream, ".h5")
    assert os.environ["HDF5_PLUGIN_PRELOAD"] == "::"
    assert h5py.h5pl.size() == 0


def test_hdf_attributes_are_not_loaded(monkeypatch):
    stream = hdf(np.arange(3))

    def blocked(*args, **kwargs):
        pytest.fail("No attribute data should be read")

    monkeypatch.setattr(h5py.AttributeManager, "__getitem__", blocked)
    assert scientific.inspect(stream, ".h5")["members"][0]["attributes_read"] is False
    assert scientific.read_array(stream, ".h5")["values"] == [0, 1, 2]


def test_mat_v73_userblock_routes_to_same_hdf_safety_rules():
    stream = io.BytesIO()
    with h5py.File(stream, "w", userblock_size=512) as file:
        file.create_dataset("matrix", data=np.arange(6).reshape(2, 3))
    stream.seek(0)
    stream.write(b"MATLAB 7.3 MAT-file, test fixture".ljust(128, b" "))
    assert scientific.inspect(stream, ".mat")["format"] == "mat-v7.3"
    result = scientific.read_array(stream, ".mat", member="matrix", start=[1, 1], shape=[1, 2])
    assert result["values"] == [4, 5]
    assert result["order"] == "C"


@pytest.mark.parametrize("endian", ["<", ">"])
@pytest.mark.parametrize("compressed", [False, True])
def test_mat5_numeric_metadata_and_storage_order_reads(endian, compressed):
    array = np.arange(12).reshape(3, 4)
    stream = mat5(matrix("a", array, endian), matrix("second", array + 20, endian),
                  endian=endian, compressed=compressed)
    first = scientific.inspect(stream, ".mat", limit=1)
    assert first["members"][0]["shape"] == [3, 4]
    assert first["next_offset"] == 1
    assert scientific.inspect(stream, ".mat", offset=1)["members"][0]["member"] == "second"
    result = scientific.read_array(stream, ".mat", member="a", offset=2, count=5)
    assert result["values"] == array.ravel(order="F")[2:7].tolist()
    assert result["order"] == "F"
    rectangle = scientific.read_array(stream, ".mat", member="second", start=[1, 1], shape=[2, 2])
    assert rectangle["values"] == (array + 20)[1:3, 1:3].ravel(order="F").tolist()
    assert not stream.closed


def test_large_uncompressed_mat_seeks_without_loading_entire_variable():
    stream = CountingFile(mat5(matrix("large", np.arange(1000000).reshape(1000, 1000))).getvalue())
    result = scientific.read_array(stream, ".mat", offset=999998, count=2)
    assert result["values"] == [998999.0, 999999.0]
    assert stream.read_bytes < 2048


def test_mat_compression_bomb_rejected_with_expanded_budget(monkeypatch):
    stream = mat5(matrix("wide", np.zeros((100, 100))), compressed=True)
    monkeypatch.setattr(scientific, "MAX_MAT_EXPANDED", 1024)
    with pytest.raises(ValueError, match="decompression budget"):
        scientific.inspect(stream, ".mat")


def test_mat5_complex_logical_and_unsupported_classes():
    real = np.array([[1, 2], [3, 4]])
    stream = mat5(matrix("z", real, complex_values=real + 1))
    values = scientific.read_array(stream, ".mat")["values"]
    assert values == [{"real": 1.0, "imag": 2.0}, {"real": 3.0, "imag": 4.0},
                      {"real": 2.0, "imag": 3.0}, {"real": 4.0, "imag": 5.0}]
    logical = mat5(matrix("x", np.array([[0, 1]]), logical=True))
    assert scientific.read_array(logical, ".mat")["values"] == [False, True]
    cell = mat5(matrix("cell", real, class_id=1))
    assert scientific.inspect(cell, ".mat")["members"][0]["readable"] is False
    with pytest.raises(ValueError, match="dense numeric"):
        scientific.read_array(cell, ".mat")


@pytest.mark.parametrize("endian", ["<", ">"])
def test_mat4_numeric_and_complex_arrays(endian):
    array = np.arange(6).reshape(2, 3)
    stream = io.BytesIO(mat4("one", array, endian) + mat4("two", array, endian, imaginary=array + 10))
    assert scientific.inspect(stream, ".mat")["format"] == "mat-v4"
    assert scientific.read_array(stream, ".mat", "one", offset=2, count=3)["values"] == [1., 4., 2.]
    assert scientific.read_array(stream, ".mat", "two", count=1)["values"] == [{"real": 0., "imag": 10.}]


@pytest.mark.parametrize("suffix,payload", [(".h5", b"invalid hdf"), (".mat", b""),
                                            (".mat", b"not a mat file at all")])
def test_corrupt_scientific_data_fails_closed(suffix, payload):
    with pytest.raises(ValueError):
        scientific.inspect(io.BytesIO(payload), suffix)


def test_mat_truncated_payload_and_ambiguous_names_rejected():
    stream = mat5(matrix("a", np.arange(6).reshape(2, 3)))
    with pytest.raises(ValueError, match="container|padding"):
        scientific.read_array(io.BytesIO(stream.getvalue()[:-1]), ".mat")
    duplicate = mat5(matrix("a", np.array([[1]])), matrix("a", np.array([[2]])))
    with pytest.raises(ValueError, match="Duplicate"):
        scientific.inspect(duplicate, ".mat")


@pytest.mark.parametrize("options", [{"count": 4097}, {"offset": -1},
    {"start": [0], "shape": [1]}, {"start": [0, 0], "shape": [5000, 1]},
    {"offset": 1, "start": [0, 0], "shape": [1, 1]}])
def test_invalid_array_ranges_rejected(options):
    for stream, suffix in ((hdf(np.arange(6).reshape(2, 3)), ".h5"),
                            (mat5(matrix("x", np.arange(6).reshape(2, 3))), ".mat")):
        with pytest.raises(ValueError):
            scientific.read_array(stream, suffix, **options)


@pytest.mark.parametrize("suffix", [".pkl", ".pickle", ".joblib", ".pt", ".pth"])
def test_unsafe_extensions_never_deserialized(suffix):
    with pytest.raises(ValueError, match="Expected"):
        scientific.read_array(io.BytesIO(b"do-not-deserialize"), suffix)
