const canvas = document.querySelector("#curve");
const dayInput = document.querySelector("#day");
const propertyInput = document.querySelector("#property");
const statusEl = document.querySelector("#status");
const processesEl = document.querySelector("#processes");
const logMessagesEl = document.querySelector("#log-messages");
const statusUpdatedEl = document.querySelector("#status-updated");
let payload;

const css = getComputedStyle(document.documentElement);
const colors = {
  grid: css.getPropertyValue("--grid"), forecast: css.getPropertyValue("--sun"),
  actual: css.getPropertyValue("--sun-actual"), planned: css.getPropertyValue("--battery-plan"),
  actualSoc: css.getPropertyValue("--battery-actual"), cheap: css.getPropertyValue("--cheap"),
  muted: css.getPropertyValue("--muted")
};

function localDay() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

function drawLine(ctx, points, start, stop, width, height, max, color, dashed = false) {
  if (!points.length) return;
  ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = 2.5; ctx.setLineDash(dashed ? [7, 6] : []);
  points.forEach((point, index) => {
    const x = 36 + (Date.parse(point.at) - start) / (stop - start) * (width - 48);
    const y = 10 + (1 - point.value / max) * (height - 28);
    index ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.stroke(); ctx.setLineDash([]);
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
    ctx.fillText(`${Math.round(pvMax * (1 - i / 4))}kW`, 2, y + 3);
    const percent = `${100 - i * 25}%`; ctx.fillText(percent, width - ctx.measureText(percent).width, y + 3);
  }
  drawLine(ctx, payload.series.forecast_generation_kw, start, stop, width, height, pvMax, colors.forecast, true);
  drawLine(ctx, payload.series.actual_generation_kw, start, stop, width, height, pvMax, colors.actual);
  drawLine(ctx, payload.series.planned_soc_percent, start, stop, width, height, 100, colors.planned, true);
  drawLine(ctx, payload.series.actual_soc_percent, start, stop, width, height, 100, colors.actualSoc);
}

async function loadCurve() {
  statusEl.textContent = "Loading…";
  const property = propertyInput.value;
  try {
    const response = await fetch(`/api/v1/properties/${encodeURIComponent(property)}/curve?date=${dayInput.value}`);
    if (!response.ok) throw new Error("No data available");
    payload = await response.json();
    document.querySelector("#forecast-total").textContent = payload.summary.forecast_generation_kwh ?? "—";
    document.querySelector("#actual-total").textContent = payload.summary.actual_generation_kwh ?? "—";
    document.querySelector("#battery-now").textContent = payload.summary.latest_actual_soc_percent ?? "—";
    document.querySelector("#battery-target").textContent = payload.summary.target_soc_percent ?? "—";
    document.querySelector("#grid-charge").textContent = payload.summary.recommended_grid_charge_kwh ?? "—";
    document.querySelector("#charge-cost").textContent = payload.summary.estimated_charge_cost_pence ?? "—";
    document.querySelector("#assumption").textContent = `Planned battery: ${payload.assumptions.soc_projection}`;
    document.querySelector("#factor").textContent = `Forecast calibration factor: ${payload.summary.correction_factor ?? "not learned yet"}`;
    const issued = payload.summary.forecast_issued_at ? new Date(payload.summary.forecast_issued_at).toLocaleString() : "unknown";
    statusEl.textContent = `Forecast ${issued}`; render();
  } catch (error) { statusEl.textContent = error.message; }
}

async function start() {
  dayInput.value = localDay();
  const response = await fetch("/api/v1/properties");
  if (!response.ok) throw new Error("Property list unavailable");
  const data = await response.json();
  const requested = new URLSearchParams(location.search).get("property");
  data.properties.forEach(item => {
    const option = document.createElement("option");
    option.value = item.id; option.textContent = item.id;
    option.selected = item.id === requested; propertyInput.append(option);
  });
  await loadCurve();
  await loadStatus();
  window.setInterval(loadStatus, 30000);
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

dayInput.addEventListener("change", loadCurve);
propertyInput.addEventListener("change", () => {
  const url = new URL(location.href); url.searchParams.set("property", propertyInput.value);
  history.replaceState(null, "", url); loadCurve();
});
window.addEventListener("resize", render);
start().catch(error => { statusEl.textContent = error.message; });
