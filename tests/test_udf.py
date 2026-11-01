"""drex_udf.py without ClickHouse: the script as a subprocess fed RowBinary blocks as ClickHouse sends them, against the
mock Drex API in this process, and the rules that are pure functions.

test_clickhouse.py covers the functions through ClickHouse; this covers what is awkward there: OpenRouter (through the
mock as an HTTPS proxy), TLS failures, the retry waits, and the Cloud configuration file.
"""

import io
import json
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "udf"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "mock"))
import drex_mock
import drex_udf
import pngs
from nace_sdk import NaceAPIError

SCRIPT = Path(drex_udf.__file__)
KEY = "nace_sk_mockKeyForDrexContractTests_222222222222222"
OPEN_ROUTER_KEY = "sk-or-v1-mockKeyForDrexContractTests"
#: Runs a script as ClickHouse Cloud's UDF sandbox would: it refuses Unix sockets, socketpair() included, and allows
#: 5 threads, the main one included (RLIMIT_NPROC)
CLOUD_SANDBOX = """
import resource, runpy, socket, sys, threading

def refused(*args, **kwargs):
    raise PermissionError(1, "Operation not permitted")

def getrlimit(which, _getrlimit=resource.getrlimit):
    return (5, 5) if which == resource.RLIMIT_NPROC else _getrlimit(which)

def limited(start):
    def start_thread(*args, **kwargs):
        # active_count() includes the thread being started
        if threading.active_count() > 5:
            raise RuntimeError("can't start new thread")
        return start(*args, **kwargs)
    return start_thread

resource.getrlimit = getrlimit
for name in ("_start_new_thread", "_start_joinable_thread"):
    if hasattr(threading, name):
        setattr(threading, name, limited(getattr(threading, name)))

class Socket(socket.socket):
    def __init__(self, family=-1, *args, **kwargs):
        if family == socket.AF_UNIX:
            refused()
        super().__init__(family, *args, **kwargs)

socket.socketpair = refused
socket.socket = Socket
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


@pytest.fixture(scope="module")
def mock():
    server = drex_mock.make_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "nace_api_key"
    path.write_text(KEY + "\n")
    return path


def requests(mock, ns):
    import urllib.request

    with urllib.request.urlopen(f"{mock}/__log/{ns}") as response:
        return [json.loads(line) for line in response.read().decode().splitlines()][1:]


def nullable(value):
    out = bytearray()
    if value is None:
        out.append(1)
    else:
        out.append(0)
        drex_udf.write_varint(out, len(value))
        out += value
    return bytes(out)


def strings(values):
    out = bytearray()
    drex_udf.write_varint(out, len(values))
    for value in values:
        drex_udf.write_varint(out, len(value))
        out += value
    return bytes(out)


def block(rows):
    """A RowBinary block with its chunk header; each row is a tuple of already encoded arguments."""
    return f"{len(rows)}\n".encode() + b"".join(b"".join(row) for row in rows)


def run(args, stdin, env=None, cwd=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], input=stdin, capture_output=True, env=env, cwd=cwd, timeout=120
    )


def floats(stdout):
    reader = drex_udf.Reader(io.BytesIO(stdout))
    results = []
    while reader.stream.tell() < len(stdout):
        results.append(None if reader.exact(1) == b"\x01" else struct.unpack("<d", reader.exact(8))[0])
    return results


def nullable_strings(stdout):
    reader = drex_udf.Reader(io.BytesIO(stdout))
    results = []
    while reader.stream.tell() < len(stdout):
        value = reader.nullable_string()
        results.append(None if value is None else value.decode())
    return results


def noul_row(text, instructions=b"q?", criteria=None, model=None):
    return (nullable(text), nullable(instructions), nullable(criteria), nullable(model))


def args(function, key_file, base_url, concurrency=2):
    return [
        *("--function", function, "--key-file", str(key_file)),
        *("--base-url", base_url, "--concurrency", str(concurrency)),
    ]


# -- the process -------------------------------------------------------------------------------------------------------


def test_answers_each_block_in_order_and_keeps_running(mock, key_file):
    stdin = block([noul_row(b"a"), noul_row(None), noul_row(b"b")]) + block([noul_row(b"c")])
    result = run(args("noul", key_file, f"{mock}/ns/blocks/ok"), stdin)
    assert result.returncode == 0, result.stderr
    assert floats(result.stdout) == [0.75, None, 0.75, 0.75]
    sent = [record for record in requests(mock, "blocks") if record["endpoint"] == "/v1/systemone"]
    states = [json.loads(record["state"]) for record in sent]
    # Rows within a block run concurrently; only the output and block boundaries are ordered.
    assert sorted(states[:2]) == ["a", "b"]
    assert states[2:] == ["c"]
    # The model list is read once per process
    assert [record["endpoint"] for record in requests(mock, "blocks")].count("/v1/models") == 1


def test_a_fatal_error_exits_1_with_the_message_on_stderr(mock, key_file):
    result = run(args("noul", key_file, f"{mock}/ns/fatal/401"), block([noul_row(b"a")]))
    assert result.returncode == 1
    assert result.stderr.decode().startswith("drex_noul: Invalid API key.")


def test_arguments_without_their_null_flag_fail_the_query(mock, key_file):
    """ClickHouse 26.6 sends a String passed to a Nullable(String) argument without the flag."""
    stdin = block([(nullable(b"text")[1:], nullable(b"q?"), nullable(None), nullable(None))])
    result = run(args("noul", key_file, f"{mock}/ns/flag/ok"), stdin)
    assert result.returncode == 1
    assert "need ClickHouse 26.7 or later" in result.stderr.decode()
    assert requests(mock, "flag") == []


def test_the_key_file_is_required(tmp_path, mock):
    result = run(args("noul", tmp_path / "missing", f"{mock}/ns/key/ok"), b"")
    assert result.returncode == 1
    assert "could not read the API key file" in result.stderr.decode()


@pytest.mark.parametrize(
    "base_url", ["http://console.nace.ai", "http://10.0.0.1:8787", "ftp://localhost", "https://user:pw@console.nace.ai"]
)
def test_base_url_must_be_https_outside_localhost(key_file, base_url):
    result = run(args("noul", key_file, base_url), b"")
    assert result.returncode == 1
    assert "base_url must" in result.stderr.decode()


def test_tls_failures_fail_at_once(mock, key_file):
    """The mock speaks plain HTTP, so an https:// URL to it fails the handshake, which no retry would fix."""
    started = time.monotonic()
    result = run(args("noul", key_file, mock.replace("http://", "https://")), block([noul_row(b"a")]))
    assert result.returncode == 1
    assert "could not reach Drex over TLS" in result.stderr.decode()
    assert time.monotonic() - started < 10


