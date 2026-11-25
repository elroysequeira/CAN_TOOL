/* Simulate view: auto-built controls per message, or per spec requirement */
"use strict";
async function silentRefresh() { CB.S.state = await api("/api/state"); indexState(); }

function buildCtl(it) {
  const uid = it.uid, cur = () => { const v = st().values[uid]; return v === undefined ? it.default : v; };
  let setv; const el = h("div", { class: "ctl" });
  const send = (v) => api("/api/signal", { uid, value: v }).then((r) => { st().values[uid] = r.value; setv(r.value, r.in_range); }).catch(() => setv(cur()));
  const sendT = throttle(send, 70);
  const warn = h("span", { class: "warn-dot hidden", title: "Outside the DBC's declared range (still sent — useful for negative tests)" }, "▲");
  if (it.kind === "bool") {
    const sw = h("button", { class: "switch", role: "switch" }), lab = h("span", { class: "val-pill muted" });
    setv = (v) => { const on = !!v; sw.classList.toggle("on", on); lab.textContent = (it.choices[on ? "1" : "0"] || (on ? "ON" : "OFF")); };
    sw.onclick = () => { const v = sw.classList.contains("on") ? 0 : 1; setv(v); send(v); };
    el.append(sw, lab);
  } else if (it.kind === "enum") {
    const opts = Object.entries(it.choices).map(([k, l]) => h("option", { value: k }, `${k} · ${l}`));
    const sel = h("select", { style: { maxWidth: "210px" } }, opts, h("option", { value: "__raw" }, "other…"));
    const raw = h("input", { type: "number", title: "raw value", min: 0, step: 1 });
    setv = (v) => { sel.value = String(v) in it.choices ? String(v) : "__raw"; raw.value = v; };
    sel.onchange = () => { if (sel.value !== "__raw") { raw.value = sel.value; send(+sel.value); } else raw.focus(); };
    raw.onchange = () => { sel.value = raw.value in it.choices ? raw.value : "__raw"; send(+raw.value); };
    el.append(sel, raw);
  } else {
    const num = h("input", { type: "number", step: it.scale > 0 && it.scale < 1e6 ? it.scale : "any" });
    let rng = null;
    const showRange = !it.composite && !it.counter && it.max > it.min && Number.isFinite(it.min) && Number.isFinite(it.max) && it.max - it.min < 1e7;
    if (showRange) {
      rng = h("input", { type: "range", min: it.min, max: it.max, step: it.scale > 0 ? it.scale : "any" });
      rng.oninput = () => { num.value = rng.value; sendT(+rng.value); };
      rng.onchange = () => send(+rng.value);
    }
    num.onchange = () => { const v = parseFloat(num.value); if (!isNaN(v)) send(v); };
    setv = (v, inRange) => {
      num.value = fmtNum(v); if (rng) rng.value = v;
      const out = inRange === undefined ? (it.soft_range && (v < it.min || v > it.max)) : !inRange;
      warn.classList.toggle("hidden", !out);
    };
    [rng, num, it.unit ? h("span", { class: "muted" }, it.unit) : null, warn].forEach((n) => { if (n) el.append(n); });
    if (it.counter) {
      const on = ((st().auto || {})[it.msg] || []).includes(it.name);
      el.append(h("label", { class: "row muted", title: "Increment on every frame" }, h("input", { type: "checkbox", checked: on, onchange: (e) => api("/api/auto", { uid, on: e.target.checked }) }), "auto +1"));
    }
  }
  setv(cur());
  CB.ctls[uid] = { set: setv };
  return el;
}

