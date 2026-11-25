/* Results, CAN trace, Serial */
"use strict";
function download(url, name) { const a = h("a", { href: url, download: name || "" }); document.body.appendChild(a); a.click(); a.remove(); }
function saveText(text, name) { const u = URL.createObjectURL(new Blob([text], { type: "text/plain" })); download(u, name); setTimeout(() => URL.revokeObjectURL(u), 4000); }

/* ---------------- results ---------------- */
CB.views.results = {
  render(el) {
    CB.res = CB.res || { f: "all", q: "", open: {}, sel: new Set() };
    const S = CB.res;
    el.appendChild(h("div", { class: "page-h" }, h("h2", null, "Results"), h("span", { class: "sub" }, "Saved to disk the moment each verdict is recorded"), h("span", { class: "grow" }),
      h("button", { class: "btn", onclick: () => download(tokenUrl("/api/results.csv")) }, icon("save"), "Export CSV"),
      h("button", { class: "btn primary", onclick: () => download(tokenUrl("/api/report.html")) }, icon("file"), "Report (HTML)")));
    const top = h("div", { class: "row", style: { marginBottom: "14px", alignItems: "stretch" } }), tiles = h("div", { class: "tiles grow" });
    top.append(tiles); el.appendChild(top);
    const bar = h("div", { class: "row wrap", style: { marginBottom: "12px" } }), card = h("div", { class: "card" }), body = h("div", { style: { overflow: "auto" } });
    card.appendChild(body); el.append(bar, card);

    const draw = () => {
      const all = st().results, c = { PASS: 0, FAIL: 0, SKIP: 0 };
      all.forEach((r) => { c[r.verdict] = (c[r.verdict] || 0) + 1; });
      const dec = c.PASS + c.FAIL, rate = dec ? c.PASS / dec : 0, C = 2 * Math.PI * 46;
      tiles.innerHTML = "";
      tiles.append(
        h("div", { class: "tile" }, ringSvg(rate, dec, C)),
        h("div", { class: "tile PASS" }, h("b", null, c.PASS), h("span", null, "Passed")), h("div", { class: "tile FAIL" }, h("b", null, c.FAIL), h("span", null, "Failed")),
        h("div", { class: "tile SKIP" }, h("b", null, c.SKIP), h("span", null, "Skipped")), h("div", { class: "tile" }, h("b", null, all.length), h("span", null, "Total recorded")));
      bar.innerHTML = "";
      bar.append(h("div", { class: "seg" }, [["all", "All"], ["PASS", "Pass"], ["FAIL", "Fail"], ["SKIP", "Skip"]].map(([k, l]) => h("button", { class: S.f === k ? "on" : "", onclick: () => { S.f = k; draw(); } }, l))),
        h("input", { type: "search", placeholder: "Search results…", value: S.q, style: { width: "240px" }, oninput: debounce((e) => { S.q = e.target.value; drawTable(); }, 120) }),
        h("span", { class: "grow" }),
        h("button", { class: "btn bad sm", disabled: !S.sel.size, onclick: async () => { if (await ask({ title: "Delete results", body: `Delete ${S.sel.size} selected result(s)? This cannot be undone.`, yes: "Delete", danger: true })) { await api("/api/result/delete", { ids: [...S.sel] }); st().results = st().results.filter((r) => !S.sel.has(r.id)); S.sel.clear(); indexState(); draw(); renderRail(); } } }, icon("trash"), "Delete selected"),
        h("button", { class: "btn sm", onclick: async () => { if (all.length && await ask({ title: "Clear all results", body: "Clear ALL results of this project? Reports and CSV exports will be empty afterwards. This cannot be undone.", yes: "Clear all", danger: true })) { await api("/api/result/clear", {}); st().results = []; indexState(); draw(); } } }, "Clear all"));
      drawTable();
    };
    const drawTable = () => {
      const ql = S.q.toLowerCase();
      const rows = st().results.filter((r) => (S.f === "all" || r.verdict === S.f) && (!ql || (r.title + " " + r.group + " " + r.note + " " + r.expected).toLowerCase().includes(ql))).slice().reverse();
      body.innerHTML = "";
      if (!rows.length) { body.appendChild(h("div", { class: "empty" }, h("h3", null, "No results yet"), "Record Pass/Fail in Simulate or run scenarios.")); return; }
      const t = h("table", { class: "t" }, h("thead", null, h("tr", null, ["", "Time", "Test", "Verdict", "Expected", "Note"].map((x) => h("th", null, x))))), tb = h("tbody");
      for (const r of rows.slice(0, 600)) {
        const open = !!S.open[r.id];
        tb.appendChild(h("tr", { class: "res-row" + (open ? " open" : ""), style: { cursor: "pointer" }, onclick: (e) => { if (e.target.tagName === "INPUT") return; S.open[r.id] = !open; drawTable(); } },
          h("td", { style: { width: "30px" } }, h("input", { type: "checkbox", checked: S.sel.has(r.id), onchange: (e) => { e.target.checked ? S.sel.add(r.id) : S.sel.delete(r.id); draw(); } })),
          h("td", { class: "mono muted", style: { whiteSpace: "nowrap" } }, fmtDT(r.ts)), h("td", null, h("b", null, r.title), h("div", { class: "faint", style: { fontSize: "12px" } }, r.group)),
          h("td", null, h("span", { class: "chip " + r.verdict }, r.verdict), h("div", { class: "faint", style: { fontSize: "11px", marginTop: "2px" } }, r.mode)),
          h("td", { class: "muted", style: { maxWidth: "380px" } }, (r.expected || "").slice(0, 140)), h("td", null, r.note || "")));
        if (open) tb.appendChild(h("tr", { class: "res-row open" }, h("td", { colspan: 6 }, h("div", { class: "res-detail" },
          h("div", { class: "stack", style: { gap: "10px" } },
            h("div", null, h("h5", null, "Stimulus"), h("div", { class: "mono" }, r.stimulus || "–")), h("div", null, h("h5", null, "Expected"), h("div", null, r.expected || "–")),
            (r.checks || []).length ? h("div", null, h("h5", null, "Automatic checks"), r.checks.map((k) => h("div", null, h("span", { class: "chip " + (k.ok ? "PASS" : "FAIL") }, k.ok ? "ok" : "fail"), " ", h("span", { class: "mono" }, k.desc), h("span", { class: "muted" }, "  — " + k.detail)))) : null),
          h("div", { class: "stack", style: { gap: "10px" } },
            h("div", null, h("h5", null, "DUT output at that moment"), Object.keys(r.dut || {}).length ? h("pre", null, Object.entries(r.dut).map(([k, v]) => k + " = " + v).join("\n")) : h("span", { class: "muted" }, "no DUT frames received / none configured")),
            (r.serial || []).length ? h("div", null, h("h5", null, "Serial log tail"), h("pre", null, r.serial.join("\n"))) : null)))));
      }
      t.appendChild(tb); body.appendChild(t);
    };
    CB.views.results.redraw = draw; draw();
  },
  onResult() { if (CB.views.results.redraw) CB.views.results.redraw(); },
};
function ringSvg(rate, dec, C) {
  const ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 120 120"); svg.setAttribute("class", "ring"); svg.style.width = svg.style.height = "86px";
  svg.innerHTML = `<circle cx="60" cy="60" r="46" fill="none" stroke="var(--card3)" stroke-width="12"/><circle cx="60" cy="60" r="46" fill="none" stroke="var(--ok)" stroke-width="12" stroke-linecap="round" stroke-dasharray="${C * rate} ${C}" transform="rotate(-90 60 60)" style="stroke:var(--ok)"/><text x="60" y="66" text-anchor="middle" font-size="22" font-weight="700" fill="currentColor" style="stroke:none">${dec ? Math.round(rate * 100) + "%" : "–"}</text>`;
  return svg;
}

