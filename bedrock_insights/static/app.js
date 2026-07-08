let CONFIG = { refresh_seconds: 5, threshold: null, periods: [], regions: [] };
let STATE = {
  period: null,
  since: null,
  filters: {}, // dim -> {value, label}; dims: model | identity | region (stackable)
  window: null, // {start, end, label} — absolute drill-in from a chart bucket
  tab: "overview",
};
let REFRESH_TIMER = null;

// ── Safe DOM construction ────────────────────────────────────────────────────
// The dashboard renders data sourced from CloudWatch logs (model IDs, identity
// ARNs, error codes/messages, region names) that an attacker who can invoke
// Bedrock could influence. We never assemble HTML from those strings; instead we
// build elements with these helpers so all text goes through textContent, which
// cannot execute markup. This removes the XSS class entirely (no innerHTML).
//
// h(tag, props?, children?) — create an element.
//   props: { class, title, style, text, dataset:{}, attrs:{}, on:{event:fn} }
//   children: node | string | array of (node | string); strings become text.
function h(tag, props, children) {
  const el = document.createElement(tag);
  if (props) {
    if (props.class) el.className = props.class;
    if (props.title != null) el.title = props.title;
    if (props.text != null) el.textContent = props.text;
    if (props.style)
      for (const k in props.style) el.style[k] = props.style[k];
    if (props.dataset)
      for (const k in props.dataset) el.dataset[k] = props.dataset[k];
    if (props.attrs)
      for (const k in props.attrs) el.setAttribute(k, props.attrs[k]);
    if (props.on)
      for (const k in props.on) el.addEventListener(k, props.on[k]);
  }
  append(el, children);
  return el;
}

// Append a node, a string (as text), or an array of them; ignores null/undefined.
function append(el, children) {
  if (children == null) return el;
  const list = Array.isArray(children) ? children : [children];
  for (const c of list) {
    if (c == null || c === false) continue;
    el.appendChild(typeof c === "object" ? c : document.createTextNode(String(c)));
  }
  return el;
}

// Remove all children (safe replacement for `el.innerHTML = ""`).
function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

// Replace an element's contents with the given node(s)/string(s).
function replaceChildren(el, children) {
  clear(el);
  return append(el, children);
}

function _windowParams(p) {
  // Absolute drill-in window wins, then a custom `since`, then the named period.
  if (STATE.window) {
    p.set("start", STATE.window.start);
    p.set("end", STATE.window.end);
  } else if (STATE.since) {
    p.set("since", STATE.since);
  } else if (STATE.period) {
    p.set("period", STATE.period);
  }
  // Stackable dimension filters (model + identity + region applied together).
  for (const dim in STATE.filters) p.set(dim, STATE.filters[dim].value);
}

function buildUsageUrl() {
  const p = new URLSearchParams();
  _windowParams(p);
  const q = p.toString();
  return "/api/usage" + (q ? "?" + q : "");
}

function downloadExport(fmt) {
  const p = new URLSearchParams();
  p.set("format", fmt);
  _windowParams(p);
  const a = document.createElement("a");
  a.href = "/api/export?" + p.toString();
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function applyRefreshInterval(seconds) {
  const s = Math.max(2, Number(seconds) || CONFIG.refresh_seconds || 5);
  if (REFRESH_TIMER) clearInterval(REFRESH_TIMER);
  REFRESH_TIMER = setInterval(refresh, s * 1000);
  document.getElementById("foot").textContent =
    "Refreshing every " +
    s +
    "s · CloudWatch polled every " +
    (CONFIG.poll_seconds || 5) +
    "s";
}

function _fmtClock(ms) {
  return new Date(ms).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function _syncRegionButtons() {
  const cur = STATE.filters.region ? STATE.filters.region.value : null;
  for (const b of document.querySelectorAll("#region-sel button"))
    b.classList.toggle("active", (b.dataset.region || null) === cur);
}

function setCustomWindow(since) {
  STATE.since = since || null;
  if (STATE.since) {
    STATE.window = null; // a custom duration overrides an absolute drill-in
    for (const b of document.querySelectorAll("#periods button"))
      b.classList.remove("active");
  }
  writeStateToUrl();
  refresh();
}

// Absolute drill-in window from clicking a chart bucket.
function setWindow(start, end) {
  STATE.window = { start, end, label: _fmtClock(start) + "–" + _fmtClock(end) };
  STATE.since = null;
  for (const b of document.querySelectorAll("#periods button"))
    b.classList.remove("active");
  renderFilterChips();
  writeStateToUrl();
  refresh();
}

function clearWindow() {
  STATE.window = null;
  renderFilterChips();
  setPeriod(STATE.period || CONFIG.default_period);
}

function setRegion(region) {
  if (region) STATE.filters.region = { value: region, label: region };
  else delete STATE.filters.region;
  _syncRegionButtons();
  renderFilterChips();
  writeStateToUrl();
  refresh();
}

function setFilter(dim, value, label) {
  STATE.filters[dim] = { value, label: label || value };
  if (dim === "region") _syncRegionButtons();
  renderFilterChips();
  writeStateToUrl();
  refresh();
}

function removeFilter(dim) {
  delete STATE.filters[dim];
  if (dim === "region") _syncRegionButtons();
  renderFilterChips();
  writeStateToUrl();
  refresh();
}

// Build one breadcrumb chip: "<label> = <value> [✕]".
function _filterChip(labelText, valueText, removeKey, removeTitle, removeAria) {
  return h("span", { class: "chip" }, [
    h("b", { text: labelText }),
    " = " + valueText,
    h("button", {
      text: "✕",
      dataset: { remove: removeKey },
      title: removeTitle,
      attrs: { "aria-label": removeAria },
    }),
  ]);
}

function renderFilterChips() {
  const chip = document.getElementById("filter-chip");
  const parts = [];
  for (const dim in STATE.filters) {
    parts.push(
      _filterChip(
        dim,
        STATE.filters[dim].label,
        dim,
        "Remove filter",
        "Remove " + dim + " filter",
      ),
    );
  }
  if (STATE.window) {
    parts.push(
      _filterChip(
        "time",
        STATE.window.label,
        "__window__",
        "Clear time window",
        "Clear time window",
      ),
    );
  }
  replaceChildren(chip, parts);
  chip.classList.toggle("show", parts.length > 0);
}

function setPeriod(id) {
  STATE.period = id;
  STATE.since = null; // a named period overrides a custom window
  STATE.window = null; // …and an absolute drill-in
  document.getElementById("set-since").value = "";
  for (const b of document.querySelectorAll("#periods button"))
    b.classList.toggle("active", b.dataset.id === id);
  renderFilterChips();
  writeStateToUrl();
  refresh();
}

let SETTINGS = { threshold: null, webhook_url: null };

async function loadSettings() {
  try {
    const r = await fetch("/api/settings");
    SETTINGS = await r.json();
    const setVal = (id, v) =>
      (document.getElementById(id).value =
        v === null || v === undefined ? "" : v);
    setVal("set-threshold", SETTINGS.threshold);
    setVal("set-daily-budget", SETTINGS.daily_budget);
    setVal("set-monthly-budget", SETTINGS.monthly_budget);
    document.getElementById("set-webhook").value = SETTINGS.webhook_url || "";
    document.getElementById("set-sns").value = SETTINGS.sns_topic_arn || "";
  } catch (e) {
    /* ignore */
  }
}

function setStatus(msg, kind) {
  const el = document.getElementById("set-status");
  el.textContent = msg;
  el.className = "setstatus" + (kind ? " " + kind : "");
}

async function saveSettings() {
  const val = (id) => {
    const v = document.getElementById(id).value.trim();
    return v === "" ? null : v;
  };
  const wv = document.getElementById("set-webhook").value.trim();
  const snsv = document.getElementById("set-sns").value.trim();
  setStatus("Saving…");
  try {
    const r = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        threshold: val("set-threshold"),
        daily_budget: val("set-daily-budget"),
        monthly_budget: val("set-monthly-budget"),
        webhook_url: wv || null,
        sns_topic_arn: snsv || null,
      }),
    });
    const data = await r.json();
    if (data.error) {
      setStatus(data.error, "bad");
      toast(data.error, "bad");
      return;
    }
    SETTINGS = data;
    setStatus("Saved.", "ok");
    toast("Settings saved", "ok");
    refreshBudgets();
  } catch (e) {
    setStatus("Save failed.", "bad");
    toast("Save failed", "bad");
  }
}

