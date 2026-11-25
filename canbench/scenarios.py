"""Scenario generation (from DBC + documents) and the automated scenario runner.

A scenario is plain data, so it can be edited, exported and re-run:

  {id, title, group, source: auto|doc|custom,
   set:      [{uid, value}]            signals to drive (UI units)
   tx_off:   [message names] | "ALL"   frames to stop
   tx_off_after_ms: 0                  stop them only after this delay
   dwell_ms: 1500                      settle time before judging
   expect:   free text shown to the tester
   checks:   [{type:"can",    signal: uid, op, value, timeout_ms}
              {type:"serial", regex, timeout_ms}]      auto-verdict if present
   incomplete: bool                    stimulus still has to be assigned}

Verdict policy (the reliable-decision part):
  * with checks   -> PASS only if every check passes in time, else FAIL (no human needed)
  * without checks-> the run pauses and asks the operator (Pass / Fail / Skip)
  * a scenario never passes by default; "no answer" is never PASS.
"""
from __future__ import annotations

import hashlib
import operator
import re
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional

from .dbcmodel import Model, Sig, Comp, fmt_num

OPS = {"==": operator.eq, "!=": operator.ne, ">": operator.gt, ">=": operator.ge, "<": operator.lt,
       "<=": operator.le}


def sid(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:10]


