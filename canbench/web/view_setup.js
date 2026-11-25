/* Setup view: inputs, what was understood, message roles, health, spec mismatches */
"use strict";
CB.views.setup = {
  render(el) {
    const s = st();
    if (!s.project) return heroView(el);
    const p = s.project, m = s.model, a = s.analysis;
    el.appendChild(h("div", { class: "page-h" }, h("h2", null, "Setup"), h("span", { class: "sub" }, "Give the bench a DBC (and optionally the spec documents). Everything else is built from them.")));
    const stack = h("div", { class: "stack" });
    el.appendChild(stack);

    /* 1. inputs */
    const zone = (title, hint, accept) => {
      const inp = h("input", { type: "file", multiple: true, accept, class: "hidden", onchange: () => up(inp.files) });
      const z = h("div", { class: "drop", onclick: () => inp.click() }, icon("upload"), h("div", { style: { fontWeight: 650, color: "var(--text)" } }, title), h("div", { class: "faint" }, hint), inp);
      z.ondragover = (e) => { e.preventDefault(); z.classList.add("over"); };
      z.ondragleave = () => z.classList.remove("over");
      z.ondrop = (e) => { e.preventDefault(); z.classList.remove("over"); up(e.dataTransfer.files); };
      return z;
    };
    const tableRow = (f) => {
      const t = (s.tables || {})[f.name];
      if (!t) {
        if (!/[.](xlsx|csv|tsv)$/i.test(f.name) || f.source) return null;
        return h("div", { class: "tbl-hint" }, h("div", null, h("b", null, "Is this a CAN ID / signal table?"), " The reader did not recognise one automatically. You can point at the columns yourself."),
          h("div", { class: "row", style: { marginTop: "6px" } }, h("button", { class: "btn primary sm", onclick: () => mapColumnsDialog(f.name, { id_base: "auto", id_format: "auto" }) }, "Map columns…")));
      }
      const base = h("select", { title: "How to read bare numbers in the ID column" },
        h("option", { value: "auto" }, "IDs: auto (hex)"), h("option", { value: "hex" }, "IDs are hex"), h("option", { value: "dec" }, "IDs are decimal"));
      base.value = "auto";
      const fmt = h("select", { title: "IDs written with 8 hex digits (0x000000A2) are read as 29-bit by default" },
        h("option", { value: "auto" }, "ID format: auto"), h("option", { value: "std" }, "ID format: 11-bit"), h("option", { value: "ext" }, "ID format: all 29-bit"));
      fmt.value = "auto";
      const opts = () => ({ name: f.name, id_base: base.value, id_format: fmt.value });
      const doImport = async () => {
        try {
          const r = await api("/api/table/import", opts());
          toast(`Built a database from ${f.name}: ${r.frames} frames, ${r.signals} signals.` + (r.bitrate ? ` Bitrate set to ${r.bitrate / 1000} kbit/s.` : "") + (r.warnings.length ? ` ${r.warnings.length} note(s) — see Preview.` : ""), r.warnings.length ? "warn" : "ok", 9000);
          await refresh();
        } catch (e) { /* api() already showed the error */ }
      };
      const go = h("button", { class: "btn primary sm", onclick: doImport }, "Use as CAN database");
      const prev = h("button", { class: "btn sm", onclick: async () => {
        try { const r = await api("/api/table/preview", opts()); showTablePreview(f.name, r, doImport); } catch (e) { /* shown */ }
      } }, "Preview");
      const map = h("button", { class: "btn sm", title: "Pick the header row and the columns by hand when the automatic reading is wrong", onclick: () => mapColumnsDialog(f.name, opts()) }, "Map columns…");
      return h("div", { class: "tbl-hint" },
        h("div", null, h("b", null, "This looks like a CAN ID table"), ` — ${t.frames} frame${t.frames === 1 ? "" : "s"}, ${t.signals} signal${t.signals === 1 ? "" : "s"}` + (t.layout === "frames" ? " (no signal layout: raw bytes)" : "") + ". No DBC needed."),
        t.warnings.map((w) => h("div", { class: "faint", style: { fontSize: "12px" } }, "• " + w)),
        t.n_warnings > t.warnings.length ? h("div", { class: "faint", style: { fontSize: "12px" } }, `…and ${t.n_warnings - t.warnings.length} more notes — open Preview.`) : null,
        h("div", { class: "row", style: { marginTop: "6px", flexWrap: "wrap" } }, base, fmt, prev, map, go));
    };
    const fileList = (kind) => {
      const fs = p.files.filter((f) => f.kind === kind);
      return h("div", { class: "stack", style: { gap: "8px", marginTop: "10px" } }, fs.map((f) => h("div", null, h("div", { class: "file" },
        icon("file"), h("div", { class: "grow" }, h("div", { class: "nm" }, f.name), h("div", { class: "hash" }, (f.source ? "built from " + f.source + "  ·  " : "") + "sha256 " + f.sha256.slice(0, 16) + "…  ·  " + Math.round(f.size / 1024) + " KB")),
        h("button", { class: "btn ghost icon sm", title: "Remove", onclick: async () => { await api("/api/file/remove", { name: f.name }); await refresh(); } }, icon("trash"))),
        kind === "doc" ? tableRow(f) : null)));
    };
    stack.appendChild(h("div", { class: "grid2" },
      h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", null, "1 · CAN database"), h("span", { class: "sub" }, "required — a .dbc, or an Excel/CSV table of CAN IDs")),
        h("div", { class: "card-b" }, zone("Drop a DBC here", ".dbc  .kcd  .sym  .arxml — or drop an Excel/CSV CAN-ID table below", ".dbc,.kcd,.sym,.arxml"), fileList("dbc"))),
      h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", null, "2 · Supporting documents"), h("span", { class: "sub" }, "SOR, test plan, alert codes — or your CAN-ID table")),
        h("div", { class: "card-b" }, zone("Drop documents or a CAN-ID table here", ".docx  .xlsx  .csv  .md  .txt  .pdf",".docx,.xlsx,.csv,.tsv,.md,.txt,.pdf"), fileList("doc"),
          s.docs.filter((d) => d.error).map((d) => h("div", { class: "chip error", style: { marginTop: "8px" } }, d.name + ": " + d.error))))));

    if (!m || !m.messages.length) {
      stack.appendChild(h("div", { class: "card" }, h("div", { class: "empty" }, h("h3", null, "Add a DBC (or a CAN-ID table) to begin"), "The Simulate, Scenarios and Results screens are generated from it. No DBC? Drop an Excel/CSV with CAN ID, message and signal columns into the documents box and press “Use as CAN database”.")));
      return;
    }

    /* 2. understood */
    const sum = m.summary, nsc = s.scenarios.length;
    const nmaps = a.value_maps.reduce((n, v) => n + v.total, 0);
    const tile = (n, l, cls) => h("div", { class: "tile " + (cls || "") }, h("b", null, n), h("span", null, l));
    stack.appendChild(h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", null, "What the bench understood")),
      h("div", { class: "card-b" }, h("div", { class: "tiles" },
        tile(sum.messages, "messages"), tile(sum.signals, "signals"), tile(sum.enums + sum.bools, "enums & flags"),
        tile(sum.composites, "16-bit pairs merged"), tile(a.requirements.filter((r) => !r.na).length, "spec requirements"),
        tile(nmaps, "alert-code entries"), tile(nsc, "scenarios ready")))));

    /* 3. roles */
    stack.appendChild(rolesCard(s));
    CB.unk = buildUnknownCard(); stack.appendChild(CB.unk.el); CB.unk.update(live().unknown || []);

    /* 4. documents analysis */
    if (s.docs.length) stack.appendChild(docsCard(s));

    /* 5. findings */
    const fs = m.findings;
    stack.appendChild(h("div", { class: "card" },
      h("div", { class: "card-h" }, h("h3", null, "DBC health"), h("span", { class: "sub" }, fs.length ? `${fs.filter((f) => f.level !== "info").length} to review · ${fs.filter((f) => f.level === "info").length} notes` : "no findings"),
        h("span", { class: "grow" }),
        h("label", { class: "row muted", title: "Show ..._Hi_Byte / ..._Lw_Byte signal pairs as a single 16-bit value" },
          h("input", { type: "checkbox", checked: p.composites, onchange: async (e) => { await api("/api/composites", { on: e.target.checked }); await refresh(); } }), "Merge Hi/Lo byte pairs")),
      h("div", { class: "card-b" }, fs.length ? fs.map((f) => h("div", { class: "find" },
        h("span", { class: "chip " + f.level }, f.level), h("div", { class: "mono muted" }, f.where),
        h("div", null, f.text, f.fix ? h("div", { class: "fix" }, "Fix: " + f.fix) : null))) : h("div", { class: "muted" }, "The database looks clean."))));

    /* 6. mismatches */
    if (a.mismatches.length) stack.appendChild(h("div", { class: "card" },
      h("div", { class: "card-h" }, h("h3", null, "Spec ↔ DBC mismatches"), h("span", { class: "sub" }, "names in your documents that are not in the DBC")),
      h("div", { class: "card-b" }, a.mismatches.map((x) => h("div", { class: "find" }, h("span", { class: "chip warn" }, "unknown"), h("div", { class: "mono" }, x.token),
        h("div", null, x.guesses.length ? ["Closest DBC signals: ", x.guesses.map((g) => h("span", { class: "chip info", style: { marginRight: "4px" } }, g))] : "No close match — likely a discrete IO signal or a name that needs a DBC entry.",
          h("div", { class: "faint", style: { fontSize: "12px" } }, x.doc)))))));

    /* 7. meta */
    stack.appendChild(metaCard(p));
  },
};

