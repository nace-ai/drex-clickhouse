"""The functions through ClickHouse, against the mock Drex API: tests/docker-compose.test.yml's stack.

Each test calls `t_<scenario>_<function>`, the shipped function pointed at a mock scenario
(tests/clickhouse_functions.py), and reads what the mock received from its request log. Skipped unless
DREX_CLICKHOUSE_URL and DREX_MOCK_URL are set.
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "mock"))
import pngs

CLICKHOUSE_URL = os.environ.get("DREX_CLICKHOUSE_URL")
MOCK_URL = os.environ.get("DREX_MOCK_URL")

pytestmark = [
    pytest.mark.clickhouse,
    pytest.mark.skipif(not (CLICKHOUSE_URL and MOCK_URL), reason="needs DREX_CLICKHOUSE_URL and DREX_MOCK_URL"),
]

REFUND = "Is the customer asking for a refund?"


class QueryError(Exception):
    pass


def query(sql, settings=None):
    """The rows of `sql` as dictionaries, or QueryError with ClickHouse's message."""
    params = {"default_format": "JSONEachRow", **(settings or {})}
    url = CLICKHOUSE_URL + "/?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=sql.encode()), timeout=300) as response:
            body = response.read().decode()
    except urllib.error.HTTPError as error:
        raise QueryError(error.read().decode()) from None
    if "DB::Exception" in body:
        # An error after the first rows were sent comes at the end of a 200
        raise QueryError(body)
    return [json.loads(line) for line in body.splitlines() if line]


def value(sql, settings=None):
    rows = query(sql, settings)
    assert len(rows) == 1
    return next(iter(rows[0].values()))


def sql_string(text):
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def sql_json(value):
    return sql_string(json.dumps(value))


def sql_image(colour):
    return f"base64Decode('{base64.b64encode(pngs.fixture(colour)).decode()}')"


def requests(ns):
    """What the mock received under `ns`, without the marker record."""
    with urllib.request.urlopen(f"{MOCK_URL}/__log/{ns}", timeout=10) as response:
        records = [json.loads(line) for line in response.read().decode().splitlines()]
    return [record for record in records[1:]]


def decisions(ns):
    return [record for record in requests(ns) if record["endpoint"] == "/v1/systemone"]


def warnings_since(started):
    """The reasons of the NULL warnings logged since `started`. ClickHouse logs a block's stderr as one message."""
    query("SYSTEM FLUSH LOGS")
    rows = query(
        "SELECT message FROM system.text_log WHERE message LIKE '%a row returned NULL%' "
        f"AND event_time_microseconds >= fromUnixTimestamp64Micro({int(started * 1_000_000)}) "
        "ORDER BY event_time_microseconds"
    )
    marker = "a row returned NULL: "
    return [line.split(marker, 1)[1] for row in rows for line in row["message"].splitlines() if marker in line]


@pytest.fixture(autouse=True)
def reset_mock():
    for ns in ("ok", "401", "402", "422", "429-once", "529-once", "529-always", "too-long", "slow-300", "cap"):
        urllib.request.urlopen(f"{MOCK_URL}/__reset/{ns}", timeout=10).read()


@pytest.fixture(scope="session", autouse=True)
def test_functions():
    if not query("SELECT name FROM system.functions WHERE name = 't_ok_drex_noul'"):
        pytest.fail("the test functions are missing: run tests/clickhouse_functions.py before starting the stack")