def _short(t: str, n=260) -> str:
    t = re.sub(r"\s+", " ", t or "").strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def generate(model: Model, roles: Dict[str, str], analysis: dict) -> List[dict]:
    out: List[dict] = []
    sim = [m for m in model.messages if roles.get(m.name) == "sim" and not m.placeholder]
    reqs_by_uid: Dict[str, List[dict]] = {}
    for r in analysis.get("requirements", []):
        for u in r["signals"]:
            reqs_by_uid.setdefault(u, []).append(r)

    def spec_note(uid: str) -> str:
        rs = reqs_by_uid.get(uid)
        if not rs:
            return ""
        r = rs[0]
        label = f"Spec #{r['no']} {r['title']}" if r["no"] else f"Spec: {r['title']}"
        return f"{label} — {_short(r['text'])}" if r["text"] else label

    # ---- documents: value maps (alert-code lists etc.) -----------------------------
    covered = set()
    for vm in analysis.get("value_maps", []):
        group = f"{vm['doc']} · {vm['title']}"
        for e in vm["entries"]:
            tg = e.get("target")
            e["scenario"] = sid("doc", vm["id"], e["label"], e["value"])
            title = f"{e['label']}  →  {e['value']}"
            if tg and tg["signal"] in model.items:
                it = model.items[tg["signal"]]
                if roles.get(it.msg.name) != "sim":
                    tg = None
            if tg:
                covered.add((tg["signal"], tg["value"]))
                note = "" if e["status"] == "auto" else "  (stimulus inferred — please confirm)"
                out.append({"id": sid("doc", vm["id"], e["label"], e["value"]), "title": title, "group": group,
                            "source": "doc", "set": [{"uid": tg["signal"], "value": tg["value"]}], "tx_off": [],
                            "tx_off_after_ms": 0, "dwell_ms": 1500, "checks": [], "incomplete": False,
                            "expect": f"Document '{vm['doc']}' maps “{e['label']}” to code {e['value']}. "
                                      f"Stimulus: {tg['why']}.{note}",
                            "review": e["status"] != "auto"})
            else:
                out.append({"id": sid("doc", vm["id"], e["label"], e["value"]), "title": title, "group": group,
                            "source": "doc", "set": [], "tx_off": [], "tx_off_after_ms": 0, "dwell_ms": 1500,
                            "checks": [], "incomplete": True, "review": True,
                            "expect": f"Document '{vm['doc']}' maps “{e['label']}” to code {e['value']}. "
                                      "No matching DBC signal found — edit this scenario and pick the stimulus."})

    # ---- DBC only: message loss ------------------------------------------------------
    if sim:
        out.append({"id": sid("loss", "ALL"), "title": "Loss of ALL frames", "group": "Message loss",
                    "source": "auto", "set": [], "tx_off": "ALL", "tx_off_after_ms": 0, "dwell_ms": 2500,
                    "checks": [], "incomplete": False,
                    "expect": "DUT detects total CAN loss and shows its documented fallback / error state."})
    for m in sim:
        out.append({"id": sid("loss", m.name), "title": f"Loss of {m.name}", "group": "Message loss",
                    "source": "auto", "set": [], "tx_off": [m.name], "tx_off_after_ms": 0,
                    "dwell_ms": max(2000, 4 * m.cycle_ms), "checks": [], "incomplete": False,
                    "expect": f"Frame {m.name} (0x{m.frame_id:X}, {m.cycle_ms} ms) stops. DUT must time out and "
                              "show the documented behaviour for missing data."})

    # ---- DBC only: enums, flags, boundaries --------------------------------------------
    for m in sim:
        for it in m.items():
            if it.kind == "enum" and isinstance(it, Sig):
                for raw, label in sorted(it.choices.items()):
                    if (it.uid, raw) in covered:
                        continue
                    out.append({"id": sid("enum", it.uid, raw), "title": f"{it.name} = {label} ({raw})",
                                "group": f"Values · {m.name}.{it.name}", "source": "auto",
                                "set": [{"uid": it.uid, "value": raw}], "tx_off": [], "tx_off_after_ms": 0,
                                "dwell_ms": 1200, "checks": [], "incomplete": False,
                                "expect": spec_note(it.uid) or f"DUT reacts correctly to {it.name} = {label}."})
            elif it.kind == "bool" and isinstance(it, Sig):
                if (it.uid, 1) in covered:
                    continue
                out.append({"id": sid("bool", it.uid), "title": f"{it.name} = 1", "group": f"Flags · {m.name}",
                            "source": "auto", "set": [{"uid": it.uid, "value": 1}], "tx_off": [],
                            "tx_off_after_ms": 0, "dwell_ms": 1200, "checks": [], "incomplete": False,
                            "expect": spec_note(it.uid) or f"DUT reacts correctly to {it.name} = 1."})
            elif it.kind == "num" and isinstance(it, Sig) and it.has_soft_range and not it.is_counter \
                    and not re.search(r"reserved|version", it.name, re.I):
                for tag, v in (("min", it.dbc_min), ("max", it.dbc_max)):
                    out.append({"id": sid("bound", it.uid, tag), "title": f"{it.name} = {tag} ({fmt_num(v)} {it.unit})".strip(),
                                "group": f"Limits · {m.name}", "source": "auto",
                                "set": [{"uid": it.uid, "value": v}], "tx_off": [], "tx_off_after_ms": 0,
                                "dwell_ms": 1200, "checks": [], "incomplete": False,
                                "expect": spec_note(it.uid) or f"DUT shows/handles {it.name} at its {tag} correctly."})
    return out


def apply_overrides(generated: List[dict], project: dict) -> List[dict]:
    hidden = set(project.get("scenarios_hidden", []))
    edits = project.get("scenario_edits", {})
    out = []
    for s in generated:
        if s["id"] in hidden:
            continue
        if s["id"] in edits:
            s = {**s, **edits[s["id"]], "edited": True}
        out.append(s)
    for c in project.get("scenarios_custom", []):
        out.append({**c, "source": "custom"})
    return out


def normalise(s: dict) -> dict:
    """Clean a scenario coming from the UI."""
    return {
        "id": s.get("id") or "c" + uuid.uuid4().hex[:9],
        "title": str(s.get("title", "Untitled")).strip() or "Untitled",
        "group": str(s.get("group", "Custom")).strip() or "Custom",
        "source": s.get("source", "custom"),
        "set": [{"uid": x["uid"], "value": x["value"]} for x in s.get("set", []) if x.get("uid")],
        "tx_off": "ALL" if s.get("tx_off") == "ALL" else [str(x) for x in (s.get("tx_off") or [])],
        "tx_off_after_ms": max(0, int(s.get("tx_off_after_ms") or 0)),
        "dwell_ms": max(0, int(s.get("dwell_ms") or 1500)),
        "expect": str(s.get("expect", "")),
        "checks": [c for c in s.get("checks", []) if c.get("type") in ("can", "serial")],
        "incomplete": False,
        "confirm": bool(s.get("confirm", False)),
    }


