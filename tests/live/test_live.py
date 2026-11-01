"""The shipped functions against the live Drex API: billed requests, run by LiveTests.yml on workflow_dispatch.

docker-compose.yml's stack with a real key in secrets/nace_api_key. Skipped unless DREX_LIVE=1 and DREX_CLICKHOUSE_URL
is set. The inputs are made up; no customer content. Each test prints its responses' request ids, so they can be found
in Drex's api_request logs.
"""

import base64
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mock"))
import pngs
from test_clickhouse import query, sql_json, sql_string, value

pytestmark = pytest.mark.skipif(
    os.environ.get("DREX_LIVE") != "1" or not os.environ.get("DREX_CLICKHOUSE_URL"),
    reason="needs DREX_LIVE=1 and DREX_CLICKHOUSE_URL",
)

TICKET = "I was charged twice for my subscription this month. Please refund the duplicate payment."


def sql_image(colour):
    return f"base64Decode('{base64.b64encode(pngs.fixture(colour)).decode()}')"


def test_typed_functions_on_drex_v2():
    ticket = sql_string(TICKET)
    row = query(
        f"SELECT drex_noul({ticket}, 'Is the customer asking for a refund?', NULL, NULL) AS refund, "
        f"drex_choice({ticket}, 'Which team should handle this ticket?', ['billing', 'technical', 'account'], NULL) "
        f"AS team, drex_score({ticket}, 'How upset is the customer?', ['calm', 'annoyed', 'angry'], 'drex-v2.0') "
        "AS upset"
    )[0]
    assert 0.5 < row["refund"] <= 1
    assert row["team"]["choice"] == "billing"
    assert [entry["value"] for entry in row["team"]["probabilities"]] == ["account", "billing", "technical"]
    assert abs(sum(entry["probability"] for entry in row["team"]["probabilities"]) - 1) < 0.01
    assert 0 <= row["upset"]["score"] <= 2
    assert [entry["index"] for entry in row["upset"]["probabilities"]] == [0, 1, 2]
    assert 0 <= row["upset"]["confidence"] <= 1


def test_decide_returns_drexs_body():
    questions = {
        "refund": {"type": "noul", "instructions": "Is the customer asking for a refund?"},
        "team": {"type": "choice", "instructions": "Which team?", "criteria": {"billing": None, "technical": None}},
    }
    body = json.loads(value(f"SELECT drex_decide({sql_json({'ticket': TICKET})}, {sql_json(questions)}, NULL)"))
    print("drex_decide request", body["request_id"])
    assert body["model"] == "drex-v2.0"
    assert body["answers"]["team"]["choice"] == "billing"
    assert body["usage"]["input_tokens"] > 0


@pytest.mark.parametrize(
    ("images", "expected"), [(["red"], "red"), (["red", "blue"], "red"), (["blue", "red"], "blue")]
)
def test_decide_image_on_drex_max(images, expected):
    questions = {"colour": {"type": "choice", "instructions": "Which colour is Image 1?", "criteria": ["red", "blue"]}}
    array = ", ".join(sql_image(colour) for colour in images)
    body = json.loads(value(f"SELECT drex_decide_image('{{}}', [{array}], {sql_json(questions)}, NULL)"))
    print("drex_decide_image request", body["request_id"])
    assert body["model"] == "drex-max"
    assert body["answers"]["colour"]["choice"] == expected
