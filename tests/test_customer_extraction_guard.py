"""Direct extraction requests must enforce the saved customer-identity hold.

Real synthetic PDF bytes are parsed in an isolated TemporaryDirectory. Only the
worker is replaced with a model-free observation stub. No live model, external
network, normal application store, or historic evaluation record is used.
"""
from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from handoff_review.contracts import FIELD_KEYS
from handoff_review.web_app import create_app, write_json

SLOTS = ("spec_file", "amendment_file", "handoff_file")
SAMPLE_NAMES = ("01_customer_spec.pdf", "02_amendment.pdf", "03_handoff.pdf")
HELD_SUMMARY = {"MATCH": 0, "MISMATCH": 0, "MISSING": 0, "NEEDS_REVIEW": 10}


class CustomerExtractionGuardTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="handoff-customer-extraction-guard-")
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        self.app = create_app(self.data)
        self.worker = Mock(side_effect=self.model_free_result)
        self.app.state.extract_upload = self.worker

        self.network_guards = []
        for target in ("handoff_review.extraction_agent._local_request",
                       "socket.create_connection", "socket.socket.connect",
                       "socket.socket.connect_ex"):
            guard = patch(target, side_effect=AssertionError("Model/network calls are forbidden."))
            self.network_guards.append(guard.start())
            self.addCleanup(guard.stop)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def tearDown(self):
        for guard in self.network_guards:
            guard.assert_not_called()

    @staticmethod
    def model_free_result(documents):
        """Observe the allowed worker path without generating any field values."""
        copied = deepcopy(documents)
        for item in copied:
            item["document"]["fields"] = {
                key: ["EXTRACTION_UNCERTAIN"] for key in FIELD_KEYS
            }
        return {
            "documents": copied, "trace": [], "status": "unavailable",
            "accepted_fields": 0, "proposed_fields": 0,
            "warnings": ["MODEL_FREE_GUARD_TEST"], "llm_calls": 0,
            "method": "local_tool_rag",
        }

    def upload(self, case):
        if case in ("missing", "unknown", "ambiguous"):
            folder = ROOT / "output/pdf/customer-boundary"
            names = ("SPEC-CB.pdf", "AMD-CB.pdf", f"HANDOFF-{case.upper()}.pdf")
        else:
            folder = ROOT / ("output/pdf/varied" if case == "V01" else "output/pdf/samples") / case
            names = SAMPLE_NAMES
        original_bytes = {name: (folder / name).read_bytes() for name in names}
        response = self.client.post("/api/uploads/text", files=[
            (slot, (name, original_bytes[name], "application/pdf"))
            for slot, name in zip(SLOTS, names)
        ])
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual({name: (folder / name).read_bytes() for name in names}, original_bytes)
        return response.json()

    def snapshot(self):
        """Include empty directories, catching even a rejected request's job folder."""
        return (
            {str(path.relative_to(self.data)): path.read_bytes()
             for path in self.data.rglob("*") if path.is_file()},
            {str(path.relative_to(self.data))
             for path in self.data.rglob("*") if path.is_dir()},
        )

    def assert_denied_without_side_effects(self, uploaded, **request_kwargs):
        before = self.snapshot()
        metadata_before = self.client.get(f"/api/uploads/{uploaded['upload_id']}").json()
        response = self.client.post(
            f"/api/uploads/{uploaded['upload_id']}/extractions", **request_kwargs,
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("고객사", response.json()["detail"])
        self.worker.assert_not_called()
        self.assertFalse(self.app.state.extraction_tasks)
        self.assertFalse(self.app.state.ai_gate.locked())
        self.assertEqual(self.snapshot(), before, "Rejected extraction wrote app state.")
        self.assertEqual(
            self.client.get(f"/api/uploads/{uploaded['upload_id']}").json(),
            metadata_before,
        )
        self.assertFalse((self.data / "extractions").exists())
        return response

    def assert_followup_review_held(self, uploaded, expected_customer):
        # A customer hold can be inspected without extraction. Policy approval
        # must not reclassify the identity hold or populate technical values.
        before_set = {
            path.name: path.read_bytes()
            for path in (self.data / "sets" / uploaded["upload_id"]).iterdir()
            if path.is_file()
        }
        response = self.client.post("/api/reviews", json={
            "upload_id": uploaded["upload_id"], "policy_confirmed": True,
            "approved_amendment_ids": [
                doc["id"] for doc in uploaded["documents"] if doc["role"] == "amendment"
            ],
        })
        self.assertEqual(response.status_code, 200, response.text)
        review = response.json()
        self.assertEqual(review["customer_check"]["status"], expected_customer)
        self.assertIs(review["customer_check"]["can_compare"], False)
        self.assertEqual(review["summary"], HELD_SUMMARY)
        self.assertEqual(len(review["results"]), 10)
        self.assertEqual({row["field"] for row in review["results"]}, set(FIELD_KEYS))
        for row in review["results"]:
            self.assertEqual(row["status"], "NEEDS_REVIEW")
            self.assertIsNone(row["expected"])
            self.assertIsNone(row["actual"])
        self.assertEqual(review["extraction_method"], "customer_identity_hold_before_extraction")
        self.assertIsNone(review["extraction_report"])
        self.assertEqual(
            {path.name: path.read_bytes()
             for path in (self.data / "sets" / uploaded["upload_id"]).iterdir()
             if path.is_file()},
            before_set,
        )
        self.worker.assert_not_called()

    def assert_real_pdf_case_held(self, case, expected_customer, expected_handoff):
        uploaded = self.upload(case)
        self.assertEqual(uploaded["customer_check"]["status"], expected_customer)
        handoff = next(doc for doc in uploaded["customer_check"]["documents"]
                       if doc["role"] == "handoff")
        self.assertEqual(handoff["status"], expected_handoff)
        self.assert_denied_without_side_effects(uploaded)
        self.assert_followup_review_held(uploaded, expected_customer)

    def test_missing_customer_real_pdf_blocks_direct_extraction(self):
        self.assert_real_pdf_case_held("missing", "NEEDS_REVIEW", "missing")

    def test_unknown_customer_real_pdf_blocks_direct_extraction(self):
        self.assert_real_pdf_case_held("unknown", "NEEDS_REVIEW", "unknown")

    def test_conflicting_names_in_one_real_pdf_block_direct_extraction(self):
        self.assert_real_pdf_case_held("ambiguous", "NEEDS_REVIEW", "ambiguous")

    def test_cross_document_customer_mismatch_blocks_direct_extraction(self):
        self.assert_real_pdf_case_held("C01", "MISMATCH", "recognized")

    def test_client_claimed_customer_match_in_body_and_query_cannot_bypass(self):
        uploaded = self.upload("unknown")
        self.assert_denied_without_side_effects(
            uploaded,
            params={"can_compare": "true", "customer_name": "KEPCO"},
            json={
                "can_compare": True, "customer_confirmed": True,
                "customer_name": "KEPCO",
                "customer_check": {"status": "MATCH", "can_compare": True},
                "documents": [
                    {**doc, "customer_name": "KEPCO"} for doc in uploaded["documents"]
                ],
            },
        )
        self.assert_followup_review_held(uploaded, "NEEDS_REVIEW")

    def test_saved_documents_are_rechecked_not_stale_match_summary(self):
        uploaded = self.upload("D02")
        self.assertTrue(uploaded["customer_check"]["can_compare"])
        manifest = self.data / "sets" / uploaded["upload_id"] / "manifest.json"
        saved = json.loads(manifest.read_text(encoding="utf-8"))
        # Deliberately change only the isolated test store: a stale MATCH cache
        # or earlier client response must not outrank current saved documents.
        handoff = next(item["document"] for item in saved["documents"]
                       if item["document"]["role"] == "handoff")
        handoff["customer_name"] = "Powergrid"
        saved["customer_check"] = deepcopy(uploaded["customer_check"])
        write_json(manifest, saved)
        self.assert_denied_without_side_effects(
            uploaded, json={"customer_check": uploaded["customer_check"]},
        )
        self.assert_followup_review_held(uploaded, "NEEDS_REVIEW")

    def test_legacy_saved_documents_without_customer_metadata_are_blocked(self):
        uploaded = self.upload("D02")
        manifest = self.data / "sets" / uploaded["upload_id"] / "manifest.json"
        saved = json.loads(manifest.read_text(encoding="utf-8"))
        for item in saved["documents"]:
            item["document"].pop("customer_name", None)
            item["document"].pop("customer_sources", None)
        write_json(manifest, saved)
        self.assert_denied_without_side_effects(uploaded)
        self.assert_followup_review_held(uploaded, "NEEDS_REVIEW")

    def test_customer_guard_precedes_semaphore_and_job_state(self):
        uploaded = self.upload("C01")
        before = self.snapshot()
        original_gate = self.app.state.ai_gate
        gate = Mock()
        gate.locked.side_effect = AssertionError("Customer hold must precede the semaphore.")
        gate.acquire.side_effect = AssertionError("Rejected customer must not acquire the gate.")
        self.app.state.ai_gate = gate
        try:
            response = self.client.post(f"/api/uploads/{uploaded['upload_id']}/extractions")
            self.assertEqual(response.status_code, 409, response.text)
            self.assertIn("고객사", response.json()["detail"])
            gate.locked.assert_not_called()
            gate.acquire.assert_not_called()
            gate.release.assert_not_called()
        finally:
            self.app.state.ai_gate = original_gate
        self.worker.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.app.state.extraction_tasks)
        self.assertFalse(original_gate.locked())

    def assert_recognized_case_can_extract(self, case):
        uploaded = self.upload(case)
        self.assertEqual(uploaded["customer_check"]["status"], "MATCH")
        original_set = self.data / "sets" / uploaded["upload_id"]
        before = {path.name: path.read_bytes() for path in original_set.iterdir() if path.is_file()}
        response = self.client.post(f"/api/uploads/{uploaded['upload_id']}/extractions")
        self.assertEqual(response.status_code, 202, response.text)
        job_id = response.json()["job_id"]
        deadline = time.monotonic() + 5
        while True:
            response = self.client.get(f"/api/extractions/{job_id}")
            self.assertEqual(response.status_code, 200, response.text)
            job = response.json()
            if job["status"] != "running":
                break
            self.assertLess(time.monotonic(), deadline, "Model-free worker did not complete.")
            time.sleep(0.01)
        self.assertEqual(job["status"], "completed", job)
        self.worker.assert_called_once()
        self.assertEqual(job["extraction"]["llm_calls"], 0)
        self.assertTrue(job["upload"]["customer_check"]["can_compare"])
        self.assertNotEqual(job["upload"]["upload_id"], uploaded["upload_id"])
        self.assertEqual(
            {path.name: path.read_bytes() for path in original_set.iterdir() if path.is_file()},
            before,
        )
        self.assertFalse(self.app.state.extraction_tasks)
        self.assertFalse(self.app.state.ai_gate.locked())

    def test_known_aliases_d02_still_accept_extraction_and_invoke_worker_once(self):
        self.assert_recognized_case_can_extract("D02")

    def test_known_customer_v01_still_accept_extraction_and_invoke_worker_once(self):
        self.assert_recognized_case_can_extract("V01")


if __name__ == "__main__":
    unittest.main()
