"""Read-only report regression tests: synthetic saved snapshots, no model/server."""
from copy import deepcopy
from html import escape
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from handoff_review.contracts import FIELD_SPECS
from handoff_review.report import render_report
from handoff_review.web_app import create_app, write_json


REVIEW_ID = "a" * 32
UPLOAD_ID = "b" * 32


class Tags(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def saved_review():
    """Manually authored saved states, not a re-run of the review engine."""
    documents = [
        {"id": doc_id, "title": title, "filename": f"{doc_id}.pdf", "role": role,
         "project_id": "SYNTHETIC-REPORT", "customer_name": "한전",
         "status": "approved", "approved_by": "DEMO-REVIEWER",
         "supersedes": "SPEC-001" if role == "amendment" else None, "page_count": 2}
        for doc_id, title, role in (
            ("SPEC-001", "가상 규격서", "customer_spec"),
            ("AMD-001", "가상 변경서", "amendment"),
            ("HANDOFF-001", "가상 인계서", "handoff"),
        )
    ]
    rows = []
    for spec, value in zip(FIELD_SPECS, (31500, 154000, 22900, 60, 3, "Yyn0", "ONAF", 440, "DETC", "옥외형")):
        expected_source = {"document_id": "SPEC-001", "title": "가상 규격서",
                           "page": 2, "quote": f"{spec.label}: {value}"}
        actual_source = {"document_id": "HANDOFF-001", "title": "가상 인계서",
                         "page": 2, "quote": f"{spec.label}: {value}"}
        rows.append({
            "field": spec.key, "label": spec.label, "status": "MATCH",
            "reason": "저장된 가상 사례의 비교 결과입니다.",
            "expected": {"value": value, "unit": spec.canonical_unit, "source": expected_source},
            "actual": {"value": value, "unit": spec.canonical_unit, "source": actual_source},
            "sources": [deepcopy(expected_source), deepcopy(actual_source)],
            "evidence_complete": True,
            "evidence_check": {"version": "original-page-evidence-v2", "status": "linked",
                               "linked_source_count": 2, "invalid_source_count": 0,
                               "scope": "saved original-page binding"},
        })
    return {
        "review_id": REVIEW_ID, "upload_id": UPLOAD_ID, "created_at": "2026-10-20T01:02:03+00:00",
        "record_kind": "research_review_not_approval", "extraction_method": "local_tool_rag",
        "policy_confirmed": True,
        "policy": {"base_id": "SPEC-001", "handoff_id": "HANDOFF-001",
                   "approved_amendment_ids": ["AMD-001"]},
        "summary": {"MATCH": 10, "MISMATCH": 0, "MISSING": 0, "NEEDS_REVIEW": 0},
        "results": rows, "documents": documents,
        "customer_check": {"version": "customer-identity-v1", "status": "MATCH",
                           "can_compare": True, "message": "가상 고객사 별칭 일치",
                           "documents": [{"document_id": "SPEC-001", "raw_name": "한전",
                                          "canonical_id": "KEPCO", "status": "recognized",
                                          "sources": [{"page": 1, "quote": "고객사: 한전"}]}]},
        "warnings": [], "limitations": ["합성 개발 사례이며 실제 업무 정확도 시험이 아닙니다."],
    }


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="handoff-report-test-")
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.app = create_app(self.data)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.record = saved_review()
        self.path = self.data / "reviews" / f"{REVIEW_ID}.json"
        self.seed()

    def seed(self):
        write_json(self.path, self.record)

    def get_report(self):
        response = self.client.get(f"/api/reviews/{REVIEW_ID}/report")
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def hashes(self):
        return {str(path.relative_to(self.data)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.data.rglob("*") if path.is_file()}

    def test_normal_report_assets_controls_and_result_first(self):
        response = self.get_report()
        html = response.text
        self.assertIn("text/html", response.headers["content-type"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("script-src 'self'", response.headers["content-security-policy"])
        self.assertEqual(html.count('<th scope="row">'), 10)
        self.assertIn("저장된 10개 항목", html)
        self.assertIn("회의용 검토 보고서", html)
        self.assertIn(self.record["created_at"], html)
        self.assertIn("AMD-001", html)
        self.assertIn("추가 경고 없음", html)
        self.assertLess(html.index("<h2>저장된 요약"), html.index("<h2>항목별 저장 결과"))
        self.assertLess(html.index("<h2>항목별 저장 결과"), html.index("<h2>고객사 기본정보"))
        parsed = Tags(html)
        self.assertIn(("button", {"id": "print-button", "type": "button"}), parsed.tags)
        self.assertIn(("p", {"id": "print-error", "role": "alert", "hidden": None}), parsed.tags)
        for filename in ("report.css", "report.js"):
            self.assertIn(f'"/assets/{filename}"', html)
            self.assertEqual(self.client.get(f"/assets/{filename}").status_code, 200)
        self.assertEqual(self.client.get("/assets/other.css").status_code, 404)

    def test_saved_mismatch_is_not_reclassified_or_summarized(self):
        row = self.record["results"][7]
        row["status"] = "MISMATCH"
        row["actual"]["value"] = 380
        row["reason"] = "저장된 팬 전원 440 V와 380 V 불일치"
        self.record["summary"] = {"MATCH": 9, "MISMATCH": 1, "MISSING": 0, "NEEDS_REVIEW": 0}
        self.seed()
        html = self.get_report().text
        self.assertIn("440", html)
        self.assertIn("380", html)
        self.assertIn("<code>MISMATCH</code>", html)
        self.assertIn(escape(row["reason"]), html)
        self.assertIn("보고서에서 다시 계산하지 않습니다", html)

    def test_held_null_candidate_and_missing_evidence_are_explicit(self):
        row = self.record["results"][0]
        row.update(status="NEEDS_REVIEW", expected=None, actual=None, evidence_complete=False)
        row["evidence_check"]["status"] = "review_required"
        row["sources"] = [{"document_id": "SPEC-001", "title": "가상 규격서", "page": 2,
                           "quote": "정격용량: 상충 후보", "evidence_kind": "field_occurrence",
                           "occurrence_count": 2}]
        self.record["results"][1].update(status="MISSING", expected=None, actual=None, sources=[])
        self.seed()
        html = self.get_report().text
        self.assertIn("null (저장값)", html)
        self.assertIn("판정 보류 상태", html)
        self.assertIn("확정된 요구값·인계값이나 승인값을 뜻하지 않습니다", html)
        self.assertIn("저장된 반복 횟수: 2", html)
        self.assertIn("저장된 원문 근거 목록: []", html)
        self.assertIn("<code>MISSING</code>", html)

    def test_customer_hold_preserves_raw_conflict_and_all_held_rows(self):
        self.record["customer_check"].update(status="MISMATCH", can_compare=False)
        self.record["customer_check"]["documents"][0]["raw_name"] = ["한전", "SK하이닉스"]
        for row in self.record["results"]:
            row.update(status="NEEDS_REVIEW", expected=None, actual=None, evidence_complete=False)
        self.record["summary"] = {"MATCH": 0, "MISMATCH": 0, "MISSING": 0, "NEEDS_REVIEW": 10}
        self.seed()
        html = self.get_report().text
        self.assertEqual(html.count('<th scope="row">'), 10)
        self.assertEqual(html.count("<code>NEEDS_REVIEW</code>"), 20)
        self.assertIn("SK하이닉스", html)
        self.assertIn("<dd>false</dd>", html)
        self.assertNotIn("고객사 미검증 과거기록", html)

    def test_legacy_eight_rows_summary_and_old_evidence_remain_unchanged(self):
        self.record["results"] = self.record["results"][:8]
        self.record["summary"]["MATCH"] = 8
        self.record.pop("customer_check")
        for row in self.record["results"]:
            row.pop("evidence_check")
        self.seed()
        html = self.get_report().text
        self.assertEqual(html.count('<th scope="row">'), 8)
        self.assertIn("저장된 8개 항목", html)
        self.assertNotIn("저장된 10개 항목", html)
        self.assertIn("고객사 미검증 과거기록", html)
        self.assertIn("현재 방식의 근거 완전성 보증이 아닙니다", html)
        self.assertEqual(self.client.get(f"/api/reviews/{REVIEW_ID}/export.json").json(), self.record)

    def test_missing_metadata_remains_missing(self):
        for key in ("created_at", "policy", "documents", "summary", "customer_check"):
            self.record.pop(key)
        self.seed()
        html = self.get_report().text
        self.assertIn("생성 시각 메타데이터가 없거나 null", html)
        self.assertIn("기록 없음 (키 없음)", html)
        self.assertNotIn("2026-10-20", html)
        self.assertIn("원본 링크 생략", html)

    def test_unknown_and_malformed_statuses_render_without_inventing_verdicts(self):
        for status in ("FUTURE", None, [], {"new": "unknown"}):
            with self.subTest(status=status):
                self.record["customer_check"]["status"] = status
                self.record["results"][0]["status"] = status
                self.record["summary"]["MATCH"] = 999
                self.seed()
                html = self.get_report().text
                self.assertIn("알 수 없는 저장 상태", html)
                self.assertIn("알 수 없는 고객사 메타데이터", html)
                self.assertIn("<dd>999</dd>", html)

    def test_long_quote_is_preserved_in_full(self):
        quote = ("긴 원문 <원문> & 인용\n" * 1200) + "마지막 인용 표식"
        self.record["results"][0]["sources"][0]["quote"] = quote
        self.seed()
        self.assertIn(escape(quote, quote=True), self.get_report().text)

    def test_all_untrusted_html_and_saved_urls_are_inert(self):
        payload = '<img src="https://invalid.example/x" onerror="alert(1)"><script>bad()</script>&'
        row = self.record["results"][0]
        row.update(label=payload, field=payload, status=payload, reason=payload)
        row["expected"].update(value=payload, unit=payload, condition=payload)
        row["sources"][0].update(title=payload, quote=payload,
                                 pdf_url="javascript:alert(1)", page_url="https://invalid.example/")
        self.record["documents"][0].update(title=payload, filename=payload, customer_name=payload)
        self.record["customer_check"]["message"] = payload
        self.record["customer_check"]["documents"][0]["raw_name"] = payload
        self.record["policy"]["approved_amendment_ids"] = [payload]
        self.seed()
        html = self.get_report().text
        self.assertIn(escape(payload, quote=True), html)
        parsed = Tags(html)
        self.assertEqual([attrs for tag, attrs in parsed.tags if tag == "script"],
                         [{"src": "/assets/report.js", "defer": None}])
        self.assertFalse(any(tag in {"img", "iframe", "object"} for tag, _ in parsed.tags))
        for _, attrs in parsed.tags:
            self.assertFalse(any(key.startswith("on") for key in attrs))
            for key in ("href", "src"):
                if key in attrs:
                    self.assertTrue(attrs[key].startswith(("/assets/", "/api/uploads/")), attrs[key])
        self.assertNotIn("javascript:", html)
        self.assertNotIn('href="https:', html)

    def test_pdf_links_require_valid_stored_identity_page_and_unique_document(self):
        link = f"/api/uploads/{UPLOAD_ID}/documents/SPEC-001/pdf#page=2"
        self.assertIn(f'href="{link}"', render_report(self.record))
        for change in ("upload", "unknown_document", "page", "bool_page", "page_count", "duplicate"):
            with self.subTest(change=change):
                record = deepcopy(self.record)
                if change == "upload":
                    record["upload_id"] = "../../unsafe"
                elif change == "unknown_document":
                    record["documents"] = []
                elif change == "duplicate":
                    record["documents"].append(deepcopy(record["documents"][0]))
                elif change == "page_count":
                    record["documents"][0]["page_count"] = "2"
                else:
                    for row in record["results"]:
                        row["sources"][0]["page"] = True if change == "bool_page" else 3
                        row["expected"]["source"]["page"] = row["sources"][0]["page"]
                self.assertNotIn(f'href="{link}"', render_report(record))

    def test_legacy_value_source_quote_is_preserved(self):
        row = self.record["results"][0]
        row.pop("sources")
        row["expected"]["source"]["quote"] = "과거 요구값 출처의 전체 인용"
        self.seed()
        html = self.get_report().text
        self.assertIn("과거 요구값 출처의 전체 인용", html)
        self.assertIn("요구값의 저장 출처", html)

    def test_unknown_review_and_invalid_identifier_are_404_without_writes(self):
        before = self.hashes()
        for identifier in ("f" * 32, "not-an-id"):
            self.assertEqual(self.client.get(f"/api/reviews/{identifier}/report").status_code, 404)
        self.assertEqual(self.hashes(), before)

    def test_get_report_is_read_only_zero_ai_and_preserves_export(self):
        before = self.hashes()
        original = deepcopy(self.record)
        self.app.state.extract_upload = Mock(side_effect=AssertionError("no extraction"))
        self.app.state.parse_pdf = Mock(side_effect=AssertionError("no PDF parse"))
        with patch("handoff_review.web_app.review_case", side_effect=AssertionError("no review")) as review, \
             patch("handoff_review.web_app.explain_review", side_effect=AssertionError("no AI")) as explain, \
             patch("handoff_review.web_app.write_json", side_effect=AssertionError("no write")) as write:
            for _ in range(2):
                self.get_report()
            exported = self.client.get(f"/api/reviews/{REVIEW_ID}/export.json").json()
        review.assert_not_called()
        explain.assert_not_called()
        write.assert_not_called()
        self.app.state.extract_upload.assert_not_called()
        self.app.state.parse_pdf.assert_not_called()
        self.assertEqual(self.hashes(), before)
        self.assertEqual(exported, original)
        render_report(self.record)
        self.assertEqual(self.record, original)
        self.assertEqual(json.loads(self.path.read_text()), original)


if __name__ == "__main__":
    unittest.main()