async function testWebhook() {
  const wv = document.getElementById("set-webhook").value.trim();
  const snsv = document.getElementById("set-sns").value.trim();
  if (!wv && !snsv) {
    setStatus("Enter a webhook URL or SNS topic ARN first.", "bad");
    return;
  }
  setStatus("Sending test…");
  try {
    const r = await fetch("/api/test-webhook", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        webhook_url: wv || null,
        sns_topic_arn: snsv || null,
      }),
    });
    const data = await r.json();
    const msg = data.ok
      ? "Test sent (HTTP " + data.message + ")."
      : "Failed: " + data.message;
    setStatus(msg, data.ok ? "ok" : "bad");
    toast(msg, data.ok ? "ok" : "bad");
  } catch (e) {
    setStatus("Test failed.", "bad");
    toast("Webhook test failed", "bad");
  }
}

function fmtTokens(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e4) return (n / 1e3).toFixed(1) + "K";
  return n.toLocaleString();
}
function fmtCost(c) {
  const a = Math.abs(c);
  if (a >= 1) return "$" + c.toFixed(2); // $96.50, $8.41
  if (a >= 0.01) return "$" + c.toFixed(4); // $0.0234
  return "$" + c.toFixed(5); // sub-cent precision
}
function fmtPct(x) {
  return (x * 100).toFixed(1) + "%";
}

// ── Animated metric updates (count-up) + skeleton removal ────────────────────
const _animTimers = {};
const _prefersReduced = () =>
  window.matchMedia &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function setMetricText(id, text) {
  const el = document.getElementById(id);
  el.classList.remove("skeleton");
  if (_animTimers[id]) {
    cancelAnimationFrame(_animTimers[id]);
    delete _animTimers[id];
  }
  el.dataset.val = "";
  el.textContent = text;
}

function animateMetric(id, value, fmt) {
  const el = document.getElementById(id);
  el.classList.remove("skeleton");
  if (value === null || value === undefined || isNaN(value)) {
    setMetricText(id, "—");
    return;
  }
  const to = Number(value);
  const from =
    el.dataset.val !== undefined && el.dataset.val !== ""
      ? Number(el.dataset.val)
      : 0;
  el.dataset.val = String(to);
  if (_animTimers[id]) cancelAnimationFrame(_animTimers[id]);
  if (_prefersReduced() || from === to) {
    el.textContent = fmt(to);
    return;
  }
  const dur = 380,
    t0 = performance.now();
  function step(now) {
    const k = Math.min(1, (now - t0) / dur);
    const eased = 1 - Math.pow(1 - k, 3);
    el.textContent = fmt(from + (to - from) * eased);
    if (k < 1) {
      _animTimers[id] = requestAnimationFrame(step);
    } else {
      el.textContent = fmt(to);
      delete _animTimers[id];
    }
  }
  _animTimers[id] = requestAnimationFrame(step);
}

// ── Shareable state in the URL hash (token never goes here) ──────────────────
function writeStateToUrl() {
  const p = new URLSearchParams();
  if (STATE.window) {
    p.set("start", STATE.window.start);
    p.set("end", STATE.window.end);
  } else if (STATE.since) {
    p.set("since", STATE.since);
  } else if (STATE.period) {
    p.set("period", STATE.period);
  }
  for (const dim in STATE.filters) {
    p.set("f_" + dim, STATE.filters[dim].value);
    if (STATE.filters[dim].label !== STATE.filters[dim].value)
      p.set("f_" + dim + "_l", STATE.filters[dim].label);
  }
  if (STATE.tab && STATE.tab !== "overview") p.set("tab", STATE.tab);
  const q = p.toString();
  history.replaceState(null, "", q ? "#" + q : location.pathname);
}

