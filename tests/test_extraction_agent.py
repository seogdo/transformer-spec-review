"""Mock-model tests of actual keyword retrieval and original-text validation.

No fixture answer map, live model, API key or network is used here. Values below
are explicitly authored test inputs/proposals. The production agent must not
produce those values when the mock model does not propose them.
"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from handoff_review import review_case
from handoff_review import extraction_agent as agent
from handoff_review.contracts import FIELD_KEYS

FIELDS = [
    ("rated_power", "정격용량", "1000", "kVA"),
    ("hv_voltage", "고압측 정격전압", "22", "kV"),
    ("lv_voltage", "저압측 정격전압", "400", "V"),
    ("frequency", "정격주파수", "60", "Hz"),
    ("phases", "상 수", "3", "count"),
    ("vector_group", "결선 그룹", "Dyn11", ""),
    ("cooling_type", "냉각 방식", "ONAF", ""),
    ("tap_changer_type", "탭절환기 방식", "DETC", ""),
    ("installation_type", "설치 구분", "옥내형", ""),
    ("fan_supply_voltage", "냉각팬 전원전압", "380", "V"),
]
EXAMPLE_FIELDS = 2 * len(FIELDS) + 1


def example():
    documents, proposals = [], []
    for identifier, role in (("SPEC", "customer_spec"), ("AMD", "amendment"), ("HANDOFF", "handoff")):
        lines = []
        for field, alias, value, unit in FIELDS:
            if role == "amendment" and field != "fan_supply_voltage":
                continue
            if role == "amendment":
                value = "440"
            rendered = "3상" if field == "phases" else f"{value} {unit}".strip()
            line = f"{alias}: {rendered}"
            lines.append(line)
            proposals.append({"document_id": identifier, "field": field, "value": value,
                              "unit": unit, "page": 1, "quote": line})
        documents.append({"document": {"id": identifier, "role": role, "title": identifier,
                                      "project_id": "TEST-PROJECT", "status": "approved",
                                      "customer_name": "한전",
                                      "approved_by": "demo", "supersedes": "SPEC" if role == "amendment" else None,
                                      "fields": {}},
                          "pages": [{"page": 1, "text": "\n".join(lines)}],
                          "warnings": [{"code": "AI_EXTRACTION_PENDING", "message": "pending"}],
                          "extraction_method": "pending"})
    return documents, proposals


def plan_for(documents):
    queries = []
    for item in documents:
        for field in agent._scopes(item):
            queries.append({"document_id": item["document"]["id"], "field": field,
                            "query": agent.FIELD_ALIASES[field][0]})
    return {"tool": "search_documents", "queries": queries}


def run_with(documents, proposals, plan=None):
    request = MagicMock(side_effect=[plan if plan is not None else plan_for(documents), {"items": proposals}])
    return agent.extract_upload(documents, request_fn=request), request


def field_of(result, document_id="HANDOFF", field="fan_supply_voltage"):
    return next(item for item in result["documents"] if item["document"]["id"] == document_id)["document"]["fields"][field]


class ExtractionAgentTests(unittest.TestCase):
    def setUp(self):
        self.documents, self.proposals = example()

    def assertUncertain(self, entry):
        self.assertEqual(entry["value"], ["EXTRACTION_UNCERTAIN"])

    def test_two_calls_execute_search_and_accept_literal_values(self):
        before = deepcopy(self.documents)
        result, request = run_with(self.documents, self.proposals)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["accepted_fields"], EXAMPLE_FIELDS)
        self.assertEqual(result["proposed_fields"], EXAMPLE_FIELDS)
        self.assertEqual(result["llm_calls"], 2)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(self.documents, before)
        self.assertEqual(field_of(result)["value"], "380")
        tool_result = json.loads(request.call_args_list[1].args[0]["messages"][1]["content"])
        self.assertEqual(tool_result["tool"], "search_documents")
        self.assertEqual(len(tool_result["results"]), EXAMPLE_FIELDS)
        self.assertIn("냉각팬 전원전압: 440 V", [hit["text"] for hit in tool_result["results"]])
        self.assertTrue(any(row["tool"] == "search_documents" and row["retrieval"] == "keyword_alias_not_vector" for row in result["trace"]))
        for original, extracted in zip(before, result["documents"]):
            self.assertEqual({k: v for k, v in original["document"].items() if k != "fields"}, {k: v for k, v in extracted["document"].items() if k != "fields"})
            self.assertEqual(original["pages"], extracted["pages"])
            self.assertFalse(any(warning.get("code") == "AI_EXTRACTION_PENDING" for warning in extracted["warnings"]))

    def test_verified_model_values_flow_into_existing_code_review(self):
        result, _ = run_with(self.documents, self.proposals)
        review = review_case({"case_id": "real-extraction-test", "documents": [item["document"] for item in result["documents"]],
                              "review_policy": {"base_id": "SPEC", "handoff_id": "HANDOFF", "approved_amendment_ids": ["AMD"]}})
        self.assertEqual(review["summary"]["MATCH"], 9)
        self.assertEqual(review["summary"]["MISMATCH"], 1)

    def test_phases_three_sang_is_validated_as_count(self):
        result, _ = run_with(self.documents, self.proposals)
        entry = field_of(result, field="phases")
        self.assertEqual((entry["value"], entry["unit"]), ("3", "count"))
        self.assertIn("상 수: 3상", entry["quote"])

    def test_missing_ai_items_never_get_code_extracted_values(self):
        result, _ = run_with(self.documents, [])
        self.assertEqual(result["accepted_fields"], 0)
        self.assertEqual(result["status"], "partial")
        for item in result["documents"]:
            for value in item["document"]["fields"].values():
                self.assertUncertain(value)

    def test_stale_input_fields_are_not_a_fallback(self):
        self.documents[0]["document"]["fields"] = {"rated_power": {"value": "9999", "unit": "kVA"}}
        result, _ = run_with(self.documents, [])
        self.assertUncertain(field_of(result, "SPEC", "rated_power"))
        self.assertNotIn('"9999"', json.dumps(result, ensure_ascii=False))

    def test_missing_single_item_stays_uncertain_not_missing(self):
        result, _ = run_with(self.documents, self.proposals[:-1])
        self.assertEqual(result["accepted_fields"], EXAMPLE_FIELDS - 1)
        self.assertUncertain(field_of(result))

    def test_network_failure_does_not_download_or_infer_values(self):
        request = MagicMock(side_effect=ConnectionRefusedError("offline"))
        result = agent.extract_upload(self.documents, request_fn=request)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["llm_calls"], 1)
        self.assertEqual(result["accepted_fields"], 0)
        self.assertUncertain(field_of(result))

    def test_invalid_tool_plan_is_rejected_without_second_call(self):
        for plan in ({"tool": "delete_files", "queries": []}, {"tool": "search_documents", "queries": []}, None):
            with self.subTest(plan=plan):
                request = MagicMock(return_value=plan)
                result = agent.extract_upload(self.documents, request_fn=request)
                self.assertEqual(result["status"], "partial")
                self.assertEqual(request.call_count, 1)
                self.assertUncertain(field_of(result))

    def test_unknown_document_and_duplicate_queries_are_rejected(self):
        for mutation in ("unknown", "duplicate"):
            plan = plan_for(self.documents)
            if mutation == "unknown":
                plan["queries"][0]["document_id"] = "NOT-UPLOADED"
            else:
                plan["queries"][1] = deepcopy(plan["queries"][0])
            request = MagicMock(return_value=plan)
            result = agent.extract_upload(self.documents, request_fn=request)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(result["accepted_fields"], 0)

    def test_search_miss_does_not_invoke_value_extraction(self):
        for item in self.documents:
            item["pages"][0]["text"] = "No supported field labels in this document."
        request = MagicMock(return_value=plan_for(self.documents))
        result = agent.extract_upload(self.documents, request_fn=request)
        self.assertEqual(result["llm_calls"], 1)
        self.assertEqual(result["accepted_fields"], 0)
        self.assertUncertain(field_of(result))

    def test_real_original_quote_outside_search_result_is_not_accepted(self):
        plan = plan_for(self.documents)
        plan["queries"] = [query for query in plan["queries"] if not (query["document_id"] == "HANDOFF" and query["field"] == "fan_supply_voltage")]
        result, _ = run_with(self.documents, self.proposals, plan)
        self.assertUncertain(field_of(result))
        self.assertIn("검색 도구", field_of(result)["extraction_uncertainty"])

    def test_wrong_value_unit_page_quote_and_metadata_are_rejected(self):
        for key, value in (("value", "440"), ("unit", "kV"), ("page", 2), ("quote", "냉각팬 전원전압: 999 V"), ("approved_by", "AI APPROVED")):
            with self.subTest(key=key):
                proposals = deepcopy(self.proposals)
                proposals[-1][key] = value
                result, _ = run_with(self.documents, proposals)
                self.assertUncertain(field_of(result))
                self.assertEqual(result["documents"][-1]["document"]["approved_by"], "demo")

    def test_unit_conversion_by_model_is_not_a_literal_source_value(self):
        proposal = next(row for row in self.proposals if row["document_id"] == "SPEC" and row["field"] == "hv_voltage")
        proposal.update(value="22000", unit="V")
        result, _ = run_with(self.documents, self.proposals)
        self.assertUncertain(field_of(result, "SPEC", "hv_voltage"))

    def test_model_duplicate_field_is_not_last_value_wins(self):
        self.proposals.append(deepcopy(self.proposals[-1]))
        result, _ = run_with(self.documents, self.proposals)
        self.assertUncertain(field_of(result))

    def test_repeated_document_field_anywhere_is_uncertain_even_when_equal(self):
        for value in ("380", "440"):
            documents = deepcopy(self.documents)
            documents[-1]["pages"].append({"page": 2, "text": f"팬보조전원: {value} V"})
            result, _ = run_with(documents, self.proposals)
            self.assertUncertain(field_of(result))
            self.assertIn("반복", field_of(result)["extraction_uncertainty"])

    def test_model_short_quote_cannot_hide_condition_in_full_line(self):
        self.documents[-1]["pages"][0]["text"] += " (운전 시에만 적용)"
        result, _ = run_with(self.documents, self.proposals)
        self.assertUncertain(field_of(result))
        self.assertIn("조건", field_of(result)["extraction_uncertainty"])

    def test_condition_elsewhere_in_document_is_conservatively_withheld(self):
        self.documents[-1]["pages"].append({"page": 2, "text": "별도 조건: 주변 온도를 담당자가 확인해야 합니다."})
        result, _ = run_with(self.documents, self.proposals)
        for entry in result["documents"][-1]["document"]["fields"].values():
            self.assertUncertain(entry)

    def test_administrative_condition_headings_do_not_erase_valid_values(self):
        prose = ("구성 및 보조설비 조건\n기재 요구조건\n"
                 "수주 후 기술 인계를 위한 가상 요구조건입니다.\n"
                 "변경 합의서가 있는 경우 담당자가 적용 대상과 대체 관계를 확인합니다.\n"
                 "합의서에 없는 요구조건은 이 규격표를 함께 확인합니다.\n"
                 "인계 담당자는 변경 조건의 반영 여부를 원문과 함께 확인합니다.\n")
        for document in self.documents:
            document["pages"].append({"page": 2, "text": prose})
        result, _ = run_with(self.documents, self.proposals)
        self.assertEqual(result["accepted_fields"], EXAMPLE_FIELDS)
        self.assertEqual(result["status"], "completed")

    def test_separate_numeric_temperature_bound_is_not_hidden(self):
        for text in ("단, 40℃ 이상에서만 적용합니다.", "단, 40℃\n이상에서만 적용합니다.", "주위 온도는 20~40℃ 범위입니다."):
            with self.subTest(text=text):
                documents = deepcopy(self.documents)
                documents[-1]["pages"].append({"page": 2, "text": text})
                result, _ = run_with(documents, self.proposals)
                for entry in result["documents"][-1]["document"]["fields"].values():
                    self.assertUncertain(entry)

    def test_inline_field_does_not_capture_administrative_footer_or_page_number(self):
        self.documents[-1]["pages"][0]["text"] += "\n기술 인계 시 확인사항\n변경 합의서가 있는 경우 담당자가 확인합니다.\n1\n"
        result, _ = run_with(self.documents, self.proposals)
        self.assertEqual(result["accepted_fields"], EXAMPLE_FIELDS)

    def test_short_quote_cannot_hide_negative_or_excluded_value(self):
        for suffix in (" 아님", " 사용 금지", " (제외)", "\n적용하지 않습니다.", "\n440 VAC로 변경", "\n440 VAC"):
            with self.subTest(suffix=suffix):
                documents = deepcopy(self.documents)
                documents[-1]["pages"][0]["text"] += suffix
                result, _ = run_with(documents, self.proposals)
                self.assertUncertain(field_of(result))

    def test_example_context_is_not_a_normative_requirement(self):
        for prefix in ("참고 예시: ", "예를 들면 "):
            documents = deepcopy(self.documents)
            documents[-1]["pages"][0]["text"] = documents[-1]["pages"][0]["text"].replace("냉각팬 전원전압: 380 V", prefix + "냉각팬 전원전압: 380 V")
            result, _ = run_with(documents, self.proposals)
            self.assertUncertain(field_of(result))

    def test_flat_requested_pairs_and_exact_count_schema_have_no_answer_values(self):
        _, request = run_with(self.documents, self.proposals)
        payload = request.call_args_list[0].args[0]
        data = json.loads(payload["messages"][1]["content"])
        self.assertEqual(len(data["requested_pairs"]), EXAMPLE_FIELDS)
        self.assertEqual(payload["format"]["properties"]["queries"]["minItems"], EXAMPLE_FIELDS)
        self.assertEqual(payload["format"]["properties"]["queries"]["maxItems"], EXAMPLE_FIELDS)
        self.assertNotIn("1000", payload["messages"][1]["content"])
        self.assertNotIn("380", payload["messages"][1]["content"])

    def test_second_call_has_every_document_field_pair_without_answer_values(self):
        _, request = run_with(self.documents, self.proposals)
        payload = request.call_args_list[1].args[0]
        data = json.loads(payload["messages"][1]["content"])
        expected = [{"document_id": item["document"]["id"], "field": field}
                    for item in self.documents for field in agent._scopes(item)]
        self.assertEqual(data["requested_pairs"], expected)
        self.assertEqual(len(data["requested_pairs"]), EXAMPLE_FIELDS)
        self.assertEqual({pair["document_id"] for pair in expected}, {"SPEC", "AMD", "HANDOFF"})
        self.assertTrue(all(set(pair) == {"document_id", "field"} for pair in data["requested_pairs"]))
        self.assertEqual(sum(pair["field"] == "fan_supply_voltage" for pair in expected), 3)
        system = payload["messages"][0]["content"]
        self.assertIn("Do not stop after the first document", system)
        self.assertIn("Preserve every original space", system)
        self.assertIn("absent or ambiguous", system)
        self.assertNotIn("1000", system)
        self.assertNotIn("380", system)

    def test_first_document_only_model_response_remains_partial_without_fill(self):
        proposals = [row for row in self.proposals if row["document_id"] == "SPEC"]
        result, request = run_with(self.documents, proposals)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["accepted_fields"], len(FIELD_KEYS))
        self.assertEqual(request.call_count, 2)
        for field in FIELD_KEYS:
            self.assertUncertain(field_of(result, field=field))
        self.assertUncertain(field_of(result, "AMD", "fan_supply_voltage"))

    def test_quote_reformatted_spaces_are_still_rejected_not_repaired(self):
        proposal = next(row for row in self.proposals if row["document_id"] == "SPEC" and row["field"] == "hv_voltage")
        proposal["quote"] = proposal["quote"].replace("고압측", "고압 측")
        result, _ = run_with(self.documents, self.proposals)
        self.assertUncertain(field_of(result, "SPEC", "hv_voltage"))
        self.assertIn("원문 페이지에 존재하지", field_of(result, "SPEC", "hv_voltage")["extraction_uncertainty"])

    def test_partial_quote_cannot_hide_ranges_or_extra_values(self):
        for suffix in (" 또는 440 V", ", 440 V", ", 440 VAC", ", 440", " / 440 V", " ~ 440 V"):
            with self.subTest(suffix=suffix):
                documents = deepcopy(self.documents)
                documents[-1]["pages"][0]["text"] += suffix
                result, _ = run_with(documents, self.proposals)
                self.assertUncertain(field_of(result))

    def test_same_excerpt_label_and_unrelated_value_are_not_bound(self):
        self.documents[-1]["pages"][0]["text"] = self.documents[-1]["pages"][0]["text"].replace("냉각팬 전원전압: 380 V", "냉각팬 전원전압: 미정. 별개 기재 380 V")
        self.proposals[-1]["quote"] = "냉각팬 전원전압: 미정. 별개 기재 380 V"
        result, _ = run_with(self.documents, self.proposals)
        self.assertUncertain(field_of(result))
        self.assertIn("직접 연결", field_of(result)["extraction_uncertainty"])

    def test_unapproved_amendment_metadata_stays_unapproved(self):
        self.documents[1]["document"]["status"] = "draft"
        self.documents[1]["document"]["approved_by"] = None
        result, _ = run_with(self.documents, self.proposals)
        self.assertEqual(result["documents"][1]["document"]["status"], "draft")
        review = review_case({"documents": [item["document"] for item in result["documents"]],
                              "review_policy": {"base_id": "SPEC", "handoff_id": "HANDOFF", "approved_amendment_ids": ["AMD"]}})
        fan = next(row for row in review["results"] if row["field"] == "fan_supply_voltage")
        self.assertEqual(fan["status"], "NEEDS_REVIEW")

    def test_request_budget_is_shared_and_at_most_two_calls(self):
        with patch.object(agent.time, "monotonic", side_effect=[0, 0.5, 20, 21, 35]):
            result, request = run_with(self.documents, self.proposals)
        self.assertEqual(result["llm_calls"], 2)
        self.assertEqual(request.call_args_list[0].args[1], 89.5)
        self.assertEqual(request.call_args_list[1].args[1], 69)

    def test_overall_deadline_does_not_get_extended_for_second_call(self):
        with patch.object(agent.time, "monotonic", side_effect=[0, 1, 91]):
            result, request = run_with(self.documents, self.proposals)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(request.call_count, 1)

    def test_invalid_input_id_and_pages_are_rejected(self):
        documents = deepcopy(self.documents)
        documents[1]["document"]["id"] = documents[0]["document"]["id"]
        with self.assertRaises(ValueError):
            agent.extract_upload(documents, request_fn=MagicMock())
        self.documents[0]["pages"][0]["page"] = True
        with self.assertRaises(ValueError):
            agent.extract_upload(self.documents, request_fn=MagicMock())

    def enum_case(self, field, source_text, proposed_value, quote=None):
        """Replace an authored field's entire line without editing metadata."""
        documents, proposals = example()
        row = next(row for row in proposals if row["document_id"] == "HANDOFF" and row["field"] == field)
        original = row["quote"]
        documents[-1]["pages"][0]["text"] = documents[-1]["pages"][0]["text"].replace(original, source_text)
        row.update(value=proposed_value, quote=quote if quote is not None else source_text)
        return run_with(documents, proposals)[0]

    def test_new_enum_supported_aliases_preserve_raw_evidence(self):
        aliases = (
            ("tap_changer_type", "탭절환방식", "부하시 탭절환기", "OLTC"),
            ("tap_changer_type", "탭절환방식", "부하시", "OLTC"),
            ("tap_changer_type", "탭절환기 방식", "무전압 탭절환기", "DETC"),
            ("tap_changer_type", "탭절환기 방식", "무전압", "DETC"),
            ("tap_changer_type", "탭절환기 방식", "detc", "DETC"),
            ("installation_type", "설치 형식", "실내", "옥내형"),
            ("installation_type", "설치 형식", "INDOOR", "옥내형"),
            ("installation_type", "설치 구분", "indoor", "옥내형"),
            ("installation_type", "설치 구분", "옥내", "옥내형"),
            ("installation_type", "설치 구분", "실외", "옥외형"),
            ("installation_type", "설치 구분", "OUTDOOR", "옥외형"),
            ("installation_type", "설치 구분", "옥외", "옥외형"),
        )
        for field, label, literal, canonical in aliases:
            with self.subTest(field=field, literal=literal):
                quote = f"{label}: {literal}"
                result = self.enum_case(field, quote, canonical)
                self.assertEqual(field_of(result, field=field)["value"], canonical, result["warnings"])
                self.assertEqual(field_of(result, field=field)["quote"], quote)
                self.assertEqual(result["accepted_fields"], EXAMPLE_FIELDS, result["warnings"])

    def test_new_enum_literal_proposals_and_repeated_whitespace_are_supported(self):
        quote = "탭절환기 방식: 무전압   탭절환기"
        result = self.enum_case("tap_changer_type", quote, "무전압   탭절환기")
        self.assertEqual(field_of(result, field="tap_changer_type")["value"], "무전압   탭절환기")
        self.assertEqual(result["accepted_fields"], EXAMPLE_FIELDS, result["warnings"])

    def test_bound_tap_alias_in_standalone_table_cell_is_not_a_condition(self):
        source = "탭절환기 방식\n부하시 탭절환기\nA-10"
        result = self.enum_case("tap_changer_type", source, "OLTC", "탭절환기 방식\n부하시 탭절환기")
        self.assertEqual(field_of(result, field="tap_changer_type")["value"], "OLTC", result["warnings"])

    def test_enum_proposal_must_agree_with_actual_source_not_just_be_supported(self):
        for field, source, proposal in (
            ("tap_changer_type", "탭절환기 방식: 무전압 탭절환기", "OLTC"),
            ("installation_type", "설치 구분: INDOOR", "옥외형"),
        ):
            with self.subTest(field=field):
                result = self.enum_case(field, source, proposal)
                self.assertUncertain(field_of(result, field=field))

    def test_new_enum_unknown_multiple_pending_and_qualified_values_are_withheld(self):
        attacks = (
            ("tap_changer_type", "탭절환기 방식: 무부하", "DETC"),
            ("tap_changer_type", "탭절환기 방식: OLTC/DETC", "OLTC"),
            ("tap_changer_type", "탭절환기 방식: OLTC 또는 DETC", "OLTC"),
            ("tap_changer_type", "탭절환기 방식: DETC, 무전압", "DETC"),
            ("tap_changer_type", "탭절환기 방식: DETC 미확정", "DETC"),
            ("tap_changer_type", "탭절환기 방식: DETC 거부", "DETC"),
            ("tap_changer_type", "탭절환기 방식: DETC 아님", "DETC"),
            ("tap_changer_type", "참고 예시: 탭절환기 방식: DETC", "DETC"),
            ("installation_type", "설치 구분: 옥외형 부속함", "옥외형"),
            ("installation_type", "설치 구분: 옥내 설치 시", "옥내형"),
            ("installation_type", "설치 구분: 옥내형/옥외형", "옥내형"),
            ("installation_type", "설치 구분: 옥내형 적용하지 않습니다.", "옥내형"),
            ("installation_type", "설치 구분: 옥내형 (예정)", "옥내형"),
            ("installation_type", "참고 예시: 설치 구분: 옥내형", "옥내형"),
        )
        for field, source, proposal in attacks:
            with self.subTest(source=source):
                # The model's short substring deliberately omits qualifiers.
                label = "탭절환기 방식" if field == "tap_changer_type" else "설치 구분"
                short = f"{label}: {proposal}"
                quote = short if short in source else source
                result = self.enum_case(field, source, proposal, quote)
                self.assertUncertain(field_of(result, field=field))

    def test_accessory_scope_and_adjacent_qualification_are_not_transformer_values(self):
        for source in (
            "제어반 설치 구분: 옥외형",
            "부속함\n설치 구분: 옥외형",
            "설치 구분: 옥외형\n부속함에 한함",
            "설치 구분: 옥외형\n미확정",
            "설치 구분: 옥외형\n옥내 설치 시 적용",
            "설치 구분: 옥외형\nINDOOR",
        ):
            with self.subTest(source=source):
                result = self.enum_case("installation_type", source, "옥외형", "설치 구분: 옥외형")
                self.assertUncertain(field_of(result, field="installation_type"))

    def test_unlabeled_new_enum_value_cannot_bind_to_a_different_target(self):
        result = self.enum_case("installation_type", "부속함: 옥외형", "옥외형")
        self.assertUncertain(field_of(result, field="installation_type"))

    def test_bound_tap_alias_does_not_mask_an_actual_load_condition(self):
        source = "탭절환기 방식: 부하시 탭절환기\n냉각팬 전원전압: 380 V (부하 시)"
        result = self.enum_case("tap_changer_type", source, "OLTC", "탭절환기 방식: 부하시 탭절환기")
        self.assertUncertain(field_of(result, field="tap_changer_type"))

    def test_tap_condition_mask_requires_a_contract_known_whole_alias(self):
        source = "탭절환기 방식: 부하 시"
        self.assertEqual(agent._mask_bound_tap_literal(source), source)
        result = self.enum_case("tap_changer_type", source, "OLTC")
        self.assertUncertain(field_of(result, field="tap_changer_type"))
        # The unrecognized spacing must remain a visible load condition for
        # the document-wide guard, not just fail the new categorical field.
        self.assertUncertain(field_of(result, field="rated_power"))
        self.assertIn("조건", field_of(result, field="rated_power")["extraction_uncertainty"])
        self.assertNotEqual(agent._mask_bound_tap_literal("탭절환기 방식: 부하시"),
                            "탭절환기 방식: 부하시")

    def test_all_three_full_documents_support_thirty_queries_and_proposals(self):
        documents, proposals = example()
        documents[1]["pages"] = deepcopy(documents[0]["pages"])
        proposals = [row for row in proposals if row["document_id"] != "AMD"]
        proposals.extend({**row, "document_id": "AMD"} for row in deepcopy(proposals) if row["document_id"] == "SPEC")
        result, request = run_with(documents, proposals)
        self.assertEqual(result["accepted_fields"], 30, result["warnings"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["llm_calls"], 2)
        search = request.call_args_list[0].args[0]
        extraction = request.call_args_list[1].args[0]
        self.assertEqual(search["format"]["properties"]["queries"]["maxItems"], 30)
        self.assertEqual(extraction["format"]["properties"]["items"]["maxItems"], 30)
        self.assertEqual(len(json.loads(search["messages"][1]["content"])["requested_pairs"]), 30)
        self.assertLessEqual(len(json.dumps(extraction).encode()), agent.MAX_REQUEST_BYTES)
        self.assertLessEqual(extraction["options"]["num_predict"], 6000)
        self.assertEqual(extraction["options"]["num_ctx"], 16384)

    def test_thirty_one_queries_or_proposals_exceed_bounded_contract(self):
        plan = plan_for(self.documents)
        plan["queries"] = (plan["queries"] * 2)[:31]
        result, request = run_with(self.documents, self.proposals, plan)
        self.assertEqual(result["accepted_fields"], 0)
        self.assertEqual(request.call_count, 1)
        result, request = run_with(self.documents, (self.proposals * 2)[:31])
        self.assertEqual(result["accepted_fields"], 0)
        self.assertEqual(request.call_count, 2)

    def test_long_early_evidence_does_not_starve_later_field_queries(self):
        documents = deepcopy(self.documents)
        for item in documents:
            item["pages"][0]["text"] = "\n".join(line + " 내용" * 400 for line in item["pages"][0]["text"].splitlines())
        queries = plan_for(documents)["queries"]
        hits = agent._search_documents(queries, {item["document"]["id"]: item for item in documents})
        self.assertEqual({(hit["document_id"], hit["field"]) for hit in hits},
                         {(query["document_id"], query["field"]) for query in queries})
        self.assertLessEqual(sum(len(hit["text"]) for hit in hits), agent.MAX_RETRIEVED_CHARACTERS)


class ExtractionTransportTests(unittest.TestCase):
    def connection(self, body, status=200, content_length=None):
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = status
        response.getheader.return_value = content_length
        response.read1.side_effect = [body, b""]
        return connection

    def test_local_response_and_model_payload(self):
        response = json.dumps({"done": True, "message": {"content": '{"items":[]}'}}).encode()
        connection = self.connection(response)
        payload = agent._payload("system", {"x": 1}, agent._extraction_schema(), 4000)
        with patch.object(agent.http.client, "HTTPConnection", return_value=connection), patch.dict(agent.os.environ, {"HANDOFF_OLLAMA_URL": "http://127.0.0.1:11435"}):
            result = agent._local_request(payload, 15)
        self.assertEqual(result, {"items": []})
        sent = json.loads(connection.request.call_args.kwargs["body"])
        self.assertEqual(sent["model"], "qwen3.5:2b")
        self.assertFalse(sent["stream"])
        self.assertFalse(sent["think"])
        self.assertEqual(sent["options"]["temperature"], 0)
        connection.close.assert_called_once()

    def test_external_url_is_rejected_before_connection(self):
        with patch.dict(agent.os.environ, {"HANDOFF_OLLAMA_URL": "http://external.example:11434"}), patch.object(agent.http.client, "HTTPConnection") as connection:
            with self.assertRaises(ValueError):
                agent._local_request({}, 5)
        connection.assert_not_called()

    def test_redirect_and_unfinished_response_rejected(self):
        for status, body, error in ((302, b"", OSError), (200, b'{"done":false}', agent.PlanRejected)):
            connection = self.connection(body, status)
            with patch.object(agent.http.client, "HTTPConnection", return_value=connection):
                with self.assertRaises(error):
                    agent._local_request({}, 5)
            connection.close.assert_called_once()

    def test_declared_and_streamed_response_limits(self):
        for body, length in ((b"", str(agent.MAX_RESPONSE_BYTES + 1)), (b"x" * (agent.MAX_RESPONSE_BYTES + 1), None), (b"", "-1")):
            connection = self.connection(body, content_length=length)
            with patch.object(agent.http.client, "HTTPConnection", return_value=connection):
                with self.assertRaises(agent.PlanRejected):
                    agent._local_request({}, 5)

    def test_duplicate_json_keys_are_rejected(self):
        connection = self.connection(b'{"done":true,"done":false}')
        with patch.object(agent.http.client, "HTTPConnection", return_value=connection):
            with self.assertRaises(agent.PlanRejected):
                agent._local_request({}, 5)

    def test_zero_timeout_and_oversized_request_never_connect(self):
        with patch.object(agent.http.client, "HTTPConnection") as connection:
            with self.assertRaises(TimeoutError):
                agent._local_request({}, 0)
            with self.assertRaises(ValueError):
                agent._local_request({"x": "x" * agent.MAX_REQUEST_BYTES}, 5)
        connection.assert_not_called()


class RealVariedPdfGuardTests(unittest.TestCase):
    """Authored mock proposals against actual PDF text, not live AI accuracy."""

    def test_both_varied_layout_sets_accept_exact_supported_values(self):
        from handoff_review.text_pdf import read_text_document
        aliases = {
            "V01": dict(zip(FIELD_KEYS, ("정격용량", "고압측정격전압", "저압측정격전압", "정격주파수", "상수", "결선그룹", "냉각방식", "냉각팬전원", "탭절환기방식", "설치구분"))),
            "V02": dict(zip(FIELD_KEYS, ("정격출력", "1차정격전압", "2차정격전압", "전원주파수", "상수", "결선기호", "냉각방식", "팬보조전원", "탭절환기방식", "설치구분"))),
            "V03": dict(zip(FIELD_KEYS, ("정격용량", "고압측정격전압", "저압측정격전압", "정격주파수", "상수", "결선그룹", "냉각방식", "냉각팬전원", "탭절환기방식", "설치구분"))),
        }
        for case_id in ("V01", "V02", "V03"):
            with self.subTest(case_id=case_id):
                documents, proposals = [], []
                for role, filename in (("customer_spec", "01_customer_spec.pdf"), ("amendment", "02_amendment.pdf"), ("handoff", "03_handoff.pdf")):
                    path = ROOT / "output" / "pdf" / "varied" / case_id / filename
                    self.assertTrue(path.is_file(), "실제 다양한 양식 PDF 검증에는 생성된 가상 문서가 필요합니다.")
                    item = read_text_document(path.read_bytes(), role)
                    documents.append(item)
                    values = {field: (value, unit) for field, _, value, unit in FIELDS}
                    if role == "amendment":
                        values = ({"tap_changer_type": ("OLTC", ""), "installation_type": ("옥외형", "")}
                                  if case_id == "V03" else {"fan_supply_voltage": ("440", "V")})
                    if case_id == "V02" and role == "customer_spec":
                        values["hv_voltage"] = ("22000", "V")
                    if case_id == "V02" and role == "handoff":
                        values.update(rated_power=("1", "MVA"), lv_voltage=("0.4", "kV"), fan_supply_voltage=("440", "V"))
                        del values["frequency"]  # Authored source explicitly says 미기재, not a number.
                    for field, (value, unit) in values.items():
                        source_lines = [(page["page"], line.strip()) for page in item["pages"] for line in page["text"].splitlines()
                                        if "".join(line.split()).startswith(aliases[case_id][field] + ":")]
                        self.assertEqual(len(source_lines), 1)
                        page, quote = source_lines[0]
                        proposals.append({"document_id": item["document"]["id"], "field": field,
                                          "value": value, "unit": unit, "page": page, "quote": quote})
                result, request = run_with(documents, proposals)
                self.assertEqual(result["accepted_fields"], {"V01": 21, "V02": 20, "V03": 22}[case_id], result["warnings"])
                self.assertEqual(result["status"], "partial" if case_id == "V02" else "completed")
                self.assertEqual(request.call_count, 2)
                if case_id == "V02":
                    self.assertEqual(field_of(result, "HANDOFF-V02", "frequency")["value"], ["EXTRACTION_UNCERTAIN"])
                if case_id == "V03":
                    roles = {item["document"]["role"]: item["document"]["id"] for item in result["documents"]}
                    review = review_case({"documents": [item["document"] for item in result["documents"]],
                                          "review_policy": {"base_id": roles["customer_spec"],
                                                            "handoff_id": roles["handoff"],
                                                            "approved_amendment_ids": [roles["amendment"]]}})
                    self.assertEqual(review["summary"]["MATCH"], 8)
                    self.assertEqual(review["summary"]["MISMATCH"], 2)
                    self.assertEqual({row["field"] for row in review["results"] if row["status"] == "MISMATCH"},
                                     {"tap_changer_type", "installation_type"})


if __name__ == "__main__":
    unittest.main()
