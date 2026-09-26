import asyncio
import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import time

from mcp import Client, StdioServerParameters
import numpy as np


def make_config(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "sample.py").write_text("def example():\n    return 42\n", encoding="utf-8")
    np.save(data / "sample.npy", np.arange(5))
    config = tmp_path / "config.toml"
    config.write_text("[roots]\ntest = " + json.dumps(str(data)), encoding="utf-8")
    return config


async def protocol_checks(target):
    async with Client(target) as client:
        tools = (await client.list_tools()).tools
        assert len(tools) == 9
        assert all(t.annotations.read_only_hint for t in tools)
        assert all(t.output_schema is not None for t in tools)
        result = await client.call_tool("read_text", {"root": "test", "path": "sample.py"})
        assert result.structured_content["text"].startswith("def example")
        array = await client.call_tool("read_array", {"root": "test", "path": "sample.npy", "count": 2})
        assert array.structured_content["values"] == [0, 1]
        denied = await client.call_tool("read_text", {"root": "test", "path": "../config.toml"})
        assert "error" in denied.structured_content
        missing = await client.call_tool("execute_shell", {"command": "echo unsafe"})
        assert missing.is_error


def test_stdio_real_process(tmp_path):
    config = make_config(tmp_path)
    asyncio.run(protocol_checks(StdioServerParameters(command=sys.executable, args=[
        "-B", "-m", "local_files_mcp.server", "--transport", "stdio", "--config", str(config)])))


def test_http_real_process_and_host_origin_guards(tmp_path):
    config = make_config(tmp_path)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen([sys.executable, "-B", "-m", "local_files_mcp.server",
                                "--config", str(config), "--port", str(port)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            assert process.poll() is None, "HTTP server exited before listening"
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("HTTP server did not start in time")
        asyncio.run(protocol_checks(f"http://127.0.0.1:{port}/mcp"))
        for headers, body, expected in [
            ({"Host": "attacker.example", "Content-Type": "application/json"}, b"{}", {421}),
            ({"Origin": "https://attacker.example", "Content-Type": "application/json"}, b"{}", {403}),
            ({"Content-Type": "application/json"}, b"x" * (65536 + 1), {413}),
        ]:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("POST", "/mcp", body=body, headers=headers)
            response = connection.getresponse()
            assert response.status in expected
            response.read()
            connection.close()
    finally:
        process.terminate()
        process.wait(timeout=10)
