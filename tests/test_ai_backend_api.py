"""Provider/consent orchestration with temporary storage and injected responses.

No API key, live model, provider network request or user upload is used here.
The public config double deliberately contains no credential value.
"""
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import test_extraction_api as fixtures
from handoff_review.web_app import create_app, write_json, EXTERNAL_AI_CONSENT


@dataclass(frozen=True)
class PublicConfig:
    provider: str = "openai"
    model: str = "test-api-model"
    ready: bool = True

    def public_info(self):
        return {"provider": self.provider, "model": self.model,
                "configuration_ready": self.ready, "external_processing": self.provider == "openai",
                "key_configured": self.ready if self.provider == "openai" else None,
                "configuration_error": None if self.ready else "MISSING_OPENAI_API_KEY",
                "config_signature": hashlib.sha256(f"{self.provider}\0{self.model}".encode()).hexdigest(),
                "model_readiness_checked": False}


class AIBackendApiTests(unittest.TestCase):
    upload = fixtures.ExtractionApiTests.upload
    wait = fixtures.ExtractionApiTests.wait
    body = fixtures.ExtractionApiTests.body

    def setUp(self):
        self.temp = TemporaryDirectory(prefix="handoff-ai-provider-api-")
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.config = PublicConfig()
        self.app = create_app(self.data, ai_config=self.config)
        self.app.state.extract_upload = fixtures.unavailable
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.consent = {"X-AI-Consent": EXTERNAL_AI_CONSENT,
                        "X-AI-Configuration": self.config.public_info()["config_signature"]}

    def start(self, uploaded, key=None):
        headers = {**self.consent, **({"Idempotency-Key": key} if key else {})}
        return self.client.post(f"/api/uploads/{uploaded['upload_id']}/extractions", headers=headers)

    def saved_review(self):
        uploaded = self.upload(mode="template", case="D01")
        result = self.client.post("/api/reviews", json=self.body(uploaded))
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def test_health_exposes_only_public_configuration_and_keeps_startup_snapshot(self):
        with patch("handoff_review.web_app.get_ai_config", return_value=self.config):
            app = create_app(self.data)
        with patch("handoff_review.web_app.get_ai_config", side_effect=AssertionError("No re-resolution")):
            with TestClient(app) as client:
                health = client.get("/api/health").json()
        self.assertEqual(health["provider"], "openai")
        self.assertEqual(health["model"], self.config.model)
        self.assertTrue(health["key_configured"])
        self.assertFalse(health["model_readiness_checked"])
        self.assertNotIn("api_key", health)

    def test_extraction_requires_exact_single_consent_header_before_worker_or_job(self):
        uploaded = self.upload()
        for headers in ({}, {"X-AI-Consent": "yes"}, [("X-AI-Consent", EXTERNAL_AI_CONSENT), ("X-AI-Consent", EXTERNAL_AI_CONSENT)]):
            response = self.client.post(f"/api/uploads/{uploaded['upload_id']}/extractions", headers=headers)
            self.assertEqual(response.status_code, 403, response.text)
        self.assertFalse((self.data / "extractions").exists())
        self.assertEqual(len(list((self.data / "sets").iterdir())), 1)

    def test_unconfigured_api_keeps_upload_and_read_routes_available_but_starts_no_ai(self):
        app = create_app(self.data, ai_config=PublicConfig(ready=False))
        app.state.extract_upload = lambda _: self.fail("Unconfigured worker must not run")
        uploaded = self.upload()
        with TestClient(app) as client:
            health = client.get("/api/health").json()
            self.assertFalse(health["configuration_ready"])
            self.assertEqual(client.get(f"/api/uploads/{uploaded['upload_id']}").status_code, 200)
            blocked = client.post(f"/api/uploads/{uploaded['upload_id']}/extractions", headers=self.consent)
            self.assertEqual(blocked.status_code, 503)
        self.assertFalse((self.data / "extractions").exists())

    def test_missing_stale_or_duplicate_configuration_header_blocks_new_requests(self):
        uploaded = self.upload()
        path = f"/api/uploads/{uploaded['upload_id']}/extractions"
        for headers in ({"X-AI-Consent": EXTERNAL_AI_CONSENT},
                        {**self.consent, "X-AI-Configuration": "stale"},
                        [*self.consent.items(), ("X-AI-Configuration", self.consent["X-AI-Configuration"])]):
            response = self.client.post(path, headers=headers)
            self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse((self.data / "extractions").exists())

    def test_open_browser_configuration_cannot_authorize_a_new_model_after_restart(self):
        uploaded = self.upload()
        new_config = PublicConfig(model="different-model")
        app = create_app(self.data, ai_config=new_config)
        app.state.extract_upload = fixtures.unavailable
        with TestClient(app) as client:
            path = f"/api/uploads/{uploaded['upload_id']}/extractions"
            response = client.post(path, headers=self.consent)
            self.assertEqual(response.status_code, 409)
            health = client.get("/api/health").json()
            response = client.post(path, headers={**self.consent, "X-AI-Configuration": health["config_signature"]})
            self.assertEqual(response.status_code, 202)

    def test_injected_executor_keeps_original_signature_and_is_never_labelled_as_openai(self):
        uploaded = self.upload()
        response = self.start(uploaded)
        self.assertEqual(response.status_code, 202)
        job = self.wait(response.json()["job_id"])
        self.assertEqual(job["status"], "completed")
        for info in (job["ai_execution"], job["upload"]["ai_execution"], job["extraction"]["ai_execution"]):
            self.assertEqual(info["provider"], "injected")
            self.assertEqual(info["execution_mode"], "injected")
            self.assertIsNone(info["model"])
            self.assertFalse(info["external_processing"])
        review = self.client.post("/api/reviews", json=self.body(job["upload"])).json()
        self.assertEqual(review["ai_execution"]["provider"], "injected")

    def test_default_wrapper_passes_snapshot_and_preserves_actual_method_and_execution(self):
        from handoff_review.web_app import extract_text_upload
        self.app.state.extract_upload = extract_text_upload
        seen = []
        def extracted(documents, *, ai_config):
            seen.append(ai_config)
            result = fixtures.unavailable(documents)
            result.update(method="openai_tool_rag", ai_execution={"provider": "openai", "model": self.config.model,
                          "execution_mode": "live_request", "calls_attempted": 2, "calls_succeeded": 2,
                          "external_processing": True})
            return result
        uploaded = self.upload()
        with patch("handoff_review.extraction_agent.extract_upload", side_effect=extracted):
            response = self.start(uploaded)
            job = self.wait(response.json()["job_id"])
        self.assertEqual(seen, [self.config])
        self.assertEqual(job["ai_execution"]["calls_succeeded"], 2)
        self.assertTrue(all(d["extraction_method"] == "openai_tool_rag" for d in job["upload"]["documents"]))
        review = self.client.post("/api/reviews", json=self.body(job["upload"])).json()
        self.assertEqual(review["extraction_method"], "openai_tool_rag")
        self.assertEqual(review["ai_execution"], job["ai_execution"])

    def test_idempotency_cannot_reuse_a_job_under_another_provider_or_model(self):
        uploaded = self.upload(); key = "a" * 32
        initial = self.start(uploaded, key)
        job = self.wait(initial.json()["job_id"])
        for config in (PublicConfig(model="different-model"), PublicConfig(provider="ollama", model="test-local")):
            app = create_app(self.data, ai_config=config)
            with TestClient(app) as client:
                response = client.post(f"/api/uploads/{uploaded['upload_id']}/extractions",
                                       headers={**self.consent, "Idempotency-Key": key})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(client.get(f"/api/extractions/{job['job_id']}").json(), job)
        self.assertEqual(len(list((self.data / "extractions").iterdir())), 1)

    def test_legacy_idempotency_mapping_is_only_compatible_with_ollama(self):
        uploaded = self.upload(); key = "b" * 32; job_id = "c" * 32
        record = {"job_id": job_id, "source_upload_id": uploaded["upload_id"], "status": "failed", "error": "legacy"}
        write_json(self.data / "extractions" / f"{job_id}.json", record)
        write_json(self.data / "extraction_requests" / f"{key}.json", {"job_id": job_id, "upload_id": uploaded["upload_id"]})
        self.assertEqual(self.start(uploaded, key).status_code, 409)
        with TestClient(create_app(self.data, ai_config=PublicConfig("ollama", "test-local"))) as client:
            response = client.post(f"/api/uploads/{uploaded['upload_id']}/extractions", headers={"Idempotency-Key": key})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json(), record)

    def test_failed_and_interrupted_workers_keep_provenance(self):
        def failed(_):
            raise RuntimeError("simulated failure")
        self.app.state.extract_upload = failed
        job = self.wait(self.start(self.upload()).json()["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["ai_execution"]["provider"], "injected")
        job_id = "d" * 32
        execution = {"provider": "ollama", "model": "old-local", "execution_mode": "live_request"}
        write_json(self.data / "extractions" / f"{job_id}.json", {"job_id": job_id, "status": "running", "ai_execution": execution})
        restarted = self.client.get(f"/api/extractions/{job_id}").json()
        self.assertEqual(restarted["status"], "failed")
        self.assertEqual(restarted["ai_execution"], execution)

    def test_optional_explanation_requires_consent_and_does_not_relabel_extraction(self):
        review = self.saved_review()
        called = []
        def injected(record, use_ai):
            called.append(use_ai)
            return {"items": [], "llm_attempted": use_ai}
        self.app.state.explain_review = injected
        path = f"/api/reviews/{review['review_id']}/explain"
        self.assertEqual(self.client.post(path, json={"use_ai": True}).status_code, 403)
        self.assertEqual(called, [])
        self.assertEqual(self.client.post(path, json={"use_ai": True}, headers={**self.consent, "X-AI-Configuration": "stale"}).status_code, 409)
        self.assertEqual(called, [])
        response = self.client.post(path, json={"use_ai": True}, headers=self.consent)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["ai_execution"]["provider"], "injected")
        after = self.client.get(f"/api/reviews/{review['review_id']}").json()
        self.assertEqual(after["ai_execution"], review["ai_execution"])
        self.assertEqual(after["results"], review["results"])

    def test_code_only_explanation_and_historical_reads_do_not_require_external_consent(self):
        review = self.saved_review()
        old = {**review, "ai_execution": {"provider": "ollama", "model": "historical-local", "execution_mode": "live_request"}}
        write_json(self.data / "reviews" / f"{review['review_id']}.json", old)
        self.assertEqual(self.client.get(f"/api/reviews/{review['review_id']}/export.json").json(), old)
        response = self.client.post(f"/api/reviews/{review['review_id']}/explain", json={"use_ai": False})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["llm_attempted"])
        saved = self.client.get(f"/api/reviews/{review['review_id']}").json()
        self.assertEqual(saved["ai_execution"], old["ai_execution"])


if __name__ == "__main__":
    unittest.main()
