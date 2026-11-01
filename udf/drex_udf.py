#!/usr/bin/env python3
"""Drex for ClickHouse: the executable UDF behind drex_noul, drex_choice, drex_score, drex_decide and drex_decide_image.

ClickHouse runs this script as an `executable_pool` UDF with `format RowBinary` and `send_chunk_header 1`: for each
block it writes the row count as a line of text, then the rows, and reads back exactly that many results, in order.
One process answers one block at a time and stays up for the next.

Each row is one request to Drex's POST /v1/systemone, made with nace-sdk. A block's requests run concurrently on
threads, at most `concurrency` at a time in this process, and each result is written as soon as every row before it is
written, so ClickHouse never waits longer between two rows than one request takes. Threads, not asyncio: ClickHouse
Cloud's sandbox refuses Unix sockets, and asyncio's event loop can't start without socketpair(). The sandbox also
allows each process only 5 threads (RLIMIT_NPROC), so at most 4 requests are in flight per process there.

Errors follow drex-duckdb's rules:

- Fail the query (the process writes the message to stderr and exits 1): 401, 402, a 422 that isn't about the row
  alone, an unknown model, a TLS failure, and every check made before sending (labels, questions, `__proto__`, images
  on a model that doesn't read them). ClickHouse then fails the query with UDF_EXECUTION_FAILED, quoting stderr.
- NULL with a warning on stderr: a 422 about the row (`Request too long:`, or issues only under `state` or `images`),
  a body over 4 MiB, an image that isn't a PNG, JPEG or WebP, retries exhausted, and any other status.
- Retry, at most 5 attempts within 120 s of the first: no response, 408, 429, 500, 502, 503, 504 and 529, after
  `retry-after-ms` (or `retry-after`), capped at 30 s, else 1, 2, 4 and 8 s less up to 25 % jitter.

Configuration comes from the command line (self-hosted) or, with no arguments, from config.json next to this file
(ClickHouse Cloud, which runs the script without arguments). The API key is never a function argument. On OpenRouter,
`--openrouter-referer` and `--openrouter-title` (`openrouter_referer` and `openrouter_title` in config.json) replace the
app identity sent as `HTTP-Referer` and `X-OpenRouter-Title`, and a `nace_` key is refused.
"""

import argparse
import collections
import concurrent.futures
import json
import math
import os
import queue
import random
import resource
import ssl
import struct
import sys
import threading
import time
import urllib.parse

import httpx
import pydantic
import pydantic_core
from nace_sdk import (
    NaceAPIConnectionError,
    NaceAPIError,
    NaceAPIResponseValidationError,
    NaceAPITimeoutError,
    NaceClient,
    NaceError,
    RetryPolicy,
    image_input,
)

__version__ = "0.1.2"
CLIENT_NAME = f"drex-clickhouse/{__version__}"

DEFAULT_BASE_URL = "https://console.nace.ai"
DEFAULT_MODEL = "drex-v2.0"
DEFAULT_IMAGE_MODEL = "drex-max"
#: The versions that read images. Drex's GET /v1/models has no image field; a listed model reads images when it is
#: one of these or its `alias_for` names one. OpenRouter lists each model's input modalities instead
IMAGE_MODELS = ("drex-max", "drex-max-v1.1")

MAX_QUESTIONS = 512
MIN_LABELS = 2
MAX_LABELS = 255
MAX_IMAGES = 8
#: Drex refuses a larger body with a 413
MAX_BODY_BYTES = 4 * 1024 * 1024

MAX_ATTEMPTS = 5
#: From a request's first attempt; a retry that would start after it is not made
REQUEST_BUDGET_SECONDS = 120.0
MAX_RETRY_WAIT_SECONDS = 30.0
ATTEMPT_TIMEOUT_SECONDS = 60.0
RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 529})

OPENROUTER_REFERER = "https://nace.ai"
OPENROUTER_TITLE = "Drex for ClickHouse"
OPENROUTER_BILLING_URL = "https://openrouter.ai/settings/credits"

#: The id of the one question the typed functions ask
ANSWER_ID = "answer"

#: Retries are this script's own (see `send`), so the SDK makes one attempt per call
NO_RETRY = RetryPolicy(max_retries=0)


class Fatal(Exception):
    """Fails the query: the message goes to stderr and the process exits 1."""


class Lost(Exception):
    """Makes the row NULL, with a warning on stderr that never holds the row's content."""


class Body(pydantic.RootModel[dict]):
    """Drex's response body as it came, for `drex_decide` and the typed answers alike."""


# ---------------------------------------------------------------------------------------------------------------------
# RowBinary
# ---------------------------------------------------------------------------------------------------------------------


