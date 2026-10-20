"use strict";

// Asynchronous UI regression with DOM/fetch doubles only.
// Runs the actual web/app.js, but never opens a browser, starts a server/model,
// sends network traffic, or changes stored production uploads/reviews/reports.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "web/index.html"), "utf8");
const script = fs.readFileSync(path.join(root, "web/app.js"), "utf8");
assert.match(script, /\ninitialize\(\);\s*$/);
const source = script.replace(/\ninitialize\(\);\s*$/, "\n");
const UPLOAD = "a".repeat(32);
const DERIVED = "b".repeat(32);
const JOB = "c".repeat(32);
const STORED_KEY = "d".repeat(32);
const STORAGE = {
  upload: "handoff-review:upload_id",
  job: "handoff-review:job_id",
  request: "handoff-review:extraction_request",
};
const postPath = `/api/uploads/${UPLOAD}/extractions`;
const jobPath = `/api/extractions/${JOB}`;
const copy = (value) => JSON.parse(JSON.stringify(value));

class NodeDouble {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.attributes = {};
    this.events = {};
    this.dataset = {};
    this.className = "";
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.files = [];
    this._text = "";
    this.classList = {
      toggle: (name, force) => {
        const values = new Set(this.className.split(/\s+/).filter(Boolean));
        const enabled = force ?? !values.has(name);
        if (enabled) values.add(name); else values.delete(name);
        this.className = [...values].join(" ");
        return enabled;
      },
    };
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  set innerHTML(_) { throw new Error("Unsafe HTML insertion"); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this._text = ""; this.children = [...nodes]; }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(name, handler) { this.events[name] = handler; }
  querySelectorAll(selector) {
    const all = this.children.flatMap((child) => [child, ...child.querySelectorAll("*")]);
    if (selector === "*") return all;
    if (selector === "input") return all.filter((node) => node.tagName === "INPUT");
    if (selector === "input:checked") return all.filter((node) => node.tagName === "INPUT" && node.checked);
    throw new Error(`Unsupported DOM selector: ${selector}`);
  }
  scrollIntoView() {}
}

function uploadFixture(id = UPLOAD, pending = true) {
  return {
    upload_id: id, mode: "text_ai",
    documents: ["customer_spec", "amendment", "handoff"].map((role, index) => ({
      id: `DOC-${index}`, role, title: `가상 문서 ${index}`, status: "approved",
      project_id: "RECOVERY-UI", customer_name: "한전", warnings: [],
      extraction_method: pending ? "text_pending_ai" : "local_tool_rag",
    })),
    customer_check: {version: "customer-identity-v1", status: "MATCH", can_compare: true, documents: []},
  };
}
function completed(extraction = {status: "completed", llm_calls: 0, trace: []}) {
  const upload = uploadFixture(DERIVED, false);
  upload.extraction_report = extraction;
  return {job_id: JOB, source_upload_id: UPLOAD, status: "completed", upload, extraction};
}
function jobState(status, extra = {}) {
  return {job_id: JOB, source_upload_id: UPLOAD, status, ...extra};
}

