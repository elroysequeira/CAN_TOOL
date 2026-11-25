/* Firmware (UDS) view, unknown-frame card and the frame designer */
"use strict";

/* =========================================================================
   Frames not described by any DBC  +  frame designer
   ========================================================================= */
function sigBitsJS(start, len, little) {
  const out = []; let p = start;
  for (let i = 0; i < len; i++) { out.push(little ? start + i : p); if (!little) p = p % 8 === 0 ? p + 15 : p - 1; }
  return out;
}
function extractJS(bytes, s) {
  const bits = sigBitsJS(s.start, s.length, s.little), bit = (p) => { const b = bytes[p >> 3]; return b === undefined ? 0 : (b >> (p & 7)) & 1; };
  let v = 0;
  if (s.little) bits.forEach((p, i) => { v += bit(p) * 2 ** i; }); else bits.forEach((p) => { v = v * 2 + bit(p); });
  if (s.signed && s.length > 1 && (s.little ? bit(bits[bits.length - 1]) : bit(bits[0]))) v -= 2 ** s.length;
  return v * s.scale + s.offset;
}

function buildUnknownCard() {
  const list = h("div", { class: "stack", style: { gap: "10px" } }), defd = h("div", { class: "stack", style: { gap: "6px", marginTop: "6px" } });
  const count = h("span", { class: "chip" }, "0");
  const pid = () => ((st() && st().project) || {}).id || "";
  const hiddenKey = () => "unkHidden:" + pid();
  const isHidden = (k) => (pref.get(hiddenKey(), []) || []).includes(k);
  const setHidden = (k, on) => { const a = new Set(pref.get(hiddenKey(), []) || []); on ? a.add(k) : a.delete(k); pref.set(hiddenKey(), [...a]); };
  let open = pref.get("unkOpen", true), showHidden = false, last = [];
  const body = h("div", { class: "card-b" }, list, defd);
  const summary = h("div", { class: "card-b muted hidden" });
  const hiddenBtn = h("button", { class: "btn ghost sm hidden", onclick: () => { showHidden = !showHidden; update(last); } });
  const chev = h("button", { class: "btn ghost sm icon", title: "Minimise / expand this list", onclick: () => { open = !open; pref.set("unkOpen", open); paint(); } }, icon("chev"));
  const el = h("div", { class: "card unk-card" }, h("div", { class: "card-h" }, h("h3", null, "Frames not in your DBC"), count, h("span", { class: "grow" }), hiddenBtn, chev), summary, body);
  const rows = {};
  const paint = () => {
    body.classList.toggle("hidden", !open); summary.classList.toggle("hidden", open);
    chev.style.transform = open ? "" : "rotate(-90deg)";
    const vis = last.filter((u) => !isHidden(u.id + ":" + u.ext)).length;
    summary.textContent = last.length ? `${vis} unknown frame${vis === 1 ? "" : "s"} on the bus` + (last.length > vis ? ` (${last.length - vis} hidden)` : "") + " — expand to see them." : "No unknown frames on the bus.";
  };
  const drawDefined = () => {
    defd.innerHTML = "";
    const cf = (st() && st().custom_frames) || [];
    if (!cf.length) return;
    defd.appendChild(h("div", { class: "faint", style: { fontSize: "11px", textTransform: "uppercase", letterSpacing: ".6px" } }, "Defined by you"));
    cf.forEach((f) => defd.appendChild(h("div", { class: "row wrap" }, h("span", { class: "chip accent mono" }, "0x" + f.id.toString(16).toUpperCase()), h("b", null, f.name), h("span", { class: "faint" }, f.signals.length + " signals"), h("span", { class: "grow" }),
      h("button", { class: "btn sm", onclick: () => defineFrameDialog({ id: f.id, ext: f.ext, dlc: f.dlc, period_ms: f.period_ms, name: f.name, signals: f.signals, getData: () => ((live().rx[f.name] || {}).data || []), getChanged: () => [] }) }, icon("edit"), "Edit"),
      h("button", { class: "btn bad sm icon", title: "Delete definition", onclick: async () => { if (await ask({ title: "Delete definition", body: `Delete the definition of ${f.name}? The frame goes back to “not in your DBC”.`, yes: "Delete", danger: true })) { await api("/api/frame/delete", { name: f.name }); await refresh(); } } }, icon("trash")))));
  };
  function update(unk) {
    last = unk;
    const L = live(), connected = (L.conn || {}).connected;
    const hidden = unk.filter((u) => isHidden(u.id + ":" + u.ext)).length;
    count.textContent = hidden ? `${unk.length - hidden} of ${unk.length}` : String(unk.length);
    hiddenBtn.classList.toggle("hidden", !hidden);
    hiddenBtn.textContent = showHidden ? "Fold hidden" : `Show ${hidden} hidden`;
    const keep = new Set();
    const msg = connected ? "No unknown frames on the bus." : "Connect to the CAN interface to see what the DUT sends.";
    if (!unk.length) {
      if (!list.firstChild || list.firstChild.className !== "muted") { list.innerHTML = ""; Object.keys(rows).forEach((k) => delete rows[k]); list.appendChild(h("div", { class: "muted" }, msg)); }
      else list.firstChild.textContent = msg;
    } else if (list.firstChild && list.firstChild.className === "muted") list.innerHTML = "";
    for (const u of unk) {
      const k = u.id + ":" + u.ext; keep.add(k);
      let r = rows[k];
      if (!r) {
        const bytes = h("div", { class: "bytes" }), cells = [];
        for (let i = 0; i < 8; i++) { const c = h("span", { class: "byte" }, "--"); cells.push(c); bytes.appendChild(c); }
        const meta = h("div", { class: "faint unk-meta" });
        const hideBtn = h("button", { class: "btn ghost sm", title: "Not important: hide this frame from the list (you can bring it back)", onclick: () => { setHidden(k, !isHidden(k)); update(last); paint(); } });
        const row = h("div", { class: "unk" }, h("div", { class: "row wrap" }, h("span", { class: "chip accent mono" }, "0x" + u.id_hex), u.ext ? h("span", { class: "chip" }, "EXT") : null, h("span", { class: "grow" }), hideBtn,
          h("button", { class: "btn primary sm", onclick: () => { const cur = (live().unknown || []).find((x) => x.id === u.id && x.ext === u.ext) || u; defineFrameDialog({ id: u.id, ext: u.ext, dlc: cur.dlc, period_ms: Math.round((cur.period_ms || 0) / 10) * 10, name: "DUT_0x" + u.id_hex, signals: [], getData: () => ((live().unknown || []).find((x) => x.id === u.id && x.ext === u.ext) || {}).data || [], getChanged: () => ((live().unknown || []).find((x) => x.id === u.id && x.ext === u.ext) || {}).changed || [] }); } }, icon("plus"), "Define signals")), meta, bytes);
        list.appendChild(row); r = rows[k] = { row, cells, meta, hideBtn };
      }
      const hid = isHidden(k);
      r.row.classList.toggle("hidden", hid && !showHidden);
      r.row.classList.toggle("dimmed", hid);
      r.hideBtn.textContent = hid ? "Unhide" : "Hide";
      r.meta.textContent = `${u.dlc} B · ${u.period_ms ? "every " + u.period_ms + " ms" : "–"} · ${u.count} seen`;
      r.cells.forEach((c, i) => {
        if (i < u.data.length) { c.textContent = u.data[i].toString(16).toUpperCase().padStart(2, "0"); c.classList.toggle("chg", u.changed[i] !== null && u.changed[i] < 3); c.classList.remove("na"); } else { c.textContent = "--"; c.classList.add("na"); }
      });
    }
    for (const k of Object.keys(rows)) if (!keep.has(k)) { rows[k].row.remove(); delete rows[k]; }
    paint();
  }
  drawDefined(); paint();
  return { el, update };
}

