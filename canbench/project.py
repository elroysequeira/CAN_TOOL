"""Project persistence. One folder per project:

    workspace/<id>/project.json   settings, roles, baseline, custom scenarios
    workspace/<id>/assets/        copies of the DBC + documents (reproducible evidence)
    workspace/<id>/results.jsonl  every verdict, appended the moment it is recorded
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Dict, List, Optional

from .dbcmodel import sha256_file
from .docs import DOC_EXTS

DEFAULT_BUS = {"interface": "pcan", "channel": "PCAN_USBBUS1", "bitrate": 500000}


def slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "file"
    return s[:80]


def kind_of(filename: str) -> Optional[str]:
    ext = os.path.splitext(filename)[1].lower()
    if ext in (".dbc", ".kcd", ".sym", ".arxml"):
        return "dbc"
    if ext in (".bin", ".srec", ".s19"):
        return "fw"
    if ext in DOC_EXTS:
        return "doc"
    return None


class ProjectStore:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    def dir(self, pid: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,80}", pid) or ".." in pid:
            raise ValueError("bad project id")
        return os.path.join(self.root, pid)

    def list(self) -> List[dict]:
        out = []
        for pid in sorted(os.listdir(self.root)):
            if not re.fullmatch(r"[A-Za-z0-9_.-]{3,80}", pid):
                continue
            f = os.path.join(self.root, pid, "project.json")
            if os.path.isfile(f):
                try:
                    p = json.load(open(f, encoding="utf-8"))
                    out.append({"id": pid, "name": p.get("name", pid), "created": p.get("created", 0)})
                except (OSError, ValueError):
                    continue
        return sorted(out, key=lambda x: -x["created"])

    def create(self, name: str) -> dict:
        # the id is a folder name: letters, digits and '-' only (a name like "Cluster V0.0.1" must work)
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30].strip("-") or "project"
        pid = base + "-" + uuid.uuid4().hex[:5]
        p = {"id": pid, "name": name.strip() or "Untitled project", "created": time.time(), "bus": dict(DEFAULT_BUS),
             "files": [], "roles": {}, "periods": {}, "baseline": {}, "scenarios_custom": [], "scenario_edits": {},
             "scenarios_hidden": [], "dut": {"name": "", "serial": "", "firmware": ""}, "operator": "", "notes": "",
             "composites": True}
        os.makedirs(os.path.join(self.dir(pid), "assets"), exist_ok=True)
        self.save(p)
        return p

    def load(self, pid: str) -> dict:
        with open(os.path.join(self.dir(pid), "project.json"), encoding="utf-8") as f:
            p = json.load(f)
        for k, v in (("bus", dict(DEFAULT_BUS)), ("files", []), ("roles", {}), ("periods", {}), ("baseline", {}),
                     ("scenarios_custom", []), ("scenario_edits", {}), ("scenarios_hidden", []),
                     ("dut", {"name": "", "serial": "", "firmware": ""}), ("operator", ""), ("notes", ""),
                     ("composites", True), ("custom_frames", []), ("uds", {}), ("record_buslog", True), ("manual_frames", []),
                     ("flash_recovery", None)):
            p.setdefault(k, v)
        return p

    def save(self, p: dict):
        d = self.dir(p["id"])
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, "project.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(p, f, indent=2)
        os.replace(tmp, os.path.join(d, "project.json"))  # atomic: never a half-written project

    def delete(self, pid: str):
        import shutil
        shutil.rmtree(self.dir(pid), ignore_errors=True)

    def asset_path(self, pid: str, name: str) -> str:
        return os.path.join(self.dir(pid), "assets", name)

    def add_file(self, p: dict, filename: str, data: bytes) -> dict:
        kind = kind_of(filename)
        if kind is None:
            raise ValueError("Unsupported file type. Use a CAN database (.dbc .kcd .sym .arxml), a document (.docx .xlsx .csv .md .txt .pdf) "
                             "or a firmware image (.bin .srec).")
        if len(data) > 50 * 1024 * 1024:
            raise ValueError("File too large (50 MB max).")
        name = slug(filename)
        path = self.asset_path(p["id"], name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        entry = {"name": name, "kind": kind, "sha256": sha256_file(path), "added": time.time(), "size": len(data)}
        p["files"] = [f for f in p["files"] if f["name"] != name] + [entry]
        self.save(p)
        return entry

    def remove_file(self, p: dict, name: str):
        p["files"] = [f for f in p["files"] if f["name"] != name]
        try:
            os.remove(self.asset_path(p["id"], slug(name)))
        except OSError:
            pass
        self.save(p)
