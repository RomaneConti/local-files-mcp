"""Small real OOXML ZIP fixtures, including hostile archives and XML parts."""

import io
import json
import socket
import struct
import zipfile

import pytest

from local_files_mcp.readers import office


S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def archive(parts, compression=zipfile.ZIP_STORED):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=compression) as package:
        for name, content in parts.items():
            package.writestr(name, content)
    stream.seek(0)
    return stream


def rels(items):
    return f'<Relationships xmlns="{REL}">' + "".join(
        f'<Relationship Id="{identity}" Type="{R}/{kind}" Target="{target}" {extra}/>'
        for identity, kind, target, extra in items) + "</Relationships>"


def content_types(kind):
    return ('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f'<Override PartName="/{kind}" ContentType="application/xml"/></Types>')


def workbook(rows=None, *, extra=None, external=False):
    if rows is None:
        rows = ('<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>12</v></c>'
                '<c r="C1" t="b"><v>1</v></c><c r="D1"><f>WEBSERVICE("https://example.invalid")</f><v>2</v></c>'
                '<c r="E1"><f>1+1</f></c><c r="F1" t="d"><v>2025-01-01</v></c></row>'
                '<row r="3"><c r="A3" t="inlineStr"><is><t>Beta literal [a.*]</t></is></c>'
                '<c r="B3"><v>2.5</v></c></row>')
    parts = {
        "[Content_Types].xml": content_types("xl/workbook.xml"),
        "_rels/.rels": rels([("rId1", "officeDocument", "xl/workbook.xml", "")]),
        "xl/workbook.xml": f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets>'
                           '<sheet name="Data" sheetId="1" r:id="rId1"/>'
                           '<sheet name="Hidden" state="hidden" sheetId="2" r:id="rId2"/>'
                           '</sheets></workbook>',
        "xl/_rels/workbook.xml.rels": rels([
            ("rId1", "worksheet", "https://example.invalid/sheet.xml" if external else "worksheets/sheet1.xml",
             'TargetMode="External"' if external else ""),
            ("rId2", "worksheet", "worksheets/sheet2.xml", ""),
            ("rId3", "externalLink", "file:///private/never-read.xml", 'TargetMode="External"'),
        ]),
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{S}"><dimension ref="A1:XFD1048576"/><sheetData>{rows}</sheetData></worksheet>',
        "xl/worksheets/sheet2.xml": f'<worksheet xmlns="{S}"><sheetData/></worksheet>',
        "xl/sharedStrings.xml": f'<sst xmlns="{S}"><si><r><t>Al</t></r><r><t>pha</t></r></si></sst>',
    }
    parts.update(extra or {})
    return parts


def word(body=None, extra=None):
    body = body if body is not None else (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Introduction</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>Some literal [a.*] content.</w:t><w:tab/><w:t>Tabbed</w:t></w:r></w:p>'
        '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Header</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc></w:tr>'
        '<w:tr><w:tc><w:p><w:r><w:t>Target</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>42</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')
    parts = {
        "[Content_Types].xml": content_types("word/document.xml"),
        "_rels/.rels": rels([("rId1", "officeDocument", "word/document.xml", "")]),
        "word/document.xml": f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>',
        "word/_rels/document.xml.rels": rels([
            ("rId1", "hyperlink", "file:///private/never-read.txt", 'TargetMode="External"')]),
    }
    parts.update(extra or {})
    return parts


def presentation():
    parts = {
        "[Content_Types].xml": content_types("ppt/presentation.xml"),
        "_rels/.rels": rels([("rId1", "officeDocument", "ppt/presentation.xml", "")]),
        "ppt/presentation.xml": f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst>'
                                '<p:sldId id="256" r:id="rId2"/><p:sldId id="257" r:id="rId1"/>'
                                '</p:sldIdLst></p:presentation>',
        "ppt/_rels/presentation.xml.rels": rels([
            ("rId1", "slide", "slides/slide1.xml", ""), ("rId2", "slide", "slides/slide2.xml", "")]),
    }
    for number in (1, 2):
        parts[f"ppt/slides/slide{number}.xml"] = (
            f'<p:sld xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree><p:sp><p:txBody>'
            f'<a:p><a:r><a:t>Slide {number} heading</a:t></a:r></a:p></p:txBody></p:sp>'
            '<p:graphicFrame><a:graphic><a:graphicData><a:tbl><a:tr><a:tc><a:txBody>'
            f'<a:p><a:r><a:t>Cell {number}</a:t></a:r></a:p></a:txBody></a:tc></a:tr>'
            '</a:tbl></a:graphicData></a:graphic></p:graphicFrame></p:spTree></p:cSld></p:sld>')
    return parts


def test_xlsx_inspect_sheets_and_paged_values_not_declared_dimensions():
    stream = archive(workbook())
    first = office.inspect(stream, ".xlsx", limit=1)
    assert first["sheet_count"] == 2 and first["sheets"][0]["name"] == "Data"
    assert first["next_offset"] == 1
    second = office.inspect(stream, ".xlsx", offset=1)
    assert second["sheets"][0]["state"] == "hidden"
    page = office.read_table(stream, ".xlsx", columns=["A", "B", "C", "D", "E", "F"], limit=1)
    assert page["rows"][0]["values"] == ["Alpha", 12, True, 2, None, "2025-01-01"]
    assert page["rows"][0]["formula_columns"] == ["D", "E"]
    assert page["rows"][0]["uncached_formula_columns"] == ["E"]
    assert page["next_offset"] == 1
    page = office.read_table(stream, ".xlsx", columns=["A", "B"], offset=1)
    assert page["rows"][0]["row_number"] == 3
    assert page["rows"][0]["values"] == ["Beta literal [a.*]", 2.5]
    assert page["complete"] and page["next_offset"] is None
    assert not stream.closed


def test_macro_formulas_and_external_references_are_never_run_or_opened(monkeypatch):
    stream = archive(workbook(extra={"xl/vbaProject.bin": b"not executable", "xl/externalLinks/externalLink1.xml": b"not parsed"}))
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("network access"))
    monkeypatch.setattr("builtins.open", lambda *a, **k: pytest.fail("filesystem path open"))
    metadata = office.inspect(stream, ".xlsm")
    assert not metadata["macros_executed"] and not metadata["formulas_evaluated"]
    assert office.read_table(stream, ".xlsm", columns=["D"])["rows"][0]["values"] == [2]


