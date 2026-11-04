"""Read files out of DSH's app.asar without Electron.

The desktop build ships its packages inside ``resources/app.asar``. rg/glob/node
cannot open that container, but the format is simple: a Chromium pickle header
followed by concatenated file bytes, so plain Python can read any entry.

Usage:
    python scripts/asar_extract.py list <prefix>          # list entry paths
    python scripts/asar_extract.py cat <path> [outfile]   # print one file
    python scripts/asar_extract.py grep <pattern> <prefix># regex over files under prefix
"""

from __future__ import annotations

import json
import re
import struct
import sys
from pathlib import Path

ASAR = Path(
    r"C:\Users\<you>\AppData\Local\Programs\DeepSeek Harness\resources\app.asar"
)

_HEADER: dict | None = None
_DATA_START = 0


def _load_header() -> dict:
    global _HEADER, _DATA_START
    if _HEADER is not None:
        return _HEADER
    with ASAR.open("rb") as handle:
        handle.read(4)  # uint32: size of the next field (always 4)
        pickle_size = struct.unpack("<I", handle.read(4))[0]
        handle.read(4)  # uint32: size of the next field
        json_len = struct.unpack("<I", handle.read(4))[0]
        _HEADER = json.loads(handle.read(json_len))
    _DATA_START = 8 + pickle_size
    return _HEADER


def _node(parts: list[str]) -> dict:
    node = _load_header()
    for part in parts:
        node = node["files"][part]
    return node


def read_file(entry: str) -> bytes:
    node = _node(entry.strip("/").split("/"))
    if "files" in node:
        raise IsADirectoryError(entry)
    with ASAR.open("rb") as handle:
        handle.seek(_DATA_START + int(node["offset"]))
        return handle.read(int(node["size"]))


def walk(entry: str = "") -> list[str]:
    out: list[str] = []

    def visit(node: dict, prefix: str) -> None:
        for name, child in node.get("files", {}).items():
            path = f"{prefix}/{name}" if prefix else name
            if "files" in child:
                visit(child, path)
            else:
                out.append(path)

    visit(_node(entry.strip("/").split("/")) if entry else _load_header(), entry.strip("/"))
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    command = argv[1]
    if command == "list":
        prefix = argv[2] if len(argv) > 2 else ""
        for path in walk(prefix):
            print(path)
        return 0
    if command == "cat":
        data = read_file(argv[2])
        if len(argv) > 3:
            Path(argv[3]).write_bytes(data)
            print(f"wrote {len(data)} bytes to {argv[3]}", file=sys.stderr)
        else:
            sys.stdout.buffer.write(data)
            print()
        return 0
    if command == "grep":
        pattern = re.compile(argv[2])
        prefix = argv[3] if len(argv) > 3 else ""
        for path in walk(prefix):
            if not path.endswith((".js", ".mjs", ".json", ".ts", ".tsx", ".map")):
                continue
            try:
                text = read_file(path).decode("utf-8", "replace")
            except Exception:
                continue
            for index, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    print(f"{path}:{index}: {line.strip()[:200]}")
        return 0
    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