function showTablePreview(fname, r, doImport) {
  modal({ title: "Preview — " + fname, body: tablePreviewBody(r), width: "1100px", buttons: [{ label: "Close" }, { label: "Use as CAN database", kind: "primary", onClick: async () => { await doImport(); } }] });
}

function tablePreviewBody(r) {
  const hex = (n, ext) => "0x" + n.toString(16).toUpperCase().padStart(ext ? 8 : 3, "0");
  const rows = [];
  for (const f of r.frames) {
    rows.push(h("tr", { class: "grp" }, h("td", { colspan: 9 }, h("b", { class: "mono" }, hex(f.id, f.ext)), "  ", h("b", null, f.name),
      h("span", { class: "faint" }, `   ${f.ext ? "29-bit" : "11-bit"} · DLC ${f.dlc}` + (f.period_ms ? ` · ${f.period_ms} ms` : "") + (f.sender ? ` · sent by ${f.sender}` : "")))));
    for (const s of f.signals) {
      const ch = Object.entries(s.choices || {});
      rows.push(h("tr", null, h("td", null, s.name), h("td", { class: "mono" }, s.start), h("td", { class: "mono" }, s.length), h("td", null, s.little ? "Intel" : "Motorola"),
        h("td", null, s.signed ? "signed" : "unsigned"), h("td", { class: "mono" }, s.scale + (s.offset ? ` ${s.offset > 0 ? "+" : ""}${s.offset}` : "")), h("td", null, s.unit || ""),
        h("td", { class: "mono" }, s.min != null || s.max != null ? `${s.min ?? ""}…${s.max ?? ""}` : ""),
        h("td", { class: "faint" }, ch.slice(0, 4).map(([k, v]) => `${k}=${v}`).join(", ") + (ch.length > 4 ? " …" : ""))));
    }
  }
  const body = h("div", { class: "stack", style: { gap: "10px", maxHeight: "68vh", overflow: "auto" } },
    h("div", { class: "muted" }, `${r.frames.length} frames · ${r.signals} signals · IDs read as ${r.id_base === "hex" ? "hex" : "decimal"}` + (r.bitrate ? ` · bus ${r.bitrate / 1000} kbit/s` : "")),
    r.warnings.length ? h("div", { class: "card", style: { padding: "10px 14px" } }, h("b", null, "What I assumed / skipped"), r.warnings.map((w) => h("div", { style: { fontSize: "13px", marginTop: "4px" } }, "• " + w))) : null,
    h("table", { class: "t tight" }, h("thead", null, h("tr", null, ["Signal", "Start bit", "Bits", "Order", "Sign", "Factor", "Unit", "Range", "Values"].map((x) => h("th", null, x)))), h("tbody", null, rows)));
  return body;
}

