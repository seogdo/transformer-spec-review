"use strict";

// Execute production browser scripts with small DOM doubles only.
// No server, browser, model, network, print dialog, or production record is used.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "web/index.html"), "utf8");
const appSource = fs.readFileSync(path.join(root, "web/app.js"), "utf8");
const printSource = fs.readFileSync(path.join(root, "web/report.js"), "utf8");
const css = fs.readFileSync(path.join(root, "web/report.css"), "utf8");
assert.match(appSource, /\ninitialize\(\);\s*$/);
const source = appSource.replace(/\ninitialize\(\);\s*$/, "\n");
const ID = "a".repeat(32);
const OTHER_ID = "b".repeat(32);
const ORIGIN = "http://127.0.0.1:8765";
const checks = [];
function test(name, action) { action(); checks.push(name); }

class NodeDouble {
  constructor(tag = "div", onClick = () => {}) {
    this.tagName = tag.toUpperCase();
    this.children = []; this.attributes = {}; this.events = {}; this.dataset = {};
    this.className = ""; this.hidden = false; this.disabled = false; this.checked = false;
    this.files = []; this.value = ""; this._text = ""; this.parentNode = null; this.onClick = onClick;
    this.classList = {toggle: (name, force) => {
      const values = new Set(this.className.split(/\s+/).filter(Boolean));
      const enabled = force ?? !values.has(name);
      if (enabled) values.add(name); else values.delete(name);
      this.className = [...values].join(" "); return enabled;
    }};
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  set innerHTML(_) { throw new Error("Unsafe HTML insertion"); }
  append(...nodes) { for (const node of nodes) { node.parentNode = this; this.children.push(node); } }
  replaceChildren(...nodes) { this._text = ""; this.children = []; this.append(...nodes); }
  remove() {
    if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((node) => node !== this);
    this.parentNode = null;
  }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(name, handler) { this.events[name] = handler; }
  click() { this.onClick(this); if (this.events.click) this.events.click(); }
  querySelectorAll(selector) {
    const all = this.children.flatMap((child) => [child, ...child.querySelectorAll("*")]);
    if (selector === "*") return all;
    if (selector === "input") return all.filter((node) => node.tagName === "INPUT");
    if (selector === "input:checked") return all.filter((node) => node.tagName === "INPUT" && node.checked);
    throw new Error(`Unsupported DOM selector: ${selector}`);
  }
  scrollIntoView() {}
}

function appHarness() {
  const nodes = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map((match) => [match[1], new NodeDouble()]));
  const opened = [];
  const body = new NodeDouble("body");
  const stored = new Map();
  const context = vm.createContext({
    document: {
      body,
      getElementById: (id) => { assert.ok(nodes.has(id), `Missing HTML id: ${id}`); return nodes.get(id); },
      createElement: (tag) => new NodeDouble(tag, (node) => {
        if (node.tagName === "A") opened.push({href: node.href, target: node.target, rel: node.rel});
      }),
    },
    window: {location: {origin: ORIGIN}},
    localStorage: {
      setItem: (key, value) => stored.set(key, value),
      getItem: (key) => stored.get(key) || null,
      removeItem: (key) => stored.delete(key),
    },
    URL,
    fetch: () => { throw new Error("Network is forbidden in report UI checks"); },
  });
  vm.runInContext(source, context, {filename: "web/app.js"});
  return {nodes, context, opened, body, run: (code) => vm.runInContext(code, context)};
}
function reviewFixture(id = ID, count = 10, held = false) {
  const fields = ["rated_power", "hv_voltage", "lv_voltage", "frequency", "phases",
    "vector_group", "cooling_type", "fan_supply_voltage", "tap_changer_type", "installation_type"];
  return {review_id: id, upload_id: OTHER_ID,
    customer_check: {version: "customer-identity-v1", status: held ? "NEEDS_REVIEW" : "MATCH",
      can_compare: !held, documents: []},
    results: fields.slice(0, count).map((field) => ({
      field, status: held ? "NEEDS_REVIEW" : "MATCH", expected: null, actual: null,
      reason: "모의 화면 확인", sources: [],
    })),
    summary: {MATCH: held ? 0 : count, MISMATCH: 0, MISSING: 0, NEEDS_REVIEW: held ? count : 0},
    warnings: [], limitations: [],
  };
}
function accept(h, record = reviewFixture()) {
  h.context.record = record; h.run("acceptReview(record);");
}
function confirm(h) {
  h.nodes.get("export-confirmed").checked = true;
  h.nodes.get("export-confirmed").events.change();
}

