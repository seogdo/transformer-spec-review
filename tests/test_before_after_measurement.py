"""Fixed-response evidence measurement tests; never a model-quality benchmark."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import measure_evidence_before_after as measurement


def invented_pair():
    before = {
        "summary": {"MATCH": 9, "MISMATCH": 0, "MISSING": 0, "NEEDS_REVIEW": 1},
        "policy": deepcopy(measurement.POLICY),
        "results": [
            {"field": field, "status": "MATCH",
             "expected": {"value": "1", "unit": ""},
             "actual": {"value": "1", "unit": ""}, "sources": []}
            for field in measurement.FIELD_KEYS
        ],
    }
    fan = before["results"][measurement.FIELD_KEYS.index(measurement.FAN)]
    fan.update(
        status="NEEDS_REVIEW", expected={"value": "400", "unit": "V"},
        actual=None, evidence_complete=True,
        sources=[{"document_id": "HANDOFF-F04", "page": 1,
                  "quote": measurement.CONFLICT_QUOTES[0]}],
    )
    after = deepcopy(before)
    fan_after = after["results"][measurement.FIELD_KEYS.index(measurement.FAN)]
    fan_after["sources"].append({
        "document_id": "HANDOFF-F04", "page": 1,
        "quote": measurement.CONFLICT_QUOTES[1],
    })
    fan_after.update(evidence_complete=False, evidence_check={"status": "review_required"})
    return before, after


class MeasurementUnitTests(unittest.TestCase):
    def test_expected_limited_change_and_exact_ten_field_preservation(self):
        before, after = invented_pair()
        untouched = deepcopy((before, after))
        result = measurement.compare_reviews(before, after)
        self.assertTrue(result["passed"])
        self.assertEqual(result["status_unchanged_count"], 10)
        self.assertEqual(result["normalized_value_pairs_unchanged_count"], 10)
        self.assertEqual(result["fan_before"]["conflict_quote_kind_count"], 1)
        self.assertEqual(result["fan_after"]["conflict_quote_kind_count"], 2)
        self.assertEqual((before, after), untouched)

    def test_duplicate_source_does_not_inflate_measurement(self):
        before, after = invented_pair()
        fan = after["results"][measurement.FIELD_KEYS.index(measurement.FAN)]
        fan["sources"] += deepcopy(fan["sources"])
        result = measurement.compare_reviews(before, after)
        self.assertTrue(result["passed"])
        self.assertEqual(result["fan_after"]["handoff_unique_source_count"], 2)

    def test_spec_quote_cannot_be_counted_as_handoff_conflict(self):
        before, after = invented_pair()
        after["results"][measurement.FIELD_KEYS.index(measurement.FAN)]["sources"][1]["document_id"] = "SPEC-F04"
        result = measurement.compare_reviews(before, after)
        self.assertFalse(result["passed"])
        self.assertEqual(result["fan_after"]["conflict_quote_kind_count"], 1)

    def test_wrong_page_is_not_an_equivalent_source(self):
        before, after = invented_pair()
        after["results"][measurement.FIELD_KEYS.index(measurement.FAN)]["sources"][1]["page"] = 2
        result = measurement.compare_reviews(before, after)
        self.assertFalse(result["checks"]["handoff_distinct_sources_one_to_two"])

    def test_boolean_page_is_rejected(self):
        before, after = invented_pair()
        after["results"][measurement.FIELD_KEYS.index(measurement.FAN)]["sources"][0]["page"] = True
        with self.assertRaises(measurement.MeasurementError):
            measurement.compare_reviews(before, after)

    def test_status_value_or_unit_change_is_not_hidden(self):
        for key, value in (("status", "MISMATCH"), ("value", "2"), ("unit", "kV")):
            with self.subTest(key=key):
                before, after = invented_pair()
                if key == "status":
                    after["results"][0][key] = value
                else:
                    after["results"][0]["actual"][key] = value
                self.assertFalse(measurement.compare_reviews(before, after)["passed"])

    def test_false_flag_and_review_required_are_both_required(self):
        for key, value in (("evidence_complete", True), ("evidence_check", {"status": "verified"})):
            with self.subTest(key=key):
                before, after = invented_pair()
                after["results"][measurement.FIELD_KEYS.index(measurement.FAN)][key] = value
                self.assertFalse(measurement.compare_reviews(before, after)["passed"])

    def test_missing_duplicate_or_extra_field_is_rejected(self):
        for change in ("missing", "duplicate", "extra"):
            with self.subTest(change=change):
                before, after = invented_pair()
                if change == "missing":
                    after["results"].pop()
                elif change == "duplicate":
                    after["results"][-1]["field"] = measurement.FIELD_KEYS[0]
                else:
                    after["results"].append(deepcopy(after["results"][0]))
                with self.assertRaises(measurement.MeasurementError):
                    measurement.compare_reviews(before, after)

    def test_missing_value_key_is_not_equivalent_to_null(self):
        before, after = invented_pair()
        del after["results"][measurement.FIELD_KEYS.index(measurement.FAN)]["actual"]
        with self.assertRaises(measurement.MeasurementError):
            measurement.compare_reviews(before, after)

    def test_malformed_status_is_rejected(self):
        before, after = invented_pair()
        after["results"][0]["status"] = []
        with self.assertRaises(measurement.MeasurementError):
            measurement.compare_reviews(before, after)

    def test_output_writer_refuses_to_overwrite(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "evidence-before-after-test.json"
            measurement.write_report_exclusive(path, {"first": True})
            original = path.read_bytes()
            with self.assertRaises(FileExistsError):
                measurement.write_report_exclusive(path, {"replacement": True})
            self.assertEqual(path.read_bytes(), original)

    def test_pdf_hash_mismatch_is_rejected_before_replay(self):
        fixture = measurement.read_fixture()
        historical = {
            "cases": [{
                "case_id": "F04", "review": fixture["review"],
                "model_responses_for_synthetic_diagnostics": fixture["responses"],
                "original_upload": {"documents": [
                    {"id": identifier, "sha256": "0" * 64}
                    for identifier in measurement.DOCUMENT_IDS
                ]},
            }],
        }
        real_read_bytes = Path.read_bytes

        def read_with_changed_baseline(path):
            if path == ROOT / measurement.BASELINE:
                return json.dumps(historical).encode("utf-8")
            return real_read_bytes(path)

        with patch.object(Path, "read_bytes", read_with_changed_baseline):
            with self.assertRaisesRegex(measurement.MeasurementError, "PDF bytes differ"):
                measurement.read_fixture()

    def test_two_capture_contract_is_required(self):
        real_read_bytes = Path.read_bytes
        historical = json.loads((ROOT / measurement.BASELINE).read_bytes())
        case = next(item for item in historical["cases"] if item["case_id"] == "F04")
        case["model_responses_for_synthetic_diagnostics"].pop()

        def read_with_one_response(path):
            if path == ROOT / measurement.BASELINE:
                return json.dumps(historical).encode("utf-8")
            return real_read_bytes(path)

        with patch.object(Path, "read_bytes", read_with_one_response):
            with self.assertRaisesRegex(measurement.MeasurementError, "exactly two"):
                measurement.read_fixture()


class FixedF04MeasurementIntegrationTests(unittest.TestCase):
    def test_archived_baseline_current_replay_measurement_is_offline_and_preserving(self):
        report = measurement.measure()
        self.assertTrue(report["passed"], report["checks"])
        self.assertEqual(report["before_origin"]["kind"], "historical_saved_baseline")
        self.assertIs(report["before_origin"]["old_implementation_rerun"], False)
        self.assertEqual(report["after_origin"]["kind"], "current_code_captured_response_replay")
        self.assertIs(report["after_origin"]["live_model_run"], False)
        self.assertIs(report["after_origin"]["manual_answers_used_for_replay_or_comparison"], False)
        replay = report["replay_observations"]
        self.assertEqual(replay["actual_model_transport_calls"], 0)
        self.assertEqual(replay["network_connection_attempts"], 0)
        self.assertEqual(replay["captured_response_replay_calls"], 2)
        self.assertEqual(len(replay["supplied_response_sha256"]), 2)
        self.assertTrue(replay["captured_responses_supplied_unchanged"])
        self.assertTrue(replay["temporary_store_removed"])
        self.assertEqual(len(report["input_identity"]["files_sha256_before"]), 4)
        self.assertTrue(report["checks"]["input_files_unchanged"])
        self.assertNotIn("elapsed_seconds", report)
        comparison = report["comparison"]
        self.assertEqual(comparison["status_unchanged_count"], 10)
        self.assertEqual(comparison["normalized_value_pairs_unchanged_count"], 10)
        self.assertEqual(comparison["fan_before"]["handoff_unique_source_count"], 1)
        self.assertEqual(comparison["fan_after"]["handoff_unique_source_count"], 2)
        self.assertIs(comparison["fan_before"]["evidence_complete"], True)
        self.assertIs(comparison["fan_after"]["evidence_complete"], False)
        self.assertTrue(all(item["present_in_original_page"]
                            for item in replay["source_page_checks"]))
        self.assertEqual(report["before_origin"]["public_copy_notice"]["kind"], "redacted_public_copy")


if __name__ == "__main__":
    unittest.main()