# -- the typed functions ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("model", ["drex-v2.0", "drex-max"])
def test_noul_choice_and_score_on_each_model(model):
    model_arg = "NULL" if model == "drex-v2.0" else sql_string(model)
    noul = value(f"SELECT t_ok_drex_noul('I was charged twice', {sql_string(REFUND)}, NULL, {model_arg})")
    choice = value(f"SELECT t_ok_drex_choice('My login fails', 'Which team?', ['tech', 'billing'], {model_arg})")
    score = value(f"SELECT t_ok_drex_score('Fine', 'How upset?', ['calm', 'upset', 'angry'], {model_arg})")

    assert noul == 0.9868
    # Probabilities sorted by label, from Drex's answer
    assert choice == {
        "choice": "tech",
        "probabilities": [{"value": "billing", "probability": 0.3333}, {"value": "tech", "probability": 0.6667}],
        "confidence": 0.9,
    }
    assert score == {
        "score": 1.3333,
        "probabilities": [
            {"index": 0, "value": "calm", "probability": 0.1667},
            {"index": 1, "value": "upset", "probability": 0.3333},
            {"index": 2, "value": "angry", "probability": 0.5},
        ],
        "confidence": 0.8,
    }
    sent = decisions("ok")
    assert [record["served_model"] for record in sent] == [model] * 3
    assert {record["x_nace_client"] for record in sent} == {"drex-clickhouse/0.1.2"}
    assert json.loads(sent[1]["questions"]) == [
        {"id": "answer", "type": "choice", "instructions": "Which team?", "criteria": {"tech": None, "billing": None}}
    ]
    assert json.loads(sent[2]["questions"])[0]["criteria"] == ["calm", "upset", "angry"]


def test_result_types():
    rows = query(
        "SELECT toTypeName(t_ok_drex_noul('a', 'q?', NULL, NULL)) AS noul, "
        "toTypeName(t_ok_drex_choice('a', 'q?', ['x', 'y'], NULL)) AS choice, "
        "toTypeName(t_ok_drex_score('a', 'q?', ['x', 'y'], NULL)) AS score, "
        'toTypeName(t_ok_drex_decide(\'{}\', \'{"a": {"type": "noul", "instructions": "q?"}}\', NULL)) AS decide',
        {"print_pretty_type_names": "0"},
    )
    assert rows == [
        {
            "noul": "Nullable(Float64)",
            "choice": "Tuple(choice Nullable(String), probabilities Array(Tuple(value String, probability Float64)), "
            "confidence Nullable(Float64))",
            "score": "Tuple(score Nullable(Float64), probabilities Array(Tuple(index UInt32, value String, "
            "probability Float64)), confidence Nullable(Float64))",
            "decide": "Nullable(String)",
        }
    ]


def test_null_input_sends_nothing():
    rows = query(
        "SELECT t_ok_drex_noul(body, 'q?', NULL, NULL) AS noul, "
        "t_ok_drex_choice(body, 'q?', ['x', 'y'], NULL) AS choice FROM (SELECT arrayJoin([NULL, 'text']) AS body)"
    )
    assert rows[0] == {"noul": None, "choice": {"choice": None, "probabilities": [], "confidence": None}}
    assert rows[1]["noul"] == 0.75
    assert len(decisions("ok")) == 2


def test_rows_keep_their_order_across_a_block():
    rows = query(
        "SELECT number, t_slow_300_drex_choice(toString(number), 'q?', [toString(number), 'other'], NULL).choice AS c "
        "FROM numbers(20)"
    )
    assert [row["c"] for row in rows] == [str(n) for n in range(20)]


def test_noul_criteria_without_instructions():
    criteria = {"true": "The customer wants money back", "false": None}
    assert value(f"SELECT t_ok_drex_noul('Refund me', '', {sql_json(criteria)}, NULL)") == 0.75
    assert json.loads(decisions("ok")[0]["questions"]) == [
        {"id": "answer", "type": "noul", "instructions": "", "criteria": criteria}
    ]


