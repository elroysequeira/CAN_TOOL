"""A fake UDS ECU on a python-can virtual bus, for testing the flash flow without hardware."""
import binascii
import struct
import threading
import time

import can
import isotp


class FakeEcu:
    def __init__(self, channel, tx_id=0x7E8, rx_id=0x7E0, sw="V0.0.1_VARCAS", new_sw="V0.0.2_VARCAS", hw="HW-A", dev="DEV-0001",
                 key_algo="echo", maxlen=256, reboot_s=0.5, seed=b"\x11\x22\x33\x44", ignore_version_change=False,
                 startup_s=0.0, terminator=b""):
        self.bus = can.Bus(interface="virtual", channel=channel)
        addr = isotp.Address(isotp.AddressingMode.Normal_11bits, txid=tx_id, rxid=rx_id)
        params = {"stmin": 0, "blocksize": 8, "wftmax": 0, "tx_padding": 0x00, "rx_flowcontrol_timeout": 1000,
                  "rx_consecutive_frame_timeout": 2000, "max_frame_size": 4095}
        self.stack = isotp.CanStack(bus=self.bus, address=addr, params=params)
        self.terminator = terminator            # e.g. b"\xA5": real ECUs end identity strings with a marker byte
        self.did = {0xF187: sw.encode() + terminator, 0xF188: hw.encode(), 0xF190: dev.encode() + terminator}
        self.startup_s, self.placeholder_until = startup_s, 0.0   # after a reset the ECU first answers F187 with a lone 00
        self.new_sw, self.key_algo, self.maxlen, self.reboot_s, self.seed = new_sw, key_algo, maxlen, reboot_s, seed
        self.ignore_version_change = ignore_version_change
        self.session = 1
        self.unlocked = False
        self.erased = False
        self.download = None  # (addr, size)
        self.received = bytearray()
        self.next_seq = 1
        self.crc_ok = None
        self.down_until = 0.0
        self.log = []
        self._stop = threading.Event()
        self.stack.start()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def stop(self):
        self._stop.set()
        self._t.join(timeout=2)
        self.stack.stop()
        self.bus.shutdown()

    def _send(self, data):
        self.stack.send(list(data))

    def _neg(self, sid, nrc):
        self._send([0x7F, sid, nrc])

    def _loop(self):
        while not self._stop.is_set():
            msg = self.stack.recv(0.01)
            if not msg:
                continue
            if time.time() < self.down_until:
                continue  # rebooting: silent
            msg = bytes(msg)
            self.log.append(msg[:8].hex())
            try:
                self._handle(msg)
            except Exception as e:  # a fake must never kill the test thread
                self.log.append(f"fake-ecu error: {e}")

    def _handle(self, m):
        sid = m[0]
        if sid == 0x22:
            dids = [(m[i] << 8) | m[i + 1] for i in range(1, len(m) - 1, 2)]
            out = [0x62]
            for d in dids:
                if d not in self.did:
                    return self._neg(0x22, 0x31)
                val = b"\x00" if (d == 0xF187 and time.time() < self.placeholder_until) else self.did[d]
                out += [d >> 8, d & 0xFF] + list(val)
            return self._send(out)
        if sid == 0x10:
            self.session = m[1]
            return self._send([0x50, m[1], 0x00, 0x32, 0x01, 0xF4])
        if sid == 0x27:
            if m[1] == 0x01:
                return self._send([0x67, 0x01] + list(self.seed))
            if m[1] == 0x02:
                want = bytes(self.seed) if self.key_algo == "echo" else bytes((~b) & 0xFF for b in self.seed)
                if bytes(m[2:]) != want:
                    return self._neg(0x27, 0x35)
                self.unlocked = True
                return self._send([0x67, 0x02])
        if sid == 0x31:
            rid = (m[2] << 8) | m[3]
            if not self.unlocked:
                return self._neg(0x31, 0x33)
            if rid == 0xFF00:
                self._send([0x7F, 0x31, 0x78])  # response pending, as a slow flash erase would
                time.sleep(0.3)
                self.erased = True
                self.received = bytearray()
                return self._send([0x71, 0x01, 0xFF, 0x00, 0x00])
            if rid == 0xFF01:
                crc = struct.unpack(">I", bytes(m[4:8]))[0]
                self.crc_ok = crc == (binascii.crc32(bytes(self.received)) & 0xFFFFFFFF)
                return self._send([0x71, 0x01, 0xFF, 0x01, 0x00]) if self.crc_ok else self._neg(0x31, 0x72)
        if sid == 0x34:
            if not self.erased:
                return self._neg(0x34, 0x22)
            addr, size = struct.unpack(">II", bytes(m[3:11]))
            self.download = (addr, size)
            self.next_seq = 1
            return self._send([0x74, 0x20, self.maxlen >> 8, self.maxlen & 0xFF])
        if sid == 0x36:
            if m[1] != self.next_seq:
                return self._neg(0x36, 0x73)
            self.received += bytes(m[2:])
            self.next_seq += 1
            if self.next_seq > 0x0F:
                self.next_seq = 1
            return self._send([0x76, m[1]])
        if sid == 0x37:
            return self._send([0x77])
        if sid == 0x11:
            self._send([0x51, 0x01])
            if self.crc_ok and not self.ignore_version_change:
                self.did[0xF187] = self.new_sw.encode() + self.terminator
            self.down_until = time.time() + self.reboot_s
            self.placeholder_until = self.down_until + self.startup_s
            return
        self._neg(sid, 0x11)
