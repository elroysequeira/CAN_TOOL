"""UDS (ISO 14229) over ISO-TP on the bench's own CAN connection: identity reads + firmware update.

The service sequence is the usual bootloader flow
(10 02 -> 27 -> 31 erase -> 34 -> 36... -> 37 -> 31 CRC -> 11 -> re-read identity), with the
safety/robustness gaps closed:
  * negative responses are decoded; "response pending" (NRC 0x78) is waited out instead of failing
  * a zero seed means "already unlocked"; the key algorithm is selectable (default = echo the seed)
  * the image is inspected first (size, CRC32, SHA-256, product markers) and a mismatching product is refused
  * the post-reset wait polls for the ECU instead of sleeping blindly, and success is judged against an expected
    version when one is given
  * everything is logged; nothing is sent unless the caller explicitly starts it
"""
from __future__ import annotations

import binascii
import hashlib
import os
import queue
import re
import struct
import threading
import time
from typing import Callable, Dict, List, Optional

import isotp

NRC_NAMES = {
    0x10: "generalReject", 0x11: "serviceNotSupported", 0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat", 0x14: "responseTooLong", 0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect", 0x24: "requestSequenceError", 0x25: "noResponseFromSubnetComponent",
    0x26: "failurePreventsExecutionOfRequestedAction", 0x31: "requestOutOfRange", 0x33: "securityAccessDenied",
    0x35: "invalidKey", 0x36: "exceededNumberOfAttempts", 0x37: "requiredTimeDelayNotExpired",
    0x70: "uploadDownloadNotAccepted", 0x71: "transferDataSuspended", 0x72: "generalProgrammingFailure",
    0x73: "wrongBlockSequenceCounter", 0x78: "requestCorrectlyReceived-ResponsePending",
    0x7E: "subFunctionNotSupportedInActiveSession", 0x7F: "serviceNotSupportedInActiveSession",
}
SERVICE_NAMES = {0x10: "DiagnosticSessionControl", 0x11: "ECUReset", 0x14: "ClearDTC", 0x19: "ReadDTC",
                 0x22: "ReadDataByIdentifier", 0x27: "SecurityAccess", 0x31: "RoutineControl", 0x34: "RequestDownload",
                 0x36: "TransferData", 0x37: "RequestTransferExit", 0x3E: "TesterPresent"}
DID_NAMES = {0xF187: "Software version (F187)", 0xF188: "Hardware version (F188)", 0xF190: "Device ID / VIN (F190)",
             0xF18A: "System supplier (F18A)", 0xF189: "ECU software version (F189)", 0xF191: "ECU hardware number (F191)"}
DEFAULT_DIDS = [0xF187, 0xF188, 0xF190]


class UdsError(Exception):
    pass


class UdsTimeout(UdsError):
    pass


class UdsAborted(UdsError):
    pass


class UdsNegative(UdsError):
    def __init__(self, sid: int, nrc: int):
        self.sid, self.nrc = sid, nrc
        super().__init__(f"{SERVICE_NAMES.get(sid, hex(sid))} rejected: NRC 0x{nrc:02X} {NRC_NAMES.get(nrc, '')}".strip())


def hexs(b) -> str:
    return " ".join(f"{x:02X}" for x in b)


def as_text(b: bytes) -> str:
    """Printable text of an identity value. Trailing padding/terminator bytes (00, FF, A5 ...) are tolerated:
    'V0.0.1_MOTOVOLT_M7_LITE' + A5 is shown as text, not as a hex dump."""
    s = bytes(b).rstrip(b"\x00")
    if s and all(32 <= c < 127 for c in s):
        return s.decode("ascii")
    n = 0
    while n < len(s) and 32 <= s[n] < 127:
        n += 1
    if n >= 3 and len(s) - n <= 2:
        return s[:n].decode("ascii")
    return ""


