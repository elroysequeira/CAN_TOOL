"""CAN ID / signal tables (Excel, CSV) -> a real DBC.

Many projects have no DBC, only a spreadsheet. Every spreadsheet is laid out differently, so this reader is adaptive:

  * several header rows in one sheet (e.g. "MCU -> VCU" and "VCU -> MCU" blocks) - each block has its own columns
  * "Motor Controller  TX / VCU  Rx" lines above a block say who sends the frames in it
  * a baud-rate line ("Communication baud rate  250Kbps") is picked up
  * message cells may be blank below the first row of a group; frames without a name are named from sender + ID
  * the position column may hold a BYTE index ("Byte Position") or a BIT index ("Start Bit"); a frame whose "byte"
    positions go above 7 is read as bit positions
  * byte order: from a column, from a stray "Motorola"/"Intel" in the row, or - when nothing says - tried both ways and the one
    that fits the frame without overlapping is used (and reported)
  * remark text is mined: "bit[6]: Motor LOCK [0-unlock,1-lock]" becomes a 1-bit signal, "1-ECO, 2-POWER" a value table,
    "-128 to +127" a range (and signedness)

Nothing is hidden: every assumption and every skipped row is returned as a warning, and the UI offers a preview of the
frames and signals before anything is used.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from .dbcmodel import signal_bits

# field -> header spellings (normalised: lower case, letters and digits only).
# Spellings of 4 characters or fewer must match the whole header; longer ones match as a prefix.
# The longest matching spelling wins ("signalsize" is a length, not a name).
FIELDS: Dict[str, List[str]] = {
    "id": ["canid", "msgid", "messageid", "frameid", "arbitrationid", "identifier", "canidentifier", "id", "idhex",
           "iddec", "canfrmid", "canmsgid", "pgn"],
    "msg": ["messagename", "msgname", "framename", "canmessagename", "canmessage", "message", "msg", "frame",
            "messagelabel", "framelabel"],
    "sig": ["signalname", "signal", "signalid", "parameter", "parametername", "signallabel", "signame", "datafield",
            "fieldname", "datadescription"],
    "start": ["startbit", "startbitposition", "bitstart", "startposition", "lsb", "bitposition", "start", "startbitlsb",
              "bitoffset"],
    "bytepos": ["byteposition", "bytepos", "byteno", "bytenumber", "byteindex", "byte", "startbyte", "byteoffset",
                "databyte", "bytelocation"],
    "length": ["bitlength", "signallength", "signalsize", "lengthbits", "lengthbit", "numberofbits", "nbits", "bits",
               "length", "size", "width", "siglen", "siglength"],
    "dlc": ["dlc", "msgdlc", "messagelength", "framelength", "msglength", "datalengthbytes", "lengthbytes", "bytes",
            "datalength", "payload", "messagesize"],
    "order": ["byteorder", "endianness", "endian", "byteformat", "intelmotorola", "bitorder", "format"],
    "signed": ["signed", "signedness", "sign", "valuetype", "datatype", "signtype"],
    "factor": ["factor", "scale", "scaling", "resolution", "gain", "multiplier"],
    "offset": ["offset"],
    "min": ["minimum", "minvalue", "physmin", "min", "minphys", "rangemin", "lowerlimit"],
    "max": ["maximum", "maxvalue", "physmax", "max", "maxphys", "rangemax", "upperlimit"],
    "unit": ["unit", "units", "uom"],
    "values": ["valuetable", "valuedescription", "valuedescriptions", "values", "enum", "enumeration", "encoding",
               "valuemeaning", "states", "descriptionofvalues", "valuemap", "choices"],
    "cycle": ["cycletime", "cycle", "period", "periodicity", "periodms", "txperiod", "interval", "sendcycle",
              "cyclems", "repetition", "rate", "transmissioncycle"],
    "sender": ["sender", "txnode", "transmitter", "node", "ecu", "sentby", "source", "sendernode", "transmitternode",
               "txecu", "txsource"],
    "dir": ["direction", "dir", "txrx", "rxtx", "inout"],
    "ext": ["idformat", "frameformat", "idtype", "extended", "ext", "frametype", "canformat", "idlength"],
    "comment": ["comment", "comments", "description", "remarks", "remark", "notes", "note"],
}
_ALL = [(f, s) for f, ss in FIELDS.items() for s in ss]
_TEXT_FIELDS = {"id", "msg", "sig", "comment", "values", "dlc", "start", "bytepos", "length", "sender", "dir", "ext", "cycle"}
_ORDER_WORDS = {"motorola": "big", "bigendian": "big", "big": "big", "be": "big", "intel": "little", "littleendian": "little",
                "little": "little", "le": "little"}


def _n(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _field_of(header: str) -> Optional[str]:
    h = _n(header)
    best, best_len = None, 0
    for f, s in _ALL:
        if (h == s or (len(s) > 4 and h.startswith(s))) and len(s) > best_len:
            best, best_len = f, len(s)
    return best


def map_columns(row: List[str]) -> Dict[str, int]:
    cols: Dict[str, int] = {}
    for i, cell in enumerate(row):
        if not str(cell).strip():
            continue
        f = _field_of(cell)
        if f is None and _n(cell) == "name":
            f = "name"
        if f and f not in cols:
            cols[f] = i
    return cols


def _is_header(cols: Dict[str, int]) -> bool:
    if "id" not in cols:
        return False
    if "msg" in cols or "name" in cols:                      # frame table (or message + signal table)
        return True
    has_pos = "start" in cols or "bytepos" in cols
    return len(cols) >= 3 and "sig" in cols and has_pos and "length" in cols


# ---------------------------------------------------------------------------------------------------------------------
def _num(txt) -> Optional[float]:
    t = str(txt or "").strip().replace("−", "-")
    if not t:
        return None
    m = re.fullmatch(r"([-+]?\d+(?:[.,]\d+)?)\s*/\s*(\d+(?:[.,]\d+)?)", t)
    if m:
        d = float(m.group(2).replace(",", "."))
        return float(m.group(1).replace(",", ".")) / d if d else None
    m = re.match(r"[xX*]?\s*([-+]?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?)", t)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            return None
    return None


def _int(txt) -> Optional[int]:
    t = str(txt or "").strip()
    if re.fullmatch(r"0[xX][0-9a-fA-F]+", t):
        return int(t, 16)
    v = _num(t)
    return None if v is None else int(round(v))


def _parse_id(txt: str, base: str):
    """-> (id, explicit_hex, digits) ; id None if unusable."""
    t = str(txt or "").strip().replace(" ", "").replace("_", "")
    if not t:
        return None, False, 0
    t = re.sub(r"\.0+$", "", t)
    m = re.fullmatch(r"(?i)0x([0-9a-f]+)", t) or re.fullmatch(r"(?i)\$([0-9a-f]+)", t) or re.fullmatch(r"(?i)([0-9a-f]+)h", t)
    if m:
        return int(m.group(1), 16), True, len(m.group(1))
    if re.fullmatch(r"[0-9]+", t):
        return int(t, 16 if base == "hex" else 10), False, len(t)
    if re.fullmatch(r"(?i)[0-9a-f]+", t):
        return int(t, 16), True, len(t)
    return None, False, 0


def _choices(txt: str) -> Dict[int, str]:
    out: Dict[int, str] = {}
    t = str(txt or "").replace("，", ",").replace("：", ":")
    for m in re.finditer(r"(0[xX][0-9a-fA-F]+|\d+)\s*[=:\-–]\s*([^;,\n|\]\)]+?)(?=\s+\d+\s*[=:\-–]|[;,\n|\]\)]|$)", t):
        try:
            out.setdefault(int(m.group(1), 0), m.group(2).strip()[:60])
        except ValueError:
            continue
    return out


def _bracket_choices(txt: str) -> Dict[int, str]:
    """'[0 Forward, 1 Reverse ]' / '[0- unlock,1- lock)' -> {0:..,1:..}"""
    out = _choices(txt)
    if len(out) >= 2:
        return out
    t = str(txt or "").replace("，", ",")
    for m in re.finditer(r"(\d+)\s+([A-Za-z][^,;\]\)]*)", t):
        out.setdefault(int(m.group(1)), m.group(2).strip()[:60])
    return out


def _clean_name(s: str, fallback: str) -> str:
    n = re.sub(r"[^A-Za-z0-9_]+", "_", str(s or "").strip()).strip("_")
    if not n:
        n = fallback
    if not n:
        return ""
    if n[0].isdigit():
        n = "_" + n
    return n[:60]


def _dir_of(txt: str) -> Optional[str]:
    t = _n(txt)
    if not t:
        return None
    if t.startswith(("tx", "transmit", "send", "sent", "out")):
        return "tx"
    if t.startswith(("rx", "receiv", "in")):
        return "rx"
    return None


BIT_LINE = re.compile(r"^\s*bit\s*\[?\s*(\d)\s*\]?\s*[:：\-=]?\s*(.+?)\s*$", re.I)


def _flags(comment: str):
    """'bit[6]: Motor LOCK [0-unlock,1-lock]' lines -> ([(bit, name, choices)], default choices) ; [] if fewer than 2 bits."""
    flags, extra = [], []
    for line in re.split(r"[\r\n]+", str(comment or "")):
        m = BIT_LINE.match(line)
        if m:
            rest = m.group(2)
            name = re.split(r"[\[\(]", rest)[0].strip(" :-")
            flags.append((int(m.group(1)), name, _bracket_choices(rest[len(name):])))
        else:
            extra.append(line)
    if len(flags) < 2:
        return [], {}
    dflt = _bracket_choices(" ".join(extra))
    return flags, (dflt if len(dflt) >= 2 else {})


def _range_of(txt: str):
    m = re.search(r"(-\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?:to|~|\.\.|-)\s*\+?\s*(\d+(?:\.\d+)?)", str(txt or ""))
    if not m:
        return None
    a, b = float(m.group(1).replace(" ", "")), float(m.group(2))
    return (a, b) if a < b else None


def _baud(cells: List[str]) -> Optional[int]:
    if not cells or not re.search(r"baud|bit ?rate|bus speed|can speed", cells[0], re.I):
        return None
    for c in cells[1:]:
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*([kKmM])?\s*(?:bps|bit/s|baud|b/s)?", c)
        if m and re.search(r"\d", c):
            v = float(m.group(1).replace(",", "."))
            v *= {"k": 1e3, "m": 1e6}.get((m.group(2) or "").lower(), 1)
            if v < 1000 and not m.group(2):
                v *= 1000          # "250" under a baud header means kbit/s
            return int(v)
    return None


# ---------------------------------------------------------------------------------------------------------------------
class _Ctx:
    def __init__(self):
        self.warns: List[str] = []
        self.skipped = 0
        self.used_names: Dict[str, tuple] = {}
        self.sticky_order: Optional[str] = None
        self.guessed: List[str] = []
        self.bitmode: List[str] = []
        self.declared_be: List[str] = []

    def warn(self, t: str):
        if t not in self.warns:
            self.warns.append(t)


def _names(names: List[str]) -> str:
    return ", ".join(names[:6]) + (f" (+{len(names) - 6} more)" if len(names) > 6 else "")


def _place(recs: List[dict], default_order: str, byte_mode: bool, ctx: _Ctx, fname: str):
    """Try to lay out signals; -> (placed, failures)."""
    placed, failures, used = [], [], {}
    for r in recs:
        little = (r["order"] or default_order) == "little"
        start = r["pos"] * 8 if byte_mode else r["pos"]
        if byte_mode:
            little = True
        length = r["length"]
        if length < 1 or length > 32:
            failures.append((r, f"{fname}.{r['name']}: length {length} is not supported (1-32 bits) - signal skipped."))
            continue
        bits = signal_bits(start, length, little)
        if min(bits) < 0 or max(bits) >= 64:
            failures.append((r, f"{fname}.{r['name']}: does not fit in 8 bytes (start {start}, length {length}) - signal skipped."))
            continue
        clash = next((used[b] for b in bits if b in used), None)
        if clash:
            failures.append((r, f"{fname}.{r['name']}: overlaps {clash} - signal skipped."))
            continue
        for b in bits:
            used[b] = r["name"]
        placed.append({**r, "start": start, "little": little})
    return placed, failures


def _finish_frame(fr: dict, has_bytepos: bool, has_start: bool, ctx: _Ctx):
    recs = fr.pop("recs")
    laid = [r for r in recs if r["pos"] is not None and r["length"]]
    unlaid = [r for r in recs if not (r["pos"] is not None and r["length"])]
    placed: List[dict] = []
    if laid:
        byte_mode = has_bytepos and not has_start and all(0 <= r["pos"] <= 7 for r in laid)
        if has_bytepos and not has_start and not byte_mode:
            ctx.bitmode.append(fr["name"])
        if byte_mode:
            placed, fails = _place(laid, "little", True, ctx, fr["name"])
        else:
            declared = fr["hint"] or ctx.sticky_order
            if fr["hint"]:
                ctx.sticky_order = fr["hint"]
            if declared == "big":
                ctx.declared_be.append(fr["name"])
            first = declared or "little"
            other = "big" if first == "little" else "little"
            pa, fa = _place(laid, first, False, ctx, fr["name"])
            if fa:
                pb, fb = _place(laid, other, False, ctx, fr["name"])
                if len(fb) < len(fa):
                    pa, fa = pb, fb
                    ctx.warn(f"{fr['name']}: byte order {'Intel' if first == 'big' else 'Motorola'} was "
                             f"{'declared' if declared else 'assumed'} but only {'Motorola' if first == 'little' else 'Intel'} "
                             "fits without overlap - used that. Please verify.")
                    ctx.guessed.append(fr["name"])
            elif not declared and not any(r["order"] for r in laid):
                ctx.guessed.append(fr["name"])
            placed, fails = pa, fa
        for _r, msg in fails:
            ctx.warn(msg)
            ctx.skipped += 1
    # bit flags described in the remark text become their own 1-bit signals
    out: List[dict] = []
    for s in placed:
        flags, dflt = _flags(s["comment"])
        if flags and s["length"] <= 8 and not s["choices"]:
            base = s["start"] if s["little"] else s["start"] - (s["length"] - 1)
            taken = {x["name"] for x in out}
            for bit, nm, ch in flags:
                if bit >= s["length"]:
                    continue
                n = _clean_name(nm, f"{s['name']}_bit{bit}")[:40]
                if n in taken:
                    n = f"{n}_b{bit}"
                taken.add(n)
                out.append({**s, "name": n, "start": base + bit, "length": 1, "little": True, "signed": False,
                            "scale": 1.0, "offset": 0.0, "min": None, "max": None, "unit": "",
                            "choices": ch or dflt, "comment": f"{s['name']} bit {bit}"})
            ctx.warn(f"{fr['name']}.{s['name']}: split into {len(flags)} flag bits from its remark text.")
        else:
            out.append(s)
    seen: Dict[str, int] = {}
    for s in out:  # a name used twice in one frame (e.g. two "Faults/Alarms" bytes) gets its byte appended
        seen[s["name"]] = seen.get(s["name"], 0) + 1
    dup = {n for n, c in seen.items() if c > 1}
    for s in out:
        if s["name"] in dup:
            s["name"] = f"{s['name']}_B{s['start'] // 8}"
    fr["signals"] = out
    if unlaid:
        names = ", ".join(r["name"] for r in unlaid)
        ctx.warn(f"{fr['name']}: no start/size in the sheet for {names} - not added" +
                 (" (frame kept as raw bytes)." if not out else "."))
        ctx.skipped += len(unlaid)


FIELD_LABELS = [
    ("id", "CAN ID"), ("msg", "Message name"), ("dlc", "DLC / length in bytes"), ("cycle", "Cycle time (ms)"),
    ("sender", "Sender / node"), ("dir", "Direction (Tx/Rx)"), ("ext", "ID format (std/ext)"),
    ("sig", "Signal name"), ("start", "Start bit"), ("bytepos", "Byte position"), ("length", "Length (bits)"),
    ("order", "Byte order (Intel/Motorola)"), ("signed", "Signed"), ("factor", "Factor / resolution"), ("offset", "Offset"),
    ("min", "Minimum"), ("max", "Maximum"), ("unit", "Unit"), ("values", "Value table"), ("comment", "Remark / description"),
]


def check_mapping(mapping: dict, rows: List[List[str]]) -> dict:
    """A column mapping chosen by hand in the UI -> a clean {header_row, columns}; raises ValueError saying what is wrong."""
    if not isinstance(mapping, dict):
        raise ValueError("Column mapping is missing.")
    try:
        hdr = int(mapping.get("header_row"))
    except (TypeError, ValueError):
        raise ValueError("Pick the header row (the row that holds the column titles).")
    if hdr < 1 or hdr > len(rows):
        raise ValueError("The header row is outside the table.")
    width = max((len(r) for r in rows), default=0)
    cols: Dict[str, int] = {}
    for f, i in (mapping.get("columns") or {}).items():
        if f not in FIELDS:
            raise ValueError(f"Unknown column type '{f}'.")
        try:
            i = int(i)
        except (TypeError, ValueError):
            raise ValueError(f"Column for '{f}' is not a number.")
        if i < 0 or i >= max(width, 1):
            raise ValueError(f"Column for '{f}' is outside the table.")
        cols[f] = i
    if "id" not in cols:
        raise ValueError("Say which column holds the CAN ID.")
    signal_level = "sig" in cols and ("start" in cols or "bytepos" in cols) and "length" in cols
    if "msg" not in cols and not signal_level:
        raise ValueError("Pick either a message-name column (a frame list) or signal name + start bit/byte position + length "
                         "(a signal table).")
    return {"header_row": hdr, "columns": cols, "sender": str(mapping.get("sender") or "").strip()[:40]}


def grid(doc: dict, max_rows: int = 80, max_cols: int = 30) -> List[dict]:
    """What the manual column picker shows: the first rows of every table in a document and where the automatic reader would put
    the header / columns (so the tester only corrects what is wrong)."""
    out = []
    tables = [b for b in doc.get("blocks", []) if b.get("type") == "table"]
    for ti, b in enumerate(tables):
        rows = [[str(c).strip() for c in r] for r in b["rows"]]
        auto_hdr, auto_cols = None, {}
        for ri, cells in enumerate(rows, 1):
            cols = map_columns(cells)
            if _is_header(cols):
                auto_hdr, auto_cols = ri, cols
                break
        out.append({"index": ti, "n_rows": len(rows), "n_cols": max((len(r) for r in rows), default=0),
                    "rows": [[c[:60] for c in r[:max_cols]] for r in rows[:max_rows]],
                    "auto_header": auto_hdr, "auto_columns": auto_cols})
    return out


def parse_table(rows: List[List[str]], id_base: str = "auto", id_format: str = "auto",
                mapping: Optional[dict] = None) -> Optional[dict]:
    """One table -> {frames, warnings, ...} or None when it is not a CAN ID table.
    mapping (from check_mapping): the tester picked the header row and the columns by hand - nothing is guessed about them."""
    ctx = _Ctx()
    sections: List[dict] = []
    pending = {"sender": "", "tx": "", "rx": ""}
    bitrate: Optional[int] = None
    cur = None
    manual = check_mapping(mapping, rows) if mapping is not None else None
    for ri, row in enumerate(rows, 1):
        cells = [str(c).strip() for c in row]
        if not any(cells):
            continue
        if manual is not None:
            if ri == manual["header_row"]:
                cur = {"cols": dict(manual["columns"]), "header": cells, "sender": manual["sender"], "rows": [], "hdr": ri}
                sections.append(cur)
            elif cur is not None:
                b = _baud([c for c in cells if c])
                if b:
                    bitrate = b
                else:
                    cur["rows"].append((ri, cells))
            continue
        cols = map_columns(cells)
        if _is_header(cols):
            if "name" in cols:  # a bare "Name" column
                if "sig" not in cols and ("start" in cols or "bytepos" in cols) and "length" in cols:
                    cols["sig"] = cols.pop("name")
                elif "msg" not in cols:
                    cols["msg"] = cols.pop("name")
            cur = {"cols": cols, "header": cells, "sender": pending["sender"], "rows": [], "hdr": ri}
            sections.append(cur)
            pending = {"sender": "", "tx": "", "rx": ""}
            continue
        nz = [c for c in cells if c]
        # "Motor Controller | TX" lines: who transmits the block that follows
        if 2 <= len(nz) <= 3 and _parse_id(nz[0], "hex")[0] is None or (len(nz) == 2 and re.fullmatch(r"(?i)tx|rx", nz[1] or "")):
            kind = next((_dir_of(c) for c in nz[1:] if re.fullmatch(r"(?i)(tx|rx|transmit(ter)?|receiv(e|er)|sender|sent)", c)), None)
            if kind and nz[0]:
                node = _clean_name(nz[0], "")
                pending[kind] = node
                if kind == "tx":
                    pending["sender"] = node
                continue
        b = _baud(nz)
        if b:
            bitrate = b
            continue
        if cur is not None:
            cur["rows"].append((ri, cells))
    if not sections:
        return None

    all_ids = [c for s in sections for _ri, r in s["rows"] for c in [r[s["cols"]["id"]] if s["cols"]["id"] < len(r) else ""]]
    header_txt = " ".join(_n(s["header"][s["cols"]["id"]]) for s in sections)
    if id_base == "auto":
        explicit = any(_parse_id(c, "dec")[1] for c in all_ids)
        if "hex" in header_txt or explicit:
            id_base = "hex"
        elif "dec" in header_txt:
            id_base = "dec"
        else:
            id_base = "hex"
            if any(c for c in all_ids):
                ctx.warn("Bare numbers in the ID column were read as HEX (CAN convention); choose 'decimal' if they are not.")

    frames: Dict[tuple, dict] = {}
    order: List[tuple] = []
    wide_ids: List[int] = []
    for sec in sections:
        cols = sec["cols"]
        signal_level = ("sig" in cols) and ("start" in cols or "bytepos" in cols) and "length" in cols
        has_start, has_bytepos = "start" in cols, "bytepos" in cols
        if not signal_level and "length" in cols and "dlc" not in cols:
            cols["dlc"] = cols.pop("length")
        # a CAN ID table without a message-name column: frames are named from sender + ID
        known_cols = {i for f, i in cols.items() if f in _TEXT_FIELDS}

        def cell(r, f):
            i = cols.get(f)
            return str(r[i]).strip() if i is not None and i < len(r) else ""

        last = {k: "" for k in ("id", "msg", "dlc", "cycle", "sender", "dir", "ext")}
        sec_frames: List[dict] = []
        for ri, r in sec["rows"]:
            idc = cell(r, "id")
            rest = [c for i, c in enumerate(r) if i != cols["id"] and c]
            if idc and not rest and _parse_id(idc, id_base)[0] is None:
                continue                                     # a title line such as "VCU to MCU Communication:"
            if idc:
                for k in last:
                    last[k] = cell(r, k) if k in cols else ""
            elif not cell(r, "sig"):
                continue
            idtxt = idc or last["id"]
            fid, _hex, digits = _parse_id(idtxt, id_base)
            if fid is None:
                ctx.warn(f"row {ri}: ID '{idtxt}' is not a number - row skipped.")
                ctx.skipped += 1
                continue
            if fid > 0x1FFFFFFF:
                ctx.warn(f"row {ri}: ID 0x{fid:X} is larger than 29 bits - row skipped.")
                ctx.skipped += 1
                continue
            ext_txt = _n(cell(r, "ext") or last["ext"])
            wide = bool(re.fullmatch(r"(?i)\s*0x[0-9a-f]{8}\s*", idtxt)) if _hex else False
            if id_format == "ext":
                ext = True
            elif id_format == "std":
                ext = fid > 0x7FF
            else:
                ext = fid > 0x7FF or wide or (any(k in ext_txt for k in ("ext", "29", "j1939")) and "std" not in ext_txt)
            if wide and id_format == "auto" and fid <= 0x7FF and fid not in wide_ids:
                wide_ids.append(fid)
            key = (fid, ext)
            fr = frames.get(key)
            if fr is None:
                d = _dir_of(cell(r, "dir") or last["dir"])
                sender = _clean_name(cell(r, "sender") or last["sender"], "") or sec["sender"] or \
                    ("DUT" if d == "tx" else "TESTER" if d == "rx" else "")
                nm = _clean_name(cell(r, "msg") or last["msg"], "") or \
                    (f"{sender}_0x{fid:03X}" if sender else f"ID_0x{fid:03X}")
                if nm in ctx.used_names and ctx.used_names[nm] != key:
                    nm = f"{nm}_{fid:X}"
                ctx.used_names[nm] = key
                dlc = _int(cell(r, "dlc") or last["dlc"]) or 0
                if dlc > 8:
                    ctx.warn(f"{nm}: DLC {dlc} is more than 8 (CAN FD is not supported) - using 8.")
                    dlc = 8
                cyc = _num(cell(r, "cycle") or last["cycle"]) or 0
                fr = {"name": nm, "id": fid, "ext": ext, "dlc": dlc, "period_ms": int(cyc) if cyc and cyc > 0 else 0,
                      "sender": sender, "dir": d, "recs": [], "hint": None, "_sec": sec}
                frames[key] = fr
                order.append(key)
                sec_frames.append(fr)
            if not signal_level:
                continue
            sn = _clean_name(cell(r, "sig"), "")
            if not sn:
                continue
            # a stray "Motorola" / "Intel" typed in some other column of the row declares the frame's byte order
            for i, c in enumerate(r):
                if i not in known_cols and _n(c) in _ORDER_WORDS and fr["hint"] is None:
                    fr["hint"] = _ORDER_WORDS[_n(c)]
            pos = _int(cell(r, "start") if has_start else cell(r, "bytepos"))
            length = _int(cell(r, "length"))
            o = _n(cell(r, "order"))
            row_order = None
            if o:
                if any(k in o for k in ("motorola", "big", "be", "0")) and not any(k in o for k in ("intel", "little")):
                    row_order = "big"
                elif any(k in o for k in ("intel", "little", "le", "1")):
                    row_order = "little"
            sg = _n(cell(r, "signed"))
            signed = False
            if sg:
                if "unsigned" in sg or sg.startswith(("u", "no", "false", "0")):
                    signed = False
                elif any(k in sg for k in ("signed", "sint", "int", "yes", "true", "1")) or sg == "s":
                    signed = True
                if any(k in sg for k in ("float", "double", "real")):
                    ctx.warn("Float/double signals are not supported; they were read as unsigned integers.")
            scale = _num(cell(r, "factor"))
            if scale is None or scale == 0:
                scale = 1.0
            comment = cell(r, "comment")
            mn, mx = _num(cell(r, "min")), _num(cell(r, "max"))
            if mn is None and mx is None:
                rg = _range_of(comment) if not _flags(comment)[0] else None
                if rg:
                    mn, mx = rg
                    if mn < 0 and not signed:
                        signed = True
                        ctx.warn(f"{fr['name']}.{sn}: read as SIGNED because its remark gives the range {mn:g} to {mx:g}.")
            if not signed and re.search(r"[+(]?\s*-ve|\bnegative\b", comment, re.I):
                signed = True
                ctx.warn(f"{fr['name']}.{sn}: read as SIGNED because its remark mentions negative values.")
            ch = _choices(cell(r, "values"))
            if not ch and not _flags(comment)[0]:
                c2 = _choices(comment)
                if len(c2) >= 2 and max(c2) < (1 << max(1, length or 1)):
                    ch = c2
            unit = cell(r, "unit")
            unit = "" if _n(unit) in ("na", "nil", "none") or re.fullmatch(r"[\d.]+", unit.strip()) else unit[:12]
            fr["recs"].append({"name": sn, "pos": pos, "length": length, "order": row_order, "signed": signed,
                               "scale": scale, "offset": _num(cell(r, "offset")) or 0.0, "min": mn, "max": mx, "unit": unit,
                               "choices": ch, "comment": comment})
        for fr in sec_frames:
            if signal_level:
                _finish_frame(fr, has_bytepos, has_start, ctx)
            else:
                fr["signals"] = []
                fr.pop("recs", None)

    if not frames:
        return None
    any_signal_level = any(("sig" in s["cols"]) and ("start" in s["cols"] or "bytepos" in s["cols"]) and "length" in s["cols"]
                           for s in sections)
    raw_frames = []
    for fr in (frames[k] for k in order):
        fr.pop("_sec", None)
        top = max((max(signal_bits(s["start"], s["length"], s["little"])) for s in fr["signals"]), default=-1)
        need = max(1, top // 8 + 1)
        if not fr["dlc"]:
            fr["dlc"] = 8
        if top >= fr["dlc"] * 8:
            ctx.warn(f"{fr['name']}: signals reach byte {top // 8} but DLC is {fr['dlc']} - DLC raised to {need}.")
            fr["dlc"] = need
        if not fr["signals"]:   # nothing usable: expose the raw bytes so the frame can still be sent / watched
            raw_frames.append(fr["name"])
            fr["signals"] = [{"name": f"Byte{i}", "start": i * 8, "length": 8, "little": True, "signed": False, "scale": 1.0,
                              "offset": 0.0, "min": None, "max": None, "unit": "", "choices": {}, "comment": ""}
                             for i in range(fr["dlc"])]
    if raw_frames and any_signal_level:
        ctx.warn("Frames with no usable signal layout are exposed as raw bytes: " + ", ".join(raw_frames) + ".")
    elif not any_signal_level:
        ctx.warn("This table has frames but no signal layout: each frame is exposed as raw bytes (Byte0..Byte7).")
    fl = [frames[k] for k in order]
    if ctx.bitmode:
        ctx.warn("'Byte Position' holds values above 7 in " + _names(ctx.bitmode) + ", so those frames' positions were read as "
                 "BIT positions (start bit).")
    be = [n for n in ctx.declared_be if any(not s["little"] for f in fl if f["name"] == n for s in f["signals"])]
    if be:
        ctx.warn("Motorola (big endian, start bit = most significant bit) was taken from the sheet for " + _names(be) +
                 "; it was stated once and applied to the bit-position frames that follow. Verify with the firmware owner.")
    if any_signal_level and not any("order" in s["cols"] for s in sections):
        intel = [f["name"] for f in fl if f["signals"] and not f["signals"][0]["name"].startswith("Byte") and f["name"] not in be]
        if intel:
            ctx.warn("No byte-order column: " + _names(intel) + " were read as Intel (little endian).")
    if wide_ids:
        ctx.warn("IDs written with 8 hex digits (e.g. 0x%08X) were read as 29-bit EXTENDED frames. If your device uses 11-bit "
                 "IDs, import again with 'ID format: 11-bit'." % wide_ids[0])
    dirs = {fr["dir"] for fr in fl if fr["dir"]}
    if dirs:
        ctx.warn("Direction (Tx/Rx) was read from the DUT's point of view: Tx = the DUT sends it.")
    nodes = sorted({fr["sender"] for fr in fl if fr["sender"]} - {"DUT", "TESTER"})
    if nodes:
        ctx.warn("Frames were assigned to the nodes named in the sheet (" + ", ".join(nodes) + "). Pick which one is your DUT "
                 "under Message roles → DUT node.")
    if bitrate:
        ctx.warn(f"The sheet says the bus runs at {bitrate // 1000} kbit/s - the project bitrate was set to that.")
    return {"layout": "signals" if any_signal_level else "frames", "id_base": id_base, "id_format": id_format, "frames": fl,
            "warnings": ctx.warns, "skipped": ctx.skipped, "has_direction": bool(dirs), "nodes": nodes, "bitrate": bitrate,
            "columns": {f: str(sections[0]["header"][i]) for f, i in sections[0]["cols"].items()},
            "signals": sum(len(f["signals"]) for f in fl)}


def detect(doc: dict, id_base: str = "auto", id_format: str = "auto", mapping: Optional[dict] = None) -> Optional[dict]:
    """A document read by docs.read_document -> merged result over all its tables, or None.
    With a hand-made mapping only the table it names is read."""
    tables = [b for b in doc.get("blocks", []) if b.get("type") == "table"]
    if mapping is not None:
        try:
            ti = int(mapping.get("table", 0))
        except (TypeError, ValueError):
            ti = 0
        if ti < 0 or ti >= len(tables):
            raise ValueError("That table does not exist in the document.")
        r = parse_table(tables[ti]["rows"], id_base, id_format, mapping)
        if r:
            r["manual"] = True
        return r
    results = [r for r in (parse_table(b["rows"], id_base, id_format) for b in tables) if r]
    if not results:
        return None
    out = results[0]
    seen = {(f["id"], f["ext"]) for f in out["frames"]}
    for r in results[1:]:
        for f in r["frames"]:
            if (f["id"], f["ext"]) not in seen:
                seen.add((f["id"], f["ext"]))
                out["frames"].append(f)
        for w in r["warnings"]:
            if w not in out["warnings"]:
                out["warnings"].append(w)
        out["has_direction"] = out["has_direction"] or r["has_direction"]
        out["bitrate"] = out["bitrate"] or r["bitrate"]
        out["nodes"] = sorted(set(out["nodes"]) | set(r["nodes"]))
        out["skipped"] += r["skipped"]
    out["signals"] = sum(len(f["signals"]) for f in out["frames"])
    return out


def to_dbc_bytes(result: dict) -> bytes:
    import cantools
    from cantools.database.can import Message as CMsg, Node, Signal as CSig
    from cantools.database.conversion import BaseConversion
    msgs, nodes = [], {}
    for f in result["frames"]:
        sigs = []
        for s in f["signals"]:
            ch = {int(k): v for k, v in s["choices"].items()} or None
            sigs.append(CSig(name=s["name"], start=s["start"], length=s["length"],
                             byte_order="little_endian" if s["little"] else "big_endian", is_signed=s["signed"],
                             conversion=BaseConversion.factory(scale=s["scale"], offset=s["offset"], choices=ch, is_float=False),
                             minimum=s["min"], maximum=s["max"], unit=s["unit"] or None,
                             comment=(s["comment"] or "").replace('"', "'")[:200] or None))
        snd = [f["sender"]] if f["sender"] else []
        for n in snd:
            nodes[n] = Node(n, None)
        cmt = f"Periodicity = {f['period_ms']}ms" if f["period_ms"] else None
        msgs.append(CMsg(frame_id=f["id"], name=f["name"], length=f["dlc"], signals=sigs, comment=cmt, senders=snd,
                         is_extended_frame=f["ext"], strict=False))
    db = cantools.database.Database(messages=msgs, nodes=list(nodes.values()), strict=False)
    return db.as_dbc_string().encode("utf-8")


def preview(result: dict) -> dict:
    """Compact view of what was extracted, for the UI."""
    return {"frames": [{"id": f["id"], "ext": f["ext"], "name": f["name"], "dlc": f["dlc"], "period_ms": f["period_ms"],
                        "sender": f["sender"], "signals": [
                            {"name": s["name"], "start": s["start"], "length": s["length"], "little": s["little"],
                             "signed": s["signed"], "scale": s["scale"], "offset": s["offset"], "unit": s["unit"],
                             "min": s["min"], "max": s["max"], "choices": {str(k): v for k, v in s["choices"].items()}}
                            for s in f["signals"]]} for f in result["frames"]],
            "warnings": result["warnings"], "bitrate": result["bitrate"], "nodes": result["nodes"],
            "id_base": result["id_base"], "id_format": result["id_format"], "signals": result["signals"]}