function harness({stored = new Map(), disabledStorage = false, rejectRequestWrite = false,
                  requestReadbackMismatch = false, cryptoAvailable = true} = {}) {
  const nodes = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map((match) => [match[1], new NodeDouble()]));
  const queue = [];
  const calls = [];
  let counter = 0;
  let clock = 0;
  const context = vm.createContext({
    document: {
      getElementById: (id) => {
        assert.ok(nodes.has(id), `Missing HTML node ${id}`);
        return nodes.get(id);
      },
      createElement: (tag) => new NodeDouble(tag),
    },
    window: {location: {origin: "http://127.0.0.1:8765"}, matchMedia: () => ({matches: true})},
    localStorage: {
      setItem: (key, value) => {
        if (disabledStorage || (rejectRequestWrite && key === STORAGE.request)) throw new Error("Storage disabled");
        stored.set(key, value);
      },
      getItem: (key) => {
        if (disabledStorage) throw new Error("Storage disabled");
        if (requestReadbackMismatch && key === STORAGE.request) return null;
        return stored.get(key) || null;
      },
      removeItem: (key) => {
        if (disabledStorage) throw new Error("Storage disabled");
        stored.delete(key);
      },
    },
    URL, AbortController, Error, TypeError,
    crypto: cryptoAvailable ? {
      randomUUID: () => `00000000-0000-4000-8000-${(++counter).toString(16).padStart(12, "0")}`,
    } : undefined,
    performance: {now: () => clock},
    setTimeout: (fn, delay) => {
      if (delay <= 1500) {
        clock += delay;
        queueMicrotask(fn);
        return Symbol("mock-poll-delay");
      }
      return setTimeout(fn, delay);
    },
    clearTimeout: (timer) => { if (typeof timer !== "symbol") clearTimeout(timer); },
    fetch: async (url, options) => {
      const call = {path: new URL(url).pathname, method: options.method || "GET", headers: options.headers || {}};
      calls.push(call);
      const next = queue.shift();
      assert.ok(next, `Unexpected mocked fetch: ${call.method} ${call.path}`);
      assert.equal(call.method, next.method);
      assert.equal(call.path, next.path);
      if (next.inspect) next.inspect(call);
      if (next.wait) await next.wait;
      if (next.error) throw next.error;
      return {
        ok: next.status >= 200 && next.status < 300, status: next.status,
        json: async () => {
          if (next.jsonError) throw new Error("Simulated truncated JSON response");
          return copy(next.body);
        },
      };
    },
  });
  vm.runInContext(source, context, {filename: "web/app.js"});
  const run = (code) => vm.runInContext(code, context);
  const respond = (method, path, body, extra = {}) => queue.push({method, path, body, status: 200, ...extra});
  return {
    nodes, stored, calls, context, run, respond,
    queue, uuidCount: () => counter,
    read: (code) => copy(run(code)),
    seed: (fixture = uploadFixture()) => {
      context.fixture = fixture;
      run("acceptUpload(fixture, 'text_ai'); controls();");
    },
    request: () => JSON.parse(stored.get(STORAGE.request)),
    finish: () => {
      assert.equal(queue.length, 0, "Not all mocked responses were consumed");
      assert.equal(run("state.busy"), false);
      assert.equal(run("state.extracting"), false);
    },
  };
}

const checks = [];
async function test(name, action) {
  await action();
  checks.push(name);
}
function expectCleared(h) {
  assert.equal(h.run("state.job"), null);
  assert.equal(h.run("state.extractionRequest"), null);
  assert.equal(h.stored.has(STORAGE.job), false);
  assert.equal(h.stored.has(STORAGE.request), false);
}
function manualSuccess(h, extra = {}) {
  h.respond("POST", postPath, jobState("completed"), {status: 202, ...extra});
  h.respond("GET", jobPath, completed());
}
function pendingStorage({job = false, request = true, legacyJob = false} = {}) {
  const stored = new Map([[STORAGE.upload, UPLOAD]]);
  if (job) stored.set(STORAGE.job, legacyJob ? JOB : JSON.stringify({upload_id: UPLOAD, job_id: JOB}));
  if (request) stored.set(STORAGE.request, JSON.stringify({upload_id: UPLOAD, key: STORED_KEY}));
  return stored;
}

