"""Writes test/mock/openapi.json: the schemas of Drex's openapi.json that the mock's responses are validated against,
with every schema they refer to.

    python3 test/mock/openapi_subset.py <path to drex's openapi.json> <drex commit>
"""

import json
import sys
from pathlib import Path

ROOTS = ("SystemOneRequest", "SystemOneResponse", "ModelsResponse", "ErrorEnvelope")
PREFIX = "#/components/schemas/"


def refs(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and value.startswith(PREFIX):
                yield value[len(PREFIX) :]
            else:
                yield from refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from refs(value)


def main():
    source, commit = sys.argv[1], sys.argv[2]
    schemas = json.loads(Path(source).read_text())["components"]["schemas"]
    kept, pending = {}, list(ROOTS)
    while pending:
        name = pending.pop()
        if name not in kept:
            kept[name] = schemas[name]
            pending.extend(refs(kept[name]))
    subset = {
        "info": {"description": f"A subset of openapi.json at nace-ai/drex {commit}, written by openapi_subset.py"},
        "components": {"schemas": dict(sorted(kept.items()))},
    }
    (Path(__file__).parent / "openapi.json").write_text(json.dumps(subset, indent=2) + "\n")


if __name__ == "__main__":
    main()