async function up(files) {
  const list = [...files]; if (!list.length) return;
  let ok = 0;
  for (const f of list) {
    try {
      const r = await fetch(tokenUrl("/api/file?name=" + encodeURIComponent(f.name)), { method: "POST", headers: { "X-Token": TOKEN }, body: await f.arrayBuffer() });
      const d = await r.json();
      if (!r.ok) toast(`${f.name}: ${d.error}`, "error"); else ok++;
    } catch (e) { toast(`${f.name}: upload failed`, "error"); }
  }
  if (ok) toast(`Added ${ok} file${ok > 1 ? "s" : ""}.`, "ok");
  await refresh();
}

function heroView(el) {
  const inp = h("input", { type: "text", placeholder: "Project name, e.g. Cluster bring-up", style: { width: "320px" } });
  const create = async () => { if (!inp.value.trim()) return toast("Give the project a name.", "warn"); await api("/api/project/new", { name: inp.value }); await refresh(); };
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") create(); });
  el.appendChild(h("div", { class: "hero" }, h("img", { src: "/static/connectm_logo.png", alt: "ConnectM", style: { height: "64px", borderRadius: "16px", marginBottom: "16px" } }), h("h2", null, "Test any CAN device from its DBC"),
    h("p", null, "Drop in a DBC and, if you have them, the requirement documents. CANBench builds the controls, the checklist and the test scenarios for you."),
    h("div", { class: "row", style: { justifyContent: "center" } }, inp, h("button", { class: "btn primary lg", onclick: create }, "Create project")),
    h("div", { class: "steps" },
      h("div", null, h("b", null, "1 · Load"), "A DBC (or an Excel/CSV table of CAN IDs) plus optional SOR, test plan or alert-code lists."),
      h("div", null, h("b", null, "2 · Assign"), "Choose which frames the bench simulates and which the DUT sends."),
      h("div", null, h("b", null, "3 · Test"), "Run generated scenarios, record Pass/Fail, export a report."))));
}