function specCell(it) {
  const rs = (CB.idx.req[it.uid] || []).filter((r) => !r.na), d = h("div", { class: "spec" });
  if (!rs.length) { d.appendChild(document.createTextNode(it.comment || "")); return d; }
  rs.slice(0, 2).forEach((r) => d.appendChild(h("span", { class: "chip accent", title: r.text }, (r.no ? "#" + r.no + " " : "") + r.title.slice(0, 26))));
  d.appendChild(document.createTextNode(rs[0].text.slice(0, 160)));
  d.title = rs.map((r) => (r.no ? "#" + r.no + " " : "") + r.title + ": " + r.text).join("\n\n");
  return d;
}
function valueSummary(items) { return items.map((it) => { const v = st().values[it.uid]; return it.name + "=" + fmtNum(v === undefined ? it.default : v) + (it.kind === "enum" && it.choices[v] ? ` (${it.choices[v]})` : ""); }).join("; "); }
function quickVerdict(it, v) {
  const reqs = CB.idx.req[it.uid] || [];
  api("/api/result", { kind: "signal", ref: it.uid, title: `${it.msg}.${it.name}`, group: it.msg, stimulus: valueSummary([it]), expected: reqs[0] ? reqs[0].text : "", verdict: v })
    .then(() => toast(`${v} recorded for ${it.name}`, v === "PASS" ? "ok" : v === "FAIL" ? "error" : "info", 1800));
}

/* A CAN signal has no period of its own: it is sent inside its message. These fields all edit that message's period and stay in step. */
CB.perInputs = {};
function perField(msgName, cls) {
  const m = CB.idx.msgs[msgName], inp = h("input", { type: "number", class: "per-in " + (cls || ""), min: 5, max: 60000, value: m ? m.tx_period_ms : 100,
    title: `Sent every N ms. A signal travels inside its message (${msgName}), so this is the period of the whole message - all its signals share it.`,
    onclick: (e) => e.stopPropagation(), onchange: async () => { try { await api("/api/period", { msg: msgName, ms: +inp.value }); if (m) m.tx_period_ms = +inp.value; syncPer(msgName, +inp.value, inp); } catch (e) { inp.value = m ? m.tx_period_ms : inp.value; } } });
  (CB.perInputs[msgName] = CB.perInputs[msgName] || []).push(inp);
  return inp;
}
function syncPer(msgName, ms, except) { (CB.perInputs[msgName] || []).forEach((i) => { if (i !== except && document.body.contains(i)) i.value = ms; }); }

function sigRow(it) {
  const reqs = CB.idx.req[it.uid] || [];
  return h("div", { class: "sigrow" + (st().analysis.requirements.length && !reqs.length ? " unspec" : "") },
    h("div", { class: "lbl" }, h("b", null, it.name), h("small", null, it.composite ? "16-bit · Hi+Lo merged" : `${it.length} bit${it.length > 1 ? "s" : ""} · bit ${it.start}${it.scale !== 1 ? " · ×" + fmtNum(it.scale) : ""}${it.offset ? " · " + (it.offset > 0 ? "+" : "") + fmtNum(it.offset) : ""}`)),
    h("div", { class: "ctlcol" }, buildCtl(it), h("div", { class: "sigper faint" }, "sent every ", perField(it.msg, "sig"), " ms")), specCell(it),
    h("div", { class: "vd" }, h("button", { class: "btn ok sm icon", title: "Record PASS", onclick: () => quickVerdict(it, "PASS") }, icon("check")), h("button", { class: "btn bad sm icon", title: "Record FAIL", onclick: () => quickVerdict(it, "FAIL") }, icon("x"))));
}

