"""Ten-field contract regression: supported aliases, scope and amendments.

These are synthetic rule tests, not evidence of real transformer suitability.
The original fixture answers are never used to compute the modified outcomes.
PDF-to-value extraction is covered separately in the ingestion/agent tests.
"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from handoff_review import review_case
from handoff_review.contracts import FIELD_KEYS, FIELD_SPECS, UncertainValue, normalize
from handoff_review.explanation import _facts, _schema, explain_review

BASE_CASE = json.loads((ROOT / "fixtures" / "cases.json").read_text(encoding="utf-8"))["cases"][1]
SPECS = {spec.key: spec for spec in FIELD_SPECS}
NEW_FIELDS = ("tap_changer_type", "installation_type")


def entry(key, value, **extra):
    return {"value": value, "unit": "", "page": 1,
            "quote": f"가상 요구사항 {key}: {value}", **extra}


def case_with_change(key, new_value):
    case = deepcopy(BASE_CASE)
    case["case_id"] = "EXTENSION-REGRESSION"
    case.pop("expected")
    amendment = next(doc for doc in case["documents"] if doc["role"] == "amendment")
    amendment["fields"][key] = entry(key, new_value)
    return case


def row_for(case, key):
    return next(row for row in review_case(case)["results"] if row["field"] == key)


class AddedFieldTests(unittest.TestCase):
    def test_exactly_two_requested_fields_and_no_material(self):
        self.assertEqual(len(FIELD_KEYS), 10)
        self.assertEqual(FIELD_KEYS[-2:], NEW_FIELDS)
        self.assertFalse(any("material" in key for key in FIELD_KEYS))

    def test_tap_supported_aliases_only(self):
        for raw, expected in (("OLTC", "OLTC"), ("oltc", "OLTC"), ("부하시", "OLTC"),
                              ("부하시 탭절환기", "OLTC"), ("DETC", "DETC"),
                              ("무전압", "DETC"), ("무전압  탭절환기", "DETC")):
            with self.subTest(raw=raw):
                self.assertEqual(normalize(SPECS["tap_changer_type"],
                                           entry("tap_changer_type", raw)).value, expected)

    def test_installation_supported_aliases_only(self):
        for raw, expected in (("옥내형", "옥내형"), ("옥내", "옥내형"), ("실내", "옥내형"),
                              ("Indoor", "옥내형"), ("옥외형", "옥외형"), ("옥외", "옥외형"),
                              ("실외", "옥외형"), ("OUTDOOR", "옥외형")):
            with self.subTest(raw=raw):
                self.assertEqual(normalize(SPECS["installation_type"],
                                           entry("installation_type", raw)).value, expected)

    def test_complex_or_unscoped_values_are_not_coerced(self):
        for key, values in {
            "tap_changer_type": ("무부하", "OLTC/DETC", "OLTC 미사용", "OLTC 또는 DETC", ["OLTC"], 1),
            "installation_type": ("옥내/옥외", "옥내 설치 시", "옥외형 부속함", "옥내형 아님", ["옥내형"], 1),
        }.items():
            for raw in values:
                with self.subTest(key=key, raw=raw), self.assertRaises(UncertainValue):
                    normalize(SPECS[key], entry(key, raw))

    def test_enum_with_numeric_unit_is_rejected(self):
        for key, value in zip(NEW_FIELDS, ("OLTC", "옥내형")):
            with self.subTest(key=key), self.assertRaises(UncertainValue):
                normalize(SPECS[key], entry(key, value, unit="V"))

    def test_aliases_do_not_mutate_original_evidence(self):
        item = entry("installation_type", "INDOOR")
        original = deepcopy(item)
        self.assertEqual(normalize(SPECS["installation_type"], item).value, "옥내형")
        self.assertEqual(item, original)

    def test_tap_amendment_not_carried_into_handoff_is_mismatch(self):
        row = row_for(case_with_change("tap_changer_type", "OLTC"), "tap_changer_type")
        self.assertEqual(row["status"], "MISMATCH")
        self.assertEqual(row["expected"]["value"], "OLTC")
        self.assertEqual(row["actual"]["value"], "DETC")
        self.assertEqual(row["expected"]["source"]["document_id"], "AMD-001")

    def test_outdoor_amendment_not_carried_into_handoff_is_mismatch(self):
        row = row_for(case_with_change("installation_type", "옥외형"), "installation_type")
        self.assertEqual(row["status"], "MISMATCH")
        self.assertEqual(row["expected"]["value"], "옥외형")
        self.assertEqual(row["actual"]["value"], "옥내형")

    def test_amendments_reflected_using_supported_aliases_match(self):
        for key, changed, alias in (("tap_changer_type", "OLTC", "부하시 탭절환기"),
                                     ("installation_type", "옥외형", "OUTDOOR")):
            with self.subTest(key=key):
                case = case_with_change(key, changed)
                handoff = next(doc for doc in case["documents"] if doc["role"] == "handoff")
                handoff["fields"][key] = entry(key, alias)
                self.assertEqual(row_for(case, key)["status"], "MATCH")

    def test_unapproved_changes_remain_needs_review(self):
        for key, changed in (("tap_changer_type", "OLTC"), ("installation_type", "옥외형")):
            with self.subTest(key=key):
                case = case_with_change(key, changed)
                next(doc for doc in case["documents"] if doc["role"] == "amendment")["status"] = "draft"
                self.assertEqual(row_for(case, key)["status"], "NEEDS_REVIEW")

    def test_confirmed_requirement_but_missing_handoff_is_missing(self):
        for key in NEW_FIELDS:
            with self.subTest(key=key):
                case = deepcopy(BASE_CASE)
                del next(doc for doc in case["documents"] if doc["role"] == "handoff")["fields"][key]
                self.assertEqual(row_for(case, key)["status"], "MISSING")

    def test_old_eight_field_input_does_not_invent_new_values(self):
        case = deepcopy(BASE_CASE)
        for doc in case["documents"]:
            for key in NEW_FIELDS:
                doc["fields"].pop(key, None)
        result = review_case(case)
        self.assertEqual(result["summary"], {"MATCH": 8, "MISMATCH": 0, "MISSING": 0, "NEEDS_REVIEW": 2})

    def test_component_condition_is_not_ignored(self):
        case = deepcopy(BASE_CASE)
        handoff = next(doc for doc in case["documents"] if doc["role"] == "handoff")
        handoff["fields"]["installation_type"]["condition"] = {"component": "부속함"}
        self.assertEqual(row_for(case, "installation_type")["status"], "NEEDS_REVIEW")

    def test_supported_enum_does_not_bypass_evidence_requirement(self):
        case = deepcopy(BASE_CASE)
        handoff = next(doc for doc in case["documents"] if doc["role"] == "handoff")
        handoff["fields"]["tap_changer_type"]["quote"] = ""
        self.assertEqual(row_for(case, "tap_changer_type")["status"], "NEEDS_REVIEW")

    def test_explanation_schema_and_facts_cover_ten_fields(self):
        result = review_case(BASE_CASE)
        facts, _ = _facts(result)
        self.assertEqual(tuple(item["field"] for item in facts), FIELD_KEYS)
        self.assertEqual(_schema()["properties"]["items"]["minItems"], 10)
        self.assertEqual(_schema()["properties"]["items"]["maxItems"], 10)


if __name__ == "__main__":
    unittest.main()