function rolesCard(s) {
  const m = s.model, linked = {};
  for (const r of s.analysis.requirements) for (const u of r.signals) { const n = u.split(".")[0]; linked[n] = (linked[n] || 0) + 1; }
  const q = h("input", { type: "search", placeholder: "Filter messages…", style: { width: "200px" } });
  const body = h("div", { style: { maxHeight: "460px", overflow: "auto" } });
  const draw = () => {
    const f = q.value.toLowerCase();
    body.innerHTML = "";
    const t = h("table", { class: "t" }, h("thead", null, h("tr", null, ["Message", "ID", "DLC", "Period ms", "Signals", "Spec links", "Role"].map((x) => h("th", null, x)))));
    const tb = h("tbody");
    for (const mm of m.messages) {
      if (f && !(mm.name.toLowerCase().includes(f) || mm.id_hex.toLowerCase().includes(f))) continue;
      const seg = h("div", { class: "seg" }, [["sim", "Simulate", ""], ["listen", "Listen", "ok"], ["ignore", "Ignore", "mute"]].map(([r, l, c]) =>
        h("button", { class: mm.role === r ? "on " + c : "", onclick: async () => { try { await api("/api/roles", { msg: mm.name, role: r }); await refresh(); } catch (e) { /* toast shown */ } } }, l)));
      const per = h("input", { type: "number", value: mm.tx_period_ms, min: 5, max: 60000, title: "from " + mm.period_src, onchange: () => api("/api/period", { msg: mm.name, ms: +per.value }) });
      tb.appendChild(h("tr", { class: mm.role === "ignore" ? "dim" : "" },
        h("td", null, h("b", null, mm.name), mm.placeholder ? h("span", { class: "chip warn", style: { marginLeft: "6px" } }, "placeholder") : null, mm.mux ? h("span", { class: "chip warn", style: { marginLeft: "6px" } }, "mux") : null),
        h("td", { class: "mono" }, mm.id_hex, mm.ext ? h("span", { class: "chip", style: { marginLeft: "6px" } }, "EXT") : null),
        h("td", null, mm.dlc), h("td", null, per), h("td", null, mm.items.length), h("td", null, linked[mm.name] ? h("span", { class: "chip accent" }, linked[mm.name]) : h("span", { class: "faint" }, "–")), h("td", null, seg)));
    }
    t.appendChild(tb); body.appendChild(t);
  };
  q.oninput = draw; draw();
  const bulk = (r) => h("button", { class: "btn sm", onclick: async () => { const map = {}; m.messages.forEach((x) => { if (!x.placeholder) map[x.name] = r; }); await api("/api/roles", { map }); await refresh(); } }, "All → " + r);
  const nodes = m.summary.nodes || [];
  const nodeSel = nodes.length ? h("select", { title: "Pick the node that is your DUT: the frames it transmits become Listen, all others Simulate", onchange: async (e) => { if (e.target.value) { try { await api("/api/dutnode", { node: e.target.value }); await refresh(); } catch (err) { e.target.value = ""; } } } },
    h("option", { value: "" }, "DUT node…"), nodes.map((n) => h("option", { value: n }, n))) : null;
  return h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", null, "Message roles"),
    h("span", { class: "sub" }, "Simulate = the bench sends it · Listen = the DUT sends it, the bench decodes it · Ignore = unused"),
    h("span", { class: "grow" }), nodeSel, q, bulk("sim"), bulk("listen")), body);
}

