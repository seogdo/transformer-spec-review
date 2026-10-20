"""Pure, escaped HTML presentation of an already-saved review.

No I/O, model invocation, comparison, normalization or snapshot mutation occurs
here. Stored verdicts and unknown/legacy values remain visibly distinguishable.
"""
from __future__ import annotations

from html import escape
import json
import re
from typing import Any

from .contracts import FIELD_KEYS

_ABSENT = object()
_STATUS = {
    "MATCH": ("일치", "match"),
    "MISMATCH": ("불일치", "mismatch"),
    "MISSING": ("누락", "missing"),
    "NEEDS_REVIEW": ("확인 필요", "needs-review"),
}
_DOCUMENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_UPLOAD_ID = re.compile(r"[a-f0-9]{32}\Z")
_NOTICE = (
    "교육용 가상 문서의 회의용 검토 보고서입니다. 저장된 결과를 읽기 전용으로 표시하며 "
    "AI 호출·재판정·새 요약을 수행하지 않습니다. 보고서를 열거나 인쇄하는 행위는 "
    "담당자의 확인·결재·승인이나 발주·제작 지시가 아닙니다. "
    "문서 진위·실제 승인 권한·설계 적합성·제품 안전을 보증하지 않습니다."
)


def _raw(value: Any) -> str:
    if value is _ABSENT:
        return "기록 없음 (키 없음)"
    if value is None:
        return "null (저장값)"
    if isinstance(value, str):
        return value if value else '"" (빈 문자열)'
    return json.dumps(value, ensure_ascii=False)


def _html(value: Any) -> str:
    return escape(_raw(value), quote=True)


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _pairs(value: Any, labels: dict[str, str] | None = None) -> str:
    if not isinstance(value, dict) or not value:
        return f'<p class="report-raw">{_html(value)}</p>'
    labels = labels or {}
    rows = "".join(
        f"<dt>{_html(labels.get(key, key))}</dt><dd>{_html(item)}</dd>"
        for key, item in value.items()
    )
    return f'<dl class="report-meta">{rows}</dl>'


def _status(value: Any) -> str:
    label, tone = _STATUS.get(value, ("알 수 없는 저장 상태", "unknown")) if isinstance(value, str) else ("알 수 없는 저장 상태", "unknown")
    return f'<span class="report-status {tone}">{label} · <code>{_html(value)}</code></span>'


def _messages(value: Any, empty: str = "추가 경고 없음 (저장된 빈 목록)") -> str:
    if isinstance(value, list) and value:
        return '<ul class="report-messages">' + "".join(f"<li>{_html(item)}</li>" for item in value) + "</ul>"
    if value == []:
        return f'<p class="report-small">{escape(empty)}</p>'
    return f'<p class="report-raw">{_html(value)}</p>'


def _document_index(record: dict) -> dict[str, dict]:
    """Only unambiguous, bounded document metadata can produce a local link."""
    documents = record.get("documents")
    index: dict[str, dict] = {}
    duplicate = set()
    if not isinstance(documents, list):
        return index
    for document in documents:
        if not isinstance(document, dict):
            continue
        identifier = document.get("id")
        if not isinstance(identifier, str) or not _DOCUMENT_ID.fullmatch(identifier):
            continue
        if identifier in index:
            duplicate.add(identifier)
        index[identifier] = document
    return {key: value for key, value in index.items() if key not in duplicate}


def _pdf_link(record: dict, source: dict, documents: dict[str, dict]) -> str | None:
    upload_id, document_id, page = record.get("upload_id"), source.get("document_id"), source.get("page")
    if not isinstance(upload_id, str) or not _UPLOAD_ID.fullmatch(upload_id):
        return None
    if not isinstance(document_id, str) or document_id not in documents:
        return None
    pages = documents[document_id].get("page_count")
    if type(page) is not int or type(pages) is not int or not 1 <= page <= pages:
        return None
    # Ignore all saved pdf_url/page_url values, including same-origin-looking
    # paths. Link identity comes exclusively from validated structured fields.
    return f"/api/uploads/{upload_id}/documents/{document_id}/pdf#page={page}"


