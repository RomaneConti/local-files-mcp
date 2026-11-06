"""List or preview archive members without extracting anything to disk."""
from contextlib import contextmanager
import gzip
import io
from pathlib import PurePosixPath
import tarfile

from .common import BudgetIO, UNSAFE_SUFFIXES, file_size, integer, page, response, safe_member_name, safe_zip
from ..text import read_window

MAX_TAR_SCAN = 64*1024**2
MAX_MEMBERS = 4096


class _TarInfo(tarfile.TarInfo):
    """Bound physical extension headers before tarfile recursively processes them."""
    def _proc_member(self, source):
        if self.size < 0:
            raise ValueError('Negative TAR member size')
        if self.type == tarfile.GNUTYPE_SPARSE:
            raise ValueError('Sparse TAR members are unsupported')
        if self.type in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE,
                         tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK):
            depth = getattr(source, '_extension_depth', 0)
            total = getattr(source, '_extension_bytes', 0) + self.size
            if depth >= 32 or self.size > 65536 or total > 256*1024:
                raise ValueError('TAR extension headers exceed nesting/size budget')
            source._extension_bytes = total
            source._extension_depth = depth + 1
            try:
                position = source.fileobj.tell()
                metadata = source.fileobj.read(self.size)
                source.fileobj.seek(position)
                if b'GNU.sparse' in metadata:
                    raise ValueError('Sparse TAR metadata is unsupported')
                return super()._proc_member(source)
            finally:
                source._extension_depth = depth
        return super()._proc_member(source)


@contextmanager
def _tar(stream, suffix):
    if file_size(stream) > 512*1024**2:
        raise ValueError('Archive exceeds 512 MiB input limit')
    stream.seek(0)
    reader = BudgetIO(stream, max_read=1024**2)
    if suffix in ('.tar.gz', '.tgz'):
        with gzip.GzipFile(fileobj=reader, mode='rb') as compressed:
            chunks, total = [], 0
            while chunk := compressed.read(min(1024**2, MAX_TAR_SCAN + 1 - total)):
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_TAR_SCAN:
                    raise ValueError('tar.gz exceeds 64 MiB decompressed scan limit')
            reader = BudgetIO(io.BytesIO(b''.join(chunks)), max_read=1024**2)
    try:
        with tarfile.open(fileobj=reader, mode='r:', tarinfo=_TarInfo) as archive:
            yield archive
    except (tarfile.TarError, EOFError, OverflowError) as exc:
        raise ValueError('Malformed or unsupported TAR archive') from exc


def _safe_tar(item):
    return safe_member_name(item.name) and (item.isfile() or item.isdir()) and not item.issparse()


def inspect(stream, suffix, selector='', offset=0, limit=50):
    page(offset, limit)
    entries = []
    if suffix == '.zip':
        with safe_zip(stream) as archive:
            members = archive.infolist()
            for item in members[offset:offset+limit]:
                entries.append({'name': item.filename, 'size_bytes': item.file_size,
                    'directory': item.is_dir(), 'safe': True})
            next_offset = offset+limit if offset+limit < len(members) else None
            total = len(members)
    else:
        with _tar(stream, suffix) as archive:
            total = 0
            next_offset = None
            for index, item in enumerate(archive):
                if index >= MAX_MEMBERS:
                    raise ValueError('TAR exceeds 4096-member scan limit')
                total += 1
                if index >= offset+limit:
                    next_offset = offset+limit
                    break
                if index >= offset:
                    entries.append({'name': item.name[:1024], 'size_bytes': item.size,
                        'directory': item.isdir(), 'safe': _safe_tar(item)})
            if next_offset is not None:
                total = None
    return response({'format': suffix.lstrip('.'), 'members': entries, 'member_count': total,
        'offset': offset, 'next_offset': next_offset, 'reader': 'read_archive_member'})


def read(stream, suffix, member, byte_offset=0, max_bytes=16384):
    integer(byte_offset, 'byte_offset', 0, 8*1024**2)
    integer(max_bytes, 'max_bytes', 16, 32768)
    if not safe_member_name(member):
        raise ValueError('Unsafe archive member name')
    name = member[:-3] if member.lower().endswith('.gz') else member
    if PurePosixPath(name).suffix.lower() in UNSAFE_SUFFIXES:
        raise ValueError('Unsafe deserialization formats are not parsed')
    if suffix == '.zip':
        with safe_zip(stream) as archive:
            try:
                item = archive.getinfo(member)
            except KeyError as exc:
                raise ValueError('Archive member not found') from exc
            if item.is_dir():
                raise ValueError('Member is a directory')
            with archive.open(item, 'r') as source:
                return _window(source, member, byte_offset, max_bytes)
    with _tar(stream, suffix) as archive:
        for index, item in enumerate(archive):
            if index >= MAX_MEMBERS:
                raise ValueError('TAR exceeds 4096-member scan limit')
            if item.name != member:
                continue
            if not _safe_tar(item) or not item.isfile() or item.size > MAX_TAR_SCAN:
                raise ValueError('Unsafe, linked, sparse, oversized, or non-file TAR member')
            with archive.extractfile(item) as source:
                return _window(source, member, byte_offset, max_bytes)
    raise ValueError('Archive member not found within scan budget')


def _window(source, member, byte_offset, max_bytes):
    # Zip seek may inflate the skipped prefix. Explicitly bound that prefix.
    remaining = byte_offset
    while remaining:
        chunk = source.read(min(65536, remaining))
        if not chunk:
            raise ValueError('byte_offset exceeds member size')
        remaining -= len(chunk)
    data = source.read(max_bytes + 4)
    preview = read_window(io.BytesIO(data), max_bytes=max_bytes)
    preview['member'] = member
    preview['byte_offset'] = byte_offset
    preview['next_byte_offset'] = byte_offset + preview['next_byte_offset'] if preview['next_byte_offset'] is not None else None
    return response(preview)