@pytest.mark.parametrize(
    "criteria",
    [
        '["true", "false"]',
        '[{"label": "true"}, {"label": "false", "description": null}]',
    ],
)
def test_noul_labels_alone_are_no_criteria(criteria):
    assert value(f"SELECT t_ok_drex_noul('x', 'q?', {sql_string(criteria)}, NULL)") == 0.75
    assert "criteria" not in json.loads(decisions("ok")[0]["questions"])[0]


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (
            "t_ok_drex_noul('x', '', NULL, NULL)",
            "drex_noul: the question needs instructions, or criteria that describe true or false",
        ),
        (
            "t_ok_drex_noul('x', 'q?', '[\"yes\", \"no\"]', NULL)",
            "labels must be exactly true and false, got [yes, no]",
        ),
        ("t_ok_drex_noul('x', 'q?', '{\"true\": 1}', NULL)", "criteria must be"),
        ("t_ok_drex_noul('x', 'q?', 'not json', NULL)", "criteria is not valid JSON"),
        ("t_ok_drex_choice('x', 'q?', ['only'], NULL)", "labels takes 2 to 255 labels, got 1"),
        ("t_ok_drex_choice('x', 'q?', ['a', 'a'], NULL)", "drex_choice: labels must be unique; 'a' is a duplicate"),
        ("t_ok_drex_choice('x', 'q?', ['a', ''], NULL)", "drex_choice: labels must not hold an empty label"),
        ("t_ok_drex_score('x', 'q?', ['low', ''], NULL)", "drex_score: levels must not hold an empty level"),
        ("t_ok_drex_score('x', 'q?', arrayMap(n -> toString(n), range(256)), NULL)", "got 256"),
    ],
)
def test_label_rules_fail_the_query_before_any_request(call, message):
    with pytest.raises(QueryError, match="UDF_EXECUTION_FAILED") as failure:
        query(f"SELECT {call}")
    assert message in str(failure.value)
    assert decisions("ok") == []


def test_a_bad_constant_fails_the_query_wherever_it_appears():
    """Even on a row whose NULL input sends nothing."""
    with pytest.raises(QueryError, match="unknown model 'drex-nope'"):
        query(
            "SELECT t_ok_drex_noul(body, 'q?', NULL, model) FROM "
            "(SELECT arrayJoin([('text', ''), (NULL, 'drex-nope')]) AS t, t.1 AS body, t.2 AS model)"
        )
    assert decisions("ok") == []


def test_unknown_model_fails_the_query():
    with pytest.raises(QueryError) as failure:
        query("SELECT t_ok_drex_choice('x', 'q?', ['a', 'b'], 'drex-nope')")
    assert "unknown model 'drex-nope'; GET /v1/models lists drex-v1.5, drex-v2.0, drex-max" in str(failure.value)


def test_input_that_is_not_utf8_is_null():
    started = time.time()
    rows = query(
        "SELECT t_ok_drex_noul(body, 'q?', NULL, NULL) AS p FROM (SELECT arrayJoin([unhex('FF'), 'ok']) AS body)"
    )
    assert [row["p"] for row in rows] == [None, 0.75]
    assert warnings_since(started) == ["the input is not valid UTF-8"]


# -- drex_decide -------------------------------------------------------------------------------------------------------


def test_decide_mixed_batch_in_one_request():
    questions = {
        "refund": {"type": "noul", "instructions": REFUND},
        "team": {"type": "choice", "instructions": "Which team?", "criteria": {"billing": None, "tech": "Bugs"}},
        "urgency": {"type": "score", "instructions": "How urgent?", "criteria": ["low", "high"]},
    }
    body = json.loads(
        value(f"SELECT t_ok_drex_decide({sql_json({'m': 'I was charged twice'})}, {sql_json(questions)}, NULL)")
    )
    assert body["model"] == "drex-v2.0"
    assert set(body["answers"]) == {"refund", "team", "urgency"}
    assert body["answers"]["team"]["choice"] == "billing"
    assert {"usage", "evaluation_time_ms", "request_id"} <= set(body)
    sent = decisions("ok")
    assert len(sent) == 1
    assert sent[0]["state_type"] == "object"
    # A choice option with a null description is sent as null
    assert json.loads(sent[0]["questions"])[1]["criteria"] == {"billing": None, "tech": "Bugs"}


def test_decide_takes_duckdbs_criteria_forms():
    questions = {"team": {"type": "choice", "instructions": "Which team?", "criteria": [{"label": "billing"}, "tech"]}}
    value(f"SELECT t_ok_drex_decide('\"text\"', {sql_json(questions)}, NULL)")
    assert json.loads(decisions("ok")[0]["questions"])[0]["criteria"] == {"billing": None, "tech": None}


