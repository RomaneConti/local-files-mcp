"""Real HTTP calls across every format, optionally against the packaged EXE."""
import asyncio
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time

import numpy as np
import pyarrow as pa
import pyarrow.feather as feather
import pyarrow.parquet as parquet
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from mcp import Client
import pytest

from test_office import archive as office_archive, workbook, word, presentation
from test_scientific_readers import hdf, mat5, matrix
from test_archive_images import make_archive
from test_transport import raw_http_discovery_checks
from local_files_mcp import __version__
from local_files_mcp.supervisor import probe_mcp
from local_files_mcp.tool_catalog import EXPECTED_TOOLS


def fixtures(folder):
    for suffix, parts in [('xlsx',workbook()),('xlsm',workbook()),('docx',word()),('pptx',presentation())]:
        (folder/('sample.'+suffix)).write_bytes(office_archive(parts).getvalue())
    for suffix in ('h5','hdf5','mat'):
        (folder/('sample.'+suffix)).write_bytes(hdf(np.arange(12).reshape(3,4)).getvalue())
    (folder/'classic.mat').write_bytes(mat5(matrix('data', np.arange(6).reshape(2,3))).getvalue())
    table = pa.table({'a':[1,2,3], 'b':['first','second','third']})
    parquet.write_table(table, folder/'sample.parquet', row_group_size=2)
    feather.write_feather(table, folder/'sample.feather', compression='uncompressed')
    feather.write_feather(table, folder/'compressed.feather')
    with closing(sqlite3.connect(folder/'sample.db')) as db:
        db.execute('CREATE TABLE data (a INTEGER, b TEXT)')
        db.executemany('INSERT INTO data VALUES (?,?)', [(1,'first'),(2,'second')])
        db.commit()
    for suffix in ('png','jpg','jpeg','webp'):
        Image.new('RGB',(30,20),(20,40,90)).save(folder/('sample.'+suffix))
    for suffix in ('.zip','.tar','.tar.gz'):
        (folder/('sample'+suffix)).write_bytes(make_archive(suffix).getvalue())
    writer = PdfWriter()
    page = writer.add_blank_page(width=200,height=100)
    font = DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
    content = DecodedStreamObject()
    content.set_data(b'BT /F1 12 Tf 10 50 Td (Hello PDF) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(content)
    with (folder/'sample.pdf').open('wb') as stream:
        writer.write(stream)


@pytest.mark.parametrize('packaged', [False, True])
def test_real_http_formats_and_unchanged_files(tmp_path, packaged):
    executable = os.environ.get('LOCAL_FILES_MCP_TEST_EXE')
    if packaged and not executable:
        pytest.skip('Set LOCAL_FILES_MCP_TEST_EXE to test the actual built bundle')
    data = tmp_path/'data'
    data.mkdir()
    fixtures(data)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in data.iterdir()}
    config = tmp_path/'config.toml'
    config.write_text('[roots]\ntest = '+json.dumps(str(data)),encoding='utf-8')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0))
        port = listener.getsockname()[1]
    command = [executable,'--mcp-server'] if packaged else [sys.executable,'-B','-m','local_files_mcp.server']
    command += ['--config',str(config),'--port',str(port)]
    process = subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        for _ in range(200):
            assert process.poll() is None, 'MCP process exited'
            if probe_mcp(port)[0]:
                break
            time.sleep(.1)
        else:
            pytest.fail('MCP protocol readiness timeout')
        raw_http_discovery_checks(port)
        async def run():
            async with Client(f'http://127.0.0.1:{port}/mcp') as client:
                assert client.server_info is not None
                assert client.server_info.version == __version__
                listing = await client.list_tools()
                names = {tool.name for tool in listing.tools}
                assert names == EXPECTED_TOOLS
                assert len(listing.tools) == len(EXPECTED_TOOLS)
                assert listing.next_cursor is None
                async def call(name, path, **args):
                    result = await client.call_tool(name, {'root':'test','path':path,**args})
                    assert not result.is_error, (name,path,result)
                    if name == 'read_image':
                        assert result.content[1].type == 'image'
                        return None
                    value = result.structured_content
                    assert value is not None and 'error' not in value, (name,path,value)
                    return value
                for path in sorted(before):
                    await call('inspect_file',path)
                    recommendation = await call('file_info',path)
                    assert recommendation['reader']=='inspect_file'
                    assert {recommendation['reader'], *recommendation.get('readers', [])} <= names
                for suffix in ('xlsx','xlsm'):
                    value = await call('read_table','sample.'+suffix,selector='Data',columns=['A','B'],limit=1)
                    assert value['rows'][0]['values']==['Alpha',12]
                    assert (await call('search_file','sample.'+suffix,query='Alpha'))['matches']
                for suffix in ('parquet','feather'):
                    assert (await call('read_table','sample.'+suffix,columns=['b'],offset=1,limit=1))['rows']==[['second']]
                assert (await call('read_table','compressed.feather',columns=['a'],offset=1,limit=1))['rows']==[[2]]
                for suffix in ('h5','hdf5','mat'):
                    assert (await call('read_array','sample.'+suffix,member='data',start=[1,1],shape=[1,2]))['values']==[5,6]
                assert (await call('read_array','classic.mat',member='data',count=2))['values']==[0.0,3.0]
                for suffix, query in [('docx','Introduction'),('pptx','Slide'),('pdf','Hello')]:
                    await call('read_document','sample.'+suffix,limit=1)
                    assert (await call('search_file','sample.'+suffix,query=query))['matches']
                assert (await call('read_table','sample.db',selector='data',limit=1))['rows']==[[1,'first']]
                await call('read_table','sample.db',query='SELECT a FROM data ORDER BY a DESC',limit=1)
                for suffix in ('png','jpg','jpeg','webp'):
                    await call('read_image','sample.'+suffix,max_dimension=128)
                for suffix in ('.zip','.tar','.tar.gz'):
                    assert 'hello' in (await call('read_archive_member','sample'+suffix,member='folder/a.txt'))['text']
        asyncio.run(run())
    finally:
        process.terminate()
        process.wait(timeout=20)
    assert before == {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in data.iterdir()}
    # Windows may briefly finish TCP teardown after the process handle signals.
    for _ in range(50):
        with socket.socket() as listener:
            listener.settimeout(.1)
            if listener.connect_ex(('127.0.0.1',port)) != 0:
                break
        time.sleep(.1)
    else:
        pytest.fail('Test MCP port remains open after process exit')