# =============================================================================
# ISO-TP link on the engine's bus
# =============================================================================
class IsoTpLink:
    def __init__(self, engine, tx_id: int, rx_id: int, ext: bool = False):
        self.engine, self.tx_id, self.rx_id, self.ext = engine, tx_id, rx_id, ext
        self.q: "queue.Queue" = queue.Queue(maxsize=5000)
        self.layer = None
        self._run = False
        self._thread: Optional[threading.Thread] = None
        self.errors: List[str] = []
        self.other_format_seen = False       # frames with the response ID but the OTHER ID length (11 vs 29 bit) arrived

    def _tap(self, f):
        if f.arbitration_id == self.rx_id:
            if bool(f.is_extended_id) != self.ext:
                self.other_format_seen = True
                return
            try:
                self.q.put_nowait(f)
            except queue.Full:
                pass

    def _rxfn(self, timeout):
        try:
            f = self.q.get(timeout=max(0.0, timeout or 0.0))
        except queue.Empty:
            return None
        return isotp.CanMessage(arbitration_id=f.arbitration_id, data=bytes(f.data), extended_id=bool(f.is_extended_id),
                                dlc=f.dlc)

    def _txfn(self, m):
        self.engine.send_frame(m.arbitration_id, bytes(m.data), bool(m.is_extended_id))

    def start(self):
        mode = isotp.AddressingMode.Normal_29bits if self.ext else isotp.AddressingMode.Normal_11bits
        addr = isotp.Address(mode, txid=self.tx_id, rxid=self.rx_id)
        params = {"stmin": 0, "blocksize": 8, "wftmax": 0, "tx_padding": 0x00, "rx_flowcontrol_timeout": 1000,
                  "rx_consecutive_frame_timeout": 2000, "max_frame_size": 4095}
        self.layer = isotp.TransportLayer(rxfn=self._rxfn, txfn=self._txfn, address=addr,
                                          error_handler=lambda e: self.errors.append(f"{type(e).__name__}: {e}"),
                                          params=params, read_timeout=0.001)
        self.engine.add_tap(self._tap)
        self._run = True
        self._thread = threading.Thread(target=self._loop, name="isotp", daemon=True)
        self._thread.start()

    def _loop(self):
        while self._run:
            try:
                self.layer.process()
            except Exception as e:
                self.errors.append(f"isotp: {e}")
            time.sleep(0.0005)

    def stop(self):
        self._run = False
        self.engine.remove_tap(self._tap)
        if self._thread:
            self._thread.join(timeout=1.0)

    def flush(self):
        while self.layer.available():
            self.layer.recv()
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass

    def send(self, data: bytes):
        self.layer.send(bytes(data))

    def recv(self, timeout: float) -> Optional[bytes]:
        end = time.time() + timeout
        while time.time() < end:
            if self.layer.available():
                return bytes(self.layer.recv())
            time.sleep(0.001)
        return None


