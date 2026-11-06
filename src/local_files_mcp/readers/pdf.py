"""Page-at-a-time PDF text, using pypdf's context-local decompression limits."""
from contextlib import contextmanager
from itertools import islice
import time
from pypdf import PdfReader, apply_configuration
from pypdf.errors import PyPdfError
from .common import BudgetIO, file_size, integer, page, response, safe_value

MAX_FILE_BYTES = 64*1024**2
MAX_STREAM_BYTES = 1024**2
MAX_TEXT = 131072
MAX_PAGES = 10000
MAX_OPERATORS = 50000


@contextmanager
def _reader(stream):
    if file_size(stream) > MAX_FILE_BYTES:
        raise ValueError('PDF exceeds 64 MiB file limit')
    stream.seek(0)
    try:
        with apply_configuration(maximum_declared_stream_length=MAX_STREAM_BYTES,
            array_based_stream_maximum_output_length=MAX_STREAM_BYTES,
            zlib_maximum_output_length=MAX_STREAM_BYTES, zlib_maximum_recovery_input_length=65536,
            lzw_maximum_output_length=MAX_STREAM_BYTES, run_length_maximum_output_length=MAX_STREAM_BYTES,
            jbig2_maximum_output_length=MAX_STREAM_BYTES, image_maximum_buffer_size=MAX_STREAM_BYTES,
            flate_maximum_columns=16384, flate_maximum_row_length=65536,
            page_tree_maximum_entries=MAX_PAGES, page_tree_maximum_depth=30,
            xform_maximum_invocations_per_extraction=32, jbig2dec_binary=None, disable_legacy_handling=True):
            reader = PdfReader(BudgetIO(stream, budget=128*1024**2, max_read=MAX_STREAM_BYTES+65536), strict=True)
            try:
                if reader.is_encrypted:
                    raise ValueError('Encrypted PDFs are unsupported')
                if len(reader.pages) > MAX_PAGES:
                    raise ValueError('PDF exceeds 10000-page limit')
                yield reader
            finally:
                # BudgetIO borrows the validated caller handle; close clears
                # pypdf caches without closing or reopening that handle.
                reader.close()
    except (PyPdfError, RecursionError, KeyError, TypeError, OverflowError) as exc:
        raise ValueError('Malformed, encrypted, or excessively complex PDF') from exc


def _text(reader, index):
    chars = 0
    operators = 0
    deadline = time.monotonic() + 3
    def visit(text, *_):
        nonlocal chars
        chars += len(text)
        if chars > MAX_TEXT or time.monotonic() > deadline:
            raise ValueError('PDF page exceeds text/processing budget')
    def before_operand(*_):
        nonlocal operators
        operators += 1
        if operators > MAX_OPERATORS or time.monotonic() > deadline:
            raise ValueError('PDF page exceeds operator/processing budget')
    text = reader.pages[index].extract_text(visitor_text=visit, visitor_operand_before=before_operand)
    if len(text) > MAX_TEXT:
        raise ValueError('PDF page exceeds text limit')
    return text


def inspect(stream, suffix='', selector='', offset=0, limit=50):
    page(offset, limit)
    if selector:
        raise ValueError('PDF inspection does not accept a selector')
    with _reader(stream) as reader:
        metadata = reader.metadata or {}
        keys = list(islice(metadata.keys(), 33))
        selected = keys[offset:min(offset+limit, 32)]
        return response({'format': 'pdf', 'page_count': len(reader.pages),
            'metadata': {str(k)[:128]: safe_value(metadata[k]) for k in selected},
            'next_offset': offset + len(selected) if offset + len(selected) < min(len(keys), 32) else None,
            'metadata_truncated': len(keys) > 32, 'reader': 'read_document', 'search': 'search_file',
            'unit': 'zero-based pages', 'note': 'Text layer only; scanned pages need OCR, which is not performed.'})


def read_document(stream, suffix='', start=0, limit=5, text_offset=0, max_chars=8192):
    page(start, limit, 5)
    integer(text_offset, 'text_offset', 0, MAX_TEXT)
    integer(max_chars, 'max_chars', 128, 16384)
    with _reader(stream) as reader:
        result = []
        for index in range(start, min(start+limit, len(reader.pages))):
            text = _text(reader, index)
            result.append({'page': index, 'text': text[text_offset:text_offset+max_chars],
                'text_length': len(text), 'next_text_offset': text_offset+max_chars if text_offset+max_chars < len(text) else None})
        return response({'format': 'pdf', 'unit': 'zero-based pages', 'pages': result,
            'next_offset': start+len(result) if start+len(result) < len(reader.pages) else None})


def search(stream, suffix, query, selector='', offset=0, limit=20, case_sensitive=False):
    page(offset, limit, 50)
    if not isinstance(query, str) or not query or len(query) > 256:
        raise ValueError('query must contain 1..256 characters')
    if not isinstance(case_sensitive, bool):
        raise ValueError('case_sensitive must be Boolean')
    if selector:
        raise ValueError('PDF search does not accept a selector; use a page offset')
    needle = query if case_sensitive else query.casefold()
    with _reader(stream) as reader:
        matches = []
        end = min(offset+10, len(reader.pages))
        scanned = offset
        for index in range(offset, end):
            text = _text(reader, index)
            haystack = text if case_sensitive else text.casefold()
            position = haystack.find(needle)
            if position >= 0:
                if not case_sensitive:
                    # Casefold can expand characters (e.g. sharp-s -> ss).
                    # Map back to the original text instead of returning a
                    # lowercased excerpt or an incorrectly shifted snippet.
                    folded = 0
                    for original_index, character in enumerate(text):
                        folded += len(character.casefold())
                        if folded > position:
                            position = original_index
                            break
                matches.append({'page': index, 'text': text[max(0, position-100):position+len(query)+200]})
            scanned = index+1
            if len(matches) >= limit:
                break
        complete = scanned >= len(reader.pages)
        return response({'format': 'pdf', 'matches': matches, 'scanned_pages': scanned-offset,
            'next_offset': None if complete else scanned, 'complete': complete,
            'scope': 'At most 10 pages per call; one excerpt per matching page; text layer only.'})
