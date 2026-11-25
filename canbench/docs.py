"""Supporting-document ingestion.

Reads .docx / .xlsx / .csv / .tsv / .md / .txt (stdlib only) and .pdf (needs pypdf),
then links what it finds to the DBC:

  requirements  table rows / lines that mention DBC signals (the spec checklist)
  value maps    "label -> number" tables (e.g. alert-code lists) matched to DBC enums/flags
  mismatches    signal-looking names in the doc that are not in the DBC (+ closest guesses)

All matching is heuristic, so every result carries a confidence and is shown
for review in the UI; nothing is hidden from the tester.
"""
from __future__ import annotations

import csv
import difflib
import hashlib
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

from .dbcmodel import Model, Sig, Comp, clean_num

NA_RE = re.compile(r"^\s*(nil|n/?a|na|not used|none|-+|tbd)\s*$", re.I)
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+")
INT_RE = re.compile(r"^\s*(0x[0-9a-fA-F]+|\d{1,5})\s*$")
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


# ============================================================================
# readers -> {"name","kind","blocks":[{"type":"para","text"} | {"type":"table","rows":[[..]]}],"error"}
# ============================================================================
def _clean(t: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", t or "").strip()


def read_docx(path: str) -> List[dict]:
    blocks: List[dict] = []
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(f"{W}body")

    def ptext(p):
        parts = []
        for n in p.iter():
            if n.tag == f"{W}t":
                parts.append(n.text or "")
            elif n.tag in (f"{W}br", f"{W}tab"):
                parts.append(" ")
        return _clean("".join(parts))

    for el in (body if body is not None else []):
        if el.tag == f"{W}p":
            t = ptext(el)
            if t:
                blocks.append({"type": "para", "text": t})
        elif el.tag == f"{W}tbl":
            rows = []
            for tr in el.findall(f"{W}tr"):
                cells = []
                for tc in tr.findall(f"{W}tc"):
                    cells.append(_clean(" / ".join(x for x in (ptext(p) for p in tc.findall(f"{W}p")) if x)))
                if any(cells):
                    rows.append(cells)
            if rows:
                blocks.append({"type": "table", "rows": rows})
    return blocks


def read_xlsx(path: str) -> List[dict]:
    blocks = []
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{S}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{S}t")))
        names = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        for n in names:
            rows = []
            for r in ET.fromstring(z.read(n)).iter(f"{S}row"):
                row: Dict[int, str] = {}
                for c in r.findall(f"{S}c"):
                    ref = re.match(r"([A-Z]+)", c.get("r", ""))
                    if not ref:
                        continue
                    col = 0
                    for ch in ref.group(1):
                        col = col * 26 + ord(ch) - 64
                    t = c.get("t")
                    v = c.find(f"{S}v")
                    if t == "s" and v is not None and v.text is not None:
                        val = shared[int(v.text)]
                    elif t == "inlineStr":
                        val = "".join(x.text or "" for x in c.iter(f"{S}t"))
                    else:
                        val = v.text if v is not None and v.text is not None else ""
                    row[col] = _clean(val)
                if row:
                    rows.append([row.get(i, "") for i in range(1, max(row) + 1)])
            if rows:
                blocks.append({"type": "table", "rows": rows})
    return blocks


def read_csv(path: str) -> List[dict]:
    text = open(path, "r", encoding="utf-8-sig", errors="replace").read()
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [[_clean(c) for c in r] for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    return [{"type": "table", "rows": rows}] if rows else []


def read_text(path: str) -> List[dict]:
    blocks, tbl = [], []
    for line in open(path, "r", encoding="utf-8-sig", errors="replace").read().splitlines():
        s = line.strip()
        if s.startswith("|") and s.endswith("|"):
            cells = [_clean(c) for c in s.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            tbl.append(cells)
            continue
        if tbl:
            blocks.append({"type": "table", "rows": tbl})
            tbl = []
        if s:
            blocks.append({"type": "para", "text": _clean(s)})
    if tbl:
        blocks.append({"type": "table", "rows": tbl})
    return blocks


def read_pdf(path: str) -> List[dict]:
    try:
        import pypdf
    except ImportError:
        raise RuntimeError("PDF support needs the 'pypdf' package (pip install pypdf).")
    blocks = []
    for page in pypdf.PdfReader(path).pages:
        for line in (page.extract_text() or "").splitlines():
            if line.strip():
                blocks.append({"type": "para", "text": _clean(line)})
    return blocks


READERS = {".docx": read_docx, ".xlsx": read_xlsx, ".csv": read_csv, ".tsv": read_csv,
           ".md": read_text, ".txt": read_text, ".pdf": read_pdf}
DOC_EXTS = tuple(READERS)


def read_document(path: str) -> dict:
    import os
    ext = os.path.splitext(path)[1].lower()
    doc = {"name": os.path.basename(path), "kind": ext.lstrip("."), "blocks": [], "error": ""}
    fn = READERS.get(ext)
    if fn is None:
        doc["error"] = f"Unsupported document type '{ext}'."
        return doc
    try:
        doc["blocks"] = fn(path)
    except Exception as e:
        doc["error"] = f"Could not read: {e}"
    return doc


# ============================================================================
# signal matching
# ============================================================================
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


class Matcher:
    """Finds DBC signal names inside free text."""

    def __init__(self, model: Model):
        self.model = model
        self.pats: List[tuple] = []  # (regex, item)
        self.norm: Dict[str, List[object]] = {}
        for it in model.items.values():
            name = it.name
            if "_" in name:  # distinctive: case-insensitive, '_' may be space/underscore
                body = r"[_\s]*".join(re.escape(p) for p in name.split("_") if p)
                flags = re.I
            else:  # single word: only distinctive ones (SOC, Temp6), case-sensitive, to avoid prose hits
                if not (name.isupper() or any(ch.isdigit() for ch in name)):
                    self.norm.setdefault(_norm(name), []).append(it)
                    continue
                body, flags = re.escape(name), 0
            self.pats.append((re.compile(rf"(?<![A-Za-z0-9_]){body}(?![A-Za-z0-9_])", flags), it))
            self.norm.setdefault(_norm(name), []).append(it)

    def find(self, text: str):
        """-> (items, spans). Hi/Lo members collapse into their composite."""
        found, spans = {}, []
        for rx, it in self.pats:
            for m in rx.finditer(text):
                spans.append(m.span())
                target = self.model.items.get(it.hidden_by) if getattr(it, "hidden_by", None) else it
                if target is not None:
                    found[target.uid] = target
        return list(found.values()), spans

    def alias_and_unknown(self, text: str, spans):
        """Underscore identifiers that are not DBC names: exact alias (sig_xxx) or unknown."""
        aliases, unknown = [], []
        names = list(self.norm)
        for m in TOKEN_RE.finditer(text):
            a, b = m.span()
            if any(a < e and b > s for s, e in spans):
                continue
            tok = m.group()
            if len(tok) < 5 or re.fullmatch(r"[A-Za-z]+(_\d+)+", tok):  # 'Automotive_5' is a name, not a signal
                continue
            core = re.sub(r"^(sig|s\d+|m)_", "", tok, flags=re.I)
            hit = self.norm.get(_norm(core))
            if hit and len(hit) == 1:
                it = hit[0]
                it = self.model.items.get(it.hidden_by, it) if getattr(it, "hidden_by", None) else it
                aliases.append((tok, it))
                continue
            guess = difflib.get_close_matches(_norm(core), names, n=3, cutoff=0.74)
            unknown.append((tok, [self.norm[g][0].name for g in guess]))
        return aliases, unknown


# ============================================================================
# analysis
# ============================================================================
def _rid(*parts) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:10]


def _is_header(row: List[str]) -> bool:
    cells = [c for c in row if c]
    return bool(cells) and all(not re.fullmatch(r"[\d\s.:/-]+", c) for c in cells) and len(cells) >= 2


def _title_col(header: Optional[List[str]]) -> Optional[int]:
    for i, h in enumerate(header or []):
        if re.search(r"\b(item|feature|parameter|name)\b", h, re.I):
            return i
    return None


def analyze(model: Model, docs: List[dict]) -> dict:
    mt = Matcher(model)
    msg_norms = {_norm(m.name) for m in model.messages}
    reqs, maps, mism = [], [], []
    seen_unknown = set(msg_norms)
    for doc in docs:
        if doc.get("error"):
            continue
        last_header = None
        for bi, blk in enumerate(doc["blocks"]):
            if blk["type"] == "table":
                rows = blk["rows"]
                ncols = max(len(r) for r in rows)
                header = rows[0] if rows and _is_header(rows[0]) else None
                start = 1 if header else 0
                if header:
                    last_header = header
                elif last_header and len(last_header) == ncols:
                    header = last_header  # continuation of the previous table
                tcol = _title_col(header)
                tbl = []
                for ri, row in enumerate(rows[start:], start):
                    r = _row_requirement(doc["name"], mt, row, tcol, f"table {bi + 1}, row {ri + 1}", header)
                    if r:
                        tbl.append(r)
                    _collect_unknown(mt, " ".join(row), doc["name"], mism, seen_unknown)
                linked = sum(1 for r in tbl if r["signals"])
                if len(tbl) >= 3 and linked / len(tbl) >= 0.25:
                    reqs.extend(tbl)  # a spec table: keep every row, linked or not
                else:
                    reqs.extend(r for r in tbl if r["signals"])
                vm = _table_value_map(doc["name"], blk, mt, bi)
                if vm:
                    maps.append(vm)
            else:
                text = blk["text"]
                items, spans = mt.find(text)
                # free text counts as a requirement only when it names a signal exactly (with its underscores);
                # otherwise it is usually a label from a code list ("Controller Error") handled as a value map
                items = [i for i in items if i.name.lower() in text.lower()]
                if items and len(text) <= 220:
                    reqs.append({"id": _rid(doc["name"], text), "doc": doc["name"], "loc": f"line {bi + 1}",
                                 "no": "", "title": text[:80], "text": text, "signals": [i.uid for i in items],
                                 "aliases": [], "na": False})
                _collect_unknown(mt, text, doc["name"], mism, seen_unknown)
        maps.extend(_line_value_maps(doc))
    for vm in maps:
        _resolve_map(vm, model)
    uniq, keys = [], set()
    for r in reqs:  # the SOR and SOW repeat each other: keep the first
        k = (r["no"], _norm(r["title"]))
        if k in keys:
            continue
        keys.add(k)
        uniq.append(r)
    return {"requirements": uniq, "value_maps": maps, "mismatches": mism}


def _collect_unknown(mt: Matcher, text: str, doc: str, out: list, seen: set):
    _, spans = mt.find(text)
    _, unknown = mt.alias_and_unknown(text, spans)
    for tok, guess in unknown:
        k = tok.lower()
        if k not in seen and _norm(tok) not in seen:
            seen.add(k)
            out.append({"doc": doc, "token": tok, "guesses": guess, "context": text[:140]})


def _row_requirement(docname: str, mt: Matcher, row: List[str], tcol: Optional[int], loc: str,
                     header: Optional[List[str]] = None):
    cells = list(row)
    text = " | ".join(c for c in cells if c)
    if not text.strip():
        return None
    items, spans = mt.find(text)
    aliases, _ = mt.alias_and_unknown(text, spans)
    for tok, it in aliases:
        if it.uid not in {i.uid for i in items}:
            items.append(it)
    no = cells[0] if cells and re.fullmatch(r"\d{1,3}", cells[0] or "") else ""
    title = cells[tcol] if tcol is not None and tcol < len(cells) and cells[tcol] else ""
    if not title:
        for c in cells:
            if c and not re.fullmatch(r"[\d.\s]+", c) and len(c) <= 80:
                title = c
                break
    desc = ""
    if header:  # prefer the human-readable column over the raw table row
        for pat in (r"descr|requirement|behaviou?r|remark|note", r"method|signal|trigger"):
            for i, hd in enumerate(header):
                if i < len(cells) and cells[i] and i != tcol and re.search(pat, hd, re.I):
                    desc = cells[i]
                    break
            if desc:
                break
    if not desc:
        desc = " | ".join(c for c in cells if c and c != title and c != no)
    na = any(NA_RE.match(c) for c in cells if c) and not items
    return {"id": _rid(docname, loc, text), "doc": docname, "loc": loc, "no": no, "title": title or text[:60],
            "text": desc, "signals": [i.uid for i in items],
            "aliases": [{"token": t, "uid": i.uid} for t, i in aliases], "na": na}


def _int(s: str) -> Optional[int]:
    m = INT_RE.match(s or "")
    return int(m.group(1), 0) if m else None


def _table_value_map(docname: str, blk: dict, mt: Matcher, bi: int):
    rows = blk["rows"]
    cand = []
    for row in rows:
        cells = [c for c in row if c]
        if len(cells) >= 2 and _int(cells[-1]) is not None and not re.fullmatch(r"[\d.\s]+", cells[0]):
            cand.append({"label": cells[0], "value": _int(cells[-1])})
    body = len(rows) - (1 if rows and _is_header(rows[0]) else 0)
    if len(cand) >= 3 and body and len(cand) / body >= 0.6:
        title = " / ".join(rows[0]) if _is_header(rows[0]) else f"table {bi + 1}"
        return {"id": _rid(docname, title, str(cand[0])), "doc": docname, "title": title, "entries": cand}
    return None


def _line_value_maps(doc: dict) -> List[dict]:
    """PDF/text: 'Label' line followed by a number-only line, or 'Label   12' on one line."""
    out, section, cur = [], "", None
    lines = [b["text"] for b in doc["blocks"] if b["type"] == "para"]
    i = 0
    skip = re.compile(r"^(alert name|value sent to cluster|name|value|code)$", re.I)

    def flush():
        nonlocal cur
        if cur and len(cur["entries"]) >= 3:
            out.append(cur)
        cur = None

    while i < len(lines):
        ln = lines[i]
        if re.match(r"^\d+\.\s+\S", ln) and _int(ln) is None:
            flush()
            section = ln
            cur = {"id": _rid(doc["name"], ln), "doc": doc["name"], "title": ln, "entries": []}
            i += 1
            continue
        if cur is not None and not skip.match(ln):
            m = re.match(r"^(.*\S)\s+(\d{1,5})$", ln)
            if m and not re.fullmatch(r"[\d.\s]+", m.group(1)):
                cur["entries"].append({"label": m.group(1), "value": int(m.group(2))})
            elif i + 1 < len(lines) and _int(lines[i + 1]) is not None and _int(ln) is None:
                cur["entries"].append({"label": ln, "value": _int(lines[i + 1])})
                i += 1
        i += 1
    flush()
    return out


# ---- value map -> DBC target ----------------------------------------------------
def _variants(label: str) -> List[str]:
    v = [label]
    paren = re.findall(r"\(([^)]*)\)", label)
    base = re.sub(r"\([^)]*\)", "", label).strip()
    v += [p for p in paren if re.search(r"[A-Za-z]{3}", p)] + ([base] if base and base != label else [])
    return v


def _sim(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) >= 8 and short in long_:
        return 0.9
    return difflib.SequenceMatcher(None, na, nb).ratio()


def _resolve_map(vm: dict, model: Model):
    flags = [i for i in model.items.values()
             if not getattr(i, "hidden_by", None) and i.kind != "enum" and isinstance(i, Sig)
             and (i.kind == "bool" or (not i.unit and i.scale == 1 and FAULT_RE.search(i.name)))]
    numerics = [i for i in model.items.values()
                if not getattr(i, "hidden_by", None) and i.kind == "num" and getattr(i, "unit", "")]
    enums = [i for i in model.items.values() if isinstance(i, Sig) and i.kind == "enum"]
    for e in vm["entries"]:
        cands = []  # (score, target)
        threshold = re.search(r"([<>]=?)\s*(\d+(?:\.\d+)?)", e["label"])
        for var in _variants(e["label"]):
            for sg in enums:
                for raw, name in sg.choices.items():
                    s = _sim(var, name)
                    if s >= 0.6:
                        cands.append((s, {"signal": sg.uid, "value": raw, "why": f"enum '{name}'"}))
            for sg in flags:
                s = _sim(var, sg.name.replace("_", " "))
                if sg.kind != "bool":
                    s = min(s, 0.94)  # multi-bit flag: "1 = active" is an assumption -> needs review
                if s >= 0.6:
                    cands.append((s, {"signal": sg.uid, "value": 1, "why": f"flag {sg.name} = 1"}))
            if threshold:
                for sg in numerics:
                    s = _sim(re.sub(r"\(.*\)", "", var), sg.name.replace("_", " "))
                    if s >= 0.85:
                        thr, step = float(threshold.group(2)), (abs(sg.scale) or 1.0)
                        op = threshold.group(1)
                        val = thr + step if op == ">" else thr - step if op == "<" else thr
                        cands.append((s - 0.2, {"signal": sg.uid, "value": clean_num(val),
                                                "why": f"{sg.name} {op} {threshold.group(2)} {sg.unit}"}))
        if threshold and any(t["why"].find(" " + threshold.group(1)) >= 0 for _, t in cands):
            cands = [(s, t) for s, t in cands if t["why"].find(" " + threshold.group(1)) >= 0]
        best = {}
        for s, t in sorted(cands, key=lambda x: -x[0]):
            best.setdefault((t["signal"], t["value"]), (s, t))
        ranked = sorted(best.values(), key=lambda x: -x[0])
        e["candidates"] = [{**t, "score": round(s, 2)} for s, t in ranked[:5]]
        clear = ranked and (len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 0.05 or
                            (ranked[0][0] >= 1.0 and ranked[1][0] < 1.0))
        if ranked and ranked[0][0] >= 0.88 and clear:
            e["target"], e["status"] = ranked[0][1], ("auto" if ranked[0][0] >= 0.95 else "review")
        elif ranked and ranked[0][0] >= 0.6 and threshold:
            e["target"], e["status"] = ranked[0][1], "review"
        else:
            e["target"], e["status"] = None, "unresolved"
    n = len(vm["entries"])
    vm["resolved"] = sum(1 for e in vm["entries"] if e["target"])
    vm["total"] = n


FAULT_RE = re.compile(r"(error|fault|flt|alarm|err|alert|warn|runway|runaway|overload|overcurrent)", re.I)