@pytest.mark.parametrize(
    ("questions", "message"),
    [
        ({"__proto__": {"type": "noul", "instructions": "q?"}}, "'__proto__' is not supported"),
        (
            {"a": {"type": "noul", "instructions": "q?"}, "A": {"type": "noul", "instructions": "q?"}},
            "unique ignoring case",
        ),
        ({" ": {"type": "noul", "instructions": "q?"}}, "question ids must not be empty"),
        ({"a": {"type": "multiple_choice", "instructions": "q?"}}, "multiple_choice questions are not supported"),
        ({"a": {"type": "box"}}, "box questions are not supported"),
        ({"a": {"type": "noul", "instructions": "q?", "extra": 1}}, "unknown key 'extra'"),
        ({"a": {"type": "choice", "instructions": "q?"}}, "questions.a needs criteria"),
        (
            {"a": {"type": "score", "instructions": "q?", "criteria": {"low": "x", "high": "y"}}},
            "questions.a.criteria must be",
        ),
        (
            {"a": {"type": "score", "instructions": "q?", "criteria": [{"label": "x", "description": "d"}, "y"]}},
            "score levels take no description",
        ),
        ({}, "takes 1 to 512 questions, got 0"),
        ({f"q{n}": {"type": "noul", "instructions": "q?"} for n in range(513)}, "got 513"),
    ],
)
def test_question_rules_fail_the_query_before_any_request(questions, message):
    with pytest.raises(QueryError, match="UDF_EXECUTION_FAILED") as failure:
        query(f"SELECT t_ok_drex_decide('{{}}', {sql_json(questions)}, NULL)")
    assert message in str(failure.value)
    assert decisions("ok") == []


def test_decide_refuses_state_that_is_not_json():
    with pytest.raises(QueryError, match="state is not valid JSON"):
        query('SELECT t_ok_drex_decide(\'not json\', \'{"a": {"type": "noul", "instructions": "q?"}}\', NULL)')


def test_decide_with_null_state_or_questions_is_null():
    rows = query(
        'SELECT t_ok_drex_decide(NULL, \'{"a": {"type": "noul", "instructions": "q?"}}\', NULL) AS a, '
        "t_ok_drex_decide('{}', NULL, NULL) AS b"
    )
    assert rows == [{"a": None, "b": None}]
    assert decisions("ok") == []


# -- images ------------------------------------------------------------------------------------------------------------


def colour_question(n):
    return {"colour": {"type": "choice", "instructions": f"Which colour is Image {n}?", "criteria": ["red", "blue"]}}


def test_decide_image_defaults_to_drex_max_and_reads_one_image():
    body = json.loads(
        value(f"SELECT t_ok_drex_decide_image('{{}}', [{sql_image('blue')}], {sql_json(colour_question(1))}, NULL)")
    )
    assert body["model"] == "drex-max"
    assert body["answers"]["colour"]["choice"] == "blue"
    assert decisions("ok")[0]["image_colours"] == ["blue"]


@pytest.mark.parametrize(("images", "expected"), [(["red", "blue"], "red"), (["blue", "red"], "blue")])
def test_decide_image_keeps_the_order_of_two_images(images, expected):
    array = ", ".join(sql_image(colour) for colour in images)
    body = json.loads(
        value(f"SELECT t_ok_drex_decide_image('{{}}', [{array}], {sql_json(colour_question(1))}, 'drex-max')")
    )
    assert body["answers"]["colour"]["choice"] == expected
    assert decisions("ok")[0]["image_colours"] == images


def test_images_on_a_model_that_does_not_read_them_fail_the_query():
    with pytest.raises(QueryError, match=r"drex-v2\.0 does not read images"):
        query(
            f"SELECT t_ok_drex_decide_image('{{}}', [{sql_image('red')}], {sql_json(colour_question(1))}, 'drex-v2.0')"
        )
    assert decisions("ok") == []


