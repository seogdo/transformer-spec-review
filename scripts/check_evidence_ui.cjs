"use strict";

// UI-only regression: execute the actual app in a small DOM double.
// No server, browser, model, network, stored review, or report file is changed.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "web/index.html"), "utf8");
const script = fs.readFileSync(path.join(root, "web/app.js"), "utf8");

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
  set innerHTML(_) { throw new Error("Unsafe HTML insertion in UI"); }
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

const nodes = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map((match) => [match[1], new NodeDouble()]));
const stored = new Map();
const context = vm.createContext({
  document: {
    getElementById: (id) => {
      assert.ok(nodes.has(id), `App requested missing HTML id: ${id}`);
      return nodes.get(id);
    },
    createElement: (tag) => new NodeDouble(tag),
  },
  window: {location: {origin: "http://127.0.0.1:8765"}},
  localStorage: {
    setItem: (key, value) => stored.set(key, value),
    getItem: (key) => stored.get(key) || null,
    removeItem: (key) => stored.delete(key),
  },
  URL,
  fetch: () => { throw new Error("UI-only checks must not make network requests"); },
});

// Keep event bindings and all production functions, suppress only startup I/O.
assert.match(script, /\ninitialize\(\);\s*$/);
vm.runInContext(script.replace(/\ninitialize\(\);\s*$/, "\n"), context, {filename: "web/app.js"});
const run = (code) => vm.runInContext(code, context);
const checks = [];
function test(name, check) { check(); checks.push(name); }
function freezeDeep(value) {
  if (value && typeof value === "object") {
    Object.values(value).forEach(freezeDeep);
    Object.freeze(value);
  }
  return value;
}
function source(extra = {}) {
  return {
    document_id: "SPEC-UI", title: "가상 UI 문서", page: 1, quote: "팬 전원: 380 V",
    pdf_url: "/api/uploads/UI/documents/SPEC-UI.pdf",
    page_url: "/api/uploads/UI/documents/SPEC-UI/pages/1", ...extra,
  };
}
function row(status, extra = {}) {
  return {
    field: "fan_supply_voltage", label: "팬 보조전원 정격전압", status,
    reason: "UI 확인용 코드 설명", expected: null, actual: null,
    evidence_complete: status !== "NEEDS_REVIEW", sources: [source()],
    evidence_check: {version: "original-page-evidence-v2", status: status === "NEEDS_REVIEW" ? "review_required" : "linked",
      linked_source_count: 1, invalid_source_count: 0, scope: "stored_original_page_quote"},
    ...extra,
  };
}
function render(rows) {
  const review = freezeDeep({
    review_id: "UI-REVIEW", upload_id: "UI-UPLOAD", results: rows,
    summary: {MATCH: rows.filter((item) => item.status === "MATCH").length,
      MISMATCH: rows.filter((item) => item.status === "MISMATCH").length,
      MISSING: rows.filter((item) => item.status === "MISSING").length,
      NEEDS_REVIEW: rows.filter((item) => item.status === "NEEDS_REVIEW").length},
    warnings: [], limitations: [],
  });
  const before = JSON.stringify(review);
  context.reviewFixture = review;
  run("state.review = reviewFixture; state.selected = null; state.filter = 'all'; state.explanations.clear(); renderReview();");
  assert.equal(JSON.stringify(review), before, "Rendering must not rewrite review JSON");
  return review;
}
const cell = (index = 0) => nodes.get("results-body").children[index].children[4].textContent;
const panel = () => nodes.get("evidence-content").textContent;
const descendants = (node) => [node, ...node.children.flatMap(descendants)];

