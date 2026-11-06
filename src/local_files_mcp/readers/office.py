"""Bounded, read-only OOXML text readers over an already approved binary handle.

Only XML parts inside the package are inspected. External relationships, OLE
objects, macros, formulas, images and linked workbooks are never opened/run.
This is a text/data preview, not an Office renderer or formula evaluator.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import math
import posixpath
import re
from typing import BinaryIO

from defusedxml import ElementTree as XML
from defusedxml.common import DefusedXmlException


MAX_XML_BYTES = 16 * 1024 * 1024
MAX_SMALL_XML_BYTES = 2 * 1024 * 1024
MAX_XML_NODES = 250_000
MAX_XML_DEPTH = 64
MAX_SHARED_TEXT = 4 * 1024 * 1024
MAX_SEARCH_TEXT = 4 * 1024 * 1024
MAX_SHARED_STRINGS = 100_000
MAX_PREFIX_UNITS = 100_000
MAX_SEARCH_UNITS = 1000
MAX_CELL_TEXT = 1024
MAX_TEXT = 2048
MAX_RESULT_BYTES = 48 * 1024
MAX_COLUMNS = 20

_SHEETS = {".xlsx", ".xlsm"}
_FORMATS = _SHEETS | {".docx", ".pptx"}
_CELL = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})\Z")
_NS = {
    "s": {"http://schemas.openxmlformats.org/spreadsheetml/2006/main",
          "http://purl.oclc.org/ooxml/spreadsheetml/main"},
    "w": {"http://schemas.openxmlformats.org/wordprocessingml/2006/main",
          "http://purl.oclc.org/ooxml/wordprocessingml/main"},
    "p": {"http://schemas.openxmlformats.org/presentationml/2006/main",
          "http://purl.oclc.org/ooxml/presentationml/main"},
}


def _int(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}")
    return value


def _suffix(suffix):
    if not isinstance(suffix, str) or suffix.lower() not in _FORMATS:
        raise ValueError("Expected .xlsx, .xlsm, .docx or .pptx")
    return suffix.lower()


def _tag(element):
    return element.tag.rsplit("}", 1)[-1]


def _attribute(element, name, default=""):
    for key, value in element.attrib.items():
        if key.rsplit("}", 1)[-1] == name:
            return value
    return default


def _child(element, name):
    return next((item for item in element if _tag(item) == name), None)


def _children(element, name):
    return (item for item in element if _tag(item) == name)


def _root_type(element, kind, name):
    namespace = element.tag[1:].split("}", 1)[0] if element.tag.startswith("{") else ""
    if _tag(element) != name or namespace not in _NS[kind]:
        raise ValueError("The Office package contains an unsupported XML document type")


def _clip(value, limit=MAX_CELL_TEXT):
    return value[:limit]


def _text(element, limit=MAX_TEXT):
    parts, used, truncated = [], 0, False
    for item in element.iter():
        tag = _tag(item)
        value = (item.text or "") if tag == "t" else "\t" if tag == "tab" else "\n" if tag in {"br", "cr"} else ""
        if value:
            room = limit - used
            parts.append(value[:room])
            used += min(len(value), room)
            if len(value) > room:
                truncated = True
    return "".join(parts), truncated


def _fits(result, key, item):
    candidate = {**result, key: result[key] + [item]}
    if len(json.dumps(candidate, ensure_ascii=False, allow_nan=False).encode()) > MAX_RESULT_BYTES:
        return False
    result[key].append(item)
    return True


class _Package:
    """Adapter around shared ZIP guards; only vetted ZIP member handles escape."""

    def __init__(self, source):
        from .common import safe_zip
        self._context = safe_zip(source)

    def __enter__(self):
        self.archive = self._context.__enter__()
        self.names = set(self.archive.namelist())
        self.xml_bytes = 0
        return self

    def __exit__(self, *args):
        return self._context.__exit__(*args)

    @contextmanager
    def open_xml(self, name, limit=MAX_XML_BYTES):
        if name not in self.names:
            raise ValueError("A required Office XML part is missing")
        info = self.archive.getinfo(name)
        if info.file_size > limit:
            raise ValueError("Office XML part exceeds the bounded preview size; export a smaller document/table")
        with self.archive.open(info) as raw:
            package = self

            class Limited:
                def read(self, size=-1):
                    if size < 0:
                        size = min(limit + 1, 65536)
                    if package.xml_bytes + size > 2 * MAX_XML_BYTES:
                        raise ValueError("Office preview exhausted its XML decompression budget")
                    data = raw.read(size)
                    package.xml_bytes += len(data)
                    return data

            yield Limited()

    def xml(self, name):
        with self.open_xml(name, MAX_SMALL_XML_BYTES) as source:
            root = None
            for event, element, stack in _events(source):
                if root is None:
                    root = element
            if root is None:
                raise ValueError("Empty Office XML part")
            return root


def _events(source):
    stack, count = [], 0
    try:
        for event, element in XML.iterparse(source, events=("start", "end"),
                                             forbid_dtd=True, forbid_entities=True, forbid_external=True):
            if event == "start":
                count += 1
                stack.append(element)
                if len(stack) > MAX_XML_DEPTH or count > MAX_XML_NODES:
                    raise ValueError("Office XML nesting/node count exceeds the preview limit")
            yield event, element, stack
            if event == "end":
                stack.pop()
    except (XML.ParseError, DefusedXmlException, UnicodeError) as exc:
        raise ValueError("Invalid or unsafe Office XML; DTDs/entities/external references are forbidden") from exc


def _items(package, name, kind, root_name, parent_name, item_names):
    with package.open_xml(name) as source:
        capturing = None
        for event, element, stack in _events(source):
            if event == "start":
                if len(stack) == 1:
                    _root_type(element, kind, root_name)
                if capturing is None and len(stack) > 1 and _tag(stack[-2]) == parent_name and _tag(element) in item_names:
                    capturing = element
            else:
                if element is capturing:
                    yield element
                    capturing = None
                if capturing is None and len(stack) > 1:
                    stack[-2].remove(element)
                    element.clear()


def _relationships(package, part, expected):
    folder, basename = posixpath.split(part)
    root = package.xml(posixpath.join(folder, "_rels", basename + ".rels"))
    if _tag(root) != "Relationships":
        raise ValueError("Invalid Office relationship part")
    relationships = {}
    for rel in root:
        if _tag(rel) != "Relationship" or not rel.get("Type", "").endswith("/" + expected):
            continue
        identity = rel.get("Id", "")
        if not identity or identity in relationships:
            raise ValueError("Duplicate or missing Office relationship ID")
        # An external target is recorded as unavailable, never fetched.
        if rel.get("TargetMode", "Internal").lower() != "internal":
            relationships[identity] = None
            continue
        target = rel.get("Target", "")
        if (not target or "\\" in target or ":" in target or "%" in target or
                "?" in target or "#" in target or any(ord(c) < 32 for c in target)):
            raise ValueError("Unsafe internal Office relationship target")
        pieces = target.lstrip("/").split("/")
        if any(piece in ("", ".", "..") for piece in pieces):
            raise ValueError("Unsafe internal Office relationship target")
        resolved = target[1:] if target.startswith("/") else posixpath.join(folder, target)
        if resolved not in package.names:
            raise ValueError("An Office relationship refers to a missing package part")
        relationships[identity] = resolved
    return relationships


def _sheets(package):
    root = package.xml("xl/workbook.xml")
    _root_type(root, "s", "workbook")
    relationships = _relationships(package, "xl/workbook.xml", "worksheet")
    container = _child(root, "sheets")
    sheets = []
    if container is not None:
        for item in _children(container, "sheet"):
            name = item.get("name", "")
            if not name or len(name) > 128 or any(existing["name"] == name for existing in sheets):
                raise ValueError("Invalid or duplicate workbook sheet name")
            relation = _attribute(item, "id")
            if relation not in relationships:
                raise ValueError("Worksheet relationship is missing")
            sheets.append({"name": name, "selector": name, "state": item.get("state", "visible"),
                           "part": relationships[relation]})
    return sheets


def _sheet(package, selector):
    sheets = _sheets(package)
    if not sheets:
        raise ValueError("Workbook contains no worksheets")
    chosen = next((item for item in sheets if item["name"] == selector), None) if selector else sheets[0]
    if chosen is None:
        raise ValueError("Worksheet not found; inspect the workbook to list sheet selectors")
    if chosen["part"] is None:
        raise ValueError("External worksheet relationships are not supported")
    return chosen


def _shared_strings(package, *, text_limit=MAX_CELL_TEXT):
    if "xl/sharedStrings.xml" not in package.names:
        return []
    strings, characters = [], 0
    for item in _items(package, "xl/sharedStrings.xml", "s", "sst", "sst", {"si"}):
        text, truncated = _text(item, text_limit)
        characters += len(text)
        if len(strings) >= MAX_SHARED_STRINGS or characters > MAX_SHARED_TEXT:
            raise ValueError("Workbook shared strings exceed the preview budget; export the needed sheet as CSV")
        strings.append((text, truncated))
    return strings


def _column(value):
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z]{1,3}", value):
        number = 0
        for letter in value.upper():
            number = number * 26 + ord(letter) - ord("A") + 1
        value = number - 1
    return _int(value, "column", 0, 16383)


def _column_name(number):
    result, number = "", number + 1
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _columns(columns, column_offset, column_limit):
    _int(column_offset, "column_offset", 0, 16383)
    _int(column_limit, "column_limit", 1, MAX_COLUMNS)
    if columns is not None:
        if not isinstance(columns, list) or not 1 <= len(columns) <= MAX_COLUMNS:
            raise ValueError("columns must contain 1 to 20 column letters or zero-based indices")
        result = [_column(value) for value in columns]
        if len(set(result)) != len(result):
            raise ValueError("columns must not contain duplicates")
        return result
    return list(range(column_offset, min(16384, column_offset + column_limit)))


def _cell_value(cell, strings, *, text_limit=MAX_CELL_TEXT):
    kind = cell.get("t", "n")
    formula = _child(cell, "f") is not None
    value_node = _child(cell, "v")
    raw = value_node.text if value_node is not None else None
    if kind == "inlineStr":
        value, truncated = _text(cell, text_limit)
    elif raw is None:
        value, truncated = None, False
    elif kind == "s":
        if not raw.isascii() or not raw.isdigit() or len(raw) > 10 or int(raw) >= len(strings):
            raise ValueError("Invalid workbook shared-string index")
        value, truncated = strings[int(raw)]
    elif kind == "b":
        if raw not in ("0", "1"):
            raise ValueError("Invalid workbook Boolean cell")
        value, truncated = raw == "1", False
    elif kind in {"str", "e", "d"}:
        value, truncated = _clip(raw, text_limit), len(raw) > text_limit
    elif kind in {"n", ""}:
        try:
            if len(raw) > 128:
                raise ValueError
            value = int(raw) if re.fullmatch(r"[+-]?[0-9]+", raw) else float(raw)
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError
        except ValueError:
            raise ValueError("Invalid or excessive workbook numeric cell") from None
        truncated = False
    else:
        raise ValueError("Unsupported workbook cell type")
    return value, truncated, formula, formula and raw is None


def _rows(package, sheet, strings, selected=None, *, text_limit=MAX_CELL_TEXT):
    previous = 0
    for row in _items(package, sheet["part"], "s", "worksheet", "sheetData", {"row"}):
        raw_number = row.get("r", str(previous + 1))
        if not raw_number.isascii() or not raw_number.isdigit() or len(raw_number) > 7:
            raise ValueError("Invalid worksheet row number")
        number = int(raw_number)
        if not previous < number <= 1048576:
            raise ValueError("Worksheet rows must have increasing valid row numbers")
        previous = number
        values, truncated, formulas, missing, last_column = {}, [], [], [], -1
        for cell in _children(row, "c"):
            reference = cell.get("r")
            if reference:
                match = _CELL.fullmatch(reference)
                if not match or int(match[2]) != number:
                    raise ValueError("Invalid worksheet cell coordinate")
                column = _column(match[1])
            else:
                column = last_column + 1
            if not last_column < column <= 16383:
                raise ValueError("Worksheet cells must have increasing valid column coordinates")
            last_column = column
            if selected is not None and column not in selected:
                continue
            value, clipped, formula, unavailable = _cell_value(cell, strings, text_limit=text_limit)
            values[column] = value
            if clipped:
                truncated.append(_column_name(column))
            if formula:
                formulas.append(_column_name(column))
            if unavailable:
                missing.append(_column_name(column))
        yield {"row_number": number, "cells": values, "truncated_columns": truncated,
               "formula_columns": formulas, "uncached_formula_columns": missing}


def _word_blocks(package):
    yield from _items(package, "word/document.xml", "w", "document", "body", {"p", "tbl"})


def _paragraph(element):
    text, truncated = _text(element)
    properties = _child(element, "pPr")
    style, level = "", None
    if properties is not None:
        node = _child(properties, "pStyle")
        if node is not None:
            style = _clip(_attribute(node, "val"), 128)
        node = _child(properties, "outlineLvl")
        if node is not None and _attribute(node, "val") in {str(i) for i in range(9)}:
            level = int(_attribute(node, "val")) + 1
    heading = re.fullmatch(r"Heading([1-9])", style, re.IGNORECASE)
    if level is None and heading:
        level = int(heading[1])
    return {"type": "heading" if level else "paragraph", "text": text,
            "style": style, "heading_level": level, "truncated": truncated}


def _table_rows(table, kind):
    for index, row in enumerate(_children(table, "tr")):
        values, clipped = [], []
        for column, cell in enumerate(_children(row, "tc")):
            text, truncated = _text(cell, MAX_CELL_TEXT)
            values.append(text)
            if truncated:
                clipped.append(column)
        yield index, values, clipped


def _table_preview(table, kind):
    rows, truncated = [], False
    for index, values, clipped in _table_rows(table, kind):
        if index >= 5:
            truncated = True
            break
        rows.append([value[:256] for value in values[:5]])
        truncated |= bool(clipped) or len(values) > 5 or any(len(value) > 256 for value in values)
    return {"rows": rows, "truncated": truncated, "preview_rows": 5, "preview_columns": 5}


def _slides(package):
    root = package.xml("ppt/presentation.xml")
    _root_type(root, "p", "presentation")
    relationships = _relationships(package, "ppt/presentation.xml", "slide")
    container = _child(root, "sldIdLst")
    slides = []
    if container is not None:
        for item in _children(container, "sldId"):
            relation = _attribute(item, "id")
            # sldId has both an unqualified numeric id and a qualified r:id.
            relation = next((value for key, value in item.attrib.items() if key.endswith("}id")), relation)
            if relation not in relationships:
                raise ValueError("Presentation slide relationship is missing")
            slides.append(relationships[relation])
    return slides


def _slide(package, part, index, *, for_search=False):
    if part is None:
        raise ValueError("External slide relationships are not supported")
    root = package.xml(part)
    _root_type(root, "p", "sld")
    paragraphs, tables, truncated = [], [], False
    table_descendants = set()
    for item in root.iter():
        if _tag(item) == "tbl":
            table_descendants.update(item.iter())
            preview = {"selector": f"slide:{index}/table:{len(tables)}", **_table_preview(item, "p")}
            if not _fits({"tables": tables}, "tables", preview):
                truncated = True
                break
            if len(tables) >= 10:
                truncated = True
                break
    text_budget = MAX_TEXT
    for item in root.iter():
        if _tag(item) == "p" and item not in table_descendants:
            text, clipped = _text(item, max(0, text_budget))
            if text:
                paragraphs.append(text)
                text_budget -= len(text)
            truncated |= clipped
            if len(paragraphs) >= 100:
                truncated = True
                break
    result = {"index": index, "slide_number": index + 1, "type": "slide",
              "paragraphs": paragraphs, "tables": [], "truncated": truncated}
    for table in tables:
        if not _fits(result, "tables", table):
            result["truncated"] = True
            break
    if for_search:
        text, text_truncated = _text(root, MAX_SEARCH_TEXT)
        return {"unit": result, "text": text, "text_truncated": text_truncated}
    return result


def _document_units(package, suffix):
    if suffix == ".docx":
        table_index = 0
        for index, element in enumerate(_word_blocks(package)):
            if _tag(element) == "p":
                yield {"index": index, **_paragraph(element)}
            else:
                yield {"index": index, "type": "table", "selector": f"table:{table_index}",
                       **_table_preview(element, "w")}
                table_index += 1
    else:
        for index, part in enumerate(_slides(package)):
            yield _slide(package, part, index)


def _document_search_units(package, suffix):
    if suffix == ".docx":
        table_index = 0
        for index, element in enumerate(_word_blocks(package)):
            if _tag(element) == "p":
                unit = {"index": index, **_paragraph(element)}
            else:
                unit = {"index": index, "type": "table", "selector": f"table:{table_index}",
                        **_table_preview(element, "w")}
                table_index += 1
            text, truncated = _text(element, MAX_SEARCH_TEXT)
            yield {"unit": unit, "text": text, "text_truncated": truncated}
    else:
        for index, part in enumerate(_slides(package)):
            yield _slide(package, part, index, for_search=True)


def _scope(suffix):
    return ("Selected worksheet stored rows; cached formula results only; dates remain raw serials/ISO strings"
            if suffix in _SHEETS else
            "Main document body top-level paragraphs/headings/tables; no headers, footers, comments, embedded objects or external links"
            if suffix == ".docx" else
            "Presentation slide text and tables in slide order; no notes, masters, images, embedded objects or external links")


def inspect(stream: BinaryIO, suffix: str, selector: str = "", offset: int = 0, limit: int = 50) -> dict:
    suffix = _suffix(suffix)
    _int(offset, "offset", 0, MAX_PREFIX_UNITS)
    _int(limit, "limit", 1, 100)
    with _Package(stream) as package:
        if suffix in _SHEETS:
            sheets = _sheets(package)
            result = {"format": suffix[1:], "scope": _scope(suffix), "sheet_count": len(sheets),
                      "offset": offset, "sheets": [], "formulas_evaluated": False,
                      "macros_executed": False, "external_links_followed": False}
            for index, sheet in enumerate(sheets[offset:offset + limit], offset):
                item = {"index": index, "name": sheet["name"], "selector": sheet["selector"],
                        "state": sheet["state"], "external": sheet["part"] is None}
                if not _fits(result, "sheets", item):
                    break
            result["next_offset"] = offset + len(result["sheets"]) if offset + len(result["sheets"]) < len(sheets) else None
            if selector:
                result["selected_sheet"] = _sheet(package, selector)["name"]
            return result
        if selector:
            raise ValueError("Document inspection does not take a selector; use a paged start/offset")
        result = _read_document(package, suffix, offset, min(limit, 20))
        result["inspection"] = "Document structure/text preview; table selectors allow narrower reads"
        return result


def _read_document(package, suffix, start, limit):
    result = {"format": suffix[1:], "scope": _scope(suffix), "start": start, "units": [],
              "offset_unit": "body_blocks" if suffix == ".docx" else "slides"}
    next_offset = None
    for index, unit in enumerate(_document_units(package, suffix)):
        if index > MAX_PREFIX_UNITS:
            raise ValueError("Office document prefix exceeds the unit scan budget")
        if index < start:
            continue
        if len(result["units"]) >= limit or not _fits(result, "units", unit):
            next_offset = index
            break
    result.update(count=len(result["units"]), next_offset=next_offset, complete=next_offset is None)
    if next_offset == start and not result["units"]:
        raise ValueError("One document unit exceeds the response budget; use a narrower table read")
    return result


def read_document(stream: BinaryIO, suffix: str, start: int = 0, limit: int = 5) -> dict:
    suffix = _suffix(suffix)
    if suffix in _SHEETS:
        raise ValueError("Use read_table for Excel worksheets")
    _int(start, "start", 0, MAX_PREFIX_UNITS)
    _int(limit, "limit", 1, 20)
    with _Package(stream) as package:
        return _read_document(package, suffix, start, limit)


def _selected_table(package, suffix, selector):
    if suffix == ".docx":
        match = re.fullmatch(r"table:([0-9]{1,6})", selector or "table:0")
        if not match:
            raise ValueError("DOCX table selector must be table:N (zero-based)")
        index = 0
        for element in _word_blocks(package):
            if _tag(element) == "tbl":
                if index == int(match[1]):
                    yield from _table_rows(element, "w")
                    return
                index += 1
    else:
        match = re.fullmatch(r"slide:([0-9]{1,6})/table:([0-9]{1,6})", selector or "slide:0/table:0")
        if not match:
            raise ValueError("PPTX table selector must be slide:N/table:M (zero-based)")
        slides = _slides(package)
        slide_index, table_index = int(match[1]), int(match[2])
        if slide_index < len(slides) and slides[slide_index] is not None:
            root = package.xml(slides[slide_index])
            _root_type(root, "p", "sld")
            tables = (element for element in root.iter() if _tag(element) == "tbl")
            for index, table in enumerate(tables):
                if index == table_index:
                    yield from _table_rows(table, "p")
                    return
    raise ValueError("Document table not found; inspect/read_document to obtain its selector")


def read_table(stream: BinaryIO, suffix: str, selector: str = "", columns=None,
               offset: int = 0, limit: int = 100, column_offset: int = 0, column_limit: int = 20) -> dict:
    suffix = _suffix(suffix)
    _int(offset, "offset", 0, MAX_PREFIX_UNITS)
    _int(limit, "limit", 1, 100)
    selected = _columns(columns, column_offset, column_limit)
    with _Package(stream) as package:
        result = {"format": suffix[1:], "scope": _scope(suffix), "selector": selector,
                  "offset": offset, "offset_unit": "stored_rows" if suffix in _SHEETS else "table_rows",
                  "columns": [_column_name(column) for column in selected], "rows": []}
        if suffix in _SHEETS:
            sheet = _sheet(package, selector)
            result["selector"] = sheet["name"]
            source = _rows(package, sheet, _shared_strings(package), set(selected))
        else:
            source = ({"row_number": index + 1, "cells": dict(enumerate(values)),
                       "truncated_columns": [_column_name(i) for i in clipped],
                       "formula_columns": [], "uncached_formula_columns": []}
                      for index, values, clipped in _selected_table(package, suffix, selector))
        next_offset = None
        for index, row in enumerate(source):
            if index > MAX_PREFIX_UNITS:
                raise ValueError("Office table prefix exceeds the row scan budget")
            if index < offset:
                continue
            rendered = {key: value for key, value in row.items() if key != "cells"}
            rendered["values"] = [row["cells"].get(column) for column in selected]
            if len(result["rows"]) >= limit or not _fits(result, "rows", rendered):
                next_offset = index
                break
        result.update(count=len(result["rows"]), next_offset=next_offset, complete=next_offset is None)
        if next_offset == offset and not result["rows"]:
            raise ValueError("One Office row exceeds the response budget; request fewer columns")
        return result


def search(stream: BinaryIO, suffix: str, query: str, selector: str = "", offset: int = 0,
           limit: int = 20, case_sensitive: bool = False) -> dict:
    suffix = _suffix(suffix)
    if not isinstance(query, str) or not 1 <= len(query) <= 256:
        raise ValueError("query must be 1 to 256 literal characters")
    if not isinstance(case_sensitive, bool):
        raise ValueError("case_sensitive must be Boolean")
    _int(offset, "offset", 0, MAX_PREFIX_UNITS)
    _int(limit, "limit", 1, 100)
    needle = query if case_sensitive else query.casefold()
    with _Package(stream) as package:
        result = {"format": suffix[1:], "scope": _scope(suffix), "selector": selector,
                  "offset": offset, "matches": [], "scanned_units": 0,
                  "scan_limit": MAX_SEARCH_UNITS, "search_mode": "literal",
                  "text_scope": "Up to 4,194,304 text characters per cell/body block/slide; output snippets are shorter",
                  "text_truncated": False,
                  "offset_unit": "stored_rows" if suffix in _SHEETS else "body_blocks" if suffix == ".docx" else "slides"}
        if suffix in _SHEETS:
            sheet = _sheet(package, selector)
            result["selector"] = sheet["name"]
            source = _rows(package, sheet, _shared_strings(package, text_limit=MAX_SEARCH_TEXT), text_limit=MAX_SEARCH_TEXT)
        else:
            if selector:
                raise ValueError("Document search selector must be empty; offset selects a body block/slide")
            source = _document_search_units(package, suffix)
        match_cache = {}
        next_offset = None
        for index, item in enumerate(source):
            if index > MAX_PREFIX_UNITS:
                raise ValueError("Office search prefix exceeds the scan budget")
            if index < offset:
                continue
            if result["scanned_units"] >= MAX_SEARCH_UNITS or len(result["matches"]) >= limit:
                next_offset = index
                break
            result["scanned_units"] += 1
            if suffix in _SHEETS:
                result["text_truncated"] |= bool(item["truncated_columns"])
                cells = []
                for column, value in item["cells"].items():
                    text = "" if value is None else str(value)
                    # Repeated references to one large shared string should
                    # not repeat a megabyte-scale casefold/search per cell.
                    if text not in match_cache:
                        match_cache[text] = needle in (text if case_sensitive else text.casefold())
                    if match_cache[text]:
                        cells.append({"column": _column_name(column),
                                      "value": _clip(value, 256) if isinstance(value, str) else value,
                                      "preview_truncated": isinstance(value, str) and len(value) > 256})
                        if len(cells) == MAX_COLUMNS:
                            break
                if not cells:
                    continue
                match = {"offset": index, "row_number": item["row_number"], "cells": cells,
                         "truncated_columns": item["truncated_columns"][:MAX_COLUMNS]}
            else:
                result["text_truncated"] |= item["text_truncated"]
                text = item["text"]
                if needle not in (text if case_sensitive else text.casefold()):
                    continue
                match = {"offset": index, "unit": item["unit"]}
            if not _fits(result, "matches", match):
                result["scanned_units"] -= 1
                next_offset = index
                break
        result.update(count=len(result["matches"]), next_offset=next_offset,
                      complete=next_offset is None and not result["text_truncated"])
        if next_offset == offset and not result["matches"]:
            raise ValueError("One matching Office unit exceeds the response budget; narrow the query or table columns")
        return result