def test_cloud_reads_config_json_next_to_the_script(tmp_path, mock):
    """ClickHouse Cloud runs main.py with no arguments, in a sandbox without Unix sockets."""
    shutil.copy(SCRIPT, tmp_path / "main.py")
    config = {"function": "choice", "api_key": KEY, "base_url": f"{mock}/ns/cloud/ok", "concurrency": 4}
    (tmp_path / "config.json").write_text(json.dumps(config))
    row = (nullable(b"text"), nullable(b"q?"), strings([b"b", b"a"]), nullable(None))
    result = subprocess.run(
        [sys.executable, "-c", CLOUD_SANDBOX, str(tmp_path / "main.py")],
        input=block([row]),
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    reader = drex_udf.Reader(io.BytesIO(result.stdout))
    assert reader.nullable_string() == b"b"
    assert [record["x_nace_client"] for record in requests(mock, "cloud")] == ["drex-clickhouse/0.1.2"] * 2


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"function": "noul", "concurrency": 2}, "api_key is missing"),
        ({"function": "nope", "api_key": KEY, "concurrency": 2}, "function must be one of"),
        ({"function": "noul", "api_key": KEY, "concurrency": 0}, "concurrency must be an integer of at least 1"),
        ({"function": "noul", "api_key": KEY, "concurrency": 2, "base_url": "http://example.com"}, "https://"),
    ],
)
def test_cloud_config_is_checked(tmp_path, config, message):
    shutil.copy(SCRIPT, tmp_path / "main.py")
    (tmp_path / "config.json").write_text(json.dumps(config))
    result = subprocess.run([sys.executable, str(tmp_path / "main.py")], input=b"", capture_output=True, timeout=60)
    assert result.returncode == 1
    assert message in result.stderr.decode()