function docsCard(s) {
  const a = s.analysis, box = h("div", { class: "card-b stack" });
  box.appendChild(h("div", { class: "row wrap" }, s.docs.map((d) => h("span", { class: "chip " + (d.error ? "error" : "accent") }, d.name, " · ", d.blocks, " blocks"))));
  if (a.requirements.length) {
    const lk = a.requirements.filter((r) => r.signals.length).length, na = a.requirements.filter((r) => r.na).length;
    box.appendChild(h("div", null, h("b", null, `${a.requirements.length} requirement rows`), h("span", { class: "muted" }, ` — ${lk} linked to DBC signals, ${a.requirements.length - lk - na} manual checks, ${na} marked N/A. Open `),
      h("a", { href: "#", onclick: (e) => { e.preventDefault(); go("simulate"); } }, "Simulate → By requirement"), h("span", { class: "muted" }, " to work through them.")));
  }
  for (const vm of a.value_maps) {
    const t = h("table", { class: "t" }, h("thead", null, h("tr", null, ["Label in document", "Code", "Stimulus the bench will send", "Match", ""].map((x) => h("th", null, x)))));
    const tb = h("tbody");
    for (const e of vm.entries) {
      tb.appendChild(h("tr", null, h("td", null, e.label), h("td", { class: "mono" }, e.value),
        h("td", { class: "muted" }, e.target ? e.target.why + "  →  " + e.target.signal.split(".")[0] : "— no matching DBC signal —"),
        h("td", null, h("span", { class: "chip " + e.status }, e.status)),
        h("td", null, h("button", { class: "btn ghost sm", onclick: () => editScenarioDialog(e.scenario) }, icon("edit"), "Edit"))));
    }
    t.appendChild(tb);
    box.appendChild(h("div", null, h("div", { class: "row", style: { margin: "6px 0" } }, h("b", null, vm.title), h("span", { class: "chip " + (vm.resolved === vm.total ? "ok" : "warn") }, `${vm.resolved}/${vm.total} matched`), h("span", { class: "faint" }, vm.doc)), t));
  }
  return h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", null, "What the documents contributed"), h("span", { class: "sub" }, "matches are heuristic — review the yellow ones")), box);
}