# =============================================================================
# UDS client
# =============================================================================
class UdsClient:
    def __init__(self, link: IsoTpLink, log: Optional[Callable[[str, str], None]] = None,
                 p2: float = 2.0, p2star: float = 15.0, abort: Optional[threading.Event] = None):
        self.link, self.log, self.p2, self.p2star, self.abort = link, log or (lambda d, t: None), p2, p2star, abort

    def request(self, payload: bytes, timeout: Optional[float] = None, quiet: bool = False,
                expect: Optional[Callable[[bytes], bool]] = None) -> bytes:
        """quiet=True: do not put this exchange in the live log (used for the thousands of TransferData blocks - the progress
        bar reports them; logging each one floods the browser)."""
        payload = bytes(payload)
        self.link.flush()
        short = hexs(payload) if len(payload) <= 24 else hexs(payload[:12]) + f" … ({len(payload)} bytes)"
        if not quiet:
            self.log("TX", short)
        self.link.send(payload)
        deadline = time.time() + (timeout or self.p2)
        while True:
            if self.abort is not None and self.abort.is_set():
                raise UdsAborted("aborted")
            r = self.link.recv(0.05)
            if r is None:
                if time.time() > deadline:
                    raise UdsTimeout(f"no response to {SERVICE_NAMES.get(payload[0], hex(payload[0]))}" + self._format_hint())
                continue
            if not quiet or r[0] == 0x7F:      # a negative response is always worth showing
                self.log("RX", hexs(r) if len(r) <= 24 else hexs(r[:24]) + f" … ({len(r)} bytes)")
            if r[0] == 0x7F and len(r) >= 3 and r[1] == payload[0]:
                if r[2] == 0x78:  # response pending: the ECU is busy, keep waiting
                    deadline = time.time() + self.p2star
                    continue
                raise UdsNegative(r[1], r[2])
            if r[0] == (payload[0] + 0x40) & 0xFF:
                if expect is None or expect(r):
                    return r
                # a late answer to an EARLIER request (e.g. after a timeout) - not ours: keep waiting for the right one
            # anything else is a stray frame; keep waiting

    def _format_hint(self) -> str:
        """The ECU answered, but with the other ID length than the one set up: the single most likely UDS misconfiguration."""
        if not getattr(self.link, "other_format_seen", False):
            return ""
        has = "29-bit (extended)" if self.link.ext else "11-bit (standard)"
        got = "11-bit (standard)" if self.link.ext else "29-bit (extended)"
        return (f" - BUT the ECU did answer on 0x{self.link.rx_id:X} with {got} IDs while the tester is set to {has}. "
                f"{'Untick' if self.link.ext else 'Tick'} '29-bit IDs' on the Firmware page.")

    def read_did(self, did: int) -> bytes:
        r = self.request(bytes([0x22, did >> 8, did & 0xFF]), expect=lambda x: len(x) >= 3 and ((x[1] << 8) | x[2]) == did)
        if len(r) < 3 or ((r[1] << 8) | r[2]) != did:
            raise UdsError("unexpected DID in response")
        return r[3:]

    def read_identity(self, dids: List[int]) -> List[dict]:
        """One DID per request (unambiguous parsing). Falls back to the original combined request if the very
        first read times out."""
        out, combined_tried = [], False
        for did in dids:
            item = {"did": f"{did:04X}", "name": DID_NAMES.get(did, f"DID {did:04X}"), "hex": "", "text": "", "error": ""}
            try:
                data = self.read_did(did)
                item["hex"], item["text"] = hexs(data), as_text(data)
            except UdsNegative as e:
                item["error"] = str(e)
            except UdsTimeout as e:
                item["error"] = str(e)
                if not out and not combined_tried:
                    combined_tried = True
                    try:
                        return self._combined(dids)
                    except UdsError:
                        pass
            out.append(item)
        return out

    def _combined(self, dids: List[int]) -> List[dict]:
        req = bytes([0x22] + [b for d in dids for b in (d >> 8, d & 0xFF)])
        r = self.request(req)
        idx, items = 1, []
        while idx + 1 < len(r):
            did = (r[idx] << 8) | r[idx + 1]
            idx += 2
            start = idx
            while idx + 1 < len(r) and ((r[idx] << 8) | r[idx + 1]) not in dids:
                idx += 1
            if idx + 1 >= len(r):
                idx = len(r)
            data = r[start:idx]
            items.append({"did": f"{did:04X}", "name": DID_NAMES.get(did, f"DID {did:04X}"), "hex": hexs(data),
                          "text": as_text(data), "error": ""})
        return items


def make_key(algo: str, seed: bytes, const: int = 0) -> bytes:
    if algo == "echo":  # the default: return the seed unchanged
        return bytes(seed)
    if algo == "invert":
        return bytes((~b) & 0xFF for b in seed)
    if algo == "xor":
        return bytes(b ^ (const & 0xFF) for b in seed)
    if algo == "add":
        n = len(seed)
        return ((int.from_bytes(seed, "big") + const) % (1 << (8 * n))).to_bytes(n, "big")
    raise UdsError(f"unknown key algorithm '{algo}'")


# =============================================================================
# firmware images
# =============================================================================
def parse_srec(text: str):
    """-> (start_address, image bytes, gap_count). Verifies every record checksum."""
    segs = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line[0] != "S" or len(line) < 4 or line[1] not in "123":
            continue
        try:
            raw = bytes.fromhex(line[2:])
        except ValueError:
            raise UdsError(f"S-record line {n}: not hex")
        if (sum(raw) & 0xFF) != 0xFF:
            raise UdsError(f"S-record line {n}: bad checksum")
        alen = {"1": 2, "2": 3, "3": 4}[line[1]]
        addr = int.from_bytes(raw[1:1 + alen], "big")
        segs.append((addr, raw[1 + alen:-1]))
    if not segs:
        raise UdsError("no data records in S-record file")
    segs.sort()
    start = segs[0][0]
    img, gaps, end = bytearray(), 0, start
    for addr, data in segs:
        if addr > end:
            img += b"\xFF" * (addr - end)
            gaps += 1
        elif addr < end:
            raise UdsError("overlapping S-record segments")
        img += data
        end = addr + len(data)
    return start, bytes(img), gaps


