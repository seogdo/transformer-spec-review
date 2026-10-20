"""Author a fresh synthetic PDF corpus without importing review implementation.

The field expectations below are manually authored from the public contract.
They must be frozen before any review-model run and must not be tuned to results.
This script only creates its own fresh-evaluation output and intermediate trees.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from xml.sax.saxutils import escape

from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "pdf" / "fresh-evaluation"
TMP = ROOT / "tmp" / "pdfs" / "fresh-evaluation"
FONT = "FreshEvaluationKorean"
NAVY = colors.HexColor("#163242")
TEAL = colors.HexColor("#087E83")
GRAY = colors.HexColor("#566B75")
PALE = colors.HexColor("#F0F6F7")
LINE = colors.HexColor("#D4E0E3")
HEADER = "HANDOFF REVIEW / SYNTHETIC DOCUMENT"
FOOTER = "교육용 가상 자료 | 실제 설계·시험·승인용이 아닙니다."
NOTICE = "고객사 명칭은 식별 기능 검증에 사용하는 가상 표기이며 실제 거래 관계를 뜻하지 않습니다."
KEYS = (
    "rated_power", "hv_voltage", "lv_voltage", "frequency", "phases",
    "vector_group", "cooling_type", "fan_supply_voltage",
    "tap_changer_type", "installation_type",
)
LABELS = {
    "rated_power": "정격용량",
    "hv_voltage": "고압측 정격전압",
    "lv_voltage": "저압측 정격전압",
    "frequency": "정격주파수",
    "phases": "상수",
    "vector_group": "결선기호",
    "cooling_type": "냉각방식",
    "fan_supply_voltage": "냉각팬 전원전압",
    "tap_changer_type": "탭절환기 방식",
    "installation_type": "설치 구분",
}
UNITS = {
    "rated_power": "kVA", "hv_voltage": "V", "lv_voltage": "V",
    "frequency": "Hz", "phases": "count", "vector_group": "",
    "cooling_type": "", "fan_supply_voltage": "V",
    "tap_changer_type": "", "installation_type": "",
}
ROLE_NAMES = {
    "customer_spec": "고객 사양서",
    "amendment": "사양 변경 통지",
    "handoff": "설계 인계서",
}
ROLE_PREFIX = {"customer_spec": "SPEC", "amendment": "AMD", "handoff": "HANDOFF"}


# These are newly authored input documents. No fixture builder or engine is used.
CASES = [
    {
        "case_id": "F01",
        "customers": {"customer_spec": "AEP", "amendment": "미국 AEP", "handoff": "AEP"},
        "spec": {
            "rated_power": "37.5 MVA", "hv_voltage": "66 kV",
            "lv_voltage": "6.9 kV", "frequency": "60 Hz", "phases": "3상",
            "vector_group": "Dyn11", "cooling_type": "ONAF",
            "fan_supply_voltage": "230 V", "tap_changer_type": "DETC",
            "installation_type": "옥외형",
        },
        "changes": {"fan_supply_voltage": "240 V"},
        "handoff": {
            "rated_power": "37500 kVA", "hv_voltage": "66000 V",
            "lv_voltage": "6900 V", "frequency": "60 Hz", "phases": "3상",
            "vector_group": "Dyn11", "cooling_type": "ONAF",
            "fan_supply_voltage": "240 V",
            "tap_changer_type": "무전압 탭절환기", "installation_type": "OUTDOOR",
        },
        "spec_order": ("rated_power", "phases", "frequency", "hv_voltage",
                       "lv_voltage", "vector_group", "installation_type",
                       "cooling_type", "tap_changer_type", "fan_supply_voltage"),
        "handoff_order": ("installation_type", "cooling_type", "fan_supply_voltage",
                          "tap_changer_type", "vector_group", "hv_voltage",
                          "lv_voltage", "rated_power", "frequency", "phases"),
    },
    {
        "case_id": "F02",
        "customers": {"customer_spec": "Transgrid", "amendment": "호주 Transgrid",
                      "handoff": "트랜스그리드"},
        "spec": {
            "rated_power": "18.75 MVA", "hv_voltage": "110 kV",
            "lv_voltage": "6.6 kV", "frequency": "50 Hz", "phases": "3상",
            "vector_group": "Dyn1", "cooling_type": "ONAN",
            "fan_supply_voltage": "415 V", "tap_changer_type": "DETC",
            "installation_type": "옥내형",
        },
        "changes": {"hv_voltage": "132 kV", "tap_changer_type": "OLTC"},
        "handoff": {
            "rated_power": "18750 kVA", "hv_voltage": "110000 V",
            "lv_voltage": "6600 V", "frequency": "50 Hz", "phases": "3상",
            "vector_group": "Dyn1", "cooling_type": "ONAN",
            "fan_supply_voltage": "415 V",
            "tap_changer_type": "무전압 탭절환기", "installation_type": "INDOOR",
        },
        "spec_order": ("installation_type", "hv_voltage", "lv_voltage",
                       "rated_power", "frequency", "phases", "vector_group",
                       "tap_changer_type", "fan_supply_voltage", "cooling_type"),
        "handoff_order": ("rated_power", "frequency", "phases", "vector_group",
                          "cooling_type", "installation_type", "fan_supply_voltage",
                          "lv_voltage", "hv_voltage", "tap_changer_type"),
    },
    {
        "case_id": "F03",
        "customers": {"customer_spec": "삼성전자", "amendment": "Samsung Electronics",
                      "handoff": "삼전"},
        "spec": {
            "rated_power": "27 MVA", "hv_voltage": "77 kV",
            "lv_voltage": "13.8 kV", "frequency": "60 Hz", "phases": "3상",
            "vector_group": "YNd11", "cooling_type": "ONAF",
            "fan_supply_voltage": "460 V", "tap_changer_type": "OLTC",
            "installation_type": "옥내형",
        },
        "changes": {"cooling_type": "OFAF"},
        "handoff": {
            "rated_power": "27000 kVA", "hv_voltage": "77000 V",
            "lv_voltage": "13800 V", "frequency": "60 Hz", "phases": "3상",
            "vector_group": "YNd11", "cooling_type": "OFAF",
            "tap_changer_type": "부하시 탭절환기", "installation_type": "미확정",
        },
        "spec_order": ("cooling_type", "fan_supply_voltage", "installation_type",
                       "tap_changer_type", "vector_group", "rated_power",
                       "hv_voltage", "lv_voltage", "phases", "frequency"),
        "handoff_order": ("hv_voltage", "lv_voltage", "vector_group",
                          "rated_power", "phases", "frequency", "cooling_type",
                          "tap_changer_type", "installation_type"),
    },
    {
        "case_id": "F04",
        "customers": {"customer_spec": "SK하이닉스", "amendment": "하이닉스",
                      "handoff": "SK hynix"},
        "spec": {
            "rated_power": "31.5 MVA", "hv_voltage": "154 kV",
            "lv_voltage": "22.9 kV", "frequency": "60 Hz", "phases": "3상",
            "vector_group": "Yyn0", "cooling_type": "ONAN",
            "fan_supply_voltage": "400 V", "tap_changer_type": "DETC",
            "installation_type": "옥외형",
        },
        "changes": {"cooling_type": "ONAF"},
        "handoff": {
            "rated_power": "31500 kVA", "hv_voltage": "154000 V",
            "lv_voltage": "22900 V", "frequency": "60 Hz", "phases": "3상",
            "vector_group": "Yyn0", "cooling_type": "ONAF",
            "fan_supply_voltage": ["400 V", "440 V"],
            "tap_changer_type": "무전압 탭절환기", "installation_type": "OUTDOOR",
        },
        "spec_order": ("vector_group", "phases", "frequency", "rated_power",
                       "hv_voltage", "lv_voltage", "tap_changer_type",
                       "installation_type", "fan_supply_voltage", "cooling_type"),
        "handoff_order": ("cooling_type", "fan_supply_voltage", "installation_type",
                          "tap_changer_type", "rated_power", "hv_voltage",
                          "lv_voltage", "frequency", "phases", "vector_group"),
    },
    {
        "case_id": "F05",
        "customers": {"customer_spec": "한국전력공사", "amendment": "KEPCO",
                      "handoff": "SK하이닉스"},
        "spec": {
            "rated_power": "45 MVA", "hv_voltage": "161 kV",
            "lv_voltage": "34.5 kV", "frequency": "60 Hz", "phases": "3상",
            "vector_group": "Yd11", "cooling_type": "OFAF",
            "fan_supply_voltage": "208 V", "tap_changer_type": "OLTC",
            "installation_type": "옥내형",
        },
        "changes": {"rated_power": "50 MVA"},
        "handoff": {
            "rated_power": "50000 kVA", "hv_voltage": "161000 V",
            "lv_voltage": "34500 V", "frequency": "60 Hz", "phases": "3상",
            "vector_group": "Yd11", "cooling_type": "OFAF",
            "fan_supply_voltage": "208 V",
            "tap_changer_type": "부하시 탭절환기", "installation_type": "INDOOR",
        },
        "spec_order": ("rated_power", "hv_voltage", "lv_voltage",
                       "cooling_type", "fan_supply_voltage", "frequency",
                       "phases", "vector_group", "tap_changer_type", "installation_type"),
        "handoff_order": ("tap_changer_type", "installation_type", "phases",
                          "frequency", "vector_group", "rated_power", "hv_voltage",
                          "lv_voltage", "cooling_type", "fan_supply_voltage"),
    },
    {
        "case_id": "F06",
        "customers": {"customer_spec": "NGET", "amendment": "NGET", "handoff": "NGET"},
        "spec": {
            "rated_power": "22.5 MVA", "hv_voltage": "115 kV",
            "lv_voltage": "11 kV", "frequency": "50 Hz", "phases": "3상",
            "vector_group": "YNyn0", "cooling_type": "ONAF",
            "fan_supply_voltage": "240 V", "tap_changer_type": "DETC",
            "installation_type": "옥내형",
        },
        "changes": {"installation_type": "옥외형"},
        "handoff": {
            "rated_power": "22500 kVA", "hv_voltage": "115000 V",
            "lv_voltage": "11000 V", "frequency": "50 Hz", "phases": "3상",
            "vector_group": "YNyn0", "cooling_type": "ONAF",
            "fan_supply_voltage": "240 V",
            "tap_changer_type": "무전압 탭절환기", "installation_type": "OUTDOOR",
        },
        "spec_order": ("frequency", "phases", "vector_group", "installation_type",
                       "tap_changer_type", "rated_power", "hv_voltage",
                       "lv_voltage", "cooling_type", "fan_supply_voltage"),
        "handoff_order": ("fan_supply_voltage", "cooling_type", "lv_voltage",
                          "hv_voltage", "rated_power", "vector_group", "phases",
                          "frequency", "installation_type", "tap_changer_type"),
    },
]

# Every normalized value is specified by the author, not obtained from an engine.
# The tuple order is KEYS. Requirement values already apply the single amendment.
MANUAL_REQUIREMENT = {
    "F01": ("37500", "66000", "6900", "60", "3", "Dyn11", "ONAF", "240", "DETC", "옥외형"),
    "F02": ("18750", "132000", "6600", "50", "3", "Dyn1", "ONAN", "415", "OLTC", "옥내형"),
    "F03": ("27000", "77000", "13800", "60", "3", "YNd11", "OFAF", "460", "OLTC", "옥내형"),
    "F04": ("31500", "154000", "22900", "60", "3", "Yyn0", "ONAF", "400", "DETC", "옥외형"),
    "F05": ("50000", "161000", "34500", "60", "3", "Yd11", "OFAF", "208", "OLTC", "옥내형"),
    "F06": ("22500", "115000", "11000", "50", "3", "YNyn0", "ONAF", "240", "DETC", "옥외형"),
}
MANUAL_HANDOFF = {
    "F01": ("37500", "66000", "6900", "60", "3", "Dyn11", "ONAF", "240", "DETC", "옥외형"),
    "F02": ("18750", "110000", "6600", "50", "3", "Dyn1", "ONAN", "415", "DETC", "옥내형"),
    "F03": ("27000", "77000", "13800", "60", "3", "YNd11", "OFAF", None, "OLTC", None),
    "F04": ("31500", "154000", "22900", "60", "3", "Yyn0", "ONAF", ("400", "440"), "DETC", "옥외형"),
    "F05": ("50000", "161000", "34500", "60", "3", "Yd11", "OFAF", "208", "OLTC", "옥내형"),
    "F06": ("22500", "115000", "11000", "50", "3", "YNyn0", "ONAF", "240", "DETC", "옥외형"),
}
MANUAL_STATUSES = {
    "F01": ("MATCH",) * 10,
    "F02": ("MATCH", "MISMATCH", "MATCH", "MATCH", "MATCH", "MATCH",
            "MATCH", "MATCH", "MISMATCH", "MATCH"),
    "F03": ("MATCH", "MATCH", "MATCH", "MATCH", "MATCH", "MATCH",
            "MATCH", "NEEDS_REVIEW", "MATCH", "NEEDS_REVIEW"),
    "F04": ("MATCH", "MATCH", "MATCH", "MATCH", "MATCH", "MATCH",
            "MATCH", "NEEDS_REVIEW", "MATCH", "MATCH"),
    "F05": ("NEEDS_REVIEW",) * 10,
    "F06": ("NEEDS_REVIEW",) * 10,
}
CUSTOMER_STATUS = {
    "F01": "MATCH", "F02": "MATCH", "F03": "MATCH", "F04": "MATCH",
    "F05": "MISMATCH", "F06": "NEEDS_REVIEW",
}


def styles(font_path: Path) -> dict:
    pdfmetrics.registerFont(TTFont(FONT, str(font_path)))
    return {
        "title": ParagraphStyle("FreshTitle", fontName=FONT, fontSize=23,
                                leading=31, textColor=NAVY, spaceAfter=8),
        "deck": ParagraphStyle("FreshDeck", fontName=FONT, fontSize=10,
                               leading=16, textColor=GRAY, spaceAfter=13, wordWrap="CJK"),
        "meta": ParagraphStyle("FreshMeta", fontName=FONT, fontSize=9,
                               leading=14, textColor=NAVY, spaceAfter=3, wordWrap="CJK"),
        "section": ParagraphStyle("FreshSection", fontName=FONT, fontSize=12,
                                  leading=18, textColor=TEAL, spaceBefore=15,
                                  spaceAfter=7, wordWrap="CJK"),
        "body": ParagraphStyle("FreshBody", fontName=FONT, fontSize=10.5,
                               leading=17, textColor=NAVY, spaceAfter=8, wordWrap="CJK"),
        "cell": ParagraphStyle("FreshCell", fontName=FONT, fontSize=10,
                               leading=15.5, textColor=NAVY, alignment=TA_LEFT,
                               wordWrap="CJK"),
        "num": ParagraphStyle("FreshNum", fontName=FONT, fontSize=9,
                              leading=15.5, textColor=GRAY),
        "thead": ParagraphStyle("FreshTH", fontName=FONT, fontSize=9,
                                leading=14, textColor=colors.white),
        "notice": ParagraphStyle("FreshNotice", fontName=FONT, fontSize=8.3,
                                 leading=13, textColor=GRAY, spaceBefore=11, wordWrap="CJK"),
    }


def p(text: object, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(str(text)).replace("\n", "<br/>"), style)


def line(field: str, value: str) -> str:
    return f"{LABELS[field]}: {value}"


def expanded_lines(values: dict, order: tuple | list) -> list[str]:
    result = []
    for key in order:
        value = values[key]
        for item in value if isinstance(value, list) else [value]:
            result.append(line(key, item))
    return result


def content_table(lines: list[str], sheet: dict) -> Table:
    rows = [[p("순번", sheet["thead"]), p("명시된 사양", sheet["thead"])]]
    rows.extend([p(f"{i:02d}", sheet["num"]), p(text, sheet["cell"])]
                for i, text in enumerate(lines, 1))
    table = Table(rows, colWidths=[47, A4[0] - 127], hAlign="LEFT", repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PALE]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 11),
        ("RIGHTPADDING", (0, 0), (-1, -1), 11),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LINEBELOW", (0, 0), (-1, 0), 1, TEAL),
        ("LINEBELOW", (0, 1), (-1, -1), 0.3, LINE),
    ]))
    return table


def render_document(case: dict, role: str, sheet: dict) -> Path:
    cid = case["case_id"]
    doc_id = f"{ROLE_PREFIX[role]}-{cid}"
    path = OUT / cid / f"{doc_id}.pdf"
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing PDF: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    supersedes = f"SPEC-{cid}" if role == "amendment" else "없음"
    story = [
        p(ROLE_NAMES[role], sheet["title"]),
        p(f"FRESH-{cid} / 변압기 문서 검토용 가상 프로젝트", sheet["deck"]),
        p(f"문서 ID {doc_id} | 프로젝트 FRESH-{cid}", sheet["meta"]),
        p(f"문서 역할 {role} | 데모 상태 approved", sheet["meta"]),
        p(f"승인 표기 DEMO-REVIEWER | 대체 대상 {supersedes}", sheet["meta"]),
        p(f"고객사(발주처) {case['customers'][role]}", sheet["meta"]),
    ]
    if role == "amendment":
        story.extend([
            p("변경 통지 항목", sheet["section"]),
            p(f"본 문서는 SPEC-{cid}의 아래 항목을 대체합니다. 기재하지 않은 "
              "항목은 해당 사양서의 요구사항을 유지합니다.", sheet["body"]),
            Spacer(1, 5),
        ])
        for index, (key, value) in enumerate(case["changes"].items(), 1):
            story.append(p(f"{index:02d} / 변경 요구사항", sheet["section"]))
            story.append(content_table([line(key, value)], sheet))
        story.extend([
            p("적용 문서", sheet["section"]),
            p(f"기준 문서: SPEC-{cid}\n대상 프로젝트: FRESH-{cid}", sheet["body"]),
        ])
    else:
        values = case["spec" if role == "customer_spec" else "handoff"]
        order = case["spec_order" if role == "customer_spec" else "handoff_order"]
        lines = expanded_lines(values, order)
        section = "기본 요구사항" if role == "customer_spec" else "설계 인계 내용"
        story.append(p(section, sheet["section"]))
        # Alternating layouts vary field placement without changing the contract.
        if role == "customer_spec" and cid in {"F02", "F04", "F06"}:
            story.append(content_table(lines[:5], sheet))
            story.append(Spacer(1, 12))
            story.append(content_table(lines[5:], sheet))
        else:
            story.append(content_table(lines, sheet))
    story.append(p(NOTICE, sheet["notice"]))

    def decoration(canvas, doc):
        canvas.saveState()
        width, height = A4
        canvas.setFillColor(TEAL)
        canvas.rect(40, height - 29, 24, 3, fill=1, stroke=0)
        canvas.setFont(FONT, 8)
        canvas.setFillColor(GRAY)
        canvas.drawString(73, height - 29, HEADER)
        canvas.setStrokeColor(LINE)
        canvas.line(40, 39, width - 40, 39)
        canvas.setFont(FONT, 7.4)
        canvas.drawString(40, 25, FOOTER)
        canvas.drawRightString(width - 40, 25, f"{doc.page:02d}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        str(path), pagesize=A4, leftMargin=40, rightMargin=40,
        topMargin=56, bottomMargin=55, title=f"{doc_id} | {ROLE_NAMES[role]}",
        author="Synthetic Evaluation Corpus",
    )
    doc.build(story, onFirstPage=decoration, onLaterPages=decoration)
    return path


def value_obj(value: str, key: str) -> dict:
    return {"value": value, "unit": UNITS[key]}


def evidence(doc_id: str, quote: str) -> dict:
    return {"document_id": doc_id, "page": 1, "quote": quote}


def expected_case(case: dict) -> dict:
    cid = case["case_id"]
    fields = {}
    for index, key in enumerate(KEYS):
        changed = key in case["changes"]
        source_id = f"{'AMD' if changed else 'SPEC'}-{cid}"
        raw_requirement = case["changes"][key] if changed else case["spec"][key]
        row_evidence = [evidence(source_id, line(key, raw_requirement))]
        raw_handoff = case["handoff"].get(key)
        if raw_handoff is not None:
            for raw in raw_handoff if isinstance(raw_handoff, list) else [raw_handoff]:
                row_evidence.append(evidence(f"HANDOFF-{cid}", line(key, raw)))

        status = MANUAL_STATUSES[cid][index]
        required = MANUAL_REQUIREMENT[cid][index]
        handed = MANUAL_HANDOFF[cid][index]
        if cid in {"F05", "F06"}:
            for role in ("customer_spec", "amendment", "handoff"):
                row_evidence.append(evidence(
                    f"{ROLE_PREFIX[role]}-{cid}",
                    f"고객사(발주처) {case['customers'][role]}",
                ))
            if cid == "F05":
                rationale = (
                    "사양서·변경 통지의 한국전력공사/KEPCO와 인계서의 SK하이닉스는 "
                    "서로 다른 고객이다. 기술값이 같아도 고객 식별 관문에서 전체 항목을 보류한다."
                )
            else:
                rationale = (
                    "세 문서의 NGET는 모두 같지만 허용된 고객사 별칭이 아니다. "
                    "National Grid로 추정하지 않고 고객 식별 관문에서 전체 항목을 보류한다."
                )
        elif cid == "F02" and key == "hv_voltage":
            rationale = "변경 통지가 요구 전압을 132000 V로 대체했으나 인계서는 110000 V를 명시한다."
        elif cid == "F02" and key == "tap_changer_type":
            rationale = "변경 통지는 OLTC를 요구하지만 인계서의 무전압 탭절환기는 DETC이므로 불일치한다."
        elif cid == "F03" and key == "fan_supply_voltage":
            rationale = (
                "사양서의 냉각팬 전원전압은 460 V이나 인계서 1페이지에 해당 항목과 값이 없다. "
                "텍스트 AI 경로에서는 이를 NEEDS_REVIEW로 판정하며 MISSING으로 분류하지 않는다."
            )
        elif cid == "F03" and key == "installation_type":
            rationale = (
                "요구사항은 INDOOR이나 인계서가 설치 구분을 미확정으로 명시해 "
                "지원 범주의 확정값을 선택할 수 없다."
            )
        elif cid == "F04" and key == "fan_supply_voltage":
            rationale = (
                "인계서에 동일한 냉각팬 전원전압 항목이 400 V와 440 V로 각각 적혀 있다. "
                "두 리터럴 중 하나를 선택할 근거가 없으므로 이 항목만 보류한다."
            )
        elif changed:
            rationale = (
                f"승인 표기가 있는 AMD-{cid}의 대체 요구사항과 인계값을 "
                f"{UNITS[key] or '지원 범주'} 기준으로 정규화하면 같다."
            )
        elif key in {"rated_power", "hv_voltage", "lv_voltage"}:
            rationale = f"사양서와 인계서의 단위 표기를 {UNITS[key]}로 환산하면 같은 값이다."
        elif key in {"tap_changer_type", "installation_type"}:
            rationale = "사양서와 인계서의 한국어·영문 표기는 같은 지원 범주에 해당한다."
        else:
            rationale = "사양서의 명시값과 인계서의 명시값이 같은 요구사항을 나타낸다."

        field = {
            "status": status,
            "expected": value_obj(required, key) if status in {"MATCH", "MISMATCH"} else None,
            "actual": value_obj(handed, key) if status in {"MATCH", "MISMATCH"} else None,
            "rationale": rationale,
            "evidence": row_evidence,
        }
        if status == "NEEDS_REVIEW":
            # These annotations preserve manually authored technical expectations
            # while null expected/actual explicitly exclude value-scoring on holds.
            field["authored_requirement"] = value_obj(required, key)
            if isinstance(handed, tuple):
                field["authored_handoff_candidates"] = [value_obj(v, key) for v in handed]
            else:
                field["authored_handoff"] = value_obj(handed, key) if handed is not None else None
        if cid == "F03" and key == "fan_supply_voltage":
            field["missing_reason"] = "HANDOFF-F03 1페이지 전체에 냉각팬 전원전압 항목 및 값이 없다."
        if cid == "F03" and key == "installation_type":
            field["unresolved_reason"] = "HANDOFF-F03 1페이지에 '설치 구분: 미확정'이라고 적혀 있다."
        fields[key] = field
    return {"case_id": cid, "customer_status": CUSTOMER_STATUS[cid], "fields": fields}


def write_json(path: Path, data: dict) -> None:
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {path}")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_pdf(path: Path, case: dict, role: str) -> tuple[str, dict]:
    reader = PdfReader(path)
    if len(reader.pages) != 1:
        raise AssertionError(f"{path.name}: expected exactly one page, got {len(reader.pages)}")
    text = reader.pages[0].extract_text()
    cid = case["case_id"]
    doc_id = f"{ROLE_PREFIX[role]}-{cid}"
    supersedes = f"SPEC-{cid}" if role == "amendment" else "없음"
    must_exist = [
        HEADER, FOOTER,
        f"문서 ID {doc_id} | 프로젝트 FRESH-{cid}",
        f"문서 역할 {role} | 데모 상태 approved",
        f"승인 표기 DEMO-REVIEWER | 대체 대상 {supersedes}",
        f"고객사(발주처) {case['customers'][role]}",
    ]
    values = case["changes"] if role == "amendment" else case["spec" if role == "customer_spec" else "handoff"]
    must_exist.extend(expanded_lines(values, list(values)))
    for snippet in must_exist:
        if snippet not in text:
            raise AssertionError(f"{path.name}: text missing {snippet!r}")
    # Metadata approved is contractually required; no outcome labels are printed.
    forbidden = ("NEEDS_REVIEW", "MISMATCH", "MISSING", "MATCH", "정답", "기대값",
                 "판정 결과", "예상 결과", "expected_status", "expected_value")
    for snippet in forbidden:
        if snippet in text:
            raise AssertionError(f"{path.name}: answer label leaked {snippet!r}")
    if cid == "F03" and role == "handoff" and LABELS["fan_supply_voltage"] in text:
        raise AssertionError("F03 handoff unexpectedly contains intentionally absent field")
    if cid == "F04" and role == "handoff":
        if text.count("냉각팬 전원전압:") != 2:
            raise AssertionError("F04 handoff must contain exactly two literal fan-voltage lines")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return text, {
        "document_id": doc_id, "page_count": 1, "sha256": digest,
        "metadata_present": True, "required_field_text_present": True,
        "answer_label_leak": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--font", type=Path, default=Path("/Library/Fonts/Arial Unicode.ttf"))
    parser.add_argument("--pdftoppm", type=Path, required=True)
    parser.add_argument("--authoring-marker-confirmed", action="store_true")
    args = parser.parse_args()
    if not args.authoring_marker_confirmed:
        parser.error("Root must run the PDF skill authoring marker exactly once before this generator.")
    if OUT.exists() and any(OUT.iterdir()):
        raise FileExistsError("Fresh evaluation output is nonempty; historical artifacts will not be overwritten.")
    if not args.font.is_file() or not args.pdftoppm.is_file():
        raise FileNotFoundError("Font and pdftoppm must both be available.")
    OUT.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)
    sheet = styles(args.font)
    manifest_cases = []
    text_by_id = {}
    checks = []
    for case in CASES:
        cid = case["case_id"]
        documents = []
        for role in ("customer_spec", "amendment", "handoff"):
            path = render_document(case, role, sheet)
            text, check = validate_pdf(path, case, role)
            text_by_id[check["document_id"]] = text
            checks.append(check)
            documents.append({
                "document_id": check["document_id"], "role": role,
                "path": str(path.relative_to(ROOT)), "page_count": 1,
            })
            (TMP / f"{check['document_id']}.txt").write_text(text, encoding="utf-8")
            subprocess.run([
                str(args.pdftoppm), "-scale-to", "1400", "-png", "-singlefile",
                str(path), str(TMP / check["document_id"]),
            ], check=True, capture_output=True, text=True)
        manifest_cases.append({
            "case_id": cid, "documents": documents,
            "review_policy": {
                "base_id": f"SPEC-{cid}", "handoff_id": f"HANDOFF-{cid}",
                "approved_amendment_ids": [f"AMD-{cid}"],
            },
        })
    expectations = {
        "schema_version": "fresh-evaluation-v1",
        "evaluation_scope": (
            "Newly authored synthetic examples for a prototype trial. The expectations are "
            "manually specified from the public contract and frozen before model execution. "
            "This is neither an independent real-world evaluation nor a production holdout."
        ),
        "do_not_send_to_model": True,
        "value_scoring_policy": (
            "NEEDS_REVIEW rows deliberately have expected=null and actual=null. "
            "Their authored_* annotations preserve the author's technical interpretation "
            "but are excluded from confirmed-value agreement scoring."
        ),
        "cases": [expected_case(case) for case in CASES],
    }
    for case in expectations["cases"]:
        assert set(case["fields"]) == set(KEYS)
        for key, field in case["fields"].items():
            for item in field["evidence"]:
                if item["quote"] not in text_by_id[item["document_id"]]:
                    raise AssertionError(f"Evidence quote not found: {case['case_id']}/{key}/{item}")
    write_json(OUT / "input-manifest.json", {
        "schema_version": "fresh-evaluation-v1", "cases": manifest_cases,
    })
    write_json(OUT / "expected-results.json", expectations)
    write_json(TMP / "authoring-validation.json", {
        "document_count": len(checks), "page_count": sum(x["page_count"] for x in checks),
        "all_metadata_present": True, "all_required_field_text_present": True,
        "all_evidence_quotes_verified": True, "answer_label_leak": False,
        "documents": checks, "visual_review": "pending human-agent inspection of all rendered pages",
    })
    print(json.dumps({
        "input_manifest": str(OUT / "input-manifest.json"),
        "expected_results": str(OUT / "expected-results.json"),
        "documents": len(checks), "pages": 18, "renders": str(TMP),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