# -- OpenRouter --------------------------------------------------------------------------------------------------------


@pytest.fixture
def open_router(mock, tmp_path):
    """The environment that sends https://openrouter.ai through the mock, which answers as OpenRouter."""
    import urllib.request

    certificate = tmp_path / "openrouter.pem"
    with urllib.request.urlopen(f"{mock}/__fixtures/openrouter.pem") as response:
        certificate.write_bytes(response.read())
    key = tmp_path / "openrouter_key"
    key.write_text(OPEN_ROUTER_KEY)
    env = {**os.environ, "HTTPS_PROXY": mock, "SSL_CERT_FILE": str(certificate), "NO_PROXY": ""}
    return env, key


def test_open_router_takes_its_own_body_and_headers(mock, open_router):
    env, key = open_router
    questions = json.dumps({"colour": {"type": "choice", "criteria": ["red", "blue"]}}).encode()
    image_row = (nullable(b'"Which colour is Image 2?"'), strings([pngs.fixture("red"), pngs.fixture("blue")]))
    stdin = block([(*image_row, nullable(questions), nullable(None))])
    result = run(args("decide_image", key, "https://openrouter.ai/api/ns/router/ok"), stdin, env=env)
    assert result.returncode == 0, result.stderr
    (body,) = nullable_strings(result.stdout)
    assert json.loads(body)["answers"]["colour"]["choice"] == "red"
    models, decision = requests(mock, "router")
    assert models["open_router"] and models["endpoint"] == "/v1/models"
    assert decision["model"] == "nace-ai/drex-max"
    for record in (models, decision):
        assert (record["http_referer"], record["x_openrouter_title"]) == ("https://nace.ai", "Drex for ClickHouse")
    # The images as state content parts, and instructions always a string
    assert decision["image_colours"] == ["red", "blue"]
    assert json.loads(decision["body"])["state"][0] == {"type": "text", "text": "Which colour is Image 2?"}
    assert json.loads(decision["questions"])[0]["instructions"] == ""


def test_open_router_402_names_its_credits_page(open_router):
    env, key = open_router
    result = run(args("noul", key, "https://openrouter.ai/api/ns/router402/402"), block([noul_row(b"a")]), env=env)
    assert result.returncode == 1
    assert "Billing: https://openrouter.ai/settings/credits" in result.stderr.decode()


def test_open_router_attribution_is_set_by_the_caller(mock, open_router):
    env, key = open_router
    options = ["--openrouter-referer", "https://app.example", "--openrouter-title", "Example"]
    result = run(
        [*args("noul", key, "https://openrouter.ai/api/ns/router-app/ok"), *options], block([noul_row(b"a")]), env=env
    )
    assert result.returncode == 0, result.stderr
    sent = requests(mock, "router-app")
    assert len(sent) == 2
    assert {(r["http_referer"], r["x_openrouter_title"]) for r in sent} == {("https://app.example", "Example")}


def test_a_nace_host_gets_no_attribution_headers(mock, key_file):
    options = ["--openrouter-referer", "https://app.example", "--openrouter-title", "Example"]
    result = run([*args("noul", key_file, f"{mock}/ns/nace-host/ok"), *options], block([noul_row(b"a")]))
    assert result.returncode == 0, result.stderr
    assert {(r["http_referer"], r["x_openrouter_title"]) for r in requests(mock, "nace-host")} == {(None, None)}


def test_a_nace_key_is_never_sent_to_open_router(mock, key_file, open_router):
    env, _ = open_router
    result = run(
        args("noul", key_file, "https://openrouter.ai/api/ns/router-nace/ok"), block([noul_row(b"a")]), env=env
    )
    assert result.returncode == 1
    assert "OPENROUTER_API_KEY" in result.stderr.decode()
    assert KEY not in result.stderr.decode()
    assert requests(mock, "router-nace") == []