def test_decide_image_without_images_or_with_an_unknown_format_is_null():
    started = time.time()
    rows = query(
        f"SELECT t_ok_drex_decide_image('{{}}', images, {sql_json(colour_question(1))}, NULL) AS a FROM "
        "(SELECT arrayJoin([[], ['GIF89a not a png']]) AS images)"
    )
    assert rows == [{"a": None}, {"a": None}]
    assert decisions("ok") == []
    assert warnings_since(started) == ["image 1 is not a PNG, JPEG or WebP image of at most 16 MiB"]


# -- Drex's errors -----------------------------------------------------------------------------------------------------


def test_401_fails_the_query_with_drexs_message():
    with pytest.raises(QueryError) as failure:
        query("SELECT t_401_drex_noul('x', 'q?', NULL, NULL)")
    assert "drex_noul: Invalid API key." in str(failure.value)
    assert "nace_sk_" not in str(failure.value).replace("Pass a nace_sk_ key", "")


def test_402_fails_the_query_and_names_the_billing_page():
    with pytest.raises(QueryError) as failure:
        query("SELECT t_402_drex_choice('x', 'q?', ['a', 'b'], NULL)")
    assert "insufficient available funds" in str(failure.value)
    assert "Billing: http://127.0.0.1:8787/ns/402/402/dashboard/billing" in str(failure.value)


def test_422_names_each_issue():
    with pytest.raises(QueryError) as failure:
        query("SELECT t_422_drex_noul('x', 'q?', NULL, NULL)")
    assert (
        "Drex refused the request: questions.a.instructions: must be a string; "
        "questions.a.instructions: noul needs instructions or criteria" in str(failure.value)
    )


@pytest.mark.parametrize(("scenario", "wait_ms"), [("429-once", 1000), ("529-once", 2000)])
def test_429_and_529_are_retried_after_retry_after_ms(scenario, wait_ms):
    function = "t_" + scenario.replace("-", "_") + "_drex_noul"
    assert value(f"SELECT {function}('x', 'q?', NULL, NULL)") == 0.75
    sent = decisions(scenario)
    assert [record["status"] for record in sent] == [int(scenario[:3]), 200]
    assert sent[1]["body_sha256"] == sent[0]["body_sha256"]
    assert sent[1]["waited_ms"] >= wait_ms


def test_exhausted_retries_return_null_and_the_query_continues():
    started = time.time()
    assert value("SELECT t_529_always_drex_noul('x', 'q?', NULL, NULL)") is None
    assert [record["status"] for record in decisions("529-always")] == [529] * 5
    (warning,) = warnings_since(started)
    assert "no answer after 5 attempts, the last HTTP 529: Drex is at capacity. Retry shortly." in warning


def test_oversized_rows_are_null_and_the_query_continues():
    """A row Drex refuses as too long, and a body over 4 MiB, which is never sent."""
    started = time.time()
    rows = query(
        "SELECT t_too_long_drex_noul(body, 'q?', NULL, NULL) AS p FROM "
        "(SELECT arrayJoin(['short', repeat('a long row ', 1000), repeat('xxxxx', 1000000), 'short too']) AS body)"
    )
    assert [row["p"] for row in rows] == [0.75, None, None, 0.75]
    assert len(decisions("too-long")) == 3
    reasons = sorted(warnings_since(started))
    assert len(reasons) == 2
    assert reasons[0].startswith("HTTP 422: state: exceeds the 1,024-token state limit")
    assert reasons[1] == "the request body is 5000091 bytes, over Drex's 4194304 bytes"
    assert "a long row" not in "".join(reasons)


# -- concurrency -------------------------------------------------------------------------------------------------------


def test_the_cap_holds_across_a_pool_of_processes():
    """t_cap_*: 2 processes x 3 requests in flight, against answers held 300 ms. Four parallel streams of blocks
    contend for the pool, so both processes run at their cap and nothing goes over 6."""
    rows = query(
        "SELECT count(t_cap_drex_noul(toString(number), 'q?', NULL, NULL)) AS n FROM numbers_mt(120)",
        {"max_threads": "4", "max_block_size": "15"},
    )
    assert rows == [{"n": 120}]
    peak = max(record["in_flight"] for record in decisions("cap"))
    assert peak == 6
