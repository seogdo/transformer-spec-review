"""Secret-free backend configuration and a bounded OpenAI JSON transport.

Backend/model selection is a server-startup decision. The API key is obtained
only when checking server configuration or making a request; it is never part
of the immutable configuration or returned execution metadata. No SDK retries,
environment proxy, redirect, arbitrary base URL, or provider fallback is used.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import http.client
import json
import math
import os
import re
import socket
import threading
import time
from typing import Any

DEFAULT_OLLAMA_MODEL = "qwen3.5:2b"
MAX_REQUEST_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 128 * 1024
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z")
# DNS system calls cannot be cancelled portably. Bound their concurrency; a
# timed-out worker can only finish name lookup, never connect or send a request.
_DNS_SLOTS = threading.BoundedSemaphore(2)


class PlanRejected(ValueError):
    """A complete, trustworthy structured response could not be established."""


def strict_json(raw: str | bytes) -> Any:
    """Reject duplicate keys/nonfinite values without exposing raw model text."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PlanRejected("JSON key 중복")
            result[key] = value
        return result

    def no_constant(value):
        raise PlanRejected("유한하지 않은 JSON 값")

    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=no_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise PlanRejected("JSON 구조를 확인할 수 없습니다.") from None


def _api_key() -> str:
    # Whitespace/control/non-ASCII bytes are not valid Bearer credentials here.
    # A malformed value must never reach http.client's header error messages.
    key = os.environ.get("OPENAI_API_KEY", "")
    return key if key and len(key) <= 8192 and all(33 <= ord(c) <= 126 for c in key) else ""


