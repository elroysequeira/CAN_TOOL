"""One CANBench per PC.

Two copies would fight over the CAN adapter (a PCAN channel cannot be opened twice - the second one fails or, worse, both see
half the traffic), and two windows on the same project overwrite each other's settings. So the second launch does not start a
second bench: it finds the first one and opens its window instead.

The first instance writes a small file in the system temp folder (pid + port). A later launch reads it, asks that port
`/api/ping` and only believes the answer if it really is CANBench; a file left behind by a crash is ignored and replaced.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.request
from typing import Optional

from . import APP_NAME

LOCK_NAME = "CANBench.instance.json"


def lock_path() -> str:
    return os.path.join(tempfile.gettempdir(), LOCK_NAME)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            k32.CloseHandle(h)
            return bool(ok) and code.value == 259            # STILL_ACTIVE
        except Exception:
            return True                                       # cannot tell: assume it is alive rather than start a twin
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def ping(port: int, timeout: float = 1.5) -> Optional[dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/api/ping", timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
        return d if d.get("app") == APP_NAME else None
    except Exception:
        return None


def find_running(path: Optional[str] = None) -> Optional[dict]:
    """The other live CANBench ({'pid','port','url',...}) or None. A stale lock file does not count."""
    path = path or lock_path()
    try:
        with open(path, encoding="utf-8") as f:
            info = json.load(f)
        pid, port = int(info.get("pid", 0)), int(info.get("port", 0))
    except (OSError, ValueError, TypeError):
        return None
    if pid == os.getpid() or not pid_alive(pid):
        return None
    if port:
        d = ping(port)
        if d is None:
            return None
        return {"pid": pid, "port": port, "url": f"http://127.0.0.1:{port}/", "version": d.get("version", "")}
    # the other process is alive but has not published its port yet (it is just starting)
    if time.time() - float(info.get("started", 0)) < 20:
        return {"pid": pid, "port": 0, "url": "", "version": ""}
    return None


def claim(port: int, path: Optional[str] = None):
    path = path or lock_path()
    tmp = f"{path}.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "port": int(port), "started": time.time()}, f)
    os.replace(tmp, path)


def release(path: Optional[str] = None):
    path = path or lock_path()
    try:
        with open(path, encoding="utf-8") as f:
            if int(json.load(f).get("pid", 0)) != os.getpid():
                return                                         # not ours any more: leave it
        os.remove(path)
    except (OSError, ValueError, TypeError):
        pass