# =============================================================================
# runner
# =============================================================================
class Runner:
    """Executes scenarios one after another on a worker thread."""

    def __init__(self, app):
        self.app = app
        self.state = {"running": False, "phase": "idle", "index": 0, "total": 0, "current": None,
                      "awaiting": False, "message": "", "done": 0, "passed": 0, "failed": 0, "skipped": 0}
        self._abort = threading.Event()
        self._verdict_evt = threading.Event()
        self._verdict = None
        self._thread: Optional[threading.Thread] = None

    # ---- public --------------------------------------------------------------------
    def start(self, scenarios: List[dict], manual: str = "prompt", restore: bool = True):
        if self.state["running"]:
            raise RuntimeError("A run is already in progress.")
        if not scenarios:
            raise RuntimeError("Nothing to run.")
        self._abort.clear()
        self._verdict_evt.clear()
        self.state.update(running=True, phase="starting", index=0, total=len(scenarios), current=None,
                          awaiting=False, message="", done=0, passed=0, failed=0, skipped=0)
        self._thread = threading.Thread(target=self._run, args=(scenarios, manual, restore), name="runner",
                                        daemon=True)
        self._thread.start()
        self._push()

    def abort(self):
        self._abort.set()
        self._verdict_evt.set()

    def verdict(self, verdict: str, note: str = ""):
        if not self.state["awaiting"]:
            raise RuntimeError("No scenario is waiting for a verdict.")
        self._verdict = (verdict.upper(), note)
        self._verdict_evt.set()

    # ---- internals -------------------------------------------------------------------
    def _push(self):
        self.app.emit("run", dict(self.state))

    def _set(self, **kw):
        self.state.update(kw)
        self._push()

    def _sleep(self, ms: int, phase: str) -> bool:
        """Interruptible wait. Returns False when aborted."""
        end = time.time() + ms / 1000.0
        while time.time() < end:
            if self._abort.is_set():
                return False
            self._set(phase=phase, remaining_ms=max(0, int((end - time.time()) * 1000)))
            time.sleep(0.1)
        return True

    def _run(self, scenarios: List[dict], manual: str, restore: bool):
        app = self.app
        try:
            for i, sc in enumerate(scenarios):
                if self._abort.is_set():
                    break
                self._set(index=i + 1, current={"id": sc["id"], "title": sc["title"], "expect": sc.get("expect", ""),
                                                "group": sc.get("group", "")}, awaiting=False, phase="applying")
                res = self._one(sc, manual)
                if res is None:
                    break
                app.record_result(res)
                k = {"PASS": "passed", "FAIL": "failed"}.get(res["verdict"], "skipped")
                self.state[k] += 1
                self.state["done"] += 1
        except Exception as e:  # the runner must never die silently
            self._set(message=f"Run stopped: {e}")
            app.emit("toast", {"level": "error", "text": f"Run stopped: {e}"})
        finally:
            try:
                if restore:
                    app.reset_baseline()
            except Exception:
                pass
            aborted = self._abort.is_set()
            self._set(running=False, phase="aborted" if aborted else "done", awaiting=False, current=None,
                      remaining_ms=0)

    def _one(self, sc: dict, manual: str) -> Optional[dict]:
        app = self.app
        if sc.get("incomplete"):
            return app.make_result(sc, "SKIP", "auto", "Stimulus not assigned — scenario skipped.", [])
        app.reset_baseline()
        app.serial.marker(f"SCENARIO: {sc['title']}")
        errs = []
        for s in sc.get("set", []):
            try:
                app.set_value(s["uid"], s["value"], marker=False)
            except Exception as e:
                errs.append(f"{s['uid']}: {e}")
        if errs:
            return app.make_result(sc, "SKIP", "auto", "Could not apply stimulus: " + "; ".join(errs), [])
        app.emit("values", app.values)  # let the Simulate screen follow the stimulus
        t0 = time.time()
        off = sc.get("tx_off")
        names = list(app.engine.tx) if off == "ALL" else list(off or [])
        delay = sc.get("tx_off_after_ms", 0)
        if names and delay <= 0:
            for n in names:
                app.set_enabled(n, False)
        if not self._sleep(delay if (names and delay > 0) else 0, "applying"):
            return None
        if names and delay > 0:
            for n in names:
                app.set_enabled(n, False)
        if not self._sleep(sc.get("dwell_ms", 1500), "dwell"):
            return None
        checks = sc.get("checks", [])
        outcomes = self._evaluate(checks, t0) if checks else []
        if outcomes is None:
            return None
        if checks and all(o["ok"] for o in outcomes) and not sc.get("confirm"):
            return app.make_result(sc, "PASS", "auto", "", outcomes)
        if checks and not all(o["ok"] for o in outcomes):
            return app.make_result(sc, "FAIL", "auto", "", outcomes)
        # operator decides
        if manual == "skip":
            return app.make_result(sc, "SKIP", "auto", "Manual check skipped (auto-only run).", outcomes)
        self._verdict = None
        self._verdict_evt.clear()
        self._set(awaiting=True, phase="awaiting", remaining_ms=0)
        self._verdict_evt.wait()
        if self._abort.is_set():
            return None
        v, note = self._verdict or ("SKIP", "")
        v = v if v in ("PASS", "FAIL", "SKIP") else "SKIP"
        self._set(awaiting=False)
        return app.make_result(sc, v, "manual", note, outcomes)

    def _evaluate(self, checks: List[dict], t0: float):
        app = self.app
        pending = list(range(len(checks)))
        outcomes: List[Optional[dict]] = [None] * len(checks)
        deadline = [time.time() + c.get("timeout_ms", 2000) / 1000.0 for c in checks]
        while pending:
            if self._abort.is_set():
                return None
            for i in list(pending):
                c = checks[i]
                ok, detail = self._check_once(c, t0)
                if ok:
                    outcomes[i] = {"desc": self._desc(c), "ok": True, "detail": detail}
                    pending.remove(i)
                elif time.time() >= deadline[i]:
                    outcomes[i] = {"desc": self._desc(c), "ok": False, "detail": detail}
                    pending.remove(i)
            time.sleep(0.05)
        return outcomes

    @staticmethod
    def _desc(c: dict) -> str:
        if c["type"] == "serial":
            return f"serial matches /{c.get('regex', '')}/"
        return f"{c.get('signal')} {c.get('op', '==')} {c.get('value')}"

    def _check_once(self, c: dict, t0: float):
        app = self.app
        if c["type"] == "serial":
            try:
                rx = re.compile(c.get("regex", ""))
            except re.error as e:
                return False, f"bad regex: {e}"
            for ts, line in app.serial.lines_since(t0):
                if rx.search(line):
                    return True, line[:160]
            return False, "no matching serial line"
        uid = c.get("signal", "")
        it = app.model.items.get(uid) if app.model else None
        if it is None:
            return False, "signal not in DBC"
        if app.engine.rx_age(it.msg.name) is None:
            return False, f"{it.msg.name} not received (is it set to Listen?)"
        v = app.engine.rx_value(it.msg.name, it.name)
        if v is None:
            return False, "no value yet"
        op = OPS.get(c.get("op", "=="), operator.eq)
        try:
            return bool(op(float(v), float(c.get("value")))), f"got {fmt_num(v)}"
        except (TypeError, ValueError):
            return False, f"cannot compare {v!r}"
