"""Local-only PDF handoff review: immutable uploads, explicit policy, bound evidence.

No fixture answer or case title is read by this API. Demo buttons download actual
PDFs and submit them through the same upload/parser path as file-picker uploads.
Original PDFs and page text stay in the local ignored data directory. This app
has no user authentication and must not be exposed on a public interface.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Literal
from urllib.parse import quote, urlsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import UploadFile
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .contracts import FIELD_KEYS, STATUSES
from .customers import VERSION as CUSTOMER_CHECK_VERSION, check_customers
from .review import review_case
from .explanation import explain_review
from .report import render_report

ROOT = Path(__file__).resolve().parents[2]
ROLES = {"spec_file": "customer_spec", "amendment_file": "amendment", "handoff_file": "handoff"}
FILE_LIMIT = 5 * 1024**2
BODY_LIMIT = 16 * 1024**2
NOTICE = "교육용 가상 문서 정합성 검토입니다. 설계 적합성·제품 안전·실제 승인이나 발주를 판정하지 않습니다."


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upload_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    policy_confirmed: Literal[True]
    approved_amendment_ids: list[str] = Field(max_length=8)


class ExplainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    use_ai: bool = False


class LocalRequestBoundary:
    """Reject cross-origin mutations and bound the actual streamed body, not just its header."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        if scope["method"] in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = headers.get(b"origin")
            if origin:
                parsed = urlsplit(origin.decode("ascii", errors="replace"))
                if parsed.scheme != "http" or parsed.netloc != headers.get(b"host", b"").decode("ascii", errors="replace"):
                    return await JSONResponse({"detail": "같은 로컬 화면에서 요청해 주세요."}, status_code=403)(scope, receive, send)
            try:
                if int(headers.get(b"content-length", b"0")) > BODY_LIMIT:
                    return await JSONResponse({"detail": "업로드 전체 크기는 16 MiB 이하여야 합니다."}, status_code=413)(scope, receive, send)
            except ValueError:
                return await JSONResponse({"detail": "잘못된 요청 길이입니다."}, status_code=400)(scope, receive, send)
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > BODY_LIMIT:
                    return await JSONResponse({"detail": "업로드 전체 크기는 16 MiB 이하여야 합니다."}, status_code=413)(scope, receive, send)
                body.extend(chunk)
                if not message.get("more_body", False):
                    break
            consumed = False

            async def bounded_receive():
                nonlocal consumed
                if consumed:
                    return await receive()
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            return await self.app(scope, bounded_receive, send)
        return await self.app(scope, receive, send)