def _source_card(record: dict, source: Any, documents: dict[str, dict], label: str) -> str:
    if not isinstance(source, dict):
        return f'<section class="report-source"><h4>{escape(label)}</h4><p class="report-warning">원문 근거 형식 확인 필요</p><pre class="report-raw">{_html(source)}</pre></section>'
    candidate = source.get("evidence_kind") == "field_occurrence"
    title = source.get("title", _ABSENT)
    meta = f'문서 ID: {_html(source.get("document_id", _ABSENT))} · 페이지: {_html(source.get("page", _ABSENT))}'
    count = source.get("occurrence_count", _ABSENT)
    if count is not _ABSENT:
        meta += f" · 저장된 반복 횟수: {_html(count)}"
    note = "저장된 원문 후보입니다. 확정된 요구값·인계값이나 승인값을 뜻하지 않습니다." if candidate else "저장된 원문 인용입니다. 보고서에서 원문 대조를 새로 수행하지 않았습니다."
    link = _pdf_link(record, source, documents)
    action = (f'<a href="{escape(link, quote=True)}" target="_blank" rel="noopener noreferrer">원본 PDF 보기 · {_html(source["page"])}쪽</a>'
              if link else '<span class="report-small">원본 링크 생략: 저장된 문서·페이지 연결 정보를 확인할 수 없습니다.</span>')
    return (
        f'<section class="report-source"><h4>{escape(label)} · {_html(title)}</h4>'
        f'<p class="report-small">{meta}</p><p class="report-small">{note}</p>'
        f'<blockquote class="report-quote">{_html(source.get("quote", _ABSENT))}</blockquote>{action}</section>'
    )


def _value(value: Any) -> str:
    if not isinstance(value, dict):
        return f'<span class="report-value">{_html(value)}</span>'
    content = f'{_html(value.get("value", _ABSENT))} <span class="report-unit">{_html(value.get("unit", _ABSENT))}</span>'
    if "condition" in value:
        content += f'<span class="report-small">조건: {_html(value["condition"])}</span>'
    source = value.get("source")
    if isinstance(source, dict):
        content += f'<span class="report-small">출처: {_html(source.get("document_id", _ABSENT))} · {_html(source.get("page", _ABSENT))}쪽</span>'
    return f'<div class="report-value">{content}</div>'


def _evidence(record: dict, row: dict, index: int, documents: dict[str, dict]) -> str:
    check = row.get("evidence_check", _ABSENT)
    current = isinstance(check, dict) and check.get("version") == "original-page-evidence-v2"
    notice = (
        "저장된 원문 대조 정보입니다. 원문 인용 확인과 판정 확정은 다르며, 원문 연결은 승인이나 안전 보증이 아닙니다."
        if current else
        "과거 또는 알 수 없는 근거 메타데이터입니다. 기존 evidence_complete 값은 현재 방식의 근거 완전성 보증이 아닙니다."
    )
    if row.get("status") == "NEEDS_REVIEW":
        notice += " 이 항목은 판정 보류 상태입니다. 원문이 있어도 확정 판정으로 바꾸지 않습니다."
    parts = [
        f'<section class="report-evidence"><h3>{index}. {_html(row.get("label", row.get("field", _ABSENT)))}</h3>',
        _status(row.get("status", _ABSENT)),
        f'<p class="report-reason">{_html(row.get("reason", _ABSENT))}</p>',
        f'<p class="report-notice">{notice}</p>',
        _pairs({"evidence_complete": row.get("evidence_complete", _ABSENT)},
               {"evidence_complete": "저장된 evidence_complete"}),
        _pairs(check),
    ]
    sources = row.get("sources", _ABSENT)
    stored_sources = sources if isinstance(sources, list) else []
    if not isinstance(sources, list) or not sources:
        parts.append(f'<p class="report-warning">저장된 원문 근거 목록: {_html(sources)}</p>')
    for source in stored_sources:
        parts.append(_source_card(record, source, documents, "원문 근거"))
    # Some legacy rows contain only the value's source, without a sources
    # list. Preserve that stored quote too; do not invent a replacement.
    for key, label in (("expected", "요구값의 저장 출처"), ("actual", "인계값의 저장 출처")):
        source = _mapping(row.get(key)).get("source")
        if isinstance(source, dict) and not any(
            isinstance(item, dict) and all(item.get(name) == source.get(name) for name in ("document_id", "title", "page", "quote"))
            for item in stored_sources
        ):
            parts.append(_source_card(record, source, documents, label))
    parts.append("</section>")
    return "".join(parts)


