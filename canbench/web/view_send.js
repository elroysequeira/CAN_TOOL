/* Send view: put ANY CAN frame on the bus by hand - once, or repeating for a while. Independent of the DBC and of "Start TX". */
"use strict";
CB.views.send = {
  render(el) {
    const s = st(), L0 = live();
    const f = (CB.snd = CB.snd || { name: "", id: "7DF", ext: "auto", data: "02 01 0C", period: 100, duration: 0, count: 0 });
    el.appendChild(h("div", { class: "page-h" }, h("h2", null, "Send a frame"),
      h("span", { class: "sub" }, "Any CAN ID, any bytes — once or repeating for a set time. No DBC needed; independent of “Start TX”.")));

    const banner = h("div"), left = h("div", { class: "stack" }), right = h("div", { class: "stack" });
    el.append(banner, h("div", { class: "send-grid" }, left, right));
    const drawBanner = () => {
      const L = live(), c = L.conn || {}; banner.innerHTML = "";
      const msg = !c.connected ? "Not connected — press Connect in the top bar first." : (CB.udsRun && CB.udsRun.running) ? "A firmware update is running — sending by hand is blocked until it finishes." : "";
      if (msg) banner.appendChild(h("div", { class: "banner", style: { marginBottom: "14px" } }, h("div", { class: "grow" }, msg)));
    };

    /* ---- the form ---- */
    const bind = (key, type, opts = {}) => {
      const i = h("input", { type: type || "text", value: f[key], style: { width: opts.w || "100%" }, placeholder: opts.ph || "", min: 0, step: opts.step || 1,
        oninput: (e) => { f[key] = type === "number" ? (e.target.value === "" ? 0 : +e.target.value) : e.target.value; refreshBtns(); } });
      return h("div", { class: "field" }, h("label", null, opts.label), i, opts.hint ? h("div", { class: "faint", style: { fontSize: "12px", marginTop: "3px" } }, opts.hint) : null);
    };
    const extSel = h("select", { onchange: (e) => { f.ext = e.target.value; } },
      h("option", { value: "auto" }, "auto (29-bit if above 0x7FF)"), h("option", { value: "0" }, "11-bit (standard)"), h("option", { value: "1" }, "29-bit (extended)"));
    extSel.value = String(f.ext);
    const dbcSel = h("select", { title: "Fill the ID and data length from a message of your database" },
      h("option", { value: "" }, "Load from the database…"), ((s.model && s.model.messages) || []).map((m) => h("option", { value: m.name }, `${m.name}  (0x${m.id_hex})`)));
    dbcSel.onchange = () => {
      const m = ((s.model && s.model.messages) || []).find((x) => x.name === dbcSel.value); if (!m) return;
      f.id = m.id_hex.replace(/^0x/i, ""); f.ext = m.ext ? "1" : "0"; f.name = f.name || m.name;
      f.data = Array.from({ length: m.dlc || 8 }, () => "00").join(" "); go("send");
    };
    const bytesInfo = h("span", { class: "faint" });
    const sendOnce = h("button", { class: "btn primary", onclick: () => doSend(true) }, icon("send"), "Send once");
    const sendRep = h("button", { class: "btn", onclick: () => doSend(false) }, icon("refresh"), "Send repeating");
    const saveBtn = h("button", { class: "btn", title: "Keep this frame in the project's saved list", onclick: async () => { try { await api("/api/manual/save", payload(false)); await refresh(); toast("Saved.", "ok"); } catch (e) { /* shown */ } } }, icon("save"), "Save");
    const payload = (once) => ({ name: f.name, id: f.id, ext: f.ext === "auto" ? "auto" : f.ext === "1", data: f.data, period_ms: once ? 0 : f.period, duration_s: once ? 0 : f.duration, count: once ? 0 : f.count });
    function refreshBtns() {
      const n = (f.data || "").replace(/0x|[\s,;:-]/gi, "");
      bytesInfo.textContent = /^[0-9a-f]*$/i.test(n) ? `${Math.floor(n.length / 2)} byte(s)` + (n.length % 2 ? " — odd number of hex digits" : "") : "not hex";
      bytesInfo.style.color = /^[0-9a-f]*$/i.test(n) && n.length % 2 === 0 && n.length <= 16 ? "" : "var(--warn)";
      sendRep.textContent = f.period > 0 ? `Send every ${f.period} ms` + (f.duration ? ` for ${f.duration} s` : f.count ? `, ${f.count} frames` : "") : "Send repeating";
      sendRep.disabled = !(f.period > 0);
    }
    async function doSend(once) {
      try {
        const r = await api("/api/manual/send", payload(once));
        toast(r.once ? "Frame sent." : "Repeating frame started.", "ok", 2500);
      } catch (e) { /* api() showed why */ }
    }
    left.appendChild(h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", null, "Frame")),
      h("div", { class: "card-b stack" },
        h("div", { class: "grid2" }, bind("name", "text", { label: "Name (optional)", ph: "e.g. wake-up request" }), h("div", { class: "field" }, h("label", null, "From your database"), dbcSel)),
        h("div", { class: "grid2" }, bind("id", "text", { label: "CAN ID (hex)", ph: "7DF or 18FEF100" }), h("div", { class: "field" }, h("label", null, "ID format"), extSel)),
        h("div", { class: "field" }, h("label", null, "Data bytes (hex, up to 8)"), h("input", { type: "text", class: "mono", value: f.data, style: { width: "100%" }, placeholder: "02 01 0C 00 00 00 00 00",
          oninput: (e) => { f.data = e.target.value; refreshBtns(); } }), bytesInfo),
        h("div", { class: "row wrap", style: { alignItems: "flex-end" } },
          bind("period", "number", { label: "Period (ms)", w: "110px", hint: "0 = send once" }), bind("duration", "number", { label: "Duration (s)", w: "110px", hint: "0 = until stopped" }),
          bind("count", "number", { label: "Max frames", w: "110px", hint: "0 = no limit" })),
        h("div", { class: "row wrap" }, sendOnce, sendRep, h("span", { class: "grow" }), saveBtn),
        h("div", { class: "faint", style: { fontSize: "12px" } }, "A repeating frame stops by itself when the duration or frame count is reached, or when you press Stop, E-STOP or Disconnect. " +
          "Sending the same ID again replaces the earlier repeat. Every frame also appears in the Trace and in the bus log."))));

    /* ---- saved ---- */
    const savedBox = h("div", { class: "stack", style: { gap: "6px" } });
    const drawSaved = () => {
      savedBox.innerHTML = ""; const list = (st().manual_frames || []);
      if (!list.length) { savedBox.appendChild(h("div", { class: "muted" }, "Nothing saved yet. Fill the form and press Save to keep a frame you send often.")); return; }
      list.forEach((x) => savedBox.appendChild(h("div", { class: "row" },
        h("b", null, x.name), h("span", { class: "chip accent mono" }, "0x" + x.id.toUpperCase()), h("span", { class: "mono faint", style: { overflow: "hidden", textOverflow: "ellipsis" } }, x.data || "(no data)"),
        h("span", { class: "faint" }, x.period_ms ? `every ${x.period_ms} ms` + (x.duration_s ? ` · ${x.duration_s} s` : "") : "once"), h("span", { class: "grow" }),
        h("button", { class: "btn sm", onclick: () => { Object.assign(f, { name: x.name, id: x.id, ext: x.ext ? "1" : "auto", data: x.data, period: x.period_ms, duration: x.duration_s, count: x.count }); go("send"); } }, "Load"),
        h("button", { class: "btn primary sm", onclick: async () => { try { await api("/api/manual/send", { name: x.name, id: x.id, ext: x.ext, data: x.data, period_ms: x.period_ms, duration_s: x.duration_s, count: x.count }); toast(x.period_ms ? "Repeating frame started." : "Frame sent.", "ok", 2000); } catch (e) { /* shown */ } } }, "Send"),
        h("button", { class: "btn ghost icon sm", title: "Delete", onclick: async () => { await api("/api/manual/delete", { name: x.name }); await refresh(); } }, icon("trash")))));
    };
    left.appendChild(h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", null, "Saved frames")), h("div", { class: "card-b" }, savedBox)));

    /* ---- running ---- */
    const runBox = h("div", { class: "stack", style: { gap: "8px" } });
    const stopAll = h("button", { class: "btn bad sm", onclick: () => api("/api/manual/stop", {}) }, icon("stop"), "Stop all");
    const clearBtn = h("button", { class: "btn sm", onclick: () => api("/api/manual/clear", {}) }, "Clear finished");
    right.appendChild(h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", null, "Repeating frames"), h("span", { class: "grow" }), clearBtn, stopAll), h("div", { class: "card-b" }, runBox)));
    let key = "";
    const drawRunning = (list) => {
      const k = JSON.stringify(list.map((m) => [m.key, m.running, m.why]));
      if (k !== key) { key = k; runBox.innerHTML = ""; runBox._cells = {};
        if (!list.length) runBox.appendChild(h("div", { class: "muted" }, "No repeating frame. Use “Send repeating” (set a period) to start one."));
        list.forEach((m) => {
          const stat = h("span", { class: "chip" }), cnt = h("span", { class: "mono faint" }), left_ = h("span", { class: "faint" });
          runBox._cells[m.key] = { stat, cnt, left_ };
          runBox.appendChild(h("div", { class: "row wrap", style: { padding: "6px 0", borderBottom: "1px solid var(--line)" } },
            h("span", { class: "chip accent mono" }, "0x" + m.id_hex), m.ext ? h("span", { class: "chip" }, "EXT") : null, m.label ? h("b", null, m.label) : null,
            h("span", { class: "mono" }, m.data.map((b) => b.toString(16).toUpperCase().padStart(2, "0")).join(" ") || "(no data)"), h("span", { class: "faint" }, `every ${m.period_ms} ms`),
            h("span", { class: "grow" }), left_, cnt, stat,
            m.running ? h("button", { class: "btn sm", onclick: () => api("/api/manual/stop", { key: m.key }) }, "Stop") : h("button", { class: "btn ghost icon sm", title: "Remove", onclick: () => api("/api/manual/stop", { key: m.key }) }, icon("x"))));
        }); }
      list.forEach((m) => { const c = runBox._cells && runBox._cells[m.key]; if (!c) return;
        c.cnt.textContent = m.sent + " sent" + (m.max_count ? " / " + m.max_count : ""); c.left_.textContent = m.running && m.left_s != null ? m.left_s + " s left" : "";
        c.stat.className = "chip " + (m.running ? "ok" : m.why === "error" ? "bad" : ""); c.stat.textContent = m.running ? "sending" : m.why === "error" ? "error: " + m.error : m.why || "stopped"; });
      stopAll.disabled = !list.some((m) => m.running); clearBtn.disabled = !list.some((m) => !m.running);
    };
    CB.views.send.onLive = (d) => { drawBanner(); drawRunning(d.manual || []); };
    drawBanner(); drawSaved(); refreshBtns(); drawRunning(L0.manual || []);
  },
  onLive: null,
};
