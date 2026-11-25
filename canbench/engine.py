"""CAN engine: periodic transmit scheduler + receive/decode, all on background threads.

No UI code in here. Everything that crosses to the UI goes through plain
snapshot() dicts, so the engine can also be driven headless (tests, scripts).
"""
from __future__ import annotations

import collections
import sys
import threading
import time
from typing import Dict, List, Optional

import can

from .buslog import BusRecorder
from .dbcmodel import Model, Msg, Comp, Sig, clean_num

INTERFACES = ["pcan", "virtual", "kvaser", "vector", "socketcan", "ixxat", "slcan", "gs_usb"]


def _timer_resolution(on: bool):
    """Windows' default 15 ms timer tick wrecks 10 ms CAN periods; ask for 1 ms."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        (ctypes.windll.winmm.timeBeginPeriod if on else ctypes.windll.winmm.timeEndPeriod)(1)
    except Exception:
        pass


class TxState:
    """Transmit state of one simulated message."""

    def __init__(self, msg: Msg, period_ms: int):
        self.msg = msg
        self.raw: Dict[str, int] = msg.default_raw()
        self.period = max(5, int(period_ms)) / 1000.0
        self.enabled = True
        self.next_due = 0.0
        self.count = 0
        self.max_late = 0.0
        self.auto = {s.name for s in msg.signals if s.is_counter and not s.hidden_by}
        self.once = False

    def frame(self) -> can.Message:
        return can.Message(arbitration_id=self.msg.frame_id, is_extended_id=self.msg.ext,
                           data=self.msg.encode(self.raw), dlc=self.msg.length, is_fd=False)


class ManualTx:
    """A frame the tester typed in by hand (any ID, any bytes), sent periodically for a while. Independent of the DBC."""

    MAX = 16

    def __init__(self, arb_id: int, ext: bool, data: bytes, period_ms: int, duration_s: float, count: int, label: str = ""):
        self.id, self.ext, self.data, self.label = arb_id, bool(ext), bytes(data), label
        self.period = max(2, int(period_ms)) / 1000.0
        self.duration, self.max_count = float(duration_s or 0), int(count or 0)
        self.sent = 0
        self.running = True
        self.error = ""
        self.next_due = 0.0
        self.t_start = time.perf_counter()
        self.end_pc = self.t_start + self.duration if self.duration > 0 else 0.0
        self.why_done = ""

    @property
    def key(self) -> str:
        return f"{'x' if self.ext else 's'}{self.id:X}"

    def finish(self, why: str):
        self.running = False
        self.why_done = why

    def frame(self) -> can.Message:
        return can.Message(arbitration_id=self.id, is_extended_id=self.ext, data=self.data, dlc=len(self.data), is_fd=False)


class RxState:
    def __init__(self, msg: Msg):
        self.msg = msg
        self.values: Dict[str, object] = {}
        self.data = b""
        self.last = 0.0
        self.count = 0
        self.intervals = collections.deque(maxlen=20)


class UnknownState:
    """A frame ID on the bus that no loaded DBC describes (e.g. a DUT output with no DBC yet)."""

    def __init__(self, fid: int, ext: bool):
        self.fid, self.ext = fid, ext
        self.count = 0
        self.first = self.last = 0.0
        self.data = b""
        self.dlc = 0
        self.intervals = collections.deque(maxlen=20)
        self.changed = [0.0] * 8  # time each byte last changed

    def update(self, frame, now: float):
        data = bytes(frame.data)
        if self.count:
            self.intervals.append(now - self.last)
            for i, b in enumerate(data[:8]):
                if i >= len(self.data) or self.data[i] != b:
                    self.changed[i] = now
        else:
            self.first = now
        self.count += 1
        self.last, self.data, self.dlc = now, data, frame.dlc or len(data)


class Engine:
    def __init__(self):
        self.send_lock = threading.Lock()
        self.taps: List = []  # callbacks(frame) for every received data frame (used by UDS)
        self.unknown: Dict[tuple, UnknownState] = {}
        self.lock = threading.RLock()
        self.model: Optional[Model] = None
        self.bus: Optional[can.BusABC] = None
        self.tx: Dict[str, TxState] = {}
        self.rx: Dict[str, RxState] = {}
        self.tx_on = False
        self.manual: Dict[str, ManualTx] = {}
        self.recorder: Optional[BusRecorder] = None
        self.trace_tx = True
        self.trace = collections.deque(maxlen=20000)
        self.trace_seq = 0
        self.unknown_ids: Dict[int, int] = collections.defaultdict(int)
        self.tx_total = self.rx_total = self.err_count = 0
        self.last_err = ""
        self.last_err_t = 0.0
        self.last_rx_err = 0.0
        self.bus_ok = True
        self._bits_window = collections.deque()  # (t, bits)
        self._stop = threading.Event()
        self.bitrate = 0
        self._listeners: List = []  # callbacks(msg_name, values) for rx
        _timer_resolution(True)
        self._threads = [threading.Thread(target=self._tx_loop, name="can-tx", daemon=True),
                         threading.Thread(target=self._rx_loop, name="can-rx", daemon=True)]
        for t in self._threads:
            t.start()

    # ---- configuration -----------------------------------------------------------
    def configure(self, model: Optional[Model], roles: Dict[str, str], periods: Dict[str, int],
                  keep_values: Optional[Dict[str, Dict[str, int]]] = None):
        """(Re)build TX/RX tables. roles: msg name -> sim | listen | ignore."""
        with self.lock:
            old_tx = self.tx
            self.unknown.clear()  # frames may now be described by the DBC
            self.model = model
            self.tx, self.rx = {}, {}
            if model is None:
                return
            for m in model.messages:
                role = roles.get(m.name, "ignore")
                if role == "sim":
                    st = TxState(m, periods.get(m.name, m.cycle_ms))
                    if keep_values and m.name in keep_values:
                        st.raw.update({k: v for k, v in keep_values[m.name].items() if k in st.raw})
                    elif m.name in old_tx and old_tx[m.name].msg.cm is m.cm:
                        st.raw = old_tx[m.name].raw
                        st.enabled = old_tx[m.name].enabled
                    self.tx[m.name] = st
                elif role == "listen":
                    self.rx[m.name] = RxState(m)

    # ---- bus ---------------------------------------------------------------------
    def open(self, interface: str, channel: str, bitrate: int, log_dir: Optional[str] = None, log_stem: str = ""):
        """log_dir: record every frame of this connection there (one .asc file); None = do not record."""
        self.close()
        if interface == "virtual":
            bus = can.Bus(interface="virtual", channel=channel or "canbench", receive_own_messages=False)
        else:
            bus = can.Bus(interface=interface, channel=channel, bitrate=int(bitrate))
        self.bitrate = int(bitrate)
        self.bus_ok = True
        self.last_err = ""
        self.last_err_t = 0.0
        self.tx_total = self.rx_total = self.err_count = 0   # counters describe THIS connection, not the whole app run
        self.unknown_ids.clear()
        if log_dir:
            try:
                self.recorder = BusRecorder(log_dir, log_stem)
                self.recorder.event(f"CANBench connected {interface} {channel} @ {int(bitrate)} bit/s")
            except OSError as e:                      # a full or read-only disk must not stop testing: say so and carry on
                self.recorder = None
                self._note_err(f"bus log not recorded: {e}")
        self.bus = bus

    def close(self):
        self.tx_on = False
        with self.lock:
            self.manual.clear()
        bus, self.bus = self.bus, None
        if bus is not None:
            time.sleep(0.05)
            try:
                bus.shutdown()
            except Exception:
                pass
        rec, self.recorder = self.recorder, None
        if rec is not None:
            rec.event("CANBench disconnected")
            rec.stop()

    def record_event(self, text: str):
        """A marker line in the bus log (e.g. 'FIRMWARE UPDATE started')."""
        rec = self.recorder
        if rec is not None:
            rec.event(text)

    def recorder_info(self) -> dict:
        rec = self.recorder
        if rec is None:
            return {"active": False}
        return {"active": True, "name": rec.name, "frames": rec.frames, "dropped": rec.dropped}

    def shutdown(self):
        self._stop.set()
        self.close()
        _timer_resolution(False)

    @property
    def connected(self) -> bool:
        return self.bus is not None

    def bus_state(self) -> str:
        try:
            st = getattr(self.bus, "state", None)
            return st.name if st is not None else ""
        except Exception:
            return ""

    # ---- tx control -----------------------------------------------------------------
    def set_raw(self, msg: str, raws: Dict[str, int]):
        with self.lock:
            st = self.tx.get(msg)
            if st:
                st.raw.update(raws)

    def get_raw(self, msg: str) -> Dict[str, int]:
        with self.lock:
            st = self.tx.get(msg)
            return dict(st.raw) if st else {}

    def set_enabled(self, msg: str, on: bool):
        with self.lock:
            st = self.tx.get(msg)
            if st:
                if on and not st.enabled:
                    st.next_due = 0.0
                st.enabled = bool(on)

    def set_period(self, msg: str, ms: int):
        with self.lock:
            st = self.tx.get(msg)
            if st:
                st.period = max(5, int(ms)) / 1000.0

    def set_auto(self, msg: str, signal: str, on: bool):
        with self.lock:
            st = self.tx.get(msg)
            if st:
                (st.auto.add if on else st.auto.discard)(signal)

    def auto_signals(self, msg: str):
        with self.lock:
            st = self.tx.get(msg)
            return sorted(st.auto) if st else []

    def send_once(self, msg: str):
        with self.lock:
            st = self.tx.get(msg)
            if st:
                st.once = True

    def start_tx(self, on: bool):
        with self.lock:
            for st in self.tx.values():
                st.next_due = 0.0
            self.tx_on = bool(on) and self.bus is not None

    def stop_all(self):
        """E-stop: nothing more leaves the bench (simulated frames and hand-sent repeating frames)."""
        self.tx_on = False
        self.stop_manual()

    # ---- manual frames (any ID, typed by the tester) -----------------------------------------------
    def start_manual(self, arb_id: int, ext: bool, data: bytes, period_ms: int = 0, duration_s: float = 0.0, count: int = 0,
                     label: str = "") -> Optional[str]:
        """period_ms == 0: send once now. Otherwise repeat every period_ms until `duration_s` seconds or `count` frames have
        passed (0 = until stopped). Returns the key of the repeating entry (None for a single frame)."""
        if self.bus is None:
            raise RuntimeError("CAN interface is not connected.")
        if arb_id < 0 or arb_id > (0x1FFFFFFF if ext else 0x7FF):
            raise ValueError(f"ID 0x{arb_id:X} does not fit in {'29' if ext else '11'} bits.")
        if len(data) > 8:
            raise ValueError("A classic CAN frame carries at most 8 data bytes.")
        if not period_ms:
            self.send_frame(arb_id, bytes(data), bool(ext))
            return None
        m = ManualTx(arb_id, ext, bytes(data), period_ms, duration_s, count, label)
        with self.lock:
            if m.key not in self.manual and len(self.manual) >= ManualTx.MAX:
                raise ValueError(f"At most {ManualTx.MAX} repeating frames at once - stop one first.")
            self.manual[m.key] = m
        return m.key

    def stop_manual(self, key: Optional[str] = None):
        with self.lock:
            targets = list(self.manual.values()) if key is None else [self.manual[key]] if key in self.manual else []
            for m in targets:
                if m.running:
                    m.finish("stopped")
            if key is not None:
                self.manual.pop(key, None)

    def clear_finished_manual(self):
        with self.lock:
            for k in [k for k, m in self.manual.items() if not m.running]:
                del self.manual[k]

    def manual_active(self) -> bool:
        with self.lock:
            return any(m.running for m in self.manual.values())

    def manual_snapshot(self) -> List[dict]:
        with self.lock:
            return [{"key": m.key, "id": m.id, "ext": m.ext, "id_hex": f"{m.id:08X}" if m.ext else f"{m.id:03X}",
                     "data": list(m.data), "period_ms": round(m.period * 1000), "sent": m.sent, "running": m.running,
                     "left_s": round(max(0.0, m.end_pc - time.perf_counter()), 1) if m.running and m.end_pc else None,
                     "max_count": m.max_count, "why": m.why_done, "error": m.error, "label": m.label}
                    for m in self.manual.values()]

    # ---- loops -------------------------------------------------------------------------
    def _note_err(self, text: str):
        self.err_count += 1
        self.last_err = text
        self.last_err_t = time.time()

    def _tx_loop(self):
        while not self._stop.is_set():
            bus = self.bus
            if bus is None:
                time.sleep(0.05)
                continue
            now = time.perf_counter()
            batch = []
            mbatch = []
            with self.lock:
                for m in self.manual.values():
                    if not m.running:
                        continue
                    if (m.end_pc and now >= m.end_pc) or (m.max_count and m.sent >= m.max_count):
                        m.finish("finished")
                        continue
                    if now >= m.next_due:
                        mbatch.append(m)
                        nxt = m.next_due + m.period
                        m.next_due = nxt if nxt > now - m.period else now + m.period
                for st in self.tx.values():
                    due = self.tx_on and st.enabled and now >= st.next_due
                    if not (due or st.once):
                        continue
                    try:
                        batch.append((st, st.frame()))
                    except Exception as e:  # never let one bad frame stop the others
                        self._note_err(f"encode {st.msg.name}: {e}")
                    if st.once:
                        st.once = False
                    else:
                        st.max_late = max(st.max_late * 0.99, now - st.next_due if st.next_due else 0.0)
                        nxt = st.next_due + st.period
                        st.next_due = nxt if nxt > now - st.period else now + st.period
                        for name in st.auto:  # rolling counters
                            sg = st.msg.by_name[name]
                            hi = sg.raw_range[1] + 1
                            st.raw[name] = (st.raw.get(name, 0) + 1) % hi
            for st, frame in batch:
                try:
                    with self.send_lock:
                        bus.send(frame, timeout=0.05)
                    st.count += 1
                    self.tx_total += 1
                    self._count_bits(frame)
                    frame.timestamp = time.time()
                    self._trace("TX", frame)
                except Exception as e:
                    self._note_err(f"send {st.msg.name}: {e}")
            for m in mbatch:
                frame = m.frame()
                try:
                    with self.send_lock:
                        bus.send(frame, timeout=0.05)
                    m.sent += 1
                    self.tx_total += 1
                    self._count_bits(frame)
                    frame.timestamp = time.time()
                    self._trace("TX", frame)
                except Exception as e:
                    m.error = str(e)
                    m.finish("error")
                    self._note_err(f"send 0x{m.id:X}: {e}")
            time.sleep(0.001)

    def _rx_loop(self):
        while not self._stop.is_set():
            bus = self.bus
            if bus is None:
                time.sleep(0.05)
                continue
            try:
                frame = bus.recv(0.1)
            except Exception as e:
                if self.bus is bus:
                    self._note_err(f"recv: {e}")
                    self.bus_ok = False
                time.sleep(0.2)
                continue
            if frame is None:
                continue
            self.bus_ok = True
            if not frame.timestamp or frame.timestamp < 1e9:
                # some adapters (PCAN) time-stamp from the adapter's start, not the wall clock; sent frames use the wall clock.
                # One clock for everything keeps the trace and the bus log in true order.
                frame.timestamp = time.time()
            self.rx_total += 1
            self._count_bits(frame)
            if frame.is_error_frame:
                self._note_err("CAN error frame received")
                self._trace("ERR", frame)
                continue
            self._trace("RX", frame)
            for tap in list(self.taps):
                try:
                    tap(frame)
                except Exception:
                    pass
            model = self.model
            m = model.msg_for_frame(frame.arbitration_id, frame.is_extended_id) if model else None
            if m is None:
                self.unknown_ids[frame.arbitration_id] += 1
                key = (frame.arbitration_id, bool(frame.is_extended_id))
                with self.lock:
                    u = self.unknown.get(key)
                    if u is None and len(self.unknown) < 64:
                        u = self.unknown[key] = UnknownState(*key)
                    if u is not None:
                        u.update(frame, time.time())
                continue
            st = self.rx.get(m.name)
            if st is None:
                continue
            try:
                vals = m.decode_values(frame.data)
            except Exception as e:
                self._note_err(f"decode {m.name}: {e}")
                continue
            now = time.time()
            with self.lock:
                if st.last:
                    st.intervals.append(now - st.last)
                st.values.update(vals)
                st.data = bytes(frame.data)
                st.last = now
                st.count += 1
            for cb in list(self._listeners):
                try:
                    cb(m.name, vals)
                except Exception:
                    pass

    def _count_bits(self, frame):
        bits = (67 if frame.is_extended_id else 47) + 8 * (frame.dlc or 0)
        bits = int(bits * 1.2)  # rough stuffing allowance
        self._bits_window.append((time.time(), bits))

    def _trace(self, d: str, frame):
        rec = self.recorder
        if rec is not None:
            rec.add(d, frame)             # the bus log always gets every frame; only the on-screen trace may hide TX
        if d == "TX" and not self.trace_tx:
            return
        self.trace_seq += 1
        self.trace.append((self.trace_seq, frame.timestamp or time.time(), d, frame.arbitration_id,
                           bool(frame.is_extended_id), bytes(frame.data), frame.dlc))

    # ---- raw access (UDS, diagnostics) ------------------------------------------------------
    def add_tap(self, fn):
        self.taps.append(fn)

    def remove_tap(self, fn):
        if fn in self.taps:
            self.taps.remove(fn)

    def send_frame(self, arb_id: int, data: bytes, ext: bool = False):
        bus = self.bus
        if bus is None:
            raise RuntimeError("CAN interface is not connected.")
        msg = can.Message(arbitration_id=arb_id, is_extended_id=ext, data=bytes(data), dlc=len(data), is_fd=False)
        with self.send_lock:
            bus.send(msg, timeout=0.1)
        self.tx_total += 1
        self._count_bits(msg)
        msg.timestamp = time.time()
        self._trace("TX", msg)

    def unknown_snapshot(self):
        now = time.time()
        with self.lock:
            out = []
            for (fid, ext), u in sorted(self.unknown.items()):
                iv = u.intervals
                out.append({"id": fid, "id_hex": f"{fid:08X}" if ext else f"{fid:03X}", "ext": ext, "dlc": u.dlc,
                            "count": u.count, "age": round(now - u.last, 2),
                            "period_ms": round(1000 * sum(iv) / len(iv)) if iv else None,
                            "data": list(u.data),
                            "changed": [None if not t else round(now - t, 1) for t in u.changed[:len(u.data)]]})
            return out[:12]

    def forget_unknown(self, fid: int, ext: bool):
        with self.lock:
            self.unknown.pop((fid, bool(ext)), None)

    # ---- reading state --------------------------------------------------------------------
    def trace_since(self, seq: int, limit: int = 400):
        out = [t for t in list(self.trace) if t[0] > seq]
        dropped = max(0, len(out) - limit)
        return out[-limit:], dropped

    def bus_load(self) -> float:
        now = time.time()
        w = self._bits_window
        while w and now - w[0][0] > 1.0:
            w.popleft()
        if not self.bitrate:
            return 0.0
        return min(100.0, sum(b for _, b in w) / self.bitrate * 100.0)

    def rx_snapshot(self) -> Dict[str, dict]:
        now = time.time()
        with self.lock:
            out = {}
            for name, st in self.rx.items():
                iv = st.intervals
                out[name] = {
                    "age": None if not st.last else round(now - st.last, 2),
                    "count": st.count,
                    "period_ms": round(1000 * sum(iv) / len(iv)) if iv else None,
                    "values": dict(st.values),
                    "data": list(st.data),
                }
            return out

    def tx_snapshot(self) -> Dict[str, dict]:
        with self.lock:
            return {n: {"count": st.count, "enabled": st.enabled, "period_ms": round(st.period * 1000),
                        "late_ms": round(st.max_late * 1000, 1)} for n, st in self.tx.items()}

    def rx_value(self, msg_name: str, signal: str):
        with self.lock:
            st = self.rx.get(msg_name)
            if not st or signal not in st.values:
                return None
            return st.values[signal]

    def rx_age(self, msg_name: str) -> Optional[float]:
        with self.lock:
            st = self.rx.get(msg_name)
            return None if not st or not st.last else time.time() - st.last
