"""Result storage (append-only JSONL), CSV export and the self-contained HTML report."""
from __future__ import annotations

import csv
import html
import io
import json
import os
import re
import threading
import time
from typing import List, Optional

import base64
import hashlib

from . import APP_NAME, COMPANY, __version__

_LOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "connectm_logo.png")


def logo_data_uri() -> str:
    try:
        with open(_LOGO, "rb") as f:
            return "data:image/png;base64," + base64.b64encode(f.read()).decode()
    except OSError:
        return ""


class ResultStore:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.items: List[dict] = []
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if ln:
                        try:
                            self.items.append(json.loads(ln))
                        except ValueError:
                            continue  # a torn last line must not lose the rest

    def add(self, r: dict):
        with self.lock:
            self.items.append(r)
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(r) + "\n")
                f.flush()
                os.fsync(f.fileno())

    def _rewrite(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for r in self.items:
                f.write(json.dumps(r) + "\n")
        os.replace(tmp, self.path)

    def delete(self, ids: List[str]):
        with self.lock:
            s = set(ids)
            self.items = [r for r in self.items if r["id"] not in s]
            self._rewrite()

    def clear(self):
        with self.lock:
            self.items = []
            self._rewrite()

    def counts(self):
        c = {"PASS": 0, "FAIL": 0, "SKIP": 0}
        for r in self.items:
            c[r["verdict"]] = c.get(r["verdict"], 0) + 1
        return c


def _ts(t) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def file_slug(name: str) -> str:
    """Project name -> safe file-name part (keeps dots in version numbers, drops everything else odd)."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "").strip()).strip("._-")
    return s[:60] or "project"


def to_csv(results: List[dict], project_name: str = "") -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Project", "Time", "Kind", "Group", "Item", "Stimulus", "Expected", "Verdict", "Mode", "Checks",
                "DUT feedback (CAN)", "Note", "CAN log file"])
    for r in results:
        checks = "; ".join(f"{'OK' if c['ok'] else 'FAIL'}: {c['desc']} ({c['detail']})" for c in r.get("checks", []))
        fb = "; ".join(f"{k}={v}" for k, v in r.get("dut", {}).items())
        w.writerow([project_name, _ts(r["ts"]), r.get("kind", ""), r.get("group", ""), r["title"], r.get("stimulus", ""),
                    r.get("expected", ""), r["verdict"], r.get("mode", ""), checks, fb, r.get("note", ""), r.get("buslog", "")])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def html_report(project: dict, results: List[dict], summary: dict, findings: List[dict], bus: dict,
                logs: Optional[List[dict]] = None) -> bytes:
    """logs: the bus-log files (name, size, sha256) the results point to - the CAN-level evidence behind each verdict."""
    e = html.escape
    c = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for r in results:
        c[r["verdict"]] = c.get(r["verdict"], 0) + 1
    total = len(results)
    decided = c["PASS"] + c["FAIL"]
    rate = f"{100 * c['PASS'] / decided:.0f}%" if decided else "–"
    dut = project.get("dut", {})
    rows = []
    for r in results:
        cls = r["verdict"].lower()
        det = ""
        if r.get("checks"):
            det += "<ul>" + "".join(f"<li class='{'ok' if k['ok'] else 'bad'}'>{e(k['desc'])} — {e(str(k['detail']))}</li>"
                                    for k in r["checks"]) + "</ul>"
        if r.get("dut"):
            det += "<div class='mono'>" + e("; ".join(f"{k}={v}" for k, v in r["dut"].items())) + "</div>"
        if r.get("serial"):
            det += "<pre>" + e("\n".join(r["serial"][-12:])) + "</pre>"
        if r.get("buslog"):
            det += f"<div class='sub'>CAN trace: <span class='mono'>{e(r['buslog'])}</span></div>"
        rows.append(
            f"<tr><td class='mono'>{e(_ts(r['ts']))}</td><td>{e(r.get('group', ''))}<b>{e(r['title'])}</b>"
            f"<div class='sub'>{e(r.get('stimulus', ''))}</div></td><td>{e(r.get('expected', ''))}</td>"
            f"<td><span class='tag {cls}'>{e(r['verdict'])}</span><div class='sub'>{e(r.get('mode', ''))}</div></td>"
            f"<td>{e(r.get('note', ''))}{det}</td></tr>")
    files = "".join(f"<tr><td>{e(f['kind'].upper())}</td><td>{e(f['name'])}</td><td class='mono'>{e(f['sha256'])}</td></tr>"
                    for f in project.get("files", []))
    logo = logo_data_uri()
    logs_html = ""
    if logs:
        lrows = "".join(f"<tr><td class='mono'>{e(x['name'])}</td><td>{x['size'] / 1024:.0f} KB</td><td class='mono'>{e(x['sha256'])}</td></tr>"
                        for x in logs)
        logs_html = ("<h2>CAN bus logs (evidence)</h2><div class='sub'>Vector ASC files recorded during the tests, kept in the "
                     "project folder (logs/). Open them in CANalyzer, PCAN-View or BUSMASTER.</div>"
                     f"<table><tr><th>File</th><th>Size</th><th>SHA-256</th></tr>{lrows}</table>")
    fnd = "".join(f"<tr><td><span class='tag {e(x['level'])}'>{e(x['level'])}</span></td><td>{e(x['where'])}</td>"
                  f"<td>{e(x['text'])}</td></tr>" for x in findings)
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>CAN test report — {e(project.get('name', ''))}</title>
<style>
body{{font:14px/1.5 "Segoe UI",system-ui,sans-serif;color:#0f172a;margin:32px auto;max-width:1100px;padding:0 20px}}
.hd{{display:flex;align-items:center;gap:18px}} .hd .logo{{height:44px;border-radius:10px;border:1px solid #e2e8f0}}
h1{{margin:0 0 4px}} h2{{margin:32px 0 8px;font-size:16px;border-bottom:1px solid #e2e8f0;padding-bottom:6px}}
.sub{{color:#64748b;font-size:12px}} .mono{{font-family:Consolas,monospace;font-size:12px;word-break:break-all}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th{{text-align:left;color:#475569;font-size:12px;
border-bottom:2px solid #e2e8f0;padding:6px 8px}} td{{border-bottom:1px solid #eef2f7;padding:7px 8px;vertical-align:top}}
.tiles{{display:flex;gap:12px;margin:16px 0}} .tile{{flex:1;border:1px solid #e2e8f0;border-radius:10px;padding:12px 16px}}
.tile b{{display:block;font-size:26px}} .tag{{padding:2px 8px;border-radius:99px;font-size:11px;font-weight:600}}
.pass,.info{{background:#dcfce7;color:#166534}} .fail,.error{{background:#fee2e2;color:#991b1b}}
.skip,.warn{{background:#fef3c7;color:#92400e}} .info{{background:#e0f2fe;color:#075985}}
li.ok{{color:#166534}} li.bad{{color:#991b1b}} pre{{background:#f1f5f9;padding:6px;border-radius:6px;font-size:11px;
white-space:pre-wrap}} @media print{{body{{margin:0}}}}
</style></head><body>
<div class="hd">{('<img class="logo" alt="' + e(COMPANY) + '" src="' + logo + '">') if logo else ''}<div>
<h1>CAN test report — {e(project.get('name', ''))}</h1><div class="sub">Project: {e(project.get('name', ''))} · generated {e(_ts(time.time()))} · {e(APP_NAME)} {__version__}</div></div></div>
<div class="tiles"><div class="tile"><b>{c['PASS']}</b>Pass</div><div class="tile"><b>{c['FAIL']}</b>Fail</div>
<div class="tile"><b>{c['SKIP']}</b>Skipped</div><div class="tile"><b>{total}</b>Total</div>
<div class="tile"><b>{rate}</b>Pass rate (decided)</div></div>
<h2>Test setup</h2><table>
<tr><td>Operator</td><td>{e(project.get('operator', ''))}</td></tr>
<tr><td>DUT</td><td>{e(dut.get('name', ''))} · serial {e(dut.get('serial', ''))} · firmware {e(dut.get('firmware', ''))}</td></tr>
<tr><td>Bus</td><td>{e(str(bus.get('interface', '')))} {e(str(bus.get('channel', '')))} @ {e(str(bus.get('bitrate', '')))} bit/s</td></tr>
<tr><td>Database</td><td>{summary.get('messages', 0)} messages · {summary.get('signals', 0)} signals</td></tr>
<tr><td>Notes</td><td>{e(project.get('notes', ''))}</td></tr></table>
<h2>Inputs (SHA-256)</h2><table><tr><th>Type</th><th>File</th><th>SHA-256</th></tr>{files}</table>
{logs_html}<h2>Results</h2><table><tr><th>Time</th><th>Test</th><th>Expected</th><th>Verdict</th><th>Notes / evidence</th></tr>
{''.join(rows)}</table>
<h2>Database health findings</h2><table><tr><th>Level</th><th>Where</th><th>Finding</th></tr>{fnd or '<tr><td colspan=3>None</td></tr>'}</table>
</body></html>"""
    return doc.encode("utf-8")
