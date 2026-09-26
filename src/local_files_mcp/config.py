"""Small, local-only configuration. MCP clients cannot change it."""

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib


@dataclass(frozen=True)
class Config:
    roots: dict[str, Path]
    skip_directories: frozenset[str]


def load_config(path: Path) -> Config:
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    if set(data) - {"roots", "browse"}:
        raise ValueError("Unknown configuration section")
    roots = data.get("roots", {})
    if not isinstance(roots, dict) or not roots or len(roots) > 32:
        raise ValueError("Configure 1 to 32 roots")
    for name, value in roots.items():
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
            raise ValueError("Root names must be short alphanumeric aliases")
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError("Each configured root must be an absolute local path")
    browse = data.get("browse", {})
    if not isinstance(browse, dict) or set(browse) - {"skip_directories"}:
        raise ValueError("Unknown browse configuration")
    skipped = browse.get("skip_directories", [])
    if not isinstance(skipped, list) or not all(isinstance(x, str) for x in skipped):
        raise ValueError("skip_directories must be a list of names")
    return Config({key: Path(value) for key, value in roots.items()},
                  frozenset(x.casefold() for x in skipped))