class Reader:
    """RowBinary values from ClickHouse, read from a binary stream."""

    def __init__(self, stream):
        self.stream = stream

    def exact(self, size):
        data = self.stream.read(size)
        if len(data) != size:
            raise EOFError("ClickHouse closed the input in the middle of a row")
        return data

    def varint(self):
        result = 0
        shift = 0
        while True:
            byte = self.exact(1)[0]
            result |= (byte & 0x7F) << shift
            if byte < 0x80:
                return result
            shift += 7
            if shift > 63:
                raise ValueError("a RowBinary length is longer than 64 bits")

    def string(self):
        return self.exact(self.varint())

    def nullable_string(self):
        flag = self.exact(1)[0]
        if flag > 1:
            # ClickHouse before 26.7 sends each argument in the type it has, not the declared Nullable(String), so a
            # String comes without its null flag. An empty or one-byte one can't be told from a flag
            raise Fatal("an argument came without its null flag; Drex's functions need ClickHouse 26.7 or later")
        return None if flag else self.string()

    def strings(self):
        return [self.string() for _ in range(self.varint())]


def write_varint(out, value):
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)


def write_string(out, value):
    data = value.encode("utf-8")
    write_varint(out, len(data))
    out += data


def write_nullable_string(out, value):
    if value is None:
        out.append(1)
    else:
        out.append(0)
        write_string(out, value)


def write_float(out, value):
    out += struct.pack("<d", value)


def write_nullable_float(out, value):
    if value is None:
        out.append(1)
    else:
        out.append(0)
        write_float(out, value)


# ---------------------------------------------------------------------------------------------------------------------
# Questions: drex-duckdb's rules for criteria, labels and questions
# ---------------------------------------------------------------------------------------------------------------------


class Label:
    __slots__ = ("description", "label")

    def __init__(self, label, description=None):
        self.label = label
        #: None when the label has no description
        self.description = description


class Question:
    __slots__ = ("id", "instructions", "labels", "labels_from_object", "type")

    def __init__(self, id, type, instructions="", labels=(), labels_from_object=False):
        self.id = id
        self.type = type
        self.instructions = instructions
        self.labels = list(labels)
        #: Whether the labels were written as Drex's object of label to description, where a noul may describe one
        self.labels_from_object = labels_from_object

    def wire(self):
        """The question as Drex takes it, with `instructions` always a string."""
        result = {"type": self.type, "instructions": self.instructions}
        if self.type == "score":
            result["criteria"] = [label.label for label in self.labels]
        elif self.type == "choice":
            result["criteria"] = {label.label: label.description for label in self.labels}
        elif any(label.description is not None for label in self.labels):
            # A noul's labels alone are the same as no criteria
            result["criteria"] = {label.label: label.description for label in self.labels}
        return result


def _reject_constant(name):
    raise ValueError(f"{name} is not valid JSON")


def parse_json(text, what):
    """`text` as JSON, refusing NaN and Infinity, which Drex can't read, and duplicate object keys."""

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Fatal(f"{what} has the key '{key}' twice")
            result[key] = value
        return result

    try:
        return json.loads(text, parse_constant=_reject_constant, object_pairs_hook=pairs)
    except ValueError as error:
        raise Fatal(f"{what} is not valid JSON: {error}") from None


def labels_from_json(name, value, allow_object):
    """drex-duckdb's criteria forms: a list of labels, a list of {label, description}, or (where `allow_object`) an
    object of label to description, whose description may be null."""

    def shape_error():
        forms = "a JSON array of labels or of {label, description} objects"
        return Fatal(f"{name} must be {forms}" + (", or an object of label to description" if allow_object else ""))

    def text_or_none(value):
        if value is None:
            return None
        if not isinstance(value, str):
            raise shape_error()
        return value

    def label(value, description=None):
        if value is None:
            raise Fatal(f"{name} labels must not be null")
        return Label(value, description)

    if isinstance(value, list):
        labels = []
        for element in value:
            if isinstance(element, dict):
                if not set(element) <= {"label", "description"}:
                    raise shape_error()
                labels.append(label(text_or_none(element.get("label")), text_or_none(element.get("description"))))
            else:
                labels.append(label(text_or_none(element)))
        return labels
    if isinstance(value, dict) and allow_object:
        return [Label(key, text_or_none(description)) for key, description in value.items()]
    raise shape_error()