def test_external_required_worksheet_fails_closed():
    stream = archive(workbook(external=True))
    assert office.inspect(stream, ".xlsx")["sheets"][0]["external"]
    with pytest.raises(ValueError, match="External worksheet"):
        office.read_table(stream, ".xlsx")


def test_xlsx_literal_search_and_continuation(monkeypatch):
    rows = "".join(f'<row r="{i + 1}"><c r="A{i + 1}" t="inlineStr"><is><t>{"needle" if i == 4 else "other"}</t></is></c></row>' for i in range(5))
    stream = archive(workbook(rows))
    monkeypatch.setattr(office, "MAX_SEARCH_UNITS", 2)
    first = office.search(stream, ".xlsx", "needle")
    assert first["matches"] == [] and first["next_offset"] == 2 and not first["complete"]
    second = office.search(stream, ".xlsx", "needle", offset=2)
    assert second["next_offset"] == 4
    third = office.search(stream, ".xlsx", "NEEDLE", offset=4)
    assert third["matches"][0]["row_number"] == 5 and third["complete"]
    assert office.search(archive(workbook()), ".xlsx", "[a.*]")["count"] == 1
    assert office.search(archive(workbook()), ".xlsx", "BETA", case_sensitive=True)["count"] == 0


def test_docx_headings_paragraphs_tables_and_literal_search():
    stream = archive(word())
    first = office.read_document(stream, ".docx", limit=1)
    assert first["units"][0]["type"] == "heading"
    assert first["units"][0]["heading_level"] == 1 and first["next_offset"] == 1
    assert "\t" in office.read_document(stream, ".docx", start=1, limit=1)["units"][0]["text"]
    table = office.read_document(stream, ".docx", start=2)["units"][0]
    assert table["selector"] == "table:0" and table["rows"][1] == ["Target", "42"]
    assert office.read_table(stream, ".docx", "table:0", columns=["B"], offset=1)["rows"][0]["values"] == ["42"]
    assert office.search(stream, ".docx", "[a.*]")["count"] == 1
    assert office.search(stream, ".docx", "Target")["matches"][0]["offset"] == 2
    assert office.search(stream, ".docx", '"type"')["count"] == 0


def test_pptx_preserves_presentation_order_and_reads_slide_tables():
    stream = archive(presentation())
    first = office.inspect(stream, ".pptx", limit=1)
    assert first["units"][0]["paragraphs"] == ["Slide 2 heading"]
    assert first["next_offset"] == 1
    assert first["units"][0]["tables"][0]["selector"] == "slide:0/table:0"
    second = office.read_document(stream, ".pptx", start=1)
    assert second["units"][0]["paragraphs"] == ["Slide 1 heading"]
    assert office.read_table(stream, ".pptx", "slide:1/table:0", columns=["A"])["rows"][0]["values"] == ["Cell 1"]
    assert office.search(stream, ".pptx", "Cell 2")["count"] == 1


def test_bounded_responses_page_large_rows():
    value = "汉" * 1000
    rows = "".join(f'<row r="{i}">' + "".join(
        f'<c r="{chr(65 + j)}{i}" t="inlineStr"><is><t>{value}</t></is></c>' for j in range(10)) + '</row>'
        for i in range(1, 5))
    result = office.read_table(archive(workbook(rows)), ".xlsx", column_limit=6)
    assert len(json.dumps(result, ensure_ascii=False).encode()) < 64 * 1024
    assert result["next_offset"] is not None and result["rows"]


