"""Local API integration tests using real sample bytes and isolated storage.

All normal uploads invoke the real PDF worker subprocess with this interpreter.
Tests never start a listening web server or Ollama, never read manual fixture
answers, and never use the project's uploads directory. Existing PDFs are reused
byte-for-byte; only application-owned copies in TemporaryDirectory are created.
AI transport is mocked where noted, while its validation/rendering remains real.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from handoff_review.contracts import FIELD_KEYS
from handoff_review.web_app import BODY_LIMIT, FILE_LIMIT, create_app, write_json

SAMPLES = ROOT / "output" / "pdf" / "samples"
SLOTS = (
    ("spec_file", "customer_spec", "01_customer_spec.pdf"),
    ("amendment_file", "amendment", "02_amendment.pdf"),
    ("handoff_file", "handoff", "03_handoff.pdf"),
)


class WebAppIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pdf_bytes = {
            case: {slot: (SAMPLES / case / filename).read_bytes() for slot, _role, filename in SLOTS}
            for case in ("D01", "D02")
        }

    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="handoff-api-test-")
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name) / "app-data"
        self.app = create_app(self.data)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def files(self, case="D01"):
        return [(slot, (filename, self.pdf_bytes[case][slot], "application/pdf")) for slot, _role, filename in SLOTS]

    def upload(self, case="D01"):
        response = self.client.post("/api/uploads", files=self.files(case))
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def review(self, upload=None, case="D01"):
        uploaded = upload or self.upload(case)
        response = self.client.post("/api/reviews", json={
            "upload_id": uploaded["upload_id"], "policy_confirmed": True,
            "approved_amendment_ids": ["AMD-001"],
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def assert_no_committed_upload(self):
        self.assertEqual(list(self.data.rglob("manifest.json")), [])
        self.assertEqual(list(self.data.rglob("*.pdf")), [])
        self.assertEqual(list(self.data.glob(".incoming-*")), [])

    def test_health_is_explicit_about_model_not_being_checked(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["official_approval"])
        self.assertFalse(response.json()["model_readiness_checked"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_page_references_served_static_assets(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        for filename, media_type in (("app.css", "text/css"), ("app.js", "javascript")):
            asset_path = f"/assets/{filename}"
            self.assertIn(f'"{asset_path}"', page.text)
            response = self.client.get(asset_path)
            self.assertEqual(response.status_code, 200)
            self.assertIn(media_type, response.headers["content-type"])
            self.assertGreater(len(response.content), 100)
        self.assertEqual(self.client.get("/assets/unknown.txt").status_code, 404)
        self.assertIn("10개 항목 검토하기", page.text)
        self.assertIn('id="review-field-note"', page.text)
        self.assertIn('id="results-caption"', page.text)
        self.assertIn('id="demo-v03"', page.text)
        self.assertNotIn("8개 항목 검토하기", page.text)
        script = self.client.get("/assets/app.js").text
        self.assertIn('tap_changer_type: "탭절환기 방식"', script)
        self.assertIn('installation_type: "설치 구분"', script)
        self.assertIn("review.results.length", script)
        self.assertIn("검토가 포함되지 않았습니다", script)

    def test_sample_links_download_the_existing_pdf_bytes(self):
        listing = self.client.get("/api/samples")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual({item["id"] for item in listing.json()["cases"]}, {"D01", "D02", "C01", "V01", "V02", "V03"})
        response = self.client.get("/api/samples/D01/customer_spec.pdf")
        self.assertEqual(response.content, self.pdf_bytes["D01"]["spec_file"])
        self.assertIn("application/pdf", response.headers["content-type"])

    def test_v03_sample_links_serve_real_text_mode_pdfs(self):
        samples = self.client.get("/api/samples").json()["cases"]
        sample = next(item for item in samples if item["id"] == "V03")
        self.assertEqual(sample["mode"], "text_ai")
        self.assertEqual(len(sample["files"]), 3)
        for file in sample["files"]:
            response = self.client.get(file["url"])
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, (ROOT / "output/pdf/varied/V03" / file["filename"]).read_bytes())

    def test_real_d01_upload_policy_review_has_correct_values(self):
        uploaded = self.upload()
        self.assertRegex(uploaded["upload_id"], r"^[0-9a-f]{32}$")
        self.assertEqual([doc["role"] for doc in uploaded["documents"]], ["customer_spec", "amendment", "handoff"])
        self.assertEqual([doc["extraction_method"] for doc in uploaded["documents"]], ["template_text"] * 3)
        result = self.review(uploaded)
        self.assertEqual(result["summary"], {"MATCH": 9, "MISMATCH": 1, "MISSING": 0, "NEEDS_REVIEW": 0})
        fan = next(row for row in result["results"] if row["field"] == "fan_supply_voltage")
        self.assertEqual((fan["expected"]["value"], fan["actual"]["value"]), ("440", "380"))
        self.assertEqual({source["document_id"] for source in fan["sources"]}, {"AMD-001", "HANDOFF-001"})
        self.assertTrue(result["policy_confirmed"])
        self.assertFalse(result["llm_used_for_verdict"])
        self.assertEqual(result["record_kind"], "research_review_not_approval")
        self.assertFalse(result["explanations"]["llm_attempted"])

    def test_real_d02_unit_equivalence_and_amendment_are_all_match(self):
        result = self.review(case="D02")
        self.assertEqual(result["summary"], {"MATCH": 10, "MISMATCH": 0, "MISSING": 0, "NEEDS_REVIEW": 0})
        rated = next(row for row in result["results"] if row["field"] == "rated_power")
        self.assertEqual(rated["actual"]["value"], "1000")
        self.assertEqual(rated["actual"]["unit"], "kVA")

    def test_new_categories_have_values_sources_and_exported_rows(self):
        self.assertEqual(len(FIELD_KEYS), 10)
        for case in ("D01", "D02"):
            with self.subTest(case=case):
                result = self.review(case=case)
                self.assertEqual({row["field"] for row in result["results"]}, set(FIELD_KEYS))
                for key, value in (("tap_changer_type", "DETC"), ("installation_type", "옥내형")):
                    row = next(row for row in result["results"] if row["field"] == key)
                    self.assertEqual(row["status"], "MATCH")
                    self.assertEqual(row["expected"]["value"], value)
                    self.assertEqual(row["actual"]["value"], value)
                    self.assertEqual(row["expected"]["unit"], "")
                    self.assertEqual(row["actual"]["unit"], "")
                    self.assertTrue(row["evidence_complete"])
                    self.assertTrue(row["sources"])
                    for proof in row["sources"]:
                        self.assertIn(proof["quote"], self.client.get(proof["page_url"]).json()["text"])
                exported = self.client.get(f"/api/reviews/{result['review_id']}/export.json").json()
                self.assertEqual(exported["results"], result["results"])
                self.assertEqual(len(exported["results"]), 10)

    def test_evidence_points_to_actual_saved_pdf_page_text_and_hash(self):
        result = self.review()
        docs = {doc["id"]: doc for doc in result["documents"]}
        for row in result["results"]:
            self.assertTrue(row["evidence_complete"])
            for proof in row["sources"]:
                self.assertTrue(proof["verified_in_page"])
                self.assertEqual(proof["origin"], "code_bound_pdf_page")
                page = self.client.get(proof["page_url"])
                self.assertEqual(page.status_code, 200)
                self.assertEqual(page.json()["page"], proof["page"])
                self.assertIn(proof["quote"], page.json()["text"])
                self.assertEqual(page.json()["sha256"], docs[proof["document_id"]]["sha256"])
                self.assertEqual(page.json()["source"], "saved_pdf_text")

    def test_original_pdf_download_is_byte_identical_not_regenerated(self):
        uploaded = self.upload()
        for doc, (slot, _role, _filename) in zip(uploaded["documents"], SLOTS):
            response = self.client.get(f"/api/uploads/{uploaded['upload_id']}/documents/{doc['id']}/pdf")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, self.pdf_bytes["D01"][slot])
            self.assertEqual(hashlib.sha256(response.content).hexdigest(), doc["sha256"])
            self.assertIn("inline", response.headers["content-disposition"])

    def test_export_and_new_app_restore_preserve_review_and_sources(self):
        result = self.review()
        review_id, upload_id = result["review_id"], result["upload_id"]
        exported = self.client.get(f"/api/reviews/{review_id}/export.json")
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(exported.json(), result)
        self.assertIn("attachment", exported.headers["content-disposition"])
        # This separate app instance has no in-memory upload or review state.
        with TestClient(create_app(self.data)) as restored:
            self.assertEqual(restored.get(f"/api/reviews/{review_id}").json(), result)
            self.assertEqual(restored.get(f"/api/uploads/{upload_id}").status_code, 200)
            proof = result["results"][-1]["sources"][0]
            self.assertIn(proof["quote"], restored.get(proof["page_url"]).json()["text"])

    def test_legacy_eight_field_snapshot_is_not_rewritten_as_ten(self):
        # Simulate a pre-expansion record only in this test's temporary store.
        # Reading/exporting it must not manufacture verdicts for the new fields.
        legacy = self.review()
        new_keys = {"tap_changer_type", "installation_type"}
        legacy["results"] = [row for row in legacy["results"] if row["field"] not in new_keys]
        legacy["summary"] = {status: sum(row["status"] == status for row in legacy["results"]) for status in legacy["summary"]}
        legacy["explanations"]["items"] = [item for item in legacy["explanations"]["items"] if item["field"] not in new_keys]
        self.assertEqual(len(legacy["results"]), 8)
        record_path = self.data / "reviews" / f"{legacy['review_id']}.json"
        write_json(record_path, legacy)
        original_bytes = record_path.read_bytes()
        with TestClient(create_app(self.data)) as restored:
            with patch("handoff_review.web_app.explain_review", side_effect=AssertionError("Legacy records must not reach explanation/model execution")) as explanation:
                for use_ai in (True, False):
                    response = restored.post(f"/api/reviews/{legacy['review_id']}/explain", json={"use_ai": use_ai})
                    self.assertEqual(response.status_code, 409)
                    self.assertIn("문서를 다시 읽고 검토", response.json()["detail"])
                    self.assertEqual(record_path.read_bytes(), original_bytes)
                explanation.assert_not_called()
            for suffix in ("", "/export.json"):
                response = restored.get(f"/api/reviews/{legacy['review_id']}{suffix}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), legacy)
                self.assertEqual(len(response.json()["results"]), 8)
                self.assertEqual(sum(response.json()["summary"].values()), 8)

    def test_explanation_requires_current_field_set_not_just_ten_rows(self):
        record = self.review()
        record["results"][-1]["field"] = record["results"][0]["field"]
        self.assertEqual(len(record["results"]), 10)
        record_path = self.data / "reviews" / f"{record['review_id']}.json"
        write_json(record_path, record)
        original_bytes = record_path.read_bytes()
        with patch("handoff_review.web_app.explain_review", side_effect=AssertionError("Invalid field set must be rejected")) as explanation:
            response = self.client.post(f"/api/reviews/{record['review_id']}/explain", json={"use_ai": True})
        self.assertEqual(response.status_code, 409)
        explanation.assert_not_called()
        self.assertEqual(record_path.read_bytes(), original_bytes)
        self.assertEqual(self.client.get(f"/api/reviews/{record['review_id']}/export.json").json(), record)

    def test_missing_or_false_policy_confirmation_is_rejected(self):
        uploaded = self.upload()
        body = {"upload_id": uploaded["upload_id"], "approved_amendment_ids": ["AMD-001"]}
        self.assertEqual(self.client.post("/api/reviews", json=body).status_code, 422)
        self.assertEqual(self.client.post("/api/reviews", json={**body, "policy_confirmed": False}).status_code, 422)
        self.assertEqual(list((self.data / "reviews").glob("*.json")), [])

    def test_unknown_amendment_id_is_rejected(self):
        uploaded = self.upload()
        response = self.client.post("/api/reviews", json={
            "upload_id": uploaded["upload_id"], "policy_confirmed": True,
            "approved_amendment_ids": ["NOT-IN-UPLOAD"],
        })
        self.assertEqual(response.status_code, 422)

    def test_duplicate_amendment_id_is_rejected(self):
        uploaded = self.upload()
        response = self.client.post("/api/reviews", json={
            "upload_id": uploaded["upload_id"], "policy_confirmed": True,
            "approved_amendment_ids": ["AMD-001", "AMD-001"],
        })
        self.assertEqual(response.status_code, 422)

    def test_omitted_known_amendment_causes_review_not_silent_base_match(self):
        uploaded = self.upload()
        response = self.client.post("/api/reviews", json={
            "upload_id": uploaded["upload_id"], "policy_confirmed": True,
            "approved_amendment_ids": [],
        })
        self.assertEqual(response.status_code, 200)
        fan = next(row for row in response.json()["results"] if row["field"] == "fan_supply_voltage")
        self.assertEqual(fan["status"], "NEEDS_REVIEW")
        self.assertIn("AMD-001", {source["document_id"] for source in fan["sources"]})

    def test_fewer_than_three_files_are_rejected(self):
        for count in (1, 2):
            with self.subTest(count=count):
                response = self.client.post("/api/uploads", files=self.files()[:count])
                self.assertEqual(response.status_code, 422)
        self.assert_no_committed_upload()

    def test_more_than_three_files_are_rejected(self):
        response = self.client.post("/api/uploads", files=self.files() + [("extra_file", ("extra.pdf", self.pdf_bytes["D01"]["spec_file"], "application/pdf"))])
        # Starlette rejects the fourth part before endpoint field validation.
        self.assertEqual(response.status_code, 400)
        self.assert_no_committed_upload()

    def test_duplicate_multipart_file_fields_are_rejected(self):
        files = self.files()
        files[2] = ("spec_file", files[2][1])
        response = self.client.post("/api/uploads", files=files)
        self.assertEqual(response.status_code, 422)
        self.assert_no_committed_upload()

    def test_extra_text_field_is_rejected(self):
        response = self.client.post("/api/uploads", files=self.files(), data={"unapproved_extra": "value"})
        self.assertEqual(response.status_code, 400)
        self.assert_no_committed_upload()

    def test_per_file_size_limit_is_rejected_before_parsing(self):
        files = self.files()
        files[0] = ("spec_file", ("oversize.pdf", b"%PDF-1.7\n" + b"0" * FILE_LIMIT, "application/pdf"))
        response = self.client.post("/api/uploads", files=files)
        self.assertEqual(response.status_code, 413)
        self.assert_no_committed_upload()

    def test_declared_oversized_body_is_rejected(self):
        response = self.client.post("/api/uploads", content=b"", headers={"content-length": str(BODY_LIMIT + 1)})
        self.assertEqual(response.status_code, 413)
        self.assert_no_committed_upload()

    def test_actual_stream_size_is_bounded_without_content_length(self):
        response = self.client.post("/api/uploads", content=iter([b"0" * (1024**2)] * 17))
        self.assertEqual(response.status_code, 413)
        self.assert_no_committed_upload()

    def test_malformed_pdf_after_valid_first_file_leaves_no_partial_set(self):
        files = self.files()
        files[1] = ("amendment_file", ("broken.pdf", b"%PDF-1.7\nbroken data", "application/pdf"))
        response = self.client.post("/api/uploads", files=files)
        self.assertEqual(response.status_code, 422)
        self.assert_no_committed_upload()

    def test_non_pdf_payload_is_rejected(self):
        files = self.files()
        files[0] = ("spec_file", ("fake.pdf", b"not pdf", "application/pdf"))
        self.assertEqual(self.client.post("/api/uploads", files=files).status_code, 422)
        self.assert_no_committed_upload()

    def test_role_swapped_pdf_is_rejected_by_actual_worker(self):
        files = self.files()
        files[0] = ("spec_file", ("wrong-role.pdf", self.pdf_bytes["D01"]["amendment_file"], "application/pdf"))
        response = self.client.post("/api/uploads", files=files)
        self.assertEqual(response.status_code, 422)
        self.assertIn("역할", response.json()["detail"])
        self.assert_no_committed_upload()

    def test_worker_timeout_is_reported_without_committing_upload(self):
        with patch("handoff_review.web_app.subprocess.run", side_effect=subprocess.TimeoutExpired("pdf-worker", 15)):
            response = self.client.post("/api/uploads", files=self.files())
        self.assertEqual(response.status_code, 422)
        self.assertIn("제한시간", response.json()["detail"])
        self.assert_no_committed_upload()

    def test_cross_origin_mutation_is_rejected(self):
        response = self.client.post("/api/uploads", files=self.files(), headers={"origin": "https://untrusted.example"})
        self.assertEqual(response.status_code, 403)
        self.assert_no_committed_upload()

    def test_same_origin_upload_is_allowed(self):
        response = self.client.post("/api/uploads", files=self.files(), headers={"origin": "http://testserver"})
        self.assertEqual(response.status_code, 201)

    def test_untrusted_host_is_rejected(self):
        response = self.client.get("/api/health", headers={"host": "untrusted.example"})
        self.assertEqual(response.status_code, 400)

    def test_invalid_and_unknown_record_ids_return_404(self):
        for identifier in ("not-a-uuid", "f" * 32):
            for prefix in ("/api/uploads", "/api/reviews"):
                with self.subTest(identifier=identifier, prefix=prefix):
                    self.assertEqual(self.client.get(f"{prefix}/{identifier}").status_code, 404)
        self.assertEqual(self.client.get("/api/reviews/not-a-uuid/export.json").status_code, 404)

    def test_unknown_document_and_page_return_404(self):
        uploaded = self.upload()
        base = f"/api/uploads/{uploaded['upload_id']}/documents"
        for path in (f"{base}/UNKNOWN/pdf", f"{base}/SPEC-001/pages/0", f"{base}/SPEC-001/pages/2"):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_default_explanation_never_calls_optional_ai(self):
        with patch("handoff_review.explanation._request_plan", side_effect=AssertionError("AI must not be called")) as transport:
            result = self.review()
            response = self.client.post(f"/api/reviews/{result['review_id']}/explain", json={"use_ai": False})
        transport.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["llm_attempted"])

    def test_ai_unavailable_falls_back_without_changing_verdict_or_export(self):
        original = self.review()
        with patch("handoff_review.explanation._request_plan", side_effect=OSError("synthetic unavailable detail not for UI")):
            response = self.client.post(f"/api/reviews/{original['review_id']}/explain", json={"use_ai": True})
        self.assertEqual(response.status_code, 200)
        explanation = response.json()
        self.assertTrue(explanation["llm_attempted"])
        self.assertEqual({item["validation_status"] for item in explanation["items"]}, {"unavailable"})
        self.assertEqual({item["origin"] for item in explanation["items"]}, {"code"})
        self.assertNotIn("synthetic unavailable detail", response.text)
        restored = self.client.get(f"/api/reviews/{original['review_id']}").json()
        self.assertEqual(restored["results"], original["results"])
        self.assertEqual(restored["summary"], original["summary"])
        exported = self.client.get(f"/api/reviews/{original['review_id']}/export.json").json()
        self.assertEqual(exported["results"], original["results"])
        self.assertEqual(exported["explanations"], explanation)

    def test_malformed_ai_plan_is_rejected_and_not_exposed(self):
        original = self.review()
        malicious = {"items": [], "prose": "승인 완료 <script>untrusted</script>"}
        with patch("handoff_review.explanation._request_plan", return_value=malicious):
            response = self.client.post(f"/api/reviews/{original['review_id']}/explain", json={"use_ai": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual({item["validation_status"] for item in response.json()["items"]}, {"rejected"})
        self.assertNotIn("<script>", response.text)
        self.assertNotIn("승인 완료", response.text)
        self.assertEqual(self.client.get(f"/api/reviews/{original['review_id']}").json()["results"], original["results"])

    def test_ai_value_status_and_source_tampering_cannot_change_visible_facts(self):
        original = self.review()

        def tampered(facts, _base_url, **_options):
            items = deepcopy(facts)
            fan = next(item for item in items if item["field"] == "fan_supply_voltage")
            fan.update(actual_text="99000 V", status="MATCH", source_ids=["UNTRUSTED-SOURCE"])
            return {"items": items}

        with patch("handoff_review.explanation._request_plan", side_effect=tampered):
            response = self.client.post(f"/api/reviews/{original['review_id']}/explain", json={"use_ai": True})
        self.assertEqual(response.status_code, 200)
        fan = next(item for item in response.json()["items"] if item["field"] == "fan_supply_voltage")
        self.assertEqual(fan["validation_status"], "rejected")
        self.assertEqual(fan["origin"], "code")
        self.assertIn("440 V", fan["text"])
        self.assertIn("380 V", fan["text"])
        self.assertNotIn("99000", response.text)
        self.assertNotIn("UNTRUSTED-SOURCE", response.text)
        self.assertEqual(self.client.get(f"/api/reviews/{original['review_id']}").json()["results"], original["results"])

    def test_exact_mock_ai_plan_is_only_labeled_verified_not_independent(self):
        original = self.review()
        with patch("handoff_review.explanation._request_plan", side_effect=lambda facts, _url, **_options: {"items": deepcopy(facts)}):
            response = self.client.post(f"/api/reviews/{original['review_id']}/explain", json={"use_ai": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual({item["origin"] for item in response.json()["items"]}, {"ai_verified"})
        self.assertIn("독립 정확도 평가", response.json()["note"])
        self.assertEqual([item["text"] for item in response.json()["items"]], [item["text"] for item in original["explanations"]["items"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
