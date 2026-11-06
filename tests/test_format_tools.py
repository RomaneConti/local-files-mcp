import asyncio
import base64
import io
import json
from pathlib import Path
import tomllib

from mcp import Client, StdioServerParameters
from mcp.types import Tool
from jsonschema.validators import validator_for
from PIL import Image
import pytest

from local_files_mcp import __version__
from local_files_mcp.config import Config
from local_files_mcp.server import create_server
from local_files_mcp.readers.common import UNSAFE_SUFFIXES
from local_files_mcp.readers.dispatch import STRUCTURED, recommendation
from local_files_mcp.tool_catalog import EXPECTED_TOOLS


NORMAL_SUFFIXES = {'.npy', '.npz', '.json', '.json.gz', '.jsonl', '.jsonl.gz',
                  '.csv', '.csv.gz', '.tsv', '.tsv.gz', '.yaml', '.yml', '.toml',
                  '.py', '.log', '.log.gz', '.txt', '.txt.gz', '.gz'}
RECOMMENDATION_SUFFIXES = (STRUCTURED | NORMAL_SUFFIXES | UNSAFE_SUFFIXES |
                          {suffix + '.gz' for suffix in UNSAFE_SUFFIXES})


def test_server_version_matches_project_metadata():
    metadata = tomllib.loads((Path(__file__).parents[1] / 'pyproject.toml').read_text(encoding='utf-8'))
    assert __version__ == metadata['project']['version']


@pytest.mark.parametrize('suffix', sorted(RECOMMENDATION_SUFFIXES))
def test_file_info_recommends_only_registered_tools(tmp_path, suffix):
    path = 'sample' + suffix
    # file_info must only stat the file, even for malformed/unsafe content.
    (tmp_path / path).write_bytes(b'metadata only; never deserialize this')
    server = create_server(Config({'test': tmp_path}, frozenset()))

    async def run():
        names = {tool.name for tool in await server.list_tools()}
        result = await server.call_tool('file_info', {'root': 'test', 'path': path})
        assert not result.is_error
        info = result.structured_content
        assert info is not None and 'error' not in info
        expected = recommendation(path)
        assert {key: info[key] for key in expected} == expected
        recommended = set(info.get('readers', []))
        if info['reader'] is not None:
            recommended.add(info['reader'])
        assert recommended <= names, (path, recommended - names)
        if suffix in STRUCTURED:
            assert info['reader'] == 'inspect_file'
        if suffix.removesuffix('.gz') in UNSAFE_SUFFIXES:
            assert info['reader'] is None and not recommended

    asyncio.run(run())


def test_registered_tool_schemas_are_valid_wire_json(tmp_path):
    server = create_server(Config({'test': tmp_path}, frozenset()))

    async def run():
        tools = await server.list_tools()
        assert len(tools) == len(EXPECTED_TOOLS)
        assert {tool.name for tool in tools} == EXPECTED_TOOLS
        for tool in tools:
            wire = tool.model_dump(mode='json', by_alias=True, exclude_none=True)
            encoded = json.dumps(wire, allow_nan=False)
            decoded = Tool.model_validate_json(encoded)
            assert decoded == tool
            for key in ('inputSchema', 'outputSchema'):
                if key in wire:
                    schema = wire[key]
                    validator_for(schema).check_schema(schema)

    asyncio.run(run())


@pytest.mark.parametrize('suffix', ['pkl','pickle','joblib','pt','pth'])
def test_unsafe_content_rejected(tmp_path, suffix):
    path = 'unsafe.'+suffix
    (tmp_path/path).write_bytes(b'never deserialize this')
    server = create_server(Config({'test': tmp_path}, frozenset()))
    async def run():
        for tool in ('read_text', 'read_json', 'read_array', 'inspect_array', 'inspect_file'):
            result = await server.call_tool(tool, {'root': 'test', 'path': path})
            assert 'error' in result.structured_content
        info = await server.call_tool('file_info', {'root': 'test', 'path': path})
        assert info.structured_content['reader'] is None
    asyncio.run(run())


def test_all_new_tools_use_path_policy(tmp_path):
    server = create_server(Config({'test': tmp_path}, frozenset()))
    async def run():
        for name, extra in [('inspect_file',{}),('read_table',{}),('read_document',{}),
            ('search_file',{'query':'x'}),('read_image',{}),('read_archive_member',{'member':'a'})]:
            result = await server.call_tool(name, {'root': 'test', 'path': '../outside', **extra})
            # Media tools have no structured output schema; error is textual JSON.
            value = result.structured_content or json.loads(result.content[0].text)
            assert 'error' in value
    asyncio.run(run())


def test_image_wire_content_and_no_path_reopen(tmp_path, monkeypatch):
    source = io.BytesIO()
    Image.new('RGB',(20,10)).save(source,'PNG')
    (tmp_path/'image.png').write_bytes(source.getvalue())
    server = create_server(Config({'test': tmp_path}, frozenset()))
    original = Image.open
    def checked(fp, *args, **kwargs):
        assert not isinstance(fp, (str, Path)), 'Image parser reopened a path'
        return original(fp, *args, **kwargs)
    monkeypatch.setattr(Image,'open',checked)
    async def run():
        result = await server.call_tool('read_image', {'root':'test','path':'image.png'})
        assert not result.is_error
        assert [block.type for block in result.content] == ['text','image']
        assert len(base64.b64decode(result.content[1].data)) < 512*1024
        assert result.content[1].mime_type == 'image/jpeg'
        assert 'base64' not in result.content[0].text
    asyncio.run(run())
