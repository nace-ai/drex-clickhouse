"""The mock's own checks: python3 -m unittest discover -s test/mock

With the jsonschema package installed, every response is also validated against its schema in Drex's openapi.json:
SystemOneResponse, ModelsResponse or ErrorEnvelope. The schemas are test/mock/openapi.json (openapi_subset.py writes
it), or the file DREX_OPENAPI names.
"""

import base64
import json
import os
import ssl
import struct
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import drex_mock  # noqa: E402
import pngs  # noqa: E402

KEY = "nace_sk_" + "t" * 43
SUPPORT_TICKET = {
    "model": "drex-v2.0",
    "state": "I was charged twice for my March invoice. Please refund one of the charges.",
    "questions": {
        "category": {
            "type": "choice",
            "instructions": "What is this support ticket about?",
            "criteria": {
                "billing": "Payments, invoices, refunds, or charges",
                "technical": "Bugs, errors, or outages",
                "account": "Login, profile, or permissions",
                "other": None,
            },
        },
        "sentiment": {
            "type": "score",
            "instructions": "How upset is the customer?",
            "criteria": ["Calm", "Mildly annoyed", "Frustrated", "Angry"],
        },
        "wants_refund": {"type": "noul", "instructions": "Is the customer asking for a refund?"},
    },
}


def _openapi_validators():
    path = os.environ.get("DREX_OPENAPI") or Path(__file__).parent / "openapi.json"
    try:
        import jsonschema
    except ImportError:
        return None
    spec = json.loads(Path(path).read_text())

    def validator(name):
        schema = {"$ref": f"#/components/schemas/{name}", "components": spec["components"]}
        return jsonschema.Draft202012Validator(schema)

    return {
        name: validator(name) for name in ("SystemOneRequest", "SystemOneResponse", "ModelsResponse", "ErrorEnvelope")
    }


class MockTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = drex_mock.make_server()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.schemas = _openapi_validators()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.ns = self.id().rsplit(".", 1)[-1]
        self.fetch(f"/__reset/{self.ns}")

    # -- helpers --------------------------------------------------------------------------------------------------

    def fetch(self, path, *, body=None, key=KEY, method=None, headers=None):
        """(status, headers, raw body)."""
        request = urllib.request.Request(self.url + path, method=method)
        if key:
            request.add_header("Authorization", f"Bearer {key}")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        if body is not None:
            request.data = body if isinstance(body, bytes) else json.dumps(body).encode()
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def api(self, scenario, endpoint, body=None, **kwargs):
        """(status, headers, parsed JSON), validated against openapi.json when it is available."""
        status, headers, raw = self.fetch(f"/ns/{self.ns}/{scenario}{endpoint}", body=body, **kwargs)
        payload = json.loads(raw)
        if status < 400:
            self.assertRegex(payload["request_id"], r"^req_[a-f0-9]{32}$")
        else:
            self.assertEqual(set(payload), {"error", "request_id"})
        self.assertEqual(headers["x-request-id"], payload["request_id"])
        self.assertEqual(headers["x-typesafe-request-id"], payload["request_id"])
        if self.schemas:
            name = (
                "ErrorEnvelope"
                if status >= 400
                else ("ModelsResponse" if endpoint == "/v1/models" else "SystemOneResponse")
            )
            self.schemas[name].validate(payload)
            if body is not None and isinstance(body, dict) and status == 200:
                self.schemas["SystemOneRequest"].validate(body)
        return status, headers, payload

    def log(self):
        status, _, raw = self.fetch(f"/__log/{self.ns}")
        self.assertEqual(status, 200)
        records = [json.loads(line) for line in raw.decode().splitlines()]
        self.assertEqual(records[0]["endpoint"], "(log)")
        for record in records:
            self.assertEqual(set(record), set(drex_mock.LOG_FIELDS))
        return records[1:]

    @staticmethod
    def image(colour):
        return base64.b64encode(pngs.fixture(colour)).decode()

    # -- fixtures and plumbing ------------------------------------------------------------------------------------

    def test_fixtures_are_solid_png_of_each_colour(self):
        for colour in ("red", "blue"):
            status, headers, png = self.fetch(f"/__fixtures/{colour}.png", key=None)
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"], "image/png")
            self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", png[16:24]), (pngs.SIZE, pngs.SIZE))
            self.assertEqual(pngs.colour_of(png), colour)
        self.assertEqual(pngs.colour_of(b"not a png"), "unknown")

    def test_resources_answer_head_and_ranges_and_are_not_cacheable(self):
        _, _, png = self.fetch("/__fixtures/red.png", key=None)
        status, headers, body = self.fetch("/__fixtures/red.png", key=None, method="HEAD")
        self.assertEqual((status, body, headers["Content-Length"]), (200, b"", str(len(png))))
        self.assertEqual(headers["Cache-Control"], "no-store")
        status, headers, body = self.fetch("/__fixtures/red.png", key=None, headers={"Range": "bytes=4-9"})
        self.assertEqual((status, body, headers["Content-Range"]), (206, png[4:10], f"bytes 4-9/{len(png)}"))

    def test_log_starts_with_a_marker_and_reset_and_namespaces_isolate_it(self):
        self.assertEqual(self.log(), [])
        self.api("ok", "/v1/models")
        self.fetch("/ns/elsewhere/ok/v1/models")
        self.assertEqual([r["endpoint"] for r in self.log()], ["/v1/models"])
        self.fetch(f"/__reset/{self.ns}")
        self.assertEqual(self.log(), [])

    def test_unknown_scenario_is_a_404_not_an_answer(self):
        status, _, _ = self.fetch(f"/ns/{self.ns}/nope/v1/models")
        self.assertEqual(status, 404)

    # -- /v1/models -----------------------------------------------------------------------------------------------

    def test_models_lists_the_catalog_without_image_flags(self):
        status, _, payload = self.api("ok", "/v1/models")
        self.assertEqual(status, 200)
        by_name = {m["name"]: m for m in payload["models"]}
        self.assertEqual(
            set(by_name),
            {"drex-v1.5", "drex-v2.0", "drex-max", "drex-max-v1.1", "drex-latest", "drex-v1.0", "drex-v1.1"},
        )
        self.assertEqual(by_name["drex-latest"]["alias_for"], "drex-v2.0")
        self.assertEqual(by_name["drex-v1.0"]["alias_for"], "drex-max")
        self.assertIsNone(by_name["drex-max"]["alias_for"])
        for model in payload["models"]:
            self.assertEqual(set(model), {"name", "description", "release_date", "alias_for"})

    def test_models_refuses_a_missing_or_malformed_key(self):
        for key in (None, "nace_sk_short"):
            status, _, payload = self.api("ok", "/v1/models", key=key)
            self.assertEqual((status, payload["error"]["type"]), (401, "authentication_error"))
        status, _, _ = self.api("401", "/v1/models")
        self.assertEqual(status, 401)

    # -- /v1/systemone answers ------------------------------------------------------------------------------------

    def test_documented_example_gets_the_documented_answers(self):
        status, _, payload = self.api("ok", "/v1/systemone", SUPPORT_TICKET)
        self.assertEqual(status, 200)
        self.assertEqual(payload["model"], "drex-v2.0")
        answers = payload["answers"]
        self.assertEqual(answers["wants_refund"], {"type": "noul", "noul": 0.9868, "confidence": 0.9868})
        self.assertEqual(answers["category"]["choice"], "billing")
        self.assertEqual(answers["category"]["probabilities"]["other"], 0.0049)
        self.assertEqual(answers["sentiment"]["score"], 1.5306)
        self.assertEqual(answers["sentiment"]["probabilities"], {"0": 0.1572, "1": 0.3516, "2": 0.2946, "3": 0.1966})

    def test_answers_are_matched_by_content_not_id(self):
        body = {"state": "x", "questions": {"q": SUPPORT_TICKET["questions"]["wants_refund"]}, "model": "drex-max"}
        status, _, payload = self.api("ok", "/v1/systemone", body)
        self.assertEqual((status, payload["model"], payload["answers"]["q"]["noul"]), (200, "drex-max", 0.9868))

    def test_generic_answers_are_deterministic_and_well_formed(self):
        body = {
            "state": {"ticket": 1},
            "questions": {
                "n": {"type": "noul", "instructions": "", "criteria": {"true": "yes", "false": None}},
                "c": {"type": "choice", "instructions": "", "criteria": {"b": None, "a": "first"}},
                "s": {"type": "score", "instructions": "How?", "criteria": ["low", "mid", "high"]},
            },
        }
        first = self.api("ok", "/v1/systemone", body)[2]
        self.assertEqual(first["answers"], self.api("ok", "/v1/systemone", body)[2]["answers"])
        answers = first["answers"]
        self.assertEqual(answers["n"], {"type": "noul", "noul": 0.75, "confidence": 0.75})
        self.assertEqual(answers["c"]["choice"], "b")
        self.assertAlmostEqual(sum(answers["c"]["probabilities"].values()), 1, places=6)
        self.assertAlmostEqual(sum(answers["s"]["probabilities"].values()), 1, places=6)
        self.assertEqual(answers["s"]["legend"], ["low", "mid", "high"])
        self.assertEqual(first["model"], "drex-v2.0")

    def test_image_questions_follow_image_order(self):
        def colour_of(images, instructions):
            body = {
                "model": "drex-max",
                "state": "",
                "images": [self.image(c) for c in images],
                "questions": {
                    "c": {"type": "choice", "instructions": instructions, "criteria": {"red": None, "blue": None}}
                },
            }
            status, _, payload = self.api("ok", "/v1/systemone", body)
            self.assertEqual(status, 200)
            self.assertEqual(payload["usage"]["image_tokens"], 258 * len(images))
            return payload["answers"]["c"]["choice"]

        self.assertEqual(colour_of(["red"], "What colour is Image 1?"), "red")
        self.assertEqual(colour_of(["red", "blue"], "What colour is Image 2?"), "blue")
        self.assertEqual(colour_of(["blue", "red"], "What colour is Image 2?"), "red")
        self.assertEqual(colour_of(["blue", "red"], "What colour is it?"), "blue")
        self.assertEqual(
            [r["image_colours"] for r in self.log()], [["red"], ["red", "blue"], ["blue", "red"], ["blue", "red"]]
        )

    def test_data_url_images_are_accepted(self):
        body = {
            "model": "drex-max-v1.1",
            "state": "",
            "images": ["data:image/png;base64," + self.image("blue")],
            "questions": {"c": {"type": "choice", "instructions": "Image 1?", "criteria": {"red": None, "blue": None}}},
        }
        self.assertEqual(self.api("ok", "/v1/systemone", body)[2]["answers"]["c"]["choice"], "blue")

    # -- /v1/systemone validation, as Drex refuses ---------------------------------------------------------------

    def assert_issue(self, body, path, message):
        status, _, payload = self.api("ok", "/v1/systemone", body)
        self.assertEqual((status, payload["error"]["type"]), (422, "invalid_request_error"))
        self.assertIn({"path": path, "message": message}, payload["error"]["issues"])

    def test_null_instructions_are_refused(self):
        self.assert_issue(
            {"state": "x", "questions": {"a": {"type": "noul", "instructions": None}}},
            "questions.a.instructions",
            "must be a string",
        )

    def test_noul_with_criteria_needs_no_instructions(self):
        body = {"state": "x", "questions": {"a": {"type": "noul", "instructions": "", "criteria": {"true": "y"}}}}
        self.assertEqual(self.api("ok", "/v1/systemone", body)[0], 200)
        self.assert_issue(
            {"state": "x", "questions": {"a": {"type": "noul", "instructions": ""}}},
            "questions.a.instructions",
            "noul needs instructions or criteria",
        )

    def test_images_need_an_image_model(self):
        body = {
            "model": "drex-v2.0",
            "state": "",
            "images": [self.image("red")],
            "questions": {"a": {"type": "noul", "instructions": "Red?"}},
        }
        self.assert_issue(body, "images", "images require drex-max or drex-max-v1.1")

    def test_max_takes_the_global_question_limit(self):
        def body(n):
            return {
                "model": "drex-max",
                "state": "x",
                "questions": {f"q{i}": {"type": "noul", "instructions": "?"} for i in range(n)},
            }

        self.assertEqual(self.api("ok", "/v1/systemone", body(512))[0], 200)
        self.assert_issue(body(513), "questions", "at most 512 questions allowed")

    def test_unknown_drex_names_are_served_like_drex_latest_and_others_refused(self):
        body = {"model": "drex-v99.9", "state": "x", "questions": {"a": {"type": "noul", "instructions": "?"}}}
        self.assertEqual(self.api("ok", "/v1/systemone", body)[2]["model"], "drex-v2.0")
        self.assert_issue(
            {**body, "model": "jev-latest"},
            "model",
            'unknown model "jev-latest"; use "drex-v1.5", "drex-v2.0", "drex-max", "drex-max-v1.1" or ' '"drex-latest"',
        )

    def test_missing_state_and_invalid_json(self):
        self.assert_issue({"questions": {"a": {"type": "noul", "instructions": "?"}}}, "state", "is required")
        status, _, payload = self.api("ok", "/v1/systemone", b"{not json")
        self.assertEqual((status, payload["error"]["issues"][0]["path"]), (422, ""))

    # -- scenarios ------------------------------------------------------------------------------------------------

    def test_error_scenarios(self):
        status, _, payload = self.api("401", "/v1/systemone", SUPPORT_TICKET)
        self.assertEqual((status, payload["error"]["type"]), (401, "authentication_error"))
        status, _, payload = self.api("402", "/v1/systemone", SUPPORT_TICKET)
        self.assertEqual((status, payload["error"]["type"]), (402, "insufficient_credit"))
        status, _, payload = self.api("422", "/v1/systemone", SUPPORT_TICKET)
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["message"], "questions.a.instructions: must be a string")
        self.assertEqual(payload["error"]["issues"], drex_mock.TWO_ISSUES)
        # 402 and 422 are /v1/systemone's; /v1/models still answers
        self.assertEqual(self.api("402", "/v1/models")[0], 200)

    def test_too_long_scenario_refuses_as_drex_does(self):
        question = {"a": {"type": "noul", "instructions": "?"}}
        state = {"model": "drex-v2.0", "state": "x" * 4096, "questions": question}
        self.assertEqual(self.api("too-long", "/v1/systemone", state)[0], 200)
        status, headers, payload = self.api("too-long", "/v1/systemone", {**state, "state": "x" * 4100})
        self.assertEqual((status, "retry-after-ms" in headers), (422, False))
        self.assertEqual(
            payload["error"],
            {
                "type": "invalid_request_error",
                "message": "Request too long: the state exceeds the 1,024-token state limit of drex-v2.0. Shorten the "
                "state.",
                "issues": [{"path": "state", "message": "exceeds the 1,024-token state limit"}],
            },
        )

        images = {"model": "drex-max", "state": "", "images": [self.image("red")] * 3, "questions": question}
        self.assertEqual(self.api("too-long", "/v1/systemone", images)[0], 200)
        status, _, payload = self.api("too-long", "/v1/systemone", {**images, "images": [self.image("red")] * 4})
        self.assertEqual(status, 422)
        self.assertTrue(payload["error"]["message"].startswith("Request too long: the whole request "))
        self.assertEqual(
            payload["error"]["issues"], [{"path": "", "message": "exceeds the 1,024-token whole-request limit"}]
        )
        # The other scenarios count nothing
        self.assertEqual(self.api("ok", "/v1/systemone", {**state, "state": "x" * 4100})[0], 200)

    def test_once_scenarios_fail_the_first_attempt_then_answer_with_retry_after(self):
        for scenario, status, kind, retry_after_ms in (
            ("429-once", 429, "rate_limit_error", 1000),
            ("529-once", 529, "overloaded", 2000),
        ):
            first_status, headers, payload = self.api(scenario, "/v1/systemone", SUPPORT_TICKET)
            self.assertEqual((first_status, payload["error"]["type"]), (status, kind))
            self.assertEqual(headers["retry-after-ms"], str(retry_after_ms))
            self.assertEqual(headers["retry-after"], str(retry_after_ms // 1000))
            self.assertEqual(self.api(scenario, "/v1/systemone", SUPPORT_TICKET)[0], 200)
        records = self.log()
        self.assertEqual(
            [(r["status"], r["attempt"], r["retry_after_ms"]) for r in records],
            [(429, 1, 1000), (200, 2, None), (529, 1, 2000), (200, 2, None)],
        )
        self.assertIsNone(records[0]["waited_ms"])
        self.assertGreaterEqual(records[1]["waited_ms"], 0)

    def test_always_scenario_never_answers(self):
        for _ in range(3):
            status, headers, _ = self.api("529-always", "/v1/systemone", SUPPORT_TICKET)
            self.assertEqual((status, headers["retry-after-ms"]), (529, "2000"))
        self.assertEqual([r["attempt"] for r in self.log()], [1, 2, 3])

    def test_slow_scenario_holds_each_answer_and_answers_as_ok(self):
        started = time.monotonic()
        status, _, payload = self.api("slow-300", "/v1/systemone", SUPPORT_TICKET)
        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        _, _, ok = self.api("ok", "/v1/systemone", SUPPORT_TICKET)
        self.assertEqual((status, payload["answers"]), (200, ok["answers"]))
        for scenario in ("slow-0", "slow-60001", "slow-x"):
            self.assertEqual(self.fetch(f"/ns/{self.ns}/{scenario}/v1/models")[0], 404)

    def test_log_counts_the_namespace_requests_in_flight(self):
        def send(ns):
            self.fetch(f"/ns/{ns}/slow-500/v1/systemone", body=SUPPORT_TICKET)

        threads = [threading.Thread(target=send, args=(ns,)) for ns in [self.ns] * 3 + ["elsewhere"]]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.api("ok", "/v1/models")
        records = self.log()
        self.assertEqual(sorted(r["in_flight"] for r in records[:3]), [1, 2, 3])
        self.assertEqual(records[3]["in_flight"], 1)
        self.assertLessEqual(records[0]["received_at"], records[3]["received_at"])
        self.assertLess(abs(records[3]["received_at"] - time.time()), 5)

    # -- OpenRouter -----------------------------------------------------------------------------------------------

    def open_router(self, endpoint, body=None, key="sk-or-v1-test"):
        """(status, parsed JSON) for https://openrouter.ai/api/ns/<ns>/ok<endpoint>, tunnelled through the mock as the
        proxy and trusting the certificate it serves."""
        with urllib.request.urlopen(f"{self.url}/__fixtures/openrouter.pem") as response:
            context = ssl.create_default_context(cadata=response.read().decode())
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": self.url}), urllib.request.HTTPSHandler(context=context)
        )
        request = urllib.request.Request(f"https://openrouter.ai/api/ns/{self.ns}/ok{endpoint}")
        request.add_header("Authorization", f"Bearer {key}")
        if body is not None:
            request.data = json.dumps(body).encode()
            request.add_header("Content-Type", "application/json")
        try:
            with opener.open(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_open_router_lists_nace_models_only_as_decision_models(self):
        self.assertEqual([m["id"] for m in self.open_router("/v1/models")[1]["data"]], ["openai/gpt-5"])
        status, payload = self.open_router("/v1/models?output_modalities=decisions")
        listed = {m["id"]: m["architecture"]["input_modalities"] for m in payload["data"]}
        self.assertEqual(status, 200)
        self.assertEqual(listed["nace-ai/drex-max"], ["text", "image"])
        self.assertEqual(listed["nace-ai/drex-v2.0"], ["text"])
        self.assertNotIn("nace-ai/drex-max-v1.1", listed)
        self.assertEqual(self.open_router("/v1/models", key=KEY)[0], 401)

    def test_open_router_takes_images_as_state_parts(self):
        def body(colours, **extra):
            parts = [{"type": "text", "text": ""}] + [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + self.image(c)}} for c in colours
            ]
            question = {
                "type": "choice",
                "instructions": "What colour is Image 2?",
                "criteria": {"red": None, "blue": None},
            }
            return {"model": "nace-ai/drex-max", "state": parts, "questions": {"c": question}, **extra}

        status, payload = self.open_router("/v1/systemone", body(["red", "blue"]))
        self.assertEqual(
            (status, payload["model"], payload["answers"]["c"]["choice"]), (200, "nace-ai/drex-max", "blue")
        )
        record = self.log()[0]
        self.assertEqual(
            (record["open_router"], record["model"], record["served_model"]), (True, "nace-ai/drex-max", "drex-max")
        )
        self.assertEqual((record["image_count"], record["image_colours"]), (2, ["red", "blue"]))
        # What OpenRouter refuses
        self.assertEqual(self.open_router("/v1/systemone", body(["red"], images=[self.image("red")]))[0], 400)
        self.assertEqual(self.open_router("/v1/systemone", {**body(["red"]), "model": "drex-max"})[0], 400)
        no_instructions = {
            "model": "nace-ai/drex-v2.0",
            "state": "x",
            "questions": {"a": {"type": "noul", "criteria": {"true": "y"}}},
        }
        self.assertEqual(self.open_router("/v1/systemone", no_instructions)[0], 400)

    # -- the request log ------------------------------------------------------------------------------------------

    def test_log_records_headers_and_instructions(self):
        headers = {"User-Agent": "drex-duckdb/0.0.1", "X-Nace-Client": "drex-duckdb/0.0.1", "Idempotency-Key": "k1"}
        self.api("ok", "/v1/systemone", SUPPORT_TICKET, headers=headers)
        no_instructions = {"state": {"a": 1}, "questions": {"a": {"type": "noul", "criteria": {"true": "y"}}}}
        self.api("ok", "/v1/systemone", no_instructions)
        first, second = self.log()
        self.assertEqual(first["authorization_header"], f"Bearer {KEY}")
        self.assertEqual(
            (first["user_agent"], first["x_nace_client"], first["idempotency_key"]),
            ("drex-duckdb/0.0.1", "drex-duckdb/0.0.1", "k1"),
        )
        self.assertEqual(first["content_type"], "application/json")
        self.assertEqual(
            (first["model"], first["served_model"], first["model_present"]), ("drex-v2.0", "drex-v2.0", True)
        )
        self.assertEqual(
            (first["state_type"], first["question_count"], first["instructions_present"]), ("string", 3, True)
        )
        self.assertEqual(json.loads(first["questions"])[0]["id"], "category")
        self.assertEqual(json.loads(first["body"]), SUPPORT_TICKET)
        self.assertEqual(
            (second["state_type"], second["instructions_present"], second["model_present"]), ("object", False, False)
        )


if __name__ == "__main__":
    unittest.main()
