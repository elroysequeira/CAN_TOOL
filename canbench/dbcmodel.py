"""DBC loading, signal metadata, health checks and the raw <-> physical codec.

Design rules (they matter for a test tool):
  * The tool never silently changes what the DBC says; where the DBC looks wrong
    it reports a finding ("lint") and uses a documented interpretation.
  * Raw values are what goes on the wire. Physical values are only for humans.
  * Values are clamped to what the *bit field* can hold, NOT to the DBC min/max,
    because sending out-of-range values is a core part of negative testing.
    The DBC range is kept as a soft range (sliders, warnings).
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Dict, List, Optional

import cantools

COUNTER_RE = re.compile(r"(rolling|alive|counter|(^|_)cnt($|_)|msg_cnt)", re.I)
PLACEHOLDER_RE = re.compile(r"^New_(Message|Signal)", re.I)
PERIOD_RE = re.compile(r"(\d+(?:\.\d+)?)\s*ms", re.I)
HI_RE = re.compile(r"^(?P<stem>.+?)_(?:Hi|High)_?Byte$", re.I)
LO_RE = re.compile(r"^(?P<stem>.+?)_(?:Lw|Lo|Low)_?Byte$", re.I)
FAULT_WORDS_RE = re.compile(r"(error|fault|flt|alarm|err|alert|warn)", re.I)
DEFAULT_PERIOD_MS = 100


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def fmt_num(v) -> str:
    if isinstance(v, bool):
        return str(int(v))
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        return f"{v:.6g}"
    return str(v)


def clean_num(v):
    """int when integral, else a rounded float - keeps JSON and the UI tidy."""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, float):
        v = round(v, 9)
        return int(v) if v.is_integer() else v
    return v


def signal_bits(start: int, length: int, little: bool) -> List[int]:
    """Bit positions (byte*8+bit) a signal occupies, DBC numbering."""
    if little:
        return list(range(start, start + length))
    bits, pos = [], start
    for _ in range(length):
        bits.append(pos)
        pos = pos + 15 if pos % 8 == 0 else pos - 1
    return bits


class Sig:
    """One DBC signal plus everything the UI/engine needs to know about it."""

    def __init__(self, msg: "Msg", s):
        self.msg = msg
        self.name: str = s.name
        self.uid = f"{msg.name}.{s.name}"
        self.start = s.start
        self.length = s.length
        self.signed = bool(s.is_signed)
        self.is_float = bool(getattr(s, "is_float", False))
        self.little = s.byte_order == "little_endian"
        self.scale = float(s.scale) if s.scale not in (None, 0) else 1.0
        self.offset = float(s.offset or 0)
        self.unit = s.unit or ""
        self.comment = (s.comment or "").strip() if isinstance(s.comment, str) else ""
        self.dbc_min = None if s.minimum is None else float(s.minimum)
        self.dbc_max = None if s.maximum is None else float(s.maximum)
        self.choices: Dict[int, str] = {int(k): str(v) for k, v in (s.choices or {}).items()}
        self.bits = signal_bits(s.start, s.length, self.little)
        self.hidden_by: Optional[str] = None  # composite uid that replaces this signal in the UI
        if self.length == 1:
            self.kind = "bool"
        elif self.choices:
            self.kind = "enum"
        else:
            self.kind = "num"
        self.is_counter = bool(COUNTER_RE.search(self.name)) and self.length >= 2 and self.kind == "num"

    # ---- raw range of the bit field ---------------------------------------
    @property
    def raw_range(self):
        if self.signed:
            return -(1 << (self.length - 1)), (1 << (self.length - 1)) - 1
        return 0, (1 << self.length) - 1

    @property
    def has_soft_range(self) -> bool:
        return self.dbc_min is not None and self.dbc_max is not None and self.dbc_max > self.dbc_min

    def soft_range(self):
        """Physical slider range: DBC min/max when sane, else the full bit-field range."""
        if self.has_soft_range:
            return self.dbc_min, self.dbc_max
        lo, hi = self.raw_range
        a, b = lo * self.scale + self.offset, hi * self.scale + self.offset
        a, b = min(a, b), max(a, b)
        if self.unit.strip() == "%" and self.kind == "num":  # a percentage with no declared range (e.g. SOC [0|0])
            a, b = max(a, 0.0), min(b, 100.0)
        return a, b

    # ---- conversion --------------------------------------------------------
    def to_raw(self, value):
        """UI value -> raw. bool: truthy; enum: raw already; num: physical."""
        if self.is_float:
            return float(value)
        lo, hi = self.raw_range
        if self.kind == "bool":
            return -1 if (value and self.signed) else (1 if value else 0)
        if self.kind == "enum":
            raw = int(round(float(value)))
        else:
            raw = int(math.floor((float(value) - self.offset) / self.scale + 0.5))
        return max(lo, min(hi, raw))

    def to_value(self, raw):
        """raw -> UI value."""
        if self.is_float:
            return clean_num(float(raw))
        if self.kind == "bool":
            return 1 if raw else 0
        if self.kind == "enum":
            return int(raw)
        return clean_num(raw * self.scale + self.offset)

    def in_dbc_range(self, value) -> bool:
        if not self.has_soft_range or self.kind != "num":
            return True
        eps = abs(self.scale) * 0.5
        return self.dbc_min - eps <= float(value) <= self.dbc_max + eps

    def default_value(self):
        """Neutral starting value: 0 (or nearest in-range value to 0)."""
        if self.kind in ("bool", "enum"):
            return 0
        if self.has_soft_range:
            lo, hi = self.dbc_min, self.dbc_max
            return clean_num(min(max(0.0, lo), hi))
        return 0 if self.offset == 0 else self.to_value(self.to_raw(0))

    def choice_label(self, raw) -> str:
        return self.choices.get(int(raw), "") if self.choices else ""

    def to_dict(self):
        lo, hi = self.soft_range()
        return {
            "uid": self.uid, "name": self.name, "msg": self.msg.name, "kind": self.kind,
            "start": self.start, "length": self.length, "signed": self.signed,
            "scale": self.scale, "offset": self.offset, "unit": self.unit,
            "min": clean_num(lo), "max": clean_num(hi), "soft_range": self.has_soft_range,
            "choices": {str(k): v for k, v in sorted(self.choices.items())},
            "comment": self.comment, "counter": self.is_counter, "hidden_by": self.hidden_by,
            "default": self.default_value(),
        }


class Comp:
    """Two byte-wide signals (..._Hi_Byte / ..._Lw_Byte) shown as one 16-bit value."""

    kind = "num"
    is_counter = False

    def __init__(self, msg: "Msg", stem: str, hi: Sig, lo: Sig):
        self.msg, self.name, self.hi, self.lo = msg, stem, hi, lo
        self.uid = f"{msg.name}.{stem}"
        self.scale = lo.scale
        self.offset = lo.offset
        self.unit = lo.unit or hi.unit
        self.length = 16
        self.signed = False
        self.comment = ""
        self.choices: Dict[int, str] = {}
        hi.hidden_by = lo.hidden_by = self.uid

    @property
    def raw_range(self):
        return 0, 65535

    def soft_range(self):
        a, b = self.offset, 65535 * self.scale + self.offset
        return min(a, b), max(a, b)

    has_soft_range = False

    def to_raw(self, value) -> int:
        raw = int(math.floor((float(value) - self.offset) / self.scale + 0.5))
        return max(0, min(65535, raw))

    def split(self, value):
        raw = self.to_raw(value)
        return {self.hi.name: (raw >> 8) & 0xFF, self.lo.name: raw & 0xFF}

    def to_value(self, raw16):
        return clean_num(raw16 * self.scale + self.offset)

    def in_dbc_range(self, value) -> bool:
        return True

    def default_value(self):
        return self.to_value(self.to_raw(0))

    def to_dict(self):
        lo, hi = self.soft_range()
        return {
            "uid": self.uid, "name": self.name, "msg": self.msg.name, "kind": "num", "composite": True,
            "members": [self.hi.uid, self.lo.uid], "start": self.lo.start, "length": 16, "signed": False,
            "scale": self.scale, "offset": self.offset, "unit": self.unit,
            "min": clean_num(lo), "max": clean_num(hi), "soft_range": False, "choices": {},
            "comment": f"16-bit value combined from {self.hi.name} (high) and {self.lo.name} (low).",
            "counter": False, "hidden_by": None, "default": self.default_value(),
        }


class Msg:
    def __init__(self, model: "Model", m):
        self.model = model
        self.cm = m
        self.name: str = m.name
        self.frame_id: int = m.frame_id
        self.ext: bool = bool(m.is_extended_frame)
        self.length: int = m.length
        self.comment = (m.comment or "").strip() if isinstance(m.comment, str) else ""
        self.senders: List[str] = [s for s in (m.senders or []) if s and s != "Vector__XXX"]
        self.mux = bool(m.is_multiplexed())
        self.signals: List[Sig] = [Sig(self, s) for s in m.signals]
        self.by_name: Dict[str, Sig] = {s.name: s for s in self.signals}
        self.comps: List[Comp] = []
        self.cycle_ms, self.period_src = self._period()
        self.placeholder = bool(
            self.frame_id == 0 and (PLACEHOLDER_RE.match(self.name) or
                                    all(PLACEHOLDER_RE.match(s.name) for s in self.signals)))

    def _period(self):
        ct = getattr(self.cm, "cycle_time", None)
        if ct:
            return int(ct), "dbc attribute"
        mt = PERIOD_RE.search(self.comment)
        if mt:
            return int(float(mt.group(1))), "dbc comment"
        return DEFAULT_PERIOD_MS, "default"

    def find_composites(self):
        his, los = {}, {}
        for s in self.signals:
            if s.length != 8 or s.kind != "num":
                continue
            mh, ml = HI_RE.match(s.name), LO_RE.match(s.name)
            if mh:
                his[mh.group("stem").lower()] = (mh.group("stem"), s)
            elif ml:
                los[ml.group("stem").lower()] = (ml.group("stem"), s)
        for key, (stem, hi) in his.items():
            if key in los:
                lo = los[key][1]
                if hi.start == lo.start + 8 and hi.little == lo.little:
                    self.comps.append(Comp(self, stem, hi, lo))

    def items(self):
        """What the UI shows: composites + every signal not absorbed by one."""
        out = [s for s in self.signals if not s.hidden_by] + list(self.comps)
        out.sort(key=lambda x: x.start if not isinstance(x, Comp) else x.lo.start)
        return out

    # ---- wire codec ----------------------------------------------------------
    def default_raw(self) -> Dict[str, int]:
        raw = {}
        for s in self.signals:
            if s.hidden_by:
                continue
            raw[s.name] = s.to_raw(s.default_value())
        for c in self.comps:
            raw.update(c.split(c.default_value()))
        return raw

    def encode(self, raw: Dict[str, int]) -> bytes:
        return bytes(self.cm.encode(raw, scaling=False, strict=False))

    def decode_raw(self, data: bytes) -> Dict[str, int]:
        return self.cm.decode(bytes(data), decode_choices=False, scaling=False, allow_truncated=True)

    def decode_values(self, data: bytes) -> Dict[str, object]:
        """Frame -> {item name: UI value} (composites merged)."""
        raw = self.decode_raw(data)
        out = {}
        for s in self.signals:
            if s.hidden_by or s.name not in raw:
                continue
            out[s.name] = s.to_value(raw[s.name])
        for c in self.comps:
            if c.hi.name in raw and c.lo.name in raw:
                out[c.name] = c.to_value((int(raw[c.hi.name]) << 8) | int(raw[c.lo.name]))
        return out

    def to_dict(self):
        return {
            "name": self.name, "id": self.frame_id, "id_hex": f"0x{self.frame_id:X}", "ext": self.ext,
            "dlc": self.length, "comment": self.comment, "senders": self.senders, "period_ms": self.cycle_ms,
            "period_src": self.period_src, "placeholder": self.placeholder, "mux": self.mux,
            "items": [i.to_dict() for i in self.items()],
        }


class Model:
    """All loaded DBCs merged into one database + derived metadata."""

    def __init__(self, paths: List[str], composites: bool = True):
        self.paths = list(paths)
        self.findings: List[dict] = []
        self.load_errors: List[str] = []
        self.db = cantools.database.Database(strict=False)
        for p in self.paths:
            try:
                ext = os.path.splitext(p)[1].lower()
                if ext == ".kcd":
                    self.db.add_kcd_file(p)
                elif ext == ".sym":
                    self.db.add_sym_file(p)
                elif ext == ".arxml":
                    self.db.add_arxml_file(p)
                else:
                    self.db.add_dbc_file(p)
            except Exception as e:  # keep going: one bad file must not hide the rest
                self.load_errors.append(f"{p}: {e}")
        self.messages: List[Msg] = [Msg(self, m) for m in self.db.messages]
        self.by_name: Dict[str, Msg] = {m.name: m for m in self.messages}
        self.by_frame: Dict[tuple, Msg] = {(m.frame_id, m.ext): m for m in self.messages}
        if composites:
            for m in self.messages:
                m.find_composites()
        self.items: Dict[str, object] = {}
        for m in self.messages:
            for s in m.signals:
                self.items[s.uid] = s
            for c in m.comps:
                self.items[c.uid] = c
        self._lint()

    # ---- lookups ---------------------------------------------------------------
    def msg_for_frame(self, frame_id: int, ext: bool) -> Optional[Msg]:
        return self.by_frame.get((frame_id, bool(ext)))

    def find_items_by_name(self, name: str):
        low = name.lower()
        return [i for i in self.items.values() if i.name.lower() == low and not getattr(i, "hidden_by", None)]

    # ---- health checks ------------------------------------------------------------
    def _add(self, level, code, where, text, fix=""):
        self.findings.append({"level": level, "code": code, "where": where, "text": text, "fix": fix})

    def _lint(self):
        for e in self.load_errors:
            self._add("error", "LOAD", "file", f"Could not load: {e}")
        no_senders = all(not m.senders for m in self.messages)
        if self.messages and no_senders:
            self._add("info", "NO_SENDER", "database",
                      "No message has a transmitter node, so the tool cannot tell which frames the DUT sends "
                      "and which it receives. Assign each message a role in Setup.")
        seen_ids = {}
        for m in self.messages:
            key = (m.frame_id, m.ext)
            if key in seen_ids:
                self._add("error", "DUP_ID", m.name,
                          f"Frame ID 0x{m.frame_id:X} is used by both {seen_ids[key]} and {m.name}.")
            seen_ids[key] = m.name
            if m.placeholder:
                self._add("warn", "PLACEHOLDER", m.name,
                          "Looks like an unnamed placeholder frame (ID 0, 'New_Message'). It is ignored by default.",
                          "Delete it from the DBC.")
            if m.mux:
                self._add("warn", "MUX", m.name, "Multiplexed message: only the default multiplexer page is sent.")
            occupied: Dict[int, str] = {}
            for s in m.signals:
                if s.bits and max(s.bits) >= m.length * 8:
                    self._add("error", "OUT_OF_FRAME", s.uid,
                              f"Signal needs bit {max(s.bits)} but the frame is only {m.length} bytes.")
                for b in s.bits:
                    if b in occupied and occupied[b] != s.name:
                        self._add("error", "OVERLAP", s.uid, f"Overlaps signal {occupied[b]} (bit {b}).")
                        break
                    occupied[b] = s.name
                if s.length == 1 and s.signed:
                    self._add("warn", "SIGNED_1BIT", s.uid,
                              "1-bit signal declared signed (@1-): valid values are only 0 and -1, so a normal "
                              "'1' cannot be encoded by DBC tools. The bench sends raw -1 for ON.",
                              "Declare it unsigned (@1+).")
                if s.choices:
                    lo_hi = (s.dbc_min, s.dbc_max)
                    if s.has_soft_range and any(k < lo_hi[0] or k > lo_hi[1] for k in s.choices):
                        self._add("warn", "ENUM_RANGE", s.uid,
                                  f"Value table has entries outside the declared range [{fmt_num(lo_hi[0])}|"
                                  f"{fmt_num(lo_hi[1])}].", "Fix the signal range or the value table.")
                if s.kind == "num" and s.has_soft_range and not s.is_float:
                    lo, hi = s.raw_range
                    pa, pb = lo * s.scale + s.offset, hi * s.scale + s.offset
                    if s.dbc_min < min(pa, pb) - abs(s.scale) or s.dbc_max > max(pa, pb) + abs(s.scale):
                        self._add("warn", "RANGE_BITS", s.uid,
                                  f"Declared range [{fmt_num(s.dbc_min)}|{fmt_num(s.dbc_max)}] cannot be "
                                  f"represented in {s.length} bits (representable "
                                  f"{fmt_num(min(pa, pb))}..{fmt_num(max(pa, pb))}).")
            for c in m.comps:
                msg = (f"{c.hi.name} + {c.lo.name} are combined into one 16-bit value '{c.name}' "
                       f"(scale {fmt_num(c.scale)}, offset {fmt_num(c.offset)}).")
                if c.hi.offset or c.lo.offset:
                    msg += (" Each byte carries the offset in the DBC, which is only correct for a single byte; "
                            "the bench applies the offset once to the 16-bit value.")
                self._add("info", "SPLIT_PAIR", c.uid, msg,
                          "Verify with the firmware owner; ideally define one 16-bit signal.")

    def nodes(self) -> List[str]:
        names = {n.name for n in self.db.nodes if n.name and n.name != "Vector__XXX"}
        for m in self.messages:
            names.update(m.senders)
        return sorted(names)

    def summary(self):
        sigs = [s for m in self.messages for s in m.signals]
        return {
            "nodes": self.nodes(),
            "messages": len(self.messages), "signals": len(sigs),
            "enums": sum(1 for s in sigs if s.kind == "enum"),
            "bools": sum(1 for s in sigs if s.kind == "bool"),
            "composites": sum(len(m.comps) for m in self.messages),
            "extended": sum(1 for m in self.messages if m.ext),
            "files": [p.replace("\\", "/").split("/")[-1] for p in self.paths],
        }
