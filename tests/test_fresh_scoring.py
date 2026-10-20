"""Offline scorer tests with invented records, not the fresh evaluation answers.

These tests never import the extractor, read evaluation PDF/answer artifacts,
start a server, or call AI. They test scoring policy rather than model quality.
"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from score_fresh_evaluation import FIELD_KEYS, NUMERIC_UNITS, score_case, score_run


def reference(case_id="SCORER-MOCK", customer_status="MATCH"):
    fields = {}
    for field in FIELD_KEYS:
        value = {"value": "10" if field in NUMERIC_UNITS else "ENUM",
                 "unit": NUMERIC_UNITS.get(field, "")}
        fields[field] = {
            "status": "MATCH", "expected": deepcopy(value), "actual": deepcopy(value),
            "rationale": "Scorer test only.",
            "evidence": [{"document_id": "MOCK-SPEC", "page": 1, "quote": "mock source"}],
        }
    case = {"case_id": case_id, "customer_status": customer_status, "fields": fields}
    if customer_status != "MATCH":
        for row in fields.values():
            row.update(status="NEEDS_REVIEW", expected=None, actual=None)
    return case


def review_for(case):
    return {
        "customer_check": {"status": case["customer_status"], "can_compare": case["customer_status"] == "MATCH"},
        "results": [{"field": field, "status": item["status"],
                     "expected": deepcopy(item["expected"]), "actual": deepcopy(item["actual"]),
                     "sources": [{"document_id": "MOCK-SPEC", "page": 1, "quote": "longer mock source excerpt"}]}
                    for field, item in case["fields"].items()],
    }


def observation(case, review=None, audit=None):
    return {"case_id": case["case_id"], "review": review_for(case) if review is None else review,
            "provenance_audit": {"status": "passed"} if audit is None else audit}


def set_status(case, field, status):
    row = case["fields"][field]
    row["status"] = status
    if status == "MISMATCH":
        row["actual"]["value"] = "20" if field in NUMERIC_UNITS else "OTHER"
    elif status == "NEEDS_REVIEW":
        row.update(expected=None, actual=None)
    elif status == "MISSING":
        row["actual"] = None


class FreshScoringTests(unittest.TestCase):
    def test_complete_case_and_run_pass_without_mutation(self):
        case = reference()
        run = [observation(case)]
        before = deepcopy((case, run))
        result = score_run([case], run)
        self.assertTrue(result["passed"])
        self.assertEqual(result["counts"]["fully_correct_fields"], 10)
        self.assertEqual(result["evidence_alignment"]["aligned_count"], 10)
        self.assertEqual((case, run), before)

    def test_status_only_match_does_not_hide_wrong_number(self):
        case = reference()
        review = review_for(case)
        review["results"][0]["actual"]["value"] = "999"
        result = score_case(case, review)
        self.assertEqual(result["counts"]["status_correct_fields"], 10)
        self.assertEqual(result["counts"]["value_mismatch_fields"], 1)
        self.assertFalse(result["status_value_passed"])

    def test_wrong_unit_is_not_renormalized(self):
        case = reference()
        review = review_for(case)
        review["results"][0]["actual"] = {"value": "0.01", "unit": "MVA"}
        self.assertFalse(score_case(case, review)["status_value_passed"])

    def test_finite_numeric_format_equivalence(self):
        case = reference()
        review = review_for(case)
        review["results"][0]["actual"]["value"] = "10.000"
        review["results"][1]["actual"]["value"] = 10.0
        self.assertTrue(score_case(case, review)["status_value_passed"])

    def test_nonfinite_and_boolean_numbers_rejected(self):
        for value in ("NaN", "Infinity", "-Infinity", True, ["10"]):
            with self.subTest(value=value):
                case = reference()
                review = review_for(case)
                review["results"][0]["actual"]["value"] = value
                self.assertFalse(score_case(case, review)["status_value_passed"])

    def test_enum_alias_is_not_silently_normalized(self):
        case = reference()
        review = review_for(case)
        review["results"][-1]["actual"]["value"] = "enum"
        self.assertFalse(score_case(case, review)["status_value_passed"])

    def test_status_error_definitions_and_confusion(self):
        case = reference()
        for field, status in zip(FIELD_KEYS[:6],
                                 ("MISMATCH", "MISMATCH", "MISMATCH", "MATCH", "MATCH", "NEEDS_REVIEW")):
            set_status(case, field, status)
        review = review_for(case)
        for row, status in zip(review["results"], ("MATCH", "NEEDS_REVIEW", "MISSING", "MISMATCH", "NEEDS_REVIEW", "MATCH")):
            row["status"] = status
        result = score_case(case, review)
        errors = result["error_counts"]
        self.assertEqual(errors["false_clear"], 2)
        self.assertEqual(errors["false_alert"], 1)
        self.assertEqual(errors["conservative_hold"], 2)
        self.assertEqual(errors["missed_clear_difference"], 3)
        self.assertEqual(errors["missed_clear_difference_hold"], 1)
        self.assertEqual(errors["missed_clear_difference_false_clear"], 1)
        self.assertEqual(errors["missed_clear_difference_other"], 1)
        self.assertEqual(errors["unsafe_resolution_to_definite"], 1)
        self.assertEqual(result["status_confusion"]["MISMATCH"]["MATCH"], 1)

    def test_hold_values_are_unscored_including_partial_expected(self):
        case = reference()
        set_status(case, FIELD_KEYS[0], "NEEDS_REVIEW")
        review = review_for(case)
        review["results"][0]["expected"] = {"value": "17", "unit": "kVA"}
        result = score_case(case, review)
        self.assertTrue(result["status_value_passed"])
        self.assertEqual(result["counts"]["value_checked_fields"], 9)
        self.assertIsNone(result["fields"][0]["values_correct"])

    def test_missing_is_explicit_null_not_omitted_key(self):
        case = reference()
        set_status(case, FIELD_KEYS[0], "MISSING")
        review = review_for(case)
        self.assertTrue(score_case(case, review)["status_value_passed"])
        del review["results"][0]["actual"]
        self.assertFalse(score_case(case, review)["status_value_passed"])

    def test_missing_row_never_receives_pass(self):
        case = reference()
        review = review_for(case)
        review["results"].pop()
        result = score_case(case, review)
        self.assertFalse(result["status_value_passed"])
        self.assertEqual(result["counts"]["status_correct_fields"], 9)
        self.assertEqual(result["status_confusion"]["MATCH"]["MISSING_ROW"], 1)

    def test_duplicate_row_not_last_wins(self):
        case = reference()
        review = review_for(case)
        review["results"].append(deepcopy(review["results"][0]))
        result = score_case(case, review)
        self.assertFalse(result["field_set"]["exact"])
        self.assertEqual(result["status_confusion"]["MATCH"]["DUPLICATE_ROW"], 1)

    def test_extra_unknown_and_malformed_rows_fail_case(self):
        for extra in ({"field": "foreign", "status": "MATCH"}, None, {"status": "MATCH"}):
            with self.subTest(extra=extra):
                case = reference()
                review = review_for(case)
                review["results"].append(extra)
                result = score_case(case, review)
                self.assertFalse(result["status_value_passed"])
                self.assertEqual(result["counts"]["status_correct_fields"], 10)

    def test_invalid_status_and_missing_review_are_explicit(self):
        case = reference()
        review = review_for(case)
        review["results"][0]["status"] = "OK"
        result = score_case(case, review)
        self.assertEqual(result["status_confusion"]["MATCH"]["INVALID_STATUS"], 1)
        result = score_case(case, None)
        self.assertEqual(result["status_confusion"]["MATCH"]["REVIEW_UNAVAILABLE"], 10)
        self.assertEqual(result["counts"]["value_correct_fields"], 0)
        self.assertFalse(result["status_value_passed"])

    def test_disabled_customer_requires_exact_all_hold_rows(self):
        for status in ("MISMATCH", "NEEDS_REVIEW"):
            with self.subTest(status=status):
                case = reference(customer_status=status)
                review = review_for(case)
                self.assertTrue(score_case(case, review)["customer"]["hold_enforced"])
                review["results"][0]["status"] = "MATCH"
                result = score_case(case, review)
                self.assertFalse(result["customer"]["hold_enforced"])
                self.assertFalse(result["status_value_passed"])
                self.assertEqual(result["error_counts"]["false_clear"], 1)

    def test_customer_status_or_boolean_disagreement_fails(self):
        case = reference()
        for identity in ({}, {"status": "MATCH", "can_compare": 1},
                         {"status": "MISMATCH", "can_compare": True}):
            with self.subTest(identity=identity):
                review = review_for(case)
                review["customer_check"] = identity
                self.assertFalse(score_case(case, review)["customer"]["correct"])

    def test_evidence_quote_document_and_page_are_separate_diagnostic(self):
        case = reference()
        review = review_for(case)
        review["results"][0]["sources"][0]["document_id"] = "OTHER"
        review["results"][1]["sources"][0]["page"] = 2
        review["results"][2]["sources"][0]["quote"] = "different valid wording"
        result = score_run([case], [observation(case, review)])
        self.assertTrue(result["passed"])
        self.assertEqual(result["evidence_alignment"]["aligned_count"], 7)
        self.assertFalse(result["evidence_alignment"]["affects_verdict_score"])

    def test_evidence_alignment_does_not_ignore_whitespace(self):
        case = reference()
        review = review_for(case)
        review["results"][0]["sources"][0]["quote"] = "mock  source"
        item = score_case(case, review)["fields"][0]["evidence_alignment"]
        self.assertEqual(item["status"], "not_aligned")

    def test_missing_provenance_is_not_a_pass(self):
        case = reference()
        result = score_run([case], [{"case_id": case["case_id"], "review": review_for(case)}])
        self.assertTrue(result["cases"][0]["status_value_passed"])
        self.assertFalse(result["passed"])
        self.assertEqual(result["provenance_counts"]["not_checked"], 1)

    def test_failed_source_check_overrides_success_declaration(self):
        case = reference()
        audit = {"passed": True, "checks": [{"field": FIELD_KEYS[0], "passed": False, "reason": "quote absent"}]}
        result = score_run([case], [observation(case, audit=audit)])
        self.assertFalse(result["passed"])
        self.assertEqual(result["provenance_counts"]["failed"], 1)

    def test_missing_duplicate_unexpected_cases_cannot_pass(self):
        case = reference()
        for runs, key in (([], "missing_cases"),
                          ([observation(case), observation(case)], "duplicate_cases"),
                          ([observation(case), observation(reference("EXTRA"))], "unexpected_cases")):
            with self.subTest(key=key):
                result = score_run([case], runs)
                self.assertFalse(result["passed"])
                self.assertTrue(result["case_set"][key])
                if key != "unexpected_cases":
                    self.assertEqual(result["counts"]["status_correct_fields"], 0)

    def test_malformed_observation_is_not_ignored(self):
        case = reference()
        result = score_run([case], [observation(case), None])
        self.assertFalse(result["passed"])
        self.assertEqual(result["case_set"]["malformed_observation_indexes"], [1])

    def test_reference_validation_rejects_wrong_field_set(self):
        case = reference()
        del case["fields"][FIELD_KEYS[0]]
        with self.assertRaises(ValueError):
            score_case(case, {})

    def test_reference_validation_rejects_contradictory_values(self):
        case = reference()
        case["fields"][FIELD_KEYS[0]]["status"] = "MISMATCH"
        with self.assertRaises(ValueError):
            score_case(case, {})

    def test_reference_validation_rejects_noncanonical_units_and_hold_values(self):
        case = reference()
        case["fields"][FIELD_KEYS[0]]["expected"]["unit"] = "MVA"
        with self.assertRaises(ValueError):
            score_case(case, {})
        case = reference()
        case["fields"][FIELD_KEYS[0]]["status"] = "NEEDS_REVIEW"
        with self.assertRaises(ValueError):
            score_case(case, {})

    def test_reference_validation_rejects_customer_hold_with_definite_fields(self):
        case = reference()
        case["customer_status"] = "MISMATCH"
        with self.assertRaises(ValueError):
            score_case(case, {})

    def test_reference_validation_rejects_bad_evidence_and_duplicate_case_ids(self):
        case = reference()
        case["fields"][FIELD_KEYS[0]]["evidence"][0]["page"] = True
        with self.assertRaises(ValueError):
            score_case(case, {})
        with self.assertRaises(ValueError):
            score_run([reference(), reference()], [])

    def test_reference_validation_rejects_empty_run(self):
        with self.assertRaises(ValueError):
            score_run([], [])

    def test_totals_preserve_denominator_in_failed_run(self):
        cases = [reference("SCORER-1"), reference("SCORER-2")]
        result = score_run(cases, [observation(cases[0])])
        self.assertEqual(result["counts"]["expected_fields"], 20)
        self.assertEqual(result["counts"]["status_correct_fields"], 10)
        self.assertEqual(result["case_set"]["expected_cases"], 2)
        self.assertEqual(result["passed_cases"], 1)


if __name__ == "__main__":
    unittest.main()
