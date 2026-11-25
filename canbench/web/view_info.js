/* Info page: about / version, what the recording is and where it is saved, where every file lives, what each page does */
"use strict";
CB.views.info = {
  async render(el) {
    let a, b;
    try { [a, b] = await Promise.all([api("/api/about"), api("/api/buslog/list")]); } catch (e) { return; }
    const P = a.paths || {};
    const kb = (n) => n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB";
    const copy = async (t, what) => { try { await navigator.clipboard.writeText(t); toast((what || "Text") + " copied.", "ok", 2000); } catch (e) { toast("Could not copy.", "warn"); } };
    const openF = async (what) => { try { await api("/api/open", { what }); } catch (e) { /* shown */ } };
    const kv = (k, v) => [h("div", null, k), h("div", { style: { wordBreak: "break-all" } }, v || "–")];
    const pathRow = (label, path, what) => h("div", { class: "field" }, h("label", null, label),
      h("div", { class: "pathbox" }, h("span", { class: "mono" }, path || "–"), h("button", { class: "btn sm", onclick: () => copy(path, "Path") }, "Copy"),
        what ? h("button", { class: "btn sm", onclick: () => openF(what) }, "Open folder") : null));

    el.appendChild(h("div", { class: "page-h" }, h("h2", null, "Info"), h("span", { class: "sub" }, "Version, what is being recorded, and where everything is saved")));
    const left = h("div", { class: "stack" }), right = h("div", { class: "stack" });
    el.appendChild(h("div", { class: "info-grid" }, left, right));

    /* ---- about ---- */
    left.appendChild(h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", null, "About")),
      h("div", { class: "card-b stack" },
        h("div", { class: "row", style: { gap: "16px" } }, h("img", { class: "info-logo", src: "/static/connectm_logo.png", alt: "ConnectM" }),
          h("div", null, h("div", { style: { fontSize: "21px", fontWeight: 700 } }, a.name), h("div", { class: "muted" }, "Version " + a.version + "  ·  build " + a.build))),
        h("div", { class: "muted" }, "DBC-driven CAN test bench with a UDS firmware updater, built for " + a.company + " test engineers. Every verdict, the CAN trace and each firmware step is kept as evidence."),
        h("div", { class: "kv" }, kv("Company", a.company), kv("Running as", a.packaged ? "packaged program (.exe)" : "Python " + a.python),
          kv("Libraries", Object.entries(a.libs).map(([k, v]) => k + " " + v).join("  ·  ")), kv("System", a.platform), kv("Project", a.project)),
        h("div", { class: "row" }, h("button", { class: "btn", onclick: () => copy(`${a.name} ${a.version} (build ${a.build}) · ${a.packaged ? "exe" : "python " + a.python} · ${a.platform}`, "Version details") }, "Copy version details"),
          h("span", { class: "faint" }, "Quote this when you report a problem.")))));

    /* ---- pages ---- */
    left.appendChild(h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", null, "What each page does")),
      h("div", { class: "card-b" }, h("div", { class: "help-list" },
        ...[["Setup", "Load the CAN database (DBC or Excel table) and documents; choose what the bench simulates and what the DUT sends."],
          ["Simulate", "Change signal values by hand and watch what the DUT sends back."],
          ["Send", "Put any CAN ID on the bus once or repeating — no database needed."],
          ["Scenarios", "Run generated or your own tests and get automatic Pass / Fail."],
          ["Results", "Every verdict, CSV export and the HTML report."],
          ["Firmware", "Read the ECU identity and update its firmware over UDS."],
          ["Trace", "Live list of every frame on the bus."],
          ["Serial", "Debug UART log with markers for every CAN change."]].flatMap(([k, v]) => [h("div", null, k), h("div", { class: "muted" }, v)])))));

    /* ---- recording ---- */
    const status = h("div"), filesBox = h("div", { class: "stack", style: { gap: "10px" } });
    const drawStatus = () => {
      const L = (live().buslog || {}); status.innerHTML = "";
      status.appendChild(L.active ? h("span", { class: "chip bad" }, `● Recording now: ${L.name} · ${L.frames.toLocaleString()} frames` + (L.dropped ? ` · ${L.dropped} dropped` : ""))
        : h("span", { class: "chip" }, b.enabled ? "Not recording — it starts when you press Connect" : "Recording is switched off for this project"));
    };
    const drawFiles = () => {
      filesBox.innerHTML = "";
      filesBox.appendChild(h("label", { class: "row" }, h("input", { type: "checkbox", checked: b.enabled, disabled: !!(live().conn || {}).connected, onchange: async (e) => { try { await api("/api/buslog/toggle", { on: e.target.checked }); b.enabled = e.target.checked; drawStatus(); } catch (err) { e.target.checked = b.enabled; } } }),
        "Record every connection", (live().conn || {}).connected ? h("span", { class: "faint" }, "  (disconnect to change)") : null));
      if (!b.files.length) { filesBox.appendChild(h("div", { class: "muted" }, "No recordings yet — press Connect to start one.")); return; }
      const t = h("table", { class: "t" }, h("thead", null, h("tr", null, ["File", "Size", "Last written", ""].map((x) => h("th", null, x)))), h("tbody", null, b.files.map((f) => h("tr", null,
        h("td", { class: "mono", style: { wordBreak: "break-all" } }, f.name, f.active ? h("span", { class: "chip bad", style: { marginLeft: "8px" } }, "recording") : null), h("td", { style: { whiteSpace: "nowrap" } }, kb(f.size)),
        h("td", { class: "muted", style: { whiteSpace: "nowrap" } }, fmtDT(f.mtime)),
        h("td", null, h("button", { class: "btn sm", onclick: () => download(tokenUrl("/api/buslog/file?name=" + encodeURIComponent(f.name)), f.name) }, "Download"))))));
      filesBox.appendChild(h("div", { style: { maxHeight: "300px", overflow: "auto" } }, t));
      filesBox.appendChild(h("div", { class: "row" }, h("span", { class: "muted" }, `${b.n_files} file(s) · ${kb(b.total)}`), h("span", { class: "grow" }),
        h("button", { class: "btn bad sm", disabled: b.files.every((f) => f.active), onclick: async () => {
          if (await ask({ title: "Delete old bus logs", body: "Delete all bus logs except the one being recorded? Results and reports that name them will no longer be able to show them.", yes: "Delete old logs", danger: true })) {
            try { await api("/api/buslog/clear", {}); b = await api("/api/buslog/list"); drawFiles(); } catch (e) { /* shown */ } } } }, icon("trash"), "Delete old logs")));
    };
    right.appendChild(h("div", { class: "card", id: "info-recording" }, h("div", { class: "card-h" }, h("h3", null, "Recording (bus log)"), h("span", { class: "grow" }), status),
      h("div", { class: "card-b stack" },
        h("div", null, h("b", null, "What is recorded"),
          h("ul", { style: { margin: "6px 0 0", paddingLeft: "20px", lineHeight: "1.65" } },
            h("li", null, "Every CAN frame the bench ", h("b", null, "sends"), " and ", h("b", null, "receives"), " — ID, data bytes, exact time, and error frames."),
            h("li", null, "The bench's own markers: connect / disconnect, TX started and stopped, each RESULT, firmware update started / finished / aborted."),
            h("li", null, "Not recorded: the serial (UART) log — that stays on the Serial page — and decoded signal values (open the file with your DBC in any CAN tool to decode).")),
          h("div", { style: { marginTop: "8px" } }, h("b", null, "When: "), "one file per connection. It starts when you press ", h("b", null, "Connect"), " and ends on Disconnect or when you close the program. A very long session continues in a numbered part file every 64 MB.")),
        h("div", null, h("b", null, "What it is for: "), "it is the evidence behind a verdict. Each result and the HTML report name the file that was recording at that moment (with its SHA-256), so a failed test can be examined frame by frame. The format is Vector ", h("span", { class: "mono" }, ".asc"), " — open it in CANalyzer, PCAN-View or BUSMASTER."),
        pathRow("Where it is saved (this project)", P.logs, "logs"),
        filesBox)));
    right.appendChild(h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", null, "Where everything is saved")),
      h("div", { class: "card-b stack" },
        h("div", { class: "muted" }, "Everything stays on this PC, in the workspace folder next to the program. Copy the project folder to move or back up a project."),
        pathRow("Workspace (all projects)", P.workspace, "workspace"), pathRow("This project's folder", P.project, "project"),
        pathRow("Database and documents you added (copies, hashed)", P.assets, "assets"), pathRow("Results (one line per verdict, written immediately)", P.results, null),
        pathRow("Project settings (roles, baseline, UDS settings…)", P.project_file, null))));
    drawStatus(); drawFiles();
    CB.views.info.onLive = drawStatus;
    if (location.hash === "#info-recording") setTimeout(() => { const t = document.getElementById("info-recording"); if (t) t.scrollIntoView({ block: "start" }); }, 50);
  },
  onLive: null,
};