function defineFrameDialog(src) {
  const PAL = (i) => `hsl(${(i * 67 + 200) % 360} 65% 42% / .55)`;
  const sigs = (src.signals || []).map((s) => ({ ...s }));
  const name = h("input", { type: "text", value: src.name, style: { width: "100%" } });
  const dlc = h("input", { type: "number", value: src.dlc || 8, min: 1, max: 8, style: { width: "70px" } });
  const per = h("input", { type: "number", value: src.period_ms || 0, min: 0, step: 10, style: { width: "90px" } });
  const mapBox = h("div", { class: "bitmap" }), tbody = h("div", { class: "stack", style: { gap: "8px" } }), vals = {};
  const bitCells = [];
  const cover = () => { const m = {}; sigs.forEach((s, i) => { try { sigBitsJS(+s.start, +s.length, s.little !== false).forEach((p) => { m[p] = i; }); } catch (e) { /* partial input */ } }); return m; };
  const drawMap = () => {
    mapBox.innerHTML = ""; bitCells.length = 0; const cov = cover(), n = Math.max(1, Math.min(8, +dlc.value || 8));
    const head = h("div", { class: "bm-row bm-head" }, h("span", null, ""), [7, 6, 5, 4, 3, 2, 1, 0].map((b) => h("span", null, b)));
    mapBox.appendChild(head);
    for (let by = 0; by < n; by++) {
      const row = h("div", { class: "bm-row" }, h("span", { class: "bm-lbl" }, "B" + by));
      for (let b = 7; b >= 0; b--) { const p = by * 8 + b, c = h("span", { class: "bm-bit", title: "bit " + p }, "·"); if (cov[p] !== undefined) c.style.background = PAL(cov[p]); bitCells.push({ p, c }); row.appendChild(c); }
      mapBox.appendChild(row);
    }
  };
  const addRow = (s) => {
    s = s || { name: "Signal" + (sigs.length + 1), start: 0, length: 8, little: true, signed: false, scale: 1, offset: 0, unit: "" };
    if (!sigs.includes(s)) sigs.push(s);
    const num = (k, w, step) => h("input", { type: "number", value: s[k], step: step || 1, style: { width: w + "px" }, oninput: (e) => { s[k] = e.target.value === "" ? 0 : +e.target.value; drawMap(); } });
    const live_ = h("span", { class: "mono val-pill" }, "–"); vals[sigs.indexOf(s)] = live_;
    const row = h("div", { class: "sigdef" },
      h("input", { type: "text", value: s.name, style: { width: "130px" }, oninput: (e) => { s.name = e.target.value; } }),
      h("label", { class: "faint" }, "bit"), num("start", 56), h("label", { class: "faint" }, "len"), num("length", 52),
      h("select", { onchange: (e) => { s.little = e.target.value === "i"; drawMap(); } }, h("option", { value: "i", selected: s.little !== false }, "Intel"), h("option", { value: "m", selected: s.little === false }, "Motorola")),
      h("label", { class: "row faint" }, h("input", { type: "checkbox", checked: !!s.signed, onchange: (e) => { s.signed = e.target.checked; } }), "signed"),
      h("label", { class: "faint" }, "×"), num("scale", 62, "any"), h("label", { class: "faint" }, "+"), num("offset", 62, "any"),
      h("input", { type: "text", value: s.unit || "", placeholder: "unit", style: { width: "56px" }, oninput: (e) => { s.unit = e.target.value; } }),
      live_, h("button", { class: "btn ghost icon sm", onclick: () => { sigs.splice(sigs.indexOf(s), 1); row.remove(); drawMap(); } }, icon("x")));
    tbody.appendChild(row); drawMap();
  };
  sigs.splice(0).forEach((s) => addRow({ little: true, signed: false, scale: 1, offset: 0, unit: "", ...s }));
  dlc.oninput = drawMap; drawMap();
  const quick = () => { const cov = cover(); for (let by = 0; by < (+dlc.value || 8); by++) if (cov[by * 8] === undefined) addRow({ name: "Byte" + by, start: by * 8, length: 8, little: true, signed: false, scale: 1, offset: 0, unit: "" }); };
  const m = modal({ title: `Describe frame 0x${src.id.toString(16).toUpperCase()}`, width: "1040px", body: [
    h("div", { class: "muted" }, "Watch the bits while you change something on the DUT (or the bench). Bits that just changed are outlined. Give them signal definitions — the frame becomes a normal DBC message the bench can decode, check in scenarios and report on."),
    h("div", { class: "row wrap" }, h("div", { class: "field", style: { flex: 1 } }, h("label", null, "Message name"), name), h("div", { class: "field" }, h("label", null, "DLC"), dlc), h("div", { class: "field" }, h("label", null, "Period ms"), per)),
    h("div", { class: "fd-grid" }, h("div", null, h("div", { class: "faint", style: { fontSize: "11px", textTransform: "uppercase", letterSpacing: ".6px", marginBottom: "6px" } }, "Live bits"), mapBox),
      h("div", null, h("div", { class: "row", style: { marginBottom: "8px" } }, h("div", { class: "faint", style: { fontSize: "11px", textTransform: "uppercase", letterSpacing: ".6px" } }, "Signals"), h("span", { class: "grow" }),
        h("button", { class: "btn sm", onclick: quick }, "One signal per free byte"), h("button", { class: "btn sm", onclick: () => addRow() }, icon("plus"), "Add signal")), tbody))],
  buttons: [{ label: "Save definition", kind: "primary", onClick: async () => {
    try {
      await api("/api/frame/define", { name: name.value.trim(), id: src.id, ext: src.ext, dlc: +dlc.value, period_ms: +per.value, signals: sigs.map((s) => ({ ...s, start: +s.start, length: +s.length })) });
    } catch (e) { return false; }
    await refresh(); toast("Frame defined — it is now a Listen message.", "ok");
  } }] });
  const timer = setInterval(() => {
    if (!document.body.contains(m.el)) return clearInterval(timer);
    const data = src.getData() || [], ch = (src.getChanged && src.getChanged()) || [];
    bitCells.forEach(({ p, c }) => { const by = data[p >> 3]; c.textContent = by === undefined ? "·" : (by >> (p & 7)) & 1; const a = ch[p >> 3]; c.classList.toggle("chg", a !== null && a !== undefined && a < 3); });
    sigs.forEach((s, i) => { if (vals[i]) { try { vals[i].textContent = data.length ? fmtNum(+extractJS(data, { ...s, start: +s.start, length: +s.length, little: s.little !== false, scale: +s.scale || 1, offset: +s.offset || 0 }).toFixed(4)) : "–"; } catch (e) { vals[i].textContent = "–"; } } });
  }, 150);
}

