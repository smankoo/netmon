#!/usr/bin/env python3
"""Entry point for the netmon MCP server.

Running this file directly puts its own directory on sys.path, so the
`netmon_mcp` package imports without needing PYTHONPATH or a particular cwd -
which matters because an MCP host launches this from wherever it happens to be.

  # normal use: stdio MCP server (this is what Hermes runs)
  .venv/bin/python run_server.py

  # call a tool from the shell, for testing or a one-off report
  .venv/bin/python run_server.py --call netmon_summary
  .venv/bin/python run_server.py --call netmon_incident_report days=2 probe=imac
  .venv/bin/python run_server.py --list
"""

from __future__ import annotations

import asyncio
import json
import sys

from netmon_mcp import server


def _parse_value(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _cli(argv: list[str]) -> int:
    tools = {
        name: obj
        for name, obj in vars(server).items()
        if name.startswith("netmon_") and callable(obj)
    }

    if "--list" in argv:
        for name in sorted(tools):
            print(name)
        return 0

    index = argv.index("--call")
    if index + 1 >= len(argv):
        print("--call needs a tool name (see --list)", file=sys.stderr)
        return 2
    name = argv[index + 1]
    if name not in tools:
        print(f"unknown tool {name!r}; known: {', '.join(sorted(tools))}", file=sys.stderr)
        return 2

    kwargs = {}
    for arg in argv[index + 2:]:
        if "=" not in arg:
            print(f"expected key=value, got {arg!r}", file=sys.stderr)
            return 2
        key, _, raw = arg.partition("=")
        kwargs[key] = _parse_value(raw)

    result = asyncio.run(tools[name](**kwargs))
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    if "--call" in sys.argv or "--list" in sys.argv:
        raise SystemExit(_cli(sys.argv))
    server.main()
