"""Small shared limits and archive preflight; no filesystem opens."""
from contextlib import contextmanager
import base64
import datetime
import json
import math
import re
import stat
import struct
import time
import zipfile
import zlib

UNSAFE_SUFFIXES = {'.pkl', '.pickle', '.joblib', '.pt', '.pth'}


class BudgetIO:
    """Borrowed handle with a cumulative read budget; never owns/reopens a path."""
    def __init__(self, stream, budget=64*1024**2, max_read=32*1024**2, seconds=5):
        self.stream, self.remaining, self.max_read = stream, budget, max_read
        self.deadline = time.monotonic() + seconds

    def read(self, size=-1):
        if size < 0:
            size = file_size(self.stream) - self.stream.tell()
        if size > self.max_read or size > self.remaining or time.monotonic() > self.deadline:
            raise ValueError('Parser input/time budget exceeded')
        value = self.stream.read(size)
        self.remaining -= len(value)
        return value

    def readinto(self, buffer):
        value = self.read(len(buffer))
        buffer[:len(value)] = value
        return len(value)

    def readline(self, size=-1):
        cap = min(self.remaining, self.max_read, size if size >= 0 else self.max_read)
        if cap <= 0 or time.monotonic() > self.deadline:
            raise ValueError('Parser input/time budget exceeded')
        value = self.stream.readline(cap)
        self.remaining -= len(value)
        if len(value) == cap and not value.endswith(b'\n'):
            raise ValueError('Parser line exceeds limit')
        return value

    def seek(self, offset, whence=0): return self.stream.seek(offset, whence)
    def tell(self): return self.stream.tell()
    def seekable(self): return True
    def readable(self): return True
    def writable(self): return False
    @property
    def closed(self): return False
    def close(self): pass


def integer(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f'{name} must be an integer between {minimum} and {maximum}')
    return value


def page(offset, limit, max_limit=100):
    integer(offset, 'offset', 0, 2**63 - 1)
    integer(limit, 'limit', 1, max_limit)


def file_size(stream):
    position = stream.tell()
    stream.seek(0, 2)
    size = stream.tell()
    stream.seek(position)
    return size


def safe_value(value, depth=0):
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return value if len(value) <= 2048 else {'preview': value[:2048], 'length': len(value), 'truncated': True}
    if isinstance(value, bytes):
        return {'type': 'bytes', 'length': len(value), 'base64_preview': base64.b64encode(value[:256]).decode(), 'truncated': len(value) > 256}
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        if depth >= 3:
            return {'type': 'array', 'length': len(value)}
        return [safe_value(v, depth + 1) for v in value[:20]]
    if isinstance(value, dict):
        if depth >= 3:
            return {'type': 'object', 'length': len(value)}
        return {str(k)[:128]: safe_value(v, depth + 1) for k, v in list(value.items())[:20]}
    return safe_value(str(value))


def response(value):
    size = 0
    for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(value):
        size += len(chunk.encode('utf-8'))
        if size > 64 * 1024:
            raise ValueError('Response exceeds 64 KiB; request a smaller range/limit')
    return value


def safe_member_name(name):
    if not name or len(name) > 1024 or '\\' in name or '\x00' in name or ':' in name or name.startswith('/'):
        return False
    return all(part not in ('', '.', '..') for part in name.rstrip('/').split('/'))


@contextmanager
def safe_zip(stream, *, max_file=512*1024**2, max_members=4096,
             max_total=128*1024**2, max_member=32*1024**2, max_ratio=200):
    stream = BudgetIO(stream)
    size = file_size(stream)
    if size > max_file or size < 22:
        raise ValueError('ZIP file size exceeds limit or header is incomplete')
    # Bound the central directory before ZipFile allocates an entry per member.
    stream.seek(max(0, size - 65557))
    tail = stream.read(65557)
    index = tail.rfind(b'PK\x05\x06')
    if index < 0 or len(tail) - index < 22:
        raise ValueError('Invalid ZIP directory')
    _, disk, cd_disk, disk_count, count, cd_size, cd_offset, comment = struct.unpack('<4s4H2IH', tail[index:index+22])
    if disk or cd_disk or disk_count != count or count > max_members or count == 65535 or cd_size > 4*1024**2 or cd_offset == 0xffffffff:
        raise ValueError('ZIP directory exceeds limits; split/ZIP64 archives unsupported')
    if (index >= 20 and tail[index-20:index-16] == b'PK\x06\x07') or index + 22 + comment != len(tail) or cd_offset + cd_size != size - (len(tail) - index):
        raise ValueError('Invalid ZIP directory range')
    stream.seek(cd_offset)
    directory = stream.read(cd_size)
    cursor = 0
    actual = 0
    while cursor < len(directory):
        if directory[cursor:cursor+4] != b'PK\x01\x02' or cursor + 46 > len(directory):
            raise ValueError('Invalid ZIP directory entry')
        name_len, extra_len, comment_len = struct.unpack_from('<3H', directory, cursor+28)
        cursor += 46 + name_len + extra_len + comment_len
        actual += 1
        if actual > max_members:
            raise ValueError('ZIP has too many members')
    if cursor != len(directory) or actual != count:
        raise ValueError('ZIP directory count mismatch')
    stream.seek(0)
    try:
        with zipfile.ZipFile(stream, 'r') as archive:
            seen = set()
            total = 0
            for item in archive.infolist():
                mode = item.external_attr >> 16
                if not safe_member_name(item.orig_filename) or not safe_member_name(item.filename) or item.filename in seen or stat.S_ISLNK(mode):
                    raise ValueError('Unsafe, linked, or duplicate ZIP member')
                seen.add(item.filename)
                total += item.file_size
                if item.flag_bits & 1 or item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise ValueError('Encrypted or unsupported ZIP member')
                if item.file_size > max_member or total > max_total or item.file_size > max(1024*1024, item.compress_size * max_ratio):
                    raise ValueError('ZIP expansion/member size exceeds limit')
            yield archive
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, zlib.error) as exc:
        raise ValueError('Malformed or unsupported ZIP archive') from exc


def read_member(archive, name, max_bytes=32*1024**2):
    if not safe_member_name(name):
        raise ValueError('Unsafe ZIP member name')
    try:
        item = archive.getinfo(name)
    except KeyError as exc:
        raise ValueError('ZIP member not found') from exc
    if item.file_size > max_bytes:
        raise ValueError('ZIP member exceeds read limit')
    with archive.open(item, 'r') as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes or len(data) != item.file_size:
        raise ValueError('ZIP expanded size exceeds limit or mismatches metadata')
    return data