def test_search_beyond_short_preview_and_reports_scan_text_truncation(monkeypatch):
    stream = archive(word('<w:p><w:r><w:t>' + "a" * 6000 + 'needle</w:t></w:r></w:p>'))
    page = office.read_document(stream, ".docx")
    assert page["units"][0]["truncated"]
    result = office.search(stream, ".docx", "needle")
    assert result["count"] == 1 and result["complete"]
    monkeypatch.setattr(office, "MAX_SEARCH_TEXT", 4000)
    result = office.search(stream, ".docx", "needle")
    assert result["text_truncated"] and not result["complete"]
    assert result["next_offset"] is None


@pytest.mark.parametrize("unsafe", ["../outside.xml", "/absolute.xml", "C:/outside.xml", "word\\outside.xml", "word/../outside.xml"])
def test_zip_member_traversal_is_rejected(unsafe):
    # ZipInfo normalizes backslashes while writing on Windows; mutate both
    # header names afterwards to exercise an actual hostile ZIP name.
    if "\\" in unsafe:
        normal = unsafe.replace("\\", "/")
        stream = archive(word(extra={normal: b"bad"}))
        stream = io.BytesIO(stream.getvalue().replace(normal.encode(), unsafe.encode()))
    else:
        stream = archive(word(extra={unsafe: b"bad"}))
    with pytest.raises(ValueError, match="Unsafe"):
        office.read_document(stream, ".docx")


def test_zip_bomb_rejected_before_xml_parsing(monkeypatch):
    stream = archive(word(extra={"word/bomb.xml": b"A" * (2 * 1024 * 1024)}), zipfile.ZIP_DEFLATED)
    monkeypatch.setattr(office.XML, "iterparse", lambda *a, **k: pytest.fail("parsed before preflight"))
    with pytest.raises(ValueError, match="expansion"):
        office.read_document(stream, ".docx")


@pytest.mark.parametrize("xml", [
    b'<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///private/file">]><x>&secret;</x>',
    b'<!DOCTYPE x SYSTEM "https://example.invalid/x.dtd"><x/>',
    b'<notclosed>',
])
def test_unsafe_and_corrupt_xml_fails_closed(xml):
    with pytest.raises(ValueError, match="unsafe|unsupported"):
        office.read_document(archive(word(extra={"word/document.xml": xml})), ".docx")


def test_xml_depth_limit():
    xml = f'<w:document xmlns:w="{W}"><w:body><w:p>' + '<w:r>' * 100 + '</w:r>' * 100 + '</w:p></w:body></w:document>'
    with pytest.raises(ValueError, match="nesting"):
        office.read_document(archive(word(extra={"word/document.xml": xml})), ".docx")


def test_xml_size_limit_before_parse(monkeypatch):
    monkeypatch.setattr(office, "MAX_XML_BYTES", 100)
    # Default arguments are bound at function definition, so exercise the
    # package's explicit per-operation decompression budget instead.
    with pytest.raises(ValueError, match="budget"):
        office.read_document(archive(word()), ".docx")


def test_relationship_traversal_is_not_followed():
    parts = workbook()
    parts["xl/_rels/workbook.xml.rels"] = rels([
        ("rId1", "worksheet", "../outside.xml", ""), ("rId2", "worksheet", "worksheets/sheet2.xml", "")])
    with pytest.raises(ValueError, match="Unsafe internal"):
        office.inspect(archive(parts), ".xlsx")


@pytest.mark.parametrize("cell", ['<c r="A1" t="s"><v>9000</v></c>', '<c r="XFE1"><v>1</v></c>', '<c r="A2"><v>1</v></c>', '<c r="A1"><v>Infinity</v></c>'])
def test_invalid_cells_fail_closed(cell):
    with pytest.raises(ValueError):
        office.read_table(archive(workbook('<row r="1">' + cell + '</row>')), ".xlsx")


@pytest.mark.parametrize("arguments", [{"columns": ["A", "A"]}, {"columns": ["XFE"]}, {"limit": 101}, {"offset": -1}, {"column_limit": 21}, {"columns": [True]}])
def test_invalid_table_arguments(arguments):
    with pytest.raises(ValueError):
        office.read_table(archive(workbook()), ".xlsx", **arguments)


def test_central_directory_count_limit_before_zipfile_allocation(monkeypatch):
    stream = archive(word())
    raw = bytearray(stream.getvalue())
    location = raw.rfind(b"PK\x05\x06")
    struct.pack_into("<H", raw, location + 10, 65535)
    monkeypatch.setattr(zipfile, "ZipFile", lambda *a, **k: pytest.fail("allocated before count preflight"))
    with pytest.raises(ValueError, match="directory"):
        office.read_document(io.BytesIO(raw), ".docx")


def test_corrupt_zip_and_missing_parts_are_clear_errors():
    with pytest.raises(ValueError, match="ZIP"):
        office.read_document(io.BytesIO(b"not a zip"), ".docx")
    with pytest.raises(ValueError, match="missing"):
        office.read_document(archive({"other.xml": b"<x/>"}), ".docx")
