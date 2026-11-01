#!/usr/bin/env python3
"""Builds the ClickHouse Cloud bundles: one zip per function, to upload as a Python executable UDF.

Each zip holds main.py (udf/drex_udf.py, unchanged), requirements.txt, and config.json with the function, the API key,
the base URL and the per-process concurrency. ClickHouse Cloud has no secrets store for UDFs yet, so the key ships
inside the zip: use a key made for this alone, keep the zips out of version control, and rotate the key when someone
who can read the service's UDFs leaves.

    NACE_API_KEY=... python3 cloud/build_bundles.py [--pool-size 2] [--output cloud/dist]

NACE_BASE_URL, when set, replaces https://console.nace.ai. With an OpenRouter URL the key is OPENROUTER_API_KEY when
set, else NACE_API_KEY, a `nace_` key is refused, and NACE_OPENROUTER_REFERER and NACE_OPENROUTER_TITLE, when set,
replace the app identity OpenRouter attributes requests to. The key is read from the environment, never from the
command line, which other users of the machine can see.
"""

import argparse
import json
import os
import stat
import urllib.parse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
#: drex_udf.py's functions (tests/test_cloud.py checks the two agree); this script needs nothing but Python
FUNCTIONS = ("noul", "choice", "score", "decide", "decide_image")
DEFAULT_BASE_URL = "https://console.nace.ai"

#: The budget of requests in flight per function and replica that a pool shares, as in the self-hosted config
BUDGET = 8
#: ClickHouse Cloud's sandbox allows each process 5 threads (RLIMIT_NPROC), the main one included, so a process sends
#: at most 4 requests at once
MAX_CONCURRENCY = 4
#: The fewest processes that send the whole budget, so a query reading one stream, which one process answers, gets
#: 4 requests in flight rather than 2 or fewer
DEFAULT_POOL_SIZE = BUDGET // MAX_CONCURRENCY
#: Fixed so that the same inputs build the same zip, and Terraform publishes a version only when they change
TIMESTAMP = (2026, 1, 1, 0, 0, 0)


def add(archive, name, data):
    info = zipfile.ZipInfo(name, TIMESTAMP)
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, data)


def is_open_router(base_url):
    host = (urllib.parse.urlsplit(base_url).hostname or "").lower()
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def build(output, api_key, base_url, pool_size, openrouter_referer="", openrouter_title=""):
    """The zips' paths. ClickHouse Cloud's sandbox reaches only public endpoints, so the URL is HTTPS."""
    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.password is not None:
        raise ValueError("NACE_BASE_URL must be an https:// URL without a user name or password")
    if is_open_router(base_url) and api_key.startswith("nace_"):
        raise ValueError("a Nace API key (nace_...) is never sent to OpenRouter; set OPENROUTER_API_KEY")
    concurrency = max(1, min(MAX_CONCURRENCY, BUDGET // pool_size))
    attribution = {}
    if openrouter_referer:
        attribution["openrouter_referer"] = openrouter_referer
    if openrouter_title:
        attribution["openrouter_title"] = openrouter_title
    output.mkdir(parents=True, exist_ok=True)
    script = (ROOT / "udf" / "drex_udf.py").read_bytes()
    requirements = (ROOT / "udf" / "requirements.txt").read_bytes()
    paths = []
    for function in FUNCTIONS:
        config = {
            "function": function,
            "api_key": api_key,
            "base_url": base_url,
            "concurrency": concurrency,
            **attribution,
        }
        path = output / f"drex_{function}.zip"
        # Readable by this user alone: the zip holds the key
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as file, zipfile.ZipFile(file, "w") as archive:
            add(archive, "main.py", script)
            add(archive, "requirements.txt", requirements)
            add(archive, "config.json", json.dumps(config, indent=2) + "\n")
        paths.append(path)
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pool-size", type=int, default=DEFAULT_POOL_SIZE, help="the pool_size you will set (2)")
    parser.add_argument("--output", type=Path, default=ROOT / "cloud" / "dist")
    args = parser.parse_args()
    base_url = os.environ.get("NACE_BASE_URL", "").strip() or DEFAULT_BASE_URL
    api_key = os.environ.get("NACE_API_KEY", "").strip()
    if is_open_router(base_url):
        api_key = os.environ.get("OPENROUTER_API_KEY", "").strip() or api_key
    if not api_key:
        parser.error("set NACE_API_KEY to the key the functions will use")
    if args.pool_size < 1:
        parser.error("--pool-size must be at least 1")
    try:
        paths = build(
            args.output,
            api_key,
            base_url,
            args.pool_size,
            os.environ.get("NACE_OPENROUTER_REFERER", "").strip(),
            os.environ.get("NACE_OPENROUTER_TITLE", "").strip(),
        )
    except ValueError as error:
        parser.error(str(error))
    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
