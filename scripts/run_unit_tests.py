"""단위 테스트를 실행하고 통과·실패 개수를 JSON 실행 기록으로 보존합니다."""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    output = {
        "generated_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        "kind": "unit_tests_not_ai_quality",
        "tests_run": result.testsRun,
        "failures": [{"test": str(test), "traceback": trace} for test, trace in result.failures],
        "errors": [{"test": str(test), "traceback": trace} for test, trace in result.errors],
        "skipped": [{"test": str(test), "reason": reason} for test, reason in result.skipped],
        "passed": result.wasSuccessful() and result.testsRun > 0,
    }
    path = ROOT / "reports" / "unit-tests.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if output["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
