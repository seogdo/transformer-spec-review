"""Offline contract/security regression tests, not a live AI quality benchmark."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from handoff_review import review_case
from handoff_review import explanation as module
from handoff_review.contracts import FIELD_KEYS

CASES = json.loads((ROOT / "fixtures" / "cases.json").read_text(encoding="utf-8"))["cases"]


class ExplanationTests(unittest.TestCase):
    def setUp(self):
        self.review = review_case(next(case for case in CASES if case["case_id"] == "D01"))
        self.facts, _ = module._facts(self.review)
        self.fan_index = FIELD_KEYS.index("fan_supply_voltage")

    def explain(self, plan):
        with patch.object(module, "_request_plan", return_value=plan):
            return module.explain_review(self.review, use_ai=True)

    def test_openai_explanation_uses_derived_facts_and_twenty_second_budget(self):
        config = module.AIConfig("openai", "explicit-model")
        with patch.object(module, "openai_request", return_value={"items": self.facts}) as api, patch.object(module, "_request_plan") as local:
            result = module.explain_review(self.review, use_ai=True, ai_config=config)
        local.assert_not_called()
        self.assertEqual(api.call_args.args[0], config)
        self.assertEqual(api.call_args.args[2], 20.0)
        sent = json.loads(api.call_args.args[1]["messages"][1]["content"])
        self.assertEqual(sent, {"items": self.facts})
        self.assertTrue(all(row["validation_status"] == "accepted" for row in result["items"]))
        self.assertEqual(result["ai_execution"]["calls_succeeded"], 1)

    def test_openai_disabled_makes_no_api_or_key_access(self):
        with patch.object(module, "openai_request") as api, patch("handoff_review.llm_backend._api_key", side_effect=AssertionError("key read")):
            result = module.explain_review(self.review, ai_config=module.AIConfig("openai", "explicit-model"))
        api.assert_not_called()
        self.assertEqual(result["ai_execution"]["execution_mode"], "not_requested")
        self.assertEqual(result["ai_execution"]["calls_attempted"], 0)

    def test_openai_failure_and_refusal_preserve_fixed_code_text(self):
        for error, expected in ((OSError("synthetic secret"), "unavailable"), (module.PlanRejected("refusal"), "rejected")):
            with self.subTest(expected=expected), patch.object(module, "openai_request", side_effect=error), patch.object(module, "_request_plan") as local:
                result = module.explain_review(self.review, use_ai=True, ai_config=module.AIConfig("openai", "explicit-model"))
            local.assert_not_called()
            self.assertEqual(result["ai_execution"]["calls_succeeded"], 0)
            self.assertTrue(all(row["validation_status"] == expected for row in result["items"]))
            self.assertNotIn("synthetic secret", json.dumps(result))

    def test_default_is_offline_and_preserves_input(self):
        before = deepcopy(self.review)
        with patch.object(module, "_request_plan", side_effect=AssertionError("network")):
            result = module.explain_review(self.review)
        self.assertEqual(self.review, before)
        self.assertFalse(result["llm_attempted"])
        self.assertTrue(all(row["origin"] == "code" and row["validation_status"] == "not_requested" for row in result["items"]))
        self.assertIn("440 V", result["items"][self.fan_index]["text"])
        self.assertIn("380 V", result["items"][self.fan_index]["text"])

    def test_valid_plan_has_only_code_rendered_sentences(self):
        result = self.explain({"items": deepcopy(self.facts)})
        default = module.explain_review(self.review)
        self.assertTrue(all(row["validation_status"] == "accepted" for row in result["items"]))
        self.assertEqual([row["text"] for row in result["items"]], [row["text"] for row in default["items"]])
        self.assertIn("고정 서식", result["note"])

    def test_changed_number_status_reason_and_source_are_rejected(self):
        for key, wrong in (("expected_text", "380 V"), ("status", "MATCH"), ("reason_code", "invented"), ("source_ids", ['["WRONG",1]'])):
            with self.subTest(key=key):
                rows = deepcopy(self.facts)
                rows[self.fan_index][key] = wrong
                result = self.explain({"items": rows})
                self.assertEqual(result["items"][self.fan_index]["validation_status"], "rejected")
                self.assertEqual(result["items"][self.fan_index]["origin"], "code")
                self.assertEqual(result["items"][0]["validation_status"], "accepted")

    def test_swapping_expected_and_actual_is_rejected(self):
        rows = deepcopy(self.facts)
        fan = rows[self.fan_index]
        fan["expected_text"], fan["actual_text"] = fan["actual_text"], fan["expected_text"]
        self.assertEqual(self.explain({"items": rows})["items"][self.fan_index]["validation_status"], "rejected")

    def test_draft_cannot_become_confirmed(self):
        self.review = review_case(next(case for case in CASES if case["case_id"] == "D05"))
        rows, _ = module._facts(self.review)
        rows[self.fan_index]["status"] = "MATCH"
        result = self.explain({"items": rows})
        self.assertEqual(result["items"][self.fan_index]["validation_status"], "rejected")
        self.assertIn("사람이 확인", result["items"][self.fan_index]["text"])

    def test_missing_duplicate_extra_and_free_prose_fieldsets_rejected(self):
        duplicate = deepcopy(self.facts)
        duplicate[-1] = deepcopy(duplicate[0])
        for plan in (None, [], {"items": self.facts[:-1]}, {"items": duplicate}, {"items": self.facts, "prose": "승인 완료"}):
            with self.subTest(plan=type(plan)):
                self.assertTrue(all(row["validation_status"] == "rejected" for row in self.explain(plan)["items"]))

    def test_extra_prose_in_row_is_never_visible(self):
        rows = deepcopy(self.facts)
        rows[-1]["text"] = "This unsafe free prose must not leak"
        result = self.explain({"items": rows})
        self.assertNotIn("unsafe", json.dumps(result))
        self.assertEqual(result["items"][-1]["validation_status"], "rejected")

    def test_transport_timeout_and_unavailable_use_code(self):
        for error in (TimeoutError("timeout"), ConnectionRefusedError("refused")):
            with patch.object(module, "_request_plan", side_effect=error):
                result = module.explain_review(self.review, use_ai=True)
            self.assertTrue(all(row["validation_status"] == "unavailable" for row in result["items"]))

    def test_invalid_engine_fieldset_raises_before_network(self):
        self.review["results"].pop()
        with patch.object(module, "_request_plan") as request:
            with self.assertRaises(ValueError):
                module.explain_review(self.review, use_ai=True)
            request.assert_not_called()

    def test_all_development_results_can_be_rendered(self):
        for case in CASES:
            with self.subTest(case=case["case_id"]):
                review = review_case(case)
                result = module.explain_review(review)
                self.assertEqual([row["field"] for row in result["items"]], list(FIELD_KEYS))
                for explanation, reference in zip(result["items"], review["results"]):
                    self.assertIn(reference["reason"], explanation["text"])


class LocalTransportTests(unittest.TestCase):
    def test_loopback_origins_only(self):
        for url, expected in (("http://127.0.0.1:11434", ("127.0.0.1", 11434)), ("http://localhost/", ("localhost", 11434)), ("http://[::1]:11435", ("::1", 11435))):
            self.assertEqual(module._endpoint(url), expected)
        for url in ("https://localhost", "http://example.com", "http://127.0.0.2", "http://127.0.0.1.evil", "http://localhost@evil", "http://user:pass@localhost", "http://localhost/api/chat", "http://localhost?x=1", "http://localhost#x", "http://localhost:0", "http://localhost:65536", "http://localhost:abc", "http://local host", "http://localhost\\@evil", "file:///etc/passwd"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                module._endpoint(url)

    def transport(self, response_body, status=200, length=None):
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = status
        response.getheader.return_value = length
        response.read1.side_effect = [response_body, b""]
        return connection

    def test_model_payload_and_complete_response(self):
        payload = {"done": True, "message": {"content": '{"items":[]}'}}
        connection = self.transport(json.dumps(payload).encode())
        with patch.object(module.http.client, "HTTPConnection", return_value=connection) as factory:
            result = module._request_plan([], module.DEFAULT_URL)
        self.assertEqual(result, {"items": []})
        factory.assert_called_once_with("127.0.0.1", port=11434, timeout=20.0)
        sent = json.loads(connection.request.call_args.kwargs["body"])
        self.assertEqual(sent["model"], "qwen3.5:2b")
        self.assertFalse(sent["think"])
        self.assertFalse(sent["stream"])
        self.assertEqual(sent["options"]["temperature"], 0)
        self.assertEqual(sent["options"]["num_ctx"], 8192)
        self.assertEqual(sent["options"]["num_predict"], 180 * len(FIELD_KEYS))
        connection.close.assert_called_once()

    def test_redirect_is_not_followed(self):
        connection = self.transport(b"", status=302)
        with patch.object(module.http.client, "HTTPConnection", return_value=connection) as factory:
            with self.assertRaises(OSError):
                module._request_plan([], module.DEFAULT_URL)
        self.assertEqual(factory.call_count, 1)
        connection.close.assert_called_once()

    def test_localhost_name_is_pinned_to_literal_loopback(self):
        connection = self.transport(b'{"done":true,"message":{"content":"{\\"items\\":[]}"}}')
        with patch.object(module.http.client, "HTTPConnection", return_value=connection) as factory:
            module._request_plan([], "http://localhost:11434")
        factory.assert_called_once_with("127.0.0.1", port=11434, timeout=20.0)

    def test_response_limits_declared_and_actual(self):
        for body, length in ((b"", str(module.MAX_RESPONSE_BYTES + 1)), (b"x" * (module.MAX_RESPONSE_BYTES + 1), None), (b"", "-1"), (b"", "bad")):
            connection = self.transport(body, length=length)
            with patch.object(module.http.client, "HTTPConnection", return_value=connection):
                with self.assertRaises(module.PlanRejected):
                    module._request_plan([], module.DEFAULT_URL)
            connection.close.assert_called_once()

    def test_incomplete_invalid_and_duplicate_json_are_rejected(self):
        for body in (b"not json", b'{"done":false}', b'{"done":true,"done":false}', b'{"done":true,"message":{"content":"NaN"}}', b'{"done":true,"message":{"content":"{\\"items\\":[],\\"items\\":[]}"}}'):
            connection = self.transport(body)
            with patch.object(module.http.client, "HTTPConnection", return_value=connection):
                with self.assertRaises(module.PlanRejected):
                    module._request_plan([], module.DEFAULT_URL)

    def test_time_budget_checked_and_connection_closed(self):
        connection = self.transport(b"")
        with patch.object(module.http.client, "HTTPConnection", return_value=connection), patch.object(module.time, "monotonic", side_effect=[0, 21]):
            with self.assertRaises(TimeoutError):
                module._request_plan([], module.DEFAULT_URL)
        connection.close.assert_called_once()

    def test_deadline_shutdown_retains_socket_when_connection_closes(self):
        connection = self.transport(b"")
        active_socket = connection.sock
        response = connection.getresponse.return_value
        callbacks = []

        def make_timer(interval, callback):
            callbacks.append(callback)
            return MagicMock()

        def body_read(size):
            connection.sock = None
            callbacks[0]()
            return b""

        response.read1.side_effect = body_read
        with patch.object(module.http.client, "HTTPConnection", return_value=connection), patch.object(module.threading, "Timer", side_effect=make_timer):
            with self.assertRaises(TimeoutError):
                module._request_plan([], module.DEFAULT_URL)
        active_socket.shutdown.assert_called_once_with(module.socket.SHUT_RDWR)
        connection.close.assert_called_once()

    def test_oversized_request_never_opens_connection(self):
        with patch.object(module.http.client, "HTTPConnection") as factory:
            with self.assertRaises(ValueError):
                module._request_plan([{"value": "x" * module.MAX_REQUEST_BYTES}], module.DEFAULT_URL)
        factory.assert_not_called()

    def test_bad_environment_cannot_connect_externally(self):
        review = review_case(CASES[0])
        with patch.dict(module.os.environ, {"HANDOFF_OLLAMA_URL": "http://external.example:11434"}), patch.object(module.http.client, "HTTPConnection") as factory:
            result = module.explain_review(review, use_ai=True)
        factory.assert_not_called()
        self.assertTrue(all(row["validation_status"] == "unavailable" for row in result["items"]))


if __name__ == "__main__":
    unittest.main()
