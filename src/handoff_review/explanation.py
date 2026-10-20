"""Fail-closed explanations for deterministic, source-linked review results.

The optional local model does not decide whether a specification is correct. It
may only return a structured plan that exactly repeats the engine's fixed sets
of facts. Every visible sentence is rendered by this module, never taken from
model prose. ``ai_verified`` therefore describes plan consistency, not an
independent accuracy measurement or an engineering approval.

No model is downloaded or started here. No API keys, external hosts, redirects,
environment proxies, document text, or fixture answer labels are used.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from .contracts import FIELD_KEYS, STATUSES

MODEL = "qwen3.5:2b"
TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_BYTES = 128 * 1024
MAX_REQUEST_BYTES = 64 * 1024
DEFAULT_URL = "http://127.0.0.1:11434"
NOTE = (
    "표시 설명은 코드의 고정 서식입니다. AI 선택 시 구조화된 제안의 값·상태·이유 식별자·출처가 "
    "코드 판정과 정확히 일치하는지만 확인합니다. AI 독립 정확도 평가나 공학적 승인 결과가 아닙니다."
)


class PlanRejected(ValueError):
    """The model response cannot be accepted as an exact structured plan."""


def _endpoint(raw: str) -> tuple[str, int]:
    """Allow a loopback HTTP origin only; reject credentials and URL tricks.

    ``http.client`` connects directly, ignores proxy environment variables and
    does not follow redirects. Restricting the origin here keeps optional AI
    facts on the user's own machine instead of an arbitrary environment URL.
    """
    if not isinstance(raw, str) or any(char.isspace() for char in raw):
        raise ValueError("Ollama URL은 공백 없는 로컬 HTTP 주소여야 합니다.")
    parsed = urlsplit(raw)
    try:
        port = parsed.port if parsed.port is not None else 11434
    except ValueError as exc:
        raise ValueError("Ollama 포트가 올바르지 않습니다.") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None or parsed.password is not None
        or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
        or not 1 <= port <= 65535
        or "\\" in raw
    ):
        raise ValueError("Ollama는 명시적인 loopback HTTP 주소만 사용할 수 있습니다.")
    return parsed.hostname, port


def _strict_json(raw: str | bytes) -> Any:
    """Reject duplicate keys/non-finite constants rather than silently replacing."""
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PlanRejected("JSON key 중복")
            result[key] = value
        return result

    def no_constant(value: str) -> None:
        raise PlanRejected("유한하지 않은 JSON 값")

    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=no_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PlanRejected("JSON 구조를 확인할 수 없습니다.") from exc


def _value_text(value: Any) -> str:
    if value is None:
        return "미확정 또는 없음"
    if not isinstance(value, dict) or not isinstance(value.get("value"), str) or not isinstance(value.get("unit"), str):
        raise ValueError("정규화된 코드 판정값이 필요합니다.")
    return " ".join(part for part in (value["value"], value["unit"]) if part)


def _facts(review: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Use only computed engine results, never original cases/expected labels."""
    rows = review.get("results")
    if not isinstance(rows, list) or len(rows) != len(FIELD_KEYS):
        raise ValueError(f"코드 판정 {len(FIELD_KEYS)}개 항목이 필요합니다.")
    by_field: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("field"), str):
            raise ValueError("판정 항목 구조가 올바르지 않습니다.")
        key = row["field"]
        if key not in FIELD_KEYS or key in by_field or row.get("status") not in STATUSES:
            raise ValueError("판정 항목이 중복·누락됐거나 상태가 올바르지 않습니다.")
        if not isinstance(row.get("reason"), str) or not isinstance(row.get("label"), str):
            raise ValueError("코드의 판정 이유와 항목명이 필요합니다.")
        by_field[key] = row
    if set(by_field) != set(FIELD_KEYS):
        raise ValueError(f"고정 비교 항목 {len(FIELD_KEYS)}개가 모두 필요합니다.")
    facts = []
    for key in FIELD_KEYS:
        row = by_field[key]
        sources = row.get("sources")
        if not isinstance(sources, list):
            raise ValueError("코드의 출처 목록이 필요합니다.")
        source_ids = []
        for source in sources:
            if (
                not isinstance(source, dict) or not isinstance(source.get("document_id"), str)
                or not source["document_id"]
                or type(source.get("page")) is not int or source["page"] <= 0
            ):
                raise ValueError("코드의 문서 ID·페이지 출처가 올바르지 않습니다.")
            # JSON tuple encoding avoids ambiguity when a document ID itself
            # contains punctuation such as ':' or '#'. Page changes matter.
            source_ids.append(json.dumps([source["document_id"], source["page"]], ensure_ascii=False, separators=(",", ":")))
        facts.append({
            "field": key, "status": row["status"],
            "expected_text": _value_text(row.get("expected")),
            "actual_text": _value_text(row.get("actual")),
            "reason_code": row["status"] + ":" + hashlib.sha256(row["reason"].encode("utf-8")).hexdigest()[:16],
            "source_ids": sorted(set(source_ids)),
        })
    return facts, by_field