def load_image(path: str):
    """-> (bytes, start_address|None, gap_count)"""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".srec", ".s19"):
        start, img, gaps = parse_srec(open(path, "r", errors="replace").read())
        return img, start, gaps
    return open(path, "rb").read(), None, 0


def inspect_firmware(path: str, keyword: str = "", fallback_keyword: str = "") -> dict:
    """keyword: what the tester typed. If empty, the product the image says it is meant for is used, then fallback_keyword."""
    data, start, gaps = load_image(path)
    name = os.path.basename(path)
    strings = [m.group().decode() for m in re.finditer(rb"[ -~]{6,}", data[:4_000_000])]
    meant = next((m.group(1).strip() for s in strings for m in [re.search(r"meant for (.+?)\s*(?:Vehicle)?\s*-{2,}", s)] if m), "")
    meant = re.sub(r"^(?:%[sdu]\s*)+", "", meant).strip()   # firmware strings are printf templates: 'meant for %s MOTOVOLT ...'
    meant_key = re.split(r"[\s\-,;]+", meant)[0] if meant else ""   # the product word: 'MOTOVOLT - SCOOTER 24 pin HW' -> 'MOTOVOLT'
    versions = []
    for s in strings:
        for v in re.findall(r"\bV\d+\.\d+\.\d+\b", s):
            if v not in versions:
                versions.append(v)
    info = {"name": name, "type": os.path.splitext(name)[1].lstrip(".").lower(), "size": len(data),
            "crc32": f"{binascii.crc32(data) & 0xFFFFFFFF:08X}", "sha256": hashlib.sha256(data).hexdigest(),
            "start_address": None if start is None else f"0x{start:08X}", "gaps": gaps, "meant_for": meant,
            "versions": versions[:6], "product": {"status": "unchecked", "text": "No product keyword set."}}
    typed = keyword.strip()
    kw_raw = typed or meant_key or fallback_keyword.strip()
    info["keyword_used"] = kw_raw
    info["keyword_source"] = "typed" if typed else ("image" if meant_key else ("project" if kw_raw else ""))
    kw = kw_raw.lower()
    if kw:
        hay = (name + "\n" + "\n".join(strings)).lower()
        if kw in hay:
            info["product"] = {"status": "ok", "text": f"“{kw_raw}” found in the image/file name."}
        else:
            others = f" The image says it is for: {meant}." if meant else ""
            info["product"] = {"status": "mismatch",
                               "text": f"“{kw_raw}” does NOT appear in this image or its file name.{others}"}
    warns = []
    if len(data) > 2_000_000:
        lo, hi = len(data) / 30_000 / 60, len(data) / 10_000 / 60
        warns.append(f"Large image ({len(data) / 1e6:.1f} MB). Over CAN this takes roughly {lo:.0f}-{hi:.0f} minutes. "
                     "Make sure this is the file the bootloader expects (normally the small OTA .bin of the product).")
    if info["type"] in ("srec", "s19") and gaps:
        warns.append(f"The S-record has {gaps} gap(s) that would be sent as 0xFF padding.")
    info["warnings"] = warns
    return info


# =============================================================================
# firmware update job
# =============================================================================
STEPS = ["Read identity (before)", "Programming session", "Security access", "Erase memory", "Request download",
         "Transfer data", "Transfer exit", "Verify checksum", "ECU reset", "Wait for reboot", "Verify new version"]