function readStateFromUrl() {
  const h = location.hash.replace(/^#/, "");
  if (!h) return;
  const p = new URLSearchParams(h);
  if (p.get("since")) STATE.since = p.get("since");
  if (p.get("period")) STATE.period = p.get("period");
  const st = p.get("start"),
    en = p.get("end");
  if (st && en)
    STATE.window = {
      start: +st,
      end: +en,
      label: _fmtClock(+st) + "–" + _fmtClock(+en),
    };
  STATE.filters = {};
  for (const dim of ["model", "identity", "region"]) {
    const v = p.get("f_" + dim);
    if (v)
      STATE.filters[dim] = { value: v, label: p.get("f_" + dim + "_l") || v };
  }
  if (p.get("tab")) STATE.tab = p.get("tab");
}

function applyStateToUi() {
  for (const b of document.querySelectorAll("#periods button"))
    b.classList.toggle(
      "active",
      !STATE.since && !STATE.window && b.dataset.id === STATE.period,
    );
  _syncRegionButtons();
  document.getElementById("set-since").value = STATE.since || "";
  renderFilterChips();
  setTab(STATE.tab || "overview");
}

// ── Toast notifications ──────────────────────────────────────────────────────
function toast(msg, kind) {
  const box = document.getElementById("toasts");
  if (!box) return;
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  el.textContent = msg;
  box.appendChild(el);
  requestAnimationFrame(() => el.classList.add("show"));
  setTimeout(() => {
    el.classList.remove("show");
    setTimeout(() => el.remove(), 220);
  }, 2600);
}

// A toast that stays on screen (with a close button) until dismissed, instead
// of auto-fading — used for things the user might miss otherwise (e.g. an
// anomaly flagged on a trend bucket that's no longer "latest" by the next
// poll). Deduped by `key` so repeated polls for the same event don't stack up.
const _persistentToastKeys = new Set();
function persistentToast(msg, kind, key) {
  const box = document.getElementById("toasts");
  if (!box || (key && _persistentToastKeys.has(key))) return;
  if (key) _persistentToastKeys.add(key);
  const el = document.createElement("div");
  el.className = "toast persistent" + (kind ? " " + kind : "");
  const text = document.createElement("span");
  text.textContent = msg;
  const close = document.createElement("button");
  close.textContent = "✕";
  close.setAttribute("aria-label", "Dismiss");
  close.addEventListener("click", () => {
    el.classList.remove("show");
    setTimeout(() => {
      el.remove();
      if (key) _persistentToastKeys.delete(key);
    }, 220);
  });
  el.append(text, close);
  box.appendChild(el);
  requestAnimationFrame(() => el.classList.add("show"));
}

// ── Inline SVG sparkline for a row ───────────────────────────────────────────
// Returns an <svg> DOM node (or null when there's nothing to draw). Built from
// purely numeric data via createElementNS — no markup strings.
function sparklineSvg(values) {
  const v = values || [];
  const max = v.length ? Math.max.apply(null, v) : 0;
  if (v.length < 2 || max <= 0) return null;
  const w = 60,
    height = 18,
    step = w / (v.length - 1);
  const pts = v
    .map(
      (x, i) =>
        (i * step).toFixed(1) +
        "," +
        (height - (x / max) * (height - 2) - 1).toFixed(1),
    )
    .join(" ");
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", "spark");
  svg.setAttribute("width", String(w));
  svg.setAttribute("height", String(height));
  svg.setAttribute("viewBox", "0 0 " + w + " " + height);
  svg.setAttribute("aria-hidden", "true");
  const poly = document.createElementNS(NS, "polyline");
  poly.setAttribute("points", pts);
  svg.appendChild(poly);
  return svg;
}

// Model-table sort state + last payload (so header clicks can re-sort without a refetch).
let SORT = { key: "cost", dir: -1 }; // dir: 1 asc, -1 desc
let LAST_DATA = null;

function positionTabUnderline() {
  const u = document.getElementById("tab-underline");
  const active = document.querySelector(".tab.active");
  if (!u || !active) return;
  u.style.left = active.offsetLeft + "px";
  u.style.width = active.offsetWidth + "px";
}

let CHART = null; // {pts, geometry...} kept so hover can redraw with a highlight
let LAST_TREND = null; // last trend payload, so a theme switch can redraw the chart

function _chartColors() {
  const cs = getComputedStyle(document.documentElement);
  const v = (name, fallback) => cs.getPropertyValue(name).trim() || fallback;
  return {
    grid: v("--border", "#2a3340"),
    label: v("--muted", "#8b949e"),
    bar: v("--magenta", "#bc8cff"),
    line: v("--accent", "#58a6ff"),
  };
}

function _niceCeil(v) {
  // Round up to a "nice" axis maximum (1, 2, 2.5, 5, 10 × 10^n).
  if (v <= 0) return 1;
  const exp = Math.floor(Math.log10(v));
  const base = Math.pow(10, exp);
  const f = v / base;
  const nf = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
  return nf * base;
}

function _fmtAxisCost(v) {
  if (v >= 1) return "$" + v.toFixed(2);
  if (v >= 0.01) return "$" + v.toFixed(3);
  return "$" + v.toFixed(5);
}

function renderChart(trend) {
  LAST_TREND = trend;
  const canvas = document.getElementById("chart");
  const pts = (trend && trend.points) || [];
  const bs = trend ? trend.bucket_seconds : 60;
  const bucketLabel = bs >= 3600 ? bs / 3600 + "h" : bs / 60 + "m";
  document.getElementById("chart-meta").textContent = pts.length
    ? pts.length + " × " + bucketLabel + " buckets"
    : "";

  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || canvas.parentElement.clientWidth - 32;
  const cssH = 160;
  // Canvas is hidden (e.g. the Recent tab is active) → skip drawing; LAST_TREND
  // is retained so setTab can redraw it correctly once Overview is visible.
  if (cssW <= 0) return;
  canvas.width = cssW * dpr;
  canvas.height = cssH * dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  if (!pts.length) {
    ctx.clearRect(0, 0, cssW, cssH);
    ctx.fillStyle = _chartColors().label;
    ctx.font = "12px sans-serif";
    ctx.fillText("No data in this window yet…", 8, 22);
    CHART = null;
    return;
  }

  const pad = { l: 52, r: 10, t: 14, b: 22 };
  const w = cssW - pad.l - pad.r,
    h = cssH - pad.t - pad.b;
  const maxCost = Math.max.apply(null, pts.map((p) => p.cost).concat(1e-9));
  const scaleMax = _niceCeil(maxCost);
  const slot = w / pts.length;
  CHART = { pts, pad, w, h, slot, cssW, cssH, scaleMax, bucketSeconds: bs };
  _drawChart(-1);
}

function _drawChart(highlight) {
  if (!CHART) return;
  const ctx = document.getElementById("chart").getContext("2d");
  const { pts, pad, w, h, slot, cssW, cssH, scaleMax } = CHART;
  const col = _chartColors();
  ctx.clearRect(0, 0, cssW, cssH);

  // Horizontal gridlines + Y-axis ($) labels in the left gutter.
  const ticks = 4;
  ctx.font = "10px sans-serif";
  ctx.textBaseline = "middle";
  for (let g = 0; g <= ticks; g++) {
    const frac = g / ticks;
    const y = pad.t + h - frac * h;
    ctx.strokeStyle = col.grid;
    ctx.globalAlpha = g === 0 ? 1 : 0.45;
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(pad.l + w, y);
    ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.fillStyle = col.label;
    ctx.textAlign = "right";
    ctx.fillText(_fmtAxisCost(scaleMax * frac), pad.l - 6, y);
  }

  // Bars (brighten the hovered one).
  const bw = Math.max(1, slot - 2);
  for (let i = 0; i < pts.length; i++) {
    const bh = (pts[i].cost / scaleMax) * h;
    ctx.fillStyle = col.bar;
    ctx.globalAlpha = i === highlight ? 1 : 0.82;
    ctx.fillRect(pad.l + i * slot + 1, pad.t + h - bh, bw, bh);
  }
  ctx.globalAlpha = 1;

  // Vertical reference line at the hovered bucket.
  if (highlight >= 0 && highlight < pts.length) {
    const x = pad.l + highlight * slot + slot / 2;
    ctx.strokeStyle = col.line;
    ctx.globalAlpha = 0.55;
    ctx.beginPath();
    ctx.moveTo(x, pad.t);
    ctx.lineTo(x, pad.t + h);
    ctx.stroke();
    ctx.globalAlpha = 1;
  }

  // X-axis time labels: start, middle, end.
  const fmtT = (ms) =>
    new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  ctx.fillStyle = col.label;
  ctx.textBaseline = "alphabetic";
  ctx.textAlign = "left";
  ctx.fillText(fmtT(pts[0].t), pad.l, cssH - 6);
  if (pts.length > 2) {
    ctx.textAlign = "center";
    ctx.fillText(
      fmtT(pts[Math.floor(pts.length / 2)].t),
      pad.l + w / 2,
      cssH - 6,
    );
  }
  ctx.textAlign = "right";
  ctx.fillText(fmtT(pts[pts.length - 1].t), pad.l + w, cssH - 6);
}

function _chartHover(e) {
  const tip = document.getElementById("chart-tip");
  if (!CHART) {
    tip.style.display = "none";
    return;
  }
  const canvas = document.getElementById("chart");
  const rect = canvas.getBoundingClientRect();
  const x = e.clientX - rect.left;
  const i = Math.floor((x - CHART.pad.l) / CHART.slot);
  if (i < 0 || i >= CHART.pts.length) {
    tip.style.display = "none";
    _drawChart(-1);
    return;
  }
  _drawChart(i);

  const p = CHART.pts[i];
  const fmtT = (ms) =>
    new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const end = p.t + CHART.bucketSeconds * 1000;
  replaceChildren(tip, [
    h("div", { text: fmtT(p.t) + "–" + fmtT(end) }),
    h("div", { class: "tcost", text: fmtCost(p.cost) }),
    h("div", {
      text: fmtTokens(p.total_tokens) + " tokens · " + p.calls + " calls",
    }),
  ]);

  const panel = document.getElementById("chart-panel");
  const prect = panel.getBoundingClientRect();
  tip.style.display = "block";
  const tw = tip.offsetWidth;
  let left =
    rect.left -
    prect.left +
    CHART.pad.l +
    i * CHART.slot +
    CHART.slot / 2 -
    tw / 2;
  left = Math.max(4, Math.min(left, panel.clientWidth - tw - 4));
  tip.style.left = left + "px";
  tip.style.top = rect.top - prect.top + 6 + "px";
}

(function bindChartHover() {
  const canvas = document.getElementById("chart");
  canvas.addEventListener("mousemove", _chartHover);
  canvas.addEventListener("mouseleave", () => {
    document.getElementById("chart-tip").style.display = "none";
    _drawChart(-1);
  });
  // Click a bucket to drill the whole dashboard into that time range.
  canvas.addEventListener("click", (e) => {
    if (!CHART) return;
    const rect = canvas.getBoundingClientRect();
    const i = Math.floor((e.clientX - rect.left - CHART.pad.l) / CHART.slot);
    if (i < 0 || i >= CHART.pts.length) return;
    const p = CHART.pts[i];
    setWindow(p.t, p.t + CHART.bucketSeconds * 1000);
  });
})();

function setStale(stale) {
  document.getElementById("pulse").classList.toggle("stale", stale);
}

async function loadConfig() {
  try {
    const r = await fetch("/api/config");
    CONFIG = await r.json();
    // Compact region summary in the header (full list only on hover) — the
    // region buttons below already list every region, so repeating them
    // here in full just adds a wide, redundant line.
    const regionList = CONFIG.regions || [];
    const regionMeta = document.getElementById("meta-region");
    if (regionList.length > 1) {
      replaceChildren(regionMeta, h("b", { text: regionList.length + " regions" }));
      regionMeta.title = regionList.join(", ");
    } else {
      replaceChildren(regionMeta, h("b", { text: CONFIG.region || "—" }));
      regionMeta.title = "";
    }
    STATE.period = CONFIG.default_period;
    const box = document.getElementById("periods");
    clear(box);
    for (const p of CONFIG.periods || []) {
      const btn = document.createElement("button");
      btn.textContent = p.label;
      btn.dataset.id = p.id;
      btn.classList.toggle("active", p.id === STATE.period);
      btn.addEventListener("click", () => setPeriod(p.id));
      box.appendChild(btn);
    }

    // Region selector — only when monitoring more than one region.
    const rsel = document.getElementById("region-sel");
    clear(rsel);
    const regions = CONFIG.regions || [];
    if (regions.length > 1) {
      const mk = (label, val) => {
        const b = document.createElement("button");
        b.textContent = label;
        b.dataset.region = val;
        b.classList.toggle(
          "active",
          (val || null) ===
            (STATE.filters.region ? STATE.filters.region.value : null),
        );
        b.addEventListener("click", () => setRegion(val));
        return b;
      };
      rsel.appendChild(mk("All regions", ""));
      for (const r of regions) rsel.appendChild(mk(r, r));
    }

    // Read-only runtime info + refresh default
    document.getElementById("info-regions").textContent =
      (regions.length ? regions.join(", ") : CONFIG.region) || "—";
    document.getElementById("info-bind").textContent = CONFIG.bind || "—";
    document.getElementById("info-poll").textContent =
      CONFIG.poll_seconds || "—";
    document.getElementById("set-refresh").value = CONFIG.refresh_seconds || 5;
  } catch (e) {
    /* keep defaults */
  }
}

// Click a model / identity row to drill in; click a region row to switch region.
document.addEventListener("click", (e) => {
  const tr = e.target.closest("tr.clickable");
  if (!tr || !tr.dataset.dim) return;
  if (tr.dataset.dim === "region") setRegion(tr.dataset.val);
  else setFilter(tr.dataset.dim, tr.dataset.val, tr.dataset.label);
});

// Expand/collapse a Recent row to view its prompt/response (click or Enter/Space).
(function bindRecentExpand() {
  const recent = document.getElementById("recent");
  recent.addEventListener("click", (e) => {
    const row = e.target.closest(".rec-row");
    if (row) toggleRecentDetail(row);
  });
  recent.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const row = e.target.closest(".rec-row");
    if (row) {
      e.preventDefault();
      toggleRecentDetail(row);
    }
  });
})();

