/* Scenarios view: generated + custom scenarios, runner, editor */
"use strict";
const SRC_RANK = { custom: 0, doc: 1, auto: 2 };
const lastVerdict = (id) => (CB.idx.lastByScen[id] || {}).verdict || "";

CB.views.scenarios = {
  render(el) {
    const s = st();
    CB.scen = CB.scen || { sel: null, q: "", src: "all", status: "all", collapsed: pref.get("scCollapsed", {}), manual: "prompt" };
    const S = CB.scen;
    el.appendChild(h("div", { class: "page-h" }, h("h2", null, "Scenarios"), h("span", { class: "sub" }, `${s.scenarios.length} scenarios — generated from your DBC and documents, plus your own`),
      h("span", { class: "grow" }), h("button", { class: "btn primary", onclick: () => editScenarioDialog(null) }, icon("plus"), "New scenario")));
    const list = h("div", { class: "card sc-list" }), detail = h("div", { class: "card sc-detail" });
    el.appendChild(h("div", { class: "sc-wrap" }, list, detail));
    const seg = (key, opts) => h("div", { class: "seg" }, opts.map(([k, l]) => h("button", { class: S[key] === k ? "on" : "", onclick: () => { S[key] = k; drawList(); } }, l)));
    const q = h("input", { type: "search", placeholder: "Search…", value: S.q, style: { width: "100%" }, oninput: debounce((e) => { S.q = e.target.value; drawList(); }, 120) });
    const head = h("div", { class: "card-b stack", style: { gap: "8px", borderBottom: "1px solid var(--line)" } }, q,
      h("div", { class: "row wrap" }, seg("src", [["all", "All"], ["doc", "Docs"], ["auto", "Auto"], ["custom", "Mine"]]), seg("status", [["all", "Any"], ["none", "Not run"], ["PASS", "Pass"], ["FAIL", "Fail"]])),
      h("div", { class: "row" }, h("button", { class: "btn primary sm", id: "run-visible", onclick: () => runIds(visible().filter((x) => !x.incomplete).map((x) => x.id)) }, icon("play"), "Run visible"),
        h("div", { class: "seg" }, [["prompt", "Ask me"], ["skip", "Auto only"]].map(([k, l]) => h("button", { title: k === "skip" ? "Scenarios without automatic checks are skipped" : "Pause for your verdict when a scenario has no automatic check", class: S.manual === k ? "on" : "", onclick: (e) => { S.manual = k; [...e.target.parentNode.children].forEach((b) => b.classList.toggle("on", b === e.target)); } }, l))),
        h("span", { class: "grow" }), h("button", { class: "btn ghost sm", title: "Bring back removed or edited generated scenarios", onclick: async () => { if (await ask({ title: "Restore scenarios", body: "Restore all removed and edited generated scenarios to what the bench generated?", yes: "Restore" })) { await api("/api/scenario/restore", {}); await refresh(); } } }, "Restore")));
    const scroll = h("div", { class: "scroll" });
    list.append(head, scroll);

    function visible() {
      const ql = S.q.toLowerCase();
      return s.scenarios.filter((x) => (S.src === "all" || x.source === S.src) &&
        (S.status === "all" || (S.status === "none" ? !lastVerdict(x.id) : lastVerdict(x.id) === S.status)) &&
        (!ql || (x.title + " " + x.group + " " + (x.expect || "")).toLowerCase().includes(ql)));
    }
    function drawList() {
      const top = scroll.scrollTop; scroll.innerHTML = "";
      const vis = visible(), groups = new Map();
      for (const x of vis) { if (!groups.has(x.group)) groups.set(x.group, []); groups.get(x.group).push(x); }
      const ordered = [...groups.entries()].sort((a, b) => (SRC_RANK[a[1][0].source] ?? 9) - (SRC_RANK[b[1][0].source] ?? 9));
      $("#run-visible").textContent = "";
      $("#run-visible").append(icon("play"), `Run visible (${vis.length})`);
      if (!vis.length) scroll.appendChild(h("div", { class: "empty" }, "No scenarios match."));
      for (const [g, items] of ordered) {
        const pass = items.filter((i) => lastVerdict(i.id) === "PASS").length, fail = items.filter((i) => lastVerdict(i.id) === "FAIL").length;
        const col = !!S.collapsed[g] && !S.q;
        const box = h("div", { class: "sc-group" + (col ? " collapsed" : "") },
          h("div", { class: "gh", onclick: () => { S.collapsed[g] = !S.collapsed[g]; pref.set("scCollapsed", S.collapsed); drawList(); } }, icon("chev", "chev"), h("span", null, g),
            h("span", { class: "n" }, items.length + (pass ? ` · ${pass}✓` : "") + (fail ? ` · ${fail}✗` : "")),
            h("button", { class: "btn ghost icon sm", title: "Run this group", onclick: (e) => { e.stopPropagation(); runIds(items.filter((x) => !x.incomplete).map((x) => x.id)); } }, icon("play"))),
          h("div", { class: "items" }, items.map((x) => h("div", { class: "sc-item" + (S.sel === x.id ? " sel" : ""), onclick: () => { S.sel = x.id; drawList(); drawDetail(); } },
            h("span", { class: "dot " + (lastVerdict(x.id) || "idle") }), h("span", { class: "tt", title: x.title }, x.title),
            x.incomplete ? h("span", { class: "chip warn" }, "setup") : x.review ? h("span", { class: "chip review" }, "review") : null,
            x.checks && x.checks.length ? h("span", { class: "chip ok", title: "automatic checks" }, "auto") : null))));
        scroll.appendChild(box);
      }
      scroll.scrollTop = top;
    }
    function drawDetail() {
      detail.innerHTML = "";
      const x = CB.idx.scen[S.sel];
      if (!x) { detail.appendChild(h("div", { class: "empty" }, h("h3", null, "Select a scenario"), "It shows the stimulus, the expected behaviour and its history.")); return; }
      const rows = x.set.map((a) => { const it = CB.idx.items[a.uid]; return it ? h("div", { class: "mono" }, `${it.msg}.${it.name} = ${fmtNum(a.value)}${it.kind === "enum" && it.choices[a.value] ? "  (" + it.choices[a.value] + ")" : it.unit ? " " + it.unit : ""}`) : h("div", { class: "mono bad" }, a.uid + " (not in DBC)"); });
      const hist = s.results.filter((r) => r.scenario === x.id).slice(-8).reverse();
      detail.append(
        h("div", { class: "card-h" }, h("h3", null, x.title), h("span", { class: "chip" }, x.group), h("span", { class: "chip accent" }, x.source), x.edited ? h("span", { class: "chip info" }, "edited") : null,
          h("span", { class: "grow" }),
          h("button", { class: "btn primary", disabled: x.incomplete, onclick: () => runIds([x.id]) }, icon("play"), "Run"),
          h("button", { class: "btn", onclick: () => editScenarioDialog(x.id) }, icon("edit"), "Edit"),
          h("button", { class: "btn bad icon", title: x.source === "custom" ? "Delete" : "Remove from list (restorable)", onclick: async () => { if (await ask({ title: x.source === "custom" ? "Delete scenario" : "Remove scenario", body: `Remove “${x.title}”?`, yes: "Remove", danger: true })) { await api("/api/scenario/delete", { id: x.id }); S.sel = null; await refresh(); } } }, icon("trash"))),
        h("div", { class: "card-b stack" },
          x.expect ? h("div", { class: "expect" }, x.expect) : null,
          x.incomplete ? h("div", { class: "chip warn" }, "No stimulus assigned yet — press Edit and choose the signal to drive.") : null,
          h("div", { class: "kv" },
            h("div", null, "Stimulus"), h("div", null, rows.length ? rows : h("span", { class: "muted" }, "baseline only")),
            h("div", null, "Frames stopped"), h("div", null, x.tx_off === "ALL" ? "all frames" : (x.tx_off || []).length ? x.tx_off.join(", ") + (x.tx_off_after_ms ? ` (after ${x.tx_off_after_ms} ms)` : "") : h("span", { class: "muted" }, "none")),
            h("div", null, "Settle time"), h("div", null, x.dwell_ms + " ms"),
            h("div", null, "Automatic checks"), h("div", null, (x.checks || []).length ? x.checks.map((c) => h("div", { class: "mono" }, c.type === "serial" ? `serial ~ /${c.regex}/` : `${c.signal} ${c.op} ${c.value}`, h("span", { class: "faint" }, `  within ${c.timeout_ms || 2000} ms`))) : h("span", { class: "muted" }, "none — the run pauses for your verdict")),
            h("div", null, "Verdict policy"), h("div", { class: "muted" }, (x.checks || []).length ? "Passes only if every check succeeds in time, otherwise fails." : "Never passes by itself: you decide Pass / Fail / Skip.")),
          h("div", null, h("div", { class: "faint", style: { fontSize: "11px", textTransform: "uppercase", letterSpacing: ".6px", margin: "6px 0" } }, "History"),
            hist.length ? h("table", { class: "t" }, h("tbody", null, hist.map((r) => h("tr", null, h("td", { class: "mono" }, fmtDT(r.ts)), h("td", null, h("span", { class: "chip " + r.verdict }, r.verdict)), h("td", { class: "muted" }, r.mode), h("td", null, r.note || ""))))) : h("div", { class: "muted" }, "Not run yet."))));
    }
    CB.views.scenarios.drawList = drawList; CB.views.scenarios.drawDetail = drawDetail;
    drawList(); drawDetail();
  },
  onResult() { const v = CB.views.scenarios; if (v.drawList) { v.drawList(); v.drawDetail(); } },
};
async function runIds(ids) {
  if (!ids.length) return toast("Nothing to run.", "warn");
  if (ids.length > 25 && !(await ask({ title: "Run many scenarios", body: `Run ${ids.length} scenarios in sequence?`, yes: "Run them" }))) return;
  try { await api("/api/run", { ids, manual: (CB.scen || {}).manual || "prompt" }); } catch (e) { /* toast shown */ }
}