function metaCard(p) {
  const f = (label, val, on, w) => h("div", { class: "field", style: w ? { flex: w } : { flex: 1 } }, h("label", null, label), h("input", { type: "text", value: val || "", onchange: (e) => on(e.target.value) }));
  const save = (k, v) => api("/api/meta", { [k]: v });
  const dutSave = (k) => (v) => { p.dut[k] = v; return api("/api/meta", { dut: p.dut }); };
  return h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", null, "Test record"), h("span", { class: "sub" }, "printed on every report")),
    h("div", { class: "card-b stack" },
      h("div", { class: "row wrap" }, f("Project name", p.name, (v) => save("name", v).then(refresh)), f("Operator", p.operator, (v) => save("operator", v))),
      h("div", { class: "row wrap" }, f("DUT name", p.dut.name, dutSave("name")), f("DUT serial", p.dut.serial, dutSave("serial")), f("Firmware / version", p.dut.firmware, dutSave("firmware"))),
      h("div", { class: "field" }, h("label", null, "Notes"), h("textarea", { rows: 2, onchange: (e) => save("notes", e.target.value) }, p.notes || "")),
      h("div", { class: "row" }, h("button", { class: "btn bad sm", onclick: async () => { if (await ask({ title: "Delete project", banner: `Delete “${p.name}”?`, body: "Its database, documents, results, bus logs and settings are removed from this PC. This cannot be undone.", yes: "Delete project", danger: true })) { await api("/api/project/delete", { id: p.id }); await refresh(); } } }, icon("trash"), "Delete project"))));
}

