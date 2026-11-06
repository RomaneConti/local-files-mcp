"""Generic directory traversal and literal code/log search; no subprocesses."""

from dataclasses import dataclass, field
import fnmatch
import os
from pathlib import PurePosixPath
import stat
import time

from .security import PathPolicy, PathAccessError
from .text import decode_text, text_stream, validate_encoding
from .readers.dispatch import refuse_unsafe

MAX_VISITED = 10000
MAX_SECONDS = 5
MAX_SEARCH_BYTES = 16 * 1024 * 1024
MAX_FILE_SEARCH_BYTES = 1024 * 1024


@dataclass
class Scan:
    visited: int = 0
    skipped: int = 0
    unavailable: int = 0
    reasons: set[str] = field(default_factory=set)
    started: float = field(default_factory=time.monotonic)

    def check(self) -> bool:
        if self.visited >= MAX_VISITED:
            self.reasons.add("entry_budget")
        if time.monotonic() - self.started > MAX_SECONDS:
            self.reasons.add("time_budget")
        return not self.reasons


def page_args(offset: int, limit: int):
    if not 0 <= offset <= MAX_VISITED or not 1 <= limit <= 200:
        raise ValueError("offset must be 0..10000, limit 1..200")


def _entry_kind(entry: os.DirEntry) -> str:
    info = entry.stat(follow_symlinks=False)
    if getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return "blocked_link"
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    if stat.S_ISREG(info.st_mode):
        # DirEntry.stat on Windows may report st_nlink=0; the native policy
        # checks the actual link count before metadata or content is returned.
        return "file"
    return "blocked_special"


def walk(policy: PathPolicy, root: str, path: str, skipped: frozenset[str],
         state: Scan, max_depth: int = 12):
    if not 0 <= max_depth <= 32:
        raise ValueError("max_depth must be 0..32")

    def visit(relative: str, depth: int):
        try:
            with policy.directory(root, relative) as directory:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if not state.check():
                            return
                        state.visited += 1
                        child = str(PurePosixPath(relative.replace("\\", "/")) / entry.name)
                        try:
                            kind = _entry_kind(entry)
                        except OSError:
                            state.skipped += 1
                            state.unavailable += 1
                            continue
                        if kind == "file":
                            try:
                                policy.stat(root, child)
                            except (OSError, PathAccessError):
                                state.skipped += 1
                                state.unavailable += 1
                                continue
                            yield child
                        elif kind == "directory":
                            if entry.name.casefold() in skipped:
                                state.skipped += 1
                            elif depth < max_depth:
                                yield from visit(child, depth + 1)
                            else:
                                state.skipped += 1
                        else:
                            state.skipped += 1
        except (OSError, PathAccessError):
            if depth == 0:
                raise
            state.skipped += 1
            state.unavailable += 1

    yield from visit(path, 0)


def list_directory(policy: PathPolicy, root: str, path: str = ".", offset: int = 0,
                   limit: int = 100) -> dict:
    page_args(offset, limit)
    result = []
    state = Scan()
    has_more = False
    with policy.directory(root, path) as directory, os.scandir(directory) as entries:
        for index, entry in enumerate(entries):
            if not state.check():
                break
            state.visited += 1
            if index < offset:
                continue
            if len(result) == limit:
                has_more = True
                break
            child = str(PurePosixPath(path.replace("\\", "/")) / entry.name)
            try:
                kind = _entry_kind(entry)
                # Validate native names as well (reserved names/ADS/ambiguous aliases).
                info = policy.stat(root, child) if kind in {"directory", "file"} else None
                result.append({"path": child, "kind": kind,
                               "size_bytes": info.st_size if info and kind == "file" else None})
            except (OSError, PathAccessError):
                result.append({"path": child, "kind": "blocked_or_unavailable", "size_bytes": None})
    return {"entries": result, "next_offset": offset + len(result) if has_more else None,
            "complete": not has_more and not state.reasons, "limits_reached": sorted(state.reasons),
            "order": "filesystem order; pagination is not a snapshot"}


def matches(path: str, pattern: str) -> bool:
    if not pattern or len(pattern) > 256:
        raise ValueError("glob must contain 1..256 characters")
    pattern = pattern.replace("\\", "/").casefold()
    name = path.casefold()
    return fnmatch.fnmatchcase(name, pattern) or fnmatch.fnmatchcase(PurePosixPath(name).name, pattern)