def _render(row: dict[str, Any]) -> str:
    """Render original facts and reason exactly; uncertain rows stay uncertain."""
    label, status, reason = row["label"], row["status"], row["reason"]
    if status == "NEEDS_REVIEW":
        return f"{label}: 사람이 확인해야 합니다. {reason}"
    expected, actual = _value_text(row.get("expected")), _value_text(row.get("actual"))
    if status == "MATCH":
        return f"{label}: 기준값 {expected}과 인계값 {actual}이 같습니다. {reason}"
    if status == "MISMATCH":
        return f"{label}: 기준값 {expected}과 인계값 {actual}이 다릅니다. {reason}"
    return f"{label}: 기준값은 {expected}이며 인계표 값이 누락됐습니다. {reason}"


def _schema() -> dict[str, Any]:
    properties = {name: {"type": "string"} for name in ("field", "status", "expected_text", "actual_text", "reason_code")}
    properties["field"] = {"type": "string", "enum": list(FIELD_KEYS)}
    properties["status"] = {"type": "string", "enum": list(STATUSES)}
    properties["source_ids"] = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object", "additionalProperties": False, "required": ["items"],
        "properties": {"items": {
            "type": "array", "minItems": len(FIELD_KEYS), "maxItems": len(FIELD_KEYS),
            "items": {"type": "object", "additionalProperties": False,
                      "required": list(properties), "properties": properties},
        }},
    }


