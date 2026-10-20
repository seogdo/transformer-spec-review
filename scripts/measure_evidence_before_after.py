#!/usr/bin/env python3
"""Measure F04 evidence presentation with a historical baseline and offline replay.

Run from the repository root:
    python scripts/measure_evidence_before_after.py

The before side is an archived observation, NOT a rerun of old code. The after
side replays the same three PDF byte streams and two captured structured model
responses through the current app, in a temporary store. No model/network is
allowed. This measures evidence presentation, not AI accuracy or speed.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import platform
import re
import socket
import sys
from tempfile import TemporaryDirectory
import time
from unittest.mock import Mock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient
from handoff_review import extraction_agent
from handoff_review.web_app import create_app

BASELINE = Path("reports/fresh-evaluation-20261020T134647-008a1935.result.json")
PDF_FOLDER = Path("output/pdf/fresh-evaluation/F04")
DOCUMENT_IDS = ("SPEC-F04", "AMD-F04", "HANDOFF-F04")
FIELD_KEYS = (
    "rated_power", "hv_voltage", "lv_voltage", "frequency", "phases",
    "vector_group", "cooling_type", "fan_supply_voltage",
    "tap_changer_type", "installation_type",
)
FAN = "fan_supply_voltage"
POLICY = {
    "base_id": "SPEC-F04", "handoff_id": "HANDOFF-F04",
    "approved_amendment_ids": ["AMD-F04"],
}
CONFLICT_QUOTES = (
    "냉각팬 전원전압: 400 V", "냉각팬 전원전압: 440 V",
)


class MeasurementError(ValueError):
    """The fixed measurement contract or its input identity was violated."""


def require(condition, message):
    if not condition:
        raise MeasurementError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_digest(value):
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8"))


def file_hashes(paths, root):
    return {path.relative_to(root).as_posix(): digest(path.read_bytes())
            for path in sorted(paths)}


def read_fixture(root=ROOT):
    """Select captured observations only; manual answer data is never supplied."""
    root = Path(root)
    raw = (root / BASELINE).read_bytes()
    report = json.loads(raw)
    matches = [case for case in report["cases"] if case.get("case_id") == "F04"]
    require(len(matches) == 1, "Expected exactly one historical F04 case.")
    case = matches[0]
    responses = deepcopy(case["model_responses_for_synthetic_diagnostics"])
    require(isinstance(responses, list) and len(responses) == 2
            and all(isinstance(item, dict) for item in responses),
            "Expected exactly two unmodified captured response objects.")
    documents = case["original_upload"]["documents"]
    require(len(documents) == 3 and
            {item["id"] for item in documents} == set(DOCUMENT_IDS),
            "Historical source document identity is not exact.")
    expected_hashes = {item["id"]: item["sha256"] for item in documents}
    pdfs = {identifier: (root / PDF_FOLDER / f"{identifier}.pdf").read_bytes()
            for identifier in DOCUMENT_IDS}
    require(all(digest(data) == expected_hashes[identifier]
                for identifier, data in pdfs.items()),
            "Current PDF bytes differ from historical source PDF hashes.")
    require(case["review"].get("policy") == POLICY,
            "Historical amendment policy differs from the fixed replay policy.")
    return {
        "review": deepcopy(case["review"]),
        "responses": responses,
        "pdfs": pdfs,
        "historical_pdf_hashes": expected_hashes,
        "public_baseline_sha256": digest(raw),
        "historical_run_id": report["run_id"],
        "historical_started_at": case["started_at"],
        "public_copy_notice": deepcopy(report["public_copy_notice"]),
    }


def rows_by_field(review):
    rows = review.get("results")
    require(isinstance(rows, list) and len(rows) == len(FIELD_KEYS),
            "Measurement requires exactly ten result rows.")
    require(all(isinstance(row, dict) and isinstance(row.get("field"), str)
                for row in rows), "Malformed result row.")
    require(len({row["field"] for row in rows}) == len(FIELD_KEYS)
            and {row["field"] for row in rows} == set(FIELD_KEYS),
            "Duplicate, missing or unknown result field.")
    return {row["field"]: row for row in rows}


def value_view(row, side):
    require(side in row, f"Missing {side} value key.")
    value = row[side]
    if value is None:
        return None
    require(isinstance(value, dict) and isinstance(value.get("value"), str)
            and isinstance(value.get("unit"), str),
            f"Malformed normalized {side} value.")
    return {"value": value["value"], "unit": value["unit"]}


def fan_evidence(row):
    """Count unique page/quote identities, not duplicate source list entries."""
    sources = row.get("sources")
    require(isinstance(sources, list), "Fan sources must be a list.")
    proofs = set()
    for proof in sources:
        require(isinstance(proof, dict), "Malformed evidence entry.")
        if proof.get("document_id") != "HANDOFF-F04":
            continue
        page, quote = proof.get("page"), proof.get("quote")
        require(type(page) is int and page > 0 and isinstance(quote, str),
                "Malformed handoff evidence page or quote.")
        proofs.add(("HANDOFF-F04", page, quote))
    distinct_quotes = {quote for _, _, quote in proofs}
    return {
        "handoff_unique_source_count": len(proofs),
        "handoff_sources": [{"document_id": doc, "page": page, "quote": quote}
                            for doc, page, quote in sorted(proofs)],
        "conflict_quotes_displayed": [quote for quote in CONFLICT_QUOTES
                                     if quote in distinct_quotes],
        "conflict_quote_kind_count": sum(quote in distinct_quotes
                                        for quote in CONFLICT_QUOTES),
        "evidence_complete": row.get("evidence_complete"),
        "evidence_check": deepcopy(row.get("evidence_check")),
    }


def compare_reviews(before, after):
    old, new = rows_by_field(before), rows_by_field(after)
    changes = []
    for key in FIELD_KEYS:
        left = {"status": old[key].get("status"),
                "expected": value_view(old[key], "expected"),
                "actual": value_view(old[key], "actual")}
        right = {"status": new[key].get("status"),
                 "expected": value_view(new[key], "expected"),
                 "actual": value_view(new[key], "actual")}
        require(isinstance(left["status"], str) and isinstance(right["status"], str)
                and left["status"] in {"MATCH", "MISMATCH", "MISSING", "NEEDS_REVIEW"}
                and right["status"] in {"MATCH", "MISMATCH", "MISSING", "NEEDS_REVIEW"},
                "Unsupported result status.")
        changes.append({
            "field": key, "before": left, "after": right,
            "status_unchanged": left["status"] == right["status"],
            "values_unchanged": all(left[side] == right[side]
                                    for side in ("expected", "actual")),
        })
    old_fan, new_fan = fan_evidence(old[FAN]), fan_evidence(new[FAN])
    checks = {
        "all_ten_statuses_unchanged": all(row["status_unchanged"] for row in changes),
        "all_ten_normalized_value_pairs_unchanged": all(row["values_unchanged"] for row in changes),
        "summary_unchanged": before.get("summary") == after.get("summary"),
        "policy_unchanged": before.get("policy") == after.get("policy") == POLICY,
        "fan_stays_held_with_null_actual": (
            old[FAN]["status"] == new[FAN]["status"] == "NEEDS_REVIEW"
            and old[FAN]["actual"] is None and new[FAN]["actual"] is None
            and value_view(old[FAN], "expected") ==
            value_view(new[FAN], "expected") == {"value": "400", "unit": "V"}
        ),
        "handoff_distinct_sources_one_to_two": (
            old_fan["handoff_sources"] == [
                {"document_id": "HANDOFF-F04", "page": 1, "quote": CONFLICT_QUOTES[0]}]
            and new_fan["handoff_sources"] == [
                {"document_id": "HANDOFF-F04", "page": 1, "quote": quote}
                for quote in CONFLICT_QUOTES]
        ),
        "both_original_conflict_quotes_visible_after": (
            old_fan["conflict_quotes_displayed"] == [CONFLICT_QUOTES[0]]
            and new_fan["conflict_quotes_displayed"] == list(CONFLICT_QUOTES)
        ),
        "evidence_completeness_true_to_false": (
            old_fan["evidence_complete"] is True
            and new_fan["evidence_complete"] is False
        ),
        "after_evidence_explicitly_requires_review": (
            isinstance(new_fan["evidence_check"], dict)
            and new_fan["evidence_check"].get("status") == "review_required"
        ),
    }
    return {
        "checks": checks,
        "passed": all(checks.values()),
        "status_unchanged_count": sum(row["status_unchanged"] for row in changes),
        "normalized_value_pairs_unchanged_count": sum(row["values_unchanged"] for row in changes),
        "field_count": len(changes),
        "fan_before": old_fan, "fan_after": new_fan,
        "field_comparisons": changes,
    }


def require_response(response, status):
    require(response.status_code == status,
            f"In-process API returned {response.status_code}, expected {status}.")
    return response.json()


def replay_current(fixture):
    """Run in-process ASGI requests only; block both model and socket transports."""
    captured = deepcopy(fixture["responses"])
    supplied_response_hashes = []

    def supply_captured_response(_payload, _timeout):
        index = len(supplied_response_hashes)
        require(index < len(captured), "Unexpected additional model-response request.")
        response = deepcopy(captured[index])
        supplied_response_hashes.append(json_digest(response))
        return response

    replay = Mock(side_effect=supply_captured_response)
    blocked_model = Mock(side_effect=MeasurementError("Real model transport forbidden."))
    blocked_network = Mock(side_effect=MeasurementError("Network access forbidden."))
    source_proofs = []
    with TemporaryDirectory(prefix="f04-evidence-measurement-") as directory:
        store = Path(directory)
        app = create_app(store)
        app.state.extract_upload = lambda documents: extraction_agent.extract_upload(
            documents, request_fn=replay)
        with patch.object(extraction_agent, "_local_request", blocked_model), \
             patch.object(socket, "create_connection", blocked_network), \
             patch.object(socket.socket, "connect", blocked_network), \
             patch.object(socket.socket, "connect_ex", blocked_network), \
             TestClient(app) as client:
            files = [(slot, (f"{identifier}.pdf", fixture["pdfs"][identifier], "application/pdf"))
                     for slot, identifier in zip(
                         ("spec_file", "amendment_file", "handoff_file"), DOCUMENT_IDS)]
            original = require_response(client.post("/api/uploads/text", files=files), 201)
            submitted = require_response(client.post(
                f"/api/uploads/{original['upload_id']}/extractions"), 202)
            deadline = time.monotonic() + 10
            while True:
                job = require_response(client.get(f"/api/extractions/{submitted['job_id']}"), 200)
                if job["status"] != "running":
                    break
                require(time.monotonic() < deadline, "Offline replay did not finish.")
                time.sleep(0.01)
            require(job["status"] == "completed", "Offline replay did not complete.")
            require(replay.call_count == 2, "The two captured responses were not consumed exactly once.")
            derived = job["upload"]
            after = require_response(client.post("/api/reviews", json={
                "upload_id": derived["upload_id"], "policy_confirmed": True,
                "approved_amendment_ids": POLICY["approved_amendment_ids"],
            }), 200)
            original_after = require_response(client.get(f"/api/uploads/{original['upload_id']}"), 200)
            export = require_response(client.get(f"/api/reviews/{after['review_id']}/export.json"), 200)
            pages = {}
            stored_pdf_checks = []
            for upload in (original, derived):
                for identifier in DOCUMENT_IDS:
                    data = client.get(f"/api/uploads/{upload['upload_id']}/documents/{identifier}/pdf")
                    stored_pdf_checks.append(
                        data.status_code == 200 and digest(data.content) ==
                        fixture["historical_pdf_hashes"][identifier])
            for row in after["results"]:
                for proof in row["sources"]:
                    key = (proof["document_id"], proof["page"])
                    if key not in pages:
                        page = require_response(client.get(
                            f"/api/uploads/{original['upload_id']}/documents/{key[0]}/pages/{key[1]}"), 200)
                        pages[key] = page["text"]
                    quote = proof.get("quote")
                    source_proofs.append({
                        "field": row["field"], "document_id": key[0], "page": key[1],
                        "quote": quote,
                        "present_in_original_page": (
                            isinstance(quote, str) and bool(quote.strip()) and quote in pages[key]),
                    })
            observations = {
                "actual_model_transport_calls": blocked_model.call_count,
                "network_connection_attempts": blocked_network.call_count,
                "captured_response_replay_calls": replay.call_count,
                "captured_responses_supplied_unchanged": (
                    supplied_response_hashes ==
                    [json_digest(response) for response in fixture["responses"]]
                ),
                "supplied_response_sha256": supplied_response_hashes,
                "original_upload_metadata_unchanged": original == original_after,
                "derived_upload_is_separate": derived["upload_id"] != original["upload_id"],
                "job_source_upload_is_original": job.get("source_upload_id") == original["upload_id"],
                "original_and_derived_pdf_checks": stored_pdf_checks,
                "review_export_matches": export == after,
                "source_page_checks": source_proofs,
            }
        require(blocked_model.call_count == 0 and blocked_network.call_count == 0,
                "An external transport was attempted; result is invalid.")
    observations["temporary_store_removed"] = not store.exists()
    return after, observations


def measure(root=ROOT):
    root = Path(root)
    input_paths = [root / BASELINE] + [
        root / PDF_FOLDER / f"{identifier}.pdf" for identifier in DOCUMENT_IDS]
    source_paths = list((root / "src" / "handoff_review").glob("*.py")) + [
        root / "scripts" / "measure_evidence_before_after.py"]
    inputs_before = file_hashes(input_paths, root)
    code_before = file_hashes(source_paths, root)
    fixture = read_fixture(root)
    responses_before = json_digest(fixture["responses"])
    after, replay = replay_current(fixture)
    comparison = compare_reviews(fixture["review"], after)
    inputs_after = file_hashes(input_paths, root)
    code_after = file_hashes(source_paths, root)
    checks = {
        **comparison["checks"],
        "input_files_unchanged": inputs_before == inputs_after,
        "current_code_unchanged_during_replay": code_before == code_after,
        "captured_response_objects_unchanged": (
            replay["captured_responses_supplied_unchanged"]
            and json_digest(fixture["responses"]) == responses_before),
        "zero_actual_model_calls": replay["actual_model_transport_calls"] == 0,
        "zero_network_connection_attempts": replay["network_connection_attempts"] == 0,
        "two_captured_response_replays": replay["captured_response_replay_calls"] == 2,
        "original_metadata_preserved": replay["original_upload_metadata_unchanged"],
        "separate_derived_upload": replay["derived_upload_is_separate"],
        "job_bound_to_original_upload": replay["job_source_upload_is_original"],
        "original_and_derived_pdf_bytes_preserved": all(replay["original_and_derived_pdf_checks"]),
        "export_preserved": replay["review_export_matches"],
        "all_displayed_quotes_bound_to_original_pages": (
            bool(replay["source_page_checks"])
            and all(item["present_in_original_page"] for item in replay["source_page_checks"])),
        "temporary_store_removed": replay["temporary_store_removed"],
    }
    return {
        "schema_version": "evidence-before-after-v1",
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "case_id": "F04", "passed": all(checks.values()),
        "scope": "Synthetic fixed-input evidence presentation, not AI quality or speed.",
        "before_origin": {
            "kind": "historical_saved_baseline",
            "old_implementation_rerun": False,
            "path": BASELINE.as_posix(),
            "public_copy_sha256": fixture["public_baseline_sha256"],
            "historical_run_id": fixture["historical_run_id"],
            "historical_case_started_at": fixture["historical_started_at"],
            "public_copy_notice": fixture["public_copy_notice"],
        },
        "after_origin": {
            "kind": "current_code_captured_response_replay",
            "transport": "in_process_ASGI_TestClient",
            "live_model_run": False,
            "manual_answers_used_for_replay_or_comparison": False,
            "runtime": {
                "python": platform.python_version(),
                "packages": {name: version(name)
                             for name in ("fastapi", "starlette", "httpx", "pypdf", "python-multipart")},
            },
            "captured_responses_sha256": responses_before,
            "code_sha256_before": code_before, "code_sha256_after": code_after,
        },
        "input_identity": {
            "historical_pdf_sha256": fixture["historical_pdf_hashes"],
            "current_pdfs_match_historical_hashes": True,
            "files_sha256_before": inputs_before, "files_sha256_after": inputs_after,
            "policy": POLICY,
        },
        "checks": checks, "comparison": comparison, "replay_observations": replay,
        "limitations": [
            "Before is an archived observation; only after is executed now. This is not two historical code versions rerun side by side.",
            "Both conflicting values already existed in the captured model response. The measured change is evidence presentation, not better AI extraction.",
            "No AI accuracy, inference speed, task-time saving, production safety or course bonus eligibility is established.",
            "One authored synthetic case is not an independent evaluation or a real-company document sample.",
            "The redacted public report hash is not the original historical report or freeze hash.",
            "Quote-to-page checks establish literal presence, not semantic completeness or engineering validity.",
            "Replay request payloads are generated by current code; only PDF bytes, captured responses and amendment policy are held fixed.",
            "Temporary records and their local UI links are discarded; historical records are never rewritten.",
        ],
    }


def write_report_exclusive(path, report):
    """Never replace a prior measurement."""
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="New reports/evidence-before-after-*.json path.")
    arguments = parser.parse_args(argv)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = arguments.output or ROOT / "reports" / f"evidence-before-after-{stamp}-{uuid4().hex[:8]}.json"
    if not path.is_absolute():
        path = ROOT / path
    try:
        require(path.parent.resolve() == (ROOT / "reports").resolve()
                and re.fullmatch(r"evidence-before-after-[A-Za-z0-9_-]+\.json", path.name),
                "Output must be a new reports/evidence-before-after-*.json file.")
        require(not path.exists(), "Output already exists; refusing to overwrite.")
        report = measure()
        write_report_exclusive(path, report)
    except (MeasurementError, OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f"Measurement failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({
        "output": path.relative_to(ROOT).as_posix(), "passed": report["passed"],
        "actual_model_calls": report["replay_observations"]["actual_model_transport_calls"],
        "handoff_conflict_quote_kinds": [
            report["comparison"]["fan_before"]["conflict_quote_kind_count"],
            report["comparison"]["fan_after"]["conflict_quote_kind_count"]],
        "statuses_unchanged": report["comparison"]["status_unchanged_count"],
        "normalized_value_pairs_unchanged": report["comparison"]["normalized_value_pairs_unchanged_count"],
    }, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