// Remove a single filter / the time window from its breadcrumb chip.
document.getElementById("filter-chip").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-remove]");
  if (!btn) return;
  const dim = btn.dataset.remove;
  if (dim === "__window__") clearWindow();
  else removeFilter(dim);
});

// Click a sortable model-table header to sort (toggles direction).
document.addEventListener("click", (e) => {
  const th = e.target.closest("th.sortable");
  if (!th || !th.dataset.sort) return;
  const key = th.dataset.sort;
  if (SORT.key === key) SORT.dir = -SORT.dir;
  else SORT = { key, dir: key === "display_name" ? 1 : -1 };
  if (LAST_DATA) renderTable(LAST_DATA);
});

function renderError(msg) {
  // `msg` is a server/CloudWatch-sourced error string — render it as text.
  replaceChildren(
    document.getElementById("content"),
    h("div", { class: "err" }, [
      h("b", { text: "Could not load usage." }),
      h("br"),
      String(msg == null ? "" : msg),
    ]),
  );
}

function renderBanner(totals) {
  const banner = document.getElementById("banner");
  const threshold =
    SETTINGS.threshold != null ? SETTINGS.threshold : CONFIG.threshold;
  if (threshold != null && totals.cost_known && totals.cost >= threshold) {
    banner.textContent =
      "⚠  Threshold exceeded — " +
      fmtCost(totals.cost) +
      " ≥ $" +
      Number(threshold).toFixed(2);
    banner.classList.add("show");
  } else {
    banner.classList.remove("show");
  }
}

