import gzip
import io
import struct
import tarfile
import zipfile
import zlib

from PIL import Image
import pytest

from local_files_mcp.readers import archive, images
from local_files_mcp.readers.common import BudgetIO, safe_zip


def make_archive(kind, members=None):
    members = members or {'folder/a.txt': b'hello archive\n'*10, 'b.txt': b'second'}
    stream = io.BytesIO()
    if kind == '.zip':
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as result:
            for name, data in members.items():
                result.writestr(name, data)
    else:
        with tarfile.open(fileobj=stream, mode='w') as result:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                result.addfile(info, io.BytesIO(data))
        if kind == '.tar.gz':
            stream = io.BytesIO(gzip.compress(stream.getvalue()))
    stream.seek(0)
    return stream


@pytest.mark.parametrize('kind', ['.zip', '.tar', '.tar.gz'])
def test_archive_list_read_pages(kind):
    stream = make_archive(kind)
    before = stream.getvalue()
    result = archive.inspect(stream, kind, limit=1)
    assert result['members'][0]['name'] == 'folder/a.txt'
    assert result['next_offset'] == 1
    result = archive.read(stream, kind, 'folder/a.txt', byte_offset=6, max_bytes=16)
    assert result['text'].startswith('archive')
    assert result['next_byte_offset'] == 22
    assert stream.getvalue() == before


@pytest.mark.parametrize('kind', ['.zip', '.tar', '.tar.gz'])
def test_archive_boundaries_and_corruption(kind):
    with pytest.raises(ValueError):
        archive.read(make_archive(kind), kind, '../outside', max_bytes=16)
    with pytest.raises(ValueError):
        archive.read(make_archive(kind), kind, 'folder/a.txt', byte_offset=8*1024**2+1)
    with pytest.raises(ValueError):
        archive.read(make_archive(kind), kind, 'folder/a.txt', max_bytes=32769)
    with pytest.raises((ValueError, OSError)):
        archive.inspect(io.BytesIO(b'not an archive'), kind)
    with pytest.raises(ValueError, match='deserialization'):
        archive.read(make_archive(kind, {'evil.pkl': b'payload'}), kind, 'evil.pkl')


def test_zip_bomb_and_central_directory_cap():
    stream = make_archive('.zip', {'bomb.txt': b'a'*(2*1024**2)})
    with pytest.raises(ValueError, match='expansion'):
        archive.inspect(stream, '.zip')
    data = bytearray(make_archive('.zip').getvalue())
    pos = data.rfind(b'PK\x05\x06')
    struct.pack_into('<HH', data, pos+8, 65535, 65535)
    with pytest.raises(ValueError, match='directory'):
        archive.inspect(io.BytesIO(data), '.zip')


@pytest.mark.parametrize('name', ['../outside', '/absolute', 'C:/outside', 'foo/../../outside'])
def test_zip_traversal_names(name):
    with pytest.raises(ValueError, match='Unsafe'):
        archive.inspect(make_archive('.zip', {name: b'hello'}), '.zip')


def test_tar_links_are_listed_blocked_and_not_read():
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as output:
        info = tarfile.TarInfo('link')
        info.type = tarfile.SYMTYPE
        info.linkname = '../../outside'
        output.addfile(info)
    stream.seek(0)
    assert not archive.inspect(stream, '.tar')['members'][0]['safe']
    with pytest.raises(ValueError, match='Unsafe'):
        archive.read(stream, '.tar', 'link')


def test_tar_gzip_decompression_limit(monkeypatch):
    monkeypatch.setattr(archive, 'MAX_TAR_SCAN', 2048)
    with pytest.raises(ValueError, match='decompressed'):
        archive.inspect(make_archive('.tar.gz'), '.tar.gz')


def test_zip_cumulative_compressed_read_budget():
    with pytest.raises(ValueError, match='budget'):
        with safe_zip(BudgetIO(make_archive('.zip'), budget=10)):
            pass


@pytest.mark.parametrize('kind', ['PNG', 'JPEG', 'WEBP'])
def test_images_metadata_and_native_preview(kind):
    source = io.BytesIO()
    Image.new('RGB', (300, 150), (30, 80, 110)).save(source, kind)
    before = source.getvalue()
    result = images.inspect(source)
    assert (result['width'], result['height']) == (300, 150)
    metadata, binary = images.read(source, 128)
    assert metadata['preview_dimensions'] == [128, 64]
    assert len(binary) <= images.MAX_IMAGE_BYTES
    with Image.open(io.BytesIO(binary)) as decoded:
        assert decoded.format == 'JPEG'
    assert source.getvalue() == before


