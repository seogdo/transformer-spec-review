"""Offline backend/auth/transport tests. All credentials and responses are fake."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict
import json
from pathlib import Path
import sys
import threading
import time
import traceback
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from handoff_review import llm_backend as backend

FAKE_KEY = "synthetic-unit-test-credential"


def payload():
    return {"model": "ignored-local-model", "stream": False, "think": False,
            "messages": [{"role": "system", "content": "Return JSON."}, {"role": "user", "content": "synthetic source"}],
            "format": {"type": "object", "additionalProperties": False, "required": ["items"], "properties": {
                "items": {"type": "array", "minItems": 1, "maxItems": 30, "items": {
                    "type": "object", "additionalProperties": False, "required": ["value", "page"],
                    "properties": {"value": {"type": "string", "minLength": 1, "maxLength": 100},
                                   "page": {"type": "integer", "minimum": 1, "maximum": 20}}}}}},
            "options": {"num_predict": 4000, "temperature": 0, "num_ctx": 16384}, "keep_alive": "5m"}


def envelope(content='{"items": []}', **changes):
    choice = {"finish_reason": "stop", "message": {"role": "assistant", "content": content, "refusal": None}}
    choice.update(changes)
    return json.dumps({"choices": [choice]}).encode()


class ConfigurationTests(unittest.TestCase):
    def test_default_and_explicit_ollama_model(self):
        with patch.dict(backend.os.environ, {}, clear=True):
            config = backend.get_ai_config()
        self.assertEqual(config, backend.AIConfig("ollama", "qwen3.5:2b"))
        with patch.dict(backend.os.environ, {"HANDOFF_OLLAMA_MODEL": "custom:2b"}, clear=True):
            self.assertEqual(backend.get_ai_config().model, "custom:2b")

    def test_openai_has_no_implicit_model_and_no_secret_in_config(self):
        with patch.dict(backend.os.environ, {"LLM_BACKEND": "openai", "OPENAI_API_KEY": FAKE_KEY}, clear=True):
            config = backend.get_ai_config()
            info = config.public_info()
        self.assertEqual(asdict(config), {"provider": "openai", "model": ""})
        self.assertFalse(info["configuration_ready"])
        self.assertEqual(info["configuration_error"], "MISSING_OPENAI_MODEL")
        self.assertTrue(info["key_configured"])
        self.assertNotIn(FAKE_KEY, repr(config) + json.dumps(info))
        with self.assertRaises(FrozenInstanceError):
            config.model = "other"

    def test_configured_does_not_claim_model_or_auth_verified(self):
        with patch.dict(backend.os.environ, {"OPENAI_API_KEY": FAKE_KEY}, clear=True):
            info = backend.AIConfig("openai", "explicit-test-model").public_info()
        self.assertTrue(info["configuration_ready"])
        self.assertFalse(info["model_readiness_checked"])
        self.assertTrue(info["external_processing"])

    def test_missing_and_malformed_keys_never_appear_in_public_state(self):
        for key in ("", " \t ", "bad\r\nAuthorization: injected", "한글", "x" * 8193):
            with self.subTest(length=len(key)), patch.dict(backend.os.environ, {"OPENAI_API_KEY": key}, clear=True):
                info = backend.AIConfig("openai", "explicit-model").public_info()
                self.assertFalse(info["configuration_ready"])
                self.assertEqual(info["configuration_error"], "MISSING_OPENAI_API_KEY")

    def test_invalid_backend_and_model_are_rejected_without_echo(self):
        for provider, model in (("unexpected", "ok"), ("openai", "bad\nmodel"), ("openai", "Bearer secret")):
            with self.subTest(provider=provider), self.assertRaises(ValueError) as error:
                backend.AIConfig(provider, model)
            self.assertNotIn(model, str(error.exception))

    def test_injected_and_not_requested_are_not_external_requests(self):
        config = backend.AIConfig("openai", "explicit-model")
        self.assertEqual(backend.execution_info(config, injected=True)["execution_mode"], "injected")
        self.assertFalse(backend.execution_info(config, injected=True)["external_processing"])
        self.assertEqual(backend.execution_info(config, requested=False)["execution_mode"], "not_requested")
        self.assertFalse(backend.execution_info(config, requested=False)["external_processing"])

    def test_configuration_signature_tracks_only_provider_and_model(self):
        with patch.dict(backend.os.environ, {}, clear=True):
            first = backend.AIConfig("openai", "model-one").public_info()["config_signature"]
        with patch.dict(backend.os.environ, {"OPENAI_API_KEY": FAKE_KEY}, clear=True):
            same = backend.AIConfig("openai", "model-one").public_info()["config_signature"]
            changed = backend.AIConfig("openai", "model-two").public_info()["config_signature"]
            local = backend.AIConfig("ollama", "model-one").public_info()["config_signature"]
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertEqual(first, same)
        self.assertNotEqual(first, changed)
        self.assertNotEqual(first, local)


class DNSDeadlineTests(unittest.TestCase):
    def test_real_https_transport_stops_before_late_dns_can_open_socket(self):
        release, finished = threading.Event(), threading.Event()
        def slow_lookup(*args):
            try:
                release.wait(1)
                return [(backend.socket.AF_INET, backend.socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))]
            finally:
                finished.set()
        with patch.dict(backend.os.environ, {"OPENAI_API_KEY": FAKE_KEY}, clear=True), patch.object(backend.socket, "getaddrinfo", side_effect=slow_lookup), patch.object(backend.socket, "socket") as sockets:
            started = time.monotonic()
            try:
                with self.assertRaises(TimeoutError):
                    backend.openai_request(backend.AIConfig("openai", "explicit-model"), payload(), 0.03)
                self.assertLess(time.monotonic() - started, 0.2)
            finally:
                release.set()
                self.assertTrue(finished.wait(1))
            sockets.assert_not_called()

    def test_late_dns_result_never_connects_after_timeout(self):
        release = threading.Event()
        finished = threading.Event()
        addresses = [(backend.socket.AF_INET, backend.socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))]
        def slow_lookup(*args):
            try:
                release.wait(1)
                return addresses
            finally:
                finished.set()
        with patch.object(backend.socket, "getaddrinfo", side_effect=slow_lookup), patch.object(backend.socket, "socket") as socket_factory:
            started = time.monotonic()
            try:
                with self.assertRaises(TimeoutError):
                    backend._bounded_socket(("api.openai.com", 443), started + 0.03)
                self.assertLess(time.monotonic() - started, 0.2)
            finally:
                release.set()
                self.assertTrue(finished.wait(1))
            socket_factory.assert_not_called()

    def test_connect_and_tls_receive_only_remaining_budget(self):
        addresses = [(backend.socket.AF_INET, backend.socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))]
        active = MagicMock()
        with patch.object(backend.socket, "getaddrinfo", return_value=addresses), patch.object(backend.socket, "socket", return_value=active), patch.object(backend.time, "monotonic", side_effect=[0, 1, 2, 3, 4]):
            self.assertIs(backend._bounded_socket(("api.openai.com", 443), 10), active)
        self.assertEqual([call.args[0] for call in active.settimeout.call_args_list], [7, 6])
        active.connect.assert_called_once_with(("192.0.2.1", 443))

    def test_expired_or_disallowed_address_never_resolves(self):
        with patch.object(backend.socket, "getaddrinfo") as lookup:
            with self.assertRaises(TimeoutError):
                backend._bounded_socket(("api.openai.com", 443), time.monotonic() - 1)
            with self.assertRaises(ValueError):
                backend._bounded_socket(("evil.invalid", 443), time.monotonic() + 1)
        lookup.assert_not_called()

    def test_resolver_worker_limit_fails_closed_without_creating_more_workers(self):
        slots = threading.BoundedSemaphore(1)
        slots.acquire()
        with patch.object(backend, "_DNS_SLOTS", slots), patch.object(backend.threading, "Thread") as workers:
            with self.assertRaises(OSError):
                backend._bounded_socket(("api.openai.com", 443), time.monotonic() + 1)
        workers.assert_not_called()


class OpenAITransportTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(backend.os.environ, {"OPENAI_API_KEY": FAKE_KEY}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = backend.AIConfig("openai", "explicit-test-model")

    def transport(self, body=None, status=200, length=None):
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = status
        response.getheader.return_value = length
        response.read1.side_effect = [envelope() if body is None else body, b""]
        return connection

    def invoke(self, connection, request=None, timeout=20):
        with patch.object(backend.http.client, "HTTPSConnection", return_value=connection):
            return backend.openai_request(self.config, request or payload(), timeout)

    def test_fixed_tls_endpoint_headers_and_only_openai_payload_fields(self):
        connection = self.transport()
        original = payload()
        before = deepcopy(original)
        with patch.dict(backend.os.environ, {"OPENAI_BASE_URL": "https://evil.invalid", "HTTPS_PROXY": "http://evil.invalid"}), patch.object(backend.http.client, "HTTPSConnection", return_value=connection) as factory:
            result = backend.openai_request(self.config, original, 20)
        self.assertEqual(result, {"items": []})
        self.assertEqual(original, before)
        factory.assert_called_once_with("api.openai.com", port=443, timeout=20.0)
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ("POST", "/v1/chat/completions"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + FAKE_KEY)
        sent = json.loads(kwargs["body"])
        self.assertEqual(set(sent), {"model", "messages", "stream", "store", "max_completion_tokens", "response_format"})
        self.assertEqual(sent["model"], self.config.model)
        self.assertFalse(sent["store"])
        self.assertFalse(sent["stream"])
        self.assertEqual(sent["max_completion_tokens"], 4000)
        self.assertNotIn(FAKE_KEY, kwargs["body"].decode())
        self.assertEqual(sent["response_format"]["type"], "json_schema")
        self.assertTrue(sent["response_format"]["json_schema"]["strict"])
        schema = sent["response_format"]["json_schema"]["schema"]
        self.assertFalse(schema["additionalProperties"])
        for constraint in ("minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum"):
            self.assertNotIn(constraint, json.dumps(schema))
        connection.close.assert_called_once()

    def test_schema_property_names_are_not_removed_as_constraints(self):
        schema = {"type": "object", "additionalProperties": False, "required": ["minLength"],
                  "properties": {"minLength": {"type": "string", "maxLength": 10}}}
        self.assertIn("minLength", backend._compatible_schema(schema)["properties"])

    def test_missing_configuration_opens_no_connection(self):
        with patch.object(backend.http.client, "HTTPSConnection") as factory:
            with patch.dict(backend.os.environ, {}, clear=True), self.assertRaises(ValueError):
                backend.openai_request(self.config, payload(), 20)
            with self.assertRaises(ValueError):
                backend.openai_request(backend.AIConfig("openai", ""), payload(), 20)
        factory.assert_not_called()

    def test_invalid_or_oversized_request_opens_no_connection(self):
        for change in ("size", "tokens", "role", "schema"):
            request = payload()
            if change == "size":
                request["messages"][1]["content"] = "x" * backend.MAX_REQUEST_BYTES
            elif change == "tokens":
                request["options"]["num_predict"] = True
            elif change == "role":
                request["messages"][0]["role"] = "tool"
            else:
                request["format"]["additionalProperties"] = True
            with self.subTest(change=change), patch.object(backend.http.client, "HTTPSConnection") as factory:
                with self.assertRaises(ValueError):
                    backend.openai_request(self.config, request, 20)
                factory.assert_not_called()

    def test_nonpositive_nonfinite_deadlines_never_connect(self):
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), patch.object(backend.http.client, "HTTPSConnection") as factory:
                with self.assertRaises(TimeoutError):
                    backend.openai_request(self.config, payload(), timeout)
                factory.assert_not_called()

    def test_redirect_auth_and_rate_limit_do_not_follow_retry_or_read_body(self):
        for status in (301, 302, 307, 308, 401, 403, 429, 500):
            connection = self.transport(body=FAKE_KEY.encode(), status=status)
            with self.subTest(status=status), self.assertRaises(OSError) as error:
                self.invoke(connection)
            self.assertNotIn(FAKE_KEY, str(error.exception))
            connection.request.assert_called_once()
            connection.getresponse.return_value.read1.assert_not_called()
            connection.close.assert_called_once()

    def test_transport_exception_does_not_echo_credentials(self):
        connection = self.transport()
        connection.request.side_effect = ValueError("Authorization: Bearer " + FAKE_KEY)
        with self.assertRaises(OSError) as error:
            self.invoke(connection)
        rendered = "".join(traceback.format_exception(error.exception))
        self.assertNotIn(FAKE_KEY, rendered)

    def test_refusal_incomplete_and_unexpected_tool_calls_are_rejected(self):
        bad = [envelope(finish_reason=reason) for reason in ("length", "content_filter", "tool_calls", None)]
        bad += [envelope(message={"role": "assistant", "content": "{}", "refusal": "declined"}),
                envelope(message={"role": "assistant", "content": "{}", "tool_calls": [{}]}),
                envelope(message={"role": "user", "content": "{}"}),
                envelope(message={"role": "assistant", "content": None}),
                b'{"choices":[]}', b'{"choices":[{},{}]}', b'[]']
        for body in bad:
            with self.subTest(body=body), self.assertRaises(backend.PlanRejected):
                self.invoke(self.transport(body))

    def test_invalid_duplicate_and_nonfinite_json_are_rejected(self):
        for body in (b'not JSON', b'{"choices":[],"choices":[]}', envelope('{"items":[],"items":[]}'), envelope('{"x":NaN}'), envelope('[]')):
            with self.subTest(body=body), self.assertRaises(backend.PlanRejected):
                self.invoke(self.transport(body))

    def test_declared_actual_and_truncated_response_sizes(self):
        for body, length in ((b"", "bad"), (b"", "-1"), (b"", str(backend.MAX_RESPONSE_BYTES + 1)),
                             (b"x" * (backend.MAX_RESPONSE_BYTES + 1), None), (envelope(), str(len(envelope()) + 1))):
            with self.subTest(length=length), self.assertRaises(backend.PlanRejected):
                self.invoke(self.transport(body, length=length))

    def test_shared_deadline_and_connection_cleanup(self):
        connection = self.transport()
        with patch.object(backend.time, "monotonic", side_effect=[0, 21]), self.assertRaises(TimeoutError):
            self.invoke(connection)
        connection.close.assert_called_once()

    def test_deadline_retains_socket_after_connection_close_header(self):
        connection = self.transport()
        active_socket = connection.sock
        callbacks = []
        def make_timer(interval, callback):
            callbacks.append(callback)
            return MagicMock()
        def body_read(size):
            connection.sock = None
            callbacks[0]()
            return b""
        connection.getresponse.return_value.read1.side_effect = body_read
        with patch.object(backend.threading, "Timer", side_effect=make_timer), self.assertRaises(TimeoutError):
            self.invoke(connection)
        active_socket.shutdown.assert_called_once_with(backend.socket.SHUT_RDWR)
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
