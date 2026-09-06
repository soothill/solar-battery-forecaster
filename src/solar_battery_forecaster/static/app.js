const canvas = document.querySelector("#curve");
const dayInput = document.querySelector("#day");
const propertyInput = document.querySelector("#property");
const statusEl = document.querySelector("#status");
const processesEl = document.querySelector("#processes");
const logMessagesEl = document.querySelector("#log-messages");
const statusUpdatedEl = document.querySelector("#status-updated");
let payload;
let properties = [];
let curveRequest = 0;
let controller;
let followToday = true;
let inspectionIndex = 0;
let inspectionRows = [];

const css = getComputedStyle(document.documentElement);
const colors = {
  grid: css.getPropertyValue("--grid"), forecast: css.getPropertyValue("--sun"),
  actual: css.getPropertyValue("--sun-actual"), planned: css.getPropertyValue("--battery-plan"),
  actualSoc: css.getPropertyValue("--battery-actual"), cheap: css.getPropertyValue("--cheap"),
  muted: css.getPropertyValue("--muted")
};

function localDay() {
  const timezone = properties.find(item => item.id === propertyInput.value)?.timezone || "UTC";
  const parts = new Intl.DateTimeFormat("en-GB", {timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit"}).formatToParts(new Date());
  const part = type => parts.find(item => item.type === type).value;
  return `${part("year")}-${part("month")}-${part("day")}`;
}

function drawLine(ctx, points, start, stop, width, height, max, color, dashed = false, gap = Infinity) {
  if (!points.length) return;
  ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = 2.5; ctx.setLineDash(dashed ? [7, 6] : []);
  points.forEach((point, index) => {
    const x = 36 + (Date.parse(point.at) - start) / (stop - start) * (width - 48);
    const y = 10 + (1 - point.value / max) * (height - 28);
    index && Date.parse(point.at) - Date.parse(points[index - 1].at) <= gap ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.stroke(); ctx.setLineDash([]);
  points.forEach(point => {
    const x = 36 + (Date.parse(point.at) - start) / (stop - start) * (width - 48);
    const y = 10 + (1 - point.value / max) * (height - 28);
    ctx.fillStyle = color; ctx.fillRect(x - 1.5, y - 1.5, 3, 3);
  });
}

function render() {
  if (!payload) return;
  const ratio = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * ratio; canvas.height = rect.height * ratio;
  const ctx = canvas.getContext("2d"); ctx.scale(ratio, ratio);
  const width = rect.width, height = rect.height;
  const allPv = [...payload.series.forecast_generation_kw, ...payload.series.actual_generation_kw];
  const pvMax = Math.max(1, ...allPv.map(p => p.value)) * 1.12;
  const start = Date.parse(payload.window.start);
  const stop = Date.parse(payload.window.stop);
  const telemetryGap = (payload.telemetry_expected_interval_seconds || 300) * 2000;
  ctx.font = "11px system-ui"; ctx.fillStyle = colors.muted;
  ctx.globalAlpha = .09; ctx.fillStyle = colors.cheap;
  payload.series.cheap_rate_intervals.forEach(point => {
    const x = 36 + (Date.parse(point.start) - start) / (stop - start) * (width - 48);
    const intervalWidth = (Date.parse(point.stop) - Date.parse(point.start)) / (stop - start) * (width - 48);
    ctx.fillRect(x, 10, intervalWidth, height - 28);
  });
  const reserveY = 10 + (1 - payload.limits.minimum_soc_percent / 100) * (height - 28);
  ctx.fillStyle = colors.actualSoc; ctx.fillRect(36, reserveY, width - 48, height - 18 - reserveY);
  ctx.globalAlpha = 1;
  for (let i = 0; i <= 4; i++) {
    const y = 10 + i / 4 * (height - 28); ctx.strokeStyle = colors.grid; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(36, y); ctx.lineTo(width - 12, y); ctx.stroke();
    ctx.fillText(`${(pvMax * (1 - i / 4)).toFixed(pvMax < 4 ? 1 : 0)}kW`, 2, y + 3);
    const percent = `${100 - i * 25}%`; ctx.fillText(percent, width - ctx.measureText(percent).width, y + 3);
  }
  ctx.save(); ctx.beginPath(); ctx.rect(36, 10, width - 48, height - 28); ctx.clip();
  drawLine(ctx, payload.series.forecast_generation_kw, start, stop, width, height, pvMax, colors.forecast, true, 3600000);
  drawLine(ctx, payload.series.actual_generation_kw, start, stop, width, height, pvMax, colors.actual, false, telemetryGap);
  drawLine(ctx, payload.series.planned_soc_percent, start, stop, width, height, 100, colors.planned, true);
  drawLine(ctx, payload.series.actual_soc_percent, start, stop, width, height, 100, colors.actualSoc, false, telemetryGap);
  ctx.restore();
  const axes = document.querySelector("#axes"); axes.replaceChildren();
  for (let i = 0; i <= 4; i++) {
    addText(axes, "span", new Date(start + (stop - start) * i / 4).toLocaleTimeString("en-GB", {timeZone: payload.timezone, hour: "2-digit", minute: "2-digit", timeZoneName: "short"}));
  }
}

function clearCurve() {
  payload = undefined;
  canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
  ["forecast-total", "actual-total", "battery-now", "battery-target", "grid-charge", "charge-cost"].forEach(id => { document.querySelector(`#${id}`).textContent = "—"; });
  ["assumption", "factor", "freshness", "energy-quality", "shortfalls", "point-detail"].forEach(id => { document.querySelector(`#${id}`).textContent = ""; });
  document.querySelector("#data-rows").replaceChildren();
  document.querySelector("#axes").replaceChildren();
  inspectionRows = [];
}

function renderTable() {
  const series = ["forecast_generation_kw", "actual_generation_kw", "planned_soc_percent", "actual_soc_percent"];
  const rows = new Map();
  series.forEach((name, column) => payload.series[name].forEach(point => {
    if (!rows.has(point.at)) rows.set(point.at, {at: point.at, values: [null, null, null, null]});
    rows.get(point.at).values[column] = point.value;
  }));
  inspectionRows = [...rows.values()].sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
  const body = document.querySelector("#data-rows"); body.replaceChildren();
  inspectionRows.forEach(row => {
    const tr = document.createElement("tr");
    addText(tr, "th", formatPropertyTime(row.at));
    row.values.forEach(value => addText(tr, "td", value ?? "—")); body.append(tr);
  });
  inspectionIndex = 0;
}

function formatPropertyTime(at) {
  return new Date(at).toLocaleString("en-GB", {timeZone: payload.timezone, timeZoneName: "short"});
}

function inspectPoint(index) {
  if (!inspectionRows.length) return;
  inspectionIndex = Math.max(0, Math.min(inspectionRows.length - 1, index));
  const row = inspectionRows[inspectionIndex];
  document.querySelector("#point-detail").textContent = `${formatPropertyTime(row.at)} · Forecast ${row.values[0] ?? "—"} kW · Actual ${row.values[1] ?? "—"} kW · Planned ${row.values[2] ?? "—"}% · Battery ${row.values[3] ?? "—"}%`;
}

async function loadCurve() {
  const request = ++curveRequest;
  controller?.abort(); controller = new AbortController();
  const signal = controller.signal;
  const property = propertyInput.value, day = dayInput.value;
  clearCurve();
  statusEl.textContent = "Loading…";
  try {
    const response = await fetch(`/api/v1/properties/${encodeURIComponent(property)}/curve?date=${day}`, {signal, cache: "no-store"});
    if (!response.ok) throw new Error("No data available");
    const result = await response.json();
    if (request !== curveRequest || signal.aborted) return;
    if (result.property_id !== property || result.local_date !== day) throw new Error("Data context mismatch");
    payload = result;
    document.querySelector("#forecast-total").textContent = payload.summary.forecast_generation_kwh ?? "—";
    document.querySelector("#actual-total").textContent = payload.summary.actual_generation_kwh ?? "—";
    document.querySelector("#battery-now").textContent = payload.summary.latest_actual_soc_percent ?? "—";
    document.querySelector("#battery-target").textContent = payload.summary.target_soc_percent ?? "—";
    document.querySelector("#grid-charge").textContent = payload.summary.recommended_grid_charge_kwh ?? "—";
    document.querySelector("#charge-cost").textContent = payload.summary.estimated_charge_cost_pence ?? "—";
    document.querySelector("#assumption").textContent = `Planned battery: ${payload.assumptions.soc_projection}`;
    document.querySelector("#factor").textContent = `Forecast calibration factor: ${payload.summary.correction_factor ?? "not learned yet"}`;
    const issued = payload.summary.forecast_issued_at ? formatPropertyTime(payload.summary.forecast_issued_at) : "unavailable";
    const observed = payload.summary.latest_observed_at;
    const age = observed ? Math.max(0, (Date.now() - Date.parse(observed)) / 1000) : null;
    document.querySelector("#freshness").textContent = `Battery observation: ${observed ? formatPropertyTime(observed) : "unavailable"} · age ${compactAge(age)}${age > 600 ? " · stale" : ""}. Chart checked ${formatPropertyTime(payload.generated_at)}. Forecast issued ${issued}.`;
    document.querySelector("#energy-quality").textContent = `Generated energy: ${payload.summary.actual_energy_quality} · ${(100 * payload.summary.actual_energy_coverage_fraction).toFixed(1)}% daily coverage · ${(payload.summary.actual_energy_reason_codes || []).join(", ") || "complete counter coverage"}. Gaps have no connecting line; — means unavailable.`;
    document.querySelector("#shortfalls").textContent = payload.plan.available ? `Capacity shortfall ${payload.summary.capacity_shortfall_kwh} kWh · Charging window shortfall ${payload.summary.window_shortfall_kwh} kWh · Expected grid import ${payload.summary.unavoidable_grid_import_kwh} kWh · Reserve shortfall ${payload.summary.reserve_shortfall_kwh} kWh. Target at ${formatPropertyTime(payload.plan.target_soc_at)}.` : "Plan unavailable for this date and forecast snapshot.";
    statusEl.textContent = `${property} · ${day} · ${payload.timezone}`;
    render(); renderTable();
  } catch (error) {
    if (request !== curveRequest || signal.aborted) return;
    clearCurve(); statusEl.textContent = `${property} · ${day}: ${error.message}`;
  }
}

async function start() {
  const response = await fetch("/api/v1/properties");
  if (!response.ok) throw new Error("Property list unavailable");
  const data = await response.json();
  properties = data.properties;
  const requested = new URLSearchParams(location.search).get("property");
  data.properties.forEach(item => {
    const option = document.createElement("option");
    option.value = item.id; option.textContent = item.id;
    option.selected = item.id === requested; propertyInput.append(option);
  });
  dayInput.value = localDay();
  await loadCurve();
  await loadStatus();
  window.setInterval(refresh, 30000);
}

function refresh() {
  if (document.hidden) return;
  loadStatus();
  if (followToday) dayInput.value = localDay();
  if (dayInput.value === localDay()) loadCurve();
}

function formatTime(value) {
  return value ? new Date(value).toLocaleString() : "Never";
}

function addText(parent, tag, text, className) {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  parent.append(element);
  return element;
}

function compactBytes(value) {
  if (value === null || value === undefined) return "—";
  if (value < 1024) return `${value} B`;
  return `${(value / 1024).toFixed(1)} KiB`;
}

function compactAge(value) {
  if (value === null || value === undefined) return "—";
  if (value < 60) return `${Math.round(value)}s`;
  if (value < 3600) return `${Math.round(value / 60)}m`;
  return `${(value / 3600).toFixed(1)}h`;
}

function processState(service) {
  if (service.lifecycle === "stopped") return "stopped";
  if (service.stale || service.lifecycle === "missing") return "stale";
  if (service.last_cycle_result === "failed") return "failed";
  const outbox = service.outbox || {};
  if (outbox.delivery_paused || outbox.quarantined_records || outbox.blocked_streams) return "blocked";
  if (outbox.pending_records) return "backlog";
  return service.lifecycle || "unknown";
}

function renderStatus(data) {
  processesEl.replaceChildren();
  logMessagesEl.replaceChildren();
  const events = [];
  data.services.forEach(service => {
    const card = document.createElement("article");
    const state = processState(service);
    const outbox = service.outbox || {};
    const syslog = service.syslog || {};
    addText(card, "strong", service.service, "process-name");
    addText(card, "span", state, `pill ${state}`);
    addText(card, "small", `Last cycle: ${formatTime(service.last_cycle_completed_at)} · ${service.last_cycle_result || "never"}`);
    addText(card, "small", `Last accepted: ${formatTime(service.last_local_accepted_at)}`);
    addText(card, "small", `Last direct: ${formatTime(service.last_direct_delivery_at)}`);
    addText(card, "small", `Last buffered: ${formatTime(service.last_buffered_at)}`);
    addText(card, "small", `Last confirmed: ${formatTime(service.last_confirmed_delivery_at)}`);
    if (service.outbox) {
      addText(card, "small", `Fallback: ${outbox.pending_records ?? 0} record(s) · ${compactBytes(outbox.pending_bytes)} · oldest ${compactAge(outbox.oldest_pending_age_seconds)}`);
      addText(card, "small", `Blocked: ${outbox.blocked_streams ?? 0} · quarantine: ${outbox.quarantined_records ?? 0}`);
    }
    addText(card, "small", `Syslog: ${syslog.enabled ? "enabled" : "off"} · dropped ${syslog.dropped ?? 0} · last failure ${formatTime(syslog.last_failure_at)}`);
    processesEl.append(card);
    (service.events || []).forEach(event => events.push({...event, service: service.service}));
  });
  events.sort((left, right) => String(right.at).localeCompare(String(left.at)));
  events.slice(0, 50).forEach(event => {
    const row = document.createElement("div");
    addText(row, "time", formatTime(event.at));
    addText(row, "strong", `${event.service} · ${event.severity}`);
    addText(row, "span", event.message);
    logMessagesEl.append(row);
  });
  if (!events.length) addText(logMessagesEl, "p", "No recent messages.", "note");
  statusUpdatedEl.textContent = `Updated ${formatTime(data.generated_at)}`;
}

async function loadStatus() {
  try {
    const response = await fetch("/api/status", {cache: "no-store"});
    if (!response.ok) throw new Error("Status unavailable");
    renderStatus(await response.json());
  } catch (error) {
    statusUpdatedEl.textContent = error.message;
  }
}

dayInput.addEventListener("change", () => { followToday = dayInput.value === localDay(); loadCurve(); });
propertyInput.addEventListener("change", () => {
  const url = new URL(location.href); url.searchParams.set("property", propertyInput.value);
  history.replaceState(null, "", url);
  if (followToday) dayInput.value = localDay();
  loadCurve();
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) { controller?.abort(); curveRequest++; }
  else { if (followToday) dayInput.value = localDay(); loadCurve(); loadStatus(); }
});
window.addEventListener("online", refresh);
canvas.addEventListener("keydown", event => {
  if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
    event.preventDefault();
    inspectPoint(event.key === "Home" ? 0 : event.key === "End" ? inspectionRows.length - 1 : inspectionIndex + (event.key === "ArrowRight" ? 1 : -1));
  }
});
canvas.addEventListener("pointerdown", event => {
  if (!payload || !inspectionRows.length) return;
  const rect = canvas.getBoundingClientRect();
  const fraction = Math.max(0, Math.min(1, (event.clientX - rect.left - 36) / (rect.width - 48)));
  const at = Date.parse(payload.window.start) + fraction * (Date.parse(payload.window.stop) - Date.parse(payload.window.start));
  inspectPoint(inspectionRows.reduce((best, row, i) => Math.abs(Date.parse(row.at) - at) < Math.abs(Date.parse(inspectionRows[best].at) - at) ? i : best, 0));
});
window.addEventListener("resize", render);
start().catch(error => { statusEl.textContent = error.message; });