/* ---------------- trace ---------------- */
CB.views.trace = {
  render(el) {
    CB.tr = CB.tr || { filter: "", showTx: true };
    const S = CB.tr;
    const recChip = h("span", { class: "chip" }), logsBtn = h("button", { class: "btn", title: "What is recorded and where it is saved", onclick: () => go("info") }, icon("info"), "About the recording");
    const drawRec = () => {
      const b = live().buslog || {}, en = (st().buslog || {}).enabled;
      recChip.className = "chip " + (b.active ? (b.dropped ? "warn" : "bad") : "");
      recChip.textContent = b.active ? `● Recording ${b.name} · ${b.frames.toLocaleString()} frames` + (b.dropped ? ` · ${b.dropped} dropped` : "") : en === false ? "Recording is off" : "Recording starts when you connect";
    };
    CB.views.trace.onLive = drawRec;
    el.appendChild(h("div", { class: "page-h" }, h("h2", null, "CAN trace"), h("span", { class: "sub" }, "Every frame on the bus, decoded with your DBC"), h("span", { class: "grow" }), recChip, logsBtn));
    drawRec();
    const log = h("div", { class: "log" });
    const pauseBtn = h("button", { class: "btn", onclick: () => { CB.tracePaused = !CB.tracePaused; pauseBtn.textContent = CB.tracePaused ? "Resume" : "Pause"; pauseBtn.classList.toggle("primary", CB.tracePaused); } }, CB.tracePaused ? "Resume" : "Pause");
    const match = (r) => {
      if (!S.showTx && r[2] === "TX") return false;
      const f = S.filter.trim().toLowerCase(); if (!f) return true;
      return f.split(",").some((p) => { p = p.trim(); return p && (r[3].toLowerCase().includes(p) || r[6].toLowerCase().includes(p) || r[7].toLowerCase().includes(p)); });
    };
    const line = (r) => h("div", { class: "ln " + r[2] }, h("span", { class: "tm" }, fmtTime(r[1])), h("span", null, r[2]), h("span", null, r[3].padStart(8)), h("span", { class: "faint" }, "[" + r[4] + "]"), h("span", { style: { minWidth: "190px" } }, r[5]), h("span", { class: "nm" }, r[6]), h("span", { class: "dec" }, r[7]));
    const paint = () => { log.innerHTML = ""; const f = document.createDocumentFragment(); CB.trace.filter(match).slice(-1200).forEach((r) => f.appendChild(line(r))); log.appendChild(f); log.scrollTop = log.scrollHeight; };
    CB.views.trace.onTrace = (rows) => {
      if (CB.tracePaused) return;
      const near = log.scrollHeight - log.scrollTop - log.clientHeight < 60, f = document.createDocumentFragment();
      rows.filter(match).forEach((r) => f.appendChild(line(r))); log.appendChild(f);
      while (log.childElementCount > 1500) log.firstChild.remove();
      if (near) log.scrollTop = log.scrollHeight;
    };
    el.appendChild(h("div", { class: "logwrap" }, h("div", { class: "row wrap", style: { marginBottom: "10px" } }, pauseBtn,
      h("label", { class: "row muted" }, h("input", { type: "checkbox", checked: S.showTx, onchange: (e) => { S.showTx = e.target.checked; paint(); } }), "Show TX"),
      h("input", { type: "search", placeholder: "Filter: id, message or signal text (comma = OR)", value: S.filter, style: { width: "340px" }, oninput: debounce((e) => { S.filter = e.target.value; paint(); }, 150) }),
      h("span", { class: "grow" }), h("button", { class: "btn", onclick: () => { CB.trace.length = 0; log.innerHTML = ""; } }, "Clear"),
      h("button", { class: "btn", onclick: () => saveText(CB.trace.filter(match).map((r) => `${fmtTime(r[1])} ${r[2]} ${r[3]} [${r[4]}] ${r[5]} ${r[6]} ${r[7]}`).join("\n"), "can_trace.txt") }, icon("save"), "Save")), log));
    paint();
  },
  onTrace: null,
};

