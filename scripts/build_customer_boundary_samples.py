"""Author five new synthetic PDFs for customer-boundary UI checks.

No fixtures, expected-results files, review engine, model or server are called.
Manual expectations are authored here and kept in the manifest, not PDF bodies.
Existing output folders are refused so other samples cannot be overwritten.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

from pypdf import PdfReader
from reportlab.platypus import Spacer

from build_day1_artifacts import CUSTOMER_NOTICE, configure_styles, grid, paragraph, render

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "pdf" / "customer-boundary"
PROJECT_ID = "BROWSER-CUSTOMER"
DECLARATION = "HANDOFF REVIEW / SYNTHETIC DOCUMENT"
FOOTER = "교육용 가상 자료 | 실제 설계·시험·승인용이 아닙니다."
TECHNICAL_LINES = (
    "정격용량: 31500 kVA",
    "고압측정격전압: 154000 V",
    "저압측정격전압: 22900 V",
    "정격주파수: 60 Hz",
    "상수: 3상",
    "결선그룹: Yyn0",
    "냉각방식: ONAF",
    "냉각팬전원: 400 V",
    "탭절환기 방식: DETC",
    "설치 구분: 옥외형",
)
DOCUMENTS = (
    {"id": "SPEC-CB", "role": "customer_spec", "filename": "SPEC-CB.pdf",
     "customer_lines": ("한전",), "manual_customer_status": "recognized"},
    {"id": "AMD-CB", "role": "amendment", "filename": "AMD-CB.pdf",
     "customer_lines": ("KEPCO",), "manual_customer_status": "recognized"},
    {"id": "HANDOFF-MISSING", "role": "handoff", "filename": "HANDOFF-MISSING.pdf",
     "customer_lines": (), "manual_customer_status": "missing"},
    {"id": "HANDOFF-UNKNOWN", "role": "handoff", "filename": "HANDOFF-UNKNOWN.pdf",
     "customer_lines": ("Powergrid",), "manual_customer_status": "unknown"},
    {"id": "HANDOFF-AMBIGUOUS", "role": "handoff", "filename": "HANDOFF-AMBIGUOUS.pdf",
     "customer_lines": ("한전", "SK하이닉스"), "manual_customer_status": "ambiguous"},
)


def source_lines(document: dict) -> tuple[str, ...]:
    return ("냉각방식: ONAF",) if document["role"] == "amendment" else TECHNICAL_LINES


def metadata_lines(document: dict) -> list[str]:
    predecessor = "SPEC-CB" if document["role"] == "amendment" else "없음"
    return [
        f"문서 ID {document['id']} | 프로젝트 {PROJECT_ID}",
        f"문서 역할 {document['role']} | 데모 상태 approved",
        f"승인 표기 DEMO-REVIEWER | 대체 대상 {predecessor}",
        *[f"고객사(발주처) {name}" for name in document["customer_lines"]],
    ]


def story(document: dict, styles: dict) -> list:
    titles = {"customer_spec": "가상 고객 기술사양서", "amendment": "가상 기술 변경 합의서",
              "handoff": "가상 내부 수주 인계표"}
    parts = [paragraph(titles[document["role"]], styles["title"])]
    parts.extend(paragraph(line, styles["small"]) for line in metadata_lines(document))
    parts.extend([
        paragraph(CUSTOMER_NOTICE, styles["small"]),
        Spacer(1, 10),
        paragraph("문서 범위", styles["h2"]),
        paragraph("하나의 가상 프로젝트와 제품을 대상으로 작성한 교육용 기술 인계 문서입니다. "
                  "아래 수치는 문서 비교용 데이터이며 실제 설비의 설계 기준이 아닙니다.", styles["body"]),
        paragraph("변경 후 적용 조건" if document["role"] == "amendment" else "기재 기술 조건", styles["h2"]),
        grid([["번호", "기술 조건"]] + [[f"{index:02}", line] for index, line in enumerate(source_lines(document), 1)],
             [55, 468], styles),
        Spacer(1, 13),
        paragraph("이 합의서는 냉각방식 항목만 대체합니다. 다른 기술 조건은 SPEC-CB의 기재값을 따릅니다."
                  if document["role"] == "amendment" else
                  "적용할 변경 문서와 원문을 함께 확인합니다. 이 문서의 승인 표기는 실습용 메타데이터입니다.",
                  styles["body"]),
        paragraph("설계 승인, 제품 안전, 검사 합격 또는 실제 거래 관계를 증명하지 않습니다.", styles["small"]),
    ])
    return parts


def inspect_pdf(target: Path, document: dict) -> dict:
    """Check authored text only; no application verdict is used as an answer."""
    reader = PdfReader(target)
    pages = [{"page": index, "text": page.extract_text() or ""} for index, page in enumerate(reader.pages, 1)]
    if len(pages) != 1:
        raise AssertionError(f"Expected one-page document: {target.name} ({len(pages)})")
    text = "\n".join(page["text"] for page in pages)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    required = [DECLARATION, FOOTER, *metadata_lines(document), *source_lines(document)]
    for expected in required:
        if expected not in lines:
            raise AssertionError(f"{target.name}: missing authored text {expected!r}")
    customer_lines = [line for line in lines if line.startswith("고객사(발주처)")]
    if customer_lines != [f"고객사(발주처) {name}" for name in document["customer_lines"]]:
        raise AssertionError(f"{target.name}: customer-header count/content changed")
    return {
        "page_count": len(pages),
        "pages": [page["page"] for page in pages],
        "customer_header_count": len(customer_lines),
        "technical_line_count": len(source_lines(document)),
        "sha256": sha256(target.read_bytes()).hexdigest(),
        "bytes": target.stat().st_size,
        "text_check": "authored_metadata_declaration_and_technical_lines_present",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path, default=Path("/Library/Fonts/Arial Unicode.ttf"))
    arguments = parser.parse_args()
    styles = configure_styles(arguments.font)
    # All writes stay inside this new folder. Refuse even a partial old folder.
    OUTPUT.mkdir(parents=False, exist_ok=False)
    files = []
    for document in DOCUMENTS:
        target = OUTPUT / document["filename"]
        render(target, story(document, styles), DECLARATION)
        files.append({
            "document_id": document["id"], "role": document["role"],
            "filename": document["filename"], "path": str(target.relative_to(ROOT)),
            "project_id": PROJECT_ID, "status": "approved", "approved_by": "DEMO-REVIEWER",
            "supersedes": "SPEC-CB" if document["role"] == "amendment" else None,
            "authored_customer_headers": list(document["customer_lines"]),
            "manual_expected_customer_status": document["manual_customer_status"],
            **inspect_pdf(target, document),
        })
    cases = []
    for document in DOCUMENTS[2:]:
        cases.append({
            "id": f"CB-{document['manual_customer_status'].upper()}",
            "mode": "text_ai",
            "files": {"spec_file": "SPEC-CB.pdf", "amendment_file": "AMD-CB.pdf",
                      "handoff_file": document["filename"]},
            "manual_expectation": {
                "handoff_customer_status": document["manual_customer_status"],
                "customer_check_status": "NEEDS_REVIEW",
                "can_compare": False,
                "all_ten_technical_statuses": "NEEDS_REVIEW",
                "ai_extraction_required_for_hold_view": False,
                "note": "사람이 고객사 확인 결과를 읽고 확인란을 체크한 뒤, AI 추출 없이 보류 결과를 조회하는 UI 경계 사례입니다.",
            },
        })
    manifest = {
        "schema_version": "customer-boundary-inputs-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "synthetic_only": True,
        "project_id": PROJECT_ID,
        "mode": "text_ai",
        "expectation_origin": "manually_authored_not_application_engine_output",
        "purpose": "고객사 누락·미등록·상충의 UI 경계 확인용 개발 입력",
        "limitations": [
            "실제 고객 자료·거래 관계·법인 동일성 검증이 아닙니다.",
            "독립 holdout이나 모델 성능 평가 자료가 아닙니다.",
            "고객사 기본정보는 기술사양 10개와 분리합니다.",
            "수동 예상은 PDF 본문이나 모델 입력에 포함하지 않습니다.",
        ],
        "files": files, "cases": cases,
    }
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "pdf_files": len(files),
                      "pages": sum(item["page_count"] for item in files),
                      "authored_text_checks_passed": True}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
