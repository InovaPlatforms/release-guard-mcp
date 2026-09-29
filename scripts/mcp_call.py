#!/usr/bin/env python3
"""Call Release Guard over real MCP stdio, the same way Codex does.

    python scripts/mcp_call.py --list
    python scripts/mcp_call.py check_build_status '{"version": "2.88"}'

The server inherits this process's environment (ASC_* variables etc.).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import anyio
from mcp import Client
from mcp.client.stdio import StdioServerParameters


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tool", nargs="?")
    parser.add_argument("arguments", nargs="?", default="{}")
    parser.add_argument("--list", action="store_true", help="list tools with their annotations")
    args = parser.parse_args()

    params = StdioServerParameters(command=sys.executable, args=["-m", "release_guard"], env=dict(os.environ))
    async with Client(params) as client:
        if args.list or not args.tool:
            tools = (await client.list_tools()).tools
            for tool in tools:
                ann = tool.annotations
                print(f"{tool.name:34} read_only={ann.read_only_hint if ann else None!s:5} "
                      f"destructive={ann.destructive_hint if ann else None}")
            return 0
        result = await client.call_tool(args.tool, json.loads(args.arguments))
        if result.is_error:
            print(result.content[0].text if result.content else "error", file=sys.stderr)
            return 1
        print(json.dumps(result.structured_content, indent=2))
        return 0


if __name__ == "__main__":
    sys.exit(anyio.run(main))