def render_report(record: dict) -> str:
    """Render stored data without modifying or reinterpreting the record."""
    rows = record.get("results", _ABSENT)
    valid_rows = isinstance(rows, list)
    stored_rows = rows if valid_rows else []
    count = str(len(stored_rows)) if valid_rows else "확인할 수 없는 수의"
    documents = _document_index(record)
    identity = record.get("customer_check", _ABSENT)
    identity_current = (isinstance(identity, dict)
                        and identity.get("version") == "customer-identity-v1"
                        and isinstance(identity.get("status"), str)
                        and identity.get("status") in {"MATCH", "MISMATCH", "NEEDS_REVIEW"}
                        and type(identity.get("can_compare")) is bool)
    field_keys = [row.get("field") for row in stored_rows if isinstance(row, dict)]
    current_fields = (len(stored_rows) == len(FIELD_KEYS)
                      and all(isinstance(key, str) for key in field_keys)
                      and len(field_keys) == len(stored_rows) and set(field_keys) == set(FIELD_KEYS))
    warnings = []
    if not current_fields:
        warnings.append(f"저장된 항목 구성은 현재 {len(FIELD_KEYS)}개 항목 범위와 다르거나 확인할 수 없습니다. 누락 항목을 추가하거나 저장된 판정을 바꾸지 않습니다.")
    if not identity_current:
        warnings.append("고객사 미검증 과거기록 또는 알 수 없는 고객사 메타데이터입니다. 현재 방식으로 확인됐다고 표시하지 않습니다.")
    for key, label in (("created_at", "생성 시각"), ("policy", "적용 기준"), ("documents", "문서 목록"), ("summary", "요약")):
        if key not in record or record[key] is None:
            warnings.append(f"{label} 메타데이터가 없거나 null입니다. 보고서가 값을 추측해 채우지 않습니다.")
    if any(not isinstance(row, dict) or not isinstance(row.get("status"), str)
           or row.get("status") not in _STATUS for row in stored_rows):
        warnings.append("알 수 없는 저장 상태가 있습니다. 원래 상태를 표시하며 일치·불일치·보류로 재분류하지 않습니다.")
    body = [
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<title>회의용 검토 보고서</title><link rel="stylesheet" href="/assets/report.css">',
        '<script src="/assets/report.js" defer></script></head><body class="report-page">',
        '<div class="report-toolbar screen-only"><button id="print-button" type="button">인쇄 · PDF로 저장</button>',
        '<p>브라우저 인쇄 대화상자에서 PDF 저장을 선택할 수 있습니다. 서버가 PDF를 생성하거나 저장하지 않습니다.</p>',
        '<p id="print-error" role="alert" hidden></p><noscript>브라우저의 인쇄 메뉴를 사용해 주세요.</noscript></div>',
        '<main class="report-document"><header class="report-header"><p class="report-small">저장된 검토 기록 · 읽기 전용</p>',
        f'<h1>회의용 검토 보고서</h1><p>저장된 {count}개 항목 · 새 검토 또는 승인 아님</p></header>',
        f'<p class="report-notice">{_NOTICE}</p>',
        '<section class="report-section"><h2>저장된 요약</h2><p class="report-small">아래 수치는 저장값이며 보고서에서 다시 계산하지 않습니다.</p>',
        _pairs(record.get("summary", _ABSENT), {key: f"{value[0]} ({key})" for key, value in _STATUS.items()}), "</section>",
        '<section class="report-section"><h2>항목별 저장 결과</h2>',
        '<table class="report-table"><thead><tr><th scope="col">항목</th><th scope="col">요구값</th><th scope="col">인계값</th><th scope="col">저장된 상태</th></tr></thead><tbody>',
    ]
    for index, row in enumerate(stored_rows, 1):
        info = _mapping(row)
        body.append(
            f'<tr><th scope="row">{index}. {_html(info.get("label", info.get("field", _ABSENT)))}'
            f'<span class="report-small">{_html(info.get("field", _ABSENT))}</span></th>'
            f'<td>{_value(info.get("expected", _ABSENT))}</td><td>{_value(info.get("actual", _ABSENT))}</td>'
            f'<td>{_status(info.get("status", _ABSENT))}</td></tr>'
        )
    body.append("</tbody></table>")
    if not valid_rows:
        body.append(f'<p class="report-warning">저장된 결과 목록: {_html(rows)}</p>')
    body.extend([
        '</section><section class="report-section"><h2>기록의 범위와 주의사항</h2>',
        _messages(warnings, "현재 표시 형식에서 추가로 알릴 메타데이터 경고 없음"), "</section>",
        '<section class="report-section"><h2>저장 기록 정보</h2>',
        _pairs({key: record.get(key, _ABSENT) for key in ("review_id", "upload_id", "created_at", "record_kind", "extraction_method", "policy_confirmed")},
               {"review_id": "검토 ID", "upload_id": "업로드 ID", "created_at": "저장된 생성 시각", "record_kind": "기록 종류",
                "extraction_method": "저장된 추출 방식", "policy_confirmed": "저장 당시 기준 확인 값 (새 확인 아님)"}), "</section>",
        '<section class="report-section"><h2>고객사 기본정보</h2><p class="report-small">기술 항목과 별도인 저장 메타데이터입니다. 실제 거래 관계·법인 동일성을 인증하지 않습니다.</p>',
    ])
    if isinstance(identity, dict):
        body.append(_pairs({key: value for key, value in identity.items() if key != "documents"}))
        customer_documents = identity.get("documents", _ABSENT)
        if isinstance(customer_documents, list):
            for item in customer_documents:
                body.append('<section class="report-source"><h3>저장된 고객사 문서 정보</h3>' + _pairs(item) + "</section>")
        else:
            body.append(_pairs(customer_documents))
    else:
        body.append(_pairs(identity))
    body.extend([
        "</section>", '<section class="report-section"><h2>문서와 적용 기준</h2>',
        '<p class="report-small">approved·승인자 표기는 가상 입력 메타데이터이며 실제 권한이나 서명을 검증한 결과가 아닙니다.</p>',
    ])
    for document in record.get("documents", []) if isinstance(record.get("documents"), list) else []:
        info = _mapping(document)
        body.append('<section class="report-source">' + _pairs(
            {key: info.get(key, _ABSENT) for key in ("id", "title", "filename", "role", "project_id", "customer_name", "status", "approved_by", "supersedes", "page_count")},
            {"id": "문서 ID", "title": "문서명", "filename": "저장된 파일명", "role": "역할", "project_id": "프로젝트", "customer_name": "고객사 원문",
             "status": "저장된 승인 표기", "approved_by": "저장된 승인자 표기", "supersedes": "대체 대상", "page_count": "저장된 페이지 수"}) + "</section>")
    body.extend([
        '<h3>저장된 적용 기준</h3>', _pairs(record.get("policy", _ABSENT)), "</section>",
        '<section class="report-section"><h2>저장된 판정 사유와 원문 근거</h2>',
    ])
    for index, row in enumerate(stored_rows, 1):
        if isinstance(row, dict):
            body.append(_evidence(record, row, index, documents))
        else:
            body.append(f'<p class="report-warning">저장된 {index}번 행의 형식 확인 필요: {_html(row)}</p>')
    body.extend([
        "</section>", '<section class="report-section"><h2>저장된 경고와 한계</h2>',
        _messages(record.get("warnings", _ABSENT)),
        _messages(record.get("limitations", _ABSENT), "저장된 추가 한계 목록 없음"),
        '<p class="report-notice">회의 참고용이며 결재·승인·설계 적합성·안전 보증이 아닙니다.</p></section></main></body></html>',
    ])
    return "".join(body)