/* ---------------- serial ---------------- */
CB.views.serial = {
  async render(el) {
    CB.se = CB.se || { pause: false, marks: true, hl: "" };
    const S = CB.se;
    el.appendChild(h("div", { class: "page-h" }, h("h2", null, "Serial log"), h("span", { class: "sub" }, "Debug UART with markers for every CAN change and scenario")));
    if (!st().serial_available) { el.appendChild(h("div", { class: "card" }, h("div", { class: "empty" }, h("h3", null, "pyserial is not installed"), h("span", { class: "mono" }, "pip install pyserial")))); return; }
    const info = await api("/api/serial/ports");
    const port = h("select", { style: { minWidth: "260px" } }, info.ports.map((p) => h("option", { value: p.device }, `${p.device} — ${p.description}`)), info.ports.length ? null : h("option", { value: "" }, "no ports found"));
    const baud = h("select", null, [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600].map((b) => h("option", { value: b, selected: b === 115200 }, b)));
    let open = info.open;
    const openBtn = h("button", { class: "btn primary", onclick: async () => { if (open) { await api("/api/serial/close", {}); open = false; } else { await api("/api/serial/open", { port: port.value, baud: +baud.value }); open = true; } openBtn.textContent = open ? "Close" : "Open"; openBtn.classList.toggle("primary", !open); } }, open ? "Close" : "Open");
    const log = h("div", { class: "log" });
    const hl = h("input", { type: "text", placeholder: "highlight…", value: S.hl, style: { width: "150px" }, oninput: (e) => { S.hl = e.target.value; paint(); } });
    const line = (r) => {
      const [ts, kind, text] = r, hit = S.hl && text.toLowerCase().includes(S.hl.toLowerCase());
      return h("div", { class: "ln " + (kind === "RX" ? "RX" : kind === "TX" ? "TX" : kind === "ERR" ? "ERR" : "MARK") + (hit ? " hl" : "") }, h("span", { class: "tm" }, fmtTime(ts)), h("span", null, kind === "MARK" ? "═ " + text + " ═" : kind === "TX" ? ">> " + text : text));
    };
    const keep = (r) => S.marks || r[1] !== "MARK";
    const paint = () => { log.innerHTML = ""; const f = document.createDocumentFragment(); CB.serialLog.filter(keep).slice(-1500).forEach((r) => f.appendChild(line(r))); log.appendChild(f); log.scrollTop = log.scrollHeight; };
    CB.views.serial.onSerial = (rows) => {
      if (S.pause) return; const near = log.scrollHeight - log.scrollTop - log.clientHeight < 60, f = document.createDocumentFragment();
      rows.filter(keep).forEach((r) => f.appendChild(line(r))); log.appendChild(f); while (log.childElementCount > 1800) log.firstChild.remove(); if (near) log.scrollTop = log.scrollHeight;
    };
    const send = h("input", { type: "text", placeholder: "Send to DUT…", style: { flex: 1 } }), eol = h("select", null, [["\r\n", "CR+LF"], ["\n", "LF"], ["\r", "CR"], ["", "none"]].map(([v, l]) => h("option", { value: v }, l)));
    const doSend = async () => { if (!send.value) return; await api("/api/serial/send", { text: send.value, eol: eol.value }); send.value = ""; };
    send.addEventListener("keydown", (e) => { if (e.key === "Enter") doSend(); });
    el.appendChild(h("div", { class: "logwrap" }, h("div", { class: "row wrap", style: { marginBottom: "10px" } }, port, h("button", { class: "btn ghost icon", title: "Refresh ports", onclick: () => go("serial") }, icon("refresh")), baud, openBtn,
      h("label", { class: "row muted" }, h("input", { type: "checkbox", onchange: (e) => { S.pause = e.target.checked; } }), "Pause"),
      h("label", { class: "row muted" }, h("input", { type: "checkbox", checked: S.marks, onchange: (e) => { S.marks = e.target.checked; paint(); } }), "CAN markers"), hl,
      h("span", { class: "grow" }), h("button", { class: "btn", onclick: () => { CB.serialLog.length = 0; log.innerHTML = ""; } }, "Clear"),
      h("button", { class: "btn", onclick: () => saveText(CB.serialLog.map((r) => `${fmtTime(r[0])} ${r[1]} ${r[2]}`).join("\n"), "serial_log.txt") }, icon("save"), "Save")),
      log, h("div", { class: "row", style: { marginTop: "10px" } }, send, eol, h("button", { class: "btn primary", onclick: doSend }, "Send"))));
    paint();
  },
  onSerial: null,
};
