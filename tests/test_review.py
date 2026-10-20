"""Day 1 rules regression tests, not LLM accuracy or industrial qualification.

Each Dxx test loads one independent full synthetic document set and compares
all configured outcomes with manually authored fixture expectations. Expected data
is outside the engine so changing it cannot alter the engine's decisions.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from handoff_review import review_case
from handoff_review.contracts import FIELD_KEYS, FIELD_SPECS, UncertainValue, normalize

FIXTURE = json.loads((PROJECT_ROOT / "fixtures" / "cases.json").read_text(encoding="utf-8"))
CASES = {case["case_id"]: case for case in FIXTURE["cases"]}


class SyntheticCaseTests(unittest.TestCase):
    """Individual methods are attached below so every scenario is counted."""


def _scenario_test(case_id: str):
    def test(self):
        case = deepcopy(CASES[case_id])
        result = review_case(case)
        actual = {row["field"]: row["status"] for row in result["results"]}
        self.assertEqual(actual, case["expected"], case["title"])
        self.assertEqual(sum(result["summary"].values()), len(FIELD_KEYS))
        self.assertEqual(tuple(actual), FIELD_KEYS)
        self.assertTrue(all(row["reason"] for row in result["results"]))
    test.__doc__ = CASES[case_id]["title"]
    return test


for _case_id in CASES:
    setattr(SyntheticCaseTests, f"test_{_case_id}", _scenario_test(_case_id))


class ContractTests(unittest.TestCase):
    def test_at_least_twenty_independent_cases(self):
        self.assertGreaterEqual(len(CASES), 20)
        self.assertEqual(len(CASES), len(FIXTURE["cases"]))
        for case in CASES.values():
            self.assertEqual(set(case["expected"]), set(FIELD_KEYS))

    def test_engine_never_uses_manual_expected_answers(self):
        case = deepcopy(CASES["D01"])
        before = review_case(case)
        case["expected"] = {key: "MATCH" for key in FIELD_KEYS}
        self.assertEqual(before, review_case(case))
        del case["expected"]
        self.assertEqual(before, review_case(case))

    def test_engine_does_not_mutate_input(self):
        case = deepcopy(CASES["D11"])
        before = deepcopy(case)
        review_case(case)
        self.assertEqual(case, before)

    def test_demo_mismatch_cites_actual_approved_change(self):
        result = review_case(CASES["D01"])
        row = next(row for row in result["results"] if row["field"] == "fan_supply_voltage")
        self.assertEqual(row["expected"]["value"], "440")
        self.assertEqual(row["actual"]["value"], "380")
        self.assertEqual(row["expected"]["source"]["document_id"], "AMD-001")
        self.assertEqual(row["actual"]["source"]["document_id"], "HANDOFF-001")

    def test_draft_is_never_promoted_to_confirmed_baseline(self):
        result = review_case(CASES["D05"])
        row = next(row for row in result["results"] if row["field"] == "fan_supply_voltage")
        self.assertIsNone(row["expected"])
        self.assertIn("UNAPPROVED_AMENDMENT", [item["code"] for item in result["warnings"]])

    def test_timestamp_changes_cannot_change_precedence(self):
        case = deepcopy(CASES["D01"])
        before = review_case(case)
        for i, document in enumerate(case["documents"]):
            document["updated_at"] = f"{9999-i}-12-31"
        self.assertEqual(before, review_case(case))

    def test_draft_handoff_can_be_reviewed_before_approval(self):
        case = deepcopy(CASES["D02"])
        handoff = next(doc for doc in case["documents"] if doc["role"] == "handoff")
        handoff["status"] = "draft"
        handoff["approved_by"] = None
        self.assertEqual(review_case(case)["summary"]["MATCH"], len(FIELD_KEYS))

    def test_missing_quote_is_not_fabricated(self):
        result = review_case(CASES["D17"])
        row = next(row for row in result["results"] if row["field"] == "frequency")
        self.assertEqual([item["document_id"] for item in row["sources"]], ["HANDOFF-001"])

    def test_large_precision_unit_conversion_is_exact(self):
        spec = next(spec for spec in FIELD_SPECS if spec.key == "lv_voltage")
        result = normalize(spec, {"value": "0.123456789012345678901234567891", "unit": "kV"})
        self.assertEqual(result.comparable, Decimal("123.456789012345678901234567891"))

    def test_nonfinite_and_boolean_numeric_inputs_need_review(self):
        spec = next(spec for spec in FIELD_SPECS if spec.key == "frequency")
        for value in (float("nan"), float("inf"), True, "Infinity", "NaN", "-1", "0"):
            with self.subTest(value=value), self.assertRaises(UncertainValue):
                normalize(spec, {"value": value, "unit": "Hz"})

    def test_json_output_round_trips_without_custom_encoder(self):
        result = review_case(CASES["D21"])
        self.assertEqual(json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False)), result)

    def test_huge_positive_or_negative_exponents_do_not_expand(self):
        spec = next(spec for spec in FIELD_SPECS if spec.key == "frequency")
        for value in ("1e999999999", "1e-999999999", "1e1001", "1e-1001"):
            with self.subTest(value=value), self.assertRaises(UncertainValue):
                normalize(spec, {"value": value, "unit": "Hz"})

    def test_parser_precision_and_text_limits_are_not_engineering_limits(self):
        spec = next(spec for spec in FIELD_SPECS if spec.key == "frequency")
        for value in ("1" * 101, "0." + "0" * 300 + "1"):
            with self.subTest(length=len(value)), self.assertRaises(UncertainValue):
                normalize(spec, {"value": value, "unit": "Hz"})

    def test_malformed_handoff_fields_are_not_classified_as_missing(self):
        for malformed in (None, [], "frequency=60"):
            with self.subTest(fields=malformed):
                case = deepcopy(CASES["D02"])
                handoff = next(doc for doc in case["documents"] if doc["role"] == "handoff")
                handoff["fields"] = malformed
                result = review_case(case)
                self.assertEqual(result["summary"]["NEEDS_REVIEW"], len(FIELD_KEYS))
                self.assertEqual(result["summary"]["MISSING"], 0)
        case = deepcopy(CASES["D02"])
        handoff = next(doc for doc in case["documents"] if doc["role"] == "handoff")
        del handoff["fields"]
        self.assertEqual(review_case(case)["summary"]["NEEDS_REVIEW"], len(FIELD_KEYS))

    def test_malformed_base_fields_need_review_even_if_changes_have_values(self):
        case = deepcopy(CASES["D02"])
        case["documents"][0]["fields"] = []
        self.assertEqual(review_case(case)["summary"]["NEEDS_REVIEW"], len(FIELD_KEYS))

    def test_limitations_make_missing_ai_and_original_validation_explicit(self):
        limitations = " ".join(review_case(CASES["D02"])["limitations"])
        self.assertIn("AI 성능 평가가 아닙니다", limitations)
        self.assertIn("실제 PDF 원문 일치는 검증하지 않습니다", limitations)
        self.assertIn("실제 승인", limitations)
        self.assertIn("한 프로젝트·한 제품", limitations)


if __name__ == "__main__":
    unittest.main(verbosity=2)