def check_labels(name, question):
    """Drex's and drex-duckdb's rules for a question's labels."""
    labels = question.labels
    if question.type == "noul":
        # No labels is a noul without criteria
        found = [label.label for label in labels]
        valid = all(label in ("true", "false") for label in found) and len(set(found)) == len(found)
        complete = not labels or question.labels_from_object or set(found) == {"true", "false"}
        if not (valid and complete):
            raise Fatal(f"{name} labels must be exactly true and false, got [{', '.join(found)}]")
        return
    kind = "labels" if question.type == "choice" else "levels"
    if not MIN_LABELS <= len(labels) <= MAX_LABELS:
        raise Fatal(f"{name} takes {MIN_LABELS} to {MAX_LABELS} {kind}, got {len(labels)}")
    seen = set()
    for label in labels:
        if label.label == "":
            raise Fatal(f"{name} must not hold an empty {kind[:-1]}")
        if label.label in seen:
            raise Fatal(f"{name} must be unique; '{label.label}' is a duplicate")
        seen.add(label.label)
        if question.type == "score" and label.description is not None:
            raise Fatal("score levels take no description; describe them in instructions")


def check_noul_has_text(name, question):
    """What Drex refuses in a noul: neither instructions nor a description of true or false."""
    if question.type != "noul" or question.instructions:
        return
    if not any(label.description for label in question.labels):
        raise Fatal(f"{name} needs instructions, or criteria that describe true or false")


def check_question(name, question, label_name=None):
    """`label_name` names the labels' argument when it isn't the question itself, as for the typed functions."""
    check_labels(label_name or name, question)
    check_noul_has_text(name, question)


def questions_from_json(text):
    """`drex_decide`'s questions: a JSON object of id to {type, instructions, criteria}."""
    root = parse_json(text, "questions")
    if not isinstance(root, dict):
        raise Fatal("questions must be a JSON object of question id to question")
    if not 1 <= len(root) <= MAX_QUESTIONS:
        raise Fatal(f"questions takes 1 to {MAX_QUESTIONS} questions, got {len(root)}")
    seen = {}
    questions = []
    for question_id, entry in root.items():
        name = f"questions.{question_id}"
        if question_id.strip() == "":
            raise Fatal("question ids must not be empty")
        if question_id == "__proto__":
            # Drex answers it with nothing (`answers[id] = ...` on a plain object) and bills the request
            raise Fatal("the question id '__proto__' is not supported; Drex drops its answer")
        folded = question_id.casefold()
        if folded in seen:
            raise Fatal(f"question ids must be unique ignoring case; '{seen[folded]}' and '{question_id}' are not")
        seen[folded] = question_id
        if not isinstance(entry, dict):
            raise Fatal(f"{name} must be an object with type, instructions and criteria")
        for key in entry:
            if key not in ("type", "instructions", "criteria"):
                raise Fatal(f"{name} has an unknown key '{key}'; use type, instructions and criteria")
        question_type = entry.get("type")
        if question_type in ("multiple_choice", "box"):
            raise Fatal(
                f"{name}: {question_type} questions are not supported, since Drex has no {question_type} question"
            )
        if question_type not in ("noul", "choice", "score"):
            raise Fatal(f"{name}.type must be noul, choice or score")
        instructions = entry.get("instructions")
        if instructions is not None and not isinstance(instructions, str):
            raise Fatal(f"{name}.instructions must be a string")
        question = Question(question_id, question_type, instructions or "")
        criteria = entry.get("criteria")
        if criteria is not None:
            question.labels = labels_from_json(f"{name}.criteria", criteria, question_type != "score")
            question.labels_from_object = isinstance(criteria, dict)
        elif question_type != "noul":
            raise Fatal(f"{name} needs criteria")
        check_question(name, question)
        questions.append(question)
    return questions


# ---------------------------------------------------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------------------------------------------------


class Config:
    def __init__(self, function, api_key, base_url, concurrency, openrouter_referer="", openrouter_title=""):
        self.function = function
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.concurrency = concurrency
        self.open_router = is_open_router(self.base_url)
        if self.open_router and api_key.startswith("nace_"):
            raise Fatal(
                "a Nace API key (nace_...) is never sent to OpenRouter; use an OpenRouter key in the key file "
                "(OPENROUTER_API_KEY when building the ClickHouse Cloud bundles)"
            )
        self.openrouter_referer = openrouter_referer.strip() or OPENROUTER_REFERER
        self.openrouter_title = openrouter_title.strip() or OPENROUTER_TITLE


def is_open_router(base_url):
    host = (urllib.parse.urlsplit(base_url).hostname or "").lower()
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def check_base_url(base_url):
    """HTTPS, except to this machine: the URL receives the API key and every row sent."""
    parts = urllib.parse.urlsplit(base_url)
    if parts.username is not None or parts.password is not None:
        raise Fatal("base_url must not contain a user name or password")
    if parts.scheme == "https" and parts.hostname:
        return
    if parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1"):
        return
    raise Fatal("base_url must use https:// outside localhost")


def check_concurrency(concurrency):
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency < 1:
        raise Fatal("concurrency must be an integer of at least 1")
    return concurrency


