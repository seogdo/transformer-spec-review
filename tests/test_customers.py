"""Offline tests for explicit customer aliases and document-mixing holds."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from handoff_review.contracts import FIELD_KEYS
from handoff_review.customers import DISCLAIMER, check_customers, resolve_customer
from handoff_review.review import review_case


def document(identifier, customer, role="customer_spec"):
    return {
        "id": identifier,
        "role": role,
        "customer_name": customer,
        "customer_sources": [{"page": 2, "quote": f"고객사: {customer}"}],
    }


def valid_case():
    values = {
        "rated_power": ("1000", "kVA"),
        "hv_voltage": ("22", "kV"),
        "lv_voltage": ("400", "V"),
        "frequency": ("60", "Hz"),
        "phases": ("3", "count"),
        "vector_group": ("Dyn11", ""),
        "cooling_type": ("ONAF", ""),
        "fan_supply_voltage": ("380", "V"),
        "tap_changer_type": ("DETC", ""),
        "installation_type": ("옥내형", ""),
    }
    fields = {key: {"value": value, "unit": unit, "page": 1, "quote": f"{key}: {value} {unit}"}
              for key, (value, unit) in values.items()}
    documents = []
    for identifier, customer, role in (("SPEC", "한전", "customer_spec"), ("HANDOFF", "KEPCO", "handoff")):
        documents.append({
            **document(identifier, customer, role),
            "title": identifier,
            "project_id": "SYNTHETIC-PROJECT",
            "status": "approved",
            "approved_by": "synthetic-person",
            "fields": deepcopy(fields),
        })
    return {
        "case_id": "customer-test",
        "documents": documents,
        "review_policy": {"base_id": "SPEC", "handoff_id": "HANDOFF", "approved_amendment_ids": []},
    }


class ResolveCustomerTests(unittest.TestCase):
    def test_all_explicit_aliases_resolve_to_six_candidates(self):
        cases = {
            "aep": ("AEP", "미국 AEP"),
            "kepco": ("한국전력", "한국전력공사", "한전", "KEPCO"),
            "samsung_electronics": ("삼성전자", "삼전", "Samsung Electronics"),
            "sk_hynix": ("SK하이닉스", "하이닉스", "SK hynix"),
            "national_grid": ("National Grid", "NG", "영국 NG"),
            "transgrid": ("Transgrid", "호주 Transgrid", "트랜스그리드"),
        }
        for identifier, aliases in cases.items():
            for alias in aliases:
                with self.subTest(alias=alias):
                    result = resolve_customer(alias)
                    self.assertEqual(result["status"], "recognized")
                    self.assertEqual(result["canonical_id"], identifier)
                    self.assertIsInstance(result["canonical_name"], str)
                    self.assertEqual(result["raw_name"], alias)
                    self.assertIn("실제 거래·법인 확인은 아닙니다", result["message"])

    def test_only_case_and_whitespace_collapse_are_normalized(self):
        for raw, identifier in (
            ("  sAmSuNg \t\n ELECTRONICS  ", "samsung_electronics"),
            ("sk   HYNIX", "sk_hynix"),
            ("NATIONAL  GRID", "national_grid"),
            (" 미국\t aep ", "aep"),
        ):
            with self.subTest(raw=raw):
                result = resolve_customer(raw)
                self.assertEqual(result["canonical_id"], identifier)
                self.assertEqual(result["raw_name"], raw)

    def test_substrings_suffixes_fuzzy_matches_and_other_companies_stay_unknown(self):
        for raw in (
            "NGET", "National Grid Electricity Transmission", "NG plc",
            "Powergrid", "POWER GRID", "Transgrid Pty Ltd", "호주 Powergrid",
            "한국전력 자회사", "(주)삼성전자", "삼성전자 주식회사", "삼성",
            "SK 하이닉스", "NationalGrid", "KEPC", "AEP Korea",
            "https://example.com/AEP", "새로운 고객사", "AEP/KEPCO",
        ):
            with self.subTest(raw=raw):
                result = resolve_customer(raw)
                self.assertEqual(result["status"], "unknown")
                self.assertIsNone(result["canonical_id"])
                self.assertIsNone(result["canonical_name"])
                self.assertEqual(result["raw_name"], raw)

    def test_none_and_blank_names_are_missing(self):
        for raw in (None, "", " \t\n "):
            with self.subTest(raw=raw):
                self.assertEqual(resolve_customer(raw)["status"], "missing")

    def test_lists_and_invalid_types_never_select_a_candidate(self):
        for raw in ([], ["한전"], ["한전", "KEPCO"], ["AEP", "한전"], 0, True, {"name": "한전"}, ("AEP",)):
            with self.subTest(raw=raw):
                result = resolve_customer(raw)
                self.assertEqual(result["status"], "ambiguous")
                self.assertIsNone(result["canonical_id"])

    def test_ambiguous_original_is_deep_copied(self):
        original = ["한전", {"bad": ["KEPCO"]}]
        result = resolve_customer(original)
        result["raw_name"][1]["bad"].append("changed")
        self.assertEqual(original, ["한전", {"bad": ["KEPCO"]}])


class CheckCustomersTests(unittest.TestCase):
    def test_same_customer_aliases_permit_comparison(self):
        documents = [document("SPEC", "한전"), document("AMD", "한국전력공사", "amendment"),
                     document("HANDOFF", "KEPCO", "handoff")]
        result = check_customers(documents)
        self.assertEqual(result["version"], "customer-identity-v1")
        self.assertEqual(result["status"], "MATCH")
        self.assertTrue(result["can_compare"])
        self.assertEqual(len(result["documents"]), 3)
        self.assertEqual({row["canonical_id"] for row in result["documents"]}, {"kepco"})
        self.assertIn(DISCLAIMER, result["message"])

    def test_different_recognized_customers_are_a_mismatch(self):
        result = check_customers([document("SPEC", "AEP"), document("HANDOFF", "한전", "handoff")])
        self.assertEqual(result["status"], "MISMATCH")
        self.assertFalse(result["can_compare"])
        self.assertIn("문서 혼입", result["message"])

    def test_uncertainty_takes_precedence_over_known_customer_mismatch(self):
        for raw in (None, "신규 고객사", ["NG", "NGET"]):
            with self.subTest(raw=raw):
                result = check_customers([document("SPEC", "AEP"), document("HANDOFF", "한전", "handoff"),
                                          document("AMD", raw, "amendment")])
                self.assertEqual(result["status"], "NEEDS_REVIEW")
                self.assertFalse(result["can_compare"])

    def test_absent_customer_field_is_not_inferred_from_project_or_role(self):
        docs = [{"id": "SPEC-KEPCO", "role": "customer_spec", "project_id": "KEPCO-PROJECT"},
                document("HANDOFF", "한전", "handoff")]
        result = check_customers(docs)
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertEqual(result["documents"][0]["status"], "missing")

    def test_all_attachments_participate_regardless_of_selection_or_approval(self):
        extra = {**document("LOOSE", "삼전", "amendment"),
                 "status": "draft", "approved_by": None, "project_id": "OTHER"}
        result = check_customers([document("SPEC", "한전"), document("HANDOFF", "KEPCO", "handoff"), extra])
        self.assertEqual(result["status"], "MISMATCH")
        self.assertEqual(result["documents"][-1]["canonical_id"], "samsung_electronics")

    def test_source_rows_and_original_names_are_copied_without_alteration(self):
        docs = [document("SPEC", " \tKEPCO ")]
        docs[0]["customer_sources"].append({"page": 4, "quote": "고객사 표기 원문\nKEPCO"})
        before = deepcopy(docs)
        result = check_customers(docs)
        self.assertEqual(result["documents"][0]["sources"], before[0]["customer_sources"])
        self.assertEqual(result["documents"][0]["raw_name"], " \tKEPCO ")
        result["documents"][0]["sources"][0]["quote"] = "changed"
        self.assertEqual(docs, before)

    def test_sources_are_optional_and_are_not_external_identity_verification(self):
        result = check_customers([{"id": "SPEC", "customer_name": "한전"}])
        self.assertEqual(result["status"], "MATCH")
        self.assertEqual(result["documents"][0]["sources"], [])
        self.assertIn("법인 동일성", result["message"])

    def test_empty_or_invalid_document_collections_fail_closed(self):
        for docs in (None, [], {}, "SPEC", [None], [document("SPEC", "한전"), "invalid"]):
            with self.subTest(docs=docs):
                result = check_customers(docs)
                self.assertEqual(result["status"], "NEEDS_REVIEW")
                self.assertFalse(result["can_compare"])


class CustomerReviewGateTests(unittest.TestCase):
    def assertAllHeld(self, result):
        self.assertEqual(len(result["results"]), 10)
        self.assertEqual(result["summary"]["NEEDS_REVIEW"], 10)
        self.assertTrue(all(row["status"] == "NEEDS_REVIEW" for row in result["results"]))
        self.assertTrue(all("고객사 확인" in row["reason"] for row in result["results"]))
        self.assertEqual({row["field"] for row in result["results"]}, set(FIELD_KEYS))

    def test_customer_check_is_returned_without_becoming_an_eleventh_spec(self):
        case = valid_case()
        before = deepcopy(case)
        result = review_case(case)
        self.assertEqual(result["customer_check"]["status"], "MATCH")
        self.assertEqual(result["summary"]["MATCH"], 10)
        self.assertEqual(len(result["results"]), 10)
        self.assertEqual(case, before)
        self.assertTrue(any("고객사 확인" in limitation for limitation in result["limitations"]))

    def test_missing_unknown_ambiguous_and_mixed_customers_hold_all_specs(self):
        for raw, expected in ((None, "NEEDS_REVIEW"), ("NGET", "NEEDS_REVIEW"),
                              (["한전", "KEPCO"], "NEEDS_REVIEW"), ("AEP", "MISMATCH")):
            with self.subTest(raw=raw):
                case = valid_case()
                case["documents"][-1]["customer_name"] = raw
                result = review_case(case)
                self.assertEqual(result["customer_check"]["status"], expected)
                self.assertAllHeld(result)
                self.assertTrue(any(warning["code"] == "INVALID_REVIEW_CONTEXT" and "고객사 확인" in warning["message"]
                                    for warning in result["warnings"]))

    def test_missing_customer_metadata_in_legacy_documents_is_not_assumed_same(self):
        case = valid_case()
        for doc in case["documents"]:
            del doc["customer_name"]
        self.assertAllHeld(review_case(case))

    def test_unselected_other_project_amendment_still_blocks_customer_mix(self):
        case = valid_case()
        case["documents"].append({
            **document("LOOSE", "NG", "amendment"),
            "title": "loose attachment", "project_id": "OTHER", "status": "draft",
            "approved_by": None, "fields": {"unsupported_field": {"value": "x"}},
        })
        result = review_case(case)
        self.assertEqual(result["customer_check"]["status"], "MISMATCH")
        self.assertAllHeld(result)

    def test_all_technical_differences_are_held_when_customer_identity_is_unknown(self):
        case = valid_case()
        case["documents"][-1]["customer_name"] = "새 고객"
        case["documents"][-1]["fields"]["frequency"]["value"] = "50"
        del case["documents"][-1]["fields"]["rated_power"]
        result = review_case(case)
        self.assertAllHeld(result)
        self.assertEqual(result["summary"]["MISMATCH"], 0)
        self.assertEqual(result["summary"]["MISSING"], 0)

    def test_customer_match_does_not_override_existing_project_guard(self):
        case = valid_case()
        case["documents"][-1]["project_id"] = "OTHER"
        result = review_case(case)
        self.assertTrue(result["customer_check"]["can_compare"])
        self.assertEqual(result["summary"]["NEEDS_REVIEW"], 10)
        self.assertTrue(all("프로젝트" in row["reason"] for row in result["results"]))


if __name__ == "__main__":
    unittest.main()