function _sortModels(models) {
  const k = SORT.key,
    dir = SORT.dir;
  return models.slice().sort((a, b) => {
    if (k === "display_name")
      return dir * String(a[k]).localeCompare(String(b[k]));
    return dir * ((a[k] || 0) - (b[k] || 0));
  });
}

// Sortable <th> node with a direction arrow on the active column.
function _th(label, key) {
  const th = h("th", { class: "sortable", dataset: { sort: key } }, label);
  if (SORT.key === key)
    append(th, h("span", { class: "arrow", text: SORT.dir < 0 ? "▾" : "▴" }));
  return th;
}

// A <td> holding numeric/plain text (optionally with a CSS class).
function _td(text, cls) {
  return h("td", cls ? { class: cls, text } : { text });
}

function renderTable(data) {
  LAST_DATA = data;
  const hasCache = data.has_cache;
  const t = data.totals;

  if (!data.models.length) {
    replaceChildren(
      document.getElementById("content"),
      h("div", {
        class: "empty",
        text: "Waiting for Bedrock invocations in this window…",
      }),
    );
    return;
  }

  const headCells = [
    _th("Model", "display_name"),
    _th("Calls", "calls"),
    _th("Input", "input_tokens"),
  ];
  if (hasCache)
    headCells.push(
      _th("Cache Write", "cache_write_tokens"),
      _th("Cache Read", "cache_read_tokens"),
    );
  headCells.push(_th("Output", "output_tokens"));
  if (!hasCache) headCells.push(_th("Total", "total_tokens"));
  headCells.push(
    _th("Est. Cost", "cost"),
    _th("Share", "cost_share"),
    h("th", { text: "Trend" }),
  );
  const head = h("tr", null, headCells);

  const bodyRows = [];
  for (const m of _sortModels(data.models)) {
    const cost = m.price_known
      ? _td(fmtCost(m.cost), "col-cost")
      : _td("N/A", "na");
    const share = m.cost_share || 0;
    const shareCell = h("td", null, [
      h("div", { class: "share" }, [
        h("span", { text: fmtPct(share) }),
        h("span", { class: "track" }, [
          h("span", {
            class: "fill",
            style: { width: Math.max(2, Math.round(share * 100)) + "%" },
          }),
        ]),
      ]),
    ]);
    // Model name cell (name is CloudWatch-sourced → text, not markup).
    const nameCell = h("td", { text: m.display_name });
    if (m.is_global)
      append(nameCell, h("span", { class: "badge", text: "global" }));

    const cells = [
      nameCell,
      _td(m.calls.toLocaleString()),
      _td(fmtTokens(m.input_tokens), "col-in"),
    ];
    if (hasCache)
      cells.push(
        _td(fmtTokens(m.cache_write_tokens), "col-cache"),
        _td(fmtTokens(m.cache_read_tokens), "col-cache"),
      );
    cells.push(_td(fmtTokens(m.output_tokens), "col-out"));
    if (!hasCache) cells.push(_td(fmtTokens(m.total_tokens)));
    cells.push(
      cost,
      shareCell,
      h("td", { style: { textAlign: "right" } }, sparklineSvg(m.spark)),
    );
    bodyRows.push(
      h(
        "tr",
        {
          class: "clickable",
          dataset: {
            dim: "model",
            val: m.model_id,
            label: m.display_name,
          },
        },
        cells,
      ),
    );
  }

  const totalCost = t.cost_known
    ? _td(fmtCost(t.cost), "col-cost")
    : _td("N/A", "na");
  const footCells = [
    _td("TOTAL"),
    _td(t.calls.toLocaleString()),
    _td(fmtTokens(t.input_tokens)),
  ];
  if (hasCache)
    footCells.push(
      _td(fmtTokens(t.cache_write_tokens)),
      _td(fmtTokens(t.cache_read_tokens)),
    );
  footCells.push(_td(fmtTokens(t.output_tokens)));
  if (!hasCache) footCells.push(_td(fmtTokens(t.total_tokens)));
  footCells.push(totalCost, h("td"), h("td"));
  const foot = h("tr", null, footCells);

  const table = h("table", null, [
    h("thead", null, head),
    h("tbody", null, bodyRows),
    h("tfoot", null, foot),
  ]);
  replaceChildren(document.getElementById("content"), table);
}

// Build a breakdown <table> as a DOM node. Each column's render(r) returns a
// node or a string (rendered as text) — never an HTML string — so nothing here
// can inject markup. Returns an empty-state <div> when there are no rows.
function renderBreakdownTable(rows, cols, rowMeta) {
  if (!rows.length) return h("div", { class: "empty", text: "No data yet…" });
  const headCells = cols.map((c) =>
    h("th", c.left ? { style: { textAlign: "left" }, text: c.label } : { text: c.label }),
  );
  const bodyRows = rows.map((r) => {
    const props = rowMeta
      ? (() => {
          const meta = rowMeta(r);
          return {
            class: "clickable",
            dataset: { dim: meta.dim, val: meta.val, label: meta.label },
          };
        })()
      : null;
    return h(
      "tr",
      props,
      cols.map((c) => h("td", null, c.render(r))),
    );
  });
  return h("table", null, [
    h("thead", null, h("tr", null, headCells)),
    h("tbody", null, bodyRows),
  ]);
}

function renderIdentities(list) {
  const cols = [
    {
      label: "Identity",
      left: true,
      // r.label is a CloudWatch-sourced identity ARN/name → rendered as text.
      render: (r) =>
        r.errors > 0
          ? [
              r.label + " ",
              h("span", {
                class: "badge",
                style: { borderColor: "var(--red)", color: "var(--red)" },
                text: r.errors + " err",
              }),
            ]
          : r.label,
    },
    { label: "Calls", render: (r) => r.calls.toLocaleString() },
    {
      label: "Cost",
      render: (r) => h("span", { class: "col-cost", text: fmtCost(r.cost) }),
    },
    { label: "Share", render: (r) => fmtPct(r.cost_share || 0) },
  ];
  replaceChildren(
    document.getElementById("bd-identity"),
    renderBreakdownTable(list || [], cols, (r) => ({
      dim: "identity",
      val: r.key,
      label: r.label,
    })),
  );
}