def config_from_args(argv):
    parser = argparse.ArgumentParser(prog="drex_udf.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--function", required=True, choices=sorted(FUNCTIONS))
    parser.add_argument(
        "--key-file", required=True, help="a file holding the Nace API key, readable only by the clickhouse user"
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--concurrency", required=True, type=int, help="this process's cap on requests in flight")
    parser.add_argument("--openrouter-referer", default="", help=f"HTTP-Referer on OpenRouter, {OPENROUTER_REFERER}")
    parser.add_argument("--openrouter-title", default="", help=f"X-OpenRouter-Title on OpenRouter, {OPENROUTER_TITLE}")
    args = parser.parse_args(argv)
    try:
        with open(args.key_file, encoding="utf-8") as file:
            api_key = file.read().strip()
    except OSError as error:
        raise Fatal(f"could not read the API key file {args.key_file}: {error.strerror}") from None
    if not api_key:
        raise Fatal(f"the API key file {args.key_file} is empty")
    check_base_url(args.base_url)
    return Config(
        args.function,
        api_key,
        args.base_url,
        check_concurrency(args.concurrency),
        args.openrouter_referer,
        args.openrouter_title,
    )


def config_from_file(path):
    """ClickHouse Cloud's: {"function", "api_key", "base_url", "concurrency"} and optionally "openrouter_referer" and
    "openrouter_title" in config.json next to the script."""
    try:
        with open(path, encoding="utf-8") as file:
            values = json.load(file)
    except OSError as error:
        raise Fatal(f"could not read {path}: {error.strerror}") from None
    except ValueError as error:
        raise Fatal(f"{path} is not valid JSON: {error}") from None
    if not isinstance(values, dict):
        raise Fatal(f"{path} must hold a JSON object")
    function = values.get("function")
    if function not in FUNCTIONS:
        raise Fatal(f"{path}: function must be one of {', '.join(sorted(FUNCTIONS))}")
    api_key = values.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        raise Fatal(f"{path}: api_key is missing")
    base_url = values.get("base_url") or DEFAULT_BASE_URL
    if not isinstance(base_url, str):
        raise Fatal(f"{path}: base_url must be a string")
    check_base_url(base_url)
    app = {}
    for name in ("openrouter_referer", "openrouter_title"):
        app[name] = values.get(name) or ""
        if not isinstance(app[name], str):
            raise Fatal(f"{path}: {name} must be a string")
    return Config(function, api_key.strip(), base_url, check_concurrency(values.get("concurrency")), **app)


# ---------------------------------------------------------------------------------------------------------------------
# Talking to Drex
# ---------------------------------------------------------------------------------------------------------------------


def issue_text(path, message):
    return f"{path}: {message}" if path else message


def is_under(path, field):
    return path == field or path.startswith((field + ".", field + "["))


def refuses_row(error):
    """Whether a 422 is about the row rather than the query's constants: a request over the token limit (Drex's
    `Request too long:`), or issues only under `state` and `images`."""
    if (error.message or "").startswith("Request too long:"):
        return True
    paths = [issue.get("path") or "" for issue in error.issues]
    return bool(paths) and all(is_under(path, "state") or is_under(path, "images") for path in paths)


def request_suffix(error):
    return f" (request {error.request_id})" if error.request_id else ""


def describe(error):
    """A failure as a warning: Drex's message, or for a 422 about the row its issues with every path under `state`
    cut to `state`, since a deeper path names the row's own JSON keys."""
    if isinstance(error, NaceAPIConnectionError):
        return "no response" if isinstance(error, NaceAPITimeoutError) else str(error)
    if error.status == 422:
        issues = []
        for issue in error.issues:
            path = issue.get("path") or ""
            issues.append(issue_text("state" if is_under(path, "state") else path, issue.get("message") or ""))
        reason = "; ".join(issues) if issues else ("Request too long" if refuses_row(error) else "")
    else:
        reason = error.message or ""
    return f"HTTP {error.status}" + (f": {reason}" if reason else "") + request_suffix(error)


def is_tls_failure(error):
    """Whether no response came because TLS failed, in the handshake or on the certificate, which no retry fixes."""
    seen = error
    while seen is not None:
        if isinstance(seen, ssl.SSLError):
            return True
        text = str(seen).lower()
        if "ssl" in text or "certificate" in text:
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def raise_if_fatal(error, config):
    """Raises Fatal for the answers that fail the query: 401, 402, a 422 not about the row, and a TLS failure."""
    if isinstance(error, NaceAPIConnectionError):
        if not isinstance(error, NaceAPITimeoutError) and is_tls_failure(error):
            raise Fatal(f"could not reach Drex over TLS: {error}") from None
        return
    request = request_suffix(error)
    if error.status == 401:
        raise Fatal((error.message or "the API key was refused (HTTP 401)") + request) from None
    if error.status == 402:
        billing = OPENROUTER_BILLING_URL if config.open_router else config.base_url + "/dashboard/billing"
        message = error.message or "the account has no funds left (HTTP 402)."
        raise Fatal(f"{message} Billing: {billing}{request}") from None
    if error.status == 422 and not refuses_row(error):
        issues = "; ".join(issue_text(issue.get("path") or "", issue.get("message") or "") for issue in error.issues)
        raise Fatal(f"Drex refused the request: {issues or error.message or 'HTTP 422'}{request}") from None


def is_retryable(error):
    return isinstance(error, NaceAPIConnectionError) or error.status in RETRYABLE_STATUSES


def retry_wait(error, retry):
    """Seconds before retry number `retry` (1-based): what the response asked for, capped, else 1, 2, 4 and 8 s less
    up to 25 % jitter."""
    if isinstance(error, NaceAPIError):
        for header, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
            value = error.headers.get(header)
            if value is None:
                continue
            try:
                seconds = float(value.strip()) * scale
            except ValueError:
                continue
            if math.isfinite(seconds) and seconds >= 0:
                return min(seconds, MAX_RETRY_WAIT_SECONDS)
    backoff = float(1 << min(retry - 1, 3))
    return min(backoff - random.uniform(0, backoff / 4), MAX_RETRY_WAIT_SECONDS)


class Slots:
    """At most `size` requests in flight. A retry that comes due goes before requests not yet sent, so a row waiting
    to retry holds no slot and is not queued behind the rest of its block."""

    def __init__(self, size):
        self.free = size
        self.waiting = (collections.deque(), collections.deque())  # retries, first attempts
        self.lock = threading.Lock()

    def acquire(self, retry):
        with self.lock:
            if self.free and not any(self.waiting):
                self.free -= 1
                return
            waiter = threading.Event()
            self.waiting[0 if retry else 1].append(waiter)
        # release() hands its slot straight to this waiter
        waiter.wait()

    def release(self):
        with self.lock:
            for waiting in self.waiting:
                if waiting:
                    waiting.popleft().set()
                    return
            self.free += 1


class Drex:
    """One process's client: the SDK, the model list, and the cap on requests in flight."""

    def __init__(self, config):
        self.config = config
        self.slots = Slots(config.concurrency)
        headers = self.open_router_headers() or None
        # One connection pool for the SDK and the model list, with room for every request in flight
        limits = httpx.Limits(max_connections=config.concurrency + 1, max_keepalive_connections=config.concurrency + 1)
        self.http = httpx.Client(limits=limits, timeout=ATTEMPT_TIMEOUT_SECONDS)
        self.client = NaceClient(
            api_key=config.api_key,
            base_url=config.base_url,
            client_info=CLIENT_NAME,
            headers=headers,
            http_client=self.http,
            retry=NO_RETRY,
            timeout=ATTEMPT_TIMEOUT_SECONDS,
        )
        #: name the request sends -> whether it reads images; None until listed
        self.models = None

    def open_router_headers(self):
        """The app identity OpenRouter attributes requests to; none for a Nace host."""
        if not self.config.open_router:
            return {}
        return {"HTTP-Referer": self.config.openrouter_referer, "X-OpenRouter-Title": self.config.openrouter_title}

    def close(self):
        self.client.close()

    def send(self, call):
        """`call()` with this script's retries; Lost when it never answered, Fatal when its answer fails the query."""
        started = time.monotonic()
        attempt = 0
        while True:
            attempt += 1
            self.slots.acquire(retry=attempt > 1)
            try:
                return call()
            except NaceAPIResponseValidationError:
                raise Fatal("Drex answered with an unexpected body") from None
            except (NaceAPIError, NaceAPIConnectionError) as error:
                failure = error
            except NaceError as error:
                raise Fatal(str(error)) from None
            finally:
                self.slots.release()
            raise_if_fatal(failure, self.config)
            if not is_retryable(failure):
                raise Lost(describe(failure)) from None
            wait = retry_wait(failure, attempt)
            if attempt >= MAX_ATTEMPTS or time.monotonic() + wait > started + REQUEST_BUDGET_SECONDS:
                raise Lost(f"no answer after {attempt} attempts, the last {describe(failure)}") from None
            time.sleep(wait)

    # -- models ------------------------------------------------------------------------------------------------------

    def routed(self, model):
        """The name a request for `model` is sent as: on OpenRouter, a name without an owner is under nace-ai/."""
        return f"nace-ai/{model}" if self.config.open_router and "/" not in model else model

    def list_models(self):
        url = self.config.base_url + "/v1/models"
        if self.config.open_router:
            # OpenRouter lists only text models unless asked for others
            url += "?output_modalities=decisions"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Accept": "application/json",
            "User-Agent": CLIENT_NAME,
            "X-Nace-Client": CLIENT_NAME,
            **self.open_router_headers(),
        }

        def call():
            try:
                response = self.http.get(url, headers=headers)
            except httpx.TimeoutException:
                raise NaceAPITimeoutError(ATTEMPT_TIMEOUT_SECONDS) from None
            except httpx.RequestError as error:
                raise NaceAPIConnectionError(f"Connection error: {error}") from error
            if response.status_code != 200:
                try:
                    body = response.json()
                except ValueError:
                    body = response.text
                message = body.get("error") if isinstance(body, dict) else None
                message = message.get("message") if isinstance(message, dict) else message
                raise NaceAPIError(
                    response.status_code, body, response.headers, message if isinstance(message, str) else None
                )
            return response

        try:
            response = self.send(call)
        except Lost as error:
            raise Fatal(f"could not list Drex's models: {error}") from None
        try:
            root = response.json()
        except ValueError:
            root = None
        models = {}
        if isinstance(root, dict) and isinstance(root.get("models"), list):
            # Drex's: {"models": [{"name", "alias_for"}]}
            for entry in root["models"]:
                if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                    raise Fatal("GET /v1/models answered with an unexpected body")
                served_by = entry["alias_for"] if isinstance(entry.get("alias_for"), str) else entry["name"]
                models[entry["name"]] = served_by in IMAGE_MODELS
        elif isinstance(root, dict) and isinstance(root.get("data"), list):
            # OpenRouter's: {"data": [{"id", "architecture": {"input_modalities"}}]}, of which only Nace's are Drex
            for entry in root["data"]:
                if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                    raise Fatal("GET /v1/models answered with an unexpected body")
                if entry["id"].startswith("nace-ai/"):
                    architecture = entry.get("architecture")
                    modalities = architecture.get("input_modalities") if isinstance(architecture, dict) else None
                    models[entry["id"]] = isinstance(modalities, list) and "image" in modalities
        else:
            raise Fatal("GET /v1/models answered with an unexpected body")
        self.models = models

    def check_model(self, model, images):
        """Refuses, before any row of the block is sent, a model GET /v1/models doesn't list, and, when `images`, a
        model that doesn't read them. The list is kept for the process's life and fetched again for a model it lacks,
        so a model released since is found."""
        name = self.routed(model)
        if self.models is None or name not in self.models:
            self.list_models()
        if name not in self.models:
            raise Fatal(f"unknown model '{model}'; GET /v1/models lists {', '.join(self.models)}")
        if images and not self.models[name]:
            raise Fatal(f"{model} does not read images; use {DEFAULT_IMAGE_MODEL}")

    # -- decisions ---------------------------------------------------------------------------------------------------

    def decide(self, state, questions, model, images=None):
        """Drex's response body for one row. `images` are data URLs."""
        if self.config.open_router:
            if images:
                text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
                state = [{"type": "text", "text": text}] + [
                    {"type": "image_url", "image_url": {"url": url}} for url in images
                ]
            images = None
        body = {"state": state, "model": self.routed(model), "questions": questions}
        if images:
            body["images"] = images
        size = len(pydantic_core.to_json(body))
        if size > MAX_BODY_BYTES:
            raise Lost(f"the request body is {size} bytes, over Drex's {MAX_BODY_BYTES} bytes")
        response = self.send(
            lambda: self.client.system_one(state, questions, model=body["model"], images=images, response_model=Body)
        )
        return response.root


