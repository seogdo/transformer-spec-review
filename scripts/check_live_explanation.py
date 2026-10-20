#!/usr/bin/env python3
"""One disclosed live guard check using the already-installed local model.

Uses actual D01 PDF bytes and the production ingestion/review/evidence/guard
path. It does not read fixtures or manual answer labels, change any thresholds,
download a model, call external APIs, or stop an existing server. Own Ollama
processes are started on otherwise-unused 127.0.0.1:11435 and stopped in finally.
An unavailable/slow/invalid model is recorded as such, not retried until it works.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from handoff_review import review_case
from handoff_review import explanation
from handoff_review.pdf_ingest import extract_document
from handoff_review.web_app import bind_evidence
from smoke_local_llm import OwnedOllama, BASE_URL, MODEL, model_preflight, server_environment, assert_port_unused


def hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def main() -> int:
    started = datetime.now(ZoneInfo("Asia/Seoul"))
    run_id = started.strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:6]
    report_path = ROOT / "reports" / f"local-explanation-{run_id}.json"
    log_path = ROOT / "tmp" / "local-llm" / f"explanation-{run_id}.log"
    report = {
        "created_at": started.isoformat(), "kind": "single_development_pdf_local_guard_check_not_ai_accuracy",
        "model": MODEL, "chat_calls": 0, "model_downloaded": False, "external_api_used": False,
        "fixture_answers_used": False, "case": "D01 synthetic PDF documents",
        "base_url": BASE_URL, "deadline_seconds": explanation.TIMEOUT_SECONDS,
        "source_hashes": {}, "completed": False,
        "limitations": [
            "가상 개발 사례 하나의 연결 확인이며 독립 평가 또는 실제 산업 정확도가 아닙니다.",
            "AI 구조 결과가 일치해도 표시 문장은 코드 서식이며 AI 자유 설명이 아닙니다.",
            "모델 준비·로딩 시간은 AI 요청 20초 제한에 포함됩니다. 제한시간을 늘리거나 재시도하지 않습니다.",
        ],
    }
    server = None
    previous_url = os.environ.get("HANDOFF_OLLAMA_URL")
    original_request = explanation._request_plan
    transport_observation = {}

    def observed_request(facts, base_url):
        report["chat_calls"] += 1
        try:
            return original_request(facts, base_url)
        except Exception as exc:
            transport_observation.update(exception_type=type(exc).__name__, message=str(exc)[:300])
            raise

    try:
        report["model_preflight"] = model_preflight()
        executable = shutil.which("ollama")
        if not executable:
            raise FileNotFoundError("Ollama executable is not installed; no installation attempted")
        assert_port_unused()
        documents = []
        for role, filename in (("customer_spec", "01_customer_spec.pdf"), ("amendment", "02_amendment.pdf"), ("handoff", "03_handoff.pdf")):
            path = ROOT / "output" / "pdf" / "samples" / "D01" / filename
            raw = path.read_bytes()
            report["source_hashes"][str(path.relative_to(ROOT))] = hash_bytes(raw)
            item = extract_document(raw, role)
            item.update(filename=filename, stored_name=f"{role}.pdf", sha256=hash_bytes(raw))
            documents.append(item)
        for relative in ("src/handoff_review/explanation.py", "src/handoff_review/pdf_ingest.py", "src/handoff_review/review.py", "src/handoff_review/web_app.py"):
            report["source_hashes"][relative] = hash_bytes((ROOT / relative).read_bytes())
        specs = [item["document"] for item in documents]
        base = next(doc for doc in specs if doc["role"] == "customer_spec")
        handoff = next(doc for doc in specs if doc["role"] == "handoff")
        amendments = [doc["id"] for doc in specs if doc["role"] == "amendment" and doc["status"] == "approved"]
        policy = {"base_id": base["id"], "handoff_id": handoff["id"], "approved_amendment_ids": amendments}
        review = review_case({"case_id": "live-pdf-guard-check", "documents": specs, "review_policy": policy})
        bind_evidence(review, {"upload_id": "0" * 32, "documents": documents})
        before = deepcopy(review)
        baseline = explanation.explain_review(review, use_ai=False)
        report["review_summary"] = review["summary"]
        report["policy"] = policy
        report["all_evidence_complete"] = all(row["evidence_complete"] for row in review["results"])
        server = OwnedOllama(executable, server_environment(), log_path)
        report["startup"] = server.start()
        print("Own local Ollama ready. Running exactly one guarded AI request (20-second cap).", flush=True)
        os.environ["HANDOFF_OLLAMA_URL"] = BASE_URL
        explanation._request_plan = observed_request
        clock = time.perf_counter()
        result = explanation.explain_review(review, use_ai=True)
        elapsed = time.perf_counter() - clock
        report.update(
            completed=True, elapsed_ai_seconds=round(elapsed, 6), transport_observation=transport_observation,
            result=result, status_counts=dict(Counter(row["validation_status"] for row in result["items"])),
            review_unmodified=review == before,
            display_text_equals_code_baseline=[row["text"] for row in result["items"]] == [row["text"] for row in baseline["items"]],
        )
        report["guard_invariants_passed"] = (
            report["chat_calls"] == 1 and report["review_unmodified"]
            and report["display_text_equals_code_baseline"] and report["all_evidence_complete"]
            and elapsed < explanation.TIMEOUT_SECONDS + 2
        )
        report["model_preflight_after"] = model_preflight()
        report["model_manifest_unchanged"] = report["model_preflight"]["manifest_sha256"] == report["model_preflight_after"]["manifest_sha256"]
    except Exception as exc:
        report["error"] = {"exception_type": type(exc).__name__, "message": str(exc)[:800]}
    finally:
        explanation._request_plan = original_request
        if previous_url is None:
            os.environ.pop("HANDOFF_OLLAMA_URL", None)
        else:
            os.environ["HANDOFF_OLLAMA_URL"] = previous_url
        report["cleanup"] = server.stop() if server else {"cleaned_up": True, "existing_server_targeted": False, "server_started": False}
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(report_path), "completed": report["completed"], "status_counts": report.get("status_counts"), "elapsed_ai_seconds": report.get("elapsed_ai_seconds"), "guard_invariants_passed": report.get("guard_invariants_passed"), "cleanup": report["cleanup"], "error": report.get("error")}, ensure_ascii=False), flush=True)
    return 0 if report.get("guard_invariants_passed") and report["cleanup"]["cleaned_up"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
