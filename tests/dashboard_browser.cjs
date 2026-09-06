// Offline behavioral browser harness: DOM/canvas seams are recorded, fetch is controlled.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const events = new Map(), elements = new Map(), calls = [], intervals = [];
const drawing = [];
const context2d = new Proxy({}, {get: (_, key) => key === "measureText" ? () => ({width: 10}) : (...args) => drawing.push([key, ...args]), set: () => true});
function element() {
  return {textContent: "", value: "", children: [], width: 800, height: 300,
    append(child) { this.children.push(child); if (child.selected || !this.value) this.value = child.value || ""; },
    replaceChildren() { this.children = []; },
    addEventListener(name, fn) { this[name] = fn; },
    getBoundingClientRect: () => ({left: 0, width: 800, height: 300}),
    getContext: () => context2d};
}
const document = {hidden: false, documentElement: {}, createElement: element,
  querySelector(selector) { if (!elements.has(selector)) elements.set(selector, element()); return elements.get(selector); },
  addEventListener(name, fn) { events.set(name, fn); }};
const sandbox = {document, console, Intl, Date, Map, URL, URLSearchParams, AbortController,
  location: {href: "http://localhost/", search: ""}, history: {replaceState() {}},
  getComputedStyle: () => ({getPropertyValue: name => name}),
  window: {devicePixelRatio: 1, setInterval: fn => intervals.push(fn), addEventListener: (name, fn) => events.set(name, fn)},
  fetch(url, options = {}) {
    if (url === "/api/v1/properties") return Promise.resolve({ok: true, json: async () => ({properties: [{id: "home", timezone: "Pacific/Kiritimati"}, {id: "other", timezone: "America/Los_Angeles"}]})});
    if (url === "/api/status") return Promise.resolve({ok: true, json: async () => ({services: [], generated_at: new Date().toISOString()})});
    return new Promise(resolve => calls.push({url, options, resolve}));
  }};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync("src/solar_battery_forecaster/static/app.js", "utf8"), sandbox);
const run = code => vm.runInContext(code, sandbox);
const settle = () => new Promise(resolve => setImmediate(resolve));
function payload(property, day, soc = 50) {
  const start = `${day}T00:00:00+00:00`, stop = new Date(Date.parse(start) + 86400000).toISOString();
  return {property_id: property, local_date: day, timezone: "Europe/London", generated_at: new Date().toISOString(),
    window: {start, stop}, limits: {minimum_soc_percent: 10}, assumptions: {soc_projection: "Uniform load"},
    plan: {available: false}, summary: {latest_actual_soc_percent: soc, actual_energy_quality: "partial", actual_energy_coverage_fraction: .1, actual_energy_reason_codes: ["sampling_gap"]},
    series: {forecast_generation_kw: [], actual_generation_kw: [], planned_soc_percent: [], cheap_rate_intervals: [], actual_soc_percent: [{at: start, value: soc}, {at: stop, value: soc - 1}]}};
}
async function resolve(call, property, day, soc) { call.resolve({ok: true, json: async () => payload(property, day, soc)}); await settle(); }
(async () => {
  await settle();
  const selectedDay = run("dayInput.value");
  assert.equal(selectedDay, run("localDay()"), "Default day uses selected property's timezone");
  await resolve(calls[0], "home", selectedDay, 25);
  assert.equal(elements.get("#battery-now").textContent, 25);
  assert.equal(intervals.length, 1);
  run('propertyInput.value = "other"; loadCurve()');
  assert.equal(elements.get("#battery-now").textContent, "—", "Old context clears immediately");
  const slow = calls.at(-1);
  run('propertyInput.value = "home"; loadCurve()');
  const fast = calls.at(-1);
  assert.equal(slow.options.signal.aborted, true);
  await resolve(fast, "home", selectedDay, 66);
  await resolve(slow, "other", selectedDay, 12);
  assert.equal(elements.get("#battery-now").textContent, 66, "Late response cannot overwrite newer selection");
  run('propertyInput.value = "other"; loadCurve()');
  calls.at(-1).resolve({ok: false}); await settle();
  assert.equal(elements.get("#battery-now").textContent, "—");
  assert.match(elements.get("#status").textContent, /other.*No data/);
  document.hidden = true;
  const before = calls.length;
  intervals[0]();
  assert.equal(calls.length, before, "No hidden refresh");
  document.hidden = false;
  events.get("visibilitychange")();
  assert.equal(calls.length, before + 1, "Visible page refreshes");
  const resumedDay = run("dayInput.value");
  await resolve(calls.at(-1), "other", resumedDay, 70);
  intervals[0]();
  await resolve(calls.at(-1), "other", resumedDay, 71);
  assert.equal(elements.get("#battery-now").textContent, 71, "New telemetry appears on timer");
  drawing.length = 0;
  run("render()");
  const powerTicks = drawing.filter(call => call[0] === "fillText" && String(call[1]).endsWith("kW")).map(call => call[1]);
  assert.equal(powerTicks.length, 5);
  assert.equal(new Set(powerTicks).size, 5, "Low power axis keeps distinct fractional ticks");
  assert.equal(elements.get("#data-rows").children.length, 2, "Accessible table contains samples");
  assert.equal(elements.get("#axes").children.length, 5);
  elements.get("#curve").keydown({key: "End", preventDefault() {}});
  assert.match(elements.get("#point-detail").textContent, /Battery 70%/);
  elements.get("#curve").pointerdown({clientX: 36});
  assert.match(elements.get("#point-detail").textContent, /Battery 71%/);
  drawing.length = 0;
  run('drawLine(canvas.getContext("2d"), [{at:"2026-01-01T00:00Z",value:1},{at:"2026-01-01T02:00Z",value:2}], Date.parse("2026-01-01T00:00Z"), Date.parse("2026-01-02T00:00Z"),800,300,10,"red",false,600000)');
  assert.equal(drawing.filter(call => call[0] === "lineTo").length, 0, "Missing telemetry has a visible gap");
  run('loadCurve()');
  await resolve(calls.at(-1), "wrong-property", resumedDay, 99);
  assert.equal(elements.get("#battery-now").textContent, "—", "Wrong response context is rejected");
  console.log("Dashboard browser behavior: passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