/* ---- manual column picker: for sheets the automatic reader gets wrong or cannot read ---- */
async function mapColumnsDialog(fname, opts) {
  let g; try { g = await api("/api/table/grid", { name: fname }); } catch (e) { return; }
  const st_ = { table: 0, header: null, cols: {}, sender: "" };
  let ready = false, footer = null;
  const body = h("div", { class: "stack" }), gridBox = h("div", { class: "mapgrid" }), msg = h("div", { class: "muted" }), prevBox = h("div");
  const tbl = () => g.tables[st_.table];
  const init = () => { const t = tbl(); st_.header = t.auto_header || 1; st_.cols = {}; for (const [k, v] of Object.entries(t.auto_columns || {})) if (k !== "name") st_.cols[k] = v; };
  const label = Object.fromEntries(g.fields.map((x) => [x.key, x.label]));
  const colOf = (i) => Object.keys(st_.cols).find((k) => st_.cols[k] === i) || "";
  const draw = () => {
    const t = tbl(), n = Math.max(1, ...t.rows.map((r) => r.length));
    gridBox.innerHTML = "";
    const head = h("tr", null, h("th", null, "row"), Array.from({ length: n }, (_, i) => {
      const sel = h("select", { onchange: (e) => { Object.keys(st_.cols).forEach((k) => { if (st_.cols[k] === i) delete st_.cols[k]; }); if (e.target.value) { delete st_.cols[e.target.value]; st_.cols[e.target.value] = i; } draw(); } },
        h("option", { value: "" }, "— ignore —"), g.fields.map((x) => h("option", { value: x.key }, x.label)));
      sel.value = colOf(i); return h("th", null, sel);
    }));
    const rows = t.rows.map((r, ri) => h("tr", { class: ri + 1 === st_.header ? "hdr" : ri + 1 < st_.header ? "above" : "" },
      h("td", { class: "rn", title: "Click: this row holds the column titles", onclick: () => { st_.header = ri + 1; draw(); } }, ri + 1),
      Array.from({ length: n }, (_, i) => h("td", { class: colOf(i) ? "sel" : "", title: r[i] || "" }, r[i] || ""))));
    gridBox.appendChild(h("table", null, h("thead", null, head), h("tbody", null, rows)));
    const need = [];
    if (!("id" in st_.cols)) need.push("the CAN ID column");
    const sigLevel = "sig" in st_.cols && ("start" in st_.cols || "bytepos" in st_.cols) && "length" in st_.cols;
    if (!("msg" in st_.cols) && !sigLevel) need.push("a message-name column, or signal name + start bit (or byte position) + length");
    msg.textContent = need.length ? "Still needed: " + need.join(" and ") + "." : `Header row ${st_.header}; ${Object.keys(st_.cols).length} column(s) mapped: ` + Object.keys(st_.cols).map((k) => label[k]).join(", ") + ".";
    msg.style.color = need.length ? "var(--warn)" : "";
    ready = need.length === 0;
    (footer || []).forEach((b) => { if (b.dataset.needs) b.disabled = !ready; });
  };
  const mapping = () => ({ table: st_.table, header_row: st_.header, columns: st_.cols, sender: st_.sender });
  const doImport = async () => {
    try {
      const r = await api("/api/table/import", { ...opts, name: fname, mapping: mapping() });
      toast(`Built a database from ${fname}: ${r.frames} frames, ${r.signals} signals.` + (r.bitrate ? ` Bitrate set to ${r.bitrate / 1000} kbit/s.` : ""), r.warnings.length ? "warn" : "ok", 9000);
      await refresh();
    } catch (e) { return false; }
  };
  const tabSel = g.tables.length > 1 ? h("select", { onchange: (e) => { st_.table = +e.target.value; init(); draw(); } }, g.tables.map((t, i) => h("option", { value: i }, `Table ${i + 1} (${t.n_rows} rows × ${t.n_cols} columns)`))) : null;
  const sender = h("input", { type: "text", placeholder: "e.g. VCU (optional)", style: { width: "180px" }, oninput: (e) => { st_.sender = e.target.value; } });
  body.append(
    h("div", { class: "muted" }, "1. Click the row number of the row that holds the column titles.  2. Choose what each column contains using the drop-downs on top (leave the rest on “ignore”).  3. Preview, then use it. " +
      "Everything you pick is recorded with the file."),
    h("div", { class: "row wrap" }, tabSel, h("span", { class: "faint" }, "Node that sends these frames:"), sender, h("span", { class: "faint" }, "(only needed when the sheet has no sender column)")),
    gridBox, msg, prevBox);
  init(); draw();
  const m = modal({ title: "Map columns — " + fname, body, width: "1180px", buttons: [{ label: "Close" },
    { label: "Preview", onClick: async () => { try { const r = await api("/api/table/preview", { ...opts, name: fname, mapping: mapping() }); prevBox.innerHTML = ""; prevBox.appendChild(h("h4", { style: { margin: "6px 0" } }, "What these columns produce")); prevBox.appendChild(tablePreviewBody(r)); prevBox.scrollIntoView({ behavior: "smooth", block: "nearest" }); } catch (e) { prevBox.innerHTML = ""; } return false; } },
    { label: "Use as CAN database", kind: "primary", onClick: async () => { const r = await doImport(); return r === false ? false : undefined; } }] });
  footer = [...m.el.querySelectorAll(".mf .btn")];
  footer[1].dataset.needs = footer[2].dataset.needs = "1";
  draw();
}
