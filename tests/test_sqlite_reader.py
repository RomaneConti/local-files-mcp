"""Read-only SQLite integration and denial tests over approved binary handles."""
import hashlib
import io
import json
import sqlite3
import time

import apsw
import pytest

from local_files_mcp.readers import sqlite as reader


def database(*statements):
    with sqlite3.connect(':memory:') as connection:
        for statement in statements:
            connection.execute(statement)
        return io.BytesIO(connection.serialize())


@pytest.fixture
def source():
    with sqlite3.connect(':memory:') as connection:
        connection.execute('CREATE TABLE entries (id INTEGER PRIMARY KEY, label TEXT, value REAL, payload BLOB)')
        connection.executemany('INSERT INTO entries VALUES (?, ?, ?, ?)',
                               [(i, f'row {i}', i / 2, bytes([i])) for i in range(12)])
        connection.execute('CREATE TABLE empty (id INTEGER, label TEXT)')
        return io.BytesIO(connection.serialize())


def test_inspection_count_and_empty_schema(source):
    assert reader.inspect(source)['tables'] == ['empty', 'entries']
    info = reader.inspect(source, selector='entries', offset=1, limit=2)
    assert info['read_only'] is True
    assert info['row_count'] == 12
    assert [column['name'] for column in info['columns']] == ['label', 'value']
    assert info['next_offset'] == 3
    empty = reader.inspect(source, selector='empty')
    assert empty['row_count'] == 0
    assert [column['name'] for column in empty['columns']] == ['id', 'label']


def test_query_and_table_pagination(source):
    result = reader.read_table(source, selector='entries', columns=['label', 'id'], offset=3, limit=4)
    assert result['columns'] == ['label', 'id']
    assert result['rows'] == [[f'row {i}', i] for i in range(3, 7)]
    assert result['next_offset'] == 7
    last = reader.read_table(source, query='SELECT id, label FROM entries ORDER BY id DESC', offset=10, limit=4)
    assert last['rows'] == [[1, 'row 1'], [0, 'row 0']]
    assert last['next_offset'] is None
    empty = reader.read_table(source, query='SELECT id FROM entries WHERE 0')
    assert empty['columns'] == ['id']
    assert empty['rows'] == []
    assert not source.closed


def test_column_window_and_allowed_functions(source):
    window = reader.read_table(source, selector='entries', column_offset=1, column_limit=2, limit=1)
    assert window['columns'] == ['label', 'value']
    assert window['next_column_offset'] == 3
    result = reader.read_table(source, query='SELECT count(*), max(id), sum(id), upper(min(label)) FROM entries')
    assert result['rows'] == [[12, 11, 66, 'ROW 0']]
    assert reader.read_table(source, query='SELECT\n1 AS value')['rows'] == [[1]]


@pytest.mark.parametrize('query', [
    'PRAGMA table_info(entries)', "ATTACH 'outside.db' AS outside", 'DETACH main',
    'CREATE TABLE stolen(x)', 'DROP TABLE entries', 'ALTER TABLE entries ADD COLUMN x',
    'INSERT INTO entries(id) VALUES (99)', 'UPDATE entries SET label=1', 'DELETE FROM entries',
    'VACUUM', "VACUUM INTO 'copy.db'", 'REINDEX', 'ANALYZE', 'BEGIN',
    'SELECT 1; DELETE FROM entries', 'SELECT 1\x00',
])
def test_public_query_rejects_non_select_and_multiple_statements(source, query):
    before = source.getvalue()
    with pytest.raises(ValueError):
        reader.read_table(source, query=query)
    assert source.getvalue() == before


@pytest.mark.parametrize('query', [
    "SELECT load_extension('outside.dll')", "SELECT readfile('outside.txt')",
    "SELECT writefile('outside.txt', 'data')", 'SELECT randomblob(1000000000)',
    'SELECT zeroblob(1000000000)', "SELECT eval('DELETE FROM entries')",
    "SELECT * FROM pragma_table_info('entries')", 'SELECT * FROM sqlite_dbpage',
    "SELECT * FROM json_each('[1,2,3]')", 'SELECT sqlite_source_id()',
])
def test_functions_and_table_valued_functions_denied(source, query):
    with pytest.raises(ValueError):
        reader.read_table(source, query=query)


@pytest.mark.parametrize('sql', [
    'PRAGMA query_only=OFF', "ATTACH ':memory:' AS other", 'CREATE TABLE nope(x)',
    'INSERT INTO empty VALUES (1, 2)', 'UPDATE entries SET label=1', 'DELETE FROM entries',
    'DROP TABLE empty', 'BEGIN', 'PRAGMA writable_schema=ON',
])
def test_authorizer_independently_rejects_mutations_and_pragmas(source, sql):
    with reader._connection(source) as (connection, _):
        with pytest.raises(apsw.AuthError):
            list(connection.execute(sql))
        assert connection.readonly('main')


def test_virtual_shadow_and_views_never_readable():
    source = database('CREATE TABLE ordinary(id INTEGER)',
                      'INSERT INTO ordinary VALUES (1)',
                      'CREATE VIRTUAL TABLE fulltext USING fts5(text)',
                      "INSERT INTO fulltext VALUES ('secret')",
                      'CREATE VIEW view_data AS SELECT * FROM ordinary')
    assert reader.inspect(source)['tables'] == ['ordinary']
    for table in ('fulltext', 'fulltext_data', 'fulltext_config', 'view_data'):
        with pytest.raises(ValueError):
            reader.read_table(source, selector=table)
        with pytest.raises(ValueError):
            reader.read_table(source, query=f'SELECT count(*) FROM "{table}"')


