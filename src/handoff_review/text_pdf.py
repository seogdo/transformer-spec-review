"""Read bounded synthetic text PDFs without interpreting their requirements.

Unlike ``pdf_ingest``, this reader accepts different body layouts, not just the
published requirement-row template. Identity/authority metadata and the
synthetic-data declaration remain mandatory. Empty ``fields`` means extraction
is pending, NOT that the PDF contains no requirements; callers must not submit
this intermediate object directly as a completed extraction to the reviewer.

This module performs no field extraction, fixture lookup, LLM/network request,
approval inference, OCR, or disk write. The process wrapper provides additional
limits, but neither component is a production malicious-PDF sandbox.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

from pypdf import PdfReader

from .pdf_ingest import (
    MAX_EXTRACTED_CHARACTERS,
    MAX_PDF_BYTES,
    MAX_PDF_PAGES,
    PdfInputError,
    ROLES,
    _META_APPROVAL,
    _META_ID,
    _META_ROLE,
    _unique_metadata,
    read_customer_metadata,
)

EXTRACTION_METHOD = "text_pending_ai"


def read_text_document(pdf_bytes: bytes, expected_role: str) -> dict[str, Any]:
    """Return verified metadata, original page text, and explicit pending state.

    The limits apply to each PDF: 5 MiB, 1-20 pages and 200,000 extracted Python
    characters. Any textless page is rejected rather than silently discarded.
    Repeated identical page metadata is allowed; conflicting or malformed
    declarations are rejected. Project IDs are preserved for the set-level
    reviewer to compare. The document title is a role-based synthetic label,
    never a claim that a natural-language heading has been understood.
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
        pages = []
        total = 0
        for number, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            if not isinstance(text, str) or not text.strip():
                raise PdfInputError(f"{number}쪽에서 텍스트를 추출할 수 없습니다. 스캔·이미지·빈 페이지는 지원하지 않습니다.")
            total += len(text)
            if total > MAX_EXTRACTED_CHARACTERS:
                raise PdfInputError("PDF 추출 텍스트가 최대 200,000자를 초과했습니다.")
            pages.append({"page": number, "text": text})
    except PdfInputError:
        raise
    except Exception as exc:
        raise PdfInputError("PDF가 손상됐거나 지원하지 않는 구조여서 읽을 수 없습니다.") from exc

    lines = [line.strip() for page in pages for line in page["text"].splitlines() if line.strip()]
    if "HANDOFF REVIEW / SYNTHETIC DOCUMENT" not in lines or not any(
        line.startswith("교육용 가상 자료 | 실제 설계·시험·승인용이 아닙니다.") for line in lines
    ):
        raise PdfInputError("교육용 가상 문서 선언을 확인할 수 없습니다. 가상 선언과 명시 메타데이터가 있는 텍스트 PDF만 지원합니다.")

    # Share only the strict metadata contract with the old template parser.
    # No requirement/table parser is called here; varied body text is preserved.
    document_id, project_id = _unique_metadata(lines, _META_ID, "문서 ID·프로젝트")
    role, status = _unique_metadata(lines, _META_ROLE, "문서 역할·상태")
    approver, predecessor = _unique_metadata(lines, _META_APPROVAL, "승인 표기·대체 대상")
    if role != expected_role:
        raise PdfInputError(f"업로드 위치의 역할({expected_role})과 PDF 내부 역할({role})이 다릅니다.")
    customer_metadata, customer_warnings = read_customer_metadata(pages)
    document = {
        "id": document_id,
        "role": role,
        "title": ROLES[role],
        "project_id": project_id,
        **customer_metadata,
        "status": status,
        "approved_by": None if approver.strip() == "없음" else approver.strip(),
        "supersedes": None if predecessor == "없음" else predecessor,
        "fields": {},
    }
    return {
        "document": document,
        "pages": pages,
        "warnings": [*customer_warnings, {
            "code": "AI_EXTRACTION_PENDING",
            "message": "페이지 텍스트와 명시 메타데이터만 읽었습니다. 본문 항목 추출·근거 검증 전이며 빈 fields를 항목 누락으로 판정하면 안 됩니다.",
        }],
        "extraction_method": EXTRACTION_METHOD,
    }