# Standard values, proven on a real ECU:
# IDs 0x7E0/0x7E8, address 0x08020000, DIDs F187/F188/F190, erase 0xFF00, checksum 0xFF01 (CRC32), key = seed echoed,
# block = reported max length, sequence counter 1..15, reset then 30 s wait, ISO-TP stmin 0 / block size 8 / padding 0x00.
DEFAULT_SETTINGS = {
    "profile": "uds_client.py",
    "tx_id": "0x7E0", "rx_id": "0x7E8", "ext": False, "address": "0x08020000", "dids": "F187,F188,F190",
    "erase_rid": "0xFF00", "check_rid": "0xFF01", "key_algo": "echo", "key_const": "0x00",
    "chunk_mode": "reported", "seq_mode": "orig", "reboot_min_s": 30, "reboot_timeout_s": 90,
    "expected_version": "", "allow_same_version": False, "product_keyword": "", "allow_product_mismatch": False,
    "require_identity_match": True, "p2_s": 2.0, "p2star_s": 15.0,
    "keep_tx": False,   # opt-in: leave the simulated frames running during the update (some ECUs need them to stay in programming)
}


def parse_hex(v, name: str) -> int:
    try:
        return int(str(v).strip(), 16)
    except ValueError:
        raise UdsError(f"{name}: '{v}' is not a hex number")


def parse_dids(s: str) -> List[int]:
    out = []
    for tok in re.split(r"[,\s;]+", str(s).strip()):
        if tok:
            try:
                out.append(int(tok, 16))
            except ValueError:
                raise UdsError(f"DID '{tok}' is not hex")
    return out or list(DEFAULT_DIDS)