@pytest.mark.parametrize('kind', ['PNG', 'JPEG', 'WEBP'])
def test_images_size_limits_and_corruption(kind, monkeypatch):
    source = io.BytesIO()
    Image.new('RGB', (100, 100)).save(source, kind)
    monkeypatch.setattr(images, 'MAX_PIXELS', 5000)
    with pytest.raises(ValueError, match='pixels'):
        images.read(source)
    monkeypatch.setattr(images, 'MAX_FILE_BYTES', 10)
    with pytest.raises(ValueError, match='file limit'):
        images.inspect(source)
    with pytest.raises((OSError, ValueError)):
        images.read(io.BytesIO(b'broken'))


def test_image_dimension_and_format_allowlist():
    with pytest.raises(ValueError):
        images.read(io.BytesIO(), max_dimension=10000)
    source = io.BytesIO()
    Image.new('RGB', (1, 1)).save(source, 'BMP')
    with pytest.raises(OSError):
        images.read(source)


def _webp_container(*chunks):
    payload = b'WEBP' + b''.join(kind + struct.pack('<I', len(data)) + data + b'\0'*(len(data) & 1)
                                for kind, data in chunks)
    return io.BytesIO(b'RIFF' + struct.pack('<I', len(payload)) + payload)


@pytest.mark.parametrize('header_kind', ['extended', 'lossy', 'lossless'])
def test_webp_dimension_preflight_precedes_native_decoder(header_kind, monkeypatch):
    def never_open(*args, **kwargs):
        pytest.fail('Image.open must not allocate before WebP dimension preflight')

    monkeypatch.setattr(images.Image, 'open', never_open)
    if header_kind == 'extended':
        data = b'\0'*4 + (30000-1).to_bytes(3, 'little')*2
        stream = _webp_container((b'VP8X', data))
    elif header_kind == 'lossy':
        data = b'\0'*3 + b'\x9d\x01\x2a' + struct.pack('<HH', 16000, 16000)
        stream = _webp_container((b'VP8 ', data))
    else:
        bits = (16000-1) | ((16000-1) << 14)
        stream = _webp_container((b'VP8L', b'\x2f' + struct.pack('<I', bits)))
    with pytest.raises(ValueError, match='pixels'):
        images.inspect(stream)


@pytest.mark.parametrize('mode', ['flag', 'frame_chunk', 'mismatched_frame'])
def test_webp_animated_or_mismatched_frames_rejected_before_pillow(mode, monkeypatch):
    def never_open(*args, **kwargs):
        pytest.fail('Unsafe WebP must be rejected before decoder creation')

    monkeypatch.setattr(images.Image, 'open', never_open)
    header = bytes([2 if mode == 'flag' else 0]) + b'\0'*3 + (1).to_bytes(3, 'little')*2
    chunks = [(b'VP8X', header)]
    if mode == 'frame_chunk':
        chunks.append((b'ANMF', b'\0'*16))
    elif mode == 'mismatched_frame':
        chunks.append((b'VP8 ', b'\0'*3 + b'\x9d\x01\x2a' + struct.pack('<HH', 3, 3)))
    with pytest.raises(ValueError, match='Animated|inconsistent'):
        images.inspect(_webp_container(*chunks))


def test_webp_lossless_alpha_still_preview_is_supported():
    source = io.BytesIO()
    Image.new('RGBA', (31, 17), (1, 2, 3, 120)).save(source, 'WEBP', lossless=True)
    assert images.inspect(source)['width'] == 31
    assert images.read(source, 64)[0]['preview_dimensions'] == [31, 17]


def test_webp_real_animation_rejected_without_pixel_decode():
    source = io.BytesIO()
    first = Image.new('RGB', (4, 4), 'red')
    first.save(source, 'WEBP', save_all=True, append_images=[Image.new('RGB', (4, 4), 'blue')], duration=100)
    with pytest.raises(ValueError, match='Animated'):
        images.inspect(source)


def test_webp_corrupt_riff_length_fails_before_native_decoder(monkeypatch):
    source = io.BytesIO()
    Image.new('RGB', (4, 4)).save(source, 'WEBP')
    data = bytearray(source.getvalue())
    struct.pack_into('<I', data, 4, len(data) + 500)
    monkeypatch.setattr(images.Image, 'open', lambda *args, **kwargs: pytest.fail('Image.open should not run'))
    with pytest.raises(ValueError, match='container length'):
        images.inspect(io.BytesIO(data))