async function main() {
  await test("HTTP error keeps its status and server detail", async () => {
    const h = harness();
    h.respond("GET", jobPath, {detail: "기록 없음"}, {status: 404});
    await assert.rejects(h.run(`api("${jobPath}")`), (error) => error.status === 404 && error.message === "기록 없음");
    h.finish();
  });
  await test("network and timeout errors retain status zero", async () => {
    const h = harness();
    for (const error of [new TypeError("connection lost"), Object.assign(new Error("aborted"), {name: "AbortError"})]) {
      h.respond("GET", jobPath, null, {error});
      await assert.rejects(h.run(`api("${jobPath}")`), (failure) => failure.status === 0);
    }
    h.finish();
  });
  await test("request identity is persisted before POST and cleared after completion", async () => {
    const h = harness(); h.seed();
    h.respond("POST", postPath, jobState("running"), {status: 202, inspect: (call) => {
      assert.match(call.headers["Idempotency-Key"], /^[a-f0-9]{32}$/);
      assert.deepEqual(h.request(), {upload_id: UPLOAD, key: call.headers["Idempotency-Key"]});
    }});
    h.respond("GET", jobPath, jobState("running"));
    h.respond("GET", jobPath, completed());
    await h.run("extractUpload()");
    assert.equal(h.calls.filter((call) => call.method === "POST").length, 1);
    assert.equal(h.run("state.upload.upload_id"), DERIVED);
    expectCleared(h); h.finish();
  });
  await test("lost POST response keeps the same key for an explicit retry", async () => {
    const h = harness(); h.seed();
    h.respond("POST", postPath, null, {error: new TypeError("server accepted but response lost")});
    await h.run("extractUpload()");
    const key = h.request().key;
    assert.equal(h.calls.length, 1);
    assert.equal(h.run("state.job"), null);
    assert.equal(h.nodes.get("error-retry").hidden, false);
    manualSuccess(h, {inspect: (call) => assert.equal(call.headers["Idempotency-Key"], key)});
    await h.run("state.retry()");
    assert.equal(h.uuidCount(), 1);
    expectCleared(h); h.finish();
  });
  await test("truncated POST JSON also preserves the request identity", async () => {
    const h = harness(); h.seed();
    h.respond("POST", postPath, null, {status: 202, jsonError: true});
    await h.run("extractUpload()");
    const key = h.request().key;
    manualSuccess(h, {inspect: (call) => assert.equal(call.headers["Idempotency-Key"], key)});
    await h.run("extractUpload()");
    assert.equal(h.uuidCount(), 1);
    h.finish();
  });
  await test("503 substituted for an accepted response keeps the same key across reload", async () => {
    const stored = new Map();
    const first = harness({stored}); first.seed();
    first.respond("POST", postPath, {detail: "모의 응답 전달 실패"}, {status: 503});
    await first.run("extractUpload()");
    const key = first.request().key;
    first.finish();
    const restored = harness({stored});
    restored.respond("GET", `/api/uploads/${UPLOAD}`, uploadFixture());
    await restored.run("restoreState()");
    assert.equal(restored.calls.length, 1);
    manualSuccess(restored, {inspect: (call) => assert.equal(call.headers["Idempotency-Key"], key)});
    await restored.run("extractUpload()");
    assert.equal(restored.uuidCount(), 0);
    expectCleared(restored); restored.finish();
  });
  await test("409 busy does not automatically retry or rotate the key", async () => {
    const h = harness(); h.seed();
    h.respond("POST", postPath, {detail: "다른 AI 작업이 진행 중입니다."}, {status: 409});
    await h.run("extractUpload()");
    const key = h.request().key;
    assert.equal(h.calls.length, 1);
    assert.match(h.nodes.get("error-text").textContent, /다른 AI 작업/);
    manualSuccess(h, {inspect: (call) => assert.equal(call.headers["Idempotency-Key"], key)});
    await h.run("extractUpload()");
    assert.equal(h.uuidCount(), 1);
    h.finish();
  });
  await test("reload after an unknown POST outcome restores key without automatic POST", async () => {
    const h = harness({stored: pendingStorage()});
    h.respond("GET", `/api/uploads/${UPLOAD}`, uploadFixture());
    await h.run("restoreState()");
    assert.equal(h.calls.length, 1);
    assert.equal(h.read("state.extractionRequest").key, STORED_KEY);
    assert.equal(h.nodes.get("extract-button").textContent, "같은 추출 요청 다시 확인");
    assert.match(h.nodes.get("extraction-message").textContent, /자동으로 보내지 않습니다/);
    manualSuccess(h, {inspect: (call) => assert.equal(call.headers["Idempotency-Key"], STORED_KEY)});
    await h.run("extractUpload()");
    assert.equal(h.uuidCount(), 0);
    h.finish();
  });
  await test("polling network loss keeps job and key; retry only polls", async () => {
    const h = harness(); h.seed();
    h.respond("POST", postPath, jobState("running"), {status: 202});
    h.respond("GET", jobPath, null, {error: new TypeError("poll lost")});
    await h.run("extractUpload()");
    const key = h.request().key;
    assert.equal(h.run("state.job"), JOB);
    assert.deepEqual(JSON.parse(h.stored.get(STORAGE.job)), {upload_id: UPLOAD, job_id: JOB});
    assert.equal(h.nodes.get("extract-button").textContent, "추출 진행 상태 다시 확인");
    h.respond("GET", jobPath, completed());
    await h.run("extractUpload()");
    assert.equal(h.calls.filter((call) => call.method === "POST").length, 1);
    assert.equal(h.calls[0].headers["Idempotency-Key"], key);
    expectCleared(h); h.finish();
  });
  await test("reload with a saved job only resumes polling", async () => {
    const h = harness({stored: pendingStorage({job: true})});
    h.respond("GET", `/api/uploads/${UPLOAD}`, uploadFixture());
    h.respond("GET", jobPath, completed());
    await h.run("restoreState()");
    assert.ok(h.calls.every((call) => call.method === "GET"));
    assert.equal(h.run("state.upload.upload_id"), DERIVED);
    expectCleared(h); h.finish();
  });
  await test("legacy saved job without a request record still resumes GET only", async () => {
    const h = harness({stored: pendingStorage({job: true, request: false, legacyJob: true})});
    h.respond("GET", `/api/uploads/${UPLOAD}`, uploadFixture());
    h.respond("GET", jobPath, completed());
    await h.run("restoreState()");
    assert.ok(h.calls.every((call) => call.method === "GET"));
    expectCleared(h); h.finish();
  });
  await test("legacy job without server source identity is not applied or automatically restarted", async () => {
    const h = harness({stored: pendingStorage({job: true, request: false, legacyJob: true})});
    h.respond("GET", `/api/uploads/${UPLOAD}`, uploadFixture());
    const legacy = completed();
    delete legacy.source_upload_id;
    h.respond("GET", jobPath, legacy);
    await h.run("restoreState()");
    assert.equal(h.run("state.upload.upload_id"), UPLOAD);
    assert.ok(h.calls.every((call) => call.method === "GET"));
    assert.match(h.nodes.get("error-text").textContent, /原本|원본 문서/);
    expectCleared(h); h.finish();
  });
  await test("a POST response for a different source is rejected while its request key is retained", async () => {
    const h = harness(); h.seed();
    h.respond("POST", postPath, jobState("running", {source_upload_id: DERIVED}), {status: 202});
    await h.run("extractUpload()");
    const key = h.request().key;
    assert.equal(h.run("state.job"), null);
    assert.equal(h.run("state.upload.upload_id"), UPLOAD);
    assert.equal(h.calls.length, 1);
    manualSuccess(h, {inspect: (call) => assert.equal(call.headers["Idempotency-Key"], key)});
    await h.run("extractUpload()");
    assert.equal(h.uuidCount(), 1);
    h.finish();
  });
  await test("every polled response must match the source before a completed upload is accepted", async () => {
    const h = harness(); h.seed();
    h.respond("POST", postPath, jobState("running"), {status: 202});
    h.respond("GET", jobPath, jobState("running"));
    h.respond("GET", jobPath, {...completed(), source_upload_id: DERIVED});
    await h.run("extractUpload()");
    assert.equal(h.run("state.upload.upload_id"), UPLOAD);
    assert.equal(h.run("state.job"), null);
    assert.equal(h.request().upload_id, UPLOAD);
    assert.equal(h.calls.filter((call) => call.method === "POST").length, 1);
    assert.match(h.nodes.get("error-text").textContent, /현재 문서는 변경하지 않았습니다/);
    h.finish();
  });
  await test("job 404 clears job and key without a new POST until explicit retry", async () => {
    const h = harness(); h.seed();
    h.respond("POST", postPath, jobState("running"), {status: 202});
    h.respond("GET", jobPath, {detail: "추출 기록 없음"}, {status: 404});
    await h.run("extractUpload()");
    const firstKey = h.calls[0].headers["Idempotency-Key"];
    assert.equal(h.calls.length, 2);
    expectCleared(h);
    assert.match(h.nodes.get("error-text").textContent, /기록을 찾을 수 없어 다시 요청이 필요/);
    manualSuccess(h, {inspect: (call) => assert.notEqual(call.headers["Idempotency-Key"], firstKey)});
    await h.run("state.retry()");
    assert.equal(h.uuidCount(), 2);
    h.finish();
  });
  await test("404 during reload clears stale identity and does not auto-submit", async () => {
    const h = harness({stored: pendingStorage({job: true})});
    h.respond("GET", `/api/uploads/${UPLOAD}`, uploadFixture());
    h.respond("GET", jobPath, {detail: "기록 없음"}, {status: 404});
    await h.run("restoreState()");
    assert.ok(h.calls.every((call) => call.method === "GET"));
    expectCleared(h);
    assert.equal(h.nodes.get("error-retry").hidden, false);
    h.finish();
  });
  await test("POST 404 for a lost mapped job or upload clears key and requires explicit recovery", async () => {
    const h = harness(); h.seed();
    h.respond("POST", postPath, {detail: "기록 없음"}, {status: 404});
    await h.run("extractUpload()");
    const firstKey = h.calls[0].headers["Idempotency-Key"];
    assert.equal(h.calls.length, 1);
    expectCleared(h);
    assert.match(h.nodes.get("error-text").textContent, /같은 PDF 3종.*‘문서 읽기’/);
    assert.match(h.nodes.get("error-text").textContent, /자동으로 보내지 않습니다/);
    manualSuccess(h, {inspect: (call) => assert.notEqual(call.headers["Idempotency-Key"], firstKey)});
    await h.run("extractUpload()");
    assert.equal(h.uuidCount(), 2);
    h.finish();
  });
  await test("failed job returned by POST is polled and handled as terminal failure", async () => {
    const h = harness(); h.seed();
    h.respond("POST", postPath, jobState("failed"), {status: 202});
    h.respond("GET", jobPath, jobState("failed", {error: "서버 재시작으로 중단되었습니다."}));
    await h.run("extractUpload()");
    const firstKey = h.calls[0].headers["Idempotency-Key"];
    assert.equal(h.calls.length, 2);
    assert.equal(h.run("state.extraction.status"), "failed");
    assert.match(h.nodes.get("error-text").textContent, /서버 재시작/);
    expectCleared(h);
    manualSuccess(h, {inspect: (call) => assert.notEqual(call.headers["Idempotency-Key"], firstKey)});
    await h.run("extractUpload()");
    h.finish();
  });
  await test("completed unavailable result keeps AI button hidden and gives concrete manual recovery", async () => {
    const h = harness(); h.seed();
    h.respond("POST", postPath, jobState("completed"), {status: 202});
    h.respond("GET", jobPath, completed({status: "unavailable", trace: [], llm_calls: 0}));
    await h.run("extractUpload()");
    assert.equal(h.nodes.get("extract-button").hidden, true);
    const message = h.nodes.get("extraction-message").textContent;
    assert.match(message, /같은 PDF 3종/);
    assert.match(message, /‘문서 읽기’/);
    assert.match(message, /예시 버튼/);
    assert.match(message, /자동으로 재시도하지 않습니다/);
    assert.equal(h.calls.length, 2);
    expectCleared(h); h.finish();
  });
  await test("accepting another upload clears old job and request identity", async () => {
    const h = harness(); h.seed();
    h.run(`rememberExtractionJob("${JOB}", state.upload.upload_id); extractionRequestKey(state.upload.upload_id);`);
    h.seed(uploadFixture("e".repeat(32)));
    expectCleared(h);
    assert.equal(h.stored.get(STORAGE.upload), "e".repeat(32));
    h.finish();
  });
  await test("resetInputState and mode-change event clear recovery identity", async () => {
    for (const modeChange of [false, true]) {
      const h = harness(); h.seed();
      h.run(`rememberExtractionJob("${JOB}", state.upload.upload_id); extractionRequestKey(state.upload.upload_id);`);
      if (modeChange) {
        h.nodes.get("upload-mode").value = "template";
        h.nodes.get("upload-mode").events.change();
        assert.equal(h.run("state.mode"), "template");
      } else h.run('resetInputState("새 입력");');
      expectCleared(h);
      assert.equal(h.stored.has(STORAGE.upload), false);
      assert.equal(h.run("state.upload"), null);
      h.finish();
    }
  });
  await test("changed file selection resets the pending extraction request", async () => {
    const h = harness(); h.seed();
    h.run("extractionRequestKey(state.upload.upload_id);");
    h.nodes.get("spec-file").files = [{name: "changed.pdf", size: 1024}];
    h.nodes.get("spec-file").events.change();
    expectCleared(h);
    assert.equal(h.run("state.upload"), null);
    h.finish();
  });
  await test("malformed or another-upload stored request is never reused", async () => {
    for (const invalid of ["not-json", JSON.stringify({upload_id: DERIVED, key: STORED_KEY}),
                          JSON.stringify({upload_id: UPLOAD, key: "../invalid"})]) {
      const stored = pendingStorage();
      stored.set(STORAGE.request, invalid);
      const h = harness({stored});
      h.respond("GET", `/api/uploads/${UPLOAD}`, uploadFixture());
      await h.run("restoreState()");
      expectCleared(h);
      assert.equal(h.calls.length, 1);
      manualSuccess(h, {inspect: (call) => assert.notEqual(call.headers["Idempotency-Key"], STORED_KEY)});
      await h.run("extractUpload()");
      assert.equal(h.uuidCount(), 1);
      h.finish();
    }
  });
  await test("a job bound to a malformed or different-upload request is not auto-polled", async () => {
    for (const invalid of ["not-json", JSON.stringify({upload_id: DERIVED, key: STORED_KEY})]) {
      const stored = pendingStorage({job: true});
      stored.set(STORAGE.request, invalid);
      const h = harness({stored});
      h.respond("GET", `/api/uploads/${UPLOAD}`, uploadFixture());
      await h.run("restoreState()");
      expectCleared(h);
      assert.equal(h.calls.length, 1);
      assert.match(h.nodes.get("restore-note").textContent, /현재 문서와 맞지 않아 자동 조회하지 않았습니다/);
      h.finish();
    }
  });
  await test("a stored job for another upload is discarded without losing this upload's key", async () => {
    const stored = pendingStorage({job: true});
    stored.set(STORAGE.job, JSON.stringify({upload_id: DERIVED, job_id: JOB}));
    const h = harness({stored});
    h.respond("GET", `/api/uploads/${UPLOAD}`, uploadFixture());
    await h.run("restoreState()");
    assert.equal(h.run("state.job"), null);
    assert.equal(h.stored.has(STORAGE.job), false);
    assert.deepEqual(h.request(), {upload_id: UPLOAD, key: STORED_KEY});
    assert.equal(h.calls.length, 1);
    assert.match(h.nodes.get("restore-note").textContent, /현재 문서와 맞지 않아 자동 조회하지 않았습니다/);
    h.finish();
  });
  await test("delayed tab A response cannot restore its job as tab B's upload", async () => {
    const stored = new Map();
    const tabA = harness({stored}); tabA.seed();
    let releaseA;
    const wait = new Promise((resolve) => { releaseA = resolve; });
    tabA.respond("POST", postPath, jobState("running"), {status: 202, wait});
    tabA.respond("GET", jobPath, null, {error: new TypeError("pause tab A polling")});
    const pendingA = tabA.run("extractUpload()");

    const uploadB = "e".repeat(32);
    const tabB = harness({stored}); tabB.seed(uploadFixture(uploadB));
    tabB.respond("POST", `/api/uploads/${uploadB}/extractions`, null, {error: new TypeError("tab B response lost")});
    await tabB.run("extractUpload()");
    const requestB = tabB.request();
    releaseA();
    await pendingA;
    assert.deepEqual(JSON.parse(stored.get(STORAGE.job)), {upload_id: UPLOAD, job_id: JOB});
    assert.equal(stored.get(STORAGE.upload), uploadB);
    assert.deepEqual(tabB.request(), requestB);

    const restored = harness({stored});
    restored.respond("GET", `/api/uploads/${uploadB}`, uploadFixture(uploadB));
    await restored.run("restoreState()");
    assert.equal(restored.calls.length, 1);
    assert.equal(restored.run("state.upload.upload_id"), uploadB);
    assert.equal(restored.run("state.job"), null);
    assert.deepEqual(restored.request(), requestB);
    tabA.finish(); tabB.finish(); restored.finish();
  });
  await test("double activation while a request is pending sends only one POST", async () => {
    const h = harness(); h.seed();
    let release;
    const wait = new Promise((resolve) => { release = resolve; });
    h.respond("POST", postPath, jobState("completed"), {status: 202, wait});
    h.respond("GET", jobPath, completed());
    const first = h.run("extractUpload()");
    await h.run("extractUpload()");
    assert.equal(h.calls.length, 1);
    release();
    await first;
    assert.equal(h.uuidCount(), 1);
    h.finish();
  });
  await test("disabled localStorage blocks POST rather than relying on memory-only recovery", async () => {
    const h = harness({disabledStorage: true}); h.seed();
    await h.run("extractUpload()");
    assert.equal(h.calls.length, 0);
    assert.match(h.nodes.get("error-text").textContent, /저장하고 확인할 수 없어 AI 요청을 보내지 않았습니다/);
    await h.run("state.retry()");
    assert.equal(h.calls.length, 0);
    assert.equal(h.uuidCount(), 1);
    h.finish();
  });
  await test("failure to store just the request key blocks POST even if the upload ID remains", async () => {
    const h = harness({rejectRequestWrite: true}); h.seed();
    assert.equal(h.stored.get(STORAGE.upload), UPLOAD);
    await h.run("extractUpload()");
    assert.equal(h.calls.length, 0);
    assert.equal(h.stored.get(STORAGE.upload), UPLOAD);
    assert.equal(h.stored.has(STORAGE.request), false);
    assert.match(h.nodes.get("error-text").textContent, /AI 요청을 보내지 않았습니다/);
    h.finish();
  });
  await test("request key write without matching readback also blocks POST", async () => {
    const h = harness({requestReadbackMismatch: true}); h.seed();
    await h.run("extractUpload()");
    assert.equal(h.calls.length, 0);
    assert.equal(h.stored.has(STORAGE.request), true);
    assert.match(h.nodes.get("error-text").textContent, /저장하고 확인할 수 없어/);
    h.finish();
  });
  await test("no secure UUID support fails before POST instead of weak random fallback", async () => {
    const h = harness({cryptoAvailable: false}); h.seed();
    await h.run("extractUpload()");
    assert.equal(h.calls.length, 0);
    assert.match(h.nodes.get("error-text").textContent, /안전한 요청 식별자/);
    expectCleared(h); h.finish();
  });
  await test("customer identity hold still prevents extraction and key creation", async () => {
    const h = harness();
    const held = uploadFixture();
    held.customer_check = {version: "customer-identity-v1", status: "NEEDS_REVIEW", can_compare: false, documents: []};
    h.seed(held);
    await h.run("extractUpload()");
    assert.equal(h.calls.length, 0);
    assert.equal(h.uuidCount(), 0);
    assert.equal(h.nodes.get("extract-button").hidden, true);
    expectCleared(h); h.finish();
  });
  console.log(JSON.stringify({
    kind: "async_ui_dom_and_fetch_doubles_not_browser_or_backend_or_ai",
    passed: true, checks_run: checks.length, checks,
  }, null, 2));
}

main().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
