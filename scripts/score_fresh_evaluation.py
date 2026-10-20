"""Score a frozen synthetic-document run after every model call has finished.

This module has no dependency on the application's extractor, comparator,
normalizer, model client, or development fixtures. It does not run a model.
The runner is responsible for keeping reference answers out of model inputs
and opening them only after the one-shot run is complete.

The result describes agreement with manually authored document expectations,
not engineering suitability, independent AI accuracy, or real-world safety.
Status, normalized values, original-source verification, and manual evidence
alignment are deliberately reported separately.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "fresh-evaluation-score-v1"
# Own frozen scope, rather than importing code whose behavior is being tested.
FIELD_KEYS = (
    "rated_power", "hv_voltage", "lv_voltage", "frequency", "phases",
    "vector_group", "cooling_type", "fan_supply_voltage", "tap_changer_type",
    "installation_type",
)
NUMERIC_UNITS = {
    "rated_power": "kVA", "hv_voltage": "V", "lv_voltage": "V",
    "frequency": "Hz", "phases": "count", "fan_supply_voltage": "V",
}
STATUSES = ("MATCH", "MISMATCH", "MISSING", "NEEDS_REVIEW")
CUSTOMER_STATUSES = ("MATCH", "MISMATCH", "NEEDS_REVIEW")
OBSERVATION_ERRORS = (
    "REVIEW_UNAVAILABLE", "MISSING_ROW", "DUPLICATE_ROW", "INVALID_STATUS",
)
METRIC_KEYS = (
    "false_clear", "false_alert", "conservative_hold",
    "missed_clear_difference", "missed_clear_difference_hold",
    "missed_clear_difference_false_clear", "missed_clear_difference_other",
    "unsafe_resolution", "unsafe_resolution_to_definite",
    "expected_hold_without_valid_result",
)
LIMITATIONS = (
    "새로 작성한 가상 문서와 수동 기대 결과의 일치 여부입니다. 공학적 적합성·제품 합격을 판정하지 않습니다.",
    "실제 업무 문서의 분포나 독립적인 AI 정확도를 대표하지 않습니다.",
    "원문 근거 유효성은 실행기가 원본 PDF 페이지·해시와 대조한 별도 감사 결과입니다.",
    "수동 근거 정렬은 문서 ID·페이지와 인용 부분문자열의 기계적 대조입니다. 의미적 동등성 평가가 아닙니다.",
    "NEEDS_REVIEW 정답의 null 값은 값 채점 제외이며, 다른 상태의 null 값은 실제 값 부재를 뜻합니다.",
)


def _number(value: Any) -> Decimal | None:
    """Compare finite canonical numbers exactly, without unit conversion."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = str(value)
    if not text or len(text) > 160:
        return None
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _same_value(field: str, reference: Any, observed: Any) -> bool:
    if reference is None:
        return observed is None
    if not isinstance(observed, Mapping) or not isinstance(reference, Mapping):
        return False
    if observed.get("unit") != reference.get("unit"):
        return False
    if field in NUMERIC_UNITS:
        expected_number, actual_number = _number(reference.get("value")), _number(observed.get("value"))
        return expected_number is not None and actual_number is not None and expected_number == actual_number
    return isinstance(observed.get("value"), str) and observed["value"] == reference.get("value")


def _display_value(value: Any) -> Any:
    """Avoid copying nested application source records into value diagnostics."""
    if isinstance(value, Mapping):
        return {"value": deepcopy(value.get("value")), "unit": deepcopy(value.get("unit"))}
    return deepcopy(value)


