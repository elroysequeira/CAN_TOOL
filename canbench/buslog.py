"""Bus recorder: every frame the bench sends or receives (and every error frame) goes into a Vector ASC file
(opens in CANalyzer, PCAN-View, BUSMASTER ... and python-can reads it back).

One file per connection, in <project>/logs/. Writing happens on its own thread so a busy bus (a firmware transfer, an
adapter flooding after a missing ACK) can never slow the transmit/receive loops; if the writer ever falls behind the frames are
counted as dropped (and said so), not silently lost.
"""
from __future__ import annotations

import os
import queue
import re
import threading
import time
from typing import List, Optional

import can

NAME_RE = re.compile(r"canlog_[A-Za-z0-9._-]{1,120}\.asc")
MAX_FILE_BYTES = 64 * 1024 * 1024          # a long session rolls over to the next part instead of growing without bound


def log_dir(project_dir: str) -> str:
    return os.path.join(project_dir, "logs")


class BusRecorder:
    def __init__(self, directory: str, stem: str = ""):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)
        base = time.strftime("%Y%m%d_%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")[:40]
        self.stem = f"canlog_{base}" + (f"_{safe}" if safe else "")
        self.part = 1
        self.q: "queue.Queue" = queue.Queue(maxsize=300_000)
        self.dropped = 0
        self.frames = 0
        self.bytes = 0
        self.path = ""
        self._fh = None
        self._writer = None
        self._stop = threading.Event()
        self._open_part()
        self._t = threading.Thread(target=self._run, name="buslog", daemon=True)
        self._t.start()

    # ---- file handling ---------------------------------------------------------------------
    def _open_part(self):
        self._close_part()
        name = f"{self.stem}.asc" if self.part == 1 else f"{self.stem}_part{self.part}.asc"
        self.path = os.path.join(self.dir, name)
        self._fh = open(self.path, "w", encoding="utf-8", newline="\n")
        self._writer = can.ASCWriter(self._fh)

    def _close_part(self):
        if self._writer is not None:
            try:
                self._writer.stop()          # also closes the file it was given
            except Exception:
                pass
        elif self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
        self._writer = self._fh = None

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    # ---- producer side (called from the TX / RX threads: must stay cheap) -----------------------
    def add(self, direction: str, frame):
        try:
            self.q.put_nowait(("f", direction, frame))
        except queue.Full:
            self.dropped += 1

    def event(self, text: str):
        try:
            self.q.put_nowait(("e", text, time.time()))
        except queue.Full:
            pass

    # ---- writer thread -----------------------------------------------------------------------
    def _run(self):
        last_flush = time.time()
        while True:
            try:
                item = self.q.get(timeout=0.25)
            except queue.Empty:
                item = None
            if item is not None:
                try:
                    self._write(item)
                except Exception:
                    self.dropped += 1
            if self._fh is not None and time.time() - last_flush > 0.5:
                try:
                    self._fh.flush()
                except Exception:
                    pass
                last_flush = time.time()
            if self._stop.is_set() and self.q.empty():
                break

    def _write(self, item):
        if item[0] == "e":
            self._writer.log_event(item[1], item[2])
            return
        _k, direction, f = item
        if direction == "ERR":
            msg = can.Message(timestamp=f.timestamp or time.time(), is_error_frame=True, channel=1)
        else:
            msg = can.Message(timestamp=f.timestamp or time.time(), arbitration_id=f.arbitration_id,
                              is_extended_id=bool(f.is_extended_id), data=bytes(f.data), dlc=f.dlc, is_rx=(direction != "TX"),
                              channel=1)
        self._writer.on_message_received(msg)
        self.frames += 1
        self.bytes += 40 + 3 * len(msg.data)
        if self.bytes > MAX_FILE_BYTES:
            self.part += 1
            self.bytes = 0
            self._open_part()

    def stop(self):
        self._stop.set()
        self._t.join(timeout=5)
        if self.dropped:
            try:
                self._writer.log_event(f"WARNING: {self.dropped} frame(s) could not be written (disk too slow)")
            except Exception:
                pass
        self._close_part()


# ---- listing / safe access (UI) -------------------------------------------------------------
def list_logs(directory: str, active: Optional[str] = None) -> List[dict]:
    out = []
    if not os.path.isdir(directory):
        return out
    for n in sorted(os.listdir(directory), reverse=True):
        if not NAME_RE.fullmatch(n):
            continue
        p = os.path.join(directory, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({"name": n, "size": st.st_size, "mtime": st.st_mtime, "active": n == active})
    return out


def safe_path(directory: str, name: str) -> Optional[str]:
    """The file for `name` inside `directory`, or None when the name is not a bench log (no path tricks)."""
    if not NAME_RE.fullmatch(str(name or "")):
        return None
    p = os.path.join(directory, name)
    return p if os.path.isfile(p) else None
