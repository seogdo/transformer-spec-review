"use strict";

const $ = (id) => document.getElementById(id);
const STATUS = {MATCH: "일치", MISMATCH: "불일치", MISSING: "누락", NEEDS_REVIEW: "확인 필요"};
const STATUS_CLASS = {MATCH: "match", MISMATCH: "mismatch", MISSING: "missing", NEEDS_REVIEW: "needs-review"};
const CURRENT_FIELDS = Object.freeze({rated_power: "정격용량", hv_voltage: "고압측 정격전압", lv_voltage: "저압측 정격전압", frequency: "정격주파수", phases: "상수", vector_group: "결선 그룹", cooling_type: "냉각 방식", fan_supply_voltage: "팬 보조전원 정격전압", tap_changer_type: "탭절환기 방식", installation_type: "설치 구분"});
const ROLES = {customer_spec: "고객 기술사양서", spec: "고객 기술사양서", amendment: "기술 변경 합의서", handoff: "내부 수주 인계표"};
const FILE_FIELDS = {customer_spec: "spec_file", spec: "spec_file", spec_file: "spec_file", amendment: "amendment_file", amendment_file: "amendment_file", handoff: "handoff_file", handoff_file: "handoff_file"};
const MAX_BYTES = 5 * 1024 * 1024;
const STORAGE = {review: "handoff-review:review_id", upload: "handoff-review:upload_id", job: "handoff-review:job_id", extractionRequest: "handoff-review:extraction_request"};
const state = {health: null, samples: [], upload: null, review: null, selected: null, filter: "all", busy: false, retry: null, explanations: new Map(), mode: "template", extraction: null, job: null, extractionRequest: null, extracting: false};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function setText(id, text) { $(id).textContent = String(text ?? ""); }
function textOf(value) {
  if (typeof value === "string") return value;
  if (value && typeof value === "object") return value.message || value.reason || JSON.stringify(value);
  return String(value ?? "");
}
function safeUrl(value) {
  if (typeof value !== "string" || !value.trim()) throw new Error("원문 주소가 제공되지 않았습니다.");
  const url = new URL(value, window.location.origin);
  if (url.origin !== window.location.origin || !["http:", "https:"].includes(url.protocol)) throw new Error("이 서버에서 제공한 문서 주소만 열 수 있습니다.");
  return url;
}
function remember(key, value) { try { value ? localStorage.setItem(key, String(value)) : localStorage.removeItem(key); } catch (_) { /* Storage can be disabled; the current review remains usable. */ } }
function recalled(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }
function clearExtractionRequest() {
  state.extractionRequest = null; remember(STORAGE.extractionRequest, null);
}
function clearStoredExtractionJob() {
  state.job = null; remember(STORAGE.job, null);
}
function clearExtractionJob() {
  clearStoredExtractionJob(); clearExtractionRequest();
}
function rememberExtractionJob(jobId, uploadId) {
  state.job = jobId;
  remember(STORAGE.job, JSON.stringify({upload_id: uploadId, job_id: jobId}));
}
function storedExtractionJob(raw, uploadId) {
  if (!raw) return null;
  let record;
  try { record = JSON.parse(raw); } catch (_) { record = raw; }
  // Legacy plain IDs may only be inspected: pollExtraction still requires
  // the server's source_upload_id before it applies or renders that job.
  if (typeof record === "string" && /^[a-f0-9]{32}$/.test(record)) return record;
  return record && record.upload_id === uploadId
    && typeof record.job_id === "string" && /^[a-f0-9]{32}$/.test(record.job_id) ? record.job_id : null;
}
function verifyExtractionSource(job, uploadId) {
  if (job.source_upload_id !== uploadId) {
    // Preserve a valid request key for the current upload: a stale job from
    // another tab must not erase the current upload's deduplication identity.
    clearStoredExtractionJob();
    throw new Error("추출 작업의 원본 문서를 현재 문서와 대조할 수 없어 복원을 중단했습니다. 현재 문서는 변경하지 않았습니다. 원문을 확인한 뒤 같은 요청을 다시 확인하거나 문서를 다시 읽어 주세요. 새 요청은 자동으로 보내지 않습니다.");
  }
}
function validExtractionRequest(request, uploadId) {
  return Boolean(request && request.upload_id === uploadId
    && typeof request.key === "string" && /^[a-f0-9]{32}$/.test(request.key));
}
function restoreExtractionRequest(uploadId) {
  let request;
  try { request = JSON.parse(recalled(STORAGE.extractionRequest)); } catch (_) { /* Invalid local state is not a request identity. */ }
  if (validExtractionRequest(request, uploadId)) state.extractionRequest = {upload_id: uploadId, key: request.key};
  else clearExtractionRequest();
}
function extractionRequestKey(uploadId) {
  if (!validExtractionRequest(state.extractionRequest, uploadId)) {
    if (typeof globalThis.crypto?.randomUUID !== "function") throw new Error("안전한 요청 식별자를 만들 수 없습니다. 최신 브라우저의 localhost 화면에서 다시 시도해 주세요.");
    state.extractionRequest = {upload_id: uploadId, key: crypto.randomUUID().replaceAll("-", "")};
    if (!validExtractionRequest(state.extractionRequest, uploadId)) {
      clearExtractionRequest();
      throw new Error("추출 요청 식별자를 만들지 못했습니다. 다시 시도해 주세요.");
    }
  }
  // Persist before POST: an unreadable/lost response must not create a new
  // identity on a manual retry. The record is bound to this exact input upload.
  const serialized = JSON.stringify(state.extractionRequest);
  try {
    localStorage.setItem(STORAGE.upload, uploadId);
    localStorage.setItem(STORAGE.extractionRequest, serialized);
    if (localStorage.getItem(STORAGE.upload) !== uploadId
        || localStorage.getItem(STORAGE.extractionRequest) !== serialized) throw new Error("Storage readback differs.");
  } catch (_) {
    throw new Error("추출 요청 복구 정보를 브라우저에 저장하고 확인할 수 없어 AI 요청을 보내지 않았습니다. 브라우저의 사이트 저장소 사용을 허용하고 저장 공간을 확인한 뒤 다시 시도해 주세요.");
  }
  return state.extractionRequest.key;
}

async function api(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeout || 120000);
  try {
    const response = await fetch(safeUrl(path), {...options, signal: controller.signal, credentials: "same-origin"});
    if (!response.ok) {
      let detail = `서버 응답 오류 (${response.status})`;
      try {
        const body = await response.json();
        detail = Array.isArray(body.detail) ? body.detail.map((row) => row.msg || textOf(row)).join(" / ") : textOf(body.detail || body.message || detail);
      } catch (_) { /* Keep the HTTP error when the response is not JSON. */ }
      const error = new Error(detail); error.status = response.status;
      throw error;
    }
    if (options.blob) return await response.blob();
    try { return await response.json(); } catch (_) { throw new Error("서버 응답을 읽을 수 없습니다. 다시 시도해 주세요."); }
  } catch (error) {
    if (error.name === "AbortError" || error instanceof TypeError) {
      const failure = new Error(error.name === "AbortError"
        ? "응답 대기 시간이 지났습니다. 서버 상태를 확인한 뒤 다시 시도해 주세요."
        : "서버에 연결할 수 없습니다. 서버가 실행 중인지 확인해 주세요.");
      failure.status = 0; throw failure;
    }
    throw error;
  } finally { clearTimeout(timer); }
}