def _validate_reference(case: Any) -> None:
    """Reject incomplete answer keys instead of silently awarding partial credit."""
    if not isinstance(case, Mapping) or not isinstance(case.get("case_id"), str) or not case["case_id"].strip():
        raise ValueError("정답 사례에는 비어 있지 않은 case_id가 필요합니다.")
    if case.get("customer_status") not in CUSTOMER_STATUSES:
        raise ValueError(f"{case['case_id']}: customer_status가 올바르지 않습니다.")
    fields = case.get("fields")
    if not isinstance(fields, Mapping) or set(fields) != set(FIELD_KEYS):
        raise ValueError(f"{case['case_id']}: 정답은 기술사양 10개 키의 정확한 집합이어야 합니다.")
    for field in FIELD_KEYS:
        row = fields[field]
        if not isinstance(row, Mapping) or row.get("status") not in STATUSES:
            raise ValueError(f"{case['case_id']}/{field}: 정답 상태가 올바르지 않습니다.")
        status = row["status"]
        if "expected" not in row or "actual" not in row:
            raise ValueError(f"{case['case_id']}/{field}: expected와 actual을 명시해 주세요.")
        if case["customer_status"] != "MATCH" and status != "NEEDS_REVIEW":
            raise ValueError(f"{case['case_id']}/{field}: 고객사 보류 사례의 모든 기술 항목은 NEEDS_REVIEW여야 합니다.")
        if status == "NEEDS_REVIEW":
            if row["expected"] is not None or row["actual"] is not None:
                raise ValueError(f"{case['case_id']}/{field}: 보류 정답의 값은 null이어야 합니다.")
        else:
            for side in ("expected", "actual"):
                value = row[side]
                if status == "MISSING" and side == "actual":
                    if value is not None:
                        raise ValueError(f"{case['case_id']}/{field}: MISSING의 actual은 null이어야 합니다.")
                    continue
                if not isinstance(value, Mapping) or not isinstance(value.get("value"), str):
                    raise ValueError(f"{case['case_id']}/{field}/{side}: 정규화된 문자열 값과 단위가 필요합니다.")
                if value.get("unit") != NUMERIC_UNITS.get(field, ""):
                    raise ValueError(f"{case['case_id']}/{field}/{side}: canonical 단위가 아닙니다.")
                if field in NUMERIC_UNITS and _number(value["value"]) is None:
                    raise ValueError(f"{case['case_id']}/{field}/{side}: 유한 수치가 필요합니다.")
                if field not in NUMERIC_UNITS and not value["value"]:
                    raise ValueError(f"{case['case_id']}/{field}/{side}: 비어 있지 않은 열거값이 필요합니다.")
            if status in ("MATCH", "MISMATCH"):
                equal = _same_value(field, row["expected"], row["actual"])
                if equal != (status == "MATCH"):
                    raise ValueError(f"{case['case_id']}/{field}: 정답 상태와 정규화된 양쪽 값이 모순됩니다.")
        evidence = row.get("evidence", [])
        if not isinstance(evidence, list):
            raise ValueError(f"{case['case_id']}/{field}: evidence는 목록이어야 합니다.")
        for source in evidence:
            if (not isinstance(source, Mapping)
                    or not isinstance(source.get("document_id"), str) or not source["document_id"]
                    or type(source.get("page")) is not int or source["page"] < 1
                    or not isinstance(source.get("quote"), str) or not source["quote"].strip()):
                raise ValueError(f"{case['case_id']}/{field}: 수동 근거의 문서 ID·페이지·인용문을 확인해 주세요.")


def _row_map(review: Any) -> tuple[dict[str, list], dict]:
    buckets: dict[str, list] = defaultdict(list)
    raw_rows = review.get("results") if isinstance(review, Mapping) else None
    rows = raw_rows if isinstance(raw_rows, list) else []
    malformed, unexpected = [], []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping) or not isinstance(row.get("field"), str):
            malformed.append(index)
        elif row["field"] not in FIELD_KEYS:
            unexpected.append(row["field"])
        else:
            buckets[row["field"]].append(row)
    missing = [field for field in FIELD_KEYS if not buckets[field]]
    duplicates = [field for field in FIELD_KEYS if len(buckets[field]) > 1]
    diagnostic = {
        "exact": isinstance(raw_rows, list) and len(rows) == len(FIELD_KEYS)
                 and not (missing or duplicates or unexpected or malformed),
        "observed_row_count": len(rows),
        "results_is_list": isinstance(raw_rows, list),
        "missing_fields": missing, "duplicate_fields": duplicates,
        "unexpected_fields": unexpected, "malformed_row_indexes": malformed,
    }
    return buckets, diagnostic


