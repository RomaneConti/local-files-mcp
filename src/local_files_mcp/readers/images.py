"""Bounded raster previews; decoded pixels become MCP image content."""
import io
import struct
from PIL import Image, ImageOps, PngImagePlugin
from .common import BudgetIO, file_size, integer

MAX_FILE_BYTES = 16*1024**2
MAX_PIXELS = 16_000_000
MAX_IMAGE_BYTES = 512*1024
PngImagePlugin.MAX_TEXT_CHUNK = 256*1024
PngImagePlugin.MAX_TEXT_MEMORY = 1024*1024


def _dimensions(width, height):
    if width <= 0 or height <= 0 or width * height > MAX_PIXELS or max(width, height) > 32768:
        raise ValueError('Image exceeds 16 million pixels or dimension limit')


def _webp_dimensions(stream, size):
    """Check raw container/bitstream dimensions before libwebp allocates pixels.

    Pillow creates WebPAnimDecoder during Image.open; that constructor allocates
    two full canvases before Pillow exposes image.size. Animated files are not
    supported here so frame metadata cannot bypass a canvas-only size check.
    """
    stream.seek(0)
    header = stream.read(12)
    if len(header) != 12 or header[:4] != b'RIFF' or header[8:] != b'WEBP':
        raise ValueError('Invalid WebP container')
    if struct.unpack_from('<I', header, 4)[0] + 8 != size:
        raise ValueError('Invalid WebP container length')
    position, chunks = 12, 0
    canvas = frame = None
    while position < size:
        if chunks >= 4096 or position + 8 > size:
            raise ValueError('WebP chunk metadata exceeds limits')
        stream.seek(position)
        chunk_header = stream.read(8)
        if len(chunk_header) != 8:
            raise ValueError('Truncated WebP chunk header')
        kind, length = struct.unpack('<4sI', chunk_header)
        following = position + 8 + length + (length & 1)
        if following > size:
            raise ValueError('WebP chunk exceeds its container')
        if chunks == 0 and kind not in {b'VP8X', b'VP8 ', b'VP8L'}:
            raise ValueError('Unsupported WebP header order')
        if kind in {b'ANIM', b'ANMF'}:
            raise ValueError('Animated WebP is not supported; export a still image')
        if kind == b'VP8X':
            if chunks != 0 or canvas is not None or length != 10:
                raise ValueError('Invalid extended WebP header')
            data = stream.read(10)
            if len(data) != 10:
                raise ValueError('Truncated extended WebP header')
            if data[0] & 2:
                raise ValueError('Animated WebP is not supported; export a still image')
            canvas = (int.from_bytes(data[4:7], 'little') + 1,
                      int.from_bytes(data[7:10], 'little') + 1)
            _dimensions(*canvas)
        elif kind in {b'VP8 ', b'VP8L'}:
            if frame is not None:
                raise ValueError('Multiple WebP image chunks are not supported')
            required = 10 if kind == b'VP8 ' else 5
            if length < required:
                raise ValueError('Truncated WebP image header')
            data = stream.read(required)
            if len(data) != required:
                raise ValueError('Truncated WebP image header')
            if kind == b'VP8 ':
                if data[0] & 1 or data[3:6] != b'\x9d\x01\x2a':
                    raise ValueError('Unsupported WebP VP8 frame header')
                width, height = struct.unpack_from('<HH', data, 6)
                frame = (width & 0x3fff, height & 0x3fff)
            else:
                bits = int.from_bytes(data[1:5], 'little')
                if data[0] != 0x2f or bits >> 29:
                    raise ValueError('Unsupported WebP lossless header')
                frame = ((bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1)
            _dimensions(*frame)
        position = following
        chunks += 1
    if frame is None or (canvas is not None and canvas != frame):
        raise ValueError('Missing or inconsistent WebP canvas/frame dimensions')
    return frame


def _open(stream):
    size = file_size(stream)
    if size > MAX_FILE_BYTES:
        raise ValueError('Image exceeds 16 MiB file limit')
    stream.seek(0)
    prefix = stream.read(12)
    expected = _webp_dimensions(stream, size) if prefix[:4] == b'RIFF' and prefix[8:] == b'WEBP' else None
    stream.seek(0)
    image = Image.open(BudgetIO(stream), formats=['PNG', 'JPEG', 'WEBP'])
    try:
        _dimensions(*image.size)
        if expected is not None and image.size != expected:
            raise ValueError('WebP decoded dimensions differ from the checked header')
    except ValueError:
        image.close()
        raise
    return image


def inspect(stream, suffix='', selector='', offset=0, limit=50):
    with _open(stream) as image:
        return {'format': image.format, 'width': image.width, 'height': image.height,
            'mode': image.mode, 'reader': 'read_image', 'frame': 0,
            'note': 'Preview of first frame only; no OCR or external resources.'}


def read(stream, max_dimension=1536):
    integer(max_dimension, 'max_dimension', 64, 1536)
    with _open(stream) as source:
        original = list(source.size)
        source.seek(0)
        source.load()
        image = ImageOps.exif_transpose(source).convert('RGB')
        image.thumbnail((max_dimension, max_dimension))
        # Strip EXIF/ICC/other untrusted metadata and bound the final content block.
        image.info.clear()
        for quality in (85, 65, 45):
            output = io.BytesIO()
            image.save(output, format='JPEG', quality=quality)
            data = output.getvalue()
            if len(data) <= MAX_IMAGE_BYTES:
                return {'original_dimensions': original, 'preview_dimensions': list(image.size),
                    'frame': 0, 'mime_type': 'image/jpeg', 'size_bytes': len(data)}, data
        raise ValueError('Image preview exceeds 512 KiB; request a smaller max_dimension')