class FlashJob:
    def __init__(self, engine, settings: dict, image: bytes, info: dict, emit: Callable[[dict], None], recovery: bool = False):
        """recovery=True: re-flash an ECU left half-updated (interrupted transfer, power loss). Such an ECU often sits in its
        bootloader and cannot report an application identity, so the identity reads are optional, the 'already this version'
        shortcut is off and the same version afterwards is accepted. Everything else (session, key, erase, CRC) is unchanged."""
        self.engine, self.s, self.image, self.info, self.emit = engine, {**DEFAULT_SETTINGS, **settings}, image, info, emit
        self.recovery = bool(recovery)
        self.abort_evt = threading.Event()
        self.steps: List[dict] = []
        self.link: Optional[IsoTpLink] = None

    # ---- helpers -----------------------------------------------------------------------
    def log(self, text: str, level: str = "info"):
        self.emit({"type": "log", "level": level, "text": text, "ts": time.time()})

    def _step(self, i: int, state: str, detail: str = ""):
        self.emit({"type": "step", "index": i, "state": state, "detail": detail})

    def _wait(self, seconds: float, label: str):
        end = time.time() + seconds
        while time.time() < end:
            if self.abort_evt.is_set():
                raise UdsAborted("aborted")
            self.emit({"type": "wait", "text": f"{label}: {int(end - time.time())} s"})
            time.sleep(0.25)

    def run(self) -> dict:
        s = self.s
        t0 = time.time()
        res = {"ok": False, "summary": "", "old": {}, "new": {}, "steps": [], "size": len(self.image),
               "crc32": self.info["crc32"], "started": t0}
        i = -1
        err_at_start = self.engine.err_count
        try:
            tx, rx = parse_hex(s["tx_id"], "Tester TX ID"), parse_hex(s["rx_id"], "ECU RX ID")
            addr = parse_hex(s["address"], "Memory address") if str(s["address"]).strip() else None
            if addr is None:
                raise UdsError("Memory address is not set.")
            erase_rid, check_rid = parse_hex(s["erase_rid"], "Erase routine ID"), parse_hex(s["check_rid"], "Checksum routine ID")
            dids = parse_dids(s["dids"])
            key_const = parse_hex(s["key_const"], "Key constant") if str(s["key_const"]).strip() else 0
            self.link = IsoTpLink(self.engine, tx, rx, bool(s["ext"]))
            self.link.start()
            cli = UdsClient(self.link, lambda d, t: self.emit({"type": "frame", "dir": d, "text": t, "ts": time.time()}),
                            float(s["p2_s"]), float(s["p2star_s"]), self.abort_evt)

            def step(idx, fn):
                nonlocal i
                i = idx
                self._step(idx, "run")
                e0 = self.engine.err_count
                detail = fn() or ""
                d_err = self.engine.err_count - e0       # CAN error frames the adapter reported while this step ran
                if d_err > 0:
                    detail += f"  ·  {d_err} CAN error frame(s) on the bus"
                res["steps"].append({"desc": STEPS[idx], "ok": True, "detail": detail})
                self._step(idx, "ok", detail)

            # 0 identity before
            def s0():
                try:
                    items = cli.read_identity(dids)
                except UdsAborted:
                    raise
                except UdsError as e:
                    if not self.recovery:
                        raise
                    items = []
                    self.log(f"Identity read failed ({e})", "warn")
                ok = [x for x in items if not x["error"]]
                if not ok and self.recovery:
                    res["old"], res["old_hex"] = {}, {}
                    self.log("RECOVERY MODE: the ECU reports no application identity (normal after an interrupted update) - "
                             "continuing without it.", "warn")
                    return "no identity (recovery mode)"
                if not ok:
                    hint = next((x["error"].split(" - BUT ", 1)[1] for x in items if " - BUT " in x["error"]), "")
                    raise UdsError("ECU did not answer any identity read - check IDs, wiring and bitrate." +
                                   (" BUT " + hint if hint else ""))
                res["old"] = {x["did"]: (x["text"] or x["hex"]) for x in ok}
                res["old_hex"] = {x["did"]: x["hex"] for x in ok}
                self.log("Current: " + ", ".join(f"{k}={v}" for k, v in res["old"].items()))
                kw = str(s["product_keyword"]).strip()
                if kw and s["require_identity_match"] and not s["allow_product_mismatch"] and not self.recovery \
                        and kw.lower() not in json_like(res["old"]).lower():
                    raise UdsError(f"The ECU's identity ({json_like(res['old'])}) does not contain '{kw}' - this does not look "
                                   "like the product the image is for. Nothing was changed on the ECU.")
                tv = target_version(s, self.info.get("name", ""))
                if tv and has_version(json_like(res["old"]), tv) and not s["allow_same_version"] and not self.recovery:
                    raise VersionUnchanged(f"FW version is unchanged: the ECU already reports {tv} "
                                           f"({json_like(res['old'])}). Nothing was flashed. "
                                           "(Tick 'allow same version' to re-flash it on purpose.)")
                return ", ".join(f"{k}={v}" for k, v in res["old"].items())
            step(0, s0)

            def s1():
                cli.request(bytes([0x10, 0x02]))
                return "programming session active"
            step(1, s1)

            def s2():
                r = cli.request(bytes([0x27, 0x01]))
                seed = r[2:]
                if not seed or not any(seed):
                    return "seed is zero - already unlocked"
                key = make_key(s["key_algo"], seed, key_const)
                self.log(f"Seed {hexs(seed)} -> key {hexs(key)} ({s['key_algo']})")
                cli.request(bytes([0x27, 0x02]) + key)
                return f"unlocked ({s['key_algo']} key)"
            step(2, s2)

            def s3():
                cli.request(bytes([0x31, 0x01, erase_rid >> 8, erase_rid & 0xFF]), timeout=float(s["p2star_s"]))
                return f"routine 0x{erase_rid:04X}"
            step(3, s3)

            maxlen = {"v": 0}

            def s4():
                size = len(self.image)
                req = bytes([0x34, 0x00, 0x44]) + addr.to_bytes(4, "big") + size.to_bytes(4, "big")
                r = cli.request(req)
                n = (r[1] >> 4) if len(r) > 1 else 0
                ml = int.from_bytes(r[2:2 + n], "big") if n and len(r) >= 2 + n else int.from_bytes(r[2:], "big")
                if ml <= 2:
                    raise UdsError(f"ECU reported an unusable block length ({ml}).")
                maxlen["v"] = ml
                return f"{size} bytes @ 0x{addr:08X}, block length {ml}"
            step(4, s4)

            def s5():
                ml = maxlen["v"]
                chunk = ml if s["chunk_mode"] == "reported" else ml - 2
                chunk = min(chunk, 4095 - 2)
                total = (len(self.image) + chunk - 1) // chunk
                seq, off, n, last = 1, 0, 0, -1
                while off < len(self.image):
                    if self.abort_evt.is_set():
                        raise UdsAborted("aborted")
                    part = self.image[off:off + chunk]
                    r = cli.request(bytes([0x36, seq]) + part, timeout=float(s["p2star_s"]), quiet=True)
                    if len(r) < 2 or r[1] != seq:
                        raise UdsError(f"block {n + 1}: wrong sequence counter in response")
                    off += len(part)
                    n += 1
                    seq += 1
                    if s["seq_mode"] == "orig":
                        if seq > 0x0F:
                            seq = 1
                    elif seq > 0xFF:
                        seq = 0
                    pct = n * 100 // total
                    if pct != last:
                        last = pct
                        self.emit({"type": "progress", "pct": pct, "text": f"{n}/{total} blocks"})
                return f"{n} blocks of up to {chunk} bytes"
            step(5, s5)

            def s6():
                cli.request(bytes([0x37]))
                return "ok"
            step(6, s6)

            def s7():
                crc = binascii.crc32(self.image) & 0xFFFFFFFF
                cli.request(bytes([0x31, 0x01, check_rid >> 8, check_rid & 0xFF]) + struct.pack(">I", crc),
                            timeout=float(s["p2star_s"]))
                return f"CRC32 {crc:08X} accepted"
            step(7, s7)

            def s8():
                try:
                    cli.request(bytes([0x11, 0x01]), timeout=1.5)
                except (UdsTimeout, UdsNegative) as e:  # many ECUs reboot without answering
                    self.log(f"No/negative reset response ({e}) - continuing", "warn")
                return "reset requested"
            step(8, s8)

            def s9():
                self._wait(float(s["reboot_min_s"]), "ECU rebooting")
                end = time.time() + float(s["reboot_timeout_s"])
                prev, answered, last_seen = None, False, {}
                while time.time() < end:
                    if self.abort_evt.is_set():
                        raise UdsAborted("aborted")
                    try:
                        items = cli.read_identity(dids)
                    except UdsError:
                        items = []
                    ok = [x for x in items if not x["error"]]
                    if ok:
                        answered = True
                        cur = {x["did"]: x["hex"] for x in ok}
                        last_seen = {x["did"]: (x["text"] or x["hex"]) for x in ok}
                        # a half-booted ECU can already answer 0x22 with a placeholder ('00'): accept the identity only when it
                        # looks like the one before the update (same shape) and two reads in a row agree
                        if identity_plausible(cur, res.get("old_hex", {})) and cur == prev:
                            res["new"] = last_seen
                            return "ECU is back"
                        prev = cur
                    time.sleep(2.0)
                if answered:
                    raise UdsError("The ECU answers again, but its identity never settled to a valid value "
                                   f"(last seen: {json_like(last_seen)}). Not accepted as a verified update - "
                                   "try a longer wait, or read the identity again later.")
                raise UdsError("ECU did not come back after the reset.")
            step(9, s9)

            def s10():
                old_sw = " ".join(str(v) for k, v in res["old"].items() if k == "F187")
                new_sw = " ".join(str(v) for k, v in res["new"].items() if k == "F187")
                exp = str(s["expected_version"]).strip()
                self.log(f"Before: {old_sw or res['old']}   After: {new_sw or res['new']}")
                same = res["old"] == res["new"]
                if exp and not has_version(json_like(res["new"]), exp):
                    raise UdsError(f"Expected version '{exp}' not reported (got {new_sw or res['new']}).")
                if same and not s["allow_same_version"] and not self.recovery:
                    raise UdsError(f"FW version is unchanged ({new_sw or json_like(res['new'])} before and after) - "
                                   "the update did not take effect "
                                   "(tick 'allow same version' if you flashed the same version on purpose).")
                if same:
                    return (f"FW version is unchanged ({new_sw or json_like(res['new'])}) - same version re-flashed on purpose"
                            + (" (recovery)" if self.recovery else ""))
                if exp:
                    return f"version '{exp}' confirmed"
                return f"now {new_sw or res['new']}"
            step(10, s10)
            res["ok"] = True
            res["summary"] = "Firmware update completed and verified."
        except VersionUnchanged as e:
            res["skipped"] = True
            res["summary"] = str(e)
            self._step(i, "ok", "FW version is unchanged - not flashed")
            res["steps"].append({"desc": STEPS[i], "ok": True, "detail": "FW version is unchanged - not flashed"})
            self.log(res["summary"], "warn")
        except UdsAborted:
            res["aborted"], res["failed_at"] = True, i
            res["summary"] = ("Aborted by operator at '" + (STEPS[i] if 0 <= i < len(STEPS) else "setup") + "'. " +
                              ("Nothing had been erased - the ECU is unchanged." if i < 3 else
                               "The image was already written and checked - only the final verification was skipped." if i >= 8 else
                               "The ECU's application was erased and not completely rewritten: it may not start until it is re-flashed "
                               "(use Recovery re-flash)."))
            if i >= 0:
                self._step(i, "fail", "aborted")
                res["steps"].append({"desc": STEPS[i], "ok": False, "detail": "aborted"})
        except Exception as e:
            res["failed_at"] = i
            res["summary"] = f"Failed at '{STEPS[i] if 0 <= i < len(STEPS) else 'setup'}': {e}"
            if i >= 0:
                self._step(i, "fail", str(e))
                res["steps"].append({"desc": STEPS[i], "ok": False, "detail": str(e)})
            self.log(res["summary"], "error")
            if i >= 3:
                self.log("The ECU may be left without a valid application. Power-cycle it and retry the update.", "warn")
        finally:
            if self.link:
                self.link.stop()
            res["finished"] = time.time()
            res["bus_errors"] = self.engine.err_count - err_at_start
            if res["bus_errors"]:
                self.log(f"{res['bus_errors']} CAN error frame(s) were reported by the adapter during the update "
                         "(see the step list for where).", "warn")
            res["recovery"] = recovery_level(res)
            self.emit({"type": "done", "ok": res["ok"], "skipped": bool(res.get("skipped")), "summary": res["summary"],
                       "aborted": bool(res.get("aborted")), "recovery": res["recovery"]})
        return res