/* ---------- editor ---------- */
function editScenarioDialog(id) {
  const s = st(), sc = id ? CB.idx.scen[id] : null;
  const sims = s.model.messages.filter((m) => m.role === "sim" && !m.placeholder), listens = s.model.messages.filter((m) => m.role === "listen");
  const T = (v, ph) => h("input", { type: "text", value: v || "", placeholder: ph || "" });
  const title = T(sc && sc.title, "Scenario title"), group = T(sc ? sc.group : "Custom");
  const expect = h("textarea", { rows: 3, placeholder: "What should the DUT do?" }, sc ? sc.expect || "" : "");
  const dwell = h("input", { type: "number", value: sc ? sc.dwell_ms : 1500 }), after = h("input", { type: "number", value: sc ? sc.tx_off_after_ms : 0 });
  const confirmBox = h("input", { type: "checkbox", checked: !!(sc && sc.confirm) });
  const itemOpts = (msgs) => msgs.map((m) => h("optgroup", { label: m.name }, m.items.map((it) => h("option", { value: it.uid }, it.name))));

  /* stimulus rows */
  const setBox = h("div"), setRows = [];
  const valueInput = (uid, v) => {
    const it = CB.idx.items[uid]; if (!it) return h("input", { type: "number", value: v });
    if (it.kind === "bool") return h("select", null, h("option", { value: 0, selected: !v }, it.choices["0"] || "0 · off"), h("option", { value: 1, selected: !!v }, it.choices["1"] || "1 · on"));
    if (it.kind === "enum") return h("select", null, Object.entries(it.choices).map(([k, l]) => h("option", { value: k, selected: +k === +v }, `${k} · ${l}`)));
    return h("input", { type: "number", value: v ?? it.default, step: it.scale > 0 ? it.scale : "any", title: it.unit });
  };
  const addSet = (a) => {
    const sel = h("select", { style: { flex: 1 } }, itemOpts(sims)); if (a) sel.value = a.uid;
    let vi = valueInput(sel.value, a ? a.value : 0); const slot = h("span", null, vi);
    sel.onchange = () => { vi = valueInput(sel.value, 0); slot.innerHTML = ""; slot.appendChild(vi); row.vi = vi; };
    const row = h("div", { class: "subrow" }, sel, slot, h("button", { class: "btn ghost icon sm", onclick: () => { row.remove(); setRows.splice(setRows.indexOf(rec), 1); } }, icon("x")));
    const rec = { sel, get vi() { return vi; } }; setRows.push(rec); setBox.appendChild(row);
  };
  (sc ? sc.set : []).forEach(addSet);

  /* frames stopped */
  const offAll = h("input", { type: "checkbox", checked: sc && sc.tx_off === "ALL" });
  const offBoxes = sims.map((m) => ({ m, cb: h("input", { type: "checkbox", checked: sc && Array.isArray(sc.tx_off) && sc.tx_off.includes(m.name) }) }));

  /* checks */
  const chkBox = h("div"), chkRows = [];
  const addCheck = (c) => {
    c = c || { type: listens.length ? "can" : "serial", op: "==", value: 1, timeout_ms: 2000 };
    const type = h("select", null, h("option", { value: "can", selected: c.type === "can" }, "CAN signal"), h("option", { value: "serial", selected: c.type === "serial" }, "Serial text"));
    const sig = h("select", { style: { flex: 1 } }, itemOpts(listens.length ? listens : s.model.messages)); if (c.signal) sig.value = c.signal;
    const op = h("select", null, ["==", "!=", ">", ">=", "<", "<="].map((o) => h("option", { value: o, selected: c.op === o }, o)));
    const val = h("input", { type: "number", value: c.value ?? 1, step: "any" });
    const rx = h("input", { type: "text", value: c.regex || "", placeholder: "regular expression, e.g. SOC:\\s*80", style: { flex: 1 } });
    const to = h("input", { type: "number", value: c.timeout_ms || 2000, style: { width: "80px" }, title: "timeout ms" });
    const can = h("span", { class: "row", style: { flex: 1 } }, sig, op, val), ser = h("span", { class: "row", style: { flex: 1 } }, rx);
    const sync = () => { can.classList.toggle("hidden", type.value !== "can"); ser.classList.toggle("hidden", type.value !== "serial"); }; type.onchange = sync; sync();
    const row = h("div", { class: "subrow" }, type, can, ser, to, h("span", { class: "faint" }, "ms"), h("button", { class: "btn ghost icon sm", onclick: () => { row.remove(); chkRows.splice(chkRows.indexOf(rec), 1); } }, icon("x")));
    const rec = { type, sig, op, val, rx, to }; chkRows.push(rec); chkBox.appendChild(row);
  };
  (sc ? sc.checks || [] : []).forEach(addCheck);

  const F = (l, n, hint) => h("div", { class: "field" }, h("label", null, l), n, hint ? h("div", { class: "faint", style: { fontSize: "12px", marginTop: "3px" } }, hint) : null);
  modal({ title: sc ? "Edit scenario" : "New scenario", width: "820px", body: [
    h("div", { class: "grid2" }, F("Title", title), F("Group", group)), F("Expected behaviour", expect),
    F("Stimulus — signals to drive", h("div", null, setBox, h("button", { class: "btn sm", onclick: () => addSet(null), disabled: !sims.length }, icon("plus"), "Add signal"))),
    F("Frames to stop", h("div", { class: "row wrap" }, h("label", { class: "row" }, offAll, "All frames"), offBoxes.map(({ m, cb }) => h("label", { class: "row muted" }, cb, m.name))), "Simulates message loss; the DUT should time out."),
    h("div", { class: "grid2" }, F("Settle time (ms)", dwell, "Wait after applying the stimulus before judging."), F("Stop frames after (ms)", after, "0 = immediately. Use a delay to test loss while an error is active.")),
    F("Automatic checks (optional)", h("div", null, chkBox, h("button", { class: "btn sm", onclick: () => addCheck(null) }, icon("plus"), "Add check")), "With at least one check the bench decides Pass/Fail itself: every check must succeed within its timeout. CAN checks read the DUT's output frames (role: Listen); serial checks read the debug UART."),
    h("label", { class: "row muted" }, confirmBox, "Also ask me to confirm, even when automatic checks pass")],
  buttons: [{ label: "Save", kind: "primary", onClick: async () => {
    if (!title.value.trim()) { toast("Add a title.", "warn"); return false; }
    const body = { id: sc ? sc.id : undefined, source: sc ? sc.source : "custom", title: title.value, group: group.value, expect: expect.value, dwell_ms: +dwell.value, tx_off_after_ms: +after.value, confirm: confirmBox.checked,
      set: setRows.map((r) => ({ uid: r.sel.value, value: +r.vi.value })),
      tx_off: offAll.checked ? "ALL" : offBoxes.filter((o) => o.cb.checked).map((o) => o.m.name),
      checks: chkRows.map((r) => r.type.value === "serial" ? { type: "serial", regex: r.rx.value, timeout_ms: +r.to.value } : { type: "can", signal: r.sig.value, op: r.op.value, value: +r.val.value, timeout_ms: +r.to.value }) };
    const r = await api("/api/scenario/save", body); await refresh(); if (CB.scen) CB.scen.sel = r.scenario.id; if (CB.S.view !== "scenarios") go("scenarios");
  } }] });
}