@dataclass(frozen=True)
class AIConfig:
    provider: str
    model: str

    def __post_init__(self) -> None:
        if self.provider not in {"ollama", "openai"}:
            raise ValueError("LLM_BACKEND는 ollama 또는 openai여야 합니다.")
        if not isinstance(self.model, str) or (self.model and not _MODEL_NAME.fullmatch(self.model)):
            raise ValueError("AI 모델 설정 형식이 올바르지 않습니다.")

    def public_info(self) -> dict[str, Any]:
        """Configuration presence is not key validity, model access or readiness."""
        key_configured = bool(_api_key()) if self.provider == "openai" else None
        error = None
        if not self.model:
            error = "MISSING_OPENAI_MODEL" if self.provider == "openai" else "MISSING_OLLAMA_MODEL"
        elif self.provider == "openai" and not key_configured:
            error = "MISSING_OPENAI_API_KEY"
        return {"provider": self.provider, "model": self.model,
                "config_signature": hashlib.sha256(json.dumps(
                    {"provider": self.provider, "model": self.model},
                    sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
                "configuration_ready": error is None,
                "external_processing": self.provider == "openai",
                "model_readiness_checked": False,
                "key_configured": key_configured, "configuration_error": error}


def get_ai_config() -> AIConfig:
    """Read nonsecret startup settings; an OpenAI model is never guessed."""
    provider = os.environ.get("LLM_BACKEND", "ollama").strip().lower()
    model = (os.environ.get("OPENAI_MODEL", "") if provider == "openai"
             else os.environ.get("HANDOFF_OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL))
    return AIConfig(provider, model.strip())


def execution_info(config: AIConfig, *, injected: bool = False, requested: bool = True) -> dict[str, Any]:
    """Successful responses and validated specification fields remain distinct."""
    return {"provider": config.provider, "model": config.model,
            "execution_mode": "injected" if injected else "live_request" if requested else "not_requested",
            "external_processing": config.provider == "openai" and not injected and requested,
            "calls_attempted": 0, "calls_succeeded": 0}


def _compatible_schema(schema: Any) -> dict[str, Any]:
    """Use the conservative strict-schema subset; local validation stays stricter.

    Length/count/range constraints differ across supported model families.
    Removing them at the API boundary does not relax source or result checks.
    Property names are not interpreted as JSON-schema keywords.
    """
    if not isinstance(schema, dict):
        raise ValueError("AI JSON schema가 필요합니다.")
    removed = {"minLength", "maxLength", "pattern", "format", "minimum", "maximum",
               "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minItems", "maxItems"}
    result = {}
    for key, value in schema.items():
        if key in removed:
            continue
        if key in {"properties", "$defs"}:
            if not isinstance(value, dict):
                raise ValueError("AI JSON schema 구조가 올바르지 않습니다.")
            result[key] = {name: _compatible_schema(child) for name, child in value.items()}
        elif key == "items":
            result[key] = _compatible_schema(value)
        elif key == "anyOf":
            result[key] = [_compatible_schema(child) for child in value]
        else:
            result[key] = value
    if result.get("type") == "object":
        properties = result.get("properties", {})
        if result.get("additionalProperties") is not False or set(result.get("required", [])) != set(properties):
            raise ValueError("AI strict schema는 모든 항목과 추가 속성 금지가 필요합니다.")
    return result


def _bounded_socket(address: tuple[str, int], deadline: float, source_address=None):
    """Resolve within the same deadline, then connect only on the caller thread.

    A late DNS response is deliberately discarded. The resolver thread never
    receives the API key, request payload, connection or a callback that could
    send one. The original HTTPSConnection retains its hostname for TLS/SNI.
    """
    if address != ("api.openai.com", 443) or source_address is not None:
        raise ValueError("지원하지 않는 API 연결 주소입니다.")

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("OpenAI 연결 제한 시간 초과")
        return value

    remaining()
    if not _DNS_SLOTS.acquire(blocking=False):
        raise OSError("DNS 조회 작업 한도입니다.")
    resolved = threading.Event()
    results = []

    def resolve_only():
        try:
            results.extend(socket.getaddrinfo(address[0], address[1], 0, socket.SOCK_STREAM))
        except Exception:
            # No raw DNS error text is propagated or logged.
            pass
        finally:
            _DNS_SLOTS.release()
            resolved.set()

    worker = threading.Thread(target=resolve_only, name="handoff-api-dns", daemon=True)
    try:
        worker.start()
    except Exception:
        _DNS_SLOTS.release()
        raise OSError("DNS 조회를 시작하지 못했습니다.") from None
    if not resolved.wait(remaining()):
        raise TimeoutError("OpenAI DNS 제한 시간 초과")
    remaining()  # Do not connect even if the lookup finished just after expiry.
    for family, kind, protocol, _, sockaddr in results:
        active = None
        try:
            active = socket.socket(family, kind, protocol)
            active.settimeout(remaining())
            active.connect(sockaddr)
            # TLS handshake must use the budget remaining after TCP connect.
            active.settimeout(remaining())
            return active
        except TimeoutError:
            if active is not None:
                active.close()
            raise
        except OSError:
            if active is not None:
                active.close()
    remaining()
    raise OSError("OpenAI 서버 주소에 연결하지 못했습니다.")


def openai_request(config: AIConfig, payload: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    """Make one Chat Completions call to the fixed, TLS-verified OpenAI origin.

    ``calls_succeeded`` in callers means completed JSON received, not correct
    extraction. Refusals, truncation, unexpected tool calls and malformed JSON
    are rejected. HTTP bodies/headers/raw exceptions are never surfaced.
    ``store=False`` disables completion storage; it is not a zero-retention claim.
    """
    if config.provider != "openai" or not config.model:
        raise ValueError("OpenAI 모델을 서버 환경에 명시해야 합니다.")
    key = _api_key()
    if not key:
        raise ValueError("OpenAI API 키가 서버 환경에 설정되지 않았습니다.")
    if not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise TimeoutError("AI 응답 제한 시간이 없습니다.")
    # The caller supplies the remaining extraction budget, never a fresh 90s.
    timeout_seconds = min(float(timeout_seconds), 90.0)
    messages = payload.get("messages")
    tokens = payload.get("options", {}).get("num_predict")
    if (not isinstance(messages, list) or not messages or type(tokens) is not int or not 1 <= tokens <= 16_384
            or any(not isinstance(m, dict) or set(m) != {"role", "content"}
                   or m.get("role") not in {"system", "user"} or not isinstance(m.get("content"), str) for m in messages)):
        raise ValueError("AI 요청 메시지 또는 출력 제한이 올바르지 않습니다.")
    schema = _compatible_schema(payload.get("format"))
    if schema.get("type") != "object":
        raise ValueError("AI 응답의 최상위 schema는 object여야 합니다.")
    request = {"model": config.model, "messages": messages, "stream": False, "store": False,
               "max_completion_tokens": tokens,
               "response_format": {"type": "json_schema", "json_schema": {
                   "name": "handoff_review", "strict": True, "schema": schema}}}
    body = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(body) > MAX_REQUEST_BYTES:
        raise ValueError("AI 요청 크기 제한을 초과했습니다.")
    deadline = time.monotonic() + timeout_seconds
    connection = None
    transport_socket = None
    expired = threading.Event()

    def abort_at_deadline():
        expired.set()
        active = transport_socket if transport_socket is not None else getattr(connection, "sock", None)
        if active is not None:
            try:
                active.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def check_time():
        remaining = deadline - time.monotonic()
        if remaining <= 0 or expired.is_set():
            raise TimeoutError("AI 응답 제한 시간 초과")
        connection.timeout = remaining
        active = transport_socket if transport_socket is not None else connection.sock
        if active is not None:
            active.settimeout(remaining)

    timer = threading.Timer(timeout_seconds, abort_at_deadline)
    timer.daemon = True
    try:
        connection = http.client.HTTPSConnection("api.openai.com", port=443, timeout=timeout_seconds)
        # Replace only this connection's DNS/TCP creation, not global sockets.
        # HTTPSConnection still performs certificate verification for its host.
        connection._create_connection = lambda address, timeout=None, source_address=None: _bounded_socket(address, deadline, source_address)
        timer.start()
        connection.connect()
        transport_socket = connection.sock
        check_time()
        connection.request("POST", "/v1/chat/completions", body=body,
                           headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "Accept": "application/json"})
        check_time()
        response = connection.getresponse()
        check_time()
        if response.status != 200:
            # Never read error text: it can contain submitted text or credentials.
            labels = {401: "인증 실패", 403: "접근 거부", 429: "사용량 또는 요청 제한"}
            raise OSError("OpenAI 요청 실패: " + labels.get(response.status, "HTTP 응답 오류"))
        declared = None
        length = response.getheader("Content-Length")
        if length is not None:
            try:
                declared = int(length)
            except (ValueError, TypeError):
                raise PlanRejected("응답 길이 형식 오류") from None
            if not 0 <= declared <= MAX_RESPONSE_BYTES:
                raise PlanRejected("응답 크기 제한 초과")
        chunks, size = [], 0
        while True:
            check_time()
            chunk = response.read1(min(4096, MAX_RESPONSE_BYTES + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise PlanRejected("응답 크기 제한 초과")
            chunks.append(chunk)
        check_time()
        if declared is not None and size != declared:
            raise PlanRejected("응답 길이가 일치하지 않습니다.")
        envelope = strict_json(b"".join(chunks))
        choices = envelope.get("choices") if isinstance(envelope, dict) else None
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise PlanRejected("완료된 AI 응답이 아닙니다.")
        choice = choices[0]
        message = choice.get("message")
        if (choice.get("finish_reason") != "stop" or not isinstance(message, dict)
                or message.get("role") != "assistant" or message.get("refusal") not in (None, "")
                or message.get("tool_calls") or message.get("function_call")
                or not isinstance(message.get("content"), str)):
            raise PlanRejected("AI 응답이 거절·미완료 상태이거나 구조가 올바르지 않습니다.")
        result = strict_json(message["content"])
        if not isinstance(result, dict):
            raise PlanRejected("AI 구조화 결과가 object가 아닙니다.")
        check_time()
        return result
    except PlanRejected:
        raise
    except TimeoutError:
        raise TimeoutError("OpenAI 응답 제한 시간 초과") from None
    except Exception:
        # No server body, Bearer header or library exception text escapes here.
        raise OSError("OpenAI 연결·인증·사용량 또는 응답 오류입니다. 서버 설정을 확인하세요.") from None
    finally:
        timer.cancel()
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