/* =========================================================================
   Firmware (UDS)
   ========================================================================= */
CB.udsLog = []; CB.udsRun = { running: false, steps: {}, pct: 0, text: "", summary: "", ok: null };
const FW = { sel: null, info: null, ident: null, settings: null, recovery: false };

CB.views.firmware = {
  render(el) {
    const U = st().uds, s = (FW.settings = FW.settings || { ...U.settings });
    for (const k of Object.keys(U.settings)) if (!(k in s)) s[k] = U.settings[k];
    const files = U.files;
    if (!files.find((f) => f.name === FW.sel)) { FW.sel = files.length ? files[0].name : null; FW.info = null; }
    const save = debounce(() => api("/api/uds/settings", s), 400);
    const field = (label, key, opts = {}) => {
      const inp = opts.options ? h("select", { onchange: (e) => { s[key] = e.target.value; save(); pre(); } }, opts.options.map(([v, l]) => h("option", { value: v, selected: s[key] === v }, l)))
        : opts.bool ? h("input", { type: "checkbox", checked: !!s[key], onchange: (e) => { s[key] = e.target.checked; save(); pre(); } })
          : h("input", { type: opts.number ? "number" : "text", value: s[key], placeholder: opts.ph || "", style: { width: "100%" }, oninput: (e) => { s[key] = opts.number ? +e.target.value : e.target.value; save(); if (key === "product_keyword") inspect(); else pre(); } });
      return h("div", { class: "field" }, h("label", null, label), opts.bool ? h("label", { class: "row muted" }, inp, opts.text || "") : inp, opts.hint ? h("div", { class: "faint", style: { fontSize: "12px", marginTop: "3px" } }, opts.hint) : null);
    };

    el.appendChild(h("div", { class: "page-h" }, h("h2", null, "Firmware"), h("span", { class: "sub" }, "Read identity and update the DUT over UDS (ISO 14229) through the connected CAN interface")));
    const left = h("div", { class: "stack" }), right = h("div", { class: "stack" });
    const rc = U.recovery;
    if (rc && !CB.udsRun.running) {
      const reflash = rc.level === "reflash", when = fmtDT(rc.ts);
      el.appendChild(h("div", { class: "banner " + (reflash ? "bad" : ""), style: { marginBottom: "14px" } },
        h("div", { class: "grow" },
          h("b", null, reflash ? "The last firmware update did not finish — the ECU may not start" : "The last firmware update stopped after the image was written"),
          h("div", { class: "muted" }, `${rc.image} · ${when} · ${(rc.summary || "").split(". ")[0]}.`),
          h("div", { style: { marginTop: "4px" } }, reflash
            ? "Its application was erased and not completely rewritten. Check the power and the CAN wiring, then re-flash the same image with Recovery mode (it does not need the ECU to report an identity)."
            : "Power-cycle the ECU, press “Read identity” and compare it with the expected version. Re-flash only if it is wrong.")),
        h("div", { class: "row" },
          reflash ? h("button", { class: "btn primary", onclick: () => { FW.recovery = true; if (U.files.find((f) => f.name === rc.image)) { FW.sel = rc.image; FW.info = null; } go("firmware"); setTimeout(() => { const t = document.getElementById("fw-update-card"); if (t) t.scrollIntoView({ behavior: "smooth", block: "center" }); }, 80); } }, icon("refresh"), "Prepare recovery re-flash") : null,
          h("button", { class: "btn", onclick: async () => { await api("/api/uds/recovery/dismiss", {}); } }, "Dismiss"))));
    }
    el.appendChild(h("div", { class: "fw-wrap" }, left, right));

    /* identity */
    const identBox = h("div", { class: "stack", style: { gap: "6px" } });
    const drawIdent = () => {
      identBox.innerHTML = "";
      if (!FW.ident) { identBox.appendChild(h("div", { class: "muted" }, "Not read yet. Read-only: sends service 0x22 (ReadDataByIdentifier).")); return; }
      FW.ident.items.forEach((i) => identBox.appendChild(h("div", { class: "fb-row" }, h("span", null, i.name), i.error ? h("span", { class: "chip warn", title: i.error }, "no value") : h("b", null, i.text || i.hex))));
      if (!FW.ident.answered) identBox.appendChild(h("div", { class: "chip error" }, "No answer — check the IDs, bitrate and that the ECU is in a state that accepts diagnostics."));
    };
    const readBtn = h("button", { class: "btn primary", onclick: async () => { readBtn.disabled = true; readBtn.textContent = "Reading…"; try { FW.ident = await api("/api/uds/identity", {}); } catch (e) { FW.ident = null; } readBtn.disabled = false; readBtn.textContent = "Read identity"; drawIdent(); pre(); } }, "Read identity");
    left.appendChild(h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", null, "1 · ECU"), h("span", { class: "grow" }), readBtn),
      h("div", { class: "card-b stack" }, identBox, h("div", { class: "grid2" }, field("Tester → ECU (request ID)", "tx_id"), field("ECU → tester (response ID)", "rx_id")),
        h("div", { class: "grid2" }, field("Identity DIDs (hex)", "dids"), field("29-bit IDs", "ext", { bool: true, text: "use extended (29-bit) IDs for diagnostics", hint: "Leave OFF unless your ECU's diagnostic IDs are 29-bit. 0x7E0 / 0x7E8 are 11-bit. (Data frames such as 0x000000A2 in an Excel sheet have nothing to do with this.)" })))));

    /* firmware image */
    const infoBox = h("div", { class: "stack", style: { gap: "8px" } });
    async function inspect() {
      if (!FW.sel) { FW.info = null; drawInfo(); pre(); return; }
      try { FW.info = (await api("/api/uds/inspect", { name: FW.sel })).info; } catch (e) { FW.info = null; }
      drawInfo(); pre();
    }
    const drawInfo = () => {
      infoBox.innerHTML = "";
      const i = FW.info; if (!i) { if (FW.sel) infoBox.appendChild(h("div", { class: "muted" }, "Reading image…")); return; }
      infoBox.append(h("div", { class: "kv" }, h("div", null, "Size"), h("div", { class: "mono" }, i.size.toLocaleString() + " bytes"), h("div", null, "CRC32"), h("div", { class: "mono" }, i.crc32),
        h("div", null, "SHA-256"), h("div", { class: "mono faint", style: { wordBreak: "break-all" } }, i.sha256.slice(0, 32) + "…"),
        i.start_address ? [h("div", null, "S-record start"), h("div", { class: "mono" }, i.start_address, i.gaps ? h("span", { class: "chip warn", style: { marginLeft: "8px" } }, i.gaps + " gap(s) filled with FF") : null)] : null,
        i.meant_for ? [h("div", null, "Image says"), h("div", null, "meant for ", h("b", null, i.meant_for))] : null, i.versions.length ? [h("div", null, "Versions inside"), h("div", { class: "mono" }, i.versions.join("  "))] : null),
        h("div", { class: "chip " + ({ ok: "ok", mismatch: "bad", unchecked: "warn" }[i.product.status]), style: { alignSelf: "flex-start", whiteSpace: "normal" } }, ({ ok: "Product ✓  ", mismatch: "Product ✗  ", unchecked: "Product unchecked  " }[i.product.status]) + i.product.text));
      (i.warnings || []).forEach((w) => infoBox.appendChild(h("div", { class: "chip warn", style: { alignSelf: "flex-start", whiteSpace: "normal" } }, w)));
      if (i.keyword_source === "image") infoBox.appendChild(h("div", { class: "faint", style: { fontSize: "12px" } }, "Product keyword taken from the image: “" + i.keyword_used + "”."));
      if (i.start_address && !s.address) infoBox.appendChild(h("button", { class: "btn sm", onclick: () => { s.address = i.start_address; save(); go("firmware"); } }, "Use " + i.start_address + " as memory address"));
    };
    const fileList = h("div", { class: "stack", style: { gap: "6px" } });
    const drawFiles = () => {
      fileList.innerHTML = "";
      files.forEach((f) => fileList.appendChild(h("label", { class: "file", style: { cursor: "pointer", borderColor: FW.sel === f.name ? "var(--accent)" : "" } },
        h("input", { type: "radio", name: "fwsel", checked: FW.sel === f.name, onchange: () => { FW.sel = f.name; FW.info = null; drawFiles(); inspect(); } }),
        h("div", { class: "grow" }, h("div", { class: "nm" }, f.name), h("div", { class: "hash" }, Math.round(f.size / 1024) + " KB · sha256 " + f.sha256.slice(0, 12) + "…")),
        h("button", { class: "btn ghost icon sm", title: "Remove", onclick: async (e) => { e.preventDefault(); await api("/api/file/remove", { name: f.name }); await refresh(); } }, icon("trash")))));
    };
    const inp = h("input", { type: "file", multiple: true, accept: ".bin,.srec,.s19", class: "hidden", onchange: () => up(inp.files) });
    const zone = h("div", { class: "drop", onclick: () => inp.click() }, icon("upload"), h("div", { style: { fontWeight: 650, color: "var(--text)" } }, "Drop firmware images (.bin / .srec)"), h("div", { class: "faint" }, "stored in the project, hashed for the report"), inp);
    zone.ondragover = (e) => { e.preventDefault(); zone.classList.add("over"); }; zone.ondragleave = () => zone.classList.remove("over");
    zone.ondrop = (e) => { e.preventDefault(); zone.classList.remove("over"); up(e.dataTransfer.files); };
    left.appendChild(h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", null, "2 · Firmware image")), h("div", { class: "card-b stack" }, zone, fileList, infoBox)));
    drawFiles();

    /* parameters */
    const resetBtn = h("button", { class: "btn sm", title: "Put every parameter back to the standard values", onclick: async () => {
      try { await api("/api/uds/defaults", {}); FW.settings = null; toast("Parameters reset to the standard values.", "ok"); await refresh(); } catch (e) { /* shown */ }
    } }, "Reset to defaults");
    left.appendChild(h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", null, "3 · Programming parameters"), h("span", { class: "sub" }, "standard values - change only what your bootloader needs"), h("span", { class: "grow" }), resetBtn),
      h("div", { class: "card-b stack" },
        h("div", { class: "grid2" }, field("Memory address (hex) — required", "address", { ph: "e.g. 0x08020000", hint: "Where the bootloader expects the new application to be written. Default 0x08020000; change it if your bootloader uses another address." }), field("Product keyword (blank = automatic)", "product_keyword", { ph: FW.info && FW.info.keyword_used ? "auto: " + FW.info.keyword_used : "auto: from the image", hint: "Blank: taken from the image (“meant for …”). The ECU's identity must contain it, and an image of another product is refused." })),
        h("div", { class: "grid2" }, field("Erase routine ID", "erase_rid"), field("Checksum routine ID", "check_rid")),
        h("div", { class: "grid2" }, field("Security key algorithm", "key_algo", { options: [["echo", "Echo the seed (default)"], ["invert", "Bitwise invert"], ["xor", "XOR with constant"], ["add", "Add constant"]] }), field("Key constant (hex)", "key_const")),
        h("div", { class: "grid2" }, field("Data per block", "chunk_mode", { options: [["reported", "As reported by the ECU (default)"], ["iso", "Reported − 2 (ISO 14229 strict)"]] }), field("Block counter", "seq_mode", { options: [["orig", "1…15 then 1 (default)"], ["iso", "1…255 then 0 (ISO 14229)"]] })),
        h("div", { class: "grid2" }, field("Wait before re-reading after reset (s)", "reboot_min_s", { number: true }), field("Give up waiting after (s)", "reboot_timeout_s", { number: true })),
        h("div", { class: "grid2" }, field("Expected version afterwards (optional)", "expected_version", { ph: "e.g. V0.0.2", hint: "If empty, success means the reported identity changed." }), field("Allow same version", "allow_same_version", { bool: true, text: "re-flashing the same version is intended" })),
        field("ECU identity check", "require_identity_match", { bool: true, text: "the ECU's reported identity must contain the product keyword" }),
        field("Product mismatch", "allow_product_mismatch", { bool: true, text: "I know this is a different product — skip both product checks" }),
        field("Simulated frames", "keep_tx", { bool: true, text: "Keep simulated frames running during the update", hint: "Normally the bench stops them (a quiet bus is the safest). Tick this only if your ECU needs them to stay in programming mode, for example it faults when the MCU or charger frames disappear. The report notes it. Frames that use the update's own IDs are refused." }))));

    /* update card */
    const preBox = h("div", { class: "stack", style: { gap: "6px" } }), flashBtn = h("button", { class: "btn bad lg", onclick: confirmFlash }, icon("bolt"), "Update firmware…");
    const stageOf = () => { let m = -1; for (const [k, v] of Object.entries(CB.udsRun.steps)) if (v.state === "run" || v.state === "ok") m = Math.max(m, +k); return m; };
    const abortBtn = h("button", { class: "btn", onclick: () => {
      const i = stageOf(), safe = i < 3, late = i >= 8;
      modal({ title: "Abort the firmware update?", width: "560px", body: [
        h("div", { class: "banner " + (safe || late ? "" : "bad") }, h("div", { class: "grow" },
          h("b", null, safe ? "Nothing has been erased yet — aborting is safe." : late ? "The image is already written and checked." : "The ECU's application is erased or being rewritten."),
          safe ? "The ECU stays exactly as it was." : late ? "Aborting only skips the final verification. Power-cycle the ECU and press Read identity afterwards."
            : "If you abort now the ECU will not start until it is re-flashed. Abort only if the power or the CAN bus is failing — otherwise let it finish. CANBench will then offer a Recovery re-flash.")),
        h("div", { class: "muted" }, "The bench stops sending at the next safe point (between two data blocks), writes the stop into the report and keeps the bus log.")],
      buttons: [{ label: "Keep updating", kind: "primary" }, { label: "Abort update", kind: "bad", onClick: async () => { try { await api("/api/uds/abort", {}); toast("Abort requested — stopping at the next safe point.", "warn"); } catch (e) { return false; } } }] });
    } }, "Abort…");
    function checks() {
      const L = live(), c = L.conn || {}, i = FW.info, run = CB.udsRun.running;
      const kw = String(s.product_keyword).trim() || (i && i.keyword_used) || "";
      return [
        [!!c.connected, "CAN interface connected", "Use the Connect button in the top bar (500 kbit/s)."],
        [!c.tx_on || !!s.keep_tx, s.keep_tx ? "Simulated TX keeps running (your choice)" : "Simulated TX stopped", "Press Stop TX. The update needs a quiet bus: simulated frames can delay or corrupt the transfer, and would make the ECU react while it is erasing and rebooting."],
        [FW.recovery || !!(FW.ident && FW.ident.answered), FW.recovery ? "ECU identity not required (recovery mode)" : "ECU answers diagnostics", "Press “Read identity” first."],
        [!!FW.sel, "Firmware image selected", "Drop an image above."],
        [!!String(s.address).trim(), "Memory address set", "Required — never guessed."],
        [!!(i && (i.product.status === "ok" || s.allow_product_mismatch)), "Image matches the product", i ? i.product.text : "Select an image and set the product keyword."],
        [FW.recovery || !(s.require_identity_match && kw && !s.allow_product_mismatch) || !!(FW.ident && FW.ident.items.some((x) => ((x.text || x.hex) + "").toLowerCase().includes(kw.toLowerCase()))),
          "ECU identity matches the product", "The ECU's reported identity must contain “" + kw + "”. Read the identity first."],
        [!run, "No update running", ""],
      ];
    }
    function pre() {
      const cs = checks();
      preBox.innerHTML = "";
      cs.forEach(([ok, t, hint]) => preBox.appendChild(h("div", { class: "pf" + (ok ? " ok" : "") }, h("span", { class: "pfdot" }, ok ? "✓" : "·"), h("span", null, t), !ok && hint ? h("span", { class: "faint" }, " — " + hint) : null)));
      flashBtn.disabled = !cs.every((x) => x[0]);
      abortBtn.classList.toggle("hidden", !CB.udsRun.running);
    }
    function confirmFlash() {
      const i = FW.info, id = FW.ident ? FW.ident.items.filter((x) => !x.error).map((x) => `${x.did}=${x.text || x.hex}`).join(", ") : "?";
      const t = h("input", { type: "text", placeholder: "type FLASH", style: { width: "180px" } });
      modal({ title: FW.recovery ? "Recovery re-flash" : "Update firmware", body: [
        h("div", { class: "expect", style: { background: "var(--bad-bg)", borderColor: "var(--bad-line)" } }, "This erases the ECU's application and reprograms it. If it is interrupted the ECU may not start until it is re-flashed." +
          (FW.recovery ? " RECOVERY MODE: the identity checks before the update are skipped, so make sure this is the right image for this ECU." : "")),
        s.keep_tx && ((live().conn || {}).tx_on) ? h("div", { class: "expect" }, "Simulated frames will keep running on the bus during the update (you ticked this in the parameters). If the update fails, try again with them stopped before blaming the firmware.") : null,
        h("div", { class: "kv" }, h("div", null, "Image"), h("div", { class: "mono" }, FW.sel), h("div", null, "Size / CRC32"), h("div", { class: "mono" }, `${i.size} B / ${i.crc32}`),
          h("div", null, "Memory address"), h("div", { class: "mono" }, s.address), h("div", null, "Link"), h("div", { class: "mono" }, `${s.tx_id} → ${s.rx_id}`), h("div", null, "ECU now"), h("div", { class: "mono" }, id),
          h("div", null, "Key"), h("div", null, s.key_algo === "echo" ? "echo of the seed" : s.key_algo)),
        h("div", { class: "field" }, h("label", null, "Type FLASH to confirm"), t)],
      buttons: [{ label: "Start update", kind: "bad", onClick: async () => { if (t.value !== "FLASH") { toast("Type FLASH exactly.", "warn"); return false; } try { await api("/api/uds/flash", { name: FW.sel, confirm: "FLASH", recovery: !!FW.recovery }); } catch (e) { return false; } CB.udsRun = { running: true, steps: {}, pct: 0, text: "", summary: "", ok: null }; CB.udsLog = []; drawSteps(); drawLog(); pre(); } }] });
      setTimeout(() => t.focus(), 60);
    }
    const recBox = h("input", { type: "checkbox", checked: !!FW.recovery, onchange: (e) => { FW.recovery = e.target.checked; pre(); } });
    right.appendChild(h("div", { class: "card", id: "fw-update-card" }, h("div", { class: "card-h" }, h("h3", null, "4 · Update"), h("span", { class: "grow" }), abortBtn, flashBtn), h("div", { class: "card-b stack" }, preBox,
      h("label", { class: "row muted", title: "For an ECU that an earlier update left half-done (it may sit in its bootloader and report no identity)" }, recBox, "Recovery mode — re-flash an ECU that reports no identity (after an interrupted update)"))));

    /* progress */
    const stepsBox = h("div", { class: "steps-v" }), bar = h("i", { style: { width: "0%" } }), ptext = h("span", { class: "muted" }), sum = h("div");
    const drawSteps = () => {
      stepsBox.innerHTML = "";
      U.steps.forEach((n, i) => { const x = CB.udsRun.steps[i] || { state: "" }; stepsBox.appendChild(h("div", { class: "st " + x.state }, h("span", { class: "sdot" }, x.state === "ok" ? "✓" : x.state === "fail" ? "✗" : x.state === "run" ? "…" : i + 1), h("span", null, n), h("span", { class: "faint sd" }, x.detail || ""))); });
      bar.style.width = (CB.udsRun.pct || 0) + "%"; ptext.textContent = CB.udsRun.text || "";
      sum.innerHTML = ""; if (CB.udsRun.summary) sum.appendChild(h("div", { class: "chip " + (CB.udsRun.skipped ? "SKIP" : CB.udsRun.ok ? "PASS" : "FAIL"), style: { whiteSpace: "normal" } }, CB.udsRun.summary));
    };
    right.appendChild(h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", null, "Progress")), h("div", { class: "card-b stack" }, stepsBox, h("div", { class: "bar" }, bar), ptext, sum)));
    drawSteps();

    /* log + console */
    const log = h("div", { class: "log", style: { height: "260px" } });
    const drawLog = () => { log.innerHTML = ""; CB.udsLog.slice(-400).forEach((e) => log.appendChild(logLine(e))); log.scrollTop = log.scrollHeight; };
    const logLine = (e) => h("div", { class: "ln " + (e.type === "frame" ? e.dir : e.level === "error" ? "ERR" : e.level === "warn" ? "MARK" : "RX") }, h("span", { class: "tm" }, fmtTime(e.ts)), h("span", null, e.type === "frame" ? e.dir : "•"), h("span", null, e.text));
    const hex = h("input", { type: "text", placeholder: "22 F1 87", style: { flex: 1 }, class: "mono" }), expert = h("input", { type: "checkbox", onchange: async (e) => {
      const box = e.target;
      if (box.checked) {
        const ok = await ask({ title: "Turn on expert mode?", danger: true, yes: "Turn on expert mode", no: "Keep it off", width: "560px",
          banner: "Expert mode can erase or reprogram the ECU",
          body: h("div", { class: "stack", style: { gap: "10px" } },
            h("div", null, "Normally this console only sends safe, read-only requests: read identity, tester present, diagnostic session."),
            h("div", null, "With expert mode on you can send ", h("b", null, "any"), " UDS service, for example:"),
            h("ul", { style: { margin: "0", paddingLeft: "20px", lineHeight: "1.7" } }, h("li", null, h("b", { class: "mono" }, "31"), " routine control — e.g. erase memory"), h("li", null, h("b", { class: "mono" }, "34 / 36 / 37"), " download and data transfer"),
              h("li", null, h("b", { class: "mono" }, "2E"), " write data by identifier"), h("li", null, h("b", { class: "mono" }, "11"), " reset the ECU"), h("li", null, h("b", { class: "mono" }, "27"), " security access")),
            h("div", { class: "muted" }, "A wrong request can leave the ECU without a working application. Use it only on instructions from the firmware team — for a normal update use “Update firmware…” instead.")) });
        if (!ok) box.checked = false;
      }
      expertTag.classList.toggle("hidden", !box.checked); hex.style.borderColor = box.checked ? "var(--bad)" : "";
    } }), expertTag = h("span", { class: "chip bad hidden" }, "EXPERT MODE ON");
    const out = h("div", { class: "mono faint", style: { minHeight: "20px" } });
    const send = async () => { try { const r = await api("/api/uds/raw", { hex: hex.value, expert: expert.checked }); out.textContent = `${r.tx}  →  ${r.rx || "(no response)"}   ${r.text || ""}`; out.style.color = r.negative ? "var(--warn)" : "var(--ok)"; } catch (e) { out.textContent = ""; } };
    hex.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
    right.appendChild(h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", null, "UDS log & console")),
      h("div", { class: "card-b stack" }, log, h("div", { class: "row" }, hex, h("button", { class: "btn primary", onclick: send }, "Send"), h("label", { class: "row muted", title: "Allow services that can erase or reprogram" }, expert, "expert"), expertTag), out)));
    drawLog(); drawIdent();

    // log lines are collected and added once per animation frame, and the list is capped: thousands of single appends with a
    // forced layout each (and an ever-growing list) is what made the page "stop responding" during a long transfer
    let pend = [], raf = 0;
    const flushLog = () => {
      raf = 0;
      const near = log.scrollHeight - log.scrollTop - log.clientHeight < 60, frag = document.createDocumentFragment();
      pend.splice(0).forEach((e) => frag.appendChild(logLine(e)));
      log.appendChild(frag);
      while (log.childElementCount > 400) log.removeChild(log.firstChild);
      if (near) log.scrollTop = log.scrollHeight;
    };
    CB.views.firmware.onUds = (e) => {
      if (e.type === "log" || e.type === "frame") { pend.push(e); if (!raf) raf = requestAnimationFrame(flushLog); } else drawSteps();
      if (e.type === "done") { if (e.ok) { FW.recovery = false; recBox.checked = false; } pre(); toast(e.summary, e.skipped ? "warn" : e.ok ? "ok" : "error", 8000); }
    };
    CB.views.firmware.onLive = () => { pre(); };
    pre(); inspect();
  },
};