function showError(error, retry) {
  setText("error-text", error.message || String(error));
  state.retry = retry || null;
  $("error-retry").hidden = !retry;
  $("error-box").hidden = false;
}
function clearError() { $("error-box").hidden = true; state.retry = null; }
function hasCurrentReviewFields(review) {
  const rows = review?.results;
  const count = Object.keys(CURRENT_FIELDS).length;
  return Array.isArray(rows) && rows.length === count
    && rows.every((row) => row && typeof row.field === "string" && Object.prototype.hasOwnProperty.call(CURRENT_FIELDS, row.field))
    && new Set(rows.map((row) => row.field)).size === count;
}
function hasCustomerCheck(record) {
  const check = record?.customer_check;
  return Boolean(check && check.version === "customer-identity-v1"
    && ["MATCH", "MISMATCH", "NEEDS_REVIEW"].includes(check.status)
    && typeof check.can_compare === "boolean");
}
function customerComparisonAllowed(record) {
  return hasCustomerCheck(record) && record.customer_check.can_compare === true && record.customer_check.status === "MATCH";
}
function hasCustomerHold(record) {
  return hasCustomerCheck(record) && record.customer_check.can_compare === false;
}
function extractionRequired() {
  return pendingExtraction() && !hasCustomerHold(state.upload);
}
function canExplainReview(review) {
  return hasCurrentReviewFields(review) && customerComparisonAllowed(review);
}
function explainBlockedMessage(review) {
  if (!hasCurrentReviewFields(review)) return "이 저장 기록은 현재 항목 구성과 달라 AI 구조 확인을 사용할 수 없습니다. 문서를 다시 읽고 검토해 주세요.";
  if (!hasCustomerCheck(review)) return "고객사 미검증 과거기록은 AI 구조 확인을 사용할 수 없습니다. 고객사 표기가 있는 문서를 다시 읽고 검토해 주세요.";
  return "고객사 확인이 통과되지 않아 AI 구조 확인을 사용할 수 없습니다. 문서의 고객사 원문과 혼입·누락·모호한 표기를 확인해 주세요.";
}
function controls() {
  $("upload-button").disabled = state.busy;
  for (const id of ["spec-file", "amendment-file", "handoff-file", "health-retry", "policy-confirmed", "export-confirmed", "upload-mode"]) $(id).disabled = state.busy;
  $("demo-d01").disabled = state.busy || !state.samples.some((row) => row.id === "D01");
  $("demo-d02").disabled = state.busy || !state.samples.some((row) => row.id === "D02");
  $("demo-c01").disabled = state.busy || !state.samples.some((row) => row.id === "C01");
  $("demo-v01").disabled = state.busy || !state.samples.some((row) => row.id === "V01");
  $("demo-v02").disabled = state.busy || !state.samples.some((row) => row.id === "V02");
  $("demo-v03").disabled = state.busy || !state.samples.some((row) => row.id === "V03");
  $("policy-confirmed").disabled = state.busy || extractionRequired();
  $("review-button").disabled = state.busy || !state.upload || extractionRequired() || !$("policy-confirmed").checked;
  setText("review-button", state.upload && !customerComparisonAllowed(state.upload) ? "확인 필요 사항 보기" : "10개 항목 검토하기 →");
  $("extract-button").disabled = state.busy || !pendingExtraction() || hasCustomerHold(state.upload);
  $("explain-button").disabled = state.busy || !canExplainReview(state.review);
  $("explain-button").title = state.review && !canExplainReview(state.review) ? explainBlockedMessage(state.review) : "";
  $("export-button").disabled = state.busy || !state.review || !$("export-confirmed").checked;
  for (const input of $("amendment-options").querySelectorAll("input")) input.disabled = state.busy || input.dataset.approved !== "true";
}
function setBusy(busy, message) {
  state.busy = busy;
  $("task-status").hidden = !busy;
  setText("task-message", message || "");
  $("workspace").setAttribute("aria-busy", String(busy));
  controls();
}
async function perform(message, action, retry) {
  if (state.busy) return;
  clearError();
  setBusy(true, message);
  try { await action(); } catch (error) { showError(error, retry); }
  finally { setBusy(false); }
}
function moveTo(id) { $(id).scrollIntoView({behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start"}); }
function steps(stage) {
  for (let index = 1; index <= 3; index++) {
    const node = $(`step-${index}`);
    node.classList.toggle("active", index === stage);
    node.classList.toggle("done", index < stage);
    if (index === stage) node.setAttribute("aria-current", "step"); else node.removeAttribute("aria-current");
  }
}
function warningList(container, warnings) {
  container.replaceChildren();
  const rows = Array.isArray(warnings) ? warnings : [];
  container.hidden = !rows.length;
  if (rows.length) {
    const list = element("ul");
    rows.forEach((warning) => list.append(element("li", "", textOf(warning))));
    container.append(list);
  }
}
function clearReviewState() {
  state.review = null; state.selected = null; state.explanations.clear();
  remember(STORAGE.review, null);
  $("export-confirmed").checked = false; $("ai-note").hidden = true;
  renderReview();
}
function pendingExtraction() {
  return Boolean(state.upload?.documents?.some((doc) => doc.extraction_method === "text_pending_ai"));
}
function renderMode() {
  $("upload-mode").value = state.mode;
  $("text-mode-notice").hidden = state.mode !== "text_ai";
  setText("mode-help", state.mode === "text_ai" ? "먼저 PDF의 문서 정보를 읽고, 다음 단계에서 AI 근거 검색·추출을 별도로 요청합니다." : "제공된 정형 PDF의 항목을 읽어 바로 승인 적용을 확인합니다.");
}
function resetInputState(message) {
  clearError();
  state.upload = null; state.extraction = null; state.extracting = false;
  remember(STORAGE.upload, null); clearExtractionJob(); clearReviewState();
  $("policy-confirmed").checked = false; renderDocuments(); renderExtraction(); steps(1); controls();
  if (message) { setText("restore-note", message); $("restore-note").hidden = false; }
}

function countValue(value) {
  if (Number.isInteger(value) && value >= 0) return value;
  if (Array.isArray(value)) return value.length;
  return null;
}
function extractionSummary(value) {
  if (typeof value === "string") return value.slice(0, 600);
  if (value && typeof value === "object") {
    for (const key of ["message", "reason", "detail", "code"]) if (typeof value[key] === "string") return value[key].slice(0, 600);
  }
  return "처리 중 확인할 내용이 있습니다.";
}
function toolKind(entry) {
  const name = String(entry?.tool || entry?.type || entry?.name || "").toLowerCase();
  if (name === "plan_search") return "검색 계획";
  if (name === "scope_documents") return "문서 범위 확인";
  if (name === "local_model") return "로컬 AI 응답 확인";
  if (name.includes("search") || name.includes("retrieve")) return "근거 검색";
  if (name.includes("extract")) return "항목 추출";
  if (name.includes("valid") || name.includes("check")) return "추출값 확인";
  return "처리 단계";
}
function renderTrace(container, report) {
  container.replaceChildren();
  if (!report || typeof report !== "object") return;
  const trace = Array.isArray(report.trace) ? report.trace : [];
  const statusLabels = {completed: "추출 완료", accepted: "추출값 확인 완료", success: "추출 완료", partial: "일부 항목 확인", unavailable: "AI 연결 사용 불가", failed: "추출 실패", rejected: "추출값 확인 불통과", running: "진행 중"};
  const facts = element("dl", "trace-facts");
  const searches = Number.isInteger(report.search_calls) && report.search_calls >= 0 ? report.search_calls : trace.filter((entry) => entry?.tool === "search_documents").length;
  for (const [label, value] of [["처리 상태", statusLabels[report.status] || "결과 확인 필요"], ["근거 검색", `${searches}회`], ["AI 제안 필드", countValue(report.proposed_fields)], ["확인 통과 필드", countValue(report.accepted_fields)], ["모델 호출", countValue(report.llm_calls)]]) facts.append(element("dt", "", label), element("dd", "", value ?? "정보 없음"));
  container.append(facts);
  if (trace.length) {
    const list = element("ol", "trace-list");
    for (const entry of trace) {
      const row = element("li"); row.append(element("strong", "", toolKind(entry)));
      const description = typeof entry?.message === "string" ? extractionSummary(entry.message) : typeof entry?.reason === "string" ? extractionSummary(entry.reason) : null;
      if (description) row.append(element("span", "", description));
      const result = statusLabels[entry?.status] || ({ok: "완료", error: "실패", skipped: "건너뜀"})[entry?.status];
      if (result) row.append(element("span", "trace-status", result));
      list.append(row);
    }
    container.append(list);
  }
  if (report.error || report.message) container.append(element("p", "trace-warning", extractionSummary(report.error || report.message)));
  if (Array.isArray(report.warnings) && report.warnings.length) {
    const list = element("ul", "trace-warnings");
    report.warnings.forEach((warning) => list.append(element("li", "", extractionSummary(warning)))); container.append(list);
  }
  container.append(element("p", "small muted", "표시된 수는 문서에서 추출하고 확인한 필드 수입니다. 요구사항의 승인이나 제품 합격을 의미하지 않습니다."));
}
function renderExtraction() {
  const pending = pendingExtraction();
  const customerHold = hasCustomerHold(state.upload);
  const visible = Boolean(state.upload && (state.mode === "text_ai" || pending || state.extraction));
  $("extraction-section").hidden = !visible;
  $("extract-button").hidden = !pending || customerHold;
  if (!visible) return;
  setText("extract-button", state.extracting ? "AI 추출 진행 중…" : state.job ? "추출 진행 상태 다시 확인" : state.extractionRequest ? "같은 추출 요청 다시 확인" : "AI로 근거 찾고 항목 추출");
  let message = "문서 정보를 읽었습니다. AI 추출을 완료한 다음 승인 적용을 확인하세요.";
  if (customerHold) message = "고객사 확인이 통과되지 않아 AI 추출이 필요하지 않습니다. 고객사 확인 결과와 문서 적용 정보를 읽고 확인란을 체크한 뒤 ‘확인 필요 사항 보기’로 보류 결과를 확인하세요. 이 체크는 고객사 불일치를 해제하지 않습니다.";
  else if (state.extracting) message = "원문 근거 검색과 항목 추출을 진행하고 있습니다. 완료될 때까지 검토는 시작할 수 없습니다.";
  else if (state.extraction?.status === "unavailable") message = "AI 연결을 사용할 수 없어 항목을 확정하지 못했습니다. 승인 적용을 확인한 뒤 확인 필요 항목으로 비교할 수 있습니다. AI를 다시 시도하려면 같은 PDF 3종을 다시 선택하고 ‘문서 읽기’를 누르거나, 사용한 예시 버튼으로 문서를 다시 불러온 뒤 ‘AI로 근거 찾고 항목 추출’을 눌러 주세요. 자동으로 재시도하지 않습니다.";
  else if (!pending) message = "추출 결과를 새 문서 버전으로 저장했습니다. 원문과 승인 적용을 사람이 다시 확인하세요.";
  else if (state.job) message = "추출 완료를 아직 확인하지 못했습니다. 진행 상태 다시 확인은 기존 작업을 조회하며 새 모델 호출을 시작하지 않습니다.";
  else if (state.extractionRequest) message = "이전 추출 요청의 접수 여부를 아직 확인하지 못했습니다. ‘같은 추출 요청 다시 확인’을 누르면 같은 요청 식별자로 확인합니다. 새 요청은 자동으로 보내지 않습니다.";
  else if (state.extraction?.status === "failed") message = "항목 추출을 완료하지 못했습니다. 아래 실패 이유를 확인한 뒤 다시 요청할 수 있습니다.";
  setText("extraction-message", message);
  $("extraction-message").classList.toggle("fallback", customerHold || ["unavailable", "failed", "rejected"].includes(state.extraction?.status));
  $("extraction-details").hidden = !state.extraction;
  renderTrace($("extraction-trace"), state.extraction);
}

async function loadHealth() {
  try {
    const health = await api("/api/health", {timeout: 15000});
    state.health = health;
    const online = ["ok", "ready", "healthy"].includes(health.status);
    $("health-dot").className = `dot ${online ? "online" : "offline"}`;
    setText("health-label", online ? "검토 서버 연결됨" : `서버 상태: ${health.status || "확인 필요"}`);
    setText("model-label", health.model ? `선택 모델 · ${health.model}` : "로컬 AI 모델 상태를 확인할 수 없습니다.");
  } catch (error) {
    state.health = null;
    $("health-dot").className = "dot offline";
    setText("health-label", "서버 연결 확인 필요");
    setText("model-label", "서버 연결 후 로컬 모델 상태를 확인합니다.");
    throw error;
  }
}
async function loadSamples() {
  const payload = await api("/api/samples", {timeout: 15000});
  if (!Array.isArray(payload.cases)) throw new Error("예시 문서 목록의 형식이 올바르지 않습니다.");
  state.samples = payload.cases;
  setText("samples-note", state.samples.length ? "" : "현재 사용할 수 있는 예시 문서가 없습니다.");
  controls();
}

function checkFile(file, label) {
  if (!file) throw new Error(`${label} PDF를 선택해 주세요.`);
  if (!file.name.toLowerCase().endsWith(".pdf")) throw new Error(`${label}는 PDF 파일이어야 합니다.`);
  if (!file.size || file.size > MAX_BYTES) throw new Error(`${label}는 비어 있지 않은 5 MiB 이하 파일이어야 합니다.`);
}
function acceptUpload(upload, mode, extraction = null) {
  if (!upload.upload_id || !Array.isArray(upload.documents) || !upload.documents.length) throw new Error("문서 읽기 결과가 올바르지 않습니다.");
  state.upload = upload;
  state.mode = upload.mode === "text_ai" || mode === "text_ai" ? "text_ai" : "template";
  state.extraction = extraction || upload.extraction_report || null;
  clearExtractionJob();
  state.review = null;
  state.explanations.clear();
  state.selected = null;
  remember(STORAGE.upload, upload.upload_id);
  remember(STORAGE.review, null);
  $("restore-note").hidden = true;
  $("policy-confirmed").checked = false;
  $("export-confirmed").checked = false;
  $("ai-note").hidden = true;
  renderDocuments();
  renderMode(); renderExtraction();
  renderReview();
  steps(2);
}
async function uploadFormData(form, mode = state.mode) {
  const upload = await api(mode === "text_ai" ? "/api/uploads/text" : "/api/uploads", {method: "POST", body: form});
  acceptUpload(upload, mode);
  moveTo("policy");
}
function uploadSelected() {
  return perform("PDF 3종의 문서와 원문을 읽고 있습니다…", async () => {
    const form = new FormData();
    for (const [id, key, label] of [["spec-file", "spec_file", "고객 기술사양서"], ["amendment-file", "amendment_file", "기술 변경 합의서"], ["handoff-file", "handoff_file", "내부 수주 인계표"]]) {
      const file = $(id).files[0]; checkFile(file, label); form.append(key, file);
    }
    await uploadFormData(form);
  }, uploadSelected);
}
function useSample(id) {
  return perform("예시 PDF 3종을 불러와 문서를 읽고 있습니다…", async () => {
    const sample = state.samples.find((row) => row.id === id);
    if (!sample || !Array.isArray(sample.files)) throw new Error("예시 문서를 찾을 수 없습니다. 서버 연결을 다시 확인해 주세요.");
    const mode = sample.mode === "text_ai" ? "text_ai" : "template";
    const files = await Promise.all(sample.files.map(async (item) => {
      const key = FILE_FIELDS[item.role];
      if (!key) throw new Error("예시 문서 역할이 올바르지 않습니다.");
      const blob = await api(item.url, {blob: true, timeout: 30000});
      const file = new File([blob], item.filename || `${key}.pdf`, {type: "application/pdf"});
      checkFile(file, ROLES[item.role] || item.role);
      return {key, file};
    }));
    if (files.length !== 3 || new Set(files.map((item) => item.key)).size !== 3) throw new Error("예시는 서로 다른 역할의 PDF 3종이 필요합니다.");
    const form = new FormData(); files.forEach(({key, file}) => form.append(key, file));
    await uploadFormData(form, mode);
  }, () => useSample(id));
}

async function pollExtraction(jobId) {
  const originalUploadId = state.upload?.upload_id;
  if (!originalUploadId) throw new Error("추출할 문서를 먼저 불러와 주세요.");
  const deadline = performance.now() + 180000;
  while (performance.now() < deadline) {
    const remaining = deadline - performance.now();
    let job;
    try {
      job = await api(`/api/extractions/${encodeURIComponent(jobId)}`, {timeout: Math.min(15000, Math.max(1, remaining))});
    } catch (error) {
      if (error.status === 404) {
        clearExtractionJob();
        const message = "저장된 추출 기록을 찾을 수 없어 다시 요청이 필요합니다. 서버 상태를 확인한 뒤 ‘다시 시도’ 또는 AI 추출 버튼을 눌러 주세요. 새 요청은 자동으로 보내지 않습니다.";
        state.extraction = {status: "failed", error: message};
        const missing = new Error(message); missing.status = 404; throw missing;
      }
      throw error;
    }
    verifyExtractionSource(job, originalUploadId);
    rememberExtractionJob(jobId, originalUploadId);
    if (job.status === "completed") {
      if (!job.upload?.upload_id || job.upload.upload_id === originalUploadId) throw new Error("새 문서 버전의 추출 결과를 확인할 수 없습니다.");
      if (job.upload.documents?.some((doc) => doc.extraction_method === "text_pending_ai")) throw new Error("추출 완료 응답에 아직 처리 전인 문서가 있습니다.");
      acceptUpload(job.upload, "text_ai", job.extraction || job.upload.extraction_report);
      setText("restore-note", "추출 결과를 새 문서 버전으로 불러왔습니다. 이전 승인 적용 확인은 초기화되었습니다."); $("restore-note").hidden = false;
      return;
    }
    if (job.status === "failed") {
      clearExtractionJob();
      state.extraction = {...(job.extraction || {}), status: "failed", error: extractionSummary(job.error || job.message || "추출 작업을 완료하지 못했습니다.")};
      throw new Error(extractionSummary(job.error || job.message || "추출 작업을 완료하지 못했습니다."));
    }
    if (job.status !== "running") throw new Error("알 수 없는 추출 작업 상태입니다.");
    setText("extraction-message", typeof job.message === "string" ? extractionSummary(job.message) : "근거 검색과 항목 추출을 진행하고 있습니다…");
    const delay = Math.min(1500, Math.max(0, deadline - performance.now()));
    await new Promise((resolve) => setTimeout(resolve, delay));
  }
  throw new Error("3분 안에 추출 완료를 확인하지 못했습니다. 진행 상태 다시 확인으로 기존 작업을 이어서 조회할 수 있습니다.");
}
function extractUpload() {
  if (!pendingExtraction() || hasCustomerHold(state.upload)) return;
  return perform("AI 근거 검색·항목 추출 상태를 확인하고 있습니다…", async () => {
    state.extracting = true; $("policy-confirmed").checked = false; clearReviewState(); renderExtraction(); controls();
    try {
      if (!state.job) {
        const sourceUploadId = state.upload.upload_id;
        const key = extractionRequestKey(sourceUploadId);
        let job;
        try {
          job = await api(`/api/uploads/${encodeURIComponent(sourceUploadId)}/extractions`, {method: "POST", headers: {"Idempotency-Key": key}, timeout: 30000});
        } catch (error) {
          if (error.status === 404) {
            clearExtractionJob();
            const message = "문서 또는 추출 기록을 찾을 수 없어 다시 요청이 필요합니다. 서버 상태를 확인한 뒤 명시적으로 다시 시도해 주세요. 문서 기록도 없으면 같은 PDF 3종을 ‘문서 읽기’로 다시 읽거나 예시 버튼으로 불러와 주세요. 새 요청은 자동으로 보내지 않습니다.";
            state.extraction = {status: "failed", error: message};
            const missing = new Error(message); missing.status = 404; throw missing;
          }
          throw error;
        }
        verifyExtractionSource(job, sourceUploadId);
        if (!job.job_id || !["running", "completed", "failed"].includes(job.status)) throw new Error("추출 작업을 시작하지 못했습니다.");
        rememberExtractionJob(job.job_id, sourceUploadId);
      }
      await pollExtraction(state.job);
    } finally { state.extracting = false; renderExtraction(); controls(); }
  }, extractUpload);
}

function customerNameText(value) {
  if (typeof value === "string" && value.trim()) return value;
  if (Array.isArray(value)) {
    const names = value.filter((name) => typeof name === "string" && name.trim());
    return names.length ? `상충 표기: ${names.join(" / ")}` : "표기 없음";
  }
  return "표기 없음";
}
function customerStatusText(value) {
  return ({recognized: "등록명 식별", missing: "표기 누락", unknown: "등록명 확인 불가", ambiguous: "상충·모호한 표기"})[value] || "확인 정보 없음";
}
function customerSourceDetails(sources) {
  if (!Array.isArray(sources) || !sources.length) return null;
  const details = element("details", "customer-sources");
  details.append(element("summary", "", "고객사 표기 근거"));
  for (const source of sources) {
    if (!source || typeof source.quote !== "string") continue;
    const group = element("div", "customer-source");
    group.append(element("span", "small muted", Number.isInteger(source.page) && source.page > 0 ? `${source.page}쪽` : "페이지 확인 필요"),
      element("blockquote", "source-quote", source.quote));
    details.append(group);
  }
  return details;
}
function renderCustomerCheck(container, record, storedReview = false) {
  container.replaceChildren();
  const checked = hasCustomerCheck(record);
  const passed = customerComparisonAllowed(record);
  container.classList.toggle("customer-check-warning", !passed);
  const heading = element("div", "customer-check-heading");
  heading.append(element("h3", "", "고객사 기본정보 확인"), element("span", `status ${passed ? "match" : "needs-review"}`,
    !checked ? (storedReview ? "고객사 미검증 과거기록" : "고객사 확인 기록 없음") : passed ? "고객사 표기 일치" : record.customer_check.status === "MISMATCH" ? "고객사 혼입 · 보류" : "고객사 확인 필요 · 보류"));
  container.append(heading);
  if (!checked) {
    container.append(element("p", "customer-check-message", storedReview
      ? "고객사 미검증 과거기록입니다. 저장된 기술사양 판정과 내보내기는 그대로 유지하며 고객사 일치로 간주하지 않습니다. AI 구조 확인은 사용할 수 없습니다. 고객사 표기가 있는 문서를 다시 읽고 검토하세요."
      : "이 문서 읽기 기록에는 현재 고객사 확인 결과가 없습니다. 고객사 표기가 있는 문서를 다시 읽어 확인하세요. 사람의 체크로 고객사 미확인을 해제할 수 없습니다."));
  } else {
    const check = record.customer_check;
    if (typeof check.message === "string") container.append(element("p", "customer-check-message", check.message));
    if (!passed) container.append(element("p", "customer-hold-note", "고객사 불일치·누락·모호함이 있으면 기술사양 10개 항목의 비교를 보류하고 확인 필요로 남깁니다. 담당자 체크나 승인 변경 선택으로 우회하지 않습니다. AI 구조 확인도 사용하지 않습니다."));
    if (storedReview && Array.isArray(check.documents)) {
      const list = element("div", "customer-record-grid");
      for (const doc of check.documents) {
        const card = element("article", "customer-record");
        card.append(element("h4", "", `${ROLES[doc.role] || doc.role || "문서"} · ${doc.document_id || "ID 확인 필요"}`));
        const facts = element("dl", "document-facts");
        for (const [label, value] of [["원문 표기", customerNameText(doc.raw_name)], ["등록명", doc.canonical_name || "확인되지 않음"], ["식별 상태", customerStatusText(doc.status)]]) facts.append(element("dt", "", label), element("dd", "", value));
        card.append(facts);
        if (typeof doc.message === "string") card.append(element("p", "small muted", doc.message));
        const sources = customerSourceDetails(doc.sources); if (sources) card.append(sources);
        list.append(card);
      }
      container.append(list);
    }
  }
  container.append(element("p", "customer-boundary", "기술사양 10개 항목과 별도로 문서의 고객사 표기와 정해진 등록명·별칭을 대조합니다. 실제 고객 관계나 문서 진위·승인 권한을 인증하는 기능이 아니며 고객사·별칭을 이 화면에서 변경할 수 없습니다."));
}
function renderDocuments() {
  $("policy-empty").hidden = Boolean(state.upload);
  $("policy-content").hidden = !state.upload;
  if (!state.upload) { setText("upload-badge", "문서 대기"); return; }
  setText("upload-badge", `${state.upload.documents.length}종 문서 읽음`);
  const cards = $("document-cards"); cards.replaceChildren();
  const warnings = [];
  for (const doc of state.upload.documents) {
    const card = element("article", "document-card");
    card.append(element("span", "document-role", ROLES[doc.role] || doc.role), element("h3", "", doc.title || doc.id));
    const facts = element("dl", "document-facts");
    const method = ({text_pending_ai: "AI 근거 추출 전", local_tool_rag: "AI 근거 추출 처리됨"})[doc.extraction_method] || "정형 문서 읽기";
    const customer = Array.isArray(state.upload.customer_check?.documents) ? state.upload.customer_check.documents.find((item) => item.document_id === doc.id) : null;
    for (const [label, value] of [["문서 ID", doc.id], ["프로젝트", doc.project_id], ["고객사 원문", customerNameText(doc.customer_name ?? customer?.raw_name)], ["고객사 등록명", customer?.canonical_name || "확인되지 않음"], ["고객사 식별", customerStatusText(customer?.status)], ["승인 표기", doc.status === "approved" ? "승인됨 (입력 표기)" : "미승인 / 확인 필요"], ["승인자 표기", doc.approved_by || "표기 없음"], ["대체 대상", doc.supersedes || "해당 없음"], ["문서 분량", Number.isInteger(doc.page_count) ? `${doc.page_count}쪽` : "확인 필요"], ["처리 방식", method]]) facts.append(element("dt", "", label), element("dd", "", value || "확인 필요"));
    card.append(facts);
    const customerSources = customerSourceDetails(customer?.sources || doc.customer_sources); if (customerSources) card.append(customerSources);
    cards.append(card);
    if (Array.isArray(doc.warnings)) warnings.push(...doc.warnings.map((warning) => `${doc.title || doc.id}: ${textOf(warning)}`));
  }
  renderCustomerCheck($("policy-customer-check"), state.upload);
  warningList($("document-warnings"), warnings);
  const options = $("amendment-options"); options.replaceChildren();
  const amendments = state.upload.documents.filter((doc) => doc.role === "amendment");
  for (const doc of amendments) {
    const label = element("label", "amendment-option");
    const input = element("input"); input.type = "checkbox"; input.value = doc.id;
    input.dataset.approved = String(doc.status === "approved"); input.checked = doc.status === "approved"; input.disabled = doc.status !== "approved";
    input.addEventListener("change", () => {
      $("policy-confirmed").checked = false;
      clearReviewState(); steps(2); controls();
      setText("restore-note", "적용할 변경 문서 선택이 바뀌었습니다. 승인 적용을 확인한 뒤 다시 검토하세요.");
      $("restore-note").hidden = false;
    });
    const text = element("span", "", `${doc.title || doc.id} (${doc.id})`);
    text.append(element("small", "", doc.status === "approved" ? `${doc.supersedes || "대체 대상 미확인"} → ${doc.id}` : "미승인 변경은 확정 기준으로 선택하지 않습니다."));
    label.append(input, text); options.append(label);
  }
  if (!amendments.length) options.append(element("p", "small muted", "선택할 변경 문서가 없습니다."));
}

function runReview() {
  if (!state.upload || extractionRequired() || state.extracting || !$("policy-confirmed").checked) return;
  return perform(customerComparisonAllowed(state.upload) ? `승인 적용 관계와 ${Object.keys(CURRENT_FIELDS).length}개 항목을 비교하고 있습니다…` : "고객사 확인 필요 사항과 보류 결과를 불러오고 있습니다…", async () => {
    const selected = [...$("amendment-options").querySelectorAll("input:checked")].map((input) => input.value);
    const review = await api("/api/reviews", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({upload_id: state.upload.upload_id, policy_confirmed: true, approved_amendment_ids: selected})});
    acceptReview(review);
    moveTo("results");
  }, runReview);
}
function acceptReview(review) {
  if (!review.review_id || !Array.isArray(review.results) || !review.summary) throw new Error("검토 결과의 형식이 올바르지 않습니다.");
  state.review = review; state.filter = "all"; state.selected = null; state.explanations.clear();
  $("export-confirmed").checked = false; $("ai-note").hidden = true;
  remember(STORAGE.review, review.review_id); remember(STORAGE.upload, review.upload_id);
  renderReview(); steps(3);
}
function displayValue(value, missing) {
  if (!value || value.value === null || value.value === undefined) return missing ? "기재 없음" : "—";
  return `${String(value.value)} ${value.unit || ""}`.trim();
}
function statusNode(status) { return element("span", `status ${STATUS_CLASS[status] || "unknown"}`, STATUS[status] || "상태 확인 필요"); }
function evidencePresentation(row) {
  const check = row.evidence_check;
  const held = row.status === "NEEDS_REVIEW";
  const boundary = "원문 인용 확인과 판정 확정은 다릅니다. 원문이 연결되어도 승인 관계·조건·값이 불명확하면 판정을 보류합니다.";
  if (!check || check.version !== "original-page-evidence-v2") {
    return {
      label: "과거 근거 기록", tone: "legacy",
      note: `이 저장 기록에는 현재 방식의 원문 대조 정보가 없습니다. 과거 기록의 근거 연결 표시는 근거 완전성이나 판정 확정을 보증하지 않습니다. 기존 판정과 JSON은 변경하지 않습니다.${held ? ` ${boundary}` : ""}`,
    };
  }
  const valid = ["linked", "review_required", "unavailable"].includes(check.status)
    && Number.isSafeInteger(check.linked_source_count) && check.linked_source_count >= 0
    && Number.isSafeInteger(check.invalid_source_count) && check.invalid_source_count >= 0;
  if (!valid) return {
    label: held ? "원문 건수 미확인 · 판정 보류" : "근거 정보 확인 필요", tone: "incomplete",
    note: `원문 대조 정보의 형식이 올바르지 않아 연결 건수를 표시하지 않습니다. ${boundary}`,
  };
  const count = check.linked_source_count;
  const confirmed = !held && check.status === "linked" && count > 0
    && check.invalid_source_count === 0 && row.evidence_complete === true;
  const label = held ? `원문 ${count}건 · 판정 보류`
    : confirmed ? `원문 ${count}건 연결`
    : count > 0 ? `원문 ${count}건 · 확인 필요` : "원문 연결 없음 · 확인 필요";
  const note = held ? boundary
    : "저장된 페이지에서 인용문을 대조한 범위입니다. 원문 연결은 문서 승인·조건 해석·설계 적합성을 보증하지 않습니다.";
  return {
    label, tone: confirmed ? "linked" : "incomplete",
    note: `${note}${check.invalid_source_count ? ` 원문 대조에서 확인하지 못한 근거 ${check.invalid_source_count}건이 있습니다.` : ""}`,
  };
}
function renderReview() {
  $("results-empty").hidden = Boolean(state.review); $("results-content").hidden = !state.review;
  if (!state.review) { setText("review-badge", "검토 대기"); return; }
  const review = state.review;
  const fields = new Set(review.results.map((row) => row.field));
  const missing = Object.keys(CURRENT_FIELDS).filter((field) => !fields.has(field));
  const currentScope = hasCurrentReviewFields(review);
  setText("review-badge", `${review.results.length}개 항목${!currentScope || !hasCustomerCheck(review) ? " 저장 기록" : customerComparisonAllowed(review) ? " 검토" : " 보류"}`);
  renderCustomerCheck($("review-customer-check"), review, true);
  setText("results-caption", `저장된 ${review.results.length}개 항목의 요구값과 인계값 비교 결과`);
  $("review-field-note").hidden = currentScope;
  setText("review-field-note", missing.length
    ? `이 기록은 ${review.results.length}개 항목을 담고 있으며, 현재 10개 항목 중 ${missing.map((field) => CURRENT_FIELDS[field]).join("·")} 검토가 포함되지 않았습니다. 현재 범위 전체의 검토 완료를 의미하지 않습니다. AI 구조 확인을 사용하려면 새 항목이 있는 문서를 다시 읽고 검토하세요. 기존 기록과 내보내기는 그대로 보존합니다.`
    : "이 기록의 항목 구성이 현재 10개 항목 범위와 다릅니다. 저장된 결과를 그대로 표시하며, 현재 범위 전체의 검토 완료로 해석하지 않습니다. AI 구조 확인을 사용하려면 문서를 다시 읽고 검토하세요.");
  setText("export-description", `저장된 ${review.results.length}개 항목의 결과와 근거를 그대로 JSON으로 내려받습니다. 누락된 검토 항목을 자동 추가하지 않습니다. 저장은 설계 승인·발주·제작 지시를 의미하지 않습니다.`);
  const summary = $("summary-cards"); summary.replaceChildren();
  for (const status of Object.keys(STATUS)) {
    const card = element("div", `summary-card ${STATUS_CLASS[status]}`);
    const count = review.summary[status];
    card.append(element("span", "summary-label", STATUS[status]), element("span", "summary-count", Number.isInteger(count) ? count : "—"));
    summary.append(card);
  }
  warningList($("review-warnings"), review.warnings);
  const extraction = review.extraction_report || (state.upload?.upload_id === review.upload_id ? state.extraction : null);
  $("review-extraction-details").hidden = !extraction;
  renderTrace($("review-extraction-trace"), extraction);
  const list = $("limitations-list"); list.replaceChildren();
  (review.limitations || []).forEach((limitation) => list.append(element("li", "", textOf(limitation))));
  renderTable(); controls();
}
function renderTable() {
  if (!state.review) return;
  const visible = state.review.results.filter((row) => state.filter === "all" || row.status !== "MATCH");
  if (!visible.some((row) => row.field === state.selected)) state.selected = visible[0]?.field || null;
  $("filter-all").classList.toggle("active", state.filter === "all"); $("filter-attention").classList.toggle("active", state.filter === "attention");
  $("filter-all").setAttribute("aria-pressed", String(state.filter === "all")); $("filter-attention").setAttribute("aria-pressed", String(state.filter === "attention"));
  const body = $("results-body"); body.replaceChildren();
  for (const row of visible) {
    const tr = element("tr", row.field === state.selected ? "selected" : "");
    const name = element("td"); const button = element("button", "row-select", row.label || row.field); button.type = "button";
    button.setAttribute("aria-label", `${row.label || row.field} 근거 보기`);
    name.append(button);
    const status = element("td"); status.append(statusNode(row.status));
    const info = evidencePresentation(row);
    const evidence = element("td"); const evidenceLabel = element("span", `evidence-state ${info.tone}`, info.label);
    evidenceLabel.title = info.note; evidence.append(evidenceLabel);
    tr.append(name, element("td", "", displayValue(row.expected)), element("td", "", displayValue(row.actual, row.status === "MISSING")), status, evidence);
    tr.addEventListener("click", () => { state.selected = row.field; renderTable(); }); body.append(tr);
  }
  $("filter-empty").hidden = Boolean(visible.length);
  renderEvidence();
}
function renderEvidence() {
  const container = $("evidence-content"); container.replaceChildren();
  const row = state.review?.results.find((item) => item.field === state.selected);
  if (!row) { setText("evidence-title", "표시할 항목이 없습니다"); container.append(element("p", "small muted", "전체 항목에서 검토할 내용을 선택해 주세요.")); return; }
  setText("evidence-title", row.label || row.field); container.append(statusNode(row.status));
  const explanation = state.explanations.get(row.field);
  const ai = explanation?.origin === "ai_verified";
  const caption = element("p", "evidence-caption"); caption.append(element("span", "explanation-origin", ai ? "AI 구조 확인 통과 · 코드 설명" : "코드 설명"));
  const reason = explanation?.text || row.reason || "확인 이유가 제공되지 않았습니다.";
  container.append(element("p", "evidence-reason", reason), caption);
  const info = evidencePresentation(row);
  const notice = element("div", `evidence-notice ${info.tone}`);
  notice.append(element("strong", "", info.label), element("p", "", info.note));
  container.append(notice);
  const sources = Array.isArray(row.sources) ? row.sources : [];
  if (!sources.length) container.append(element("p", "small muted evidence-reason", "연결된 원문 근거가 없습니다. 담당자의 확인이 필요합니다."));
  sources.forEach((source) => container.append(sourceCard(source)));
}
function sourceCard(source) {
  const card = element("section", "source-card");
  card.append(element("h4", "source-heading", source.title || source.document_id || "문서 제목 확인 필요"));
  const meta = element("p", "source-meta", `${source.document_id || "문서 ID 미확인"} · ${Number.isInteger(source.page) ? `${source.page}쪽` : "페이지 미확인"}`);
  if (source.evidence_kind === "field_occurrence" && Number.isSafeInteger(source.occurrence_count) && source.occurrence_count > 1) {
    meta.append(element("span", "source-occurrence", ` · 이 페이지에 같은 표기 ${source.occurrence_count}회`));
  }
  card.append(meta);
  if (source.evidence_kind === "field_occurrence") {
    card.append(element("p", "source-candidate", "저장된 원문 후보"),
      element("p", "source-candidate-note", "이 항목이 등장한 원문입니다. 확정된 요구값이나 인계값으로 채택했다는 뜻은 아닙니다."));
  }
  card.append(element("blockquote", "source-quote", source.quote || "인용문이 제공되지 않았습니다."));
  const actions = element("div", "source-actions");
  if (source.pdf_url) {
    try {
      const url = safeUrl(source.pdf_url); if (Number.isInteger(source.page) && source.page > 0) url.hash = `page=${source.page}`;
      const link = element("a", "source-link", "원본 PDF 보기 ↗"); link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer"; actions.append(link);
    } catch (error) { card.append(element("p", "source-error", error.message)); }
  }
  if (source.page_url) {
    const button = element("button", "source-button", "추출 원문"); button.type = "button";
    const text = element("pre", "source-text"); text.hidden = true;
    const error = element("p", "source-error"); error.hidden = true;
    let loaded = false;
    button.addEventListener("click", async () => {
      if (loaded) { text.hidden = !text.hidden; button.textContent = text.hidden ? "추출 원문" : "원문 접기"; return; }
      button.disabled = true; button.textContent = "원문 읽는 중…"; error.hidden = true;
      try {
        const payload = await api(source.page_url, {timeout: 30000});
        if (typeof payload.text !== "string") throw new Error("추출 원문이 제공되지 않았습니다.");
        text.textContent = payload.text || "이 페이지에서 추출한 텍스트가 없습니다.";
        text.hidden = false; loaded = true; button.textContent = "원문 접기";
      } catch (failure) { error.textContent = failure.message; error.hidden = false; button.textContent = "원문 다시 읽기"; }
      finally { button.disabled = false; }
    });
    actions.append(button); card.append(actions, text, error);
  } else { card.append(actions); }
  return card;
}

function explainReview() {
  if (!state.review) return;
  if (!canExplainReview(state.review)) {
    showError(new Error(explainBlockedMessage(state.review)));
    return;
  }
  return perform("로컬 AI가 옮긴 항목 정보와 기준 사실이 같은지 확인하고 있습니다. 시간이 걸릴 수 있습니다…", async () => {
    try {
      const payload = await api(`/api/reviews/${encodeURIComponent(state.review.review_id)}/explain`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({use_ai: true}), timeout: 240000});
      if (!Array.isArray(payload.items)) throw new Error("AI 구조 확인 응답의 형식이 올바르지 않습니다.");
      state.explanations.clear();
      for (const item of payload.items) if (["code", "ai_verified"].includes(item.origin) && typeof item.text === "string") state.explanations.set(item.field, item);
      const verified = payload.items.filter((item) => item.origin === "ai_verified").length;
      const fallback = verified < state.review.results.length;
      const unavailable = payload.items.some((item) => item.validation_status === "unavailable");
      const rejected = payload.items.some((item) => item.validation_status === "rejected");
      let heading;
      if (verified) {
        heading = `${verified}개 항목: AI 구조 확인 통과 · 코드 설명을 표시합니다.${fallback ? " 나머지 항목은 기존 코드 설명을 유지합니다." : ""}`;
        if (unavailable) heading += " 일부 항목은 AI 연결을 사용할 수 없었습니다.";
      } else if (unavailable) {
        heading = "AI 연결을 사용할 수 없어 코드 설명을 유지합니다.";
      } else if (rejected) {
        heading = "AI가 반환한 항목 정보가 구조 확인을 통과하지 못해 코드 설명을 유지합니다.";
      } else {
        heading = "AI 구조 확인을 적용하지 않고 기존 코드 설명을 유지합니다.";
      }
      $("ai-note").className = `ai-note${fallback ? " fallback" : ""}`;
      setText("ai-note", `${heading} ${payload.note || (payload.llm_attempted ? "항목을 선택해 설명과 근거를 확인하세요." : "모델 호출이 수행되지 않았습니다.")}`);
      $("ai-note").hidden = false; renderEvidence();
    } catch (error) {
      state.explanations.clear(); renderEvidence();
      $("ai-note").className = "ai-note fallback";
      setText("ai-note", `AI 구조 확인을 완료하지 못해 기존 코드 설명을 유지합니다. ${error.message}`); $("ai-note").hidden = false;
      throw error;
    }
  }, explainReview);
}
function exportReview() {
  if (!state.review || !$("export-confirmed").checked) return;
  return perform("검토 기록을 준비하고 있습니다…", async () => {
    const id = state.review.review_id;
    const blob = await api(`/api/reviews/${encodeURIComponent(id)}/export.json`, {blob: true, timeout: 30000});
    const url = URL.createObjectURL(blob); const link = element("a");
    link.href = url; link.download = `handoff-review-${String(id).replace(/[^a-zA-Z0-9_-]/g, "_")}.json`;
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 15000);
  }, exportReview);
}
async function restoreState() {
  const uploadId = recalled(STORAGE.upload);
  const reviewId = recalled(STORAGE.review);
  const storedJob = recalled(STORAGE.job);
  let jobId = null;
  const notes = [];
  if (uploadId) {
    try {
      const upload = await api(`/api/uploads/${encodeURIComponent(uploadId)}`, {timeout: 30000});
      if (upload.upload_id !== uploadId || !Array.isArray(upload.documents) || !upload.documents.length) throw new Error("저장된 문서 정보의 형식이 올바르지 않습니다.");
      state.upload = upload;
      state.mode = upload.mode === "text_ai" ? "text_ai" : "template";
      state.extraction = upload.extraction_report || null;
      if (extractionRequired()) {
        const storedRequest = recalled(STORAGE.extractionRequest);
        restoreExtractionRequest(uploadId);
        if (storedRequest !== null && !state.extractionRequest) {
          // A legacy job with no request record can still be polled. A
          // malformed or different-upload record cannot bind this job safely.
          clearStoredExtractionJob();
          notes.push("이전 추출 요청 정보가 현재 문서와 맞지 않아 자동 조회하지 않았습니다. 원문을 확인한 뒤 추출을 다시 요청해 주세요.");
        } else {
          jobId = storedExtractionJob(storedJob, uploadId);
          if (storedJob && !jobId) {
            clearStoredExtractionJob();
            notes.push("저장된 작업의 문서 정보가 현재 문서와 맞지 않아 자동 조회하지 않았습니다. 현재 문서의 요청 식별자는 유지하며 명시적인 재시도를 기다립니다.");
          }
        }
      } else clearExtractionJob();
      $("policy-confirmed").checked = false;
      renderDocuments(); renderMode(); renderExtraction(); steps(2);
      notes.push("이전 문서 정보를 복원했습니다. 승인 적용 확인은 다시 필요합니다.");
    } catch (error) { notes.push(`이전 문서를 복원하지 못했습니다. ${error.message}`); }
  }
  if (extractionRequired() && jobId) {
    state.job = jobId; state.extracting = true;
    clearReviewState(); renderExtraction(); controls();
    try { await pollExtraction(jobId); }
    catch (error) { showError(error, extractUpload); }
    finally { state.extracting = false; renderExtraction(); controls(); }
    if (state.upload?.upload_id !== uploadId) return;
  } else if (reviewId && !extractionRequired()) {
    try {
      const review = await api(`/api/reviews/${encodeURIComponent(reviewId)}`, {timeout: 30000});
      if (state.upload && review.upload_id !== state.upload.upload_id) throw new Error("저장된 문서와 검토 기록이 일치하지 않습니다.");
      acceptReview(review);
      notes.push("이전 검토 결과를 복원했습니다. 저장 전 담당자 검토 확인은 다시 필요합니다.");
    } catch (error) { notes.push(`이전 검토를 복원하지 못했습니다. ${error.message}`); }
  }
  if (notes.length) { setText("restore-note", notes.join(" ")); $("restore-note").hidden = false; }
}
async function initialize() {
  if (state.busy) return;
  clearError(); setBusy(true, "서버와 이전 검토 기록을 확인하고 있습니다…");
  try {
    const outcomes = await Promise.allSettled([loadHealth(), loadSamples()]);
    if (outcomes[1].status === "rejected") setText("samples-note", "예시 문서를 불러오지 못했습니다. 서버 연결을 다시 확인해 주세요.");
    const failure = outcomes.find((outcome) => outcome.status === "rejected");
    if (failure) showError(failure.reason, initialize);
    if (!state.review && !state.upload) await restoreState();
  } finally { setBusy(false); }
}

