"""SQLite SELECTs over an approved handle using a genuinely read-only VFS.

No native VFS file open is inherited. Journals, temp files and attachments cannot
be opened. WAL databases are deliberately refused (their sidecars are unapproved).
"""
from contextlib import contextmanager
import re
import time
import uuid

import apsw

from .common import file_size, page, integer, response, safe_value

MAX_READ_BYTES = 64 * 1024**2
MAX_SECONDS = 3
SAFE_FUNCTIONS = {'abs', 'avg', 'coalesce', 'count', 'glob', 'ifnull', 'instr',
                  'length', 'like', 'lower', 'max', 'min', 'nullif', 'round',
                  'substr', 'substring', 'sum', 'total', 'trim', 'typeof', 'upper'}


class _File:
    def __init__(self, stream):
        self.stream = stream
        self.size = file_size(stream)
        self.read_bytes = 0
        self.deadline = time.monotonic() + MAX_SECONDS

    def xRead(self, amount, offset):
        self.read_bytes += amount
        if amount < 0 or offset < 0 or amount > 1024**2 or self.read_bytes > MAX_READ_BYTES or time.monotonic() > self.deadline:
            raise apsw.IOError('SQLite read budget exceeded')
        self.stream.seek(offset)
        return self.stream.read(amount)

    def xFileSize(self): return self.size
    def xClose(self): pass  # PathPolicy owns this handle.
    def xLock(self, level): pass
    def xUnlock(self, level): pass
    def xCheckReservedLock(self): return False
    def xFileControl(self, op, pointer): return False
    def xSectorSize(self): return 4096
    def xDeviceCharacteristics(self): return apsw.SQLITE_IOCAP_IMMUTABLE
    def xWrite(self, data, offset): raise apsw.ReadOnlyError('Read only')
    def xTruncate(self, size): raise apsw.ReadOnlyError('Read only')
    def xSync(self, flags): raise apsw.ReadOnlyError('Read only')


class _VFS(apsw.VFS):
    def __init__(self, stream):
        self.name = 'approved-' + uuid.uuid4().hex
        self.file = _File(stream)
        super().__init__(self.name, '', iVersion=1)

    def xFullPathname(self, name): return 'approved'
    def xAccess(self, name, flags): return False
    def xDelete(self, name, syncdir): raise apsw.ReadOnlyError('Read only')
    def xDlOpen(self, name): return 0

    def xOpen(self, name, flags):
        actual = name.filename() if isinstance(name, apsw.URIFilename) else name
        if (actual != 'approved' or not flags[0] & apsw.SQLITE_OPEN_MAIN_DB
                or not flags[0] & apsw.SQLITE_OPEN_READONLY
                or flags[0] & (apsw.SQLITE_OPEN_READWRITE | apsw.SQLITE_OPEN_CREATE | apsw.SQLITE_OPEN_DELETEONCLOSE)):
            raise apsw.ReadOnlyError('Only the approved main database is readable')
        flags[1] = apsw.SQLITE_OPEN_READONLY
        return self.file


def _authorize(action, first, second, db, trigger, tables=()):
    if action in (apsw.SQLITE_SELECT, apsw.SQLITE_RECURSIVE):
        return apsw.SQLITE_OK
    if (action == apsw.SQLITE_READ and not trigger
            and (first in tables or first in ('sqlite_master', 'sqlite_schema'))):
        # SQLite reports no database name for its count(*) table-read event.
        # Permit that one form only, still restricted to known ordinary tables.
        if db == 'main' or (db is None and second == ''):
            return apsw.SQLITE_OK
    if action == apsw.SQLITE_FUNCTION and (second or '').lower() in SAFE_FUNCTIONS:
        return apsw.SQLITE_OK
    return apsw.SQLITE_DENY


