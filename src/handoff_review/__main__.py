"""Day 1 가상 케이스를 조회하는 CLI입니다. PDF 또는 LLM 실행기가 아닙니다."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .review import review_case


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", default="D01", help="fixtures/cases.json의 case_id")
    parser.add_argument("--fixtures", type=Path,
                        default=Path(__file__).resolve().parents[2] / "fixtures" / "cases.json")
    args = parser.parse_args()
    payload = json.loads(args.fixtures.read_text(encoding="utf-8"))
    case = next((item for item in payload["cases"] if item["case_id"] == args.case), None)
    if case is None:
        parser.error(f"존재하지 않는 케이스입니다: {args.case}")
    # 정답 라벨과 정답을 암시하는 사례 제목은 판정기에 전달하지 않습니다.
    # 향후 LLM 어댑터도 문서와 사람의 검토 정책만 입력받아야 합니다.
    input_case = {key: case[key] for key in ("case_id", "documents", "review_policy")}
    print(json.dumps(review_case(input_case), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
