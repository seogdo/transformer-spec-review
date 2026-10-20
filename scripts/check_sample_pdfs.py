"""생성된 가상 PDF의 텍스트 보존과 페이지 수를 확인합니다.

PDF를 AI로 이해하는 테스트가 아니라 생성된 파일의 텍스트 추출 점검입니다.
레이아웃은 별도로 PNG 렌더링해 확인해야 합니다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]


def squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def main() -> int:
    manifest = json.loads((ROOT / "output" / "pdf" / "samples" / "manifest.json").read_text(encoding="utf-8"))
    cases = json.loads((ROOT / "fixtures" / "cases.json").read_text(encoding="utf-8"))["cases"]
    source_docs = {(case["case_id"], doc["id"]): doc for case in cases for doc in case["documents"]}
    checks = []
    for item in manifest["samples"]:
        path = ROOT / item["path"]
        reader = PdfReader(path)
        # C01 intentionally reuses D02's technical text with independent
        # document/project/customer metadata; expectations stay outside PDFs.
        if item["case_id"] == "C01":
            doc = next(doc for case in cases if case["case_id"] == "D02"
                       for doc in case["documents"] if doc["role"] == item["role"])
        else:
            doc = source_docs[(item["case_id"], item["document_id"])]
        missing = []
        for field, entry in doc["fields"].items():
            quote = entry.get("quote")
            page = entry.get("page")
            if not quote or not isinstance(page, int) or not 1 <= page <= len(reader.pages):
                missing.append(field)
            elif squash(quote) not in squash(reader.pages[page - 1].extract_text()):
                missing.append(field)
        combined = "\n".join(page.extract_text() for page in reader.pages)
        if item["document_id"] not in combined:
            missing.append("document_id")
        if f"고객사(발주처) {item['customer_name']}" not in combined:
            missing.append("customer_name")
        checks.append({"path": item["path"], "pages": len(reader.pages),
                       "passed": not missing and len(reader.pages) == 1,
                       "missing_fields_or_quotes": missing})
    report = {"kind": "generated_pdf_text_preservation_not_ai_extraction",
              "checks": checks, "passed": all(row["passed"] for row in checks)}
    output = ROOT / "reports" / "pdf-check.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