def _request_plan(facts: list[dict[str, Any]], base_url: str) -> Any:
    """Call local Ollama once with bounded output; do not retry/download/start.

    The socket timeout and remaining time budget are set before each transport
    stage/read. A deadline timer also shuts down the active socket so a peer
    dripping response headers cannot reset the timeout indefinitely. Reading
    small single buffered chunks prevents unbounded memory allocation. A
    non-200 response, including 3xx, is never followed.
    """
    host, port = _endpoint(base_url)
    payload = {
        "model": MODEL, "stream": False, "think": False, "format": _schema(),
        "messages": [
            {"role": "system", "content": (
                "Return only JSON with items. Copy all supplied structured facts exactly, once per field. "
                "Do not add prose, invent values, change a status, interpret document approval, or add sources. "
                "The supplied reason_code is an opaque identifier and must be preserved exactly."
            )},
            {"role": "user", "content": json.dumps({"items": facts}, ensure_ascii=False, separators=(",", ":"))},
        ],
        "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 180 * len(FIELD_KEYS)},
        "keep_alive": "0s",
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_REQUEST_BYTES:
        raise ValueError("로컬 AI 요청 크기 제한을 초과했습니다.")
    deadline = time.monotonic() + TIMEOUT_SECONDS
    # Resolve the approved name ourselves so a modified hosts/DNS configuration
    # cannot turn the 'localhost' spelling into a remote destination.
    connect_host = "127.0.0.1" if host == "localhost" else host
    connection = http.client.HTTPConnection(connect_host, port=port, timeout=TIMEOUT_SECONDS)
    transport_socket: socket.socket | None = None
    expired = threading.Event()

    def abort_at_deadline() -> None:
        expired.set()
        active_socket = transport_socket if transport_socket is not None else connection.sock
        if active_socket is not None:
            try:
                active_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    timer = threading.Timer(TIMEOUT_SECONDS, abort_at_deadline)
    timer.daemon = True

    def check_time() -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or expired.is_set():
            raise TimeoutError("로컬 AI 응답 제한 시간 초과")
        connection.timeout = remaining
        active_socket = transport_socket if transport_socket is not None else connection.sock
        if active_socket is not None:
            active_socket.settimeout(remaining)

    try:
        timer.start()
        connection.connect()
        # Preserve the socket reference even if HTTPConnection relinquishes it
        # to a Connection: close response before the body has finished reading.
        transport_socket = connection.sock
        check_time()
        connection.request("POST", "/api/chat", body=body, headers={"Content-Type": "application/json", "Accept": "application/json"})
        check_time()
        response = connection.getresponse()
        check_time()
        if response.status != 200:
            raise OSError(f"로컬 AI HTTP 상태 {response.status}")
        length = response.getheader("Content-Length")
        if length is not None:
            try:
                declared = int(length)
            except ValueError as exc:
                raise PlanRejected("응답 길이 형식 오류") from exc
            if declared < 0 or declared > MAX_RESPONSE_BYTES:
                raise PlanRejected("응답 크기 제한 초과")
        chunks: list[bytes] = []
        size = 0
        while True:
            check_time()
            chunk = response.read1(min(4096, MAX_RESPONSE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise PlanRejected("응답 크기 제한 초과")
        check_time()
        envelope = _strict_json(b"".join(chunks))
        if not isinstance(envelope, dict) or envelope.get("done") is not True:
            raise PlanRejected("완료된 응답이 아닙니다.")
        message = envelope.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise PlanRejected("모델의 구조화된 본문이 없습니다.")
        return _strict_json(message["content"])
    finally:
        timer.cancel()
        connection.close()


def _validate_plan(plan: Any, facts: list[dict[str, Any]]) -> dict[str, bool]:
    """Reject the whole malformed fieldset; validate valid rows independently."""
    if not isinstance(plan, dict) or set(plan) != {"items"} or not isinstance(plan["items"], list) or len(plan["items"]) != len(FIELD_KEYS):
        raise PlanRejected("설명 계획 구조가 올바르지 않습니다.")
    by_field: dict[str, dict[str, Any]] = {}
    for item in plan["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("field"), str):
            raise PlanRejected("설명 계획의 항목이 올바르지 않습니다.")
        if item["field"] not in FIELD_KEYS or item["field"] in by_field:
            raise PlanRejected("설명 계획 항목 중복·누락")
        by_field[item["field"]] = item
    if set(by_field) != set(FIELD_KEYS):
        raise PlanRejected("설명 계획의 항목 목록이 일치하지 않습니다.")
    return {fact["field"]: by_field[fact["field"]] == fact for fact in facts}


def explain_review(review: dict[str, Any], use_ai: bool = False) -> dict[str, Any]:
    """Return safe explanations; an absent/bad local model cannot alter facts.

    With ``use_ai=False`` (the default) no environment URL or network is read.
    When enabled, a failed response becomes ``unavailable``; invalid plans become
    ``rejected``. Both use the same code text as the successful path. Raw model
    output and transport error strings are intentionally not exposed to users.
    """
    facts, rows = _facts(review)
    decisions = {key: "not_requested" for key in FIELD_KEYS}
    if use_ai:
        try:
            plan = _request_plan(facts, os.environ.get("HANDOFF_OLLAMA_URL", DEFAULT_URL))
            checks = _validate_plan(plan, facts)
            decisions = {key: "accepted" if checks[key] else "rejected" for key in FIELD_KEYS}
        except PlanRejected:
            decisions = {key: "rejected" for key in FIELD_KEYS}
        except (OSError, ValueError, http.client.HTTPException):
            decisions = {key: "unavailable" for key in FIELD_KEYS}
    return {
        "llm_attempted": bool(use_ai),
        "items": [
            {"field": key, "text": _render(rows[key]),
             "origin": "ai_verified" if decisions[key] == "accepted" else "code",
             "validation_status": decisions[key]}
            for key in FIELD_KEYS
        ],
        "note": NOTE,
    }