function renderRegions(list) {
  const cols = [
    { label: "Region", left: true, render: (r) => r.region },
    { label: "Calls", render: (r) => r.calls.toLocaleString() },
    {
      label: "Cost",
      render: (r) => h("span", { class: "col-cost", text: fmtCost(r.cost) }),
    },
    { label: "Share", render: (r) => fmtPct(r.cost_share || 0) },
  ];
  // Rows are clickable (→ switch region) only when there's a selector to switch with.
  const meta =
    (CONFIG.regions || []).length > 1
      ? (r) => ({ dim: "region", val: r.region, label: r.region })
      : null;
  replaceChildren(
    document.getElementById("bd-region"),
    renderBreakdownTable(list || [], cols, meta),
  );
}

function renderAnomaly(anom) {
  // Persistent (not auto-fading) and deduped by the bucket's timestamp — the
  // spike stays flagged even after that bucket is no longer the "latest" one
  // on the next poll, instead of disappearing before anyone notices it.
  if (!anom || !anom.cost) return;
  const key = "anomaly:" + anom.bucket_t;
  persistentToast(
    "📈 Cost spike — a time bucket cost " +
      fmtCost(anom.cost) +
      " vs ~" +
      fmtCost(anom.baseline || 0) +
      " baseline.",
    "bad",
    key,
  );
}

// ── Budget progress (daily / monthly) ────────────────────────────────────────
async function refreshBudgets() {
  try {
    const r = await fetch("/api/budgets");
    const data = await r.json();
    renderBudgets(data);
  } catch (e) {
    /* keep last view */
  }
}

function _renderBudgetRow(scope, info) {
  const row = document.getElementById("budget-" + scope);
  if (!info) {
    row.style.display = "none";
    return false;
  }
  row.style.display = "";
  const pct = Math.min(100, Math.round((info.fraction || 0) * 100));
  const fill = document.getElementById("budget-" + scope + "-fill");
  fill.style.width = pct + "%";
  fill.className =
    "budget-fill" +
    (info.fraction >= 1 ? " critical" : info.fraction >= 0.8 ? " warning" : "");
  document.getElementById("budget-" + scope + "-amt").textContent =
    fmtCost(info.cost) + " / " + fmtCost(info.budget) + " (" + pct + "%)";
  return true;
}

function renderBudgets(data) {
  const hasDaily = _renderBudgetRow("daily", data && data.daily);
  const hasMonthly = _renderBudgetRow("monthly", data && data.monthly);
  document.getElementById("panel-budgets").style.display =
    hasDaily || hasMonthly ? "" : "none";
}

function renderComparison(cmp) {
  const el = document.getElementById("c-delta");
  if (!el) return;
  if (!cmp || cmp.delta_pct === null || cmp.delta_pct === undefined) {
    el.textContent = cmp ? "— no prior data" : "";
    el.className = "delta muted";
    return;
  }
  const d = cmp.delta_pct;
  const up = d > 0;
  const arrow = up ? "▲" : d < 0 ? "▼" : "▬";
  // For spend, an increase is "bad" (red), a decrease is "good" (green).
  el.textContent =
    arrow + " " + Math.abs(d).toFixed(1) + "% " + (cmp.label || "");
  el.className = "delta " + (d > 0 ? "up" : d < 0 ? "down" : "flat");
  el.title =
    "Previous window: " +
    fmtCost(cmp.prev_cost || 0) +
    " · " +
    (cmp.prev_calls || 0).toLocaleString() +
    " calls";
}

function renderOperations(list) {
  const cols = [
    // r.operation is CloudWatch-sourced → rendered as text (string child).
    { label: "Operation", left: true, render: (r) => r.operation },
    { label: "Calls", render: (r) => r.calls.toLocaleString() },
    {
      label: "Cost",
      render: (r) => h("span", { class: "col-cost", text: fmtCost(r.cost) }),
    },
    { label: "Share", render: (r) => fmtPct(r.cost_share || 0) },
  ];
  replaceChildren(
    document.getElementById("bd-operation"),
    renderBreakdownTable(list || [], cols, (r) => ({
      dim: "operation",
      val: r.operation,
      label: r.operation,
    })),
  );
}

function renderErrors(errors) {
  const panel = document.getElementById("panel-errors");
  if (!errors || !errors.total) {
    panel.style.display = "none";
    return;
  }
  panel.style.display = "";
  const rateLine = h("div", { class: "err-rate-line" }, [
    h("b", { text: errors.total.toLocaleString() }),
    " failed call(s) · " + fmtPct(errors.rate) + " error rate",
  ]);
  const cols = [
    {
      label: "Error Code",
      left: true,
      // r.code is a CloudWatch-sourced error code → rendered as text.
      render: (r) => h("span", { class: "err-code", text: r.code }),
    },
    { label: "Count", render: (r) => r.count.toLocaleString() },
  ];
  replaceChildren(document.getElementById("bd-errors"), [
    rateLine,
    renderBreakdownTable(errors.by_code || [], cols),
  ]);
}

function setTab(name) {
  STATE.tab = name;
  document.getElementById("view-overview").style.display =
    name === "overview" ? "" : "none";
  document.getElementById("view-recent").style.display =
    name === "recent" ? "" : "none";
  document.querySelectorAll(".tab").forEach((b) => {
    const on = b.dataset.tab === name;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
    b.tabIndex = on ? 0 : -1;
  });
  positionTabUnderline();
  writeStateToUrl();
  if (name === "recent") refreshRecent();
  // The chart can't size itself while hidden; redraw now that Overview is shown.
  else if (LAST_TREND) renderChart(LAST_TREND);
}

async function refreshRecent() {
  // Skip the periodic rebuild while a row's detail is open — re-rendering the
  // table's innerHTML would collapse the expanded request/response panel.
  if (document.querySelector("#recent .rec-detail")) return;
  try {
    let url = "/api/recent?limit=20";
    if (STATE.filters.region)
      url += "&region=" + encodeURIComponent(STATE.filters.region.value);
    const r = await fetch(url);
    const data = await r.json();
    renderRecent(data.events || []);
  } catch (e) {
    /* keep last view */
  }
}