# ---------------------------------------------------------------------------------------------------------------------
# The five functions
# ---------------------------------------------------------------------------------------------------------------------


def text(value, what):
    """A constant argument as text; '' when NULL."""
    if value is None:
        return ""
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        raise Fatal(f"{what} is not valid UTF-8") from None


def model_name(value, default):
    return text(value, "model").strip() or default


def answer_of(body, question):
    answers = body.get("answers")
    answer = answers.get(question.id) if isinstance(answers, dict) else None
    if not isinstance(answer, dict):
        raise Fatal(f"Drex's response has no answer to question '{question.id}'")
    return answer


def number(values, key, question):
    """The number under `key` in `values`, an answer or its probabilities."""
    value = values.get(key) if isinstance(values, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Fatal(f"Drex's answer to question '{question.id}' has no number {key}")
    return float(value)


class Function:
    """One UDF: how to read a row's arguments, check a block's constants, ask Drex, and write a result."""

    name = ""

    def read(self, reader):
        raise NotImplementedError

    def null_result(self, out):
        raise NotImplementedError

    def prepare(self, drex, rows):
        """Checks each distinct constant of the block once, before any request; any failure fails the query."""
        raise NotImplementedError

    def answer(self, drex, row, out):
        """Writes the row's result to `out`, raising Lost for NULL."""
        raise NotImplementedError


class TypedFunction(Function):
    """drex_noul, drex_choice and drex_score: one question about a text input."""

    question_type = ""

    def __init__(self):
        self.questions = {}

    def read(self, reader):
        return (
            reader.nullable_string(),
            reader.nullable_string(),
            self.read_criteria(reader),
            reader.nullable_string(),
        )

    def read_criteria(self, reader):
        return tuple(reader.strings())

    def question(self, instructions, criteria):
        labels = [Label(text(label, "a label")) for label in criteria]
        return Question(ANSWER_ID, self.question_type, text(instructions, "instructions"), labels)

    def prepare(self, drex, rows):
        for instructions, criteria in {(row[1], row[2]) for row in rows}:
            if (instructions, criteria) not in self.questions:
                question = self.question(instructions, criteria)
                check_question("the question", question, self.argument_name)
                self.questions[instructions, criteria] = question
        for model in {model_name(row[3], DEFAULT_MODEL) for row in rows}:
            drex.check_model(model, images=False)

    def answer(self, drex, row, out):
        value, instructions, criteria, model = row
        if value is None:
            raise Lost(None)
        try:
            state = value.decode("utf-8")
        except UnicodeDecodeError:
            raise Lost("the input is not valid UTF-8") from None
        question = self.questions[instructions, criteria]
        body = drex.decide(state, {ANSWER_ID: question.wire()}, model_name(model, DEFAULT_MODEL))
        self.write(out, question, answer_of(body, question))


class Noul(TypedFunction):
    name = "drex_noul"
    question_type = "noul"
    argument_name = "criteria"

    def read_criteria(self, reader):
        return reader.nullable_string()

    def question(self, instructions, criteria):
        question = Question(ANSWER_ID, "noul", text(instructions, "instructions"))
        criteria = text(criteria, "criteria")
        if criteria.strip():
            value = parse_json(criteria, "criteria")
            question.labels = labels_from_json("criteria", value, allow_object=True)
            question.labels_from_object = isinstance(value, dict)
        return question

    def null_result(self, out):
        write_nullable_float(out, None)

    def write(self, out, question, answer):
        write_nullable_float(out, number(answer, "noul", question))


class Choice(TypedFunction):
    name = "drex_choice"
    question_type = "choice"
    argument_name = "labels"

    def null_result(self, out):
        out += b"\x01\x00\x01"  # choice NULL, no probabilities, confidence NULL

    def write(self, out, question, answer):
        """Drex's probabilities sorted by label, as drex-duckdb returns them."""
        choice = answer.get("choice")
        if not isinstance(choice, str):
            raise Fatal(f"Drex's answer to question '{question.id}' has no choice")
        probabilities = answer.get("probabilities")
        labels = sorted(label.label for label in question.labels)
        rows = [(label, number(probabilities, label, question)) for label in labels]
        confidence = number(answer, "confidence", question)
        write_nullable_string(out, choice)
        write_varint(out, len(rows))
        for label, probability in rows:
            write_string(out, label)
            write_float(out, probability)
        write_nullable_float(out, confidence)


class Score(TypedFunction):
    name = "drex_score"
    question_type = "score"
    argument_name = "levels"

    def null_result(self, out):
        out += b"\x01\x00\x01"  # score NULL, no probabilities, confidence NULL

    def write(self, out, question, answer):
        """Drex's probabilities by level index, each with its level."""
        probabilities = answer.get("probabilities")
        rows = [
            (index, label.label, number(probabilities, str(index), question))
            for index, label in enumerate(question.labels)
        ]
        score = number(answer, "score", question)
        confidence = number(answer, "confidence", question)
        write_nullable_float(out, score)
        write_varint(out, len(rows))
        for index, label, probability in rows:
            out += struct.pack("<I", index)
            write_string(out, label)
            write_float(out, probability)
        write_nullable_float(out, confidence)


class Decide(Function):
    """drex_decide: JSON state and questions in, Drex's response body out as JSON text."""

    name = "drex_decide"
    default_model = DEFAULT_MODEL
    reads_images = False

    def __init__(self):
        self.questions = {}

    def read(self, reader):
        return (reader.nullable_string(), reader.nullable_string(), reader.nullable_string())

    def null_result(self, out):
        write_nullable_string(out, None)

    def prepare(self, drex, rows):
        for questions in {row[-2] for row in rows if row[-2] is not None}:
            if questions not in self.questions:
                wire = {question.id: question.wire() for question in questions_from_json(text(questions, "questions"))}
                self.questions[questions] = wire
        for model in {model_name(row[-1], self.default_model) for row in rows}:
            drex.check_model(model, images=self.reads_images)

    def state(self, value):
        """A row's state; it must be JSON, as in drex-duckdb, whose state is a JSON value."""
        try:
            state_text = value.decode("utf-8")
        except UnicodeDecodeError:
            raise Fatal("state is not valid UTF-8") from None
        return parse_json(state_text, "state")

    def answer(self, drex, row, out):
        state, questions, model = row
        if state is None or questions is None:
            raise Lost(None)
        body = drex.decide(self.state(state), self.questions[questions], model_name(model, self.default_model))
        write_nullable_string(out, json.dumps(body, ensure_ascii=False, separators=(",", ":")))


class DecideImage(Decide):
    """drex_decide_image: drex_decide with 1 to 8 PNG, JPEG or WebP images as bytes, on drex-max by default."""

    name = "drex_decide_image"
    default_model = DEFAULT_IMAGE_MODEL
    reads_images = True

    def read(self, reader):
        return (reader.nullable_string(), tuple(reader.strings()), reader.nullable_string(), reader.nullable_string())

    def answer(self, drex, row, out):
        state, images, questions, model = row
        if state is None or questions is None or not images:
            raise Lost(None)
        if len(images) > MAX_IMAGES:
            raise Lost(f"{len(images)} images, over Drex's {MAX_IMAGES}")
        urls = []
        for index, image in enumerate(images, start=1):
            try:
                urls.append(image_input(image))
            except NaceError:
                raise Lost(f"image {index} is not a PNG, JPEG or WebP image of at most 16 MiB") from None
        body = drex.decide(
            self.state(state), self.questions[questions], model_name(model, self.default_model), images=urls
        )
        write_nullable_string(out, json.dumps(body, ensure_ascii=False, separators=(",", ":")))


FUNCTIONS = {
    "noul": Noul,
    "choice": Choice,
    "score": Score,
    "decide": Decide,
    "decide_image": DecideImage,
}


# ---------------------------------------------------------------------------------------------------------------------
# The block loop
# ---------------------------------------------------------------------------------------------------------------------


def warn(function, reason):
    sys.stderr.write(f"{function.name}: a row returned NULL: {reason}\n")
    sys.stderr.flush()


def worker_count(concurrency):
    """Four threads per request in flight, within the process's limit on threads, which counts the main one."""
    wanted = 4 * concurrency
    limit, _ = resource.getrlimit(resource.RLIMIT_NPROC)
    return wanted if limit == resource.RLIM_INFINITY else max(1, min(wanted, limit - 1))


class Workers:
    """The threads that answer rows: `Slots` caps the requests in flight, and the threads beyond it let rows that
    wait out a retry hold no slot. Daemon threads, so a query that fails exits at once instead of waiting for the
    requests still in flight."""

    def __init__(self, count):
        self.jobs = queue.SimpleQueue()
        for _ in range(count):
            threading.Thread(target=self.work, daemon=True).start()

    def work(self):
        while True:
            future, call, args = self.jobs.get()
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(call(*args))
                except BaseException as error:
                    future.set_exception(error)

    def submit(self, call, *args):
        future = concurrent.futures.Future()
        self.jobs.put((future, call, args))
        return future


def answer_block(function, drex, workers, rows, output):
    """Answers a block, writing each result to `output` as soon as every result before it is written."""
    function.prepare(drex, rows)

    def one(row):
        out = bytearray()
        try:
            function.answer(drex, row, out)
        except Lost as error:
            if error.args[0] is not None:
                warn(function, error.args[0])
            out.clear()
            function.null_result(out)
        return bytes(out)

    futures = [workers.submit(one, row) for row in rows]
    try:
        for future in futures:
            output.write(future.result())
            output.flush()
    finally:
        # After a failure, rows not yet started are never sent
        for future in futures:
            future.cancel()


def serve(config, input_stream, output):
    function = FUNCTIONS[config.function]()
    drex = Drex(config)
    workers = Workers(worker_count(config.concurrency))
    reader = Reader(input_stream)
    try:
        while True:
            header = input_stream.readline()
            if not header:
                return
            rows = [function.read(reader) for _ in range(int(header))]
            answer_block(function, drex, workers, rows, output)
    finally:
        drex.close()


def main(argv):
    name = "drex"
    try:
        if argv:
            config = config_from_args(argv)
        else:
            config = config_from_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
        name = FUNCTIONS[config.function].name
        serve(config, sys.stdin.buffer, sys.stdout.buffer)
    except Fatal as error:
        sys.stderr.write(f"{name}: {error}\n")
        sys.stderr.flush()
        return 1
    except (EOFError, BrokenPipeError):
        # ClickHouse stopped the query or closed the pool
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
