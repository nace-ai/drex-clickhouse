#!/usr/bin/env python3
"""Writes the test functions: each function of config/drex_function.xml again, once per mock scenario.

`t_<scenario>_<function>` (for example `t_429_once_drex_noul`) is the shipped function with its name changed and
`--base-url` pointing at the mock's scenario, under the namespace of the same name. Everything else (types, format,
pool and timeouts) is the shipped config's, so the tests run what users install. `t_cap_*` also sets the per-process
concurrency, for the test of the cap across a pool.

Run: python3 tests/clickhouse_functions.py tests/.generated/drex_test_function.xml
"""

import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path

SHIPPED = Path(__file__).resolve().parent.parent / "config" / "drex_function.xml"
#: The mock, which shares the ClickHouse container's network (docker-compose.test.yml)
MOCK_URL = "http://127.0.0.1:8787"
SCENARIOS = ["ok", "401", "402", "422", "429-once", "529-once", "529-always", "too-long", "slow-300"]
#: The cap test: a pool of 2 processes with 3 requests in flight each, against slow answers
CAP_POOL_SIZE = 2
CAP_CONCURRENCY = 3


def prefix(scenario):
    return "t_" + scenario.replace("-", "_") + "_"


def variants():
    shipped = ElementTree.parse(SHIPPED).getroot()
    root = ElementTree.Element("functions")
    for scenario in [*SCENARIOS, "cap"]:
        for function in shipped.iter("function"):
            copy = ElementTree.fromstring(ElementTree.tostring(function))
            name = copy.find("name")
            name.text = prefix(scenario) + name.text
            command = copy.find("command")
            mock_scenario = "slow-300" if scenario == "cap" else scenario
            command.text += f" --base-url {MOCK_URL}/ns/{scenario}/{mock_scenario}"
            if scenario == "cap":
                command.text = command.text.replace("--concurrency 4", f"--concurrency {CAP_CONCURRENCY}")
                copy.find("pool_size").text = str(CAP_POOL_SIZE)
            root.append(copy)
    ElementTree.indent(root)
    return ElementTree.tostring(root, encoding="unicode") + "\n"


if __name__ == "__main__":
    target = Path(sys.argv[1])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(variants())