function renderRecent(events) {
  document.getElementById("recent-meta").textContent = events.length
    ? events.length + " most recent"
    : "";
  const el = document.getElementById("recent");
  if (!events.length) {
    replaceChildren(
      el,
      h("div", { class: "empty", text: "No invocations recorded yet…" }),
    );
    return;
  }
  const showRegion = (CONFIG.regions || []).length > 1;
  const left = { style: { textAlign: "left" } };
  const headCells = [
    h("th", { style: { width: "24px" } }),
    h("th", { style: { textAlign: "left" }, text: "Time" }),
    h("th", { style: { textAlign: "left" }, text: "Model" }),
    h("th", { style: { textAlign: "left" }, text: "Identity" }),
  ];
  if (showRegion)
    headCells.push(h("th", { style: { textAlign: "left" }, text: "Region" }));
  for (const label of ["Input", "Output", "Cost", "Status"])
    headCells.push(h("th", { text: label }));

  const bodyRows = events.map((e) => {
    const when = new Date(e.t).toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    const costCell = h("td", { class: "col-cost" });
    if (e.price_known) costCell.textContent = fmtCost(e.cost);
    else append(costCell, h("span", { class: "na", text: "N/A" }));
    // e.error is a CloudWatch-sourced error code → rendered as text.
    const status = e.error
      ? h("span", { class: "err-code", text: e.error })
      : h("span", { style: { color: "#3fb950" }, text: "✓" });
    const expandable = CONFIG.content_enabled !== false && !!e.event_id;

    const cells = [
      expandable ? h("td", { class: "caret", text: "▸" }) : h("td"),
      h("td", { ...left, text: when }),
      h("td", { ...left, text: e.model }),
      h("td", { ...left, text: e.identity }),
    ];
    if (showRegion) cells.push(h("td", { ...left, text: e.region }));
    cells.push(
      _td(fmtTokens(e.input_tokens)),
      _td(fmtTokens(e.output_tokens)),
      costCell,
      h("td", null, status),
    );
    const props = expandable
      ? {
          class: "rec-row",
          attrs: { tabindex: "0" },
          dataset: { eid: e.event_id, region: e.region, t: e.t },
        }
      : null;
    return h("tr", props, cells);
  });

  replaceChildren(
    el,
    h("table", null, [
      h("thead", null, h("tr", null, headCells)),
      h("tbody", null, bodyRows),
    ]),
  );
}

// Expand a Recent row to fetch (live, never stored) its request/response bodies.
async function toggleRecentDetail(row) {
  const existing = row.nextElementSibling;
  if (existing && existing.classList.contains("rec-detail")) {
    existing.remove();
    row.classList.remove("expanded");
    return;
  }
  // Close any other open detail row.
  document.querySelectorAll("#recent .rec-detail").forEach((d) => d.remove());
  document
    .querySelectorAll("#recent .rec-row.expanded")
    .forEach((r) => r.classList.remove("expanded"));

  row.classList.add("expanded");
  const cols = row.children.length;
  const box = h("div", { class: "rec-body", text: "Loading…" });
  const detail = h("tr", { class: "rec-detail" }, [
    h("td", { attrs: { colspan: String(cols) } }, box),
  ]);
  row.after(detail);

  const params = new URLSearchParams({
    id: row.dataset.eid,
    region: row.dataset.region || "",
    t: row.dataset.t || "0",
  });
  try {
    const r = await fetch("/api/event?" + params.toString());
    const d = await r.json();
    if (d.error) {
      // d.error is server/CloudWatch-sourced → rendered as text.
      replaceChildren(
        box,
        h("div", { class: "empty", text: "Could not load detail: " + d.error }),
      );
      return;
    }
    // Prompt/response bodies are live CloudWatch content → rendered as text
    // inside <pre> (never as markup).
    const block = (title, text, truncated, s3) => {
      const heading = h("h4", { text: title });
      if (text == null) {
        return [
          heading,
          h("div", {
            class: "muted",
            text: s3
              ? "Body offloaded to S3 (" + s3 + ") — not fetched."
              : "No body in log.",
          }),
        ];
      }
      if (truncated)
        append(heading, [
          " ",
          h("span", { class: "muted", text: "(truncated)" }),
        ]);
      return [heading, h("pre", { text: text })];
    };
    replaceChildren(box, [
      ...block("Input", d.input, d.input_truncated, d.input_s3),
      ...block("Output", d.output, d.output_truncated, d.output_s3),
      h("div", {
        class: "rec-note",
        text: "Fetched live from CloudWatch — prompt/response content is never stored by the dashboard.",
      }),
    ]);
  } catch (e) {
    replaceChildren(box, h("div", { class: "empty", text: "Request failed." }));
  }
}

async function refresh() {
  try {
    const r = await fetch(buildUsageUrl());
    const data = await r.json();
    if (data.error) {
      renderError(data.message || data.error);
      setStale(true);
      return;
    }

    // A warning means the background poller hit an error, but we still have
    // the last good cached data — show the notice and keep rendering it.
    const warn = document.getElementById("warn");
    if (data.warning) {
      warn.textContent =
        "⚠ " +
        data.warning.code +
        ": " +
        data.warning.message +
        " — showing last known data.";
      warn.classList.add("show");
    } else {
      warn.classList.remove("show");
    }

    if (data.totals.cost_known)
      animateMetric("c-cost", data.totals.cost, fmtCost);
    else setMetricText("c-cost", "N/A");
    animateMetric("c-calls", data.totals.calls, (v) =>
      Math.round(v).toLocaleString(),
    );
    animateMetric("c-tokens", data.totals.total_tokens, (v) =>
      fmtTokens(Math.round(v)),
    );

    // Avg cost per call
    if (data.totals.cost_known && data.totals.calls)
      animateMetric(
        "c-avgcost",
        data.totals.avg_cost_per_call,
        (v) => "$" + v.toFixed(5),
      );
    else setMetricText("c-avgcost", "—");

    // Burn rate = cost so far / hours elapsed since the window start.
    const nowMs = data.now_ms || Date.now();
    const startMs = (data.window && data.window.start_ms) || nowMs;
    const hours = Math.max((nowMs - startMs) / 3.6e6, 1 / 60);
    if (data.totals.cost_known && data.totals.cost > 0)
      animateMetric(
        "c-burn",
        data.totals.cost / hours,
        (v) => fmtCost(v) + "/hr",
      );
    else setMetricText("c-burn", "—");

    // Run-rate projection (server-computed).
    const proj = data.projection;
    if (proj && data.totals.cost_known && data.totals.cost > 0) {
      animateMetric("c-proj", proj.projected_daily, (v) => "$" + v.toFixed(2));
      document.getElementById("c-proj").title =
        "Run-rate estimate · ~$" + proj.projected_monthly.toFixed(0) + "/month";
    } else {
      setMetricText("c-proj", "—");
    }

    // Cache hit rate card (only when caching is in use).
    const cacheCard = document.getElementById("card-cache");
    if (data.has_cache) {
      cacheCard.style.display = "";
      animateMetric(
        "c-cache",
        data.totals.cache_hit_rate * 100,
        (v) => v.toFixed(1) + "%",
      );
    } else {
      cacheCard.style.display = "none";
    }

    // Cache savings card — estimated $ saved vs. full input price on cache reads.
    const savedCard = document.getElementById("card-saved");
    const saved = data.totals.cache_savings || 0;
    if (saved > 0) {
      savedCard.style.display = "";
      animateMetric("c-saved", saved, fmtCost);
    } else {
      savedCard.style.display = "none";
    }

    // Error rate card — turns red when any call has failed.
    const errors = data.errors || { total: 0, rate: 0, by_code: [] };
    animateMetric("c-err", errors.rate * 100, (v) => v.toFixed(1) + "%");
    document
      .getElementById("card-err")
      .classList.toggle("has-errors", errors.total > 0);

    renderBanner(data.totals);
    renderChart(data.trend);
    renderTable(data);
    renderIdentities(data.identities);
    renderRegions(data.regions);
    renderOperations(data.operations);
    renderErrors(errors);
    renderComparison(data.comparison);
    renderAnomaly(data.anomaly);
    refreshBudgets();
    if (STATE.tab === "recent") refreshRecent();

    const now = new Date();
    document.getElementById("updated").textContent =
      "updated " + now.toLocaleTimeString();
    // The named periods (today/yesterday/week) are already shown via the
    // active period button, so only surface this label for a custom
    // duration or a chart drill-in, where there's no button to show it.
    const metaWindow = document.getElementById("meta-window");
    if (
      data.window &&
      data.window.label &&
      !CONFIG.periods?.some((p) => p.id === data.window.period)
    ) {
      document.getElementById("label").textContent = data.window.label;
      metaWindow.style.display = "";
    } else {
      metaWindow.style.display = "none";
    }
    document.title =
      (data.totals.cost_known ? fmtCost(data.totals.cost) + " · " : "") +
      "Bedrock Insights";
    setStale(!!data.warning);
  } catch (e) {
    document.getElementById("updated").textContent =
      "connection lost — retrying";
    setStale(true);
  }
}

