"""Application controller: owns the project, model, engine, runner and results.

The HTTP layer (server.py) is a thin shell over the methods in here, so the
same object can be driven from tests or scripts without a browser.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import uuid
from typing import Dict, List, Optional

from . import APP_NAME, BUILD_DATE, COMPANY, __version__
from . import buslog
from . import docs as docmod
from . import tablecan
from . import uds as udsmod
from .dbcmodel import Comp, Model, Sig, fmt_num
from .dbcmodel import signal_bits as signal_bits_
from .engine import Engine, INTERFACES
from .project import ProjectStore, kind_of, slug
from .results import ResultStore, file_slug, html_report, to_csv
from .scenarios import Runner, apply_overrides, generate, normalise
from .serialio import SerialPort

ROLES = ("sim", "listen", "ignore")


def re_sub_ws(s: str) -> str:
    """'22 f1 87', '0x22,0xF1,0x87' -> '22f187'"""
    import re
    return re.sub(r"0x|[\s,;:-]+", "", str(s), flags=re.I)


class AppError(Exception):
    """A user-facing error (shown as a toast, HTTP 400)."""


class App:
    def __init__(self, workspace: str, hub=None):
        self.store = ProjectStore(workspace)
        self.hub = hub
        self.lock = threading.RLock()
        self.engine = Engine()
        self.serial = SerialPort(on_line=self._on_serial)
        self.runner = Runner(self)
        self.project: Optional[dict] = None
        self.model: Optional[Model] = None
        self.docs: List[dict] = []
        self.tables: Dict[str, dict] = {}
        self.analysis: dict = {"requirements": [], "value_maps": [], "mismatches": []}
        self.generated: List[dict] = []
        self.results: Optional[ResultStore] = None
        self.values: Dict[str, object] = {}
        self._serial_pending: List[list] = []
        self._trace_seq = 0
        self.uds_lock = threading.Lock()          # one UDS conversation at a time
        self.flash: Optional[udsmod.FlashJob] = None
        self.uds_log: List[dict] = []
        self.uds_run = {"running": False, "steps": {}, "pct": 0, "text": "", "summary": "", "ok": None}
        projects = self.store.list()
        if projects:
            self.open_project(projects[0]["id"])

    # ---- events ------------------------------------------------------------------------
    def emit(self, kind: str, data):
        if self.hub:
            self.hub.publish(kind, data)

    def _on_serial(self, ts, kind, text):
        self._serial_pending.append([round(ts, 3), kind, text])
        if kind == "MARK":                       # the bench's own markers (TX started, RESULT ..., FIRMWARE UPDATE ...) also go
            self.engine.record_event(text)       # into the bus log, so the CAN trace and the test steps line up

    def toast(self, text, level="info"):
        self.emit("toast", {"level": level, "text": text})

    # ---- projects ------------------------------------------------------------------------
    def projects(self):
        return self.store.list()

    def new_project(self, name: str) -> dict:
        if not str(name).strip():
            raise AppError("Give the project a name.")
        p = self.store.create(str(name)[:80])
        self.open_project(p["id"])
        return p

    def _refuse_during_flash(self, what: str):
        if self.uds_run["running"]:
            raise AppError(f"A firmware update is running - {what} now could leave the ECU without a valid application. "
                           "Wait for it to finish (or use Abort on the Firmware page).")

    def open_project(self, pid: str):
        self._refuse_during_flash("switching project")
        with self.lock:
            try:
                p = self.store.load(pid)
            except (OSError, ValueError) as e:
                raise AppError(f"Cannot open project: {e}")
            self.engine.close()
            self.project = p
            self.results = ResultStore(os.path.join(self.store.dir(pid), "results.jsonl"))
            self.reload()

    def delete_project(self, pid: str):
        self._refuse_during_flash("deleting the project")
        with self.lock:
            if self.project and self.project["id"] == pid:
                self.engine.close()
                self.project = self.model = self.results = None
                self.engine.configure(None, {}, {})
            self.store.delete(pid)
            rest = self.store.list()
            if rest:
                self.open_project(rest[0]["id"])

    def save(self):
        if self.project:
            self.store.save(self.project)

    def reload(self):
        """Rebuild everything derived from the project's files."""
        with self.lock:
            p = self.project
            if p is None:
                return
            dbcs = [self.store.asset_path(p["id"], f["name"]) for f in p["files"] if f["kind"] == "dbc"]
            self.model = Model(dbcs, composites=p.get("composites", True))
            # new messages get a suggested role; existing choices are kept
            for m in self.model.messages:
                if m.name not in p["roles"]:
                    p["roles"][m.name] = self._suggest_role(m)
            self.docs = [docmod.read_document(self.store.asset_path(p["id"], f["name"]))
                         for f in p["files"] if f["kind"] == "doc"]
            self.tables = {}
            for d in self.docs:  # spreadsheets that look like a CAN ID / signal table can be turned into a database
                if d["kind"] in ("xlsx", "csv", "tsv") and not d["error"]:
                    t = tablecan.detect(d)
                    if t:
                        self.tables[d["name"]] = {"frames": len(t["frames"]), "signals": t["signals"], "layout": t["layout"],
                                                  "id_base": t["id_base"], "warnings": t["warnings"][:3],
                                                  "n_warnings": len(t["warnings"]),
                                                  "columns": t["columns"]}
            imported = {f.get("source") for f in p["files"] if f.get("source")}   # a CAN table that became the database is
            spec_docs = [d for d in self.docs if d["name"] not in imported]       # the database, not a requirements document
            self.analysis = docmod.analyze(self.model, spec_docs) if self.model.messages else \
                {"requirements": [], "value_maps": [], "mismatches": []}
            self.engine.configure(self.model, p["roles"], p["periods"])
            self.rebuild_scenarios()
            self.values = {}
            self.reset_baseline(save=False)
            self.save()

    def rebuild_scenarios(self):
        if not self.model:
            self.generated = []
            return
        self.generated = generate(self.model, self.project["roles"], self.analysis)

    def _suggest_role(self, m) -> str:
        if m.placeholder:
            return "ignore"
        p = self.project
        if m.name in {f["name"] for f in p.get("custom_frames", [])}:
            return "listen"
        if p.get("dut_node") and p["dut_node"] in m.senders:  # the DUT transmits it, so the bench only listens
            return "listen"
        return "sim"

    def scenarios(self) -> List[dict]:
        return apply_overrides(self.generated, self.project) if self.project else []

    # ---- files -------------------------------------------------------------------------------
    def add_file(self, filename: str, data: bytes) -> dict:
        if not self.project:
            raise AppError("Create a project first.")
        try:
            entry = self.store.add_file(self.project, filename, data)
        except ValueError as e:
            raise AppError(str(e))
        self.reload()
        return entry

    def _table_doc(self, name: str) -> dict:
        p = self.project
        if not p or not any(f["name"] == name and f["kind"] == "doc" for f in p["files"]):
            raise AppError("Unknown document.")
        doc = docmod.read_document(self.store.asset_path(p["id"], name))
        if doc["error"]:
            raise AppError(f"Cannot read {name}: {doc['error']}")
        return doc

    def table_grid(self, name: str) -> dict:
        """The first rows of every table in a spreadsheet, for the manual column picker."""
        if self.project and not any(f["name"] == name and f["kind"] == "doc" for f in self.project["files"]):
            raise AppError("Unknown document.")
        doc = self._table_doc(name)
        tables = tablecan.grid(doc)
        if not tables:
            raise AppError("No table found in this document.")
        return {"tables": tables, "fields": [{"key": k, "label": l} for k, l in tablecan.FIELD_LABELS]}

    def _table_result(self, name: str, id_base: str, id_format: str, mapping: Optional[dict] = None) -> dict:
        if id_base not in ("auto", "hex", "dec") or id_format not in ("auto", "std", "ext"):
            raise AppError("Unknown ID option.")
        doc = self._table_doc(name)
        try:
            res = tablecan.detect(doc, id_base, id_format, mapping)
        except ValueError as e:
            raise AppError(str(e))
        if res is None:
            raise AppError("No CAN ID table found. The sheet needs a header row with at least a CAN ID column and either "
                           "message names (frames only) or signal name + start bit/byte position + length (signals)." +
                           (" Use 'Map columns…' to point at the columns yourself." if mapping is None else
                            " With the columns you picked nothing could be read - check the header row and the ID column."))
        return res

    def preview_table(self, name: str, id_base: str = "auto", id_format: str = "auto", mapping: Optional[dict] = None) -> dict:
        """What importing would create - nothing is changed."""
        return tablecan.preview(self._table_result(name, id_base, id_format, mapping))

    def import_table(self, name: str, id_base: str = "auto", id_format: str = "auto", mapping: Optional[dict] = None) -> dict:
        """Turn a spreadsheet of CAN IDs / signals into a DBC file of the project (the sheet itself is kept as evidence)."""
        p = self.project
        res = self._table_result(name, id_base, id_format, mapping)
        try:
            data = tablecan.to_dbc_bytes(res)
        except Exception as e:
            raise AppError(f"Could not build a database from the table: {e}")
        stem = os.path.splitext(name)[0]
        dbc_name = f"{stem}_from_table.dbc"
        if self.engine.tx_on:
            raise AppError("Stop transmitting before changing the database.")
        entry = self.store.add_file(p, dbc_name, data)
        entry["source"] = name
        if mapping is not None:
            entry["mapping"] = mapping          # recorded with the file: how the columns were read, by hand
        if res["has_direction"]:
            p["dut_node"] = "DUT"
            for f in res["frames"]:  # direction is known, so the roles are too
                p["roles"][f["name"]] = "listen" if f["dir"] == "tx" else "sim"
        if res.get("bitrate") and 10_000 <= res["bitrate"] <= 1_000_000:
            p["bus"]["bitrate"] = int(res["bitrate"])   # the sheet states the bus speed; wrong speed = no communication
        self.reload()
        return {"file": entry["name"], "frames": len(res["frames"]), "signals": res["signals"], "warnings": res["warnings"],
                "layout": res["layout"], "id_base": res["id_base"], "nodes": res["nodes"], "bitrate": res.get("bitrate")}

    def remove_file(self, name: str):
        self.store.remove_file(self.project, name)
        self.reload()

    # ---- setup edits -----------------------------------------------------------------------------
    def set_role(self, msg: str, role: str):
        if role not in ROLES or not self.model or msg not in self.model.by_name:
            raise AppError("Unknown message or role.")
        if self.engine.tx_on:
            raise AppError("Stop transmitting before changing message roles.")
        self.project["roles"][msg] = role
        self.save()
        self._reconfigure()

    def set_dut_node(self, node: str):
        """The DUT is `node`: frames it transmits are Listen, everything else is Simulate."""
        if not self.model or node not in self.model.nodes():
            raise AppError("Unknown node.")
        self.project["dut_node"] = node
        mapping = {}
        for m in self.model.messages:
            mapping[m.name] = "ignore" if m.placeholder else ("listen" if node in m.senders else "sim")
        self.set_all_roles(mapping)

    def set_all_roles(self, mapping: Dict[str, str]):
        if self.engine.tx_on:
            raise AppError("Stop transmitting before changing message roles.")
        for k, v in mapping.items():
            if v in ROLES and self.model and k in self.model.by_name:
                self.project["roles"][k] = v
        self.save()
        self._reconfigure()

    def _reconfigure(self):
        with self.lock:
            keep = {n: self.engine.get_raw(n) for n in self.engine.tx}
            self.engine.configure(self.model, self.project["roles"], self.project["periods"], keep)
            self.rebuild_scenarios()
            self.reset_baseline(save=False)

    def set_composites(self, on: bool):
        self.project["composites"] = bool(on)
        self.save()
        self.reload()

    def set_period(self, msg: str, ms: int):
        ms = max(5, min(60000, int(ms)))
        self.project["periods"][msg] = ms
        self.engine.set_period(msg, ms)
        self.save()

    def set_all_periods(self, ms=None) -> int:
        """One period for every simulated message (ms given), or back to each message's own period from the DBC (ms None).
        A CAN signal has no period of its own: it travels inside its message, so periods are per message."""
        if not self.model:
            raise AppError("No database loaded.")
        names = [m.name for m in self.model.messages if not m.placeholder]
        if ms is None:
            for n in names:
                self.project["periods"].pop(n, None)
        else:
            try:
                ms = max(5, min(60000, int(float(ms))))
            except (TypeError, ValueError):
                raise AppError("Period must be a number of milliseconds (5 - 60000).")
            for n in names:
                self.project["periods"][n] = ms
        for n in list(self.engine.tx):
            m = self.model.by_name[n]
            self.engine.set_period(n, self.project["periods"].get(n, m.cycle_ms))
        self.save()
        return len(names)

    def set_meta(self, d: dict):
        p = self.project
        for k in ("name", "operator", "notes"):
            if k in d:
                p[k] = str(d[k])[:2000]
        if "dut" in d:
            p["dut"] = {k: str(d["dut"].get(k, ""))[:200] for k in ("name", "serial", "firmware")}
        if "bus" in d:
            b = d["bus"]
            p["bus"] = {"interface": str(b.get("interface", "pcan")), "channel": str(b.get("channel", "")),
                        "bitrate": int(b.get("bitrate", 500000))}
        self.save()

    # ---- bus ------------------------------------------------------------------------------------------
    def channels(self, interface: str) -> List[str]:
        import can
        if interface == "virtual":
            return ["canbench"]
        try:
            return [str(c["channel"]) for c in can.detect_available_configs(interface)]
        except Exception:
            return []

    def connect(self, interface: str, channel: str, bitrate: int):
        if interface not in INTERFACES:
            raise AppError(f"Unknown interface '{interface}'.")
        self._refuse_during_flash("reconnecting")
        # no DBC needed to connect: firmware updates and discovering unknown frames work without one
        p = self.project
        ldir = buslog.log_dir(self.store.dir(p["id"])) if p.get("record_buslog", True) else None
        try:
            self.engine.open(interface, channel, bitrate, log_dir=ldir, log_stem=p.get("name", ""))
        except Exception as e:
            raise AppError(f"Could not open {interface} {channel}: {e}")
        p["bus"] = {"interface": interface, "channel": channel, "bitrate": int(bitrate)}
        self.save()
        self.serial.marker(f"CAN connected {interface} {channel} @ {bitrate}")

    def disconnect(self):
        self._refuse_during_flash("disconnecting")
        if self.runner.state["running"]:
            self.runner.abort()
        self.engine.close()
        self.serial.marker("CAN disconnected")

    # ---- bus log (recording) -------------------------------------------------------------------------
    def _logs_dir(self) -> str:
        return buslog.log_dir(self.store.dir(self.project["id"]))

    def buslog_state(self) -> dict:
        p = self.project
        if not p:
            return {"enabled": False, "files": [], "total": 0, "live": {"active": False}}
        info = self.engine.recorder_info()
        files = buslog.list_logs(self._logs_dir(), info.get("name"))
        return {"enabled": bool(p.get("record_buslog", True)), "files": files[:40], "n_files": len(files),
                "total": sum(f["size"] for f in files), "live": info}

    def set_buslog(self, on: bool):
        if self.engine.connected:
            raise AppError("Disconnect first - recording starts and stops with the connection.")
        self.project["record_buslog"] = bool(on)
        self.save()

    def buslog_path(self, name: str) -> str:
        p = buslog.safe_path(self._logs_dir(), name)
        if not p:
            raise AppError("No such bus log.")
        return p

    def clear_buslogs(self) -> int:
        active = self.engine.recorder_info().get("name")
        n = 0
        for f in buslog.list_logs(self._logs_dir()):
            if f["name"] == active:
                continue
            try:
                os.remove(os.path.join(self._logs_dir(), f["name"]))
                n += 1
            except OSError:
                pass
        return n

    # ---- manual frames: any CAN ID, typed by the tester --------------------------------------------------------
    @staticmethod
    def _parse_manual(d: dict) -> dict:
        try:
            fid = int(str(d.get("id", "")).strip().lower().replace("0x", "") or "x", 16)
        except ValueError:
            raise AppError("CAN ID must be hex, e.g. 7DF or 0x18FEF100.")
        ext = d.get("ext")
        ext = (fid > 0x7FF) if ext in (None, "", "auto") else bool(ext)
        if fid < 0 or fid > (0x1FFFFFFF if ext else 0x7FF):
            raise AppError(f"ID 0x{fid:X} does not fit in {'29' if ext else '11'} bits" +
                           ("" if ext else " - tick 29-bit if this is an extended ID."))
        try:
            data = bytes.fromhex(re_sub_ws(d.get("data", "")))
        except ValueError:
            raise AppError("Data must be hex bytes, e.g. 02 01 0C 00 00 00 00 00")
        if len(data) > 8:
            raise AppError("A classic CAN frame carries at most 8 data bytes.")
        try:
            period = int(float(d.get("period_ms") or 0))
            duration = float(d.get("duration_s") or 0)
            count = int(float(d.get("count") or 0))
        except (TypeError, ValueError):
            raise AppError("Period, duration and count must be numbers.")
        if period < 0 or (period and period < 2) or period > 3_600_000:
            raise AppError("Period must be 0 (send once) or between 2 ms and 3 600 000 ms.")
        if duration < 0 or duration > 86400 or count < 0 or count > 10_000_000:
            raise AppError("Duration must be 0-86400 s and count 0-10 000 000 (0 = no limit).")
        return {"id": fid, "ext": ext, "data": data, "period_ms": period, "duration_s": duration, "count": count}

    def manual_send(self, d: dict) -> dict:
        if not self.engine.connected:
            raise AppError("Connect to the CAN interface first.")
        self._refuse_manual_during_flash()
        f = self._parse_manual(d)
        label = str(d.get("name", ""))[:40]
        try:
            key = self.engine.start_manual(f["id"], f["ext"], f["data"], f["period_ms"], f["duration_s"], f["count"], label)
        except (ValueError, RuntimeError) as e:
            raise AppError(str(e))
        hexid = f"{f['id']:08X}" if f["ext"] else f"{f['id']:03X}"
        when = "once" if key is None else f"every {f['period_ms']} ms" + (f" for {f['duration_s']:g} s" if f["duration_s"] else "") + \
            (f", {f['count']} frames" if f["count"] else "")
        self.serial.marker(f"CAN manual frame {hexid} [{f['data'].hex(' ').upper()}] {when}")
        return {"key": key, "once": key is None}

    def _refuse_manual_during_flash(self):
        if self.uds_run["running"]:
            raise AppError("A firmware update is running - frames sent by hand now could corrupt it.")

    def manual_stop(self, key: Optional[str] = None):
        if key:
            self.engine.stop_manual(key)
        else:
            self.engine.stop_manual()
        self.serial.marker("CAN manual frames stopped" if not key else f"CAN manual frame {key} stopped")

    def manual_clear_finished(self):
        self.engine.clear_finished_manual()

    def manual_save(self, d: dict) -> dict:
        f = self._parse_manual(d)
        name = str(d.get("name", "")).strip()[:60] or f"0x{f['id']:X}"
        item = {"name": name, "id": f"{f['id']:X}", "ext": f["ext"], "data": f["data"].hex(" ").upper(),
                "period_ms": f["period_ms"], "duration_s": f["duration_s"], "count": f["count"]}
        lst = [x for x in self.project.get("manual_frames", []) if x["name"] != name]
        lst.append(item)
        self.project["manual_frames"] = lst[-60:]
        self.save()
        return item

    def manual_delete(self, name: str):
        self.project["manual_frames"] = [x for x in self.project.get("manual_frames", []) if x["name"] != name]
        self.save()

    # ---- where things are saved -------------------------------------------------------------------------------
    def paths(self) -> dict:
        out = {"workspace": self.store.root}
        if self.project:
            d = self.store.dir(self.project["id"])
            out.update(project=d, project_file=os.path.join(d, "project.json"), assets=os.path.join(d, "assets"),
                       results=os.path.join(d, "results.jsonl"), logs=buslog.log_dir(d))
        return out

    def open_folder(self, what: str) -> str:
        """Show a CANBench folder in the file explorer (only these fixed folders - never an arbitrary path)."""
        p = self.paths()
        if what not in ("workspace", "project", "assets", "logs") or what not in p:
            raise AppError("Unknown folder.")
        path = p[what]
        os.makedirs(path, exist_ok=True)
        import subprocess
        import sys
        try:
            if sys.platform == "win32":
                os.startfile(path)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
        except OSError as e:
            raise AppError(f"Could not open the folder: {e}")
        return path

    # ---- about ---------------------------------------------------------------------------------------------
    def about(self) -> dict:
        import platform
        import sys
        import can
        import cantools
        import isotp
        return {"name": APP_NAME, "company": COMPANY, "version": __version__, "build": BUILD_DATE,
                "python": sys.version.split()[0], "packaged": bool(getattr(sys, "frozen", False)),
                "libs": {"python-can": can.__version__, "cantools": cantools.__version__, "isotp": getattr(isotp, "__version__", "")},
                "platform": platform.platform(), "workspace": self.store.root,
                "project": (self.project or {}).get("name", ""), "paths": self.paths()}

    def _refuse_tx_clash(self, s: dict):
        """Keeping frames running is allowed, but never one that uses the update's own request/response IDs: it would corrupt the link."""
        try:
            ext = bool(s.get("ext"))
            ids = {udsmod.parse_hex(s["tx_id"], "Tester ID"), udsmod.parse_hex(s["rx_id"], "ECU ID")}
        except udsmod.UdsError as e:
            raise AppError(str(e))
        bad = [f"{st.msg.name} (0x{st.msg.frame_id:X})" for st in list(self.engine.tx.values())
               if st.enabled and st.msg.frame_id in ids and st.msg.ext == ext]
        bad += [f"hand-sent frame 0x{m['id_hex']}" for m in self.engine.manual_snapshot()
                if m["running"] and m["id"] in ids and m["ext"] == ext]
        if bad:
            raise AppError("These frames use the same CAN IDs as the update itself and would corrupt it: " + ", ".join(bad) +
                           ". Switch them off (Simulate) or stop them, then start the update.")

    def start_tx(self, on: bool):
        if on and not self.engine.connected:
            raise AppError("Connect to the CAN interface first.")
        if on and self.uds_run["running"] and not self.uds_settings().get("keep_tx"):
            raise AppError("A firmware update is running: the bus must stay quiet. "
                           "(Tick 'Keep simulated frames running during the update' in the Firmware parameters if you want this.)")
        if on and not self.engine.tx:
            raise AppError("No message is set to Simulate. Assign roles in Setup.")
        self.engine.start_tx(on)
        self.serial.marker("CAN TX started" if on else "CAN TX stopped")

    def estop(self):
        """Hard stop: abort any run, stop all frames."""
        self.runner.abort()
        self.engine.stop_all()
        self.serial.marker("E-STOP: all CAN TX stopped")
        if self.uds_run["running"]:
            # stopping a firmware transfer half way is the one thing that can leave an ECU without an application,
            # so the big red button deliberately does not do it: the Firmware page has an explicit Abort
            self.toast("E-STOP stopped the simulated and manual frames. The firmware update is still running - "
                       "stopping it half way can leave the ECU without firmware. Use Abort on the Firmware page "
                       "only if you really must.", "warn")
        else:
            self.toast("E-STOP — all transmission stopped.", "warn")

    # ---- signal values ------------------------------------------------------------------------------------
    def set_value(self, uid: str, value, marker: bool = True) -> dict:
        it = self.model.items.get(uid) if self.model else None
        if it is None:
            raise AppError(f"Unknown signal {uid}.")
        if it.msg.name not in self.engine.tx:
            raise AppError(f"{it.msg.name} is not set to Simulate.")
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise AppError("Value must be a number.")
        if isinstance(it, Comp):
            self.engine.set_raw(it.msg.name, it.split(value))
            shown = it.to_value(it.to_raw(value))
        else:
            raw = it.to_raw(value)
            self.engine.set_raw(it.msg.name, {it.name: raw})
            shown = it.to_value(raw)
        self.values[uid] = shown
        if marker:
            self.serial.marker(f"CAN {it.name} = {fmt_num(shown)}")
        return {"uid": uid, "value": shown, "in_range": it.in_dbc_range(shown)}

    def set_enabled(self, msg: str, on: bool):
        self.engine.set_enabled(msg, on)
        self.emit("tx", {"msg": msg, "enabled": bool(on)})

    def reset_baseline(self, save: bool = False):
        if not self.model:
            return
        base = self.project.get("baseline", {})
        for name in list(self.engine.tx):
            self.engine.set_enabled(name, True)
            m = self.model.by_name[name]
            for it in m.items():
                v = base.get(it.uid, it.default_value())
                try:
                    self.set_value(it.uid, v, marker=False)
                except AppError:
                    pass
        self.emit("values", self.values)

    def save_baseline(self):
        self.project["baseline"] = {u: v for u, v in self.values.items()
                                    if v != self.model.items[u].default_value()}
        self.save()
        self.serial.marker("Baseline saved")

    def set_auto(self, uid: str, on: bool):
        it = self.model.items.get(uid)
        if isinstance(it, Sig):
            self.engine.set_auto(it.msg.name, it.name, on)

    # ---- scenarios --------------------------------------------------------------------------------------------
    def save_scenario(self, raw: dict) -> dict:
        sc = normalise(raw)
        sid = raw.get("id", "")
        generated_ids = {g["id"] for g in self.generated}
        if sid and sid in generated_ids:
            self.project["scenario_edits"][sid] = {k: v for k, v in sc.items() if k not in ("id", "source")}
            sc["id"] = sid
        else:
            sc["source"] = "custom"
            cust = self.project["scenarios_custom"]
            for i, c in enumerate(cust):
                if c["id"] == sc["id"]:
                    cust[i] = sc
                    break
            else:
                cust.append(sc)
        self.save()
        return sc

    def delete_scenario(self, sid: str):
        p = self.project
        before = len(p["scenarios_custom"])
        p["scenarios_custom"] = [c for c in p["scenarios_custom"] if c["id"] != sid]
        if len(p["scenarios_custom"]) == before:
            p["scenarios_hidden"] = sorted(set(p["scenarios_hidden"]) | {sid})
        p["scenario_edits"].pop(sid, None)
        self.save()

    def restore_scenarios(self):
        self.project["scenarios_hidden"] = []
        self.project["scenario_edits"] = {}
        self.save()

    def capture_scenario(self, title: str, expect: str, group: str, dwell_ms: int) -> dict:
        base = self.project.get("baseline", {})
        sets = []
        for uid, v in self.values.items():
            it = self.model.items[uid]
            if v != base.get(uid, it.default_value()):
                sets.append({"uid": uid, "value": v})
        off = [n for n, st in self.engine.tx.items() if not st.enabled]
        return self.save_scenario({"title": title or "Captured state", "group": group or "Custom", "set": sets,
                                   "tx_off": off, "expect": expect, "dwell_ms": dwell_ms or 1500})

    def run(self, ids: List[str], manual: str = "prompt"):
        if not self.engine.connected or not self.engine.tx_on:
            raise AppError("Connect and press 'Start TX' before running scenarios.")
        by_id = {s["id"]: s for s in self.scenarios()}
        chosen = [by_id[i] for i in ids if i in by_id]
        try:
            self.runner.start(chosen, manual)
        except RuntimeError as e:
            raise AppError(str(e))

    # ---- results ---------------------------------------------------------------------------------------------------
    def dut_snapshot(self) -> Dict[str, object]:
        out = {}
        for name, st in self.engine.rx_snapshot().items():
            for k, v in st["values"].items():
                out[f"{name}.{k}"] = v
        return out

    def stimulus_text(self, sc: dict) -> str:
        parts = []
        for s in sc.get("set", []):
            it = self.model.items.get(s["uid"]) if self.model else None
            if it is None:
                continue
            label = it.choices.get(int(s["value"]), "") if getattr(it, "choices", None) else ""
            parts.append(f"{it.name}={fmt_num(s['value'])}" + (f" ({label})" if label else ""))
        off = sc.get("tx_off")
        if off == "ALL":
            parts.append("all TX stopped")
        elif off:
            parts.append("stopped " + ", ".join(off))
        return "; ".join(parts) or "baseline only"

    def make_result(self, sc: dict, verdict: str, mode: str, note: str, checks: List[dict]) -> dict:
        return {"id": uuid.uuid4().hex[:12], "ts": time.time(), "kind": "scenario", "scenario": sc["id"],
                "title": sc["title"], "group": sc.get("group", ""), "stimulus": self.stimulus_text(sc),
                "expected": sc.get("expect", ""), "verdict": verdict, "mode": mode, "note": note, "checks": checks,
                "dut": self.dut_snapshot(), "serial": self.serial.tail(20)}

    def record_result(self, r: dict):
        rec = self.engine.recorder_info()
        if rec.get("active"):
            r["buslog"] = rec["name"]          # the evidence: the CAN trace file that was being written when this was decided
        self.results.add(r)
        self.serial.marker(f"RESULT {r['verdict']}: {r['title']}")
        self.emit("results", {"counts": self.results.counts(), "latest": r})

    def record_manual(self, d: dict) -> dict:
        v = str(d.get("verdict", "")).upper()
        if v not in ("PASS", "FAIL", "SKIP"):
            raise AppError("Verdict must be PASS, FAIL or SKIP.")
        r = {"id": uuid.uuid4().hex[:12], "ts": time.time(), "kind": d.get("kind", "manual"), "ref": d.get("ref", ""),
             "title": str(d.get("title", ""))[:200], "group": str(d.get("group", ""))[:200],
             "stimulus": str(d.get("stimulus", ""))[:1000], "expected": str(d.get("expected", ""))[:2000],
             "verdict": v, "mode": "manual", "note": str(d.get("note", ""))[:2000], "checks": [],
             "dut": self.dut_snapshot(), "serial": self.serial.tail(20)}
        self.record_result(r)
        return r

    def report_meta(self):
        return self.project, (self.model.summary() if self.model else {}), \
            (self.model.findings if self.model else []), self.project["bus"]

    def csv_bytes(self) -> bytes:
        return to_csv(self.results.items, (self.project or {}).get("name", ""))

    def export_name(self, prefix: str, ext: str) -> str:
        """e.g. report_VARCAS_Cluster_V0.0.1_20261120_101530.html - the project name is part of the file name."""
        return f"{prefix}_{file_slug((self.project or {}).get('name', ''))}_{time.strftime('%Y%m%d_%H%M%S')}.{ext}"

    def report_bytes(self) -> bytes:
        p, s, f, b = self.report_meta()
        return html_report(p, self.results.items, s, f, b, self._report_logs())

    def _report_logs(self) -> List[dict]:
        """The bus-log files the recorded results refer to, with their SHA-256 (so the evidence can be proven unchanged)."""
        out = []
        for name in sorted({r.get("buslog") for r in self.results.items if r.get("buslog")}):
            path = buslog.safe_path(self._logs_dir(), name)
            if not path:
                continue
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            out.append({"name": name, "size": os.path.getsize(path), "sha256": h.hexdigest()})
        return out

    # ---- DUT frames defined by hand (no DBC available) ---------------------------------------------------------------------
    def define_frame(self, d: dict) -> dict:
        """Describe a frame the DUT sends. Stored in the project and written out as a real DBC so it is
        loaded exactly like any other database."""
        import re as _re
        name = str(d.get("name", "")).strip()
        if not _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,60}", name):
            raise AppError("Message name must be letters, digits and underscores (not starting with a digit).")
        try:
            fid, ext, dlc = int(d["id"]), bool(d.get("ext")), max(1, min(8, int(d.get("dlc", 8))))
        except (KeyError, ValueError):
            raise AppError("Frame ID / DLC missing.")
        sigs, used = [], {}
        for s in d.get("signals", []):
            sn = str(s.get("name", "")).strip()
            if not _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,60}", sn):
                raise AppError(f"Signal name '{sn}' is not valid.")
            try:
                start, length = int(s["start"]), int(s["length"])
            except (KeyError, ValueError):
                raise AppError(f"Signal {sn}: start bit / length missing.")
            little = bool(s.get("little", True))
            bits = signal_bits_(start, length, little)
            if length < 1 or length > 32 or max(bits) >= dlc * 8 or min(bits) < 0:
                raise AppError(f"Signal {sn} does not fit in {dlc} bytes.")
            for b in bits:
                if b in used:
                    raise AppError(f"Signal {sn} overlaps {used[b]}.")
                used[b] = sn
            sigs.append({"name": sn, "start": start, "length": length, "little": little, "signed": bool(s.get("signed")),
                         "scale": float(s.get("scale", 1) or 1), "offset": float(s.get("offset", 0) or 0),
                         "unit": str(s.get("unit", ""))[:12]})
        if not sigs:
            raise AppError("Add at least one signal.")
        if self.model:
            for m in self.model.messages:
                clash_id = (m.frame_id, m.ext) == (fid, ext) and m.name != name
                clash_nm = m.name == name and (m.frame_id, m.ext) != (fid, ext)
                if (clash_id or clash_nm) and m.name not in {f["name"] for f in self.project["custom_frames"]}:
                    raise AppError(f"{m.name} (0x{m.frame_id:X}) already exists in your DBC.")
        frames = [f for f in self.project["custom_frames"] if (f["id"], f["ext"]) != (fid, ext) and f["name"] != name]
        frames.append({"name": name, "id": fid, "ext": ext, "dlc": dlc, "period_ms": int(d.get("period_ms") or 0),
                       "signals": sigs})
        self.project["custom_frames"] = frames
        self._write_custom_dbc()
        self.reload()
        return {"name": name}

    def delete_frame(self, name: str):
        self.project["custom_frames"] = [f for f in self.project["custom_frames"] if f["name"] != name]
        self.project["roles"].pop(name, None)
        self._write_custom_dbc()
        self.reload()

    def _write_custom_dbc(self):
        import cantools
        from cantools.database.can import Message as CMsg, Signal as CSig
        from cantools.database.conversion import BaseConversion
        p, frames = self.project, self.project["custom_frames"]
        if not frames:
            if any(f["name"] == "dut_output.dbc" for f in p["files"]):
                self.store.remove_file(p, "dut_output.dbc")
            return
        msgs = []
        for f in frames:
            sigs = [CSig(name=s["name"], start=s["start"], length=s["length"],
                         byte_order="little_endian" if s["little"] else "big_endian", is_signed=s["signed"],
                         conversion=BaseConversion.factory(scale=s["scale"], offset=s["offset"], is_float=False),
                         unit=s["unit"] or None) for s in f["signals"]]
            cmt = f"Periodicity = {f['period_ms']}ms" if f.get("period_ms") else None
            msgs.append(CMsg(frame_id=f["id"], name=f["name"], length=f["dlc"], signals=sigs, comment=cmt,
                             senders=["DUT"], is_extended_frame=f["ext"], strict=False))
        db = cantools.database.Database(messages=msgs)
        path = self.store.asset_path(p["id"], "dut_output.dbc")
        cantools.database.dump_file(db, path, database_format="dbc")
        self.store.add_file(p, "dut_output.dbc", open(path, "rb").read())

    # ---- UDS / firmware ------------------------------------------------------------------------------------------------------
    def uds_settings(self) -> dict:
        stored = self.project.get("uds", {})
        if stored and stored.get("profile") != udsmod.DEFAULT_SETTINGS["profile"]:
            # saved by an older version (blank address etc.): start from the standard values once
            stored = self.project["uds"] = {}
            self.save()
        return {**udsmod.DEFAULT_SETTINGS, **stored}

    def reset_uds_settings(self):
        """Back to the standard values."""
        if self.uds_run["running"]:
            raise AppError("A firmware update is running.")
        self.project["uds"] = {}
        self.save()

    def _dut_keyword(self) -> str:
        first = (self.project.get("dut", {}).get("name") or "").split()
        return first[0] if first else ""

    def set_uds_settings(self, d: dict):
        cur = self.uds_settings()
        for k in udsmod.DEFAULT_SETTINGS:
            if k in d:
                cur[k] = d[k]
        self.project["uds"] = cur
        self.save()

    def fw_files(self) -> List[dict]:
        return [f for f in self.project["files"] if f["kind"] == "fw"]

    def fw_inspect(self, name: str) -> dict:
        if not any(f["name"] == name for f in self.fw_files()):
            raise AppError("Unknown firmware file.")
        try:
            return udsmod.inspect_firmware(self.store.asset_path(self.project["id"], name),
                                           self.uds_settings()["product_keyword"], self._dut_keyword())
        except (udsmod.UdsError, OSError) as e:
            raise AppError(f"Cannot read image: {e}")

    def _uds_session(self, settings: dict, abort=None):
        if not self.engine.connected:
            raise AppError("Connect to the CAN interface first.")
        try:
            tx, rx = udsmod.parse_hex(settings["tx_id"], "Tester TX ID"), udsmod.parse_hex(settings["rx_id"], "ECU RX ID")
        except udsmod.UdsError as e:
            raise AppError(str(e))
        link = udsmod.IsoTpLink(self.engine, tx, rx, bool(settings["ext"]))
        link.start()
        cli = udsmod.UdsClient(link, self._uds_frame_log, float(settings["p2_s"]), float(settings["p2star_s"]), abort)
        return link, cli

    def _uds_frame_log(self, d: str, text: str):
        ev = {"type": "frame", "dir": d, "text": text, "ts": time.time()}
        self._uds_event(ev)

    def _uds_event(self, ev: dict):
        t = ev["type"]
        if t in ("log", "frame"):
            self.uds_log.append(ev)
            del self.uds_log[:-600]
        elif t == "step":
            self.uds_run["steps"][str(ev["index"])] = {"state": ev["state"], "detail": ev.get("detail", "")}
        elif t == "progress":
            self.uds_run.update(pct=ev["pct"], text=ev["text"])
        elif t == "wait":
            self.uds_run["text"] = ev["text"]
        elif t == "done":
            # 'running' is cleared by the worker AFTER the result is recorded, so "not running" always means "result is in"
            self.uds_run.update(ok=ev["ok"], skipped=bool(ev.get("skipped")), summary=ev["summary"])
        self.emit("uds", ev)

    def uds_identity(self) -> dict:
        s = self.uds_settings()
        if not self.uds_lock.acquire(blocking=False):
            raise AppError("Another UDS operation is in progress.")
        try:
            link, cli = self._uds_session(s)
            try:
                items = cli.read_identity(udsmod.parse_dids(s["dids"]))
            finally:
                link.stop()
        except udsmod.UdsError as e:
            raise AppError(str(e))
        finally:
            self.uds_lock.release()
        ok = [i for i in items if not i["error"]]
        if not ok:
            hint = next((i["error"].split(" - BUT ", 1)[1] for i in items if " - BUT " in i["error"]), "")
            if hint:                                  # say it plainly instead of a row of "no value"
                raise AppError("The ECU is answering, but not in the format you set. " + hint)
        self._uds_event({"type": "log", "level": "info" if ok else "warn", "ts": time.time(),
                         "text": "Identity: " + (", ".join(f"{i['did']}={i['text'] or i['hex']}" for i in ok) or "no answer")})
        return {"items": items, "answered": len(ok)}

    SAFE_SIDS = {0x22, 0x3E, 0x19}

    def uds_raw(self, hexstr: str, expert: bool = False) -> dict:
        try:
            payload = bytes.fromhex(re_sub_ws(hexstr))
        except ValueError:
            raise AppError("Enter the request as hex bytes, e.g. 22 F1 87")
        if not payload:
            raise AppError("Empty request.")
        sid = payload[0]
        safe = sid in self.SAFE_SIDS or (sid == 0x10 and len(payload) == 2 and payload[1] in (0x01, 0x03))
        if not safe and not expert:
            raise AppError(f"Service 0x{sid:02X} can change or erase the ECU. Tick 'expert mode' to send it.")
        if self.uds_run["running"]:
            raise AppError("A firmware update is running.")
        s = self.uds_settings()
        if not self.uds_lock.acquire(blocking=False):
            raise AppError("Another UDS operation is in progress.")
        try:
            link, cli = self._uds_session(s)
            try:
                try:
                    r = cli.request(payload)
                    return {"tx": udsmod.hexs(payload), "rx": udsmod.hexs(r), "text": udsmod.as_text(r[3:]) if sid == 0x22 else "",
                            "negative": False}
                except udsmod.UdsNegative as e:
                    return {"tx": udsmod.hexs(payload), "rx": f"7F {e.sid:02X} {e.nrc:02X}", "text": str(e), "negative": True}
            finally:
                link.stop()
        except udsmod.UdsTimeout as e:
            return {"tx": udsmod.hexs(payload), "rx": "", "text": str(e), "negative": True}
        except udsmod.UdsError as e:
            raise AppError(str(e))
        finally:
            self.uds_lock.release()

    def start_flash(self, name: str, confirm: str, recovery: bool = False) -> dict:
        """recovery=True re-flashes an ECU that an earlier update left half-done (see FlashJob)."""
        p = self.project
        if confirm != "FLASH":
            raise AppError("Type FLASH to confirm.")
        if not self.engine.connected:
            raise AppError("Connect to the CAN interface first.")
        s = self.uds_settings()
        keep_tx = bool(s.get("keep_tx"))
        if self.engine.tx_on and not keep_tx:
            raise AppError("Stop transmitting first: simulated frames must not share the bus during a firmware update. "
                           "(If your ECU needs them, tick 'Keep simulated frames running during the update' in the parameters.)")
        if self.runner.state["running"]:
            raise AppError("A scenario run is in progress.")
        if self.uds_run["running"]:
            raise AppError("A firmware update is already running.")
        if self.engine.manual_active() and not keep_tx:
            raise AppError("Frames are being sent by hand (Send page): stop them first - they must not share the bus during an update.")
        if keep_tx:
            self._refuse_tx_clash(s)
        if not str(s["address"]).strip():
            raise AppError("Set the memory address (it is never guessed).")
        info = self.fw_inspect(name)
        s["product_keyword"] = info.get("keyword_used", "")   # typed keyword, else what the image says it is for
        if info["product"]["status"] != "ok" and not s["allow_product_mismatch"]:
            raise AppError("Product check failed: " + info["product"]["text"] +
                           " Set the product keyword correctly, or tick 'allow product mismatch' if you are sure.")
        image, _start, gaps = udsmod.load_image(self.store.asset_path(p["id"], name))
        if not image:
            raise AppError("Firmware image is empty.")
        if not self.uds_lock.acquire(blocking=False):
            raise AppError("Another UDS operation is in progress.")
        self.uds_log.clear()
        self.uds_run = {"running": True, "steps": {}, "pct": 0, "text": "", "summary": "", "ok": None}
        job = udsmod.FlashJob(self.engine, s, image, info, self._uds_event, recovery=bool(recovery))
        self.flash = job
        with_tx = keep_tx and (self.engine.tx_on or self.engine.manual_active())
        s["_tx_kept"] = bool(with_tx)       # remembered for the report only (not a setting)
        self.serial.marker(f"FIRMWARE UPDATE started: {name}" + (" (RECOVERY re-flash)" if recovery else "") +
                           (" - simulated/manual frames KEPT RUNNING (operator's choice)" if with_tx else ""))

        def work():
            try:
                res = job.run()
            except Exception as e:  # never leave the lock held
                res = {"ok": False, "summary": f"Internal error: {e}", "steps": [], "old": {}, "new": {}, "crc32": info["crc32"],
                       "size": len(image), "recovery": ""}
                self._uds_event({"type": "done", "ok": False, "summary": res["summary"]})
            finally:
                self.uds_lock.release()
            old = " ".join(str(v) for v in res["old"].values()) or "?"
            new = " ".join(str(v) for v in res["new"].values()) or "?"
            try:
                self._remember_recovery(name, res, bool(recovery))
                self._record_flash_result(name, res, s, recovery, old, new)
            finally:
                self.uds_run["running"] = False
        threading.Thread(target=work, name="flash", daemon=True).start()
        return {"started": True, "gaps": gaps}

    def _record_flash_result(self, name, res, s, recovery, old, new):
        if True:
            self.record_result({
                "id": uuid.uuid4().hex[:12], "ts": time.time(), "kind": "firmware", "title": f"Firmware update {name}",
                "group": "Firmware (UDS)", "stimulus": f"{name} · {res['size']} B · CRC32 {res['crc32']} · @ {s['address']}" +
                                                       (" · RECOVERY re-flash" if recovery else "") +
                                                       (" · simulated frames kept running during the update" if s.get("_tx_kept") else ""),
                "expected": "All UDS steps succeed, the CRC is accepted and the ECU reports the new version.",
                "verdict": "SKIP" if res.get("skipped") else ("PASS" if res["ok"] else "FAIL"), "mode": "auto",
                "note": res["summary"] + ("" if res.get("skipped") else f"  [before: {old} | after: {new}]") +
                        (f"  CAN error frames during the update: {res['bus_errors']}." if res.get("bus_errors") else "") +
                        ("  Note: simulated frames were running on the bus during this update (operator's choice)." if s.get("_tx_kept") else ""),
                "checks": [{"desc": x["desc"], "ok": x["ok"], "detail": x["detail"]} for x in res["steps"]],
                "dut": {}, "serial": self.serial.tail(10)})
            self.serial.marker(f"FIRMWARE UPDATE {'OK' if res['ok'] else 'FAILED'}: {name}")

    def abort_flash(self):
        if self.flash and self.uds_run["running"]:
            self.serial.marker("FIRMWARE UPDATE: abort requested by the operator")
            self.flash.abort_evt.set()

    def _remember_recovery(self, image: str, res: dict, was_recovery: bool):
        """An update that stopped half way is written into the project (so it survives closing the program): the next person
        who opens it is told that the ECU may need a recovery re-flash. A later successful update clears it."""
        p = self.project
        if not p:
            return
        level = res.get("recovery", "")
        if res.get("ok"):
            p["flash_recovery"] = None
        elif level:
            p["flash_recovery"] = {"level": level, "image": image, "ts": time.time(), "summary": res.get("summary", ""),
                                   "aborted": bool(res.get("aborted")), "was_recovery": was_recovery}
        else:
            return
        self.save()
        self.emit("recovery", p["flash_recovery"])

    def dismiss_recovery(self):
        if self.uds_run["running"]:
            raise AppError("A firmware update is running.")
        self.project["flash_recovery"] = None
        self.save()
        self.emit("recovery", None)

    # ---- snapshots for the UI ------------------------------------------------------------------------------------------
    def state(self) -> dict:
        p = self.project
        base = {"version": __version__, "projects": self.projects(), "interfaces": INTERFACES,
                "serial_available": self.serial.available}
        if p is None or self.model is None:
            return {**base, "project": None}
        msgs = []
        for m in self.model.messages:
            d = m.to_dict()
            d["role"] = p["roles"].get(m.name, "sim")
            d["tx_period_ms"] = p["periods"].get(m.name, m.cycle_ms)
            msgs.append(d)
        sc = self.scenarios()
        return {
            **base,
            "project": {k: p[k] for k in ("id", "name", "bus", "files", "dut", "operator", "notes", "composites",
                                          "baseline")},
            "model": {"summary": self.model.summary(), "messages": msgs, "findings": self.model.findings},
            "docs": [{"name": d["name"], "kind": d["kind"], "error": d["error"], "blocks": len(d["blocks"])}
                     for d in self.docs],
            "analysis": self.analysis,
            "tables": self.tables,
            "values": self.values,
            "scenarios": sc,
            "results": self.results.items[-2000:],
            "auto": {n: self.engine.auto_signals(n) for n in self.engine.tx},
            "run": dict(self.runner.state),
            "custom_frames": p["custom_frames"],
            "uds": {"settings": self.uds_settings(), "files": self.fw_files(), "run": dict(self.uds_run),
                    "log": self.uds_log[-300:], "steps": udsmod.STEPS, "recovery": p.get("flash_recovery")},
            "manual_frames": p.get("manual_frames", []),
            "buslog": self.buslog_state(),
        }

    def live(self) -> dict:
        e = self.engine
        return {
            "t": time.time(),
            "conn": {"connected": e.connected, "tx_on": e.tx_on, "bus_state": e.bus_state(), "bus_ok": e.bus_ok,
                     "bitrate": e.bitrate},
            "stats": {"tx_total": e.tx_total, "rx_total": e.rx_total, "errors": e.err_count, "load": round(e.bus_load(), 1),
                      "unknown_ids": len(e.unknown_ids)},
            "tx": e.tx_snapshot(), "rx": e.rx_snapshot(), "run": dict(self.runner.state),
            "unknown": e.unknown_snapshot(),
            "manual": e.manual_snapshot(),
            "buslog": e.recorder_info(),
            "counts": self.results.counts() if self.results else {},
            # only a RECENT problem is shown in red; the counter keeps the total for this connection
            "error": e.last_err if time.time() - e.last_err_t < 15 else "",
        }

    def trace_batch(self):
        rows, dropped = self.engine.trace_since(self._trace_seq, 300)
        out = []
        model = self.model
        for seq, ts, d, fid, ext, data, dlc in rows:
            self._trace_seq = max(self._trace_seq, seq)
            name, dec = "", ""
            if model and d != "ERR":
                m = model.msg_for_frame(fid, ext)
                if m:
                    name = m.name
                    try:
                        dec = " ".join(f"{k}={fmt_num(v)}" for k, v in m.decode_values(data).items())[:200]
                    except Exception:
                        dec = ""
            out.append([seq, round(ts, 3), d, f"{fid:08X}" if ext else f"{fid:03X}", dlc, data.hex(" ").upper(),
                        name, dec])
        if dropped:
            self._trace_seq = max(self._trace_seq, self.engine.trace_seq)
        return out, dropped

    def serial_batch(self):
        rows, self._serial_pending = self._serial_pending, []
        return rows[-500:]

    def shutdown(self):
        try:
            self.runner.abort()
            self.engine.stop_all()
            self.serial.close()
            self.engine.shutdown()
        except Exception:
            pass
