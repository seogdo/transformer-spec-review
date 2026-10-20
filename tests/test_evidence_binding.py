"""Original-page evidence regressions, not another model-quality measurement.

Unit tests use invented document text and call only the evidence binder.
Integration tests replay two previously captured structured model responses
against the unchanged F04 PDF bytes. They do not contact a model, alter its
prompt, read manual answers for a verdict, or use the production upload store.
The frozen report and original PDFs are checked for byte preservation.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from handoff_review import extraction_agent
from handoff_review.web_app import bind_evidence, create_app, write_json


FIELD = "fan_supply_voltage"
VERSION = "original-page-evidence-v2"
SCOPE = "original_page_binding_not_semantic_completeness"
FROZEN_REPORT = ROOT / "reports/fresh-evaluation-20261020T134647-008a1935.result.json"
F04_FOLDER = ROOT / "output/pdf/fresh-evaluation/F04"


def source(document_id, quote, page=1):
    return {"document_id": document_id, "title": document_id, "page": page, "quote": quote}


def document(document_id, texts, field=None, status="approved"):
    """Provide original pages separately from model-derived fields."""
    return {
        "document": {
            "id": document_id, "title": document_id,
            "role": "customer_spec" if document_id == "SPEC" else "handoff",
            "project_id": "EVIDENCE-MOCK", "status": status, "approved_by": "demo",
            "customer_name": "한전", "fields": {} if field is None else {FIELD: deepcopy(field)},
        },
        "pages": [{"page": index, "text": text} for index, text in enumerate(texts, 1)],
        "extraction_method": "local_tool_rag", "warnings": [],
    }


def upload_for(*items):
    return {"upload_id": "a" * 32, "documents": list(items)}


def result_for(status="NEEDS_REVIEW", sources=None, can_compare=True):
    return {
        "customer_check": {
            "version": "customer-identity-v1",
            "status": "MATCH" if can_compare else "NEEDS_REVIEW", "can_compare": can_compare,
        },
        "results": [{
            "field": FIELD, "label": "팬 보조전원 정격전압", "status": status,
            "reason": "가상 검토 이유", "expected": None, "actual": None,
            "sources": deepcopy(sources or []),
        }],
        "summary": {},
    }


def facts(row):
    return {key: deepcopy(row[key]) for key in ("status", "reason", "expected", "actual")}


class OriginalPageEvidenceUnitTests(unittest.TestCase):
    def assert_check(self, row, status):
        check = row["evidence_check"]
        self.assertEqual(check["version"], VERSION)
        self.assertEqual(check["scope"], SCOPE)
        self.assertEqual(check["status"], status)
        self.assertEqual(check["linked_source_count"], len(row["sources"]))
        self.assertIs(type(check["invalid_source_count"]), int)
        self.assertGreaterEqual(check["invalid_source_count"], 0)

    def assert_original_proofs(self, row, upload):
        index = {item["document"]["id"]: item for item in upload["documents"]}
        for proof in row["sources"]:
            self.assertIs(proof["verified_in_page"], True)
            text = next(page["text"] for page in index[proof["document_id"]]["pages"] if page["page"] == proof["page"])
            self.assertIn(proof["quote"], text)
            self.assertIn(upload["upload_id"], proof["pdf_url"])
            self.assertIn(f"/pages/{proof['page']}", proof["page_url"])

    def test_two_conflicting_original_occurrences_link_without_value_fill(self):
        original = upload_for(document("HANDOFF", [
            "냉각팬 전원전압: 400 V\n냉각팬 전원전압: 440 V\n"
        ]))
        before_upload = deepcopy(original)
        result = result_for()
        before_facts = facts(result["results"][0])
        bind_evidence(result, original)
        row = result["results"][0]
        self.assertEqual(facts(row), before_facts)
        self.assertEqual({proof["quote"] for proof in row["sources"]},
                         {"냉각팬 전원전압: 400 V", "냉각팬 전원전압: 440 V"})
        self.assertTrue(all(proof["evidence_kind"] == "field_occurrence" for proof in row["sources"]))
        self.assertFalse(row["evidence_complete"])
        self.assert_check(row, "review_required")
        self.assert_original_proofs(row, original)
        self.assertEqual(original, before_upload)

    def test_conflicting_occurrences_across_pages_preserve_page_identity(self):
        original = upload_for(document("HANDOFF", ["냉각팬 전원전압: 400 V", "냉각팬 전원전압: 440 V"]))
        result = result_for()
        bind_evidence(result, original)
        row = result["results"][0]
        self.assertEqual({(proof["page"], proof["quote"]) for proof in row["sources"]},
                         {(1, "냉각팬 전원전압: 400 V"), (2, "냉각팬 전원전압: 440 V")})
        self.assertFalse(row["evidence_complete"])
        self.assert_original_proofs(row, original)

    def test_same_page_same_quote_is_deduplicated_but_next_page_preserved(self):
        quote = "냉각팬 전원전압: 400 V"
        original = upload_for(document("HANDOFF", [quote + "\n" + quote, quote]))
        result = result_for(sources=[source("HANDOFF", quote), source("HANDOFF", quote)])
        bind_evidence(result, original)
        row = result["results"][0]
        self.assertEqual(len(row["sources"]), 2)
        self.assertEqual({(proof["document_id"], proof["page"], proof["quote"]) for proof in row["sources"]},
                         {("HANDOFF", 1, quote), ("HANDOFF", 2, quote)})
        self.assert_check(row, "review_required")

    def test_condition_and_negation_are_context_only_not_confirmed_values(self):
        for quote in (
            "냉각팬 전원전압: 400 V (주위 온도 40℃ 이상에서만 사용)",
            "냉각팬 전원전압: 400 V 사용 금지",
            "냉각팬 전원전압: 미확정",
        ):
            with self.subTest(quote=quote):
                original = upload_for(document("HANDOFF", [quote]))
                result = result_for()
                bind_evidence(result, original)
                row = result["results"][0]
                self.assertIsNone(row["expected"])
                self.assertIsNone(row["actual"])
                self.assertEqual(row["status"], "NEEDS_REVIEW")
                self.assertEqual(row["sources"][0]["quote"], quote)
                self.assertEqual(row["sources"][0]["evidence_kind"], "field_occurrence")
                self.assertFalse(row["evidence_complete"])
                self.assert_check(row, "review_required")

    def test_source_absence_is_unavailable_not_missing_or_complete(self):
        original = upload_for(document("HANDOFF", ["별도 메모입니다."]))
        result = result_for()
        bind_evidence(result, original)
        row = result["results"][0]
        self.assertEqual(row["status"], "NEEDS_REVIEW")
        self.assertEqual(row["sources"], [])
        self.assertFalse(row["evidence_complete"])
        self.assert_check(row, "unavailable")

    def test_customer_hold_does_not_add_technical_occurrences(self):
        original = upload_for(document("HANDOFF", [
            "냉각팬 전원전압: 400 V\n냉각팬 전원전압: 440 V"
        ]))
        result = result_for(can_compare=False)
        bind_evidence(result, original)
        row = result["results"][0]
        self.assertEqual(row["sources"], [])
        self.assertIsNone(row["expected"])
        self.assertIsNone(row["actual"])
        self.assertEqual(row["status"], "NEEDS_REVIEW")
        self.assertFalse(row["evidence_complete"])
        self.assert_check(row, "unavailable")

    def test_candidate_from_unapproved_document_does_not_approve_its_value(self):
        original = upload_for(document("HANDOFF", ["냉각팬 전원전압: 440 V"], status="draft"))
        result = result_for()
        bind_evidence(result, original)
        row = result["results"][0]
        self.assertTrue(row["sources"])
        self.assertEqual(row["sources"][0]["evidence_kind"], "field_occurrence")
        self.assertEqual(row["status"], "NEEDS_REVIEW")
        self.assertIsNone(row["expected"])
        self.assertIsNone(row["actual"])
        self.assertFalse(row["evidence_complete"])

    def test_normal_verified_row_retains_values_and_status(self):
        quote = "냉각팬 전원전압: 400 V"
        original = upload_for(document("SPEC", [quote]), document("HANDOFF", [quote]))
        result = result_for("MATCH", [source("SPEC", quote), source("HANDOFF", quote)])
        row = result["results"][0]
        row["expected"] = {"value": "400", "unit": "V"}
        row["actual"] = {"value": "400", "unit": "V"}
        before = facts(row)
        bind_evidence(result, original)
        self.assertEqual(facts(row), before)
        self.assertTrue(row["evidence_complete"])
        self.assert_check(row, "linked")
        self.assert_original_proofs(row, original)

    def test_normal_mismatch_row_does_not_change_into_hold(self):
        first, second = "냉각팬 전원전압: 400 V", "냉각팬 전원전압: 440 V"
        original = upload_for(document("SPEC", [first]), document("HANDOFF", [second]))
        result = result_for("MISMATCH", [source("SPEC", first), source("HANDOFF", second)])
        row = result["results"][0]
        row["expected"] = {"value": "400", "unit": "V"}
        row["actual"] = {"value": "440", "unit": "V"}
        before = facts(row)
        bind_evidence(result, original)
        self.assertEqual(facts(row), before)
        self.assertTrue(row["evidence_complete"])
        self.assert_check(row, "linked")

    def test_forged_quote_cannot_become_a_verified_source(self):
        original = upload_for(document("HANDOFF", ["냉각팬 전원전압: 400 V"]))
        result = result_for(sources=[source("HANDOFF", "냉각팬 전원전압: 999 V")])
        bind_evidence(result, original)
        row = result["results"][0]
        self.assertFalse(any("999" in proof["quote"] for proof in row["sources"]))
        self.assertTrue(any("400 V" in proof["quote"] for proof in row["sources"]))
        self.assertGreaterEqual(row["evidence_check"]["invalid_source_count"], 1)
        self.assertFalse(row["evidence_complete"])
        self.assertEqual(row["status"], "NEEDS_REVIEW")

    def test_unknown_document_and_wrong_page_are_not_verified(self):
        original = upload_for(document("HANDOFF", ["냉각팬 전원전압: 400 V"]))
        for proof in (source("FORGED", "냉각팬 전원전압: 400 V"),
                      source("HANDOFF", "냉각팬 전원전압: 400 V", 9)):
            with self.subTest(proof=proof):
                result = result_for(sources=[proof])
                bind_evidence(result, original)
                row = result["results"][0]
                self.assertNotIn((proof["document_id"], proof["page"]),
                                 {(item["document_id"], item["page"]) for item in row["sources"]})
                self.assertGreaterEqual(row["evidence_check"]["invalid_source_count"], 1)
                self.assertFalse(row["evidence_complete"])

    def test_empty_and_whitespace_quotes_do_not_pass_via_empty_substring(self):
        original = upload_for(document("HANDOFF", ["냉각팬 전원전압: 400 V"]))
        for quote in ("", " \t\n "):
            with self.subTest(quote=repr(quote)):
                result = result_for(sources=[source("HANDOFF", quote)])
                bind_evidence(result, original)
                row = result["results"][0]
                self.assertTrue(all(proof["quote"].strip() for proof in row["sources"]))
                self.assertGreaterEqual(row["evidence_check"]["invalid_source_count"], 1)
                self.assertFalse(row["evidence_complete"])

    def test_boolean_page_is_not_page_one(self):
        original = upload_for(document("HANDOFF", ["냉각팬 전원전압: 400 V"]))
        result = result_for(sources=[source("HANDOFF", "냉각팬 전원전압: 400 V", True)])
        bind_evidence(result, original)
        row = result["results"][0]
        self.assertTrue(all(type(proof["page"]) is int for proof in row["sources"]))
        self.assertGreaterEqual(row["evidence_check"]["invalid_source_count"], 1)

    def test_no_original_proof_downgrades_definite_result(self):
        original = upload_for(document("HANDOFF", ["별도 메모입니다."]))
        result = result_for("MATCH")
        bind_evidence(result, original)
        row = result["results"][0]
        self.assertEqual(row["status"], "NEEDS_REVIEW")
        self.assertFalse(row["evidence_complete"])
        self.assert_check(row, "unavailable")


class CapturedF04EvidenceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not FROZEN_REPORT.is_file():
            raise unittest.SkipTest("The immutable captured F04 report is not installed.")
        cls.original_report_bytes = FROZEN_REPORT.read_bytes()
        report = json.loads(cls.original_report_bytes)
        captured = next(case for case in report["cases"] if case["case_id"] == "F04")
        # Replay observed outputs, never the report's manual_expected_after_run.
        cls.responses = deepcopy(captured["model_responses_for_synthetic_diagnostics"])
        cls.old_review = deepcopy(captured["review"])
        if len(cls.responses) != 2:
            raise AssertionError("The frozen run must contain exactly two captured responses.")
        cls.pdf_bytes = {name: (F04_FOLDER / name).read_bytes()
                         for name in ("SPEC-F04.pdf", "AMD-F04.pdf", "HANDOFF-F04.pdf")}

    def setUp(self):
        temporary = TemporaryDirectory(prefix="handoff-evidence-v2-")
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        self.app = create_app(self.data)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def run_captured(self, one_fan_proposal=False):
        responses = deepcopy(self.responses)
        if one_fan_proposal:
            responses[1]["items"] = [
                item for item in responses[1]["items"]
                if not (item["document_id"] == "HANDOFF-F04"
                        and item["field"] == FIELD and item["value"] == "440")
            ]
            self.assertEqual(sum(item["document_id"] == "HANDOFF-F04" and item["field"] == FIELD
                                 for item in responses[1]["items"]), 1)
        request = MagicMock(side_effect=responses)
        self.app.state.extract_upload = lambda documents: extraction_agent.extract_upload(documents, request_fn=request)
        files = [(slot, (name, self.pdf_bytes[name], "application/pdf"))
                 for slot, name in (("spec_file", "SPEC-F04.pdf"),
                                    ("amendment_file", "AMD-F04.pdf"),
                                    ("handoff_file", "HANDOFF-F04.pdf"))]
        with patch.object(extraction_agent, "_local_request", side_effect=AssertionError("No model/network in replay")):
            response = self.client.post("/api/uploads/text", files=files)
            self.assertEqual(response.status_code, 201, response.text)
            original = response.json()
            response = self.client.post(f"/api/uploads/{original['upload_id']}/extractions")
            self.assertEqual(response.status_code, 202, response.text)
            job_id = response.json()["job_id"]
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                response = self.client.get(f"/api/extractions/{job_id}")
                self.assertEqual(response.status_code, 200, response.text)
                job = response.json()
                if job["status"] != "running":
                    break
                time.sleep(.01)
            else:
                self.fail("Offline replay did not finish within its test deadline.")
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(request.call_count, 2)
        upload = job["upload"]
        response = self.client.post("/api/reviews", json={
            "upload_id": upload["upload_id"], "policy_confirmed": True,
            "approved_amendment_ids": ["AMD-F04"],
        })
        self.assertEqual(response.status_code, 200, response.text)
        review = response.json()
        self.assertNotEqual(upload["upload_id"], original["upload_id"])
        self.assertEqual(self.client.get(f"/api/uploads/{original['upload_id']}").json(), original)
        return review

    def assert_f04_conflict_visible(self, review):
        row = next(row for row in review["results"] if row["field"] == FIELD)
        self.assertEqual(row["status"], "NEEDS_REVIEW")
        self.assertIsNone(row["actual"])
        self.assertFalse(row["evidence_complete"])
        self.assertEqual(row["evidence_check"]["version"], VERSION)
        self.assertEqual(row["evidence_check"]["status"], "review_required")
        handoff = [proof for proof in row["sources"] if proof["document_id"] == "HANDOFF-F04"]
        self.assertTrue(any("400 V" in proof["quote"] for proof in handoff))
        self.assertTrue(any("440 V" in proof["quote"] for proof in handoff))
        self.assertTrue(any(proof.get("evidence_kind") == "field_occurrence" and "440 V" in proof["quote"]
                            for proof in handoff))
        for proof in row["sources"]:
            page = self.client.get(proof["page_url"])
            self.assertEqual(page.status_code, 200, page.text)
            self.assertIn(proof["quote"], page.json()["text"])
            self.assertIs(proof["verified_in_page"], True)
        old_by_field = {row["field"]: row for row in self.old_review["results"]}
        for current in review["results"]:
            old = old_by_field[current["field"]]
            self.assertEqual(current["status"], old["status"])
            for side in ("expected", "actual"):
                def display(value):
                    return None if value is None else (value["value"], value["unit"])
                self.assertEqual(display(current[side]), display(old[side]))
        self.assertEqual(self.client.get(f"/api/reviews/{review['review_id']}/export.json").json(), review)
        self.assertEqual(FROZEN_REPORT.read_bytes(), self.original_report_bytes)
        for name, original in self.pdf_bytes.items():
            self.assertEqual((F04_FOLDER / name).read_bytes(), original)

    def test_frozen_two_response_replay_keeps_both_conflicting_sources(self):
        self.assert_f04_conflict_visible(self.run_captured())

    def test_single_model_proposal_still_shows_other_original_conflict(self):
        self.assert_f04_conflict_visible(self.run_captured(one_fan_proposal=True))

    def test_old_record_get_export_and_restart_never_rebind_or_rewrite(self):
        old = deepcopy(self.old_review)
        self.assertTrue(any(row.get("evidence_complete") for row in old["results"]
                            if row["status"] == "NEEDS_REVIEW"))
        self.assertTrue(all("evidence_check" not in row for row in old["results"]))
        path = self.data / "reviews" / f"{old['review_id']}.json"
        write_json(path, old)
        before = path.read_bytes()
        with patch("handoff_review.web_app.bind_evidence", side_effect=AssertionError("Never migrate old record")):
            for suffix in ("", "/export.json"):
                response = self.client.get(f"/api/reviews/{old['review_id']}{suffix}")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), old)
            with TestClient(create_app(self.data)) as restarted:
                response = restarted.get(f"/api/reviews/{old['review_id']}/export.json")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), old)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(hashlib.sha256(FROZEN_REPORT.read_bytes()).digest(),
                         hashlib.sha256(self.original_report_bytes).digest())


if __name__ == "__main__":
    unittest.main()