function settingsOpen() {
  return document.getElementById("settings").classList.contains("open");
}
function openSettings() {
  document.getElementById("settings").classList.add("open");
  document.getElementById("settings").setAttribute("aria-hidden", "false");
  const bd = document.getElementById("backdrop");
  bd.hidden = false;
  requestAnimationFrame(() => bd.classList.add("show"));
  document.getElementById("gear").setAttribute("aria-expanded", "true");
  loadSettings();
  document.getElementById("set-close").focus();
}
function closeSettings() {
  document.getElementById("settings").classList.remove("open");
  document.getElementById("settings").setAttribute("aria-hidden", "true");
  const bd = document.getElementById("backdrop");
  bd.classList.remove("show");
  setTimeout(() => {
    bd.hidden = true;
  }, 200);
  document.getElementById("gear").setAttribute("aria-expanded", "false");
  document.getElementById("gear").focus();
}
function cyclePeriod(delta) {
  const ids = (CONFIG.periods || []).map((p) => p.id);
  if (!ids.length) return;
  let idx = STATE.since ? -1 : ids.indexOf(STATE.period);
  if (idx < 0) idx = 0;
  setPeriod(ids[(idx + delta + ids.length) % ids.length]);
}

function currentTheme() {
  return document.documentElement.getAttribute("data-theme") === "light"
    ? "light"
    : "dark";
}
function setTheme(t) {
  if (t === "light")
    document.documentElement.setAttribute("data-theme", "light");
  else document.documentElement.removeAttribute("data-theme");
  try {
    localStorage.setItem("bi-theme", t);
  } catch (e) {
    /* ignore */
  }
  // Redraw the chart so its canvas colours match the new theme.
  if (LAST_TREND !== null) renderChart(LAST_TREND);
}
function toggleTheme() {
  setTheme(currentTheme() === "light" ? "dark" : "light");
}

(async function () {
  // The cookie is already set by the page response; drop the token from the
  // address bar so it doesn't linger in history or get shared accidentally.
  // Keep the hash — it carries shareable view state, not the token.
  if (location.search.includes("token=")) {
    history.replaceState({}, "", location.pathname + location.hash);
  }
  await loadConfig();
  await loadSettings();
  readStateFromUrl();
  applyStateToUi();

  const gear = document.getElementById("gear");
  gear.addEventListener("click", () =>
    settingsOpen() ? closeSettings() : openSettings(),
  );
  document.getElementById("set-close").addEventListener("click", closeSettings);
  document.getElementById("backdrop").addEventListener("click", closeSettings);
  document
    .getElementById("theme-toggle")
    .addEventListener("click", toggleTheme);

  document.getElementById("share").addEventListener("click", async () => {
    writeStateToUrl();
    try {
      await navigator.clipboard.writeText(location.href);
      toast("Link copied to clipboard", "ok");
    } catch (e) {
      toast("Couldn't copy link", "bad");
    }
  });

  // Animated tab underline + arrow-key navigation on the tablist.
  const tabsEl = document.getElementById("tabs");
  const underline = document.createElement("span");
  underline.id = "tab-underline";
  underline.className = "tab-underline";
  tabsEl.appendChild(underline);
  positionTabUnderline();
  window.addEventListener("resize", positionTabUnderline);
  tabsEl.addEventListener("click", (e) => {
    const b = e.target.closest(".tab");
    if (b && b.dataset.tab) setTab(b.dataset.tab);
  });
  tabsEl.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const tabs = [...document.querySelectorAll(".tab")];
    let i = tabs.findIndex((t) => t.getAttribute("aria-selected") === "true");
    i = (i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    setTab(tabs[i].dataset.tab);
    tabs[i].focus();
  });

  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, textarea, select")) return;
    if (e.key === "Escape" && settingsOpen()) {
      closeSettings();
      return;
    }
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    switch (e.key) {
      case "g":
        e.preventDefault();
        settingsOpen() ? closeSettings() : openSettings();
        break;
      case "r":
        e.preventDefault();
        refresh();
        break;
      case "1":
        setTab("overview");
        break;
      case "2":
        setTab("recent");
        break;
      case "[":
        cyclePeriod(-1);
        break;
      case "]":
        cyclePeriod(1);
        break;
    }
  });
  document.getElementById("set-save").addEventListener("click", saveSettings);
  document.getElementById("set-test").addEventListener("click", testWebhook);
  document
    .getElementById("export-json")
    .addEventListener("click", () => downloadExport("json"));
  document
    .getElementById("export-csv")
    .addEventListener("click", () => downloadExport("csv"));
  document
    .getElementById("set-window-apply")
    .addEventListener("click", () =>
      setCustomWindow(document.getElementById("set-since").value.trim()),
    );
  document.getElementById("set-window-clear").addEventListener("click", () => {
    document.getElementById("set-since").value = "";
    setPeriod(STATE.period || CONFIG.default_period);
  });
  document
    .getElementById("set-refresh-apply")
    .addEventListener("click", () =>
      applyRefreshInterval(document.getElementById("set-refresh").value),
    );

  await refresh();
  await refreshBudgets();
  applyRefreshInterval(CONFIG.refresh_seconds);
})();
