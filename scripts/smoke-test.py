"""Read-only MCP client check against the configured real roots. No file writes."""

import argparse
import asyncio
import json
from pathlib import Path
import sys

from mcp import Client, StdioServerParameters
from local_files_mcp.user_config import default_data_dir


async def check(url: str | None, config: Path | None = None, list_tools: bool = False):
    project = Path(__file__).resolve().parents[1]
    target = url or StdioServerParameters(
        command=sys.executable,
        args=["-B", "-m", "local_files_mcp.server", "--transport", "stdio",
              "--config", str(config or default_data_dir() / "config.toml")], cwd=str(project))
    async with Client(target) as client:
        tools = (await client.list_tools()).tools
        from local_files_mcp.tool_catalog import EXPECTED_TOOLS
        discovery = {"server": client.server_info.model_dump(by_alias=True, exclude_none=True),
                     "tool_count": len(tools), "tools": sorted(t.name for t in tools)}
        if list_tools:
            print(json.dumps(discovery, indent=2))
            return
        assert {tool.name for tool in tools} == EXPECTED_TOOLS
        assert all(t.annotations.read_only_hint and not t.annotations.destructive_hint for t in tools)

        async def call(name, **arguments):
            reply = await client.call_tool(name, arguments)
            assert not reply.is_error, reply
            result = reply.structured_content
            assert result is not None, reply
            assert "error" not in result, result
            return result

        roots = (await call("list_roots"))["roots"]
        reports = []
        for entry in roots:
            root = entry["root"]
            listing = await call("list_directory", root=root, limit=5)
            found = await call("find_files", root=root, glob="*.py", limit=3, max_depth=4)
            if not found["paths"]:
                reports.append({"root": root, "entries_shown": len(listing["entries"]),
                                "note": "No Python sample within bounded search; browse verified"})
                continue
            candidate = found["paths"][0]
            metadata = await call("file_info", root=root, path=candidate)
            content = await call("read_text", root=root, path=candidate, max_bytes=512)
            folder = str(Path(candidate).parent).replace("\\", "/")
            search = await call("search_text", root=root, path=folder, query="def ", limit=2, max_depth=0)
            denied = await client.call_tool("read_text", {"root": root, "path": "../outside.txt"})
            assert "error" in denied.structured_content
            reports.append({"root": root, "entries_shown": len(listing["entries"]),
                            "file_read": candidate, "file_size": metadata["size_bytes"],
                            "bytes_read": content["bytes_returned"], "function_matches": len(search["matches"]),
                            "traversal_denied": True})
        print(json.dumps({"transport": url or "stdio", **discovery, "roots": reports}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="Existing loopback HTTP server; omit to launch an isolated stdio process")
    parser.add_argument("--config", type=Path, help="Override user config for an isolated stdio check")
    parser.add_argument("--list-tools", action="store_true", help="Print actual MCP server version and tool names without reading files")
    args = parser.parse_args()
    asyncio.run(check(args.url, args.config, args.list_tools))