$("upload-form").addEventListener("submit", (event) => { event.preventDefault(); uploadSelected(); });
for (const prefix of ["spec", "amendment", "handoff"]) $(prefix + "-file").addEventListener("change", () => {
  const file = $(prefix + "-file").files[0];
  setText(prefix + "-filename", file ? `${file.name} · ${(file.size / 1024).toFixed(0)} KiB` : "선택된 파일 없음");
  $(prefix + "-filename").classList.toggle("selected", Boolean(file));
  if (state.upload || state.review || state.job || state.extractionRequest) resetInputState("선택한 파일이 바뀌었습니다. 문서 읽기를 눌러 새 파일을 확인해 주세요.");
});
$("demo-d01").addEventListener("click", () => useSample("D01"));
$("demo-d02").addEventListener("click", () => useSample("D02"));
$("demo-c01").addEventListener("click", () => useSample("C01"));
$("demo-v01").addEventListener("click", () => useSample("V01"));
$("demo-v02").addEventListener("click", () => useSample("V02"));
$("demo-v03").addEventListener("click", () => useSample("V03"));
$("upload-mode").addEventListener("change", () => {
  state.mode = $("upload-mode").value === "text_ai" ? "text_ai" : "template";
  resetInputState("처리 방식이 바뀌었습니다. 선택한 문서를 다시 읽어 주세요."); renderMode();
});
$("extract-button").addEventListener("click", extractUpload);
$("policy-confirmed").addEventListener("change", controls);
$("review-button").addEventListener("click", runReview);
$("export-confirmed").addEventListener("change", controls);
$("export-button").addEventListener("click", exportReview);
$("explain-button").addEventListener("click", explainReview);
$("filter-all").addEventListener("click", () => { state.filter = "all"; renderTable(); });
$("filter-attention").addEventListener("click", () => { state.filter = "attention"; renderTable(); });
$("error-dismiss").addEventListener("click", clearError);
$("error-retry").addEventListener("click", () => { const retry = state.retry; clearError(); if (retry) retry(); });
$("health-retry").addEventListener("click", () => perform("서버와 예시 문서를 다시 확인하고 있습니다…", async () => { await Promise.all([loadHealth(), loadSamples()]); }, initialize));
initialize();