def test_quoted_identifiers_unknown_columns_and_injection():
    source = database('CREATE TABLE "strange table" ("a""b" TEXT)',
                      'INSERT INTO "strange table" VALUES (\'okay\')')
    assert reader.read_table(source, selector='strange table', columns=['a"b'])['rows'] == [['okay']]
    for name in ('missing', 'a" FROM sqlite_schema --'):
        with pytest.raises(ValueError):
            reader.read_table(source, selector='strange table', columns=[name])


def test_real_file_unchanged_and_no_native_path_reopen(tmp_path, source, monkeypatch):
    path = tmp_path / 'approved.sqlite'
    path.write_bytes(source.getvalue())
    before = hashlib.sha256(path.read_bytes()).digest()
    opens = []
    original = reader._VFS.xOpen
    def audit(self, name, flags):
        opens.append((name.filename() if isinstance(name, apsw.URIFilename) else name, flags[0]))
        return original(self, name, flags)
    monkeypatch.setattr(reader._VFS, 'xOpen', audit)
    with path.open('rb') as stream:
        assert reader.read_table(stream, selector='entries', limit=1)['rows'][0][0] == 0
        assert not stream.closed
    assert hashlib.sha256(path.read_bytes()).digest() == before
    assert [name for name, _ in opens] == ['approved']
    assert all(flags & apsw.SQLITE_OPEN_READONLY and not flags & apsw.SQLITE_OPEN_READWRITE for _, flags in opens)
    assert list(tmp_path.iterdir()) == [path]


def test_vfs_denies_sidecars_attachments_temp_and_writes(source):
    vfs = reader._VFS(source)
    try:
        for name, flags in [('other', apsw.SQLITE_OPEN_MAIN_DB | apsw.SQLITE_OPEN_READONLY),
                            ('approved-journal', apsw.SQLITE_OPEN_MAIN_JOURNAL | apsw.SQLITE_OPEN_READONLY),
                            (None, apsw.SQLITE_OPEN_TEMP_DB | apsw.SQLITE_OPEN_READWRITE),
                            ('approved', apsw.SQLITE_OPEN_MAIN_DB | apsw.SQLITE_OPEN_READWRITE),
                            ('approved', apsw.SQLITE_OPEN_MAIN_DB | apsw.SQLITE_OPEN_READONLY | apsw.SQLITE_OPEN_CREATE)]:
            with pytest.raises(apsw.ReadOnlyError):
                vfs.xOpen(name, [flags, 0])
        for operation, args in ((vfs.file.xWrite, (b'bad', 0)), (vfs.file.xTruncate, (0,)),
                                (vfs.file.xSync, (0,)), (vfs.xDelete, ('anything', False))):
            with pytest.raises(apsw.ReadOnlyError):
                operation(*args)
        assert vfs.xAccess('anything', 0) is False
        assert vfs.xDlOpen('outside.dll') == 0
    finally:
        vfs.unregister()


def test_wal_header_corrupt_file_and_vfs_cleanup(source):
    baseline = set(apsw.vfs_names())
    wal = bytearray(source.getvalue())
    wal[18:20] = b'\x02\x02'
    for data in (b'invalid', source.getvalue()[:100], bytes(wal)):
        with pytest.raises(ValueError):
            reader.inspect(io.BytesIO(data))
    assert set(apsw.vfs_names()) == baseline


def test_read_and_execution_budgets(source, monkeypatch):
    monkeypatch.setattr(reader, 'MAX_READ_BYTES', 1)
    with pytest.raises(ValueError, match='read budget'):
        reader.inspect(source)
    monkeypatch.setattr(reader, 'MAX_READ_BYTES', 64 * 1024**2)
    monkeypatch.setattr(reader, 'MAX_SECONDS', -1)
    with pytest.raises(ValueError, match='read budget'):
        reader.inspect(source)


def test_cpu_expensive_select_is_interrupted(source, monkeypatch):
    monkeypatch.setattr(reader, 'MAX_SECONDS', 0.02)
    started = time.monotonic()
    with pytest.raises(ValueError, match='read budget'):
        reader.read_table(source, query='SELECT sum(a.id) FROM entries a, entries b, entries c, entries d, entries e, entries f, entries g, entries h')
    assert time.monotonic() - started < 1


def test_bounded_large_file_strings_and_responses():
    with sqlite3.connect(':memory:') as connection:
        connection.execute('CREATE TABLE data (id INTEGER, text TEXT)')
        connection.executemany('INSERT INTO data VALUES (?, ?)', ((i, 'x' * 4000) for i in range(1000)))
        source = io.BytesIO(connection.serialize())
    result = reader.read_table(source, selector='data', limit=1)
    assert result['rows'][0][1]['truncated']
    assert len(result['rows'][0][1]['preview']) == 2048
    assert result['next_offset'] == 1
    json.dumps(result, allow_nan=False)
    with pytest.raises(ValueError, match='64 KiB'):
        reader.read_table(source, selector='data', limit=100)


def test_validation_limits(source):
    for kwargs in ({'columns': 'label'}, {'columns': []}, {'columns': ['label', 'label']},
                   {'columns': [None]}, {'offset': -1}, {'limit': 0}, {'limit': True},
                   {'column_limit': 51}, {'column_limit': 50, 'limit': 100}, {'query': 10},
                   {'query': 'SELECT 1', 'columns': ['id']}):
        with pytest.raises(ValueError):
            reader.read_table(source, selector='entries', **kwargs)
