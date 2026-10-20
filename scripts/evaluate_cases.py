"""수동 구조화 개발 사례의 회귀 결과를 기록합니다.

이 스크립트는 PDF 추출·검색·LLM을 호출하지 않습니다. 측정값은 참조판정기
코드의 회귀 결과이며, AI 정확도나 현장 오류 탐지율로 해석할 수 없습니다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from handoff_review.review import review_case  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=ROOT / "fixtures" / "cases.json")
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "domain-check.json")
    args = parser.parse_args()
    fixture_bytes = args.fixtures.read_bytes()
    fixtures = json.loads(fixture_bytes)
    records = []
    matched_fields = 0
    total_fields = 0
    for case in fixtures["cases"]:
        # 정답과 정답을 암시하는 사례 제목은 실행 입력에서 제외합니다.
        expected = case["expected"]
        input_case = {key: case[key] for key in ("case_id", "documents", "review_policy")}
        started = time.perf_counter()
        result = review_case(input_case)
        elapsed_ms = (time.perf_counter() - started) * 1000
        actual = {row["field"]: row["status"] for row in result["results"]}
        differences = [
            {"field": field, "expected": status, "actual": actual.get(field)}
            for field, status in expected.items() if actual.get(field) != status
        ]
        # 예측에서 누락·중복·추가된 항목도 실패로 취급합니다.
        if set(actual) != set(expected) or len(actual) != len(result["results"]):
            differences.append({"field": "__field_set__", "expected": sorted(expected),
                                "actual": sorted(actual)})
        matched_fields += sum(actual.get(key) == value for key, value in expected.items())
        total_fields += len(expected)
        records.append({"case_id": case["case_id"], "title": case["title"],
                        "passed": not differences, "elapsed_ms": round(elapsed_ms, 3),
                        "differences": differences, "result": result})
    core_hashes = {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((ROOT / "src" / "handoff_review").glob("*.py"))
    }
    report = {
        "schema_version": "day1-regression-v1",
        "generated_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        "evaluation_kind": "structured_development_fixture_regression_not_ai_quality",
        "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
        "core_sha256": core_hashes,
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "cases": len(records), "passed_cases": sum(item["passed"] for item in records),
        "checked_fields": total_fields, "matched_fields": matched_fields,
        "llm_called": False, "pdf_extraction_evaluated": False,
        "independent_holdout": False,
        "latency_note": "단일회 코드 실행시간입니다. 모델 추론·검색·PDF 추출 시간은 포함하지 않습니다.",
        "limitations": ["개발 중 공개된 수동 구조화 가상 사례입니다.",
                        "실제 문서 추출·승인 진위·현장 안전·실무 성능은 검증하지 않았습니다.",
                        "인위적으로 주입한 문제를 검사하며 실제 오류 분포를 대표하지 않습니다."],
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in
                     ("cases", "passed_cases", "checked_fields", "matched_fields", "llm_called")},
                     ensure_ascii=False))
    print(f"보고서: {args.output}")
    return 0 if report["passed_cases"] == report["cases"] and report["cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