def test_cloud_config_sets_the_attribution_and_refuses_a_nace_key_for_open_router(tmp_path, mock):
    shutil.copy(SCRIPT, tmp_path / "main.py")
    config = {"function": "noul", "api_key": KEY, "base_url": "https://openrouter.ai/api", "concurrency": 1}
    (tmp_path / "config.json").write_text(json.dumps({**config, "openrouter_title": "Example"}))
    result = subprocess.run([sys.executable, str(tmp_path / "main.py")], input=b"", capture_output=True, timeout=60)
    assert result.returncode == 1
    assert "never sent to OpenRouter" in result.stderr.decode()
    (tmp_path / "config.json").write_text(json.dumps({**config, "api_key": OPEN_ROUTER_KEY, "openrouter_title": 7}))
    result = subprocess.run([sys.executable, str(tmp_path / "main.py")], input=b"", capture_output=True, timeout=60)
    assert "openrouter_title must be a string" in result.stderr.decode()


def test_an_open_router_key_is_accepted_and_sent(mock, open_router):
    env, key = open_router
    result = run(args("noul", key, "https://openrouter.ai/api/ns/router-key/ok"), block([noul_row(b"a")]), env=env)
    assert result.returncode == 0, result.stderr
    sent = requests(mock, "router-key")
    assert {r["authorization_header"] for r in sent} == {f"Bearer {OPEN_ROUTER_KEY}"}


# -- the rules ---------------------------------------------------------------------------------------------------------


def api_error(status, headers=None):
    import httpx

    return NaceAPIError(status, {}, httpx.Headers(headers or {}))


def test_retry_after_is_honoured_and_capped_at_30_seconds():
    assert drex_udf.retry_wait(api_error(429, {"retry-after-ms": "1500"}), 1) == 1.5
    assert drex_udf.retry_wait(api_error(429, {"retry-after": "3"}), 1) == 3.0
    assert drex_udf.retry_wait(api_error(529, {"retry-after-ms": "600000"}), 1) == 30.0


@pytest.mark.parametrize(("retry", "backoff"), [(1, 1), (2, 2), (3, 4), (4, 8)])
def test_backoff_without_retry_after_is_1_2_4_8_less_jitter(retry, backoff):
    waits = [drex_udf.retry_wait(api_error(503), retry) for _ in range(200)]
    assert all(0.75 * backoff <= wait <= backoff for wait in waits)


def test_a_due_retry_takes_the_next_slot_before_new_requests():
    slots = drex_udf.Slots(1)
    slots.acquire(retry=False)
    order = []

    def take(name, retry):
        slots.acquire(retry)
        order.append(name)
        slots.release()

    waiting = [threading.Thread(target=take, args=("new", False)), threading.Thread(target=take, args=("retry", True))]
    for thread in waiting:
        thread.start()
    while sum(map(len, slots.waiting)) < 2:
        time.sleep(0.001)
    slots.release()
    for thread in waiting:
        thread.join(5)
    assert order == ["retry", "new"]


@pytest.mark.parametrize(
    ("issues", "message", "row"),
    [
        ([{"path": "state", "message": "too long"}], "Invalid", True),
        ([{"path": "images.0", "message": "bad"}, {"path": "state.a", "message": "bad"}], "Invalid", True),
        ([], "Request too long: 300000 tokens", True),
        ([{"path": "", "message": "Request too long"}], "Request too long: x", True),
        ([{"path": "questions.a", "message": "bad"}, {"path": "state", "message": "bad"}], "Invalid", False),
        ([{"path": "model", "message": "unknown"}], "Invalid", False),
        ([], "Invalid", False),
    ],
)
def test_which_422s_are_about_the_row(issues, message, row):
    import httpx

    error = NaceAPIError(
        422, {"error": {"type": "invalid_request_error", "message": message, "issues": issues}}, httpx.Headers()
    )
    assert drex_udf.refuses_row(error) is row


def test_a_row_refusal_never_names_the_rows_keys():
    import httpx

    body = {"error": {"message": "Invalid", "issues": [{"path": "state.customer_ssn", "message": "too long"}]}}
    assert drex_udf.describe(NaceAPIError(422, body, httpx.Headers())) == "HTTP 422: state: too long"