test("current linked row has an explicit original-source count", () => {
  render([row("MATCH")]);
  assert.equal(cell(), "원문 1건 연결");
  assert.match(panel(), /문서 승인·조건 해석·설계 적합성을 보증하지 않습니다/);
});
test("held row stays held even if an inconsistent legacy boolean is true", () => {
  render([row("NEEDS_REVIEW", {evidence_complete: true})]);
  assert.equal(cell(), "원문 1건 · 판정 보류");
  assert.match(panel(), /원문 인용 확인과 판정 확정은 다릅니다/);
  assert.doesNotMatch(cell(), /연결됨/);
});
test("unavailable held row shows zero only from valid metadata", () => {
  render([row("NEEDS_REVIEW", {sources: [], evidence_check: {version: "original-page-evidence-v2",
    status: "unavailable", linked_source_count: 0, invalid_source_count: 0}})]);
  assert.equal(cell(), "원문 0건 · 판정 보류");
  assert.match(panel(), /연결된 원문 근거가 없습니다/);
});
test("invalid sources keep a current row in evidence review", () => {
  render([row("MISMATCH", {evidence_check: {version: "original-page-evidence-v2",
    status: "review_required", linked_source_count: 1, invalid_source_count: 2}})]);
  assert.equal(cell(), "원문 1건 · 확인 필요");
  assert.match(panel(), /확인하지 못한 근거 2건/);
});
test("a malformed count is never silently converted into zero or success", () => {
  render([row("NEEDS_REVIEW", {evidence_check: {version: "original-page-evidence-v2",
    status: "linked", linked_source_count: "1", invalid_source_count: 0}})]);
  assert.equal(cell(), "원문 건수 미확인 · 판정 보류");
});
test("legacy true and false records stay historical without rejudging", () => {
  for (const complete of [true, false]) {
    const legacy = row("MATCH", {evidence_complete: complete});
    delete legacy.evidence_check;
    const review = render([legacy]);
    assert.equal(cell(), "과거 근거 기록");
    assert.match(panel(), /과거 기록의 근거 연결 표시는 근거 완전성이나 판정 확정을 보증하지 않습니다/);
    assert.equal(review.results[0].evidence_complete, complete);
    assert.equal(review.results[0].status, "MATCH");
    assert.equal(review.results[0].evidence_check, undefined);
  }
});
test("legacy held rows show the historical and held boundaries together", () => {
  const legacy = row("NEEDS_REVIEW");
  delete legacy.evidence_check;
  render([legacy]);
  assert.equal(cell(), "과거 근거 기록");
  assert.match(panel(), /판정을 보류합니다/);
});
test("unknown evidence versions cannot claim current source checking", () => {
  render([row("MATCH", {evidence_check: {version: "other-version", status: "linked",
    linked_source_count: 1, invalid_source_count: 0}})]);
  assert.equal(cell(), "과거 근거 기록");
});
test("field occurrence is a literal saved candidate, not an approved value", () => {
  const attack = '<img src=x onerror="alert(1)">';
  render([row("NEEDS_REVIEW", {sources: [source({evidence_kind: "field_occurrence", title: attack, quote: attack})]})]);
  assert.match(panel(), /저장된 원문 후보/);
  assert.match(panel(), /확정된 요구값이나 인계값으로 채택했다는 뜻은 아닙니다/);
  assert.ok(panel().includes(attack));
  assert.ok(!descendants(nodes.get("evidence-content")).some((node) => node.tagName === "IMG"));
});
test("source PDF links keep their same-origin and page safeguards", () => {
  render([row("MATCH")]);
  const link = descendants(nodes.get("evidence-content")).find((node) => node.tagName === "A");
  assert.equal(link.href, "http://127.0.0.1:8765/api/uploads/UI/documents/SPEC-UI.pdf#page=1");
  assert.equal(link.rel, "noopener noreferrer");
  render([row("MATCH", {sources: [source({pdf_url: "javascript:alert(1)"})]})]);
  assert.ok(!descendants(nodes.get("evidence-content")).some((node) => node.tagName === "A"));
  assert.match(panel(), /이 서버에서 제공한 문서 주소만/);
});
test("candidate repetition counts stay per page without hiding other pages", () => {
  render([row("NEEDS_REVIEW", {sources: [
    source({evidence_kind: "field_occurrence", occurrence_count: 3}),
    source({page: 2, evidence_kind: "field_occurrence", occurrence_count: 1}),
  ]})]);
  assert.match(panel(), /이 페이지에 같은 표기 3회/);
  assert.doesNotMatch(panel(), /같은 표기 1회/);
  const cards = descendants(nodes.get("evidence-content")).filter((node) => node.className === "source-card");
  assert.equal(cards.length, 2);
  assert.match(cards[1].textContent, /2쪽/);
  render([row("NEEDS_REVIEW", {sources: [source({evidence_kind: "field_occurrence", occurrence_count: "3"})]})]);
  assert.doesNotMatch(panel(), /같은 표기/);
});
test("attention filter and selection keep the server verdicts unchanged", () => {
  const review = render([row("MATCH", {field: "frequency"}), row("NEEDS_REVIEW")]);
  run("state.filter = 'attention'; renderTable();");
  assert.equal(nodes.get("results-body").children.length, 1);
  assert.equal(cell(), "원문 1건 · 판정 보류");
  assert.deepEqual(review.results.map((item) => item.status), ["MATCH", "NEEDS_REVIEW"]);
});
console.log(JSON.stringify({kind: "ui_dom_double_not_browser_or_backend", passed: true, checks_run: checks.length, checks}, null, 2));