def checked_id(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise HTTPException(404, "저장된 기록을 찾을 수 없습니다.")
    return value


def write_json(path: Path, value: dict) -> None:
    """Atomic update; temporary paths and destinations are created only by this service."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HTTPException(404, "저장된 기록을 찾을 수 없습니다.") from None


def parse_pdf(path: Path, role: str, mode: str = "template") -> dict:
    # No shell, model, tracing credentials or course startup scripts are used.
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TMPDIR") if k in os.environ}
    env.update(PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    try:
        args = [sys.executable, "-m", "handoff_review.pdf_worker", str(path), role]
        if mode == "text":
            args.append("text")
        process = subprocess.run(args,
                                 env=env, cwd=ROOT, capture_output=True, timeout=15, check=False)
    except subprocess.TimeoutExpired:
        raise HTTPException(422, "PDF 처리 제한시간을 넘었습니다. 작은 텍스트 PDF로 다시 시도해 주세요.") from None
    if len(process.stdout) > 4 * 1024**2:
        raise HTTPException(422, "PDF 추출 결과가 너무 큽니다.")
    try:
        parsed = json.loads(process.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(422, "PDF를 처리하지 못했습니다. 손상 여부와 지원 형식을 확인해 주세요.") from None
    if process.returncode or "error" in parsed:
        raise HTTPException(422, parsed.get("error", "PDF 내용을 읽지 못했습니다."))
    return parsed


def extract_text_upload(documents: list[dict]) -> dict:
    """Lazy import keeps deterministic parsing usable without a model server."""
    from .extraction_agent import extract_upload
    return extract_upload(documents)


def compact_document(item: dict) -> dict:
    return {**item["document"], "filename": item["filename"], "sha256": item["sha256"],
            "page_count": len(item["pages"]), "warnings": item["warnings"],
            "extraction_method": item["extraction_method"]}


def bind_evidence(result: dict, upload: dict) -> None:
    """Bind saved-page citations, keeping uncertain values and evidence separate.

    A held row gets all known-label occurrences as review-only candidates.
    They never populate expected/actual or turn a hold into a definite verdict.
    v2 deliberately does not call a held row's evidence 'complete', even when
    every displayed snippet is present on its saved page.
    """
    from .extraction_agent import original_field_evidence

    index = {item["document"]["id"]: item for item in upload["documents"]}
    for row in result["results"]:
        candidates = list(row["sources"])
        if row["status"] == "NEEDS_REVIEW":
            # Conflicts and drafts need all relevant change evidence, not only
            # the previous effective value selected by the reference engine.
            for item in upload["documents"]:
                doc = item["document"]
                field = doc.get("fields", {}).get(row["field"])
                if isinstance(field, dict) and isinstance(field.get("quote"), str):
                    candidates.append({"document_id": doc["id"], "title": doc["title"],
                                       "page": field.get("page"), "quote": field["quote"]})
                # Use code-owned original pages, not extra quotes supplied by
                # the model. Customer identity holds intentionally do not add
                # technical snippets before the documents can be compared.
                if result.get("customer_check", {}).get("can_compare") is True:
                    candidates.extend(
                        {"document_id": doc["id"], "title": doc["title"], **source}
                        for source in original_field_evidence(item, row["field"])
                    )
        proofs, seen = [], {}
        invalid_count = 0
        for source in candidates:
            item = index.get(source.get("document_id"))
            page = source.get("page")
            text = next((p["text"] for p in item["pages"] if p["page"] == page), "") if item else ""
            exact_quote = source.get("quote", "")
            verified = (isinstance(exact_quote, str) and bool(exact_quote.strip())
                        and isinstance(page, int) and not isinstance(page, bool)
                        and " ".join(exact_quote.split()) in " ".join(text.split()))
            if not verified:
                invalid_count += 1
                continue
            key = (source["document_id"], page, exact_quote)
            if key in seen:
                if source.get("evidence_kind") == "field_occurrence":
                    proofs[seen[key]].update(evidence_kind="field_occurrence",
                                             occurrence_count=source["occurrence_count"])
                continue
            seen[key] = len(proofs)
            path = f"/api/uploads/{upload['upload_id']}/documents/{quote(source['document_id'], safe='')}"
            proofs.append({**source, "verified_in_page": True, "pdf_url": f"{path}/pdf#page={page}",
                           "page_url": f"{path}/pages/{page}", "origin": "code_bound_pdf_page"})
        row["sources"] = proofs
        if invalid_count or (not proofs and row["status"] != "NEEDS_REVIEW"):
            row.update(status="NEEDS_REVIEW", reason="저장된 PDF 원문의 근거를 확인하지 못해 판정을 보류했습니다.")
        held = row["status"] == "NEEDS_REVIEW"
        row["evidence_complete"] = bool(proofs) and not invalid_count and not held
        row["evidence_check"] = {
            "version": "original-page-evidence-v2",
            "status": "unavailable" if not proofs else "review_required" if held else "linked",
            "linked_source_count": len(proofs), "invalid_source_count": invalid_count,
            "scope": "original_page_binding_not_semantic_completeness",
        }
    counts = Counter(row["status"] for row in result["results"])
    result["summary"] = {status: counts.get(status, 0) for status in STATUSES}


def create_app(data_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="수주 인계 검토", version="0.2.0", docs_url=None, redoc_url=None)
    data = (data_dir or ROOT / "uploads").resolve()
    app.state.data_dir = data
    app.state.parse_pdf = parse_pdf
    app.state.parse_text_pdf = lambda path, role: parse_pdf(path, role, "text")
    app.state.extract_upload = extract_text_upload
    app.state.extraction_tasks = {}
    app.state.ai_gate = asyncio.Semaphore(1)
    app.state.extraction_request_lock = asyncio.Lock()
    app.add_middleware(LocalRequestBoundary)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        return response

    def upload_record(upload_id: str) -> dict:
        return load_json(data / "sets" / checked_id(upload_id) / "manifest.json")

    def review_record(review_id: str) -> dict:
        return load_json(data / "reviews" / f"{checked_id(review_id)}.json")

    def extraction_record(job_id: str) -> dict:
        # Call only from the async routes, without awaiting between the read
        # and live-task check. A completed job must not be marked interrupted.
        path = data / "extractions" / f"{checked_id(job_id)}.json"
        job = load_json(path)
        if job["status"] == "running" and job_id not in app.state.extraction_tasks:
            interrupted = {"job_id": job_id, "status": "failed", "error": "서버 재시작으로 추출 기록이 중단되었습니다. 다시 요청해 주세요."}
            # Preserve known provenance; a legacy job must not acquire an
            # inferred source from a request mapping or the current browser.
            if "source_upload_id" in job:
                interrupted["source_upload_id"] = job["source_upload_id"]
            job = interrupted
            write_json(path, job)
        return job

    def metadata(record: dict) -> dict:
        return {"upload_id": record["upload_id"], "mode": record.get("mode", "template"),
                "documents": [compact_document(item) for item in record["documents"]],
                "customer_check": check_customers([item["document"] for item in record["documents"]]),
                "extraction_report": record.get("extraction_report"), "notice": NOTICE}

    def stored_document(upload_id: str, document_id: str) -> tuple[dict, dict]:
        upload = upload_record(upload_id)
        item = next((item for item in upload["documents"] if item["document"]["id"] == document_id), None)
        if item is None:
            raise HTTPException(404, "문서를 찾을 수 없습니다.")
        return upload, item

    @app.get("/api/health")
    def health():
        return {"status": "ok", "model": "qwen3.5:2b", "ai_optional": True,
                "parser": "synthetic_template_and_local_ai_text", "official_approval": False,
                "notice": NOTICE, "model_readiness_checked": False}

    @app.get("/api/samples")
    def samples():
        return {"cases": [{"id": case_id, "title": title, "mode": mode, "files": [
            {"role": role, "filename": filename, "url": f"/api/samples/{case_id}/{role}.pdf"}
            for role, filename in (("customer_spec", "01_customer_spec.pdf"),
                                   ("amendment", "02_amendment.pdf"), ("handoff", "03_handoff.pdf"))]}
            for case_id, title, mode in (("D01", "변경 미반영 가상 사례", "template"),
                                        ("D02", "변경 반영·단위 동등 가상 사례", "template"),
                                        ("C01", "고객사 혼입 가상 사례", "template"),
                                        ("V01", "다른 양식 · 변경 검토", "text_ai"),
                                        ("V02", "다른 양식 · 정보 부족 검토", "text_ai"),
                                        ("V03", "다른 양식 · 탭·설치 변경 검토", "text_ai"))]}

    @app.get("/api/samples/{case_id}/{role}.pdf")
    def sample_pdf(case_id: str, role: str):
        filenames = {"customer_spec": "01_customer_spec.pdf", "amendment": "02_amendment.pdf", "handoff": "03_handoff.pdf"}
        if case_id not in {"D01", "D02", "C01", "V01", "V02", "V03"} or role not in filenames:
            raise HTTPException(404, "가상 문서를 찾을 수 없습니다.")
        folder = "varied" if case_id.startswith("V") else "samples"
        return FileResponse(ROOT / "output" / "pdf" / folder / case_id / filenames[role],
                            media_type="application/pdf", filename=filenames[role])

    @app.post("/api/uploads", status_code=201)
    async def upload_template(request: Request):
        return await accept_upload(request, "template")

    @app.post("/api/uploads/text", status_code=201)
    async def upload_text(request: Request):
        return await accept_upload(request, "text_ai")

    async def accept_upload(request: Request, mode: str):
        data.mkdir(parents=True, exist_ok=True)
        async with request.form(max_files=3, max_fields=0) as form:
            if len(form.multi_items()) != 3 or set(form) != set(ROLES):
                raise HTTPException(422, "고객 사양서·변경 합의서·내부 인계표 PDF를 각각 하나씩 선택해 주세요.")
            upload_id = uuid4().hex
            with tempfile.TemporaryDirectory(prefix=".incoming-", dir=data) as staging:
                staging_path = Path(staging)
                documents = []
                for slot, role in ROLES.items():
                    file = form[slot]
                    if not isinstance(file, UploadFile):
                        raise HTTPException(422, "세 항목 모두 PDF 파일이어야 합니다.")
                    payload = await file.read(FILE_LIMIT + 1)
                    if len(payload) > FILE_LIMIT:
                        raise HTTPException(413, "PDF 한 개의 크기는 5 MiB 이하여야 합니다.")
                    if not payload.startswith(b"%PDF-"):
                        raise HTTPException(422, "PDF 형식의 파일만 읽을 수 있습니다.")
                    stored_name = f"{role}.pdf"
                    path = staging_path / stored_name
                    path.write_bytes(payload)
                    parser = app.state.parse_text_pdf if mode == "text_ai" else app.state.parse_pdf
                    parsed = await asyncio.to_thread(parser, path, role)
                    parsed.update(filename=Path(file.filename or "document.pdf").name[:160],
                                  stored_name=stored_name, sha256=hashlib.sha256(payload).hexdigest())
                    documents.append(parsed)
                ids = [item["document"]["id"] for item in documents]
                if len(ids) != len(set(ids)):
                    raise HTTPException(422, "문서 ID가 중복됩니다. 서로 다른 역할의 한 세트인지 확인해 주세요.")
                record = {"upload_id": upload_id, "created_at": now(), "documents": documents,
                          "mode": mode, "record_kind": "local_synthetic_upload", "notice": NOTICE}
                write_json(staging_path / "manifest.json", record)
                destination = data / "sets" / upload_id
                destination.parent.mkdir(parents=True, exist_ok=True)
                # Rename only the application-owned staging directory after all 3 files pass.
                staging_path.rename(destination)
            return metadata(record)

    @app.get("/api/uploads/{upload_id}")
    def upload_metadata(upload_id: str):
        record = upload_record(upload_id)
        return metadata(record)

    async def process_extraction(job_id: str, source: dict):
        """Persist a new snapshot: an AI run never mutates previously reviewed inputs.

        One bounded local-model run is allowed at a time. Parser metadata, PDF
        bytes and pages remain code-owned. Only guarded field proposals from
        extraction_agent are eligible to become a new input to the comparator.
        """
        job_path = data / "extractions" / f"{job_id}.json"
        try:
            result = await asyncio.to_thread(app.state.extract_upload, deepcopy(source["documents"]))
            extracted = result["documents"]
            if not isinstance(extracted, list) or len(extracted) != len(source["documents"]):
                raise ValueError("Invalid extraction fieldset")
            upload_id = uuid4().hex
            with tempfile.TemporaryDirectory(prefix=".extraction-", dir=data) as staging:
                staging_path = Path(staging)
                documents = []
                for original, proposal in zip(source["documents"], extracted):
                    if proposal["document"]["id"] != original["document"]["id"]:
                        raise ValueError("Document identity changed")
                    if not isinstance(proposal["document"].get("fields"), dict):
                        raise ValueError("Missing guarded fields")
                    item = {**original,
                            "document": {**original["document"], "fields": proposal["document"]["fields"]},
                            "warnings": proposal.get("warnings", original["warnings"]),
                            "extraction_method": "local_tool_rag"}
                    old_path = data / "sets" / source["upload_id"] / original["stored_name"]
                    original_bytes = old_path.read_bytes()
                    if hashlib.sha256(original_bytes).hexdigest() != original["sha256"]:
                        raise ValueError("Saved PDF changed since parsing")
                    (staging_path / original["stored_name"]).write_bytes(original_bytes)
                    documents.append(item)
                report = {key: value for key, value in result.items() if key != "documents"}
                record = {"upload_id": upload_id, "created_at": now(), "documents": documents,
                          "mode": "text_ai", "source_upload_id": source["upload_id"],
                          "record_kind": "local_synthetic_ai_extraction", "extraction_report": report, "notice": NOTICE}
                write_json(staging_path / "manifest.json", record)
                destination = data / "sets" / upload_id
                destination.parent.mkdir(parents=True, exist_ok=True)
                staging_path.rename(destination)
            write_json(job_path, {"job_id": job_id, "source_upload_id": source["upload_id"],
                                  "status": "completed", "finished_at": now(),
                                  "message": "근거 추출을 마쳤습니다. 적용 기준과 확인 필요 항목을 검토해 주세요.",
                                  "upload": metadata(record), "extraction": report})
        except asyncio.CancelledError:
            write_json(job_path, {"job_id": job_id, "source_upload_id": source["upload_id"],
                                  "status": "failed", "error": "서버 종료로 추출 작업이 중단되었습니다. 다시 요청해 주세요."})
            raise
        except Exception:
            write_json(job_path, {"job_id": job_id, "source_upload_id": source["upload_id"],
                                  "status": "failed", "error": "추출 결과를 저장하지 못했습니다. 기존 문서는 유지됩니다. 다시 요청해 주세요."})
        finally:
            app.state.extraction_tasks.pop(job_id, None)
            app.state.ai_gate.release()

    @app.post("/api/uploads/{upload_id}/extractions", status_code=202)
    async def start_extraction(upload_id: str, request: Request):
        source = upload_record(upload_id)
        if source.get("mode") != "text_ai":
            raise HTTPException(422, "AI 텍스트 PDF 모드로 읽은 문서를 사용해 주세요.")
        # Recompute identity from every saved attachment, not from a UI check
        # or client-supplied flag. Reject before reserving AI capacity or
        # creating a job so direct requests cannot bypass the customer hold.
        identity = check_customers([item["document"] for item in source["documents"]])
        if identity["can_compare"] is not True:
            raise HTTPException(
                409,
                "고객사 확인이 통과되지 않아 AI 추출을 시작할 수 없습니다. "
                "문서의 고객사 누락·미등록·상충·혼입 여부를 확인해 주세요.",
            )
        keys = request.headers.getlist("idempotency-key")
        key = keys[0] if keys else None
        if len(keys) > 1 or (key is not None and not re.fullmatch(r"[0-9a-f]{32}", key)):
            raise HTTPException(422, "Idempotency-Key는 소문자 16진수 32자리여야 합니다.")
        request_path = data / "extraction_requests" / f"{key}.json" if key is not None else None
        # Serialize lookup and creation within this local single-process app.
        # The mapping is durable before any model work can begin.
        async with app.state.extraction_request_lock:
            if request_path is not None and request_path.is_file():
                previous = load_json(request_path)
                if previous.get("upload_id") != upload_id:
                    raise HTTPException(409, "이미 다른 문서에 사용한 추출 요청 번호입니다. 새 요청 번호를 사용해 주세요.")
                return extraction_record(previous["job_id"])
            if app.state.ai_gate.locked():
                raise HTTPException(409, "다른 AI 작업이 진행 중입니다. 완료 후 다시 시도해 주세요.")
            await app.state.ai_gate.acquire()
            job_id = uuid4().hex
            job_path = data / "extractions" / f"{job_id}.json"
            record = {"job_id": job_id, "source_upload_id": source["upload_id"],
                      "status": "running", "created_at": now(),
                      "message": "로컬 AI가 검색 계획을 세우고 근거에서 항목을 추출하고 있습니다."}
            try:
                write_json(job_path, record)
                if request_path is not None:
                    write_json(request_path, {"upload_id": upload_id, "job_id": job_id})
                worker = process_extraction(job_id, source)
                try:
                    task = asyncio.create_task(worker)
                except Exception:
                    worker.close()
                    raise
                app.state.extraction_tasks[job_id] = task
            except Exception:
                # No worker was started: remove only this attempt's new
                # records, so a retry can safely reserve the same key.
                try:
                    if request_path is not None:
                        request_path.unlink(missing_ok=True)
                    job_path.unlink(missing_ok=True)
                finally:
                    app.state.ai_gate.release()
                raise
            return record

    @app.get("/api/extractions/{job_id}")
    async def extraction_job(job_id: str):
        return extraction_record(job_id)

    @app.get("/api/uploads/{upload_id}/documents/{document_id}/pages/{page}")
    def page_text(upload_id: str, document_id: str, page: int):
        _, item = stored_document(upload_id, document_id)
        found = next((p for p in item["pages"] if p["page"] == page), None)
        if not found:
            raise HTTPException(404, "페이지를 찾을 수 없습니다.")
        return {**found, "document_id": document_id, "sha256": item["sha256"], "source": "saved_pdf_text"}

    @app.get("/api/uploads/{upload_id}/documents/{document_id}/pdf")
    def original_pdf(upload_id: str, document_id: str):
        _, item = stored_document(upload_id, document_id)
        return FileResponse(data / "sets" / upload_id / item["stored_name"], media_type="application/pdf",
                            filename=item["stored_name"], content_disposition_type="inline")

    @app.post("/api/reviews")
    def review(body: ReviewRequest):
        upload = upload_record(body.upload_id)
        documents = [item["document"] for item in upload["documents"]]
        # A customer-identity hold is useful before expensive extraction, but
        # never converts unextracted empty fields into MISSING or MATCH.
        identity = check_customers(documents)
        if identity["can_compare"] and any(item["extraction_method"] == "text_pending_ai" for item in upload["documents"]):
            raise HTTPException(409, "먼저 AI 근거 추출을 실행해 주세요. 추출 전 자료로 누락이나 일치를 판정하지 않습니다.")
        base = next(doc for doc in documents if doc["role"] == "customer_spec")
        handoff = next(doc for doc in documents if doc["role"] == "handoff")
        amendments = {doc["id"] for doc in documents if doc["role"] == "amendment"}
        if len(body.approved_amendment_ids) != len(set(body.approved_amendment_ids)) or not set(body.approved_amendment_ids) <= amendments:
            raise HTTPException(422, "이 업로드 세트의 변경 문서 ID만 중복 없이 선택해 주세요.")
        policy = {"base_id": base["id"], "handoff_id": handoff["id"], "approved_amendment_ids": body.approved_amendment_ids}
        result = review_case({"case_id": body.upload_id, "documents": documents, "review_policy": policy})
        bind_evidence(result, upload)
        result.update(review_id=uuid4().hex, upload_id=body.upload_id, created_at=now(),
                      policy=policy, policy_confirmed=True, documents=[compact_document(item) for item in upload["documents"]],
                      record_kind="research_review_not_approval", notice=NOTICE,
                      extraction_method=("customer_identity_hold_before_extraction"
                          if any(item["extraction_method"] == "text_pending_ai" for item in upload["documents"])
                          else "local_tool_rag" if upload.get("mode") == "text_ai" else "synthetic_template_text"),
                      extraction_report=upload.get("extraction_report"), llm_used_for_verdict=False)
        result["limitations"] = [NOTICE,
            "고객사 표기는 사용자 지정 후보·별칭으로만 확인합니다. 실제 거래 관계·법인 동일성·승인을 검증하지 않으며 불명확하면 기술 비교를 보류합니다.",
            "가상 문서의 명시적 메타데이터와 지원하는 항목 표현만 처리합니다. 임의 회사 양식·스캔 OCR·CAD는 지원하지 않습니다.",
            "승인 표기는 PDF와 사용자 선택의 메타데이터이며 실제 서명·승인 권한 검증이 아닙니다.",
            "인용문은 저장된 PDF 페이지의 추출 텍스트와 대조했습니다. 설계 적합성 판단은 아닙니다.",
            "최종 수치·승인관계 판정은 코드가 수행합니다. AI 추출을 사용해도 값·원문·중복·조건 검증을 통과한 제안만 비교합니다.",
            "AI 검색은 제한된 키워드 기반입니다. 검색·추출 실패는 정보가 없다는 증명이 아니므로 확인 필요로 보류합니다."]
        result["explanations"] = explain_review(result, use_ai=False)
        write_json(data / "reviews" / f"{result['review_id']}.json", result)
        return result

    @app.get("/api/reviews/{review_id}")
    def get_review(review_id: str):
        return review_record(review_id)

    @app.post("/api/reviews/{review_id}/explain")
    async def explain(review_id: str, body: ExplainRequest):
        record = review_record(review_id)
        rows = record.get("results")
        if (not isinstance(rows, list) or len(rows) != len(FIELD_KEYS)
                or any(not isinstance(row, dict) or not isinstance(row.get("field"), str) for row in rows)
                or {row["field"] for row in rows} != set(FIELD_KEYS)):
            raise HTTPException(409, "이 저장 기록은 현재 검토 항목 구성과 다릅니다. 문서를 다시 읽고 검토한 뒤 AI 구조 확인을 요청해 주세요. 기존 기록과 내보내기는 그대로 유지됩니다.")
        identity = record.get("customer_check")
        if not isinstance(identity, dict) or identity.get("version") != CUSTOMER_CHECK_VERSION or identity.get("can_compare") is not True:
            raise HTTPException(409, "고객사가 미검증이거나 일치하지 않는 기록입니다. 원문을 확인해 다시 업로드·검토해 주세요. 기존 기록과 내보내기는 그대로 유지됩니다.")
        if app.state.ai_gate.locked():
            raise HTTPException(409, "다른 AI 확인이 진행 중입니다. 완료 후 다시 시도해 주세요.")
        async with app.state.ai_gate:
            result = await asyncio.to_thread(explain_review, record, body.use_ai)
        record["explanations"] = result
        write_json(data / "reviews" / f"{review_id}.json", record)
        return result

    @app.get("/api/reviews/{review_id}/export.json")
    def export(review_id: str):
        record = review_record(review_id)
        # Export never changes the review into an approved product/design decision.
        return JSONResponse(record, headers={"Content-Disposition": f'attachment; filename="handoff-review-{review_id[:8]}.json"'})

    @app.get("/api/reviews/{review_id}/report", response_class=HTMLResponse)
    def report(review_id: str):
        # Read only: no model, comparison, evidence rebinding or record write.
        return HTMLResponse(render_report(review_record(review_id)))

    @app.get("/")
    def index():
        return FileResponse(ROOT / "web" / "index.html", media_type="text/html")

    @app.get("/assets/{filename}")
    def asset(filename: str):
        if filename not in {"app.css", "app.js", "report.css", "report.js"}:
            raise HTTPException(404)
        return FileResponse(ROOT / "web" / filename,
                            media_type="text/css" if filename.endswith(".css") else "text/javascript")

    return app


app = create_app()
