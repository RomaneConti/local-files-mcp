"""Actual in-memory PDFs exercising text, pagination and parser limits."""

import io
import socket
import subprocess

import pytest
from pypdf import PdfWriter, get_configuration
from pypdf.actions import JavaScript
from pypdf.generic import (
    ArrayObject, DecodedStreamObject, DictionaryObject, NameObject,
    NumberObject, TextStringObject,
)

from local_files_mcp.readers import pdf


def make_pdf(texts=("First Page",), *, content=None, compress=False, encrypted=False,
             actions=False, metadata=None, xmp=None):
    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
    }))
    for text in texts:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        })
        stream = DecodedStreamObject()
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.set_data(content if content is not None else
                        f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin1"))
        page[NameObject("/Contents")] = writer._add_object(stream.flate_encode() if compress else stream)
        if actions:
            page[NameObject("/Annots")] = ArrayObject([writer._add_object(DictionaryObject({
                NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Link"),
                NameObject("/Rect"): ArrayObject([NumberObject(0)] * 4),
                NameObject("/A"): DictionaryObject({NameObject("/S"): NameObject("/URI"),
                                                   NameObject("/URI"): TextStringObject("https://example.invalid/never-fetch")}),
            }))])
    writer.add_metadata(metadata or {"/Title": "Fixture title", "/Author": "Fixture author"})
    if actions:
        writer.add_open_action(JavaScript("app.launchURL('https://example.invalid/never-run');"))
        writer.add_attachment("ignored.txt", b"not extracted")
    if xmp is not None:
        stream = DecodedStreamObject()
        stream.set_data(xmp)
        stream[NameObject("/Type")] = NameObject("/Metadata")
        stream[NameObject("/Subtype")] = NameObject("/XML")
        writer.root_object[NameObject("/Metadata")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("test-password")
    result = io.BytesIO()
    writer.write(result)
    writer.close()
    result.seek(0)
    return result


def test_pdf_metadata_pages_and_borrowed_handle_remains_open():
    stream = make_pdf(("One", "Two", "Three"))
    metadata = pdf.inspect(stream, ".pdf", limit=1)
    assert metadata["page_count"] == 3 and len(metadata["metadata"]) == 1
    assert metadata["next_offset"] == 1
    assert pdf.inspect(stream)["metadata"]["/Title"] == "Fixture title"
    first = pdf.read_document(stream, ".pdf", limit=1)
    assert first["pages"][0]["page"] == 0 and first["pages"][0]["text"] == "One"
    assert first["next_offset"] == 1
    rest = pdf.read_document(stream, ".pdf", start=1)
    assert [page["text"] for page in rest["pages"]] == ["Two", "Three"]
    assert rest["next_offset"] is None and not stream.closed


def test_pdf_text_offsets():
    text = "a" * 200 + "TARGET" + "b" * 300
    stream = make_pdf((text,))
    first = pdf.read_document(stream, max_chars=128)
    assert first["pages"][0]["text"] == text[:128]
    assert first["pages"][0]["next_text_offset"] == 128
    second = pdf.read_document(stream, text_offset=128, max_chars=128)
    assert second["pages"][0]["text"] == text[128:256]


def test_literal_search_page_budget_and_limit():
    stream = make_pdf(tuple("Other" for _ in range(10)) + ("Contains [a.*] Needle", "Needle again"))
    first = pdf.search(stream, ".pdf", "[a.*]")
    assert first["matches"] == [] and first["scanned_pages"] == 10
    assert first["next_offset"] == 10 and not first["complete"]
    second = pdf.search(stream, ".pdf", "NEEDLE", offset=10, limit=1)
    assert second["matches"][0]["page"] == 10
    assert "Needle" in second["matches"][0]["text"]
    assert second["next_offset"] == 11
    third = pdf.search(stream, ".pdf", "Needle", offset=11)
    assert third["complete"] and third["next_offset"] is None
    assert pdf.search(stream, ".pdf", "NEEDLE", offset=10, case_sensitive=True)["matches"] == []


def test_casefold_search_preserves_original_excerpt_coordinates():
    text = "ß" * 300 + " TARGET Case Preserved"
    result = pdf.search(make_pdf((text,)), ".pdf", "target")
    assert "TARGET Case Preserved" in result["matches"][0]["text"]
    assert pdf.search(make_pdf(("Straße",)), ".pdf", "STRASSE")["matches"][0]["text"] == "Straße"


def test_blank_text_layer_is_explicitly_not_ocr():
    stream = make_pdf(("",))
    assert pdf.read_document(stream)["pages"][0]["text"] == ""
    assert "OCR" in pdf.inspect(stream)["note"]


def test_javascript_links_attachments_and_xmp_never_execute_or_open_paths(monkeypatch):
    stream = make_pdf(actions=True, xmp=b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///private/never-read">]><x>&e;</x>')
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("network access"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("process execution"))
    monkeypatch.setattr("builtins.open", lambda *a, **k: pytest.fail("filesystem path open"))
    assert pdf.inspect(stream)["page_count"] == 1
    assert pdf.read_document(stream)["pages"][0]["text"] == "First Page"
    assert pdf.search(stream, ".pdf", "First")["matches"]


def test_encrypted_pdf_is_rejected_without_password_prompt():
    with pytest.raises(ValueError, match="Encrypted|encrypted"):
        pdf.inspect(make_pdf(encrypted=True))


@pytest.mark.parametrize("data", [b"", b"not a PDF", b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF"])
def test_corrupt_pdf_errors_are_normalized(data):
    with pytest.raises(ValueError, match="Malformed|PDF"):
        pdf.inspect(io.BytesIO(data))


def test_large_file_rejected_before_library_allocation(monkeypatch):
    stream = make_pdf()
    monkeypatch.setattr(pdf, "MAX_FILE_BYTES", 32)
    monkeypatch.setattr(pdf, "PdfReader", lambda *a, **k: pytest.fail("library allocation"))
    with pytest.raises(ValueError, match="64 MiB"):
        pdf.inspect(stream)


def test_compressed_stream_bomb_is_rejected_and_limits_restore():
    before = get_configuration()
    stream = make_pdf(content=b" " * (pdf.MAX_STREAM_BYTES + 1), compress=True)
    assert len(stream.getvalue()) < 10000
    with pytest.raises(ValueError, match="Malformed|complex|limit"):
        pdf.read_document(stream)
    assert get_configuration() is before


def test_declared_stream_limit_rejects_oversized_uncompressed_content():
    with pytest.raises(ValueError, match="Malformed|budget|complex|limit"):
        pdf.read_document(make_pdf(content=b" " * (pdf.MAX_STREAM_BYTES + 1)))


def test_page_tree_limit(monkeypatch):
    stream = make_pdf(("One", "Two", "Three"))
    monkeypatch.setattr(pdf, "MAX_PAGES", 2)
    with pytest.raises(ValueError, match="page|Malformed"):
        pdf.inspect(stream)


def test_text_and_nontext_operator_limits(monkeypatch):
    monkeypatch.setattr(pdf, "MAX_TEXT", 10)
    with pytest.raises(ValueError, match="text"):
        pdf.read_document(make_pdf(("Text beyond ten characters",)))
    monkeypatch.setattr(pdf, "MAX_TEXT", 131072)
    monkeypatch.setattr(pdf, "MAX_OPERATORS", 5)
    with pytest.raises(ValueError, match="operator"):
        pdf.read_document(make_pdf(content=b"q Q q Q q Q q Q"))


@pytest.mark.parametrize("arguments", [{"query": None}, {"query": []}, {"query": ""},
                                        {"query": "x", "case_sensitive": 1}, {"query": "x", "selector": "ignored"}])
def test_invalid_search_arguments(arguments):
    with pytest.raises(ValueError):
        pdf.search(make_pdf(), ".pdf", **arguments)


def test_oversized_metadata_is_bounded():
    metadata = {"/Title": "A" * 10000, **{f"/Key{i}": str(i) for i in range(40)}}
    result = pdf.inspect(make_pdf(metadata=metadata))
    assert result["metadata_truncated"] and len(result["metadata"]) == 32
    assert result["metadata"]["/Title"]["truncated"]