def find_files(policy: PathPolicy, skipped: frozenset[str], root: str, path: str = ".",
               glob: str = "*", offset: int = 0, limit: int = 100, max_depth: int = 12) -> dict:
    page_args(offset, limit)
    matches("", glob)
    state, results, found = Scan(), [], 0
    has_more = False
    for child in walk(policy, root, path, skipped, state, max_depth):
        if not matches(child, glob):
            continue
        found += 1
        if found <= offset:
            continue
        if len(results) == limit:
            has_more = True
            break
        results.append(child)
    return {"paths": results, "next_offset": offset + len(results) if has_more else None,
            "complete_within_scope": not has_more and not state.reasons and state.unavailable == 0,
            "limits_reached": sorted(state.reasons), "visited": state.visited,
            "skipped_entries": state.skipped, "max_depth": max_depth,
            "skipped_directory_names": sorted(skipped),
            "note": "If a scan budget is reached, narrow path/glob; offsets do not bypass scan budgets."}


def search_text(policy: PathPolicy, skipped: frozenset[str], root: str, query: str,
                path: str = ".", glob: str = "*.py", case_sensitive: bool = False,
                limit: int = 30, offset: int = 0, max_depth: int = 12,
                encoding: str = "utf-8") -> dict:
    page_args(offset, limit)
    if not 1 <= len(query) <= 512 or "\n" in query or "\r" in query:
        raise ValueError("query must be a literal single-line string of 1..512 characters")
    validate_encoding(encoding)
    if encoding.startswith("utf-16"):
        raise ValueError("Search supports byte-oriented encodings; use read_text for UTF-16")
    matches("", glob)
    needle = query if case_sensitive else query.casefold()
    state, results, found, read_bytes, file_count, partial_files = Scan(), [], 0, 0, 0, 0
    has_more = False
    for child in walk(policy, root, path, skipped, state, max_depth):
        if not matches(child, glob):
            continue
        if read_bytes >= MAX_SEARCH_BYTES or file_count >= 500:
            state.reasons.add("search_budget")
            break
        try:
            refuse_unsafe(child)
            with policy.open_file(root, child) as stream, text_stream(stream, child.lower().endswith(".gz")) as reader:
                budget = min(MAX_FILE_SEARCH_BYTES, MAX_SEARCH_BYTES - read_bytes)
                data = reader.read(budget)
            read_bytes += len(data)
            file_count += 1
            if len(data) == budget:
                partial_files += 1
            decode_text(data[:8192], encoding)
            position = 0
            for line_num, line in enumerate(data.splitlines(keepends=True), 1):
                if time.monotonic() - state.started > MAX_SECONDS:
                    state.reasons.add("time_budget")
                    break
                value = line.decode(encoding, errors="replace").rstrip("\r\n")
                haystack = value if case_sensitive else value.casefold()
                match_at = haystack.find(needle)
                if match_at >= 0:
                    found += 1
                    if found > offset:
                        if len(results) == limit:
                            has_more = True
                            break
                        # casefold can expand characters (e.g. ß -> ss).
                        original_at = match_at
                        if not case_sensitive and len(haystack) != len(value):
                            folded_at = 0
                            for original_at, char in enumerate(value):
                                folded_at += len(char.casefold())
                                if folded_at > match_at:
                                    break
                        excerpt_start = max(0, original_at - 120)
                        results.append({"path": child, "line": line_num, "byte_offset": position,
                                        "text": value[excerpt_start:excerpt_start + 400],
                                        "line_truncated": len(value) > 400})
                position += len(line)
            if has_more or state.reasons:
                break
        except (OSError, ValueError, EOFError):
            state.skipped += 1
            state.unavailable += 1
    return {"matches": results, "next_offset": offset + len(results) if has_more else None,
            "complete_within_scope": not has_more and not state.reasons and partial_files == 0 and state.unavailable == 0,
            "limits_reached": sorted(state.reasons), "files_searched": file_count,
            "bytes_searched": read_bytes, "files_with_prefix_only": partial_files,
            "skipped_entries": state.skipped, "max_depth": max_depth,
            "skipped_directory_names": sorted(skipped),
            "note": "Literal search, at most first 1 MiB per file. No match is not proof of absence when skipped/partial. Narrow the directory or use read_text byte windows."}
