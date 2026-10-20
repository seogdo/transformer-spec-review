"""Deterministic, source-linked reference decisions for synthetic handoff data.

Human policy, not timestamps or the language model, selects the initial approved
specification and the ordered approved changes.  Each changed field must have an
explicit replacement lineage.  Ambiguity produces NEEDS_REVIEW instead of an
invented precedence or an automatic engineering approval.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping

from .contracts import FIELD_KEYS, FIELD_SPECS, STATUSES, UncertainValue, is_missing, normalize, source_for
from .customers import check_customers


LIMITATIONS = [
    "가상 문서의 구조화된 입력을 비교한 규칙 참조판정이며 AI 성능 평가가 아닙니다.",
    "AI 추론, 임베딩 검색, PDF 원문 추출은 구현하지 않았습니다.",
    "approved/approved_by는 데모 메타데이터이며 실제 승인·서명·권한을 검증하지 않습니다.",
    "출처와 양의 페이지 번호의 존재만 확인하며, 인용문과 실제 PDF 원문 일치는 검증하지 않습니다.",
    "수치 동등성은 공학적 타당성·규격 적합·안전성·실물 품질의 합격 판정이 아닙니다.",
    "프로젝트 식별자와 조건은 입력 메타데이터에 의존하며 제품 동일성을 외부 시스템과 검증하지 않습니다.",
    "한 문서 세트는 한 프로젝트·한 제품만 다룹니다. 동일 프로젝트 내 다제품 식별·분리는 구현하지 않았습니다.",
    "고객사 확인은 사용자 지정 6개 후보의 명시된 별칭 비교이며 실제 거래 관계·법인 동일성·승인을 검증하지 않습니다.",
]


def _fields(document: Mapping[str, Any]) -> Mapping[str, Any]:
    value = document.get("fields", {})
    return value if isinstance(value, Mapping) else {}


def _approved(document: Mapping[str, Any]) -> bool:
    approver = document.get("approved_by")
    return document.get("status") == "approved" and isinstance(approver, str) and bool(approver.strip())


def review_case(case: Mapping[str, Any]) -> dict[str, Any]:
    """Review the configured fixed fields and return JSON-serializable data.

    The function does not read a case's ``expected`` ground-truth map, modify
    input, write files, access a network, or invoke a model.  Dates are not used
    anywhere in precedence selection.  Unselected amendments are surfaced for
    human clarification rather than silently ignored or promoted to baseline.
    """
    warnings: list[dict[str, Any]] = []
    global_reasons: list[str] = []
    blocked: dict[str, list[str]] = {key: [] for key in FIELD_KEYS}
    documents = case.get("documents", [])
    policy = case.get("review_policy", {})
    if not isinstance(policy, Mapping):
        policy = {}
        global_reasons.append("사람이 지정한 검토 정책이 없습니다.")
    if not isinstance(documents, list):
        documents = []
        global_reasons.append("문서 목록 구조가 올바르지 않습니다.")
    customer_check = check_customers(documents)
    if not customer_check["can_compare"]:
        global_reasons.append("고객사 확인: " + customer_check["message"])
    index: dict[str, Mapping[str, Any]] = {}
    for document in documents:
        if not isinstance(document, Mapping) or not isinstance(document.get("id"), str) or not document["id"].strip():
            global_reasons.append("식별할 수 없는 문서가 포함됐습니다.")
            continue
        doc_id = document["id"]
        if doc_id in index:
            global_reasons.append(f"문서 ID가 중복됐습니다: {doc_id}")
        index[doc_id] = document

    def warn(code: str, message: str, document_id: str | None = None) -> None:
        item = {"code": code, "message": message}
        if document_id is not None:
            item["document_id"] = document_id
        warnings.append(item)

    def block_document(document: Mapping[str, Any], reason: str) -> None:
        relevant = [key for key in _fields(document) if key in blocked]
        # If scope cannot be determined, no field may silently inherit a stale
        # baseline.  Unsupported extra fields are otherwise warning-only.
        for key in relevant or FIELD_KEYS:
            blocked[key].append(reason)

    base_id, handoff_id = policy.get("base_id"), policy.get("handoff_id")
    base = index.get(base_id, {}) if isinstance(base_id, str) else {}
    handoff = index.get(handoff_id, {}) if isinstance(handoff_id, str) else {}
    if not base or base.get("role") != "customer_spec" or not _approved(base):
        global_reasons.append("지정한 최초 고객사양의 역할·승인 메타데이터를 확인할 수 없습니다.")
    if not handoff or handoff.get("role") != "handoff":
        global_reasons.append("지정한 내부 인계표를 확인할 수 없습니다.")
    if not isinstance(base.get("fields"), Mapping):
        global_reasons.append("고객사양의 fields 구조가 객체가 아니거나 누락됐습니다.")
    if not isinstance(handoff.get("fields"), Mapping):
        global_reasons.append("인계표의 fields 구조가 객체가 아니거나 누락됐습니다. 값 누락으로 확정하지 않습니다.")
    project_id = base.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip() or handoff.get("project_id") != project_id:
        global_reasons.append("기준 문서와 인계표의 프로젝트가 일치하지 않거나 명시되지 않았습니다.")

    selected = policy.get("approved_amendment_ids", [])
    if not isinstance(selected, list) or any(not isinstance(doc_id, str) for doc_id in selected):
        selected = []
        global_reasons.append("승인변경 순서 목록이 올바르지 않습니다.")
    if len(selected) != len(set(selected)):
        global_reasons.append("승인변경 목록에 동일 문서가 중복 지정됐습니다.")
    effective: dict[str, tuple[Mapping[str, Any], Any]] = {
        key: (base, _fields(base).get(key)) for key in FIELD_KEYS
    }
    # A successful amendment may replace its stated predecessor and ancestors.
    # Two overlapping sibling changes both superseding the base are ambiguous,
    # even if policy order, timestamps, or numeric values happen to agree.
    ancestors: dict[str, set[str]] = {base_id: {base_id}} if isinstance(base_id, str) else {}
    for doc_id in selected:
        amendment = index.get(doc_id)
        if amendment is None:
            global_reasons.append(f"지정한 승인변경 문서가 없습니다: {doc_id}")
            continue
        if amendment.get("role") != "amendment":
            block_document(amendment, "변경 문서의 역할이 amendment가 아닙니다.")
            warn("INVALID_AMENDMENT_ROLE", "승인변경 목록에 다른 종류의 문서가 있습니다.", doc_id)
            continue
        if not _approved(amendment):
            block_document(amendment, "변경의 승인 메타데이터가 확인되지 않아 기준값을 확정하지 않았습니다.")
            warn("UNAPPROVED_AMENDMENT", "미승인·승인자 미확인 변경은 확정 기준으로 사용하지 않습니다.", doc_id)
            continue
        if amendment.get("project_id") != project_id:
            block_document(amendment, "변경 문서가 다른 프로젝트에 속합니다.")
            warn("PROJECT_MISMATCH", "프로젝트가 다른 변경 문서입니다.", doc_id)
            continue
        predecessor = amendment.get("supersedes")
        if not isinstance(predecessor, str) or predecessor not in ancestors:
            block_document(amendment, "명시적 대체 관계가 없거나 지정 순서에서 대체 문서를 확인할 수 없습니다.")
            warn("UNRESOLVED_PRECEDENCE", "날짜나 나열 순서만으로 변경 우선순위를 추정하지 않습니다.", doc_id)
            continue
        entries = _fields(amendment)
        if not entries:
            block_document(amendment, "변경 대상 필드가 명시되지 않았습니다.")
            warn("EMPTY_AMENDMENT", "변경 범위를 확인할 수 없습니다.", doc_id)
            continue
        lineage = ancestors[predecessor]
        for key, entry in entries.items():
            if key not in effective:
                warn("UNSUPPORTED_FIELD", f"이번 비교 범위에 없는 필드입니다: {key}", doc_id)
                continue
            current_document = effective[key][0]
            if current_document.get("id") not in lineage:
                blocked[key].append("겹치는 변경들의 명시적 대체 관계가 없어 최종 기준이 모호합니다.")
                warn("OVERLAPPING_AMENDMENTS", f"{key}: 동일 항목의 변경 관계를 사람이 확인해야 합니다.", doc_id)
                continue
            effective[key] = (amendment, entry)
        ancestors[doc_id] = lineage | {doc_id}

    for doc_id, document in index.items():
        if document.get("role") != "amendment" or doc_id in selected:
            continue
        # Different-project loose attachments are not allowed to alter this
        # project's requirements.  They remain visible as an input warning.
        if document.get("project_id") != project_id:
            warn("UNSELECTED_OTHER_PROJECT", "다른 프로젝트의 미선택 변경 문서는 적용하지 않았습니다.", doc_id)
            continue
        code = "UNSELECTED_APPROVED_AMENDMENT" if _approved(document) else "UNAPPROVED_AMENDMENT"
        warn(code, "검토 정책에 포함되지 않은 변경입니다. 합의·적용 여부를 확인해야 합니다.", doc_id)
        block_document(document, "미선택 또는 미승인 변경이 있어 해당 항목의 합의 상태를 확인해야 합니다.")

    results = []
    for spec in FIELD_SPECS:
        source_document, expected_entry = effective[spec.key]
        actual_entry = _fields(handoff).get(spec.key)
        expected_source = source_for(source_document, expected_entry)
        actual_source = source_for(handoff, actual_entry)
        item: dict[str, Any] = {
            "field": spec.key, "label": spec.label, "status": "NEEDS_REVIEW",
            "reason": "", "expected": None, "actual": None,
            "sources": [source for source in (expected_source, actual_source) if source is not None],
        }
        reasons = global_reasons + blocked[spec.key]
        if reasons:
            item["reason"] = " ".join(dict.fromkeys(reasons))
            results.append(item)
            continue
        try:
            expected = normalize(spec, expected_entry)
            if expected_source is None:
                raise UncertainValue("기준값의 출처·인용문·양의 페이지 번호를 확인할 수 없습니다.")
            item["expected"] = {"value": expected.value, "unit": expected.unit, "source": expected_source}
            if is_missing(actual_entry):
                item.update(status="MISSING", reason="확정 기준값은 있으나 인계표에 값이 없습니다.")
            else:
                actual = normalize(spec, actual_entry)
                if actual_source is None:
                    raise UncertainValue("인계값의 출처·인용문·양의 페이지 번호를 확인할 수 없습니다.")
                item["actual"] = {"value": actual.value, "unit": actual.unit, "source": actual_source}
                if expected.condition != actual.condition:
                    raise UncertainValue("부품·운전조건 등 비교 조건이 같지 않거나 한쪽에서만 명시됐습니다.")
                if expected.comparable == actual.comparable:
                    item.update(status="MATCH", reason="명시된 동일 항목·조건에서 단위 정규화 후 값이 같습니다.")
                else:
                    item.update(status="MISMATCH", reason="명시된 동일 항목·조건에서 단위 정규화 후 값이 다릅니다.")
        except UncertainValue as exc:
            item.update(status="NEEDS_REVIEW", reason=str(exc))
        results.append(item)
    for reason in dict.fromkeys(global_reasons):
        warn("INVALID_REVIEW_CONTEXT", reason)
    counts = Counter(item["status"] for item in results)
    return {
        "case_id": str(case.get("case_id", "")),
        "summary": {status: counts.get(status, 0) for status in STATUSES},
        "results": results, "warnings": warnings, "limitations": list(LIMITATIONS),
        "customer_check": customer_check,
    }