def _evidence_alignment(reference: list, observed_row: Any) -> dict:
    """Only exact identity/page and reference-quote containment; no fuzzy match."""
    sources = observed_row.get("sources", []) if isinstance(observed_row, Mapping) else []
    if not isinstance(sources, list):
        sources = []
    checks = []
    for target in reference:
        candidates = [source for source in sources if isinstance(source, Mapping)
                      and source.get("document_id") == target["document_id"]
                      and type(source.get("page")) is int and source["page"] == target["page"]]
        aligned = any(isinstance(source.get("quote"), str) and target["quote"] in source["quote"]
                      for source in candidates)
        checks.append({
            "document_id": target["document_id"], "page": target["page"],
            "reference_quote": target["quote"], "aligned": aligned,
            "reason": "exact_reference_excerpt_present" if aligned
                      else "quote_not_contained" if candidates else "document_or_page_not_found",
        })
    return {
        "status": "not_requested" if not checks else "aligned" if all(row["aligned"] for row in checks) else "not_aligned",
        "reference_count": len(checks), "aligned_count": sum(row["aligned"] for row in checks),
        "checks": checks, "affects_verdict_score": False,
    }


def score_case(expected_case: Mapping[str, Any], review: Any) -> dict:
    """Score ten statuses/values; never regenerate results from reference data.

    Invalid/missing/duplicated output rows receive explicit confusion buckets,
    never a guessed status. Reference NEEDS_REVIEW values are unscored even if
    the comparator retained a partially established expected value.
    """
    _validate_reference(expected_case)
    buckets, field_set = _row_map(review)
    unavailable = not isinstance(review, Mapping)
    confusion = {status: {actual: 0 for actual in STATUSES + OBSERVATION_ERRORS} for status in STATUSES}
    metrics = {key: 0 for key in METRIC_KEYS}
    details = []
    for field in FIELD_KEYS:
        reference = expected_case["fields"][field]
        candidates = buckets[field]
        row = candidates[0] if len(candidates) == 1 else None
        if unavailable:
            observed_status = "REVIEW_UNAVAILABLE"
        elif not candidates:
            observed_status = "MISSING_ROW"
        elif len(candidates) > 1:
            observed_status = "DUPLICATE_ROW"
        elif row.get("status") not in STATUSES:
            observed_status = "INVALID_STATUS"
        else:
            observed_status = row["status"]
        expected_status = reference["status"]
        confusion[expected_status][observed_status] += 1
        correct = expected_status == observed_status
        metrics["false_clear"] += expected_status != "MATCH" and observed_status == "MATCH"
        metrics["false_alert"] += expected_status == "MATCH" and observed_status == "MISMATCH"
        metrics["conservative_hold"] += expected_status in ("MATCH", "MISMATCH") and observed_status == "NEEDS_REVIEW"
        missed = expected_status == "MISMATCH" and observed_status != "MISMATCH"
        metrics["missed_clear_difference"] += missed
        metrics["missed_clear_difference_hold"] += missed and observed_status == "NEEDS_REVIEW"
        metrics["missed_clear_difference_false_clear"] += missed and observed_status == "MATCH"
        metrics["missed_clear_difference_other"] += missed and observed_status not in ("MATCH", "NEEDS_REVIEW")
        unsafe = expected_status == "NEEDS_REVIEW" and observed_status != "NEEDS_REVIEW"
        metrics["unsafe_resolution"] += unsafe
        metrics["unsafe_resolution_to_definite"] += unsafe and observed_status in STATUSES
        metrics["expected_hold_without_valid_result"] += unsafe and observed_status not in STATUSES
        value_checks = []
        if expected_status != "NEEDS_REVIEW":
            for side in ("expected", "actual"):
                observed = row.get(side) if row is not None else None
                # Missing output keys are not equivalent to an explicit null.
                matched = row is not None and side in row and _same_value(field, reference[side], observed)
                value_checks.append({
                    "side": side, "reference": _display_value(reference[side]),
                    "observed": _display_value(observed), "matched": matched,
                    "observed_present": row is not None and side in row,
                })
        values_match = all(item["matched"] for item in value_checks) if value_checks else None
        details.append({
            "field": field, "expected_status": expected_status, "observed_status": observed_status,
            "status_correct": correct, "values_checked": bool(value_checks),
            "values_correct": values_match, "value_checks": value_checks,
            "fully_correct": correct and values_match is not False,
            "evidence_alignment": _evidence_alignment(reference.get("evidence", []), row),
        })
    identity = review.get("customer_check") if isinstance(review, Mapping) else None
    identity = identity if isinstance(identity, Mapping) else {}
    expected_customer = expected_case["customer_status"]
    expected_comparison = expected_customer == "MATCH"
    customer_correct = identity.get("status") == expected_customer and identity.get("can_compare") is expected_comparison
    hold_required = not expected_comparison
    all_held = field_set["exact"] and all(item["observed_status"] == "NEEDS_REVIEW" for item in details)
    counts = {
        "expected_fields": len(FIELD_KEYS),
        "status_correct_fields": sum(item["status_correct"] for item in details),
        "value_checked_fields": sum(item["values_checked"] for item in details),
        "value_correct_fields": sum(item["values_correct"] is True for item in details),
        "value_mismatch_fields": sum(item["values_correct"] is False for item in details),
        "fully_correct_fields": sum(item["fully_correct"] for item in details),
    }
    passed = field_set["exact"] and customer_correct and all(item["fully_correct"] for item in details)
    return {
        "case_id": expected_case["case_id"], "review_available": not unavailable,
        "status_value_passed": passed, "field_set": field_set, "counts": counts,
        "customer": {
            "expected_status": expected_customer, "observed_status": identity.get("status"),
            "observed_can_compare": identity.get("can_compare"), "correct": customer_correct,
            "hold_required": hold_required,
            "hold_enforced": (all_held and identity.get("can_compare") is False) if hold_required else None,
        },
        "error_counts": metrics, "status_confusion": confusion, "fields": details,
    }


