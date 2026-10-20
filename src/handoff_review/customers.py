"""Exact, local customer-name checks for the user-defined demo candidate set.

The six candidates and their aliases were supplied for this project. They do
not establish real transactions, verify a legal entity, or prove that a person
approved a document. There is no network lookup, fuzzy matching, substring
matching, suffix removal, or inference from a project identifier.

Customer identity is separate from the ten technical specification fields.
Every attached document participates, including unselected amendments.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


VERSION = "customer-identity-v1"
DISCLAIMER = "사용자가 지정한 고객사 후보의 문서 표기 확인이며 실제 거래 관계·법인 동일성·승인을 검증한 결과가 아닙니다."

# Display names are local canonical labels, not externally verified legal
# names. Only the explicitly enumerated full aliases may select one of them.
_CUSTOMERS = (
    ("aep", "AEP", ("AEP", "미국 AEP")),
    ("kepco", "한국전력공사", ("한국전력", "한국전력공사", "한전", "KEPCO")),
    ("samsung_electronics", "삼성전자", ("삼성전자", "삼전", "Samsung Electronics")),
    ("sk_hynix", "SK하이닉스", ("SK하이닉스", "하이닉스", "SK hynix")),
    ("national_grid", "National Grid", ("National Grid", "NG", "영국 NG")),
    ("transgrid", "Transgrid", ("Transgrid", "호주 Transgrid", "트랜스그리드")),
)


def _name_token(value: str) -> str:
    """Allow case differences and repeated whitespace, but no other rewriting."""
    return " ".join(value.split()).casefold()


_ALIASES = {
    _name_token(alias): (identifier, display_name)
    for identifier, display_name, aliases in _CUSTOMERS
    for alias in aliases
}


def resolve_customer(raw: Any) -> dict[str, Any]:
    """Resolve one original customer-name value without guessing a company.

    ``None`` and blank strings are missing. Lists are deliberately ambiguous,
    even when they contain just one name or aliases of the same candidate:
    an upstream extractor used that shape to flag conflicting/bad evidence.
    Unsupported types are also ambiguous. Unknown strings never become the
    closest candidate. ``raw_name`` is copied without normalization so the
    caller can show the original document spelling.
    """
    result: dict[str, Any] = {
        "raw_name": deepcopy(raw),
        "canonical_id": None,
        "canonical_name": None,
        "status": "ambiguous",
        "message": "고객사 표기가 복수이거나 형식이 올바르지 않아 원문 확인이 필요합니다.",
    }
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        result.update(status="missing", message="고객사명이 명시되지 않아 원문 확인이 필요합니다.")
        return result
    if not isinstance(raw, str):
        return result
    candidate = _ALIASES.get(_name_token(raw))
    if candidate is None:
        result.update(status="unknown", message="등록된 고객사 별칭과 정확히 일치하지 않습니다. 신규 고객 또는 다른 법인인지 담당자가 확인해야 합니다.")
        return result
    identifier, display_name = candidate
    result.update(
        canonical_id=identifier,
        canonical_name=display_name,
        status="recognized",
        message=f"사용자 지정 후보 '{display_name}'의 명시된 별칭으로 확인했습니다. 실제 거래·법인 확인은 아닙니다.",
    )
    return result


def check_customers(documents: Any) -> dict[str, Any]:
    """Permit comparison only when every attachment has the same known name.

    Missing/unknown/ambiguous names take precedence over recognized-name
    disagreements: an incomplete set is NEEDS_REVIEW, not a confirmed mismatch.
    Provenance records are deep-copied for display, not independently verified.
    Selection, approval status, timestamps and project IDs cannot exempt an
    attachment from this check.
    """
    result: dict[str, Any] = {
        "version": VERSION,
        "status": "NEEDS_REVIEW",
        "can_compare": False,
        "message": "첨부 문서의 고객사 동일성을 확인할 수 없습니다. " + DISCLAIMER,
        "documents": [],
    }
    if not isinstance(documents, list) or not documents:
        result["message"] = "고객사 확인에 필요한 문서 목록이 없거나 형식이 올바르지 않습니다. " + DISCLAIMER
        return result
    for document in documents:
        if not isinstance(document, Mapping):
            resolved = resolve_customer([])
            resolved["message"] = "문서 구조가 올바르지 않아 고객사 원문을 확인할 수 없습니다."
            row = {"document_id": None, "role": None, **resolved, "sources": []}
        else:
            resolved = resolve_customer(document.get("customer_name"))
            sources = document.get("customer_sources", [])
            row = {
                "document_id": deepcopy(document.get("id")),
                "role": deepcopy(document.get("role")),
                **resolved,
                "sources": deepcopy(sources) if isinstance(sources, list) else [],
            }
        result["documents"].append(row)
    rows = result["documents"]
    if any(row["status"] != "recognized" for row in rows):
        result["message"] = "고객사명 누락·미등록·상충 표기가 포함돼 문서 동일성을 확정하지 않았습니다. 기술사양 비교를 보류합니다. " + DISCLAIMER
        return result
    identifiers = {row["canonical_id"] for row in rows}
    if len(identifiers) != 1:
        result.update(
            status="MISMATCH",
            message="서로 다른 고객사 후보의 문서가 함께 첨부됐습니다. 문서 혼입 여부를 확인할 때까지 기술사양 비교를 보류합니다. " + DISCLAIMER,
        )
        return result
    result.update(
        status="MATCH",
        can_compare=True,
        message="모든 첨부 문서의 고객사 표기가 같은 사용자 지정 후보에 해당합니다. " + DISCLAIMER,
    )
    return result
