/* CANBench core: api, dom helper, state, live stream, shell */
"use strict";
const CB = { S: { state: null, live: null, view: "setup" }, views: {}, ctls: {}, trace: [], serialLog: [], tracePaused: false };
const TOKEN = document.querySelector('meta[name="token"]').content;

/* ---------- utils ---------- */
const $ = (s, r = document) => r.querySelector(s);
const pref = {
  get(k, d) { try { const v = localStorage.getItem("cb." + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("cb." + k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
};
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "value" || k === "checked" || k === "disabled" || k === "selected") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  const add = (c) => {
    if (c === null || c === undefined || c === false) return;
    if (Array.isArray(c)) c.forEach(add);
    else el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  };
  kids.forEach(add);
  return el;
}
const ICONS = {
  setup: '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
  simulate: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  scenarios: '<path d="M8 6h13M8 12h13M8 18h13"/><path d="M3 6l1 1 2-2M3 12l1 1 2-2M3 18l1 1 2-2"/>',
  results: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
  trace: '<path d="M4 5h16M4 10h16M4 15h10M4 20h6"/>',
  serial: '<rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M7 10l3 2-3 2M12 15h4"/>',
  upload: '<path d="M12 16V4M7 9l5-5 5 5"/><path d="M4 16v3a1 1 0 001 1h14a1 1 0 001-1v-3"/>',
  chev: '<path d="M6 9l6 6 6-6"/>', x: '<path d="M6 6l12 12M18 6L6 18"/>', check: '<path d="M5 12l5 5 9-10"/>',
  play: '<path d="M7 4l13 8-13 8z"/>', stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>', sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  power: '<path d="M12 3v9M6.4 6.6a8 8 0 1011.2 0"/>', plus: '<path d="M12 5v14M5 12h14"/>', trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/>', bolt: '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>', refresh: '<path d="M20 11a8 8 0 10-2 6M20 4v7h-7"/>',
  firmware: '<rect x="7" y="7" width="10" height="10" rx="2"/><path d="M9 3v2M15 3v2M9 19v2M15 19v2M3 9h2M3 15h2M19 9h2M19 15h2"/>',
  send: '<path d="M4 12l16-8-6 16-3-7z"/><path d="M11 13l9-9"/>', info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8v.01"/>',
  file: '<path d="M6 3h8l5 5v13H6z"/><path d="M14 3v5h5"/>', save: '<path d="M5 4h12l3 3v13H5z"/><path d="M8 4v6h8V4M8 20v-6h8v6"/>',
};
function icon(n, cls) { const s = document.createElementNS("http://www.w3.org/2000/svg", "svg"); s.setAttribute("viewBox", "0 0 24 24"); if (cls) s.setAttribute("class", cls); s.innerHTML = ICONS[n] || ""; return s; }
function fmtNum(v) { if (v === null || v === undefined) return "–"; if (typeof v !== "number") return String(v); if (Number.isInteger(v)) return String(v); return String(+v.toFixed(4)); }
function fmtTime(t) { const d = new Date(t * 1000); return d.toLocaleTimeString([], { hour12: false }) + "." + String(d.getMilliseconds()).padStart(3, "0"); }
function fmtDT(t) { const d = new Date(t * 1000); return d.toLocaleDateString() + " " + d.toLocaleTimeString([], { hour12: false }); }
function esc(s) { return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function throttle(fn, ms) {
  let last = 0, timer = null, args = null;
  return (...a) => {
    args = a; const now = Date.now();
    if (now - last >= ms) { last = now; fn(...args); } else if (!timer) {
      timer = setTimeout(() => { timer = null; last = Date.now(); fn(...args); }, ms - (now - last));
    }
  };
}
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

/* ---------- api ---------- */
async function api(path, body) {
  const opt = { headers: { "X-Token": TOKEN } };
  if (body !== undefined) { opt.method = "POST"; opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  let r;
  try { r = await fetch(path, opt); } catch (e) { toast("Lost connection to the bench process.", "error"); throw e; }
  let data = {};
  try { data = await r.json(); } catch (e) { /* non-json */ }
  if (!r.ok) { const msg = data.error || r.statusText; toast(msg, "error"); throw new Error(msg); }
  return data;
}
const tokenUrl = (p) => p + (p.includes("?") ? "&" : "?") + "t=" + encodeURIComponent(TOKEN);

/* ---------- toast / modal ---------- */
function toast(text, level = "info", ms) {
  ms = ms || (level === "error" ? 12000 : 4200);   // errors stay long enough to be read
  const t = h("div", { class: "toast " + level, title: "Click to dismiss", style: { cursor: "pointer" }, onclick: () => t.remove() }, text);
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), ms);
}
function modal({ title, body, buttons = [], width }) {
  const root = $("#modal-root");
  const close = () => { root.innerHTML = ""; document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  const m = h("div", { class: "modal", style: width ? { width } : null },
    h("div", { class: "mh" }, title, h("span", { class: "grow" }), h("button", { class: "btn ghost icon", onclick: close }, icon("x"))),
    h("div", { class: "mb" }, body),
    buttons.length ? h("div", { class: "mf" }, buttons.map((b) =>
      h("button", { class: "btn " + (b.kind || ""), onclick: async () => { const keep = b.onClick ? await b.onClick() : null; if (keep !== false) close(); } }, b.label))) : null);
  root.innerHTML = "";
  root.appendChild(h("div", { class: "modal-bg", onmousedown: (e) => { if (e.target.classList.contains("modal-bg")) close(); } }, m));
  return { close, el: m };
}
const modalOpen = () => !!$("#modal-root").firstChild;

/* Our own confirmation dialog (replaces the browser's "127.0.0.1:8765 says" box). Resolves true / false.
   ask("text") or ask({ title, body, banner, danger, yes, no }) — body/banner may be text or DOM nodes. */
function ask(o) {
  if (typeof o === "string") o = { body: o };
  return new Promise((resolve) => {
    let done = false; const fin = (v) => { if (!done) { done = true; resolve(v); } };
    const m = modal({ title: o.title || "Please confirm", width: o.width || "500px",
      body: [o.banner ? h("div", { class: "banner" + (o.danger ? " bad" : "") }, h("div", { class: "grow" }, h("b", null, o.banner))) : null, o.body],
      buttons: [{ label: o.no || "Cancel", onClick: () => fin(false) }, { label: o.yes || "OK", kind: o.danger ? "bad" : "primary", onClick: () => fin(true) }] });
    const obs = new MutationObserver(() => { if (!document.body.contains(m.el)) { fin(false); obs.disconnect(); } });   // X, Esc, click outside = No
    obs.observe($("#modal-root"), { childList: true });
  });
}

/* ---------- state ---------- */
function indexState() {
  const s = CB.S.state, ix = { items: {}, msgs: {}, req: {}, reqById: {}, scen: {}, lastByScen: {}, lastByRef: {} };
  CB.idx = ix;
  if (!s || !s.model) return;
  for (const m of s.model.messages) {
    ix.msgs[m.name] = m;
    for (const it of m.items) ix.items[it.uid] = it;
  }
  for (const r of s.analysis.requirements) {
    ix.reqById[r.id] = r;
    for (const u of r.signals) (ix.req[u] = ix.req[u] || []).push(r);
  }
  for (const sc of s.scenarios) ix.scen[sc.id] = sc;
  for (const r of s.results) {
    if (r.scenario) ix.lastByScen[r.scenario] = r;
    if (r.ref) ix.lastByRef[r.ref] = r;
  }
}
async function refresh() {
  CB.S.state = await api("/api/state");
  indexState();
  if (CB.S.state.uds) { CB.udsLog = CB.S.state.uds.log.slice(); CB.udsRun = CB.S.state.uds.run; FW.settings = null; }
  renderAll();
}
function st() { return CB.S.state; }
function hasProject() { return !!(CB.S.state && CB.S.state.project); }
function live() { return CB.S.live || { conn: {}, stats: {}, tx: {}, rx: {}, run: {}, counts: {} }; }

/* ---------- shell ---------- */
const NAV = [["setup", "Setup"], ["simulate", "Simulate"], ["send", "Send"], ["scenarios", "Scenarios"], ["firmware", "Firmware"], ["trace", "Trace"], ["serial", "Serial"], ["results", "Results"], ["info", "Info"]];
const BITRATES = [10000, 20000, 50000, 83333, 100000, 125000, 250000, 500000, 800000, 1000000];

function renderTop() {
  const s = st(), top = $("#top");
  top.innerHTML = "";
  top.appendChild(h("div", { class: "brand", title: "CANBench - open the Info page", style: { cursor: "pointer" }, onclick: () => go("info") },
    h("div", null, h("h1", null, "CANBench"), h("small", null, "DBC-driven test bench"))));
  const ps = h("select", { class: "proj-sel", title: "Project", onchange: async (e) => {
    const v = e.target.value;
    if (v === "__new") { e.target.value = s.project ? s.project.id : ""; newProjectDialog(); return; }
    await api("/api/project/open", { id: v }); CB.S.live = null; await refresh();
  } },
    s.projects.map((p) => h("option", { value: p.id, selected: s.project && s.project.id === p.id }, p.name)),
    h("option", { value: "__new" }, "＋ New project…"));
  if (!s.projects.length) ps.innerHTML = '<option value="__new">No project yet</option>';
  top.appendChild(ps);
  top.appendChild(h("div", { class: "top-sp" }));

  if (s.project) {
    const b = s.project.bus;
    const ifSel = h("select", { id: "bus-if", title: "CAN interface (python-can)" }, s.interfaces.map((i) => h("option", { value: i, selected: i === b.interface }, i)));
    const chIn = h("input", { type: "text", id: "bus-ch", value: b.channel, list: "ch-list", placeholder: "channel", title: "Channel, e.g. PCAN_USBBUS1" });
    const dl = h("datalist", { id: "ch-list" });
    const detect = async () => {
      const r = await api("/api/channels?interface=" + ifSel.value);
      dl.innerHTML = ""; r.channels.forEach((c) => dl.appendChild(h("option", { value: c })));
      if (ifSel.value === "virtual") chIn.value = "canbench";
      else if (r.channels.length && !r.channels.includes(chIn.value)) chIn.value = r.channels[0];
      toast(r.channels.length ? `Found: ${r.channels.join(", ")}` : "No channel detected — check adapter and driver.", r.channels.length ? "ok" : "warn");
    };
    ifSel.onchange = () => { if (ifSel.value === "virtual") chIn.value = "canbench"; };
    const brSel = h("select", { id: "bus-br", title: "Bitrate" }, BITRATES.map((x) => h("option", { value: x, selected: x === b.bitrate }, x >= 1000 ? x / 1000 + " kbit/s" : x + " bit/s")));
    const conn = h("button", { class: "btn primary", id: "btn-conn", onclick: async () => {
      if (live().conn.connected) { await api("/api/disconnect", {}); return; }
      await api("/api/connect", { interface: ifSel.value, channel: chIn.value, bitrate: +brSel.value });
    } }, "Connect");
    top.appendChild(h("div", { class: "bus" }, h("label", null, "Bus"), ifSel, h("span", { class: "sep" }), chIn, dl,
      h("button", { class: "btn ghost icon sm", title: "Detect channels", onclick: detect }, icon("refresh")), h("span", { class: "sep" }), brSel, conn));
    top.appendChild(h("button", { class: "btn", id: "btn-tx", onclick: async () => { await api("/api/tx", { on: !live().conn.tx_on }); } }, "Start TX"));
    top.appendChild(h("button", { class: "btn estop", title: "Stop all transmission and abort any run", onclick: () => api("/api/estop", {}) }, "E-STOP"));
  }
  top.appendChild(h("button", { class: "btn ghost icon", title: "Toggle theme", onclick: toggleTheme }, icon("sun")));
  top.appendChild(h("button", { class: "btn ghost icon", title: "Quit CANBench (stops all CAN transmission)", onclick: async () => {
    if (await ask({ title: "Quit CANBench?", body: "All CAN transmission will stop and the program closes.", yes: "Quit", danger: true })) { try { await api("/api/quit", {}); } catch (e) { /* closing */ } document.body.innerHTML = '<div class="empty"><h3>CANBench has stopped</h3>You can close this window.</div>'; }
  } }, icon("power")));
  top.appendChild(h("img", { class: "corner-logo", src: "/static/connectm_logo.png", alt: "ConnectM", title: "ConnectM - open the Info page", onclick: () => go("info") }));
  updateTop();
}
function updateTop() {
  const L = live(), c = L.conn || {};
  const conn = $("#btn-conn"), tx = $("#btn-tx");
  if (conn) { conn.textContent = c.connected ? "Disconnect" : "Connect"; conn.className = "btn " + (c.connected ? "" : "primary"); }
  if (tx) {
    tx.disabled = !c.connected;
    tx.className = "btn " + (c.tx_on ? "tx-on" : "");
    tx.textContent = c.tx_on ? "● Transmitting" : "Start TX";
  }
  for (const id of ["bus-if", "bus-ch", "bus-br"]) { const e = $("#" + id); if (e) e.disabled = !!c.connected; }
}
function renderRail() {
  const rail = $("#rail"); rail.innerHTML = "";
  for (const [id, label] of NAV) {
    const b = h("button", { class: "rail-btn" + (CB.S.view === id ? " on" : ""), onclick: () => go(id), "data-v": id }, icon(id), h("span", null, label));
    if (id === "results") {
      const c = live().counts || {};
      if (c.FAIL) b.appendChild(h("i", { class: "badge", style: { background: "var(--bad)", color: "#fff" } }, c.FAIL));
    }
    rail.appendChild(b);
  }
}
function go(v) { CB.S.view = v; pref.set("view", v); renderRail(); renderView(); }
function renderView() {
  const main = $("#main");
  main.innerHTML = "";
  main.scrollTop = 0;
  CB.ctls = {};
  const v = CB.views[CB.S.view];
  if (!hasProject() && CB.S.view !== "setup") { CB.S.view = "setup"; renderRail(); }
  (CB.views[CB.S.view] || v).render(main);
}
function renderAll() { renderTop(); renderRail(); renderView(); renderStatus(); renderDock(); }
function toggleTheme() {
  const r = document.documentElement, cur = r.dataset.theme || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  r.dataset.theme = cur === "dark" ? "light" : "dark"; pref.set("theme", r.dataset.theme);
}

/* status bar */
let _prev = { t: 0, tx: 0, rx: 0 }, _rates = { tx: 0, rx: 0 };
function renderStatus() {
  $("#status").innerHTML = '<span id="s-rec" class="rec"></span><span>TX <b id="s-tx">0</b> fr/s</span><span>RX <b id="s-rx">0</b> fr/s</span><span>Bus load <b id="s-load">0</b>%</span>' +
    '<span>Errors <b id="s-err">0</b></span><span>State <b id="s-state">—</b></span><span class="err" id="s-msg"></span><span class="ver" id="s-ver"></span>';
}
function updateStatus() {
  const L = live(), s = L.stats || {};
  if (_prev.t) { const dt = L.t - _prev.t; if (dt > 0.2) { _rates = { tx: (s.tx_total - _prev.tx) / dt, rx: (s.rx_total - _prev.rx) / dt }; _prev = { t: L.t, tx: s.tx_total, rx: s.rx_total }; } } else _prev = { t: L.t, tx: s.tx_total, rx: s.rx_total };
  const set = (id, v) => { const e = $("#" + id); if (e) e.textContent = v; };
  set("s-tx", Math.round(_rates.tx)); set("s-rx", Math.round(_rates.rx)); set("s-load", (s.load || 0).toFixed(0)); set("s-err", s.errors || 0);
  const c = L.conn || {};
  set("s-state", c.connected ? (c.tx_on ? "transmitting" : "connected · TX stopped") + (c.bus_state ? " · " + c.bus_state : "") : "disconnected");
  set("s-msg", L.error || "");
  set("s-ver", st() && st().version ? "v" + st().version : "");
  const rec = $("#s-rec"), b = L.buslog || {};
  if (rec && !rec.onclick) rec.onclick = () => go("info");
  if (rec) {
    rec.textContent = b.active ? "● REC " + b.name.replace(/^canlog_/, "") : "";
    rec.title = b.active ? `Recording every CAN frame to ${b.name} (${b.frames} frames${b.dropped ? ", " + b.dropped + " DROPPED" : ""}). Trace page → Bus logs.` : "";
    rec.classList.toggle("warn", !!b.dropped);
  }
}


/* ---------- run dock ---------- */
let _dockKey = "";
function renderDock() {
  const d = $("#dock"), r = (CB.S.state && CB.S.state.run) || {}, run = CB.S.run || r;
  const show = run.running && run.current;
  d.classList.toggle("show", !!show);
  if (!show) { _dockKey = ""; return; }
  const key = run.current.id + ":" + run.index + ":" + !!run.awaiting;
  if (key !== _dockKey) {
    _dockKey = key;
    d.innerHTML = "";
    const note = h("input", { type: "text", id: "dock-note", placeholder: "Note (optional)", style: { width: "220px" } });
    const vb = (label, v, kind) => h("button", { class: "btn lg " + kind, onclick: () => api("/api/run/verdict", { verdict: v, note: note.value }) }, label);
    d.appendChild(h("div", { class: "dock" + (run.awaiting ? " await" : "") },
      h("div", null,
        h("div", { class: "row" }, h("span", { class: "chip accent", id: "dock-idx" }, `${run.index}/${run.total}`), h("span", { class: "t" }, run.current.title),
          h("span", { class: "muted", id: "dock-phase" })),
        h("div", { class: "e" }, run.current.expect || ""),
        h("div", { class: "bar" }, h("i", { id: "dock-bar", style: { width: ((run.index - 1) / run.total) * 100 + "%" } }))),
      h("div", { class: "row" },
        run.awaiting ? [note, vb("Pass  [P]", "PASS", "ok"), vb("Fail  [F]", "FAIL", "bad"), vb("Skip  [S]", "SKIP", "")] : null,
        h("button", { class: "btn", onclick: () => api("/api/run/abort", {}) }, "Abort"))));
  }
  const ph = $("#dock-phase");
  if (ph) ph.textContent = run.awaiting ? "Observe the DUT, then decide" : ({ applying: "applying stimulus…", dwell: "settling " + Math.ceil((run.remaining_ms || 0) / 100) / 10 + " s", starting: "starting…" }[run.phase] || "");
  const bar = $("#dock-bar");
  if (bar) bar.style.width = Math.min(100, ((run.done || 0) + (run.awaiting ? 0.6 : 0.25)) / run.total * 100) + "%";
}
document.addEventListener("keydown", (e) => {
  const run = CB.S.run;
  if (!run || !run.awaiting || modalOpen()) return;
  if (/^(INPUT|TEXTAREA|SELECT)$/.test((e.target.tagName || ""))) return;
  const k = e.key.toLowerCase(), v = { p: "PASS", f: "FAIL", s: "SKIP" }[k];
  if (v) { e.preventDefault(); api("/api/run/verdict", { verdict: v, note: ($("#dock-note") || {}).value || "" }); }
});

/* ---------- new project ---------- */
function newProjectDialog() {
  const inp = h("input", { type: "text", placeholder: "e.g. Cluster bring-up — Varcas 3W", style: { width: "100%" } });
  const go_ = async () => { if (!inp.value.trim()) { toast("Give the project a name.", "warn"); return false; } await api("/api/project/new", { name: inp.value }); CB.S.live = null; await refresh(); go("setup"); };
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { go_().then(() => $("#modal-root").innerHTML = ""); } });
  modal({ title: "New project", body: [h("div", { class: "field" }, h("label", null, "Project name"), inp),
    h("div", { class: "muted" }, "A project keeps your DBC, documents, roles, baseline, scenarios and every result together.")],
    buttons: [{ label: "Create", kind: "primary", onClick: go_ }] });
  setTimeout(() => inp.focus(), 50);
}

/* ---------- live stream ---------- */
function connectStream() {
  const es = new EventSource(tokenUrl("/events"));
  const on = (n, f) => es.addEventListener(n, (e) => { try { f(JSON.parse(e.data)); } catch (err) { console.error(n, err); } });
  on("live", (d) => {
    CB.S.live = d; updateTop(); updateStatus();
    const v = CB.views[CB.S.view]; if (v && v.onLive) v.onLive(d);
    if (!CB.S.run || CB.S.run.running !== d.run.running) { CB.S.run = d.run; renderDock(); }
    const f = (d.counts || {}).FAIL || 0; if (f !== CB._fail) { CB._fail = f; renderRail(); }
    if (CB.unk && document.body.contains(CB.unk.el)) CB.unk.update(d.unknown || []);
  });
  on("uds", (e) => {
    if (e.type === "log" || e.type === "frame") { CB.udsLog.push(e); if (CB.udsLog.length > 700) CB.udsLog.splice(0, 100); }
    else if (e.type === "step") CB.udsRun.steps[e.index] = { state: e.state, detail: e.detail };
    else if (e.type === "progress") { CB.udsRun.pct = e.pct; CB.udsRun.text = e.text; }
    else if (e.type === "wait") CB.udsRun.text = e.text;
    else if (e.type === "done") { CB.udsRun.running = false; CB.udsRun.ok = e.ok; CB.udsRun.summary = e.summary; }
    const v = CB.views[CB.S.view]; if (v && v.onUds) v.onUds(e);
  });
  on("trace", (d) => {
    for (const r of d.rows) CB.trace.push(r);
    if (CB.trace.length > 4000) CB.trace.splice(0, CB.trace.length - 3000);
    const v = CB.views[CB.S.view]; if (v && v.onTrace) v.onTrace(d.rows, d.dropped);
  });
  on("serial", (rows) => {
    for (const r of rows) CB.serialLog.push(r);
    if (CB.serialLog.length > 6000) CB.serialLog.splice(0, CB.serialLog.length - 5000);
    const v = CB.views[CB.S.view]; if (v && v.onSerial) v.onSerial(rows);
  });
  on("run", (d) => { CB.S.run = d; renderDock(); const v = CB.views[CB.S.view]; if (v && v.onRun) v.onRun(d); });
  on("toast", (d) => toast(d.text, d.level));
  on("recovery", (d) => { if (CB.S.state && CB.S.state.uds) { CB.S.state.uds.recovery = d; if (CB.S.view === "firmware") renderView(); } });
  on("values", (d) => { if (CB.S.state) { CB.S.state.values = d; const v = CB.views[CB.S.view]; if (v && v.onValues) v.onValues(d); } });
  on("results", (d) => {
    if (!CB.S.state) return;
    CB.S.state.results.push(d.latest); indexState();
    const v = CB.views[CB.S.view]; if (v && v.onResult) v.onResult(d.latest);
  });
  let resyncAt = 0;
  on("resync", () => { const now = Date.now(); if (now - resyncAt > 1500) { resyncAt = now; refresh(); } });   // events were dropped
  es.onopen = () => { if (CB._wasDown) { CB._wasDown = false; refresh(); } };
  es.onerror = () => { CB._wasDown = true; };
}

/* ---------- boot ---------- */
async function boot() {
  const th = pref.get("theme", null); if (th) document.documentElement.dataset.theme = th;
  CB.S.view = pref.get("view", "setup");
  const hv = location.hash.slice(1);
  if (CB.views[hv]) CB.S.view = hv;
  if (!CB.views[CB.S.view]) CB.S.view = "setup";
  try { await refresh(); } catch (e) {
    document.body.innerHTML = '<div class="empty"><h3>CANBench hit a problem while starting</h3><div class="mono">' + esc(e && e.message ? e.message : e) +
      '</div><p>Press Ctrl+F5. If it persists, restart run_canbench.bat and send me this message.</p></div>'; return;
  }
  if (!hasProject()) go("setup");
  connectStream();
}
window.addEventListener("load", boot);
window.addEventListener("hashchange", () => { const v = location.hash.slice(1); if (CB.views[v] && v !== CB.S.view && hasProject()) go(v); });
