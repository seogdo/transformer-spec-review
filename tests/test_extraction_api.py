"""Test asynchronous orchestration, not model accuracy.

Real PDF bytes go through the isolated worker. Model extraction is substituted
only at the app seam here; evidence-validator tests and the disclosed live run
are separate. TemporaryDirectory keeps all test records away from user uploads.
"""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import asyncio
import inspect
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from handoff_review.contracts import FIELD_KEYS
from handoff_review.web_app import create_app, write_json


def unavailable(documents):
    result = deepcopy(documents)
    for item in result:
        item["document"]["fields"] = {key: {"value": ["EXTRACTION_UNCERTAIN"], "unit": "",
            "page": 1, "quote": item["pages"][0]["text"].splitlines()[0]} for key in FIELD_KEYS}
    return {"documents": result, "status": "unavailable", "accepted_fields": 0,
            "proposed_fields": 0, "llm_calls": 1, "trace": [], "warnings": ["시험용 연결 불가"],
            "method": "local_tool_rag"}


class ExtractionApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="handoff-extraction-api-")
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.app = create_app(self.data)
        self.app.state.extract_upload = unavailable
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def upload(self, mode="text", case="V01"):
        folder = ROOT / "output/pdf" / ("varied" if case.startswith("V") else "samples") / case
        files = [(slot, (name, (folder / name).read_bytes(), "application/pdf")) for slot, name in (
            ("spec_file", "01_customer_spec.pdf"), ("amendment_file", "02_amendment.pdf"), ("handoff_file", "03_handoff.pdf"))]
        response = self.client.post("/api/uploads" + ("/text" if mode == "text" else ""), files=files)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def start(self, upload):
        response = self.client.post(f"/api/uploads/{upload['upload_id']}/extractions")
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["job_id"]

    def wait(self, job_id):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            response = self.client.get(f"/api/extractions/{job_id}")
            self.assertEqual(response.status_code, 200, response.text)
            if response.json()["status"] != "running":
                return response.json()
            time.sleep(.01)
        self.fail("Test extraction job did not terminate")

    def body(self, uploaded):
        return {"upload_id": uploaded["upload_id"], "policy_confirmed": True,
                "approved_amendment_ids": [next(d["id"] for d in uploaded["documents"] if d["role"] == "amendment")]}

    def test_pending_text_pdf_cannot_be_reviewed_as_missing(self):
        uploaded = self.upload()
        self.assertEqual(uploaded["mode"], "text_ai")
        self.assertTrue(all(d["extraction_method"] == "text_pending_ai" for d in uploaded["documents"]))
        response = self.client.post("/api/reviews", json=self.body(uploaded))
        self.assertEqual(response.status_code, 409)

    def test_unavailable_ai_creates_new_snapshot_and_only_needs_review(self):
        uploaded = self.upload()
        original_id = uploaded["upload_id"]
        completed = self.wait(self.start(uploaded))
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["extraction"]["status"], "unavailable")
        derived = completed["upload"]
        self.assertNotEqual(derived["upload_id"], original_id)
        self.assertEqual(self.client.get(f"/api/uploads/{original_id}").json(), uploaded)
        for before, after in zip(uploaded["documents"], derived["documents"]):
            self.assertEqual(before["id"], after["id"])
            response = self.client.get(f"/api/uploads/{derived['upload_id']}/documents/{after['id']}/pdf")
            self.assertEqual(hashlib.sha256(response.content).hexdigest(), before["sha256"])
        response = self.client.post("/api/reviews", json=self.body(derived))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["summary"], {"MATCH": 0, "MISMATCH": 0, "MISSING": 0, "NEEDS_REVIEW": 10})
        self.assertEqual({row["field"] for row in response.json()["results"]}, set(FIELD_KEYS))
        self.assertEqual(len(response.json()["results"]), 10)
        self.assertEqual(response.json()["extraction_report"]["status"], "unavailable")
        self.assertFalse(response.json()["llm_used_for_verdict"])
        exported = self.client.get(f"/api/reviews/{response.json()['review_id']}/export.json").json()
        self.assertEqual(exported["results"], response.json()["results"])

    def test_mock_v03_categories_reach_comparison_evidence_and_export(self):
        """The API seam is mocked, not the real model or extraction validator."""
        def category_proposals(documents):
            output = unavailable(documents)
            for item in output["documents"]:
                changed = item["document"]["role"] == "amendment"
                values = {"tap_changer_type": "OLTC" if changed else "DETC",
                          "installation_type": "옥외형" if changed else "옥내형"}
                for key, value in values.items():
                    page = next(page for page in item["pages"] if value in page["text"])
                    quote = next(line for line in page["text"].splitlines() if value in line)
                    item["document"]["fields"][key] = {"value": value, "unit": "",
                                                        "page": page["page"], "quote": quote}
            output.update(status="partial", accepted_fields=6, proposed_fields=6, llm_calls=2,
                          warnings=["시험용 모의 추출: 새 범주 2종만 제공"])
            return output

        self.app.state.extract_upload = category_proposals
        uploaded = self.upload(case="V03")
        self.assertTrue(all(item["fields"] == {} for item in uploaded["documents"]))
        completed = self.wait(self.start(uploaded))
        self.assertEqual(completed["status"], "completed")
        derived = completed["upload"]
        self.assertNotEqual(derived["upload_id"], uploaded["upload_id"])
        response = self.client.post("/api/reviews", json=self.body(derived))
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["summary"], {"MATCH": 0, "MISMATCH": 2, "MISSING": 0, "NEEDS_REVIEW": 8})
        for key, expected, actual in (("tap_changer_type", "OLTC", "DETC"),
                                      ("installation_type", "옥외형", "옥내형")):
            row = next(row for row in result["results"] if row["field"] == key)
            self.assertEqual((row["status"], row["expected"]["value"], row["actual"]["value"]), ("MISMATCH", expected, actual))
            self.assertTrue(row["evidence_complete"])
            self.assertEqual({proof["document_id"] for proof in row["sources"]}, {"AMD-V03", "HANDOFF-V03"})
            for proof in row["sources"]:
                self.assertIn(proof["quote"], self.client.get(proof["page_url"]).json()["text"])
        exported = self.client.get(f"/api/reviews/{result['review_id']}/export.json").json()
        self.assertEqual(exported["results"], result["results"])
        self.assertEqual(len(exported["results"]), 10)

    def test_original_authority_metadata_is_not_taken_from_extractor(self):
        def changed_metadata(documents):
            output = unavailable(documents)
            for item in output["documents"]:
                item["document"]["status"] = "evil"
                item["document"]["approved_by"] = "forged"
                item["document"]["customer_name"] = "SK하이닉스"
                item["document"]["customer_sources"] = [{"page": 1, "quote": "forged customer"}]
                item["pages"] = [{"page": 1, "text": "forged page"}]
            return output
        self.app.state.extract_upload = changed_metadata
        uploaded = self.upload()
        derived = self.wait(self.start(uploaded))["upload"]
        for before, after in zip(uploaded["documents"], derived["documents"]):
            self.assertEqual(after["status"], before["status"])
            self.assertEqual(after["approved_by"], before["approved_by"])
            self.assertEqual(after["customer_name"], before["customer_name"])
            self.assertEqual(after["customer_sources"], before["customer_sources"])
            page = self.client.get(f"/api/uploads/{derived['upload_id']}/documents/{after['id']}/pages/1")
            self.assertNotEqual(page.json()["text"], "forged page")
        self.assertEqual(derived["customer_check"], uploaded["customer_check"])

    def test_identity_change_fails_without_new_snapshot(self):
        def identity_change(documents):
            output = unavailable(documents)
            output["documents"][0]["document"]["id"] = "FORGED"
            return output
        self.app.state.extract_upload = identity_change
        uploaded = self.upload()
        self.assertEqual(self.wait(self.start(uploaded))["status"], "failed")
        self.assertEqual(len(list((self.data / "sets").iterdir())), 1)

    def test_in_place_extractor_cannot_mutate_source_authority(self):
        def changes_input(documents):
            documents[0]["document"]["approved_by"] = "forged in place"
            documents[0]["pages"] = [{"page": 1, "text": "forged page"}]
            return unavailable(documents)
        self.app.state.extract_upload = changes_input
        uploaded = self.upload()
        derived = self.wait(self.start(uploaded))["upload"]
        self.assertEqual(derived["documents"][0]["approved_by"], uploaded["documents"][0]["approved_by"])
        self.assertEqual(self.client.get(f"/api/uploads/{uploaded['upload_id']}").json(), uploaded)

    def test_concurrent_model_jobs_are_rejected(self):
        barrier = threading.Event()
        self.addCleanup(barrier.set)
        def blocked(documents):
            barrier.wait(2)
            return unavailable(documents)
        self.app.state.extract_upload = blocked
        uploaded = self.upload()
        job_id = self.start(uploaded)
        duplicate = self.client.post(f"/api/uploads/{uploaded['upload_id']}/extractions")
        self.assertEqual(duplicate.status_code, 409)
        barrier.set()
        self.assertEqual(self.wait(job_id)["status"], "completed")

    def test_restart_reports_interrupted_job_instead_of_forever_running(self):
        job_id = "a" * 32
        write_json(self.data / "extractions" / f"{job_id}.json", {"job_id": job_id, "status": "running"})
        response = self.client.get(f"/api/extractions/{job_id}")
        self.assertEqual(response.json()["status"], "failed")

    def test_job_poll_is_event_loop_atomic_and_completed_record_stays_completed(self):
        endpoint = next(route.endpoint for route in self.app.routes
                        if getattr(route, "path", None) == "/api/extractions/{job_id}")
        self.assertTrue(inspect.iscoroutinefunction(endpoint))
        uploaded = self.upload()
        job_id = self.start(uploaded)
        completed = self.wait(job_id)
        for _ in range(10):
            self.assertEqual(self.client.get(f"/api/extractions/{job_id}").json(), completed)

    def test_template_upload_cannot_start_ai_extraction(self):
        uploaded = self.upload("template", "D01")
        response = self.client.post(f"/api/uploads/{uploaded['upload_id']}/extractions")
        self.assertEqual(response.status_code, 422)

    def test_job_id_traversal_is_rejected(self):
        self.assertEqual(self.client.get("/api/extractions/not-an-id").status_code, 404)

    def test_changed_saved_pdf_fails_without_new_snapshot(self):
        uploaded = self.upload()
        (self.data / "sets" / uploaded["upload_id"] / "customer_spec.pdf").write_bytes(b"changed test data")
        self.assertEqual(self.wait(self.start(uploaded))["status"], "failed")
        self.assertEqual(len(list((self.data / "sets").iterdir())), 1)

    def test_idempotent_simultaneous_retries_share_one_running_worker(self):
        uploaded = self.upload()
        key = "1" * 32
        release = threading.Event()
        started = threading.Event()
        calls = []
        self.addCleanup(release.set)

        def blocked(documents):
            calls.append(1)
            started.set()
            release.wait(3)
            return unavailable(documents)

        self.app.state.extract_upload = blocked
        url = f"/api/uploads/{uploaded['upload_id']}/extractions"
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.client.post, url, headers={"Idempotency-Key": key}) for _ in range(2)]
            responses = [future.result(timeout=3) for future in futures]
        self.assertTrue(started.wait(1))
        self.assertEqual([response.status_code for response in responses], [202, 202])
        self.assertEqual({response.json()["status"] for response in responses}, {"running"})
        self.assertEqual(len({response.json()["job_id"] for response in responses}), 1)
        self.assertEqual(len(calls), 1)
        job_id = responses[0].json()["job_id"]
        mapping = json.loads((self.data / "extraction_requests" / f"{key}.json").read_text())
        self.assertEqual(mapping, {"upload_id": uploaded["upload_id"], "job_id": job_id})
        self.assertEqual(len(list((self.data / "extractions").glob("*.json"))), 1)
        release.set()
        self.assertEqual(self.wait(job_id)["status"], "completed")

    def test_idempotent_completed_retry_returns_same_snapshot_without_worker(self):
        uploaded = self.upload()
        key = "2" * 32
        calls = []

        def counted(documents):
            calls.append(1)
            return unavailable(documents)

        self.app.state.extract_upload = counted
        url = f"/api/uploads/{uploaded['upload_id']}/extractions"
        first = self.client.post(url, headers={"Idempotency-Key": key})
        self.assertEqual(first.status_code, 202)
        completed = self.wait(first.json()["job_id"])
        before = {str(p.relative_to(self.data)): p.read_bytes() for p in self.data.rglob("*") if p.is_file()}
        for _ in range(2):
            repeated = self.client.post(url, headers={"Idempotency-Key": key})
            self.assertEqual(repeated.status_code, 202)
            self.assertEqual(repeated.json(), completed)
        self.assertEqual(len(calls), 1)
        self.assertEqual(before, {str(p.relative_to(self.data)): p.read_bytes() for p in self.data.rglob("*") if p.is_file()})

    def test_idempotent_failed_retry_does_not_restart_worker(self):
        uploaded = self.upload()
        key = "3" * 32
        calls = []

        def failure(documents):
            calls.append(1)
            raise ValueError("Synthetic extraction failure")

        self.app.state.extract_upload = failure
        url = f"/api/uploads/{uploaded['upload_id']}/extractions"
        first = self.client.post(url, headers={"Idempotency-Key": key})
        self.assertEqual(first.status_code, 202)
        failed = self.wait(first.json()["job_id"])
        self.assertEqual(failed["status"], "failed")
        repeated = self.client.post(url, headers={"Idempotency-Key": key})
        self.assertEqual(repeated.status_code, 202)
        self.assertEqual(repeated.json(), failed)
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.app.state.ai_gate.locked())
        self.app.state.extract_upload = unavailable
        fresh = self.client.post(url, headers={"Idempotency-Key": "4" * 32})
        self.assertEqual(fresh.status_code, 202)
        self.assertNotEqual(fresh.json()["job_id"], failed["job_id"])
        self.assertEqual(self.wait(fresh.json()["job_id"])["status"], "completed")

    def test_idempotency_key_cannot_be_reused_for_a_different_upload(self):
        first_upload = self.upload()
        second_upload = self.upload()
        key = "5" * 32
        first = self.client.post(f"/api/uploads/{first_upload['upload_id']}/extractions",
                                 headers={"Idempotency-Key": key})
        self.assertEqual(first.status_code, 202)
        self.wait(first.json()["job_id"])
        before = {str(p.relative_to(self.data)): p.read_bytes() for p in self.data.rglob("*") if p.is_file()}
        repeated = self.client.post(f"/api/uploads/{second_upload['upload_id']}/extractions",
                                    headers={"Idempotency-Key": key})
        self.assertEqual(repeated.status_code, 409)
        self.assertEqual(before, {str(p.relative_to(self.data)): p.read_bytes() for p in self.data.rglob("*") if p.is_file()})

    def test_idempotent_retry_after_app_restart_marks_running_job_failed(self):
        uploaded = self.upload()
        key, job_id = "6" * 32, "7" * 32
        write_json(self.data / "extractions" / f"{job_id}.json", {"job_id": job_id, "status": "running"})
        write_json(self.data / "extraction_requests" / f"{key}.json",
                   {"upload_id": uploaded["upload_id"], "job_id": job_id})
        restarted = create_app(self.data)
        calls = []
        restarted.state.extract_upload = lambda documents: calls.append(documents)
        with TestClient(restarted) as client:
            url = f"/api/uploads/{uploaded['upload_id']}/extractions"
            response = client.post(url, headers={"Idempotency-Key": key})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json()["job_id"], job_id)
            self.assertEqual(response.json()["status"], "failed")
            self.assertIn("재시작", response.json()["error"])
            self.assertEqual(client.get(f"/api/extractions/{job_id}").json(), response.json())
            self.assertEqual(client.post(url, headers={"Idempotency-Key": key}).json(), response.json())
        self.assertEqual(calls, [])
        self.assertFalse(restarted.state.ai_gate.locked())
        self.assertEqual(len(list((self.data / "sets").iterdir())), 1)

    def test_invalid_idempotency_keys_are_rejected_without_side_effects(self):
        uploaded = self.upload()
        url = f"/api/uploads/{uploaded['upload_id']}/extractions"
        headers = [{"Idempotency-Key": value} for value in ("", "A" * 32, "a" * 31, "a" * 33, "g" * 32, "../bad")]
        headers.append([("Idempotency-Key", "a" * 32), ("Idempotency-Key", "b" * 32)])
        for value in headers:
            with self.subTest(headers=value):
                response = self.client.post(url, headers=value)
                self.assertEqual(response.status_code, 422, response.text)
        self.assertFalse((self.data / "extractions").exists())
        self.assertFalse((self.data / "extraction_requests").exists())
        self.assertFalse(self.app.state.ai_gate.locked())
        self.assertEqual(self.app.state.extraction_tasks, {})

    def test_customer_hold_precedes_idempotency_and_creates_no_records(self):
        uploaded = self.upload(case="C01")
        self.assertFalse(uploaded["customer_check"]["can_compare"])
        before = {str(p.relative_to(self.data)): p.read_bytes() for p in self.data.rglob("*") if p.is_file()}
        for key in ("8" * 32, "invalid"):
            response = self.client.post(f"/api/uploads/{uploaded['upload_id']}/extractions",
                                        headers={"Idempotency-Key": key})
            self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(before, {str(p.relative_to(self.data)): p.read_bytes() for p in self.data.rglob("*") if p.is_file()})
        self.assertFalse((self.data / "extractions").exists())
        self.assertFalse((self.data / "extraction_requests").exists())
        self.assertFalse(self.app.state.ai_gate.locked())
        self.assertEqual(self.app.state.extraction_tasks, {})

    def test_idempotency_mapping_write_failure_cleans_new_job_and_releases_gate(self):
        uploaded = self.upload()
        key = "9" * 32
        calls = []

        def counted(documents):
            calls.append(1)
            return unavailable(documents)

        def broken_mapping_write(path, value):
            if path.parent.name == "extraction_requests":
                raise OSError("Synthetic request-map write failure")
            return write_json(path, value)

        self.app.state.extract_upload = counted
        url = f"/api/uploads/{uploaded['upload_id']}/extractions"
        with patch("handoff_review.web_app.write_json", side_effect=broken_mapping_write):
            with self.assertRaises(OSError):
                self.client.post(url, headers={"Idempotency-Key": key})
        self.assertEqual(calls, [])
        self.assertEqual(list((self.data / "extractions").glob("*.json")), [])
        self.assertFalse((self.data / "extraction_requests" / f"{key}.json").exists())
        self.assertFalse(self.app.state.ai_gate.locked())
        repeated = self.client.post(url, headers={"Idempotency-Key": key})
        self.assertEqual(repeated.status_code, 202)
        self.assertEqual(self.wait(repeated.json()["job_id"])["status"], "completed")
        self.assertEqual(len(calls), 1)

    def test_job_source_is_saved_input_across_running_completed_and_replay(self):
        uploaded = self.upload()
        original_id = uploaded["upload_id"]
        release = threading.Event()
        self.addCleanup(release.set)

        def forged_result_provenance(documents):
            release.wait(3)
            result = unavailable(documents)
            result["source_upload_id"] = "f" * 32
            return result

        self.app.state.extract_upload = forged_result_provenance
        url = f"/api/uploads/{original_id}/extractions"
        headers = {"Idempotency-Key": "a" * 32}
        response = self.client.post(url, headers=headers)
        self.assertEqual(response.status_code, 202)
        running = response.json()
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["source_upload_id"], original_id)
        self.assertEqual(self.client.get(f"/api/extractions/{running['job_id']}").json()["source_upload_id"], original_id)
        release.set()
        completed = self.wait(running["job_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["source_upload_id"], original_id)
        self.assertNotEqual(completed["upload"]["upload_id"], original_id)
        self.assertEqual(self.client.post(url, headers=headers).json(), completed)
        saved = json.loads((self.data / "extractions" / f"{running['job_id']}.json").read_text())
        self.assertEqual(saved["source_upload_id"], original_id)

    def test_failed_worker_keeps_original_source_upload(self):
        uploaded = self.upload()
        original_id = uploaded["upload_id"]

        def failure(documents):
            raise RuntimeError("Synthetic worker failure")

        self.app.state.extract_upload = failure
        job = self.wait(self.start(uploaded))
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["source_upload_id"], original_id)
        self.assertEqual(self.client.get(f"/api/extractions/{job['job_id']}").json(), job)
        self.assertFalse(self.app.state.ai_gate.locked())
        self.assertEqual(self.client.get(f"/api/uploads/{original_id}").json(), uploaded)

    def test_cancelled_worker_keeps_original_source_upload(self):
        uploaded = self.upload()

        def cancelled(documents):
            raise asyncio.CancelledError()

        self.app.state.extract_upload = cancelled
        job = self.wait(self.start(uploaded))
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["source_upload_id"], uploaded["upload_id"])
        self.assertIn("서버 종료", job["error"])
        self.assertFalse(self.app.state.ai_gate.locked())

    def test_restart_preserves_existing_job_source_without_restarting_worker(self):
        uploaded = self.upload()
        key, job_id = "b" * 32, "c" * 32
        write_json(self.data / "extractions" / f"{job_id}.json",
                   {"job_id": job_id, "source_upload_id": uploaded["upload_id"], "status": "running"})
        write_json(self.data / "extraction_requests" / f"{key}.json",
                   {"upload_id": uploaded["upload_id"], "job_id": job_id})
        restarted = create_app(self.data)
        calls = []
        restarted.state.extract_upload = lambda documents: calls.append(documents)
        with TestClient(restarted) as client:
            response = client.post(f"/api/uploads/{uploaded['upload_id']}/extractions",
                                   headers={"Idempotency-Key": key})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json()["status"], "failed")
            self.assertEqual(response.json()["source_upload_id"], uploaded["upload_id"])
            self.assertEqual(client.get(f"/api/extractions/{job_id}").json(), response.json())
        self.assertEqual(calls, [])
        saved = json.loads((self.data / "extractions" / f"{job_id}.json").read_text())
        self.assertEqual(saved["source_upload_id"], uploaded["upload_id"])

    def test_legacy_job_source_is_never_inferred_from_idempotency_mapping(self):
        uploaded = self.upload()
        calls = []
        self.app.state.extract_upload = lambda documents: calls.append(documents)
        for index, status in enumerate(("running", "completed", "failed"), 1):
            with self.subTest(status=status):
                key, job_id = f"{index + 100:032x}", f"{index + 200:032x}"
                write_json(self.data / "extractions" / f"{job_id}.json",
                           {"job_id": job_id, "status": status})
                write_json(self.data / "extraction_requests" / f"{key}.json",
                           {"upload_id": uploaded["upload_id"], "job_id": job_id})
                response = self.client.post(f"/api/uploads/{uploaded['upload_id']}/extractions",
                                            headers={"Idempotency-Key": key})
                self.assertEqual(response.status_code, 202)
                self.assertNotIn("source_upload_id", response.json())
                self.assertNotIn("source_upload_id", self.client.get(f"/api/extractions/{job_id}").json())
                saved = json.loads((self.data / "extractions" / f"{job_id}.json").read_text())
                self.assertNotIn("source_upload_id", saved)
        self.assertEqual(calls, [])
