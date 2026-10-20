"""Build nine synthetic, varied-layout text PDFs without fixture-answer inputs.

Run with the bundled Python containing ReportLab. This script does not call a
model or the reviewer. Input metadata/paths are recorded in manifest.json;
manually specified review labels live separately in expected-results.json.
These are authored development examples, not independent evaluation data.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from reportlab.platypus import PageBreak, Spacer

from build_day1_artifacts import CUSTOMER_ALIASES, CUSTOMER_NOTICE, configure_styles, grid, paragraph, render

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "pdf" / "varied"
FILES = {"customer_spec": "01_customer_spec.pdf", "amendment": "02_amendment.pdf", "handoff": "03_handoff.pdf"}
KEYS = ("rated_power", "hv_voltage", "lv_voltage", "frequency", "phases",
        "vector_group", "cooling_type", "fan_supply_voltage",
        "tap_changer_type", "installation_type")

# Only one value is present per field sentence. No machine-oriented field keys
# or "가상 요구사항 ..." row markers are emitted in any PDF body.
CONTENT = {
    "V01": {
        "customer_spec": [
            ["정격용량: 1000 kVA", "고압측정격전압: 22 kV", "저압측정격전압: 400 V", "정격주파수: 60 Hz", "탭절환기 방식: DETC"],
            ["상수: 3상", "결선그룹: Dyn11", "냉각방식: ONAF", "냉각팬전원: 380 V", "설치 구분: 옥내형"],
        ],
        "amendment": [["냉각팬전원: 440 V"]],
        "handoff": [
            ["정격용량: 1000 kVA", "고압측정격전압: 22 kV", "저압측정격전압: 400 V", "정격주파수: 60 Hz", "탭절환기 방식: DETC"],
            ["상수: 3상", "결선그룹: Dyn11", "냉각방식: ONAF", "냉각팬전원: 380 V", "설치 구분: 옥내형"],
        ],
    },
    "V02": {
        "customer_spec": [
            ["전원주파수: 60 Hz", "상 수: 3상", "정격출력: 1000 kVA", "결선기호: Dyn11", "설치 구분: 옥내형"],
            ["냉각 방식: ONAF", "팬보조전원: 380 V", "2차정격전압: 400 V", "1차정격전압: 22000 V", "탭절환기 방식: DETC"],
        ],
        "amendment": [["팬보조전원: 440 V"]],
        "handoff": [
            ["팬보조전원: 440 V", "냉각 방식: ONAF", "결선기호: Dyn11", "상 수: 3상", "탭절환기 방식: 무전압 탭절환기"],
            ["정격출력: 1 MVA", "2차정격전압: 0.4 kV", "1차정격전압: 22 kV", "전원주파수: 미기재", "설치 구분: INDOOR"],
        ],
    },
    "V03": {
        "customer_spec": [
            ["정격용량: 1000 kVA", "고압측정격전압: 22 kV", "저압측정격전압: 400 V", "정격주파수: 60 Hz", "탭절환기 방식: DETC"],
            ["상수: 3상", "결선그룹: Dyn11", "냉각방식: ONAF", "냉각팬전원: 380 V", "설치 구분: 옥내형"],
        ],
        "amendment": [["탭절환기 방식: OLTC", "설치 구분: 옥외형"]],
        "handoff": [
            ["정격용량: 1000 kVA", "고압측정격전압: 22 kV", "저압측정격전압: 400 V", "정격주파수: 60 Hz", "탭절환기 방식: DETC"],
            ["상수: 3상", "결선그룹: Dyn11", "냉각방식: ONAF", "냉각팬전원: 380 V", "설치 구분: 옥내형"],
        ],
    },
}


def metadata(case_id: str, role: str) -> dict:
    prefixes = {"customer_spec": "SPEC", "amendment": "AMD", "handoff": "HANDOFF"}
    return {
        "id": f"{prefixes[role]}-{case_id}",
        "role": role,
        "project_id": f"DEMO-{case_id}",
        "status": "approved",
        "approved_by": "DEMO-REVIEWER",
        "supersedes": f"SPEC-{case_id}" if role == "amendment" else None,
        "customer_name": CUSTOMER_ALIASES[role],
    }


def title_and_meta(case_id: str, role: str, title: str, styles: dict) -> list:
    doc = metadata(case_id, role)
    return [
        paragraph(title, styles["title"]),
        paragraph(f"문서 ID {doc['id']} | 프로젝트 {doc['project_id']}", styles["small"]),
        paragraph(f"문서 역할 {role} | 데모 상태 approved", styles["small"]),
        paragraph(f"승인 표기 DEMO-REVIEWER | 대체 대상 {doc['supersedes'] or '없음'}", styles["small"]),
        paragraph(f"고객사(발주처) {doc['customer_name']}", styles["small"]),
        paragraph(CUSTOMER_NOTICE, styles["small"]),
        Spacer(1, 12),
    ]


def specification(case_id: str, pages: list[list[str]], styles: dict) -> list:
    story = title_and_meta(case_id, "customer_spec", "고객 규격 요약", styles)
    story += [
        paragraph("검토용 규격표", styles["h2"]),
        paragraph("수주 후 기술 인계를 위한 가상 요구조건입니다. 아래 수치는 교육용 문서 비교 데이터이며 실제 설비의 설계 기준이 아닙니다.", styles["body"]),
        grid([["구분", "기재 요구조건"]] + [[f"A-{index}", line] for index, line in enumerate(pages[0], 1)], [70, 453], styles),
        Spacer(1, 15),
        paragraph("적용 범위", styles["h2"]),
        paragraph("이 문서는 하나의 가상 제품에 대한 요구조건입니다. 문서의 승인 표기와 대체 관계는 실습용 메타데이터입니다.", styles["body"]),
        PageBreak(),
        paragraph("고객 규격 요약", styles["title"]),
        paragraph("구성 및 보조설비 조건", styles["h2"]),
        grid([["구분", "기재 요구조건"]] + [[f"B-{index}", line] for index, line in enumerate(pages[1], 1)], [70, 453], styles),
        Spacer(1, 18),
        paragraph("기술 인계 시 확인사항", styles["h2"]),
        paragraph("변경 합의서가 있는 경우 담당자가 적용 대상과 대체 관계를 확인합니다. 합의서에 없는 요구조건은 이 규격표를 함께 확인합니다.", styles["body"]),
    ]
    return story


def amendment(case_id: str, pages: list[list[str]], styles: dict) -> list:
    story = title_and_meta(case_id, "amendment", "기술조건 변경 서신", styles)
    story += [
        paragraph("수신: 가상 기술 인계 담당자", styles["body"]),
        paragraph("제목: 기술 조건 변경 합의", styles["h2"]),
        paragraph("가상 고객 규격표의 기재 조건을 검토했습니다. 아래에 명시한 조건을 이번 기술 인계의 변경 조건으로 합의합니다.", styles["body"]),
        Spacer(1, 14),
        grid([["합의 대상", "변경 후 조건"]] + [["합의 항목", line] for line in pages[0]], [110, 413], styles),
        Spacer(1, 14),
        paragraph("적용 범위", styles["h2"]),
        paragraph("이 서신에 명시한 항목만 대체하며 그 밖의 요구조건은 고객 규격표를 따릅니다. 인계 담당자는 변경 조건의 반영 여부를 원문과 함께 확인해 주시기 바랍니다.", styles["body"]),
        paragraph("문서의 승인 표기는 실습용 상태값입니다. 실제 승인 권한이나 전자서명을 검증한 문서가 아닙니다.", styles["small"]),
    ]
    return story


def handoff(case_id: str, pages: list[list[str]], styles: dict) -> list:
    story = title_and_meta(case_id, "handoff", "내부 기술 인계 메모", styles)
    story += [
        paragraph("담당 부서 전달사항", styles["h2"]),
        paragraph("고객 규격표와 변경 서신을 검토할 때 사용할 가상 인계 메모입니다. 아래 항목은 현재 메모에 기재된 내용이며 승인 또는 설계 적합성 판정이 아닙니다.", styles["body"]),
        Spacer(1, 8),
    ]
    for line in pages[0]:
        story += [paragraph(line, styles["body"]), Spacer(1, 8)]
    story += [
        Spacer(1, 16),
        paragraph("기록 방식", styles["h2"]),
        paragraph("요구조건의 값과 단위를 그대로 기재했습니다. 각 항목의 적용 여부는 기준 문서와 합의 내용을 함께 확인합니다.", styles["body"]),
        PageBreak(),
        paragraph("기술 인계 메모 계속", styles["title"]),
        paragraph("추가 기재 조건", styles["h2"]),
    ]
    for line in pages[1]:
        story += [paragraph(line, styles["body"]), Spacer(1, 8)]
    story += [
        Spacer(1, 16),
        paragraph("확인 절차", styles["h2"]),
        paragraph("내용을 확정하기 전에 고객 규격표와 합의서의 원문을 대조합니다. 기재되지 않았거나 해석이 어려운 조건은 임의로 채우지 않고 담당자에게 확인합니다.", styles["body"]),
    ]
    return story


def build(styles: dict) -> dict:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    manifests = []
    builders = {"customer_spec": specification, "amendment": amendment, "handoff": handoff}
    for case_id, case in CONTENT.items():
        documents = []
        for role, pages in case.items():
            target = OUTPUT / case_id / FILES[role]
            render(target, builders[role](case_id, pages, styles), "HANDOFF REVIEW / SYNTHETIC DOCUMENT")
            doc = metadata(case_id, role)
            documents.append({"document_id": doc["id"], "role": role, "customer_name": doc["customer_name"],
                              "path": str(target.relative_to(ROOT)), "page_count": len(pages)})
        manifests.append({
            "case_id": case_id,
            "documents": documents,
            "review_policy": {"base_id": f"SPEC-{case_id}", "approved_amendment_ids": [f"AMD-{case_id}"], "handoff_id": f"HANDOFF-{case_id}"},
        })
        # Announce a completed set so inference can begin without waiting for
        # rendering/visual QA. This is not a claim that visual QA has finished.
        print(json.dumps({"generated_case": case_id, "paths": [doc["path"] for doc in documents]}, ensure_ascii=False), flush=True)

    manifest = {
        "synthetic_only": True,
        "schema_version": "varied-text-pdf-3",
        "notice": "가상 선언과 명시 메타데이터가 있는 다양한 본문 양식입니다. 실제 기업 자료가 아니며 일반 PDF/OCR 지원을 뜻하지 않습니다. " + CUSTOMER_NOTICE,
        "cases": manifests,
    }
    # These labels are authored expectations, not calculated by review_case.
    # In particular, explicit '미기재' is conservatively held by the AI extraction
    # gate; it is not automatically equated to a verified missing source field.
    answers = {
        "synthetic_only": True,
        "evaluation_scope": "authored_development_examples_not_independent_holdout",
        "do_not_send_to_model": True,
        "cases": [
            {"case_id": "V01", "expected": {key: "MISMATCH" if key == "fan_supply_voltage" else "MATCH" for key in KEYS},
             "review_note": "고객 팬 전원 380 V는 승인 서신의 440 V로 대체됐지만 인계 메모는 380 V입니다."},
            {"case_id": "V02", "expected": {key: "NEEDS_REVIEW" if key == "frequency" else "MATCH" for key in KEYS},
             "review_note": "팬 변경 반영, 단위 동등성, DETC/무전압 탭절환기 및 옥내형/INDOOR 동등성을 확인합니다. 인계 메모의 명시 미기재 항목은 AI 추출 게이트에서 보류합니다."},
            {"case_id": "V03", "expected": {key: "MISMATCH" if key in {"tap_changer_type", "installation_type"} else "MATCH" for key in KEYS},
             "review_note": "승인 서신은 탭절환기 방식을 OLTC, 설치 구분을 옥외형으로 변경했지만 인계 메모에는 DETC와 옥내형이 남아 있습니다. 실제 공학설계 적합성을 판정하는 사례가 아닙니다."},
        ],
    }
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUTPUT / "expected-results.json").write_text(json.dumps(answers, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path, default=Path("/System/Library/Fonts/Supplemental/AppleGothic.ttf"))
    args = parser.parse_args()
    build(configure_styles(args.font))


if __name__ == "__main__":
    main()
