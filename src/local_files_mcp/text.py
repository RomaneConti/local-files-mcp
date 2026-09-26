"""Bounded byte windows for text, logs and JSONL, optionally gzip compressed."""

import codecs
from contextlib import contextmanager
import gzip
from typing import BinaryIO

MAX_WINDOW = 32 * 1024
MAX_GZIP_PREFIX = 8 * 1024 * 1024
MAX_COMPRESSED_READ = 16 * 1024 * 1024
ENCODINGS = {"utf-8", "utf-8-sig", "utf-16-le", "utf-16-be", "gb18030", "cp1252", "ascii"}


class ReadBudget:
    """Cap compressed input too, including maliciously long gzip headers."""

    def __init__(self, stream: BinaryIO, limit: int = MAX_COMPRESSED_READ):
        self.stream, self.remaining = stream, limit

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            raise ValueError("Compressed input budget exceeded")
        size = self.remaining if size < 0 else min(size, self.remaining)
        data = self.stream.read(size)
        self.remaining -= len(data)
        return data


@contextmanager
def text_stream(stream: BinaryIO, compressed: bool):
    if compressed:
        with gzip.GzipFile(fileobj=ReadBudget(stream), mode="rb") as gz:
            yield gz
    else:
        yield stream


def validate_encoding(encoding: str) -> None:
    if encoding not in ENCODINGS:
        raise ValueError("Unsupported encoding; use " + ", ".join(sorted(ENCODINGS)))


def decode_text(data: bytes, encoding: str, *, final: bool = True) -> tuple[str, int]:
    validate_encoding(encoding)
    if not encoding.startswith("utf-16") and b"\x00" in data:
        raise ValueError("Binary data detected; use an appropriate format reader")
    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    result = decoder.decode(data, final=final)
    pending = decoder.getstate()[0]
    return result, len(data) - len(pending)


def read_window(stream: BinaryIO, *, compressed: bool = False, byte_offset: int = 0,
                max_bytes: int = 16 * 1024, encoding: str = "utf-8",
                start_line: int | None = None) -> dict:
    validate_encoding(encoding)
    if byte_offset < 0 or not 16 <= max_bytes <= MAX_WINDOW:
        raise ValueError("byte_offset >= 0 and 16 <= max_bytes <= 32768 required")
    if start_line is not None and (start_line < 1 or byte_offset != 0):
        raise ValueError("start_line >= 1 cannot be combined with byte_offset")
    if start_line is not None and encoding.startswith("utf-16"):
        raise ValueError("Use byte_offset with UTF-16")
    if compressed and byte_offset + max_bytes > MAX_GZIP_PREFIX:
        raise ValueError("gzip windows must stay within first 8 MiB decompressed; split large logs locally")
    with text_stream(stream, compressed) as reader:
        if start_line is not None:
            byte_offset = 0
            line = 1
            while line < start_line:
                remaining = MAX_GZIP_PREFIX - byte_offset
                if remaining <= 0:
                    raise ValueError("Line lookup exceeds 8 MiB scan budget; use byte_offset for plain text")
                chunk = reader.readline(min(65536, remaining))
                if not chunk:
                    break
                byte_offset += len(chunk)
                if chunk.endswith(b"\n"):
                    line += 1
            if line < start_line:
                return {"text": "", "byte_offset": byte_offset, "next_byte_offset": None,
                        "eof": True, "start_line": start_line, "compressed": compressed}
            if compressed and byte_offset + max_bytes > MAX_GZIP_PREFIX:
                raise ValueError("gzip line lookup plus window exceeds 8 MiB decompressed budget")
        elif compressed:
            remaining = byte_offset
            while remaining:
                chunk = reader.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            if remaining:
                return {"text": "", "byte_offset": byte_offset, "next_byte_offset": None,
                        "eof": True, "compressed": True}
        else:
            reader.seek(byte_offset)
        data = reader.read(max_bytes)
        eof = len(data) < max_bytes
        value, consumed = decode_text(data, encoding, final=eof)
        return {"text": value, "byte_offset": byte_offset,
                "next_byte_offset": None if eof else byte_offset + consumed,
                "eof": eof, "bytes_returned": consumed, "encoding": encoding,
                "compressed": compressed, "start_line": start_line,
                "note": "Offsets refer to uncompressed bytes. Windows may begin/end mid-line; files can change between calls."}
