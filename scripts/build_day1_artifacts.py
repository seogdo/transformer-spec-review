"""가상 업무 문서와 별도 Day 1 요약 PDF를 생성합니다.

실제 회사 양식이나 자료를 복제하지 않습니다. fixtures의 원문 quote를 PDF에
그대로 배치하므로, 별도 검사에서 페이지별 출처 텍스트 보존을 확인할 수 있습니다.
고객사 기능 샘플만 갱신할 때는 create_samples만 호출해 과거 요약을 보존합니다.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import re
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                               TableStyle, PageBreak, KeepTogether)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "pdf"
NAVY = colors.HexColor("#173C46")
TEAL = colors.HexColor("#16796E")
PALE = colors.HexColor("#EEF6F4")
GRAY = colors.HexColor("#57676B")
LINE = colors.HexColor("#D9E3E4")
FONT = "HandoffKorean"
CUSTOMER_NOTICE = "고객사 표기는 식별 기능 시험용이며 실제 거래를 뜻하지 않습니다."
CUSTOMER_ALIASES = {"customer_spec": "한국전력", "amendment": "KEPCO", "handoff": "한전"}


def configure_styles(font_path: Path) -> dict:
    if not font_path.is_file():
        raise FileNotFoundError("한글 TTF 폰트를 --font로 지정해야 합니다.")
    pdfmetrics.registerFont(TTFont(FONT, str(font_path)))
    styles = getSampleStyleSheet()
    for name in ("Normal", "BodyText", "Heading1", "Heading2", "Heading3"):
        styles[name].fontName = FONT
    return {
        "title": ParagraphStyle("KTitle", fontName=FONT, fontSize=23, leading=32,
                                textColor=NAVY, spaceAfter=13),
        "h2": ParagraphStyle("KH2", fontName=FONT, fontSize=12.4, leading=18,
                             textColor=NAVY, spaceBefore=16, spaceAfter=7),
        "body": ParagraphStyle("KBody", fontName=FONT, fontSize=10, leading=15.5,
                               textColor=NAVY, spaceAfter=7, wordWrap="CJK"),
        "small": ParagraphStyle("KSmall", fontName=FONT, fontSize=8.4, leading=12.5,
                                textColor=GRAY, spaceAfter=4, wordWrap="CJK"),
        "cell": ParagraphStyle("KCell", fontName=FONT, fontSize=9, leading=13.5,
                               textColor=NAVY, alignment=TA_LEFT, wordWrap="CJK"),
        "thead": ParagraphStyle("KTH", fontName=FONT, fontSize=9, leading=13,
                                textColor=colors.white, wordWrap="CJK"),
    }


def paragraph(text: object, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(str(text)).replace("\n", "<br/>"), style)


def grid(rows: list, widths: list[float], styles: dict) -> Table:
    data = [[paragraph(cell, styles["thead"] if i == 0 else styles["cell"])
             for cell in row] for i, row in enumerate(rows)]
    table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PALE]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, TEAL),
        ("LINEBELOW", (0, 1), (-1, -1), 0.3, LINE),
    ]))
    return table


def render(path: Path, story: list, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    def decoration(canvas, doc):
        canvas.saveState()
        width, height = A4
        canvas.setFillColor(TEAL)
        canvas.rect(36, height - 30, 27, 3, fill=1, stroke=0)
        canvas.setFont(FONT, 8)
        canvas.setFillColor(GRAY)
        canvas.drawString(72, height - 30, label)
        canvas.setStrokeColor(LINE)
        canvas.line(36, 36, width - 36, 36)
        canvas.setFont(FONT, 7.4)
        canvas.drawString(36, 23, "교육용 가상 자료 | 실제 설계·시험·승인용이 아닙니다.")
        canvas.drawRightString(width - 36, 23, str(doc.page))
        canvas.restoreState()

    document = SimpleDocTemplate(str(path), pagesize=A4, rightMargin=36,
                                 leftMargin=36, topMargin=51, bottomMargin=49,
                                 title=label, author="문성도 - 교육용 프로젝트")
    document.build(story, onFirstPage=decoration, onLaterPages=decoration)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def customer_pdf_name(document: dict) -> str:
    """Use explicit demo aliases without inferring a real customer identity."""
    name = document.get("customer_name", "한전")
    return CUSTOMER_ALIASES[document["role"]] if name == "한전" else str(name)


def sample_cases(fixtures: dict) -> list[dict]:
    """Keep D01/D02 requirements unchanged and author a separate C01 identity case."""
    cases = deepcopy(fixtures["cases"][:2])
    normal = next(case for case in cases if case["case_id"] == "D02")
    different_customer = deepcopy(normal)
    different_customer["case_id"] = "C01"
    # Answer labels remain outside the PDF generator's document content.
    different_customer.pop("expected", None)
    identifiers = {"customer_spec": "SPEC-C01", "amendment": "AMD-C01", "handoff": "HANDOFF-C01"}
    for document in different_customer["documents"]:
        role = document["role"]
        document["id"] = identifiers[role]
        document["project_id"] = "DEMO-C01"
        document["supersedes"] = "SPEC-C01" if role == "amendment" else None
        document["customer_name"] = "SK하이닉스" if role == "handoff" else "한국전력"
    different_customer["review_policy"] = {
        "base_id": "SPEC-C01",
        "approved_amendment_ids": ["AMD-C01"],
        "handoff_id": "HANDOFF-C01",
    }
    cases.append(different_customer)
    return cases


def create_samples(styles: dict, fixtures: dict) -> list:
    labels = {"rated_power": "정격용량", "hv_voltage": "고압측 정격전압",
              "lv_voltage": "저압측 정격전압", "frequency": "정격주파수",
              "phases": "상수", "vector_group": "결선기호", "cooling_type": "냉각방식",
              "fan_supply_voltage": "냉각팬 전원전압",
              "tap_changer_type": "탭절환기 방식", "installation_type": "설치 구분"}
    result = []
    cases = sample_cases(fixtures)
    for case in cases:
        for index, doc in enumerate(case["documents"], 1):
            # 경로에는 고정된 케이스 ID와 역할만 사용해 입력 제목의 경로문자를 배제합니다.
            case_id = re.sub(r"[^A-Za-z0-9_-]", "_", case["case_id"])
            role = re.sub(r"[^A-Za-z0-9_-]", "_", doc["role"])
            target = OUTPUT / "samples" / case_id / f"{index:02d}_{role}.pdf"
            story = [paragraph(doc["title"], styles["title"]),
                     paragraph(f"문서 ID  {doc['id']}  |  프로젝트  {doc['project_id']}", styles["small"]),
                     paragraph(f"문서 역할  {doc['role']}  |  데모 상태  {doc['status']}", styles["small"]),
                     paragraph(f"승인 표기  {doc.get('approved_by') or '없음'}  |  대체 대상  {doc.get('supersedes') or '없음'}", styles["small"]),
                     paragraph(f"고객사(발주처) {customer_pdf_name(doc)}", styles["small"]),
                     paragraph(CUSTOMER_NOTICE, styles["small"]),
                     Spacer(1, 10),
                     paragraph("문서 목적", styles["h2"]),
                     paragraph("수주 기술 인계 문서 대조를 검증하기 위해 작성한 가상 문서입니다. 승인 표기는 시험용 메타데이터이며 실제 권한이나 서명 확인을 의미하지 않습니다.", styles["body"])]
            rows = [["항목", "기재 값", "원문 근거"]]
            for key, field in doc["fields"].items():
                value = field.get("value", "")
                unit = field.get("unit") or ""
                rows.append([labels.get(key, key), f"{value} {unit}".strip(), field.get("quote") or "근거 없음"])
            story.extend([grid(rows, [115, 108, 300], styles), Spacer(1, 13),
                          paragraph("검토 안내", styles["h2"]),
                          paragraph("기준 문서와 승인 변경 적용 순서는 사용자가 확인합니다. 작성일만으로 문서 우선순위를 결정하지 않습니다. 이 문서는 원문 비교용이며 정답 판정은 별도 개발 데이터에 보관합니다.", styles["small"])])
            render(target, story, "HANDOFF REVIEW / SYNTHETIC DOCUMENT")
            result.append({"case_id": case["case_id"], "document_id": doc["id"],
                           "role": doc["role"], "customer_name": customer_pdf_name(doc),
                           "project_id": doc["project_id"], "supersedes": doc.get("supersedes"),
                           "path": str(target.relative_to(ROOT))})
    sample_output = OUTPUT / "samples"
    manifest = {
        "synthetic_only": True, "schema_version": "template-customer-pdf-1",
        "notice": CUSTOMER_NOTICE, "samples": result,
        "cases": [{"case_id": case["case_id"], "review_policy": case["review_policy"]}
                  for case in cases],
    }
    # The expected customer identity and technical input relationship are
    # authored separately. They never appear in uploaded PDF content.
    expected = {
        "synthetic_only": True, "do_not_send_to_model": True,
        "evaluation_scope": "authored_development_examples_not_independent_holdout",
        "cases": [
            {"case_id": "D01", "customer_identity_expected": "MATCH"},
            {"case_id": "D02", "customer_identity_expected": "MATCH"},
            {"case_id": "C01", "customer_identity_expected": "MISMATCH",
             "technical_input_relationship": {key: "MATCH" for key in labels},
             "expected": {key: "NEEDS_REVIEW" for key in labels},
             "review_note": "기술 입력은 D02와 같지만 인계표 고객사만 다릅니다. 고객사 불일치로 최종 기술판정 10개를 모두 보류합니다. 고객사 확인을 기술 항목의 값 관계와 구분하며 실제 거래관계를 뜻하지 않습니다."},
        ],
    }
    (sample_output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (sample_output / "expected-results.json").write_text(json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path, default=Path("/System/Library/Fonts/Supplemental/AppleGothic.ttf"))
    args = parser.parse_args()
    styles = configure_styles(args.font)
    fixtures = read_json(ROOT / "fixtures" / "cases.json")
    if len(fixtures.get("cases", [])) < 2:
        raise ValueError("가상 사례가 최소 2개 필요합니다.")
    samples = create_samples(styles, fixtures)
    # The public package generates synthetic samples only. The historical
    # Day 1 brief depended on private development reports and is not published.
    manifest = {"synthetic_only": True, "samples": samples,
                "notice": "회사 원본이 아닌 가상 문서이며 실제 승인에 사용할 수 없습니다."}
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
