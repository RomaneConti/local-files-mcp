"""Small explicit format routing; ordinary text stays with the text tools."""
from pathlib import PurePosixPath
from .common import UNSAFE_SUFFIXES

OFFICE = {'.xlsx', '.xlsm', '.docx', '.pptx'}
COLUMNAR = {'.parquet', '.feather'}
SCIENTIFIC = {'.h5', '.hdf5', '.mat'}
DATABASE = {'.sqlite', '.sqlite3', '.db'}
IMAGES = {'.png', '.jpg', '.jpeg', '.webp'}
ARCHIVES = {'.zip', '.tar', '.tar.gz', '.tgz'}
STRUCTURED = OFFICE | COLUMNAR | SCIENTIFIC | DATABASE | IMAGES | ARCHIVES | {'.pdf'}


def suffix(path):
    return '.tar.gz' if path.lower().endswith('.tar.gz') else PurePosixPath(path).suffix.lower()


def refuse_unsafe(path):
    name = path[:-3] if path.lower().endswith('.gz') else path
    if suffix(name) in UNSAFE_SUFFIXES:
        raise ValueError('Content parsing of pickle/joblib/PyTorch serialization is disabled; no code deserialization is permitted')


def recommendation(path):
    kind = suffix(path)
    try:
        refuse_unsafe(path)
    except ValueError:
        return {'reader': None, 'content_parsing': 'refused: unsafe deserialization format'}
    if kind in STRUCTURED:
        readers = []
        if kind in COLUMNAR | DATABASE | {'.xlsx', '.xlsm', '.docx', '.pptx'}:
            readers.append('read_table')
        if kind in {'.pdf', '.docx', '.pptx'}:
            readers.append('read_document')
        if kind in OFFICE | {'.pdf'}:
            readers.append('search_file')
        if kind in SCIENTIFIC:
            readers.append('read_array')
        if kind in IMAGES:
            readers.append('read_image')
        if kind in ARCHIVES:
            readers.append('read_archive_member')
        return {'reader': 'inspect_file', 'readers': readers}
    return {'reader': 'inspect_array' if kind in {'.npy', '.npz'} else
            'read_json' if kind == '.json' or path.lower().endswith('.json.gz') else 'read_text'}


def module(kind):
    if kind in OFFICE:
        from . import office
        return office
    if kind in COLUMNAR:
        from . import columnar
        return columnar
    if kind in SCIENTIFIC:
        from . import scientific
        return scientific
    if kind in DATABASE:
        from . import sqlite
        return sqlite
    if kind in IMAGES:
        from . import images
        return images
    if kind in ARCHIVES:
        from . import archive
        return archive
    if kind == '.pdf':
        from . import pdf
        return pdf
    raise ValueError('No structured reader for this format; use file_info for the recommended text/array tool')