def recovery_level(res: dict) -> str:
    """What is left to do after an update that did not complete:
       'reflash' - the application was erased and not completely rewritten: the ECU may not start, re-flash it;
       'verify'  - the image was written and its CRC accepted, only the reset/verification did not finish: power-cycle the ECU
                   and read its identity;
       ''        - the ECU was not touched (or the update succeeded)."""
    if res.get("ok") or res.get("skipped"):
        return ""
    at = res.get("failed_at", -1)
    if 3 <= at <= 7:
        return "reflash"
    if at >= 8:
        return "verify"
    return ""


def json_like(d: dict) -> str:
    return " ".join(str(v) for v in d.values())


def identity_plausible(new_hex: Dict[str, str], old_hex: Dict[str, str]) -> bool:
    """New identity (DID -> hex bytes) has the same shape as the one read before the update: every DID that had real data
    (3+ bytes) still has real data - not a lone 00/FF placeholder or a repeated filler byte."""
    for did, old in old_hex.items():
        ob = old.split()
        if len(ob) < 3:
            continue
        nb = new_hex.get(did, "").split()
        if len(nb) < 3 or len(set(nb)) == 1:
            return False
    return True


class VersionUnchanged(UdsError):
    """The ECU already runs the version the image is for - nothing is flashed."""


def has_version(text: str, ver: str) -> bool:
    """True if `ver` (e.g. V0.0.1) occurs in `text` as a whole version token (V0.0.1 must not match V0.0.10)."""
    ver = str(ver).strip()
    return bool(ver) and re.search(r"(?<![0-9.])" + re.escape(ver) + r"(?![0-9]|\.[0-9])", text, re.I) is not None


def target_version(settings: dict, image_name: str) -> str:
    """The version this update is meant to install: the one the tester typed, else the one in the file name."""
    exp = str(settings.get("expected_version", "")).strip()
    if exp:
        return exp
    found = re.findall(r"(?<![0-9.])V\d+\.\d+\.\d+(?![0-9]|\.[0-9])", image_name, re.I)
    return found[0] if len(set(f.lower() for f in found)) == 1 else ""