def _provenance(audit: Any) -> dict:
    """Summarize, without manufacturing an original-source verification result."""
    if not isinstance(audit, Mapping):
        return {"status": "not_checked", "passed": None, "failed_checks": [],
                "reason": "원본 페이지·해시 검증 결과가 제공되지 않았습니다."}
    checks = audit.get("checks", [])
    checks = checks if isinstance(checks, list) else []
    failures = [deepcopy(check) for check in checks
                if isinstance(check, Mapping) and check.get("passed") is False]
    declared_failure = audit.get("passed") is False or audit.get("status") == "failed"
    declared_success = audit.get("passed") is True or audit.get("status") == "passed"
    if declared_failure or failures:
        status, passed = "failed", False
    elif declared_success:
        status, passed = "passed", True
    else:
        status, passed = "not_checked", None
    return {"status": status, "passed": passed, "failed_checks": failures,
            "audit": deepcopy(dict(audit))}


def score_run(expected_cases: list, observations: list) -> dict:
    """Aggregate all planned cases; absent or duplicated runs cannot pass.

    ``observations`` contains ``case_id``, ``review`` and a runner-produced
    ``provenance_audit``. A case may satisfy status/value checks while original
    provenance is unverified; ``passed`` remains false until both are checked.
    Manual evidence alignment is diagnostic only and never changes a status.
    """
    if not isinstance(expected_cases, list) or not expected_cases:
        raise ValueError("비어 있지 않은 정답 사례 목록이 필요합니다.")
    if not isinstance(observations, list):
        raise ValueError("실행 관측 결과는 목록이어야 합니다.")
    for case in expected_cases:
        _validate_reference(case)
    expected_ids = [case["case_id"] for case in expected_cases]
    if len(set(expected_ids)) != len(expected_ids):
        raise ValueError("정답 사례 ID가 중복됐습니다.")
    indexed: dict[str, list] = defaultdict(list)
    malformed = []
    for index, observation in enumerate(observations):
        if not isinstance(observation, Mapping) or not isinstance(observation.get("case_id"), str):
            malformed.append(index)
        else:
            indexed[observation["case_id"]].append(observation)
    missing = [case_id for case_id in expected_ids if not indexed[case_id]]
    duplicate = [case_id for case_id, rows in indexed.items() if len(rows) > 1]
    unexpected = [case_id for case_id in indexed if case_id not in set(expected_ids)]
    scores = []
    for case in expected_cases:
        candidates = indexed[case["case_id"]]
        observation = candidates[0] if len(candidates) == 1 else {}
        result = score_case(case, observation.get("review"))
        result["observation_status"] = "present" if len(candidates) == 1 else "missing" if not candidates else "duplicate"
        result["provenance"] = _provenance(observation.get("provenance_audit"))
        result["passed"] = result["status_value_passed"] and result["provenance"]["passed"] is True
        scores.append(result)
    totals: Counter = Counter()
    errors: Counter = Counter()
    confusion = {status: {actual: 0 for actual in STATUSES + OBSERVATION_ERRORS} for status in STATUSES}
    alignment: Counter = Counter()
    for result in scores:
        totals.update(result["counts"])
        errors.update(result["error_counts"])
        for expected in confusion:
            for actual in confusion[expected]:
                confusion[expected][actual] += result["status_confusion"][expected][actual]
        for row in result["fields"]:
            evidence = row["evidence_alignment"]
            alignment["reference_count"] += evidence["reference_count"]
            alignment["aligned_count"] += evidence["aligned_count"]
    case_set_exact = not (missing or duplicate or unexpected or malformed)
    return {
        "schema_version": SCHEMA_VERSION,
        "evaluation_kind": "fresh_synthetic_document_reference_agreement",
        "passed": case_set_exact and all(result["passed"] for result in scores),
        "case_set": {"exact": case_set_exact, "expected_cases": len(expected_cases),
                     "observed_cases": len(observations), "missing_cases": missing,
                     "duplicate_cases": duplicate, "unexpected_cases": unexpected,
                     "malformed_observation_indexes": malformed},
        "status_value_passed_cases": sum(result["status_value_passed"] for result in scores),
        "passed_cases": sum(result["passed"] for result in scores),
        "provenance_counts": {status: sum(result["provenance"]["status"] == status for result in scores)
                              for status in ("passed", "failed", "not_checked")},
        "counts": dict(totals), "error_counts": dict(errors), "status_confusion": confusion,
        "evidence_alignment": {**dict(alignment), "affects_verdict_score": False},
        "cases": scores, "limitations": list(LIMITATIONS),
        "metric_notes": {
            "unsafe_resolution": "정답 보류에 대한 비보류 결과 전체입니다. 확정 오판과 결과 누락·잘못된 구조를 하위 지표로 구분합니다.",
            "values": "이미 정규화된 값의 유한 수치 동등성 및 canonical 단위를 비교하며 재정규화·단위 변환·별칭 추정은 하지 않습니다.",
            "passed": "정확한 사례·항목 집합, 고객사 상태, 항목 상태·값, 실행기의 원문 감사가 모두 통과해야 합니다.",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", type=Path, required=True,
                        help="모든 모델 호출 완료 후에만 열어야 하는 수동 정답 JSON")
    parser.add_argument("--observations", type=Path, required=True, help="cases 관측 목록을 포함한 실행 보고서")
    parser.add_argument("--output", type=Path, help="생략하면 결과를 표준 출력으로만 표시합니다.")
    args = parser.parse_args()
    if args.output and args.output.resolve() in {args.expected.resolve(), args.observations.resolve()}:
        parser.error("정답이나 원본 실행 보고서를 채점 결과로 덮어쓸 수 없습니다.")
    reference = json.loads(args.expected.read_text(encoding="utf-8"))
    observed = json.loads(args.observations.read_text(encoding="utf-8"))
    result = score_run(reference["cases"], observed["cases"])
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        # A caller must deliberately choose a fresh artifact path.
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(rendered)
        print(str(args.output))
    else:
        print(rendered, end="")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