@contextmanager
def _connection(stream):
    stream.seek(0)
    header = stream.read(100)
    if len(header) < 100 or not header.startswith(b'SQLite format 3\x00'):
        raise ValueError('Not a valid SQLite database')
    if header[18:20] != b'\x01\x01':
        raise ValueError('WAL databases are unsupported; use a closed/checkpointed standalone SQLite copy')
    vfs = _VFS(stream)
    connection = None
    try:
        connection = apsw.Connection('file:approved?mode=ro&immutable=1',
            flags=apsw.SQLITE_OPEN_READONLY | apsw.SQLITE_OPEN_URI, vfs=vfs.name, statementcachesize=0)
        if not connection.readonly('main'):
            raise ValueError('SQLite read-only mode was not established')
        connection.enable_load_extension(False)
        connection.config(apsw.SQLITE_DBCONFIG_TRUSTED_SCHEMA, 0)
        connection.config(apsw.SQLITE_DBCONFIG_DEFENSIVE, 1)
        connection.config(apsw.SQLITE_DBCONFIG_DQS_DML, 0)
        connection.config(apsw.SQLITE_DBCONFIG_DQS_DDL, 0)
        for category, maximum in [(apsw.SQLITE_LIMIT_LENGTH, 1024**2),
            (apsw.SQLITE_LIMIT_SQL_LENGTH, 16384), (apsw.SQLITE_LIMIT_COLUMN, 256),
            (apsw.SQLITE_LIMIT_EXPR_DEPTH, 40), (apsw.SQLITE_LIMIT_COMPOUND_SELECT, 10),
            (apsw.SQLITE_LIMIT_VARIABLE_NUMBER, 100), (apsw.SQLITE_LIMIT_VDBE_OP, 100000)]:
            connection.limit(category, maximum)
        connection.set_progress_handler(lambda: time.monotonic() > vfs.file.deadline, 1000)
        # Only this fixed internal PRAGMA runs before the user authorizer.
        # SQLite identifies virtual/shadow tables itself, avoiding SQL-text
        # prefix checks that whitespace or comments can bypass.
        tables = _tables(connection)
        connection.set_authorizer(lambda *args: _authorize(*args, tables=tables))
        yield connection, tables
    except apsw.Error as exc:
        raise ValueError('SQLite rejected the query/file or exceeded its 3-second/64-MiB read budget') from exc
    finally:
        if connection is not None:
            connection.close()
        vfs.unregister()


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _tables(connection):
    entries = []
    for row in connection.execute('PRAGMA main.table_list'):
        entries.append(row)
        if len(entries) > 1024:
            raise ValueError('SQLite schema exceeds the 1024-object inspection limit')
    allowed = {name for db, name, kind, *_ in entries
               if db == 'main' and kind == 'table' and not name.startswith('sqlite_')}
    if len(allowed) > 256:
        raise ValueError('SQLite schema exceeds 256 tables')
    rows = connection.execute("SELECT name, sql FROM sqlite_schema WHERE type='table' ORDER BY name LIMIT 1025")
    return {name: sql for name, sql in rows if name in allowed}


def _execute(connection, sql, bindings=()):
    """Capture prepared columns even when an empty result exhausts immediately."""
    description = []
    cursor = connection.cursor()
    def prepared(current, statement, parameters):
        description.extend(current.get_description())
        return True
    cursor.set_exec_trace(prepared)
    cursor.execute(sql, bindings)
    return cursor, description


def inspect(stream, suffix='', selector='', offset=0, limit=50):
    page(offset, limit)
    with _connection(stream) as (connection, tables):
        if selector:
            if selector not in tables:
                raise ValueError('Unknown or unsupported table; inspect_file lists readable tables')
            _, description = _execute(connection, f'SELECT * FROM {_quote(selector)} LIMIT 0')
            columns = [{'name': name, 'dtype': dtype} for name, dtype in description]
            # COUNT is subject to the same I/O and VM budgets, never an unbounded scan.
            count = next(connection.execute(f'SELECT count(*) FROM {_quote(selector)}'))[0]
            return response({'format': 'sqlite', 'selector': selector, 'read_only': True,
                'row_count': count, 'schema': safe_value(tables[selector]), 'columns': columns[offset:offset+limit],
                'next_offset': offset+limit if offset+limit < len(columns) else None})
        names = list(tables)
        return response({'format': 'sqlite', 'read_only': True, 'tables': names[offset:offset+limit],
            'next_offset': offset+limit if offset+limit < len(names) else None,
            'note': 'Only standalone rollback-journal databases and ordinary tables; no sidecars or virtual tables.'})


def read_table(stream, suffix='', selector='', columns=None, offset=0, limit=100,
               column_offset=0, column_limit=20, query=None):
    page(offset, limit)
    integer(column_offset, 'column_offset', 0, 255)
    integer(column_limit, 'column_limit', 1, 50)
    if columns is not None and (not isinstance(columns, list) or not columns or len(columns) > 50
            or any(not isinstance(c, str) or len(c) > 256 for c in columns) or len(set(columns)) != len(columns)):
        raise ValueError('columns must contain 1..50 column names')
    if limit * column_limit > 4096:
        raise ValueError('Preview exceeds the 4096-cell limit; request fewer rows or columns')
    with _connection(stream) as (connection, tables):
        if query is not None:
            if not isinstance(query, str) or len(query) > 8192 or not re.match(r'\s*SELECT\b', query, re.I) or ';' in query or '\x00' in query:
                raise ValueError('query must be one SELECT without semicolons, at most 8192 characters')
            if columns is not None:
                raise ValueError('Choose columns in the SELECT query itself')
            source = '(' + query + ')'
            projection = '*'
        else:
            if selector not in tables:
                raise ValueError('selector must name an ordinary table from inspect_file')
            source = _quote(selector)
            projection = ', '.join(_quote(c) for c in columns) if columns else '*'
        cursor, description = _execute(connection, f'SELECT {projection} FROM {source} LIMIT ? OFFSET ?', (limit+1, offset))
        names = [col[0] for col in description]
        rows = []
        for row in cursor:
            rows.append([safe_value(v) for v in row[column_offset:column_offset+column_limit]])
        return response({'format': 'sqlite', 'read_only': connection.readonly('main'),
            'columns': names[column_offset:column_offset+column_limit], 'offset': offset,
            'rows': rows[:limit], 'next_offset': offset+limit if len(rows) > limit else None,
            'next_column_offset': column_offset+column_limit if column_offset+column_limit < len(names) else None,
            'ordering': 'For stable pagination, supply SELECT ... ORDER BY an explicit key.'})