CB.views.simulate = {
  render(el) {
    const s = st(), L = live();
    CB.sim = CB.sim || { mode: pref.get("simMode", "msg"), spec: pref.get("simSpec", null), q: "", showNA: false };
    const S = CB.sim, hasReq = s.analysis.requirements.length > 0;
    if (S.spec === null) S.spec = hasReq;
    const sims = s.model.messages.filter((m) => m.role === "sim" && !m.placeholder);
    const listens = s.model.messages.filter((m) => m.role === "listen");
    el.appendChild(h("div", { class: "page-h" }, h("h2", null, "Simulate"), h("span", { class: "sub" }, `${sims.length} simulated message${sims.length === 1 ? "" : "s"} · ${listens.length} DUT output${listens.length === 1 ? "" : "s"}`)));
    if (!sims.length) { el.appendChild(h("div", { class: "card" }, h("div", { class: "empty" }, h("h3", null, "Nothing to simulate yet"), "Set at least one message to “Simulate” in Setup."))); return; }

    const q = h("input", { type: "search", placeholder: "Search signals, spec text…", value: S.q, style: { width: "240px" }, oninput: debounce((e) => { S.q = e.target.value; draw(); }, 120) });
    const bar = h("div", { class: "row wrap", style: { marginBottom: "14px" } },
      hasReq ? h("div", { class: "seg" }, [["msg", "By message"], ["req", "By requirement"]].map(([k, l]) => h("button", { class: S.mode === k ? "on" : "", onclick: () => { S.mode = k; pref.set("simMode", k); go("simulate"); } }, l))) : null,
      q,
      hasReq && S.mode === "msg" ? h("label", { class: "row muted" }, h("input", { type: "checkbox", checked: S.spec, onchange: (e) => { S.spec = e.target.checked; pref.set("simSpec", S.spec); draw(); } }), "Only spec-linked signals") : null,
      hasReq && S.mode === "req" ? h("label", { class: "row muted" }, h("input", { type: "checkbox", checked: S.showNA, onchange: (e) => { S.showNA = e.target.checked; draw(); } }), "Show N/A rows") : null,
      h("span", { class: "grow" }),
      S.mode === "msg" ? [h("button", { class: "btn", title: "Open every message card", onclick: () => setAllOpen(true) }, "Expand all"), h("button", { class: "btn", title: "Fold every message card", onclick: () => setAllOpen(false) }, "Collapse all")] : null,
      h("button", { class: "btn", title: "Return every signal to the saved baseline and re-enable all frames", onclick: () => api("/api/baseline/reset", {}) }, icon("refresh"), "Reset to baseline"),
      h("button", { class: "btn", title: "Use the current values as the healthy baseline for all scenarios", onclick: async () => { await api("/api/baseline/save", {}); toast("Baseline saved.", "ok"); } }, "Set as baseline"),
      h("button", { class: "btn primary", onclick: captureDialog }, icon("plus"), "Save as scenario"));
    /* sticky banner: nothing is sent until the bench is connected AND transmitting */
    const bannerText = h("span", { class: "grow" }), bannerBtn = h("button", { class: "btn primary" });
    const banner = h("div", { class: "txbanner hidden", id: "txbanner" }, h("b", null, "⚠"), bannerText, bannerBtn);
    CB.simRefs = CB.simRefs || {}; CB.simRefs.banner = { banner, bannerText, bannerBtn };
    el.appendChild(banner);
    el.appendChild(bar);
    /* one period for everything, or each message's own period from the database */
    /* show what is really being sent: the common period when every message has the same one, else the last value typed, else 100 */
    const cur = [...new Set(sims.map((m) => m.tx_period_ms))];
    const allPer = h("input", { type: "number", min: 5, max: 60000, value: cur.length === 1 ? cur[0] : (CB.allPerTyped || 100), style: { width: "84px" }, title: "ms",
      oninput: () => { CB.allPerTyped = +allPer.value; } });
    el.appendChild(h("div", { class: "row wrap per-all", style: { marginBottom: "14px" } },
      h("span", { class: "faint" }, "Send period"), h("span", null, "all messages every"), allPer, h("span", { class: "faint" }, "ms"),
      h("button", { class: "btn sm", id: "per-apply", title: "Give every simulated message this period", onclick: async () => { try { await api("/api/period/all", { ms: +allPer.value }); await silentRefresh(); go("simulate"); toast(`All messages now sent every ${allPer.value} ms.`, "ok", 2500); } catch (e) { /* shown */ } } }, "Apply to all"),
      h("button", { class: "btn sm", id: "per-reset", title: "Back to the period each message has in the database", onclick: async () => { try { await api("/api/period/all", { reset: true }); await silentRefresh(); go("simulate"); toast("Periods are back to the database values.", "ok", 2500); } catch (e) { /* shown */ } } }, "Use database periods"),
      h("span", { class: "faint" }, "Each message also has its own period in its title bar. A signal is sent inside its message, so it shares that period.")));
    CB.perInputs = {};
    const wrap = h("div", { class: "sim-wrap" });
    const main = h("div"), side = h("div", { class: "sim-side" });
    wrap.append(main, side); el.appendChild(wrap);
    CB.simRefs = { cnt: {}, sw: {}, card: {}, fb: {}, banner: CB.simRefs.banner };

    function draw() {
      CB.perInputs = {}; CB.ctls = {}; CB.simRefs.cnt = {}; CB.simRefs.sw = {}; CB.simRefs.card = {}; CB.simRefs.reqChip = {};
      main.innerHTML = "";
      const ql = S.q.toLowerCase();
      if (S.mode === "req" && hasReq) return drawReq(main, ql);
      let shown = 0;
      const openMap = openState(); const searching = !!ql;
      for (const m of sims) {
        let items = m.items.filter((it) => {
          const reqs = CB.idx.req[it.uid] || [];
          if (S.spec && hasReq && !reqs.some((r) => !r.na)) return false;
          if (ql) { const blob = (it.name + " " + it.comment + " " + reqs.map((r) => r.title + " " + r.text).join(" ")).toLowerCase(); if (!blob.includes(ql)) return false; }
          return true;
        });
        if (!items.length) continue;
        shown += items.length;
        const body = h("div", { class: "msg-b" }, items.map(sigRow));
        const sw = h("button", { class: "switch red on", title: "Enable / stop this frame", onclick: async (e) => { e.stopPropagation(); const on = !sw.classList.contains("on"); await api("/api/enable", { msg: m.name, on }); sw.classList.toggle("on", on); card.classList.toggle("off", !on); } });
        const per = perField(m.name, "msg");
        const cnt = h("span", { class: "cnt" }, "0 sent");
        const card = h("div", { class: "card msg" + (openMap[m.name] || searching ? "" : " collapsed"), "data-msg": m.name },
          h("div", { class: "msg-h", onclick: () => { card.classList.toggle("collapsed"); const c = openState(); if (card.classList.contains("collapsed")) delete c[m.name]; else c[m.name] = true; saveOpen(c); } },
            icon("chev", "chev"), sw, h("span", { class: "nm" }, m.name), h("span", { class: "chip mono" }, m.id_hex), m.ext ? h("span", { class: "chip" }, "EXT") : null,
            h("span", { class: "faint" }, `${m.dlc} B · ${items.length} signal${items.length === 1 ? "" : "s"}`), h("span", { class: "grow" }), h("span", { class: "faint" }, "every"), per, h("span", { class: "faint" }, "ms"), cnt,
            h("button", { class: "btn ghost sm icon", title: "Send one frame now", onclick: (e) => { e.stopPropagation(); api("/api/once", { msg: m.name }); } }, icon("bolt"))),
          body);
        CB.simRefs.cnt[m.name] = cnt; CB.simRefs.sw[m.name] = { sw, card };
        main.appendChild(card);
      }
      if (!shown) main.appendChild(h("div", { class: "card" }, h("div", { class: "empty" }, h("h3", null, "No signals match"), "Clear the search or untick “Only spec-linked signals”.")));
    }

    /* message cards start folded (a DBC can have dozens of messages and hundreds of signals); what you open is remembered per project */
    function openState() { return pref.get("simOpen:" + ((s.project || {}).id || ""), {}) || {}; }
    function saveOpen(c) { pref.set("simOpen:" + ((s.project || {}).id || ""), c); }
    function setAllOpen(on) { const c = {}; if (on) sims.forEach((m) => { c[m.name] = true; }); saveOpen(c); draw(); }

    function drawReq(main, ql) {
      const reqs = s.analysis.requirements.filter((r) => (S.showNA || !r.na) && (!ql || (r.title + " " + r.text).toLowerCase().includes(ql)));
      if (!reqs.length) main.appendChild(h("div", { class: "card" }, h("div", { class: "empty" }, h("h3", null, "No requirements match"))));
      for (const r of reqs) {
        const its = r.signals.map((u) => CB.idx.items[u]).filter(Boolean);
        const simmable = its.filter((it) => (CB.idx.msgs[it.msg].role === "sim"));
        const others = its.filter((it) => CB.idx.msgs[it.msg].role !== "sim");
        const note = h("input", { type: "text", placeholder: "Note (optional)", style: { width: "240px" } });
        const chip = h("span", { class: "chip" }, "not tested");
        CB.simRefs.reqChip[r.id] = chip; paintChip(chip, CB.idx.lastByRef[r.id]);
        const rec = (v) => api("/api/result", { kind: "requirement", ref: r.id, title: (r.no ? "#" + r.no + " " : "") + r.title, group: r.doc, stimulus: valueSummary(its) || "manual observation", expected: r.text, verdict: v, note: note.value }).then(() => { note.value = ""; toast(`${v} recorded for ${r.title}`, v === "PASS" ? "ok" : v === "FAIL" ? "error" : "info", 1800); });
        main.appendChild(h("div", { class: "card req" },
          h("div", { class: "req-h" }, h("div", { class: "no" }, r.no || "·"), h("div", { class: "grow" }, h("div", { class: "row" }, h("b", null, r.title), h("span", { class: "chip" }, r.doc.length > 26 ? r.doc.slice(0, 24) + "…" : r.doc), r.na ? h("span", { class: "chip warn" }, "N/A") : null, r.aliases.map((a) => h("span", { class: "chip info", title: "Spec name resolved to the DBC name" }, a.token + " → " + a.uid.split(".")[1]))), r.text ? h("div", { class: "txt" }, r.text) : null)),
          simmable.map(sigRow),
          others.map((it) => h("div", { class: "sigrow unspec" }, h("div", { class: "lbl" }, h("b", null, it.name), h("small", null, it.msg)), h("span", { class: "faint" }, "not simulated (role: " + CB.idx.msgs[it.msg].role + ")"), h("div"), h("div"))),
          !its.length ? h("div", { class: "sigrow unspec", style: { gridTemplateColumns: "1fr" } }, h("span", { class: "faint" }, "No DBC signal linked — a manual check (discrete IO, display behaviour, etc.).")) : null,
          h("div", { class: "vbar" }, h("button", { class: "btn ok", onclick: () => rec("PASS") }, icon("check"), "Pass"), h("button", { class: "btn bad", onclick: () => rec("FAIL") }, icon("x"), "Fail"), h("button", { class: "btn", onclick: () => rec("SKIP") }, "N/A"), note, h("span", { class: "grow" }), chip)));
      }
    }
    draw();

    /* DUT feedback */
    CB.unk = buildUnknownCard(); side.appendChild(CB.unk.el); CB.unk.update(L.unknown || []);
    if (!listens.length) {
      side.appendChild(h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", null, "DUT output")), h("div", { class: "card-b muted" },
        "No DUT output frames are defined. Add a DBC with the frames your DUT transmits (or set a message to ", h("b", null, "Listen"), " in Setup) and its live values appear here — scenarios can then verify them automatically.")));
    }
    for (const m of listens) {
      const age = h("span", { class: "chip" }, "never"), rows = {};
      const list = h("div", { class: "card-b" }, m.items.map((it) => { const v = h("b", null, "–"); const r = h("div", { class: "fb-row" }, h("span", { title: it.name }, it.name), v); rows[it.name] = { r, v, it }; return r; }));
      side.appendChild(h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", null, m.name), h("span", { class: "mono faint" }, m.id_hex), h("span", { class: "grow" }), age), list));
      CB.simRefs.fb[m.name] = { age, rows };
    }
    CB.views.simulate.onLive(L);
  },
  onLive(d) {
    const R = CB.simRefs; if (!R) return;
    if (R.banner) {
      const c = d.conn || {}, b = R.banner;
      if (!c.connected) { b.banner.classList.remove("hidden"); b.bannerText.textContent = "Not connected — nothing is being sent. Choose the interface in the top bar and press Connect."; b.bannerBtn.textContent = "Connect"; b.bannerBtn.onclick = () => { const x = $("#btn-conn"); if (x) x.click(); }; }
      else if (!c.tx_on) { b.banner.classList.remove("hidden"); b.bannerText.textContent = "Transmission is stopped — values you set are held and will be sent as soon as you press Start TX."; b.bannerBtn.textContent = "Start TX"; b.bannerBtn.onclick = () => api("/api/tx", { on: true }); }
      else b.banner.classList.add("hidden");
    }
    for (const [n, c] of Object.entries(R.cnt || {})) { const t = d.tx[n]; if (t) c.textContent = t.count + " sent" + (t.late_ms > 8 ? " · ±" + t.late_ms + "ms" : ""); }
    for (const [n, o] of Object.entries(R.sw || {})) { const t = d.tx[n]; if (t) { o.sw.classList.toggle("on", t.enabled); o.card.classList.toggle("off", !t.enabled); } }
    for (const [n, f] of Object.entries(R.fb || {})) {
      const x = d.rx[n]; if (!x) continue;
      if (x.age === null) f.age.className = "chip", f.age.textContent = "never";
      else if (x.age < 1.0 + (x.period_ms || 0) / 500) f.age.className = "chip ok", f.age.textContent = "live" + (x.period_ms ? " · " + x.period_ms + " ms" : "");
      else f.age.className = "chip bad", f.age.textContent = "lost " + Math.round(x.age) + " s";
      for (const [k, o] of Object.entries(f.rows)) { const v = x.values[k]; if (v === undefined) continue; o.v.textContent = fmtNum(v) + (o.it.kind === "enum" && o.it.choices[v] ? " · " + o.it.choices[v] : "") + (o.it.unit ? " " + o.it.unit : ""); o.r.classList.toggle("on", o.it.kind === "bool" && !!v); }
    }
  },
  onValues(d) { for (const [u, c] of Object.entries(CB.ctls)) if (d[u] !== undefined) c.set(d[u]); },
  onResult(r) { const c = CB.simRefs && CB.simRefs.reqChip && CB.simRefs.reqChip[r.ref]; if (c) paintChip(c, r); },
};
function paintChip(chip, r) { if (!r) { chip.className = "chip"; chip.textContent = "not tested"; return; } chip.className = "chip " + r.verdict; chip.textContent = r.verdict + " · " + fmtTime(r.ts).slice(0, 8); }

function captureDialog() {
  const t = h("input", { type: "text", placeholder: "e.g. Battery temp 61 °C → fast beep" }), g = h("input", { type: "text", value: "Custom" });
  const e = h("textarea", { rows: 3, placeholder: "What should the DUT do?" }), d = h("input", { type: "number", value: 1500, style: { width: "110px" } });
  modal({ title: "Save current state as a scenario", body: [
    h("div", { class: "muted" }, "Captures every signal that differs from the baseline, plus any frames you switched off. You can add automatic checks later via Edit."),
    h("div", { class: "field" }, h("label", null, "Title"), t), h("div", { class: "field" }, h("label", null, "Group"), g),
    h("div", { class: "field" }, h("label", null, "Expected behaviour"), e), h("div", { class: "field" }, h("label", null, "Settle time (ms)"), d)],
  buttons: [{ label: "Save scenario", kind: "primary", onClick: async () => { if (!t.value.trim()) { toast("Add a title.", "warn"); return false; } await api("/api/scenario/capture", { title: t.value, group: g.value, expect: e.value, dwell_ms: +d.value }); await silentRefresh(); toast("Scenario saved — find it under Scenarios → Custom.", "ok"); } }] });
  setTimeout(() => t.focus(), 50);
}
