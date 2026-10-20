"""Strict text-template ingestion for the project's synthetic demonstration PDFs.

This is not a general technical-document parser, OCR engine, or LLM extractor.
Only the published synthetic template is supported.  Printed table values and
the quoted requirement must agree; ambiguity is retained as an invalid scalar
so the reference engine returns NEEDS_REVIEW rather than selecting a last value.

One input document covers one project and one product.  Project identifiers are
read, not invented; cross-document identity is checked by ``review_case``.
"""

from __future__ import annotations

from io import BytesIO
import re
from typing import Any

from pypdf import PdfReader

from .contracts import FIELD_KEYS, FIELD_SPECS, UncertainValue, normalize

MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_PDF_PAGES = 20
MAX_EXTRACTED_CHARACTERS = 200_000
EXTRACTION_METHOD = "template_text"
ROLES = {
    "customer_spec": "가상 고객 기술사양서",
    "amendment": "가상 승인변경 합의서",
    "handoff": "가상 내부 기술인계표",
}
TABLE_LABELS = {
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
_LABEL_TO_KEY = {label: key for key, label in TABLE_LABELS.items()}
_SPECS = {spec.key: spec for spec in FIELD_SPECS}
_ID = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
_META_ID = re.compile(rf"문서 ID\s+({_ID})\s*\|\s*프로젝트\s+({_ID})")
_META_ROLE = re.compile(r"문서 역할\s+(customer_spec|amendment|handoff)\s*\|\s*데모 상태\s+(approved|draft)")
_META_APPROVAL = re.compile(rf"승인 표기\s+(.+?)\s*\|\s*대체 대상\s+(없음|{_ID})")
_REQUIREMENT = re.compile(r"가상 요구사항\s+([a-z][a-z0-9_]*)\s*:\s*(.*)")


class PdfInputError(ValueError):
    """An uploaded PDF cannot be safely accepted by this limited extractor."""


def _unique_metadata(lines: list[str], pattern: re.Pattern[str], label: str) -> tuple[str, ...]:
    prefix = {"문서 ID·프로젝트": "문서 ID", "문서 역할·상태": "문서 역할", "승인 표기·대체 대상": "승인 표기"}[label]
    if any(line.startswith(prefix) and not pattern.fullmatch(line) for line in lines):
        raise PdfInputError(f"{label} 메타데이터에 지원하지 않는 형식이 포함됐습니다.")
    matches = {match.groups() for line in lines if (match := pattern.fullmatch(line))}
    # Repeated identical page headers are acceptable. Conflicting headers are
    # rejected as a document-level identity failure, never resolved by order.
    if len(matches) != 1:
        state = "없거나 지원 형식이 아닙니다" if not matches else "상충하는 값이 중복됐습니다"
        raise PdfInputError(f"{label} 메타데이터가 {state}.")
    return next(iter(matches))


def read_customer_metadata(pages: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Read an explicit customer header without inferring identity from body text.

    Customer identity is optional for ingesting older PDFs, not for comparing
    them. Missing/contradictory headers are retained as review blockers. Repeated
    identical headers on multiple pages are allowed; different aliases within
    one PDF still require review instead of silently selecting one declaration.
    The list marker represents invalid evidence, never a supported scalar name.
    Source quotes are copied exactly from the extracted page (outer whitespace
    excluded), and no model or filename participates in this metadata step.
    """
    prefix = "고객사(발주처)"
    sources = []
    names = []
    malformed = False
    for page in pages:
        for raw_line in page["text"].splitlines():
            line = raw_line.strip()
            if not line.startswith(prefix):
                continue
            sources.append({"page": page["page"], "quote": line})
            suffix = line[len(prefix):]
            name = suffix.strip()
            if not suffix[:1].isspace() or not name or len(name) > 160:
                malformed = True
            if name not in names:
                names.append(name)
    warnings = []
    if not sources:
        value = None
        warnings.append({"code": "CUSTOMER_METADATA_MISSING",
                         "message": "고객사(발주처) 표기가 없어 기술사양 비교를 보류합니다."})
    elif malformed or len(names) != 1:
        value = names or [""]
        warnings.append({"code": "CUSTOMER_METADATA_AMBIGUOUS",
                         "message": "고객사 표기가 비어 있거나 형식·복수 표기를 확인해야 합니다. 기술사양 비교를 보류합니다."})
    else:
        value = names[0]
    return {"customer_name": value, "customer_sources": sources}, warnings


def _value_parts(key: str, payload: str) -> dict[str, str]:
    """Separate an already single template value; never infer missing units."""
    spec = _SPECS[key]
    if spec.allowed_values:
        return {"value": payload.strip(), "unit": ""}
    pieces = payload.strip().rsplit(None, 1)
    if len(pieces) != 2:
        raise UncertainValue("수치와 단위가 공백으로 구분된 단일 표기를 확인할 수 없습니다.")
    return {"value": pieces[0], "unit": pieces[1]}


def _uncertain(candidate: dict[str, Any], reason: str) -> dict[str, Any]:
    """Use a non-scalar marker: the engine cannot classify it as MISSING."""
    return {
        **candidate,
        "value": ["EXTRACTION_UNCERTAIN", candidate.get("value", "")],
        "extraction_uncertainty": reason,
    }


def extract_document(pdf_bytes: bytes, expected_role: str) -> dict[str, Any]:
    """Extract a supported PDF into ``document``, original ``pages``, warnings.

    Raises PdfInputError for unreadable, encrypted, oversized, textless, or
    structurally unsupported documents.  Recoverable field ambiguities retain
    their actual source line/page and become NEEDS_REVIEW inputs.  No fixture,
    manual answer, filename-derived identity, model, network, or disk write is
    used.  Byte/page/text limits are application guards, not a hostile-PDF
    process sandbox or a guarantee about a decoder's transient memory use.
    """
    if expected_role not in ROLES:
        raise PdfInputError("지원하지 않는 업로드 문서 역할입니다.")
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes:
        raise PdfInputError("비어 있지 않은 PDF 바이트가 필요합니다.")
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise PdfInputError("PDF 파일이 최대 5 MiB를 초과했습니다.")
    if not pdf_bytes.startswith(b"%PDF-"):
        raise PdfInputError("PDF 파일 헤더를 확인할 수 없습니다.")
    try:
        reader = PdfReader(BytesIO(pdf_bytes), strict=True)
        if reader.is_encrypted:
            raise PdfInputError("암호화된 PDF는 지원하지 않습니다. 비암호화 텍스트 PDF를 사용해 주세요.")
        if not 1 <= len(reader.pages) <= MAX_PDF_PAGES:
            raise PdfInputError("PDF는 1~20쪽만 지원합니다.")
        pages: list[dict[str, Any]] = []
        total = 0
        for page_number, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            if not isinstance(text, str) or not text.strip():
                raise PdfInputError(f"{page_number}쪽에서 텍스트를 추출할 수 없습니다. 스캔·이미지·빈 페이지만 있는 PDF는 지원하지 않습니다.")
            total += len(text)
            if total > MAX_EXTRACTED_CHARACTERS:
                raise PdfInputError("PDF 추출 텍스트가 최대 200,000자를 초과했습니다.")
            pages.append({"page": page_number, "text": text})
    except PdfInputError:
        raise
    except Exception as exc:
        raise PdfInputError("PDF가 손상됐거나 지원하지 않는 구조여서 읽을 수 없습니다.") from exc

    all_lines = [line.strip() for page in pages for line in page["text"].splitlines() if line.strip()]
    if "HANDOFF REVIEW / SYNTHETIC DOCUMENT" not in all_lines or not any(
        line.startswith("교육용 가상 자료 | 실제 설계·시험·승인용이 아닙니다.") for line in all_lines
    ):
        raise PdfInputError("교육용 가상 문서 선언을 확인할 수 없습니다. 제공한 텍스트 템플릿만 지원합니다.")
    doc_id, project_id = _unique_metadata(all_lines, _META_ID, "문서 ID·프로젝트")
    role, status = _unique_metadata(all_lines, _META_ROLE, "문서 역할·상태")
    approver, predecessor = _unique_metadata(all_lines, _META_APPROVAL, "승인 표기·대체 대상")
    if role != expected_role:
        raise PdfInputError(f"업로드 위치의 역할({expected_role})과 PDF 내부 역할({role})이 다릅니다.")
    titles = {line for line in all_lines if line in ROLES.values()}
    if titles != {ROLES[role]}:
        raise PdfInputError("문서 제목이 없거나 문서 역할과 일치하지 않습니다.")

    customer_metadata, warnings = read_customer_metadata(pages)
    candidates: dict[str, list[dict[str, Any]]] = {key: [] for key in FIELD_KEYS}
    unassigned: list[dict[str, Any]] = []

    def warn(code: str, message: str, key: str | None = None, page: int | None = None) -> None:
        item: dict[str, Any] = {"code": code, "message": message}
        if key is not None:
            item["field"] = key
        if page is not None:
            item["page"] = page
        warnings.append(item)

    for page in pages:
        number = page["page"]
        lines = [line.strip() for line in page["text"].splitlines() if line.strip()]
        consumed_labels: set[int] = set()
        for index, line in enumerate(lines):
            if "가상 요구사항" not in line:
                continue
            match = _REQUIREMENT.fullmatch(line)
            key = match.group(1) if match else None
            if key not in _SPECS:
                # If the expected row label is present we know which field is
                # affected; otherwise the document's comparison scope is unclear.
                nearby = _LABEL_TO_KEY.get(lines[index - 2]) if index >= 2 else None
                if nearby is not None:
                    consumed_labels.add(index - 2)
                    candidate = {"value": "", "unit": "", "page": number, "quote": line}
                    candidates[nearby].append(_uncertain(candidate, "요구사항 키나 문장 형식이 지원 템플릿과 다릅니다."))
                    warn("UNPARSED_REQUIREMENT", "항목은 있지만 원문 요구사항을 안전하게 분리할 수 없습니다.", nearby, number)
                else:
                    unassigned.append({"page": number, "quote": line})
                    warn("UNKNOWN_REQUIREMENT_SCOPE", "분류할 수 없는 요구사항이 있어 비교 범위를 확정할 수 없습니다.", page=number)
                continue

            payload = match.group(2)
            candidate: dict[str, Any] = {"value": payload, "unit": "", "page": number, "quote": line}
            reasons: list[str] = []
            label_ok = index >= 2 and lines[index - 2] == TABLE_LABELS[key]
            if label_ok:
                consumed_labels.add(index - 2)
                candidate["table_value"] = lines[index - 1]
            else:
                reasons.append("연결된 표 항목명·기재값의 위치를 확인할 수 없습니다.")
            try:
                candidate.update(_value_parts(key, payload))
                normalized_quote = normalize(_SPECS[key], candidate)
                if label_ok:
                    table_parts = _value_parts(key, lines[index - 1])
                    normalized_table = normalize(_SPECS[key], table_parts)
                    if normalized_quote.comparable != normalized_table.comparable:
                        reasons.append("표 기재값과 원문 근거의 값이 서로 다릅니다.")
            except UncertainValue as exc:
                reasons.append(str(exc))
            if reasons:
                message = " ".join(reasons)
                candidate = _uncertain(candidate, message)
                warn("UNCERTAIN_FIELD", message, key, number)
            candidates[key].append(candidate)

        # An identifiable table row without a usable requirement line must not
        # disappear and turn into an apparently ordinary missing field.
        for index, line in enumerate(lines):
            key = _LABEL_TO_KEY.get(line)
            if key is None or index in consumed_labels:
                continue
            candidate = {"value": "", "unit": "", "page": number, "quote": line}
            candidates[key].append(_uncertain(candidate, "표 항목은 있으나 연결된 원문 근거를 추출하지 못했습니다."))
            warn("UNPARSED_TABLE_ROW", "표 항목은 있지만 원문 근거를 안전하게 연결할 수 없습니다.", key, number)

    if not any(candidates.values()) and not unassigned:
        raise PdfInputError("지원하는 요구사항 표를 찾지 못했습니다. 템플릿 또는 추출 상태를 확인해 주세요.")
    fields: dict[str, dict[str, Any]] = {}
    for key, entries in candidates.items():
        if not entries:
            continue
        if len(entries) == 1:
            fields[key] = entries[0]
        else:
            fields[key] = _uncertain(entries[0], "동일 항목이 반복돼 단일 기준값으로 확정하지 않았습니다.")
            fields[key]["extraction_candidates"] = entries
            warn("DUPLICATE_FIELD", "반복된 항목은 값이 같아도 자동 병합하지 않고 확인 대상으로 남깁니다.", key)
    if unassigned:
        for key in FIELD_KEYS:
            original = fields.get(key, {"value": "", "unit": "", **unassigned[0]})
            fields[key] = _uncertain(original, "해석하지 못한 요구사항의 대상 범위가 불명확합니다.")
        warn("DOCUMENT_SCOPE_UNCERTAIN", f"해석되지 않은 요구사항을 누락으로 확정하지 않도록 {len(FIELD_KEYS)}개 항목을 모두 보류합니다.")
    # Every evidence quote must actually occur in the extracted text. This
    # assertion checks provenance within extraction, not rendered visual truth.
    assert all(entry["quote"] in pages[entry["page"] - 1]["text"] for entry in fields.values())
    document = {
        "id": doc_id, "role": role, "title": ROLES[role], "project_id": project_id,
        **customer_metadata,
        "status": status, "approved_by": None if approver.strip() == "없음" else approver.strip(),
        "supersedes": None if predecessor == "없음" else predecessor,
        "fields": fields,
    }
    return {"document": document, "pages": pages, "warnings": warnings, "extraction_method": EXTRACTION_METHOD}