test("report and JSON actions stay gated without a result or human confirmation", () => {
  const h = appHarness(); h.run("controls(); openReviewReport();");
  assert.equal(h.nodes.get("report-button").disabled, true);
  accept(h); h.run("openReviewReport();");
  assert.equal(h.nodes.get("report-button").disabled, true);
  assert.equal(h.nodes.get("export-button").disabled, true);
  assert.equal(h.opened.length, 0);
});
test("one explicit click opens only the current same-origin report with opener isolation", () => {
  const h = appHarness(); accept(h); confirm(h);
  assert.equal(h.nodes.get("report-button").disabled, false);
  assert.equal(h.nodes.get("export-button").disabled, false);
  h.nodes.get("report-button").events.click();
  assert.deepEqual(h.opened, [{href: `${ORIGIN}/api/reviews/${ID}/report`, target: "_blank", rel: "noopener noreferrer"}]);
  assert.equal(h.body.children.length, 0);
});
test("busy state disables both actions and the report handler also refuses a direct call", () => {
  const h = appHarness(); accept(h); confirm(h); h.run("setBusy(true, '대기'); openReviewReport();");
  assert.equal(h.nodes.get("report-button").disabled, true);
  assert.equal(h.nodes.get("export-button").disabled, true);
  assert.equal(h.opened.length, 0);
});
test("a new review resets human confirmation and never opens a stale report", () => {
  const h = appHarness(); accept(h); confirm(h); accept(h, reviewFixture(OTHER_ID));
  assert.equal(h.nodes.get("export-confirmed").checked, false);
  h.run("openReviewReport();"); assert.equal(h.opened.length, 0);
  confirm(h); h.run("openReviewReport();");
  assert.equal(h.opened[0].href, `${ORIGIN}/api/reviews/${OTHER_ID}/report`);
});
test("document reset clears the review and requires a new confirmation", () => {
  const h = appHarness(); accept(h); confirm(h); h.run("resetInputState('문서 변경'); openReviewReport();");
  assert.equal(h.nodes.get("export-confirmed").checked, false);
  assert.equal(h.nodes.get("report-button").disabled, true);
  assert.equal(h.run("state.review"), null);
  assert.equal(h.opened.length, 0);
});
test("malformed or URL-shaped review identifiers cannot navigate", () => {
  for (const id of ["https://example.invalid/", "//example.invalid", "../OTHER", "javascript:alert(1)", ""]) {
    const h = appHarness(); h.context.record = {review_id: id}; h.run("state.review = record;");
    confirm(h); h.run("openReviewReport();");
    assert.equal(h.opened.length, 0);
    assert.equal(h.nodes.get("error-box").hidden, false);
  }
});
test("legacy eight-field records remain eight fields and can be reported without AI", () => {
  const h = appHarness(); const record = reviewFixture(ID, 8); delete record.customer_check;
  const before = JSON.stringify(record); accept(h, record); confirm(h); h.run("openReviewReport();");
  assert.equal(JSON.stringify(record), before);
  assert.equal(h.opened.length, 1);
  assert.match(h.nodes.get("export-description").textContent, /저장된 8개 항목/);
  assert.match(h.nodes.get("export-description").textContent, /회의용 보고서/);
  assert.match(h.nodes.get("export-description").textContent, /브라우저 인쇄/);
  assert.match(h.nodes.get("review-field-note").textContent, /현재 범위 전체의 검토 완료를 의미하지 않습니다/);
});
test("customer-held results are printable records without changing nulls or verdicts", () => {
  const h = appHarness(); const record = reviewFixture(ID, 10, true); const before = JSON.stringify(record);
  accept(h, record); confirm(h); h.run("openReviewReport();");
  assert.equal(h.opened.length, 1);
  assert.equal(JSON.stringify(record), before);
  assert.equal(h.nodes.get("explain-button").disabled, true);
});
test("opening a report does not replace JSON export or assert file-save completion", () => {
  const h = appHarness(); accept(h); confirm(h);
  const before = h.nodes.get("export-description").textContent;
  h.run("openReviewReport();");
  assert.equal(h.nodes.get("export-description").textContent, before);
  assert.equal(typeof h.nodes.get("export-button").events.click, "function");
  assert.doesNotMatch(before, /저장했습니다|저장 완료|인쇄 완료/);
});

function printHarness(implementation) {
  const button = new NodeDouble("button");
  const error = new NodeDouble(); error.hidden = true;
  const context = vm.createContext({
    document: {getElementById: (id) => id === "print-button" ? button : id === "print-error" ? error : null},
    window: {print: implementation},
    fetch: () => { throw new Error("Report printing must not request network/model activity"); },
  });
  vm.runInContext(printSource, context, {filename: "web/report.js"});
  return {button, error, context};
}
test("report loading never starts printing automatically", () => {
  let calls = 0; const h = printHarness(() => { calls++; });
  assert.equal(calls, 0); assert.equal(typeof h.button.events.click, "function");
});
test("explicit printing or cancellation never claims that a PDF was saved", () => {
  let calls = 0; const h = printHarness(() => { calls++; return undefined; });
  h.button.events.click();
  assert.equal(calls, 1); assert.equal(h.error.hidden, true); assert.equal(h.error.textContent, "");
  assert.equal(h.button.textContent, "");
});
test("print failure is visible and a later explicit retry clears the error only", () => {
  const h = printHarness(() => { throw new Error("Unavailable"); });
  h.button.events.click();
  assert.equal(h.error.hidden, false); assert.match(h.error.textContent, /인쇄 창을 열지 못했습니다/);
  h.context.window.print = () => {}; h.button.events.click();
  assert.equal(h.error.hidden, true); assert.equal(h.error.textContent, "");
});
test("a missing browser print API produces guidance instead of a success claim", () => {
  const h = printHarness(undefined); h.button.events.click();
  assert.equal(h.error.hidden, false); assert.match(h.error.textContent, /브라우저 메뉴/);
});
test("print stylesheet keeps complete evidence visible and repeats table headings", () => {
  assert.match(css, /@page\s*\{\s*size:\s*A4/);
  assert.match(css, /table-header-group/);
  assert.match(css, /\.report-quote[\s\S]*white-space:\s*pre-wrap/);
  assert.doesNotMatch(css, /line-clamp|text-overflow:\s*ellipsis|overflow:\s*hidden|max-height:\s*\d/);
  assert.match(css, /break-inside:\s*auto/);
  assert.match(css, /\.screen-only,\s*\.report-toolbar\s*\{\s*display:\s*none/);
});

console.log(JSON.stringify({kind: "report_ui_dom_double_not_browser_or_pdf_render",
  passed: true, checks_run: checks.length, checks}, null, 2));
