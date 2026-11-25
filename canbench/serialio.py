"""Debug-UART logger: line stream with timestamps, markers and regex lookback."""
from __future__ import annotations

import collections
import threading
import time
from typing import Callable, List, Optional, Tuple

try:
    import serial
    import serial.tools.list_ports
except ImportError:  # the Serial tab degrades gracefully
    serial = None


class SerialPort:
    def __init__(self, on_line: Optional[Callable[[float, str, str], None]] = None):
        self.ser = None
        self.lines = collections.deque(maxlen=20000)  # (ts, kind, text)
        self.on_line = on_line
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.error = ""
        self.port = ""
        self.baud = 0

    @property
    def available(self) -> bool:
        return serial is not None

    @property
    def is_open(self) -> bool:
        return self.ser is not None

    def ports(self) -> List[dict]:
        if serial is None:
            return []
        return [{"device": p.device, "description": p.description} for p in serial.tools.list_ports.comports()]

    def open(self, port: str, baud: int):
        if serial is None:
            raise RuntimeError("pyserial is not installed (pip install pyserial).")
        self.close()
        self.ser = serial.Serial(port, int(baud), timeout=0.1)
        self.port, self.baud, self.error = port, int(baud), ""
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, args=(self.ser,), name="serial", daemon=True)
        self._thread.start()
        self._add("MARK", f"opened {port} @ {baud}")

    def close(self):
        self._stop.set()
        ser, self.ser = self.ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
            self._add("MARK", "closed")

    def write(self, text: str, eol: str = "\r\n"):
        if self.ser is None:
            raise RuntimeError("Serial port is not open.")
        self.ser.write((text + eol).encode("utf-8", "replace"))
        self._add("TX", text)

    def marker(self, text: str):
        self._add("MARK", text)

    def _add(self, kind: str, text: str):
        ts = time.time()
        self.lines.append((ts, kind, text))
        if self.on_line:
            self.on_line(ts, kind, text)

    def lines_since(self, t0: float) -> List[Tuple[float, str]]:
        return [(ts, tx) for ts, kind, tx in list(self.lines) if ts >= t0 and kind == "RX"]

    def tail(self, n: int = 40) -> List[str]:
        return [f"{time.strftime('%H:%M:%S', time.localtime(ts))} {tx}" for ts, kind, tx in list(self.lines)[-n:]]

    def _loop(self, ser):
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = ser.read(ser.in_waiting or 1)
            except Exception as e:
                self.error = f"serial read failed: {e}"
                self._add("ERR", self.error)
                self.ser = None
                return
            if not chunk:
                if buf:
                    self._add("RX", buf.decode("utf-8", "replace"))
                    buf = b""
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self._add("RX", line.rstrip(b"\r").decode("utf-8", "replace"))
