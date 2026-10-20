"""Customer identity boundaries, using saved sample bytes and isolated records.

No AI server or external service is contacted. Decoder mocks test metadata edge
cases; HTTP tests use the real PDF worker and current demonstration PDFs.
"""
from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from handoff_review.customers import check_customers
from handoff_review.pdf_ingest import extract_document, read_customer_metadata
from handoff_review.text_pdf import read_text_document
from handoff_review.web_app import create_app, write_json

SLOTS = (("spec_file", "01_customer_spec.pdf"), ("amendment_file", "02_amendment.pdf"),
         ("handoff_file", "03_handoff.pdf"))


class CustomerMetadataTests(unittest.TestCase):
    def metadata(self, *lines):
        return read_customer_metadata([{"page": i + 1, "text": line} for i, line in enumerate(lines)])

    def test_exact_header_preserves_raw_spelling_and_source(self):
        value, warnings = self.metadata("고객사(발주처) National   Grid")
        self.assertEqual(value["customer_name"], "National   Grid")
        self.assertEqual(value["customer_sources"], [{"page": 1, "quote": "고객사(발주처) National   Grid"}])
        self.assertEqual(warnings, [])

    def test_repeated_identical_headers_preserve_all_pages(self):
        value, warnings = self.metadata("고객사(발주처) 한전", "고객사(발주처) 한전")
        self.assertEqual(value["customer_name"], "한전")
        self.assertEqual([s["page"] for s in value["customer_sources"]], [1, 2])
        self.assertFalse(warnings)

    def test_aliases_conflicting_within_one_document_do_not_pick_last(self):
        value, warnings = self.metadata("고객사(발주처) 한전", "고객사(발주처) KEPCO")
        self.assertEqual(value["customer_name"], ["한전", "KEPCO"])
        self.assertEqual(warnings[0]["code"], "CUSTOMER_METADATA_AMBIGUOUS")
        self.assertFalse(check_customers([value])["can_compare"])

    def test_empty_malformed_and_oversize_headers_are_not_silently_missing(self):
        for line in ("고객사(발주처)", "고객사(발주처)한전", "고객사(발주처) " + "A" * 161):
            with self.subTest(line=line):
                value, warnings = self.metadata(line)
                self.assertIsInstance(value["customer_name"], list)
                self.assertEqual(check_customers([value])["documents"][0]["status"], "ambiguous")
                self.assertTrue(warnings)

    def test_missing_header_does_not_infer_name_from_title_or_body(self):
        value, warnings = self.metadata("한국전력의 참고 문서입니다.\n고객사 한전\n고객사 표기는 시험용입니다.")
        self.assertIsNone(value["customer_name"])
        self.assertFalse(value["customer_sources"])
        self.assertEqual(warnings[0]["code"], "CUSTOMER_METADATA_MISSING")

    def test_unknown_customer_and_nonalias_are_not_reassigned(self):
        for name in ("Powergrid", "NGET", "National Grid Electricity Transmission", "미등록 고객"):
            value, _ = self.metadata("고객사(발주처) " + name)
            result = check_customers([value])
            self.assertEqual(result["documents"][0]["raw_name"], name)
            self.assertEqual(result["documents"][0]["status"], "unknown")

    def test_both_ingesters_accept_old_pdf_but_mark_missing_customer(self):
        path = ROOT / "output/pdf/samples/D01/03_handoff.pdf"
        text = "\n".join(page.extract_text() for page in PdfReader(path).pages)
        text = "\n".join(line for line in text.splitlines() if not line.startswith("고객사(발주처)"))
        reader = SimpleNamespace(is_encrypted=False, pages=[SimpleNamespace(extract_text=lambda: text)])
        for module, function in (("pdf_ingest", extract_document), ("text_pdf", read_text_document)):
            with self.subTest(module=module), patch(f"handoff_review.{module}.PdfReader", return_value=reader):
                result = function(b"%PDF-1.7\nmock-decoder", "handoff")
                self.assertIsNone(result["document"]["customer_name"])
                self.assertIn("CUSTOMER_METADATA_MISSING", [w["code"] for w in result["warnings"]])
                self.assertFalse(check_customers([result["document"]])["can_compare"])

    def test_both_ingesters_preserve_real_mixed_customer_pdf_metadata(self):
        for function in (extract_document, read_text_document):
            records = [function((ROOT / f"output/pdf/samples/C01/{name}").read_bytes(), role)["document"]
                       for (_, name), role in zip(SLOTS, ("customer_spec", "amendment", "handoff"))]
            self.assertEqual(check_customers(records)["status"], "MISMATCH")
            self.assertEqual(records[-1]["customer_name"], "SK하이닉스")
            self.assertTrue(all(doc["customer_sources"] for doc in records))


class CustomerApiTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="handoff-customer-test-")
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        self.app = create_app(self.data)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def upload(self, case="D02", text=False):
        files = [(slot, (name, (ROOT / f"output/pdf/samples/{case}/{name}").read_bytes(), "application/pdf"))
                 for slot, name in SLOTS]
        response = self.client.post("/api/uploads/text" if text else "/api/uploads", files=files)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def review(self, uploaded, selected=True):
        response = self.client.post("/api/reviews", json={
            "upload_id": uploaded["upload_id"], "policy_confirmed": True,
            "approved_amendment_ids": [d["id"] for d in uploaded["documents"] if d["role"] == "amendment"] if selected else [],
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def assert_hold(self, review):
        self.assertEqual(review["summary"], {"MATCH": 0, "MISMATCH": 0, "MISSING": 0, "NEEDS_REVIEW": 10})
        self.assertFalse(review["customer_check"]["can_compare"])
        self.assertTrue(all(row["expected"] is None and row["actual"] is None for row in review["results"]))

    def test_alias_set_preserves_raw_names_but_compares_same_customer(self):
        uploaded = self.upload()
        self.assertEqual(uploaded["customer_check"]["status"], "MATCH")
        self.assertEqual([d["customer_name"] for d in uploaded["documents"]], ["한국전력", "KEPCO", "한전"])
        result = self.review(uploaded)
        self.assertEqual(result["summary"]["MATCH"], 10)
        self.assertEqual(result["customer_check"], uploaded["customer_check"])

    def test_mixed_customer_blocks_even_technically_equal_documents(self):
        uploaded = self.upload("C01")
        self.assertEqual(uploaded["customer_check"]["status"], "MISMATCH")
        for selected in (True, False):
            with self.subTest(selected=selected):
                result = self.review(uploaded, selected)
                self.assert_hold(result)
                exported = self.client.get(f"/api/reviews/{result['review_id']}/export.json").json()
                self.assertEqual(exported, result)

    def test_customer_hold_can_be_viewed_before_ai_extraction(self):
        uploaded = self.upload("C01", text=True)
        with patch("handoff_review.web_app.extract_text_upload", side_effect=AssertionError("AI must not run")):
            result = self.review(uploaded)
        self.assert_hold(result)
        self.assertEqual(result["extraction_method"], "customer_identity_hold_before_extraction")
        self.assertIsNone(result["extraction_report"])

    def test_old_upload_without_customer_metadata_is_held_not_migrated(self):
        uploaded = self.upload()
        manifest = self.data / "sets" / uploaded["upload_id"] / "manifest.json"
        stored = json.loads(manifest.read_text())
        for item in stored["documents"]:
            item["document"].pop("customer_name")
            item["document"].pop("customer_sources")
        write_json(manifest, stored)
        before = manifest.read_bytes()
        metadata = self.client.get(f"/api/uploads/{uploaded['upload_id']}").json()
        self.assertEqual(metadata["customer_check"]["status"], "NEEDS_REVIEW")
        self.assert_hold(self.review(uploaded))
        self.assertEqual(manifest.read_bytes(), before)

    def test_old_ten_field_result_is_read_only_and_not_sent_to_ai(self):
        record = self.review(self.upload())
        record.pop("customer_check")
        for doc in record["documents"]:
            doc.pop("customer_name", None)
            doc.pop("customer_sources", None)
        path = self.data / "reviews" / f"{record['review_id']}.json"
        write_json(path, record)
        before = path.read_bytes()
        with patch("handoff_review.web_app.explain_review", side_effect=AssertionError("Do not re-explain old records")):
            for use_ai in (False, True):
                response = self.client.post(f"/api/reviews/{record['review_id']}/explain", json={"use_ai": use_ai})
                self.assertEqual(response.status_code, 409)
                self.assertIn("고객사", response.text)
        for suffix in ("", "/export.json"):
            self.assertEqual(self.client.get(f"/api/reviews/{record['review_id']}{suffix}").json(), record)
        self.assertEqual(path.read_bytes(), before)

    def test_customer_override_is_rejected_not_applied(self):
        uploaded = self.upload("C01")
        response = self.client.post("/api/reviews", json={
            "upload_id": uploaded["upload_id"], "policy_confirmed": True,
            "approved_amendment_ids": [], "customer_name": "한전", "customer_confirmed": True})
        self.assertEqual(response.status_code, 422)

    def test_blocked_customer_result_cannot_request_ai_structure_check(self):
        record = self.review(self.upload("C01"))
        path = self.data / "reviews" / f"{record['review_id']}.json"
        before = path.read_bytes()
        with patch("handoff_review.web_app.explain_review", side_effect=AssertionError("Customer blocked")):
            response = self.client.post(f"/api/reviews/{record['review_id']}/explain", json={"use_ai": True})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(path.read_bytes(), before)
