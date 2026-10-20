#!/usr/bin/env python3
"""Disclosed local AI extraction integration checks, never a holdout.

Reads actual synthetic PDF bytes through the web app's isolated reader. The
production two-call extraction job performs its own keyword retrieval. Manual
expected states are opened only after all AI runs finish, never put in prompts.
An existing model is required. No model download, paid API, or remote request.
"""
from __future__ import annotations

import argparse
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
from fastapi.testclient import TestClient
from handoff_review.web_app import create_app
from smoke_local_llm import OwnedOllama, BASE_URL, MODEL, model_preflight, server_environment


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hold-preview", action="store_true",
                        help="Keep only this owned model server until Ctrl+C for browser QA")
    parser.add_argument("--cases", nargs="+", choices=("V01", "V02", "V03"),
                        default=["V01", "V02", "V03"])
    args = parser.parse_args()
    stamp = datetime.now(ZoneInfo("Asia/Seoul")).isoformat()
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:6]
    destination = ROOT / "reports" / f"local-extraction-{run_id}.json"
    report = {"created_at": stamp, "kind": "authored_development_pdf_integration_not_holdout",
              "model": MODEL, "external_api_used": False, "model_downloaded": False,
              "manual_answers_passed_to_model": False, "deadline_seconds_per_case": 90,
              "cases": [], "source_hashes": {}, "completed": False}
    server = None
    previous = os.environ.get("HANDOFF_OLLAMA_URL")

    def save():
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    try:
        report["model_preflight"] = model_preflight()
        executable = shutil.which("ollama")
        if not executable:
            raise RuntimeError("Existing Ollama executable not found; no installation attempted")
        server = OwnedOllama(executable, server_environment(), ROOT / "tmp/local-llm" / f"extraction-{run_id}.log")
        report["startup"] = server.start()
        os.environ["HANDOFF_OLLAMA_URL"] = BASE_URL
        for filename in ("contracts.py", "customers.py", "pdf_ingest.py", "extraction_agent.py", "text_pdf.py", "web_app.py", "review.py"):
            path = ROOT / "src/handoff_review" / filename
            report["source_hashes"][str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
        # Default storage retains the original PDFs and immutable extraction
        # snapshots so the actual browser can restore precisely these results.
        app = create_app()
        model_responses = []
        def observed_extraction(documents):
            from handoff_review import extraction_agent
            def request(payload, timeout_seconds):
                result = extraction_agent._local_request(payload, timeout_seconds)
                # Synthetic diagnostic output only; never modify the model's
                # result or pass expected answers back into a later prompt.
                model_responses.append(result)
                return result
            return extraction_agent.extract_upload(documents, request_fn=request)
        app.state.extract_upload = observed_extraction
        with TestClient(app) as client:
            for case_id in dict.fromkeys(args.cases):
                model_responses = []
                files = []
                for slot, name in (("spec_file", "01_customer_spec.pdf"), ("amendment_file", "02_amendment.pdf"), ("handoff_file", "03_handoff.pdf")):
                    path = ROOT / "output/pdf/varied" / case_id / name
                    raw = path.read_bytes()
                    report["source_hashes"][str(path.relative_to(ROOT))] = hashlib.sha256(raw).hexdigest()
                    files.append((slot, (name, raw, "application/pdf")))
                response = client.post("/api/uploads/text", files=files)
                response.raise_for_status()
                upload = response.json()
                started = time.monotonic()
                response = client.post(f"/api/uploads/{upload['upload_id']}/extractions")
                response.raise_for_status()
                job_id = response.json()["job_id"]
                print(f"{case_id}: actual local AI extraction started; at most 2 calls / 90 seconds.", flush=True)
                while True:
                    response = client.get(f"/api/extractions/{job_id}")
                    response.raise_for_status()
                    job = response.json()
                    if job["status"] != "running":
                        break
                    if time.monotonic() - started > 100:
                        raise TimeoutError("Extraction job exceeded bounded polling time")
                    time.sleep(.25)
                observation = {"case_id": case_id, "elapsed_seconds": round(time.monotonic() - started, 3),
                               "original_upload_id": upload["upload_id"], "job": job,
                               "model_responses_for_synthetic_diagnostics": model_responses}
                if job["status"] == "completed":
                    derived = job["upload"]
                    amendments = [doc["id"] for doc in derived["documents"] if doc["role"] == "amendment"]
                    response = client.post("/api/reviews", json={"upload_id": derived["upload_id"],
                        "policy_confirmed": True, "approved_amendment_ids": amendments})
                    response.raise_for_status()
                    observation["review"] = response.json()
                report["cases"].append(observation)
                save()
                print(json.dumps({"case": case_id, "elapsed_seconds": observation["elapsed_seconds"],
                    "extraction_status": job.get("extraction", {}).get("status"),
                    "accepted_fields": job.get("extraction", {}).get("accepted_fields"),
                    "summary": observation.get("review", {}).get("summary"),
                    "review_id": observation.get("review", {}).get("review_id")}, ensure_ascii=False), flush=True)
        # This post-run comparison file is not accessed by the application.
        report["manual_expected_after_run"] = json.loads((ROOT / "output/pdf/varied/expected-results.json").read_text())
        expected = {item["case_id"]: item["expected"]
                    for item in report["manual_expected_after_run"]["cases"]}
        comparisons = []
        for observation in report["cases"]:
            actual = {row["field"]: row["status"] for row in observation.get("review", {}).get("results", [])}
            target = expected[observation["case_id"]]
            comparisons.append({
                "case_id": observation["case_id"], "passed": actual == target,
                "differences": {key: {"expected": target.get(key), "actual": actual.get(key)}
                                for key in sorted(set(target) | set(actual))
                                if target.get(key) != actual.get(key)},
            })
        report["manual_comparison_after_run"] = comparisons
        report["integration_passed"] = bool(comparisons) and all(item["passed"] for item in comparisons)
        report["completed"] = True
        report["model_manifest_unchanged"] = report["model_preflight"]["manifest_sha256"] == model_preflight()["manifest_sha256"]
        save()
        print(f"Report: {destination}", flush=True)
        if args.hold_preview:
            print("Owned model ready for local browser QA. Ctrl+C cleans up only owned processes.", flush=True)
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        report["preview_finished"] = True
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)[:600]}
    finally:
        report["cleanup"] = server.stop() if server else {"cleaned_up": True, "server_started": False}
        if previous is None:
            os.environ.pop("HANDOFF_OLLAMA_URL", None)
        else:
            os.environ["HANDOFF_OLLAMA_URL"] = previous
        save()
        print(json.dumps({"report": str(destination), "completed": report["completed"], "cleanup": report["cleanup"], "error": report.get("error")}, ensure_ascii=False), flush=True)
    return 0 if report["completed"] and report.get("integration_passed") and report["cleanup"].get("cleaned_up") else 1


if __name__ == "__main__":
    raise SystemExit(main())
