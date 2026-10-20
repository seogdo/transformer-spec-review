"""Build the public project report from confirmed validation results.

This is a documentation utility, not the app's meeting-report export. Pass the
observed test counts explicitly; it never derives accuracy from unit tests or
fabricates screenshots. A Korean TrueType font must be supplied by the caller.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import textwrap
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image, PageBreak, Paragraph, Preformatted, SimpleDocTemplate, Spacer, Table, TableStyle,
)

ROOT = Path(__file__).resolve().parents[1]
NAVY = colors.HexColor("#173C46")
TEAL = colors.HexColor("#16796E")
PALE = colors.HexColor("#EEF6F4")
GRAY = colors.HexColor("#57676B")
LINE = colors.HexColor("#D9E3E4")
FONT = "ReportKorean"
WIDTH = A4[0] - 88


def build(args: argparse.Namespace) -> None:
    pdfmetrics.registerFont(TTFont(FONT, str(args.font)))
    styles = {
        "title": ParagraphStyle("title", fontName=FONT, fontSize=24, leading=33,
                                textColor=NAVY, spaceAfter=14, wordWrap="CJK"),
        "h2": ParagraphStyle("h2", fontName=FONT, fontSize=13.5, leading=20,
                             textColor=TEAL, spaceBefore=17, spaceAfter=8),
        "body": ParagraphStyle("body", fontName=FONT, fontSize=10.3, leading=17,
                               textColor=NAVY, spaceAfter=9, wordWrap="CJK"),
        "small": ParagraphStyle("small", fontName=FONT, fontSize=8.4, leading=13,
                                textColor=GRAY, spaceAfter=7, wordWrap="CJK"),
        "cell": ParagraphStyle("cell", fontName=FONT, fontSize=9.1, leading=14,
                               textColor=NAVY, wordWrap="CJK"),
        "th": ParagraphStyle("th", fontName=FONT, fontSize=9.1, leading=14,
                             textColor=colors.white, wordWrap="CJK"),
    }

    def p(text: str, style: str = "body") -> Paragraph:
        return Paragraph(escape(text).replace("\n", "<br/>"), styles[style])

    def grid(rows: list[list[str]], widths: list[float]) -> Table:
        table = Table(
            [[p(c, "th" if r == 0 else "cell") for c in row]
             for r, row in enumerate(rows)],
            colWidths=widths, repeatRows=1, hAlign="LEFT",
        )
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [PALE, colors.white]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 9),
            ("RIGHTPADDING", (0, 0), (-1, -1), 9),
            ("TOPPADDING", (0, 0), (-1, -1), 9),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
        ]))
        return table

    def screenshot(path: Path, max_height: float) -> Image:
        image = Image(str(path))
        scale = min(WIDTH / image.imageWidth, max_height / image.imageHeight)
        image.drawWidth = image.imageWidth * scale
        image.drawHeight = image.imageHeight * scale
        image.hAlign = "CENTER"
        return image

    def code_excerpt(relative_path: str, first: int, last: int) -> Table:
        """Print the checked-in source itself, not rewritten pseudocode."""
        lines = (ROOT / relative_path).read_text(encoding="utf-8").splitlines()
        snippet = textwrap.dedent("\n".join(lines[first - 1:last]))
        if not snippet:
            raise ValueError(f"Empty source excerpt: {relative_path}:{first}-{last}")
        # Fit the longest original line without truncating a literal or adding
        # visually ambiguous code wrapping. Korean exception messages remain.
        available = WIDTH - 22
        longest = max(pdfmetrics.stringWidth(line, FONT, 1) for line in snippet.splitlines())
        size = min(8.0, available / longest) if longest else 8.0
        if size < 6.6:
            raise ValueError(f"Source excerpt is too wide to read: {relative_path}")
        style = ParagraphStyle("code", fontName=FONT, fontSize=size,
                               leading=max(size * 1.52, 11.0), textColor=NAVY)
        code = Table([[Preformatted(snippet, style)]], colWidths=[WIDTH], hAlign="LEFT")
        code.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), PALE),
            ("BOX", (0, 0), (-1, -1), 0.5, LINE),
            ("LEFTPADDING", (0, 0), (-1, -1), 11),
            ("RIGHTPADDING", (0, 0), (-1, -1), 11),
            ("TOPPADDING", (0, 0), (-1, -1), 11),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 11),
        ]))
        return code

    story = [
        p("AI 서비스 Mini Project  |  선택 과제 2  |  최종 보고서", "small"),
        p("변압기 수주\n기술사양 검토 AI", "title"),
        p("수주 확정 후 기술 인계회의 전에,\n변경된 요구사항과 내부 인계표의 차이를 확인합니다."),
        p("프로젝트 요약", "h2"),
        p("고객 사양서, 기술 변경 합의서, 내부 수주 인계표를 함께 검토하는 "
          "로컬 웹 프로토타입입니다. AI가 근거와 값을 제안하고 코드가 문서 간 "
          "차이를 비교합니다. 담당자는 연결된 원문을 확인한 뒤 판단합니다."),
        grid([
            ["구분", "구현 내용"],
            ["사용 시점", "수주 확정 후, 상세 설계·구매·제작으로 기술 정보를 넘기기 전입니다."],
            ["해결할 문제", "여러 문서에 흩어진 요구사항과 승인 변경이 인계표에 누락되거나 다르게 반영되는 상황입니다."],
            ["제공 결과", "항목별 일치·불일치·누락·확인 필요, 양쪽 원문 근거, 저장·복원, 회의용 보고서입니다."],
            ["판단 경계", "문서 정합성을 보조합니다. 설계 적합성·제품 안전·실제 승인 및 발주를 결정하지 않습니다."],
        ], [82, WIDTH - 82]),
        p("검토하는 기술사양 10개", "h2"),
        p("정격용량 · 고압측 전압 · 저압측 전압 · 주파수 · 상수 · 결선 그룹 · "
          "냉각 방식 · 팬 보조전원 전압 · 탭절환기 방식 · 옥내형/옥외형"),
        p("제출 자료  |  2026년 10월 20일\n"
          "https://github.com/seogdo/transformer-spec-review", "small"),
        PageBreak(),
        p("01  구현 구조", "small"),
        p("AI 추출과 규칙 판정을\n분리했습니다", "title"),
        p("PDF 3종을 읽은 뒤 고객사 표기를 먼저 확인합니다. 고객사가 다르거나 "
          "불명확하면 비교를 보류합니다. 같은 고객사로 확인되면 근거 검색과 추출을 진행합니다."),
        grid([
            ["단계", "처리 주체와 역할"],
            ["1  문서 읽기", "코드가 텍스트 PDF와 지정 메타데이터를 읽고 문서·페이지·원문을 보존합니다."],
            ["2  근거 찾기", "AI가 검색 계획을 만들고, 코드가 키워드와 지정 별칭으로 관련 원문을 검색합니다."],
            ["3  값 제안", "AI가 검색된 원문에서 사양값·단위·출처를 제안합니다. 정상 추출 경로는 최대 2회 호출입니다."],
            ["4  근거 검증", "코드가 인용문·값·단위·조건·중복을 검사합니다. 모호한 후보는 확정값으로 채우지 않습니다."],
            ["5  변경 적용", "담당자가 기준 문서와 적용할 변경을 확인합니다. 코드가 단위와 변경 적용 순서를 처리합니다."],
            ["6  비교와 확인", "코드가 10개 항목을 비교합니다. 담당자가 양쪽 원문을 확인하고 기록을 보관합니다."],
        ], [88, WIDTH - 88]),
        p("기술 구성", "h2"),
        p("Python·FastAPI / HTML·CSS·JavaScript / Ollama·qwen3.5:2b / 로컬 JSON 저장소입니다. "
          "별도 외부 AI API나 API 키를 사용하지 않습니다."),
        p("검색은 키워드·지정 별칭 기반입니다. 임베딩 벡터 검색, MCP, 네이티브 도구 호출은 "
          "구현 범위에 포함하지 않았습니다. 정형 예시는 AI 없이 코드만으로 읽는 별도 경로입니다.", "small"),
        PageBreak(),
        p("02  대표 시연", "small"),
        p("440 V 요구와 380 V 인계를\n원문으로 확인합니다", "title"),
        p("V01 가상 변경 합의서에는 팬 보조전원이 440 V로 바뀌었지만, "
          "내부 인계표에는 380 V가 남아 있습니다. 실제 로컬 AI 시험에서 "
          "10개 항목 중 9개 일치와 1개 불일치가 표시됐습니다."),
        screenshot(ROOT / "output/live-browser-v01-result.png", 310),
        Spacer(1, 9),
        p("가상 PDF로 수행한 사전 실제 실행 화면입니다. 회사 문서나 실시간 운영 화면이 아닙니다.", "small"),
        p("담당자가 확인할 수 있는 정보", "h2"),
        p("요구값과 인계값, 적용된 변경, 문서명·페이지·인용문을 함께 제공합니다. "
          "원본 PDF를 열어 추출 내용과 대조할 수 있으며, 보류된 후보 근거는 확정값과 구분합니다."),
        p("오류를 다루는 방식", "h2"),
        p("파일 형식 오류와 AI 연결 실패를 안내하고, 확인되지 않은 값을 임의로 채우지 않습니다. "
          "접수 후 응답 오류가 발생해도 같은 요청으로 기존 작업을 다시 확인하도록 구성했습니다."),
        PageBreak(),
        p("03  검증 결과와 한계", "small"),
        p("동작 시험과 모델 정확도를\n구분했습니다", "title"),
        grid([
            ["시험", "확인 결과"],
            ["Python 자동 검사", f"{args.python_tests}개 통과. 처리 규칙·오류 처리·보고서의 저장 결과 보존 등을 검사했습니다."],
            ["화면 모의 검사", f"기존 근거 12개, 복구 32개, 보고서 {args.report_ui_tests}개 통과. 실제 브라우저 시험과는 별도입니다."],
            ["V01 실제 AI", "21개 추출값이 코드의 원문 대조를 통과했습니다. 항목 판정은 9개 일치·1개 불일치였습니다."],
            ["V02 실제 화면", args.v02],
            ["V03 실제 화면", args.v03],
            ["회의용 보고서", args.print_check],
        ], [100, WIDTH - 100]),
        p("결과 해석", "h2"),
        p("모든 시연 문서는 직접 구성한 가상 개발 자료입니다. 자동 검사 개수는 모델 정확도가 "
          "아니며, 이 결과를 실제 회사 문서의 정확도나 시간 절감률로 해석하지 않습니다."),
        p("현재 제한과 후속 과제", "h2"),
        p("지정 메타데이터와 가상 자료 선언이 있는 텍스트 PDF 3종을 지원합니다. "
          "파일당 5 MiB·20쪽 제한이 있으며 스캔·OCR·도면·다제품 문서는 지원하지 않습니다. "
          "고객사 별칭과 승인 상태는 데모 입력을 확인하는 수준입니다."),
        p("현업 적용에는 실제 문서 형식에 대한 독립 평가, 접근 권한과 승인 이력 연결, "
          "문서 진위 및 운영 보안 검토가 필요합니다. 공개 저장소는 로컬 앱의 소스 공유이며 "
          "인터넷 서비스 배포를 의미하지 않습니다."),
        PageBreak(),
        p("04  회의용 보고서와 실행 안내", "small"),
        p("검토 결과를\n회의 자료로 연결했습니다", "title"),
        p("결과와 원문을 사람이 확인한 뒤 회의용 보고서를 열 수 있습니다. 저장된 판정·값·근거를 "
          "그대로 표시하며 AI를 다시 호출하거나 기존 기록을 변경하지 않습니다."),
        screenshot(ROOT / "output/meeting-report-preview.png", 265),
        Spacer(1, 8),
        p("가상 검토 기록의 보고서 화면입니다. 인쇄 버튼으로 브라우저 인쇄 창을 열며, "
          "PDF 저장은 해당 브라우저에서 지원할 때 사용할 수 있습니다.", "small"),
        p("시연 순서", "h2"),
        p("앱 실행 → V01 선택 → AI 추출 → 변경 기준 확인 → 10개 항목 비교 → "
          "불일치의 양쪽 원문 확인 → 사람 검토 확인 → 회의용 보고서 열기입니다."),
        p("설치·실행·검증과 전체 소스", "h2"),
        p("https://github.com/seogdo/transformer-spec-review"),
        p("README에 실행 방법을, docs/presentation-demo.md에 발표 대본과 오류 발생 시 "
          "대체 시연 순서를 정리했습니다. 회사 문서·강의 원본·API 키·모델 가중치는 공개 자료에서 제외했습니다.", "small"),
        p("제출 시트의 E열에는 공개 저장소, F열에는 최종 보고서 공개 주소를 입력해야 합니다. "
          "파일 작성·공개와 시트 제출 완료는 별도이며, 보고서 작성 시점에는 시트 입력을 수행하지 않았습니다.", "small"),
        PageBreak(),
        p("05  실제 코드 설명  |  검색과 검증", "small"),
        p("검색 결과를 찾은 뒤\n원문과 다시 대조했습니다", "title"),
        p("입력은 문서별 페이지 텍스트와 AI가 제안한 문서 ID·항목·검색어입니다. "
          "아래는 현재 저장소에서 직접 읽은 실제 코드이며 전체 함수 중 핵심 부분입니다. "
          "가독성을 위해 공통 들여쓰기만 제거했습니다."),
        p("키워드 점수와 원문 위치를 보존하는 검색", "h2"),
        p("src/handoff_review/extraction_agent.py  311-322행", "small"),
        code_excerpt("src/handoff_review/extraction_agent.py", 311, 322),
        Spacer(1, 8),
        p("처리: 항목 별칭으로 찾은 원문 후보에 검색어 포함 점수를 부여하고, "
          "점수·페이지·원문 위치 순으로 정렬했습니다. 검색 요청별 상위 2개를 문자 예산 안에서 반환합니다.\n"
          "출력: 문서 ID, 항목, 페이지, 원문, 청크 ID입니다. 검색 실패는 정답이 없다는 뜻이 아니므로 "
          "미확정 항목을 임의 값으로 채우지 않습니다.", "small"),
        p("값과 단위가 인용문에도 존재하는지 검증", "h2"),
        p("src/handoff_review/extraction_agent.py  534-541행", "small"),
        code_excerpt("src/handoff_review/extraction_agent.py", 534, 541),
        Spacer(1, 8),
        p("입력: AI가 제안한 값·단위·인용문과 원문에서 읽은 토큰입니다. 숫자 항목은 "
          "추출 단계에서 단위를 바꿔 제안하지 못하도록 했습니다.\n"
          "처리·보류: 원문 값·단위, 인용문 안의 값·단위, 항목명과의 직접 연결 중 하나라도 "
          "확인되지 않으면 거부 이유를 반환합니다. 통과해도 조건·복수값·출처 등 후속 검사가 남습니다.", "small"),
        PageBreak(),
        p("06  실제 코드 설명  |  비교와 기술 선택", "small"),
        p("AI의 값 제안과\n최종 판정을 분리했습니다", "title"),
        p("입력은 검증된 기준값·인계값과 각각의 출처입니다. 승인 변경 적용 순서와 고객사 확인은 "
          "이 비교 전에 검사하며, 여기서는 단위·조건을 맞춘 뒤 동일성을 비교합니다."),
        p("src/handoff_review/review.py  189-200행", "small"),
        code_excerpt("src/handoff_review/review.py", 189, 200),
        Spacer(1, 8),
        p("출력: 조건이 같고 정규화값이 같으면 MATCH, 다르면 MISMATCH입니다. 출처나 조건이 "
          "불확실하면 NEEDS_REVIEW입니다. 확정 기준값은 있지만 인계값이 없는 MISSING 처리는 "
          "앞선 분기이며, 추출 실패와 실제 누락을 같은 의미로 취급하지 않습니다.", "small"),
        p("기술 선택 근거와 트레이드오프", "h2"),
        grid([
            ["선택", "선택 이유", "감수한 제한"],
            ["로컬 소형 LLM", "API 키 없이 값·근거 제안을 시험했습니다.", "실행 환경과 모델 품질에 영향을 받으며 정답을 보장하지 않습니다."],
            ["키워드·별칭 검색", "고정 10항목에서 검색 근거와 경로를 추적하기 쉽습니다.", "임베딩 의미 검색이 아니며 새로운 표현을 놓칠 수 있습니다."],
            ["코드 기반 판정", "단위·조건·변경 관계를 명시 규칙으로 비교했습니다.", "지원하지 않는 단위·조건·모호한 값은 보류합니다."],
            ["로컬 웹·JSON 저장", "설치 범위를 줄이고 검토 기록을 보존·복원했습니다.", "다중 사용자 인증·운영 DB·공개 서비스 보안은 별도 과제입니다."],
        ], [85, 190, WIDTH - 275]),
        p("선택 과제 2의 도메인·문서 처리·근거 검증·판정·화면을 구현했습니다. "
          "강의의 개념을 적용했지만 MCP·벡터 RAG·범용 PDF 이해를 구현했다고 주장하지 않습니다. "
          "2회 호출은 처리 지연을 늘릴 수 있으며, 첫 검색 계획 호출의 추가 효용은 별도 비교로 입증하지 않았습니다.", "small"),
    ]
    expected_pages = 7
    if args.before_after:
        measured = json.loads(args.before_after.read_text(encoding="utf-8"))
        if (measured.get("passed") is not True
                or measured.get("before_origin", {}).get("kind") != "historical_saved_baseline"
                or measured.get("after_origin", {}).get("kind") != "current_code_captured_response_replay"
                or measured.get("before_origin", {}).get("old_implementation_rerun") is not False
                or measured.get("after_origin", {}).get("live_model_run") is not False):
            raise ValueError("Before/After requires a confirmed archived-baseline/current-replay report.")
        comparison = measured["comparison"]
        replay = measured["replay_observations"]
        if replay["actual_model_transport_calls"] != 0:
            raise ValueError("This report section describes response replay, not a live AI run.")
        before = comparison["fan_before"]
        after = comparison["fan_after"]
        report_path = args.before_after.resolve().relative_to(ROOT).as_posix()
        story.extend([
            PageBreak(),
            p("07  Before / After  |  저장 응답 재생 비교", "small"),
            p("판정은 유지하고\n충돌 근거를 보완했습니다", "title"),
            p("F04 가상 인계표에는 팬 전원이 400 V와 440 V로 함께 기재돼 있습니다. "
              "이전 결과는 판정을 보류했지만 인계표 근거 하나만 연결했습니다. "
              "보완 후에는 충돌하는 두 원문을 함께 보여주도록 했습니다."),
            grid([
                ["동일 F04 입력의 비교 항목", "Before", "After"],
                ["비교 방식", "개발 중 보존한 이전 결과입니다.", "현재 코드에 저장된 AI 응답을 재생했습니다."],
                ["팬 항목의 인계표 근거 수",
                 f"{before['handoff_unique_source_count']}개",
                 f"{after['handoff_unique_source_count']}개"],
                ["팬 항목 evidence_complete",
                 str(before["evidence_complete"]).lower(),
                 str(after["evidence_complete"]).lower()],
                ["기술 판정 상태 보존", "저장된 10개 상태",
                 f"{comparison['status_unchanged_count']}/10개 동일"],
                ["항목별 기준값·인계값 보존", "저장된 값·단위·null",
                 f"{comparison['normalized_value_pairs_unchanged_count']}/10개 동일"],
            ], [168, (WIDTH - 168) / 2, (WIDTH - 168) / 2]),
            p("달라진 점", "h2"),
            p("보류를 해제하지 않고 누락됐던 충돌 원문을 추가했습니다. 근거가 연결됐다는 사실과 "
              "판정을 확정할 수 있다는 사실을 분리해, 보류 항목의 evidence_complete는 false로 유지했습니다."),
            p("측정 방법과 해석의 한계", "h2"),
            p(f"현재 재생에서 기록된 모델 응답은 {replay['captured_response_replay_calls']}회 사용했고 "
              "실제 모델 통신은 0회였습니다. 변경 전 코드를 다시 실행한 동시 실험이 아니라, "
              "이전 저장 기준과 현재 코드 재생 결과를 대조한 개발 회귀 비교입니다."),
            p("같은 문서·같은 기록된 모델 응답·같은 항목 지표로 후처리의 차이를 확인했습니다. "
              "보류가 늘거나 줄어드는 모델 성능 평가, 독립 정확도, 실제 AI 속도·업무 시간 절감의 "
              "개선으로 해석하지 않습니다. 사용한 F04는 독립 미사용 시험 자료가 아닙니다."),
            p("400 V와 440 V는 기존 모델 응답에 이미 있었습니다. 이번 차이는 AI 추출 능력이 아니라 "
              "근거 표시 보완입니다. 현재 요청 내용은 현재 코드가 만들었으며, 구버전 재실행이나 "
              "수업 보너스 조건 충족을 확정한 결과는 아닙니다.", "small"),
            p("측정 기록", "h2"),
            p(report_path, "small"),
            p("입력·저장 응답·비교 기준·원본 보존 여부는 측정 JSON에 기록했습니다. "
              "정답을 모델에 다시 보내거나 원본 보고서를 수정한 결과가 아닙니다.", "small"),
        ])
        expected_pages = 8

    def decorate(canvas, doc):
        canvas.saveState()
        w, h = A4
        canvas.setFillColor(TEAL)
        canvas.rect(44, h - 31, 25, 3, fill=1, stroke=0)
        canvas.setFont(FONT, 8)
        canvas.setFillColor(GRAY)
        canvas.drawString(78, h - 32, "변압기 수주 기술사양 검토 AI")
        canvas.setStrokeColor(LINE)
        canvas.line(44, 37, w - 44, 37)
        canvas.setFont(FONT, 7.5)
        canvas.drawString(44, 24, "교육용 프로토타입 | 가상 문서 시험 | 실제 승인·발주용이 아닙니다")
        canvas.drawRightString(w - 44, 24, f"{doc.page} / {expected_pages}")
        canvas.restoreState()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(
        str(args.output), pagesize=A4, leftMargin=44, rightMargin=44,
        topMargin=55, bottomMargin=52, title="변압기 수주 기술사양 검토 AI 최종 보고서",
        author="seogdo", subject="AI 서비스 Mini Project",
    ).build(story, onFirstPage=decorate, onLaterPages=decorate)
    from pypdf import PdfReader
    actual_pages = len(PdfReader(str(args.output)).pages)
    if actual_pages != expected_pages:
        raise ValueError(f"Layout has {actual_pages} pages, expected {expected_pages}; inspect before delivery.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path, required=True)
    parser.add_argument("--python-tests", type=int, required=True)
    parser.add_argument("--report-ui-tests", type=int, required=True)
    parser.add_argument("--v02", required=True)
    parser.add_argument("--v03", required=True)
    parser.add_argument("--print-check", required=True)
    parser.add_argument("--before-after", type=Path,
                        help="Confirmed archived-baseline/current-response-replay measurement JSON.")
    parser.add_argument("--output", type=Path, default=ROOT / "output/pdf/final-report.pdf")
    build(parser.parse_args())
