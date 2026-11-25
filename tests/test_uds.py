"""UDS / firmware / frame-designer tests (virtual bus, fake ECU)."""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import can  # noqa: E402

from canbench import uds  # noqa: E402
from canbench.app import App, AppError  # noqa: E402
from fake_ecu import FakeEcu  # noqa: E402


def wait_for(fn, timeout=5.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


def make_srec(addr, data, rec=32):
    lines = ["S00600004844521B"]
    for i in range(0, len(data), rec):
        chunk = data[i:i + rec]
        a = addr + i
        body = bytes([len(chunk) + 5]) + a.to_bytes(4, "big") + chunk
        lines.append("S3" + (body + bytes([(~sum(body)) & 0xFF])).hex().upper())
    return "\n".join(lines) + "\n"


class TestImages(unittest.TestCase):
    def test_srec_roundtrip_and_checksum(self):
        data = bytes(range(200))
        start, img, gaps = uds.parse_srec(make_srec(0x10000, data))
        self.assertEqual((start, img, gaps), (0x10000, data, 0))
        bad = make_srec(0x10000, data).replace("S3", "S3", 1)
        lines = bad.splitlines()
        lines[2] = lines[2][:-2] + ("00" if lines[2][-2:] != "00" else "01")
        with self.assertRaises(uds.UdsError):
            uds.parse_srec("\n".join(lines))

    def test_srec_gap_is_filled_and_reported(self):
        text = make_srec(0x100, b"\x01" * 16) + make_srec(0x120, b"\x02" * 16).split("\n", 1)[1]
        start, img, gaps = uds.parse_srec(text)
        self.assertEqual((start, len(img), gaps), (0x100, 0x30, 1))
        self.assertEqual(img[16:32], b"\xFF" * 16)

    def test_product_check(self):
        d = tempfile.mkdtemp()
        try:
            ok = os.path.join(d, "fw_a.bin")
            open(ok, "wb").write(b"\x00" * 64 + b"---This Firmware is meant for VARCAS Vehicle ---\x00 V1.2.3 \x00" + b"\x00" * 64)
            other = os.path.join(d, "ATHER_x.bin")
            open(other, "wb").write(b"\x00" * 64 + b"AT+QMTPUBEX=\"x/ather\" V2.0.1\x00" * 2)
            a = uds.inspect_firmware(ok, "varcas")
            self.assertEqual(a["product"]["status"], "ok")
            self.assertEqual(a["meant_for"], "VARCAS")
            self.assertIn("V1.2.3", a["versions"])
            b = uds.inspect_firmware(other, "Varcas")
            self.assertEqual(b["product"]["status"], "mismatch")
            self.assertEqual(uds.inspect_firmware(other, "")["product"]["status"], "unchecked")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_key_algorithms(self):
        s = bytes([1, 2, 3, 4])
        self.assertEqual(uds.make_key("echo", s), s)
        self.assertEqual(uds.make_key("invert", s), bytes([254, 253, 252, 251]))
        self.assertEqual(uds.make_key("xor", s, 0xFF), bytes([254, 253, 252, 251]))
        self.assertEqual(uds.make_key("add", bytes([0, 0, 0xFF, 0xFF]), 1), bytes([0, 1, 0, 0]))


class UdsCase(unittest.TestCase):
    chan = "t_uds"

    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="canbench_uds_")
        self.app = App(self.ws)
        self.app.new_project("uds")
        self.image = b"\x00" * 100 + b"---This Firmware is meant for VARCAS Vehicle ---" + os.urandom(3000)
        self.app.add_file("VARCAS_test.bin", self.image)
        self.app.set_meta({"dut": {"name": "Varcas cluster", "serial": "", "firmware": ""}})
        self.app.set_uds_settings({"address": "0x00010000", "reboot_min_s": 1, "reboot_timeout_s": 10})
        self.app.connect("virtual", self.chan, 500000)

    def tearDown(self):
        wait_for(lambda: not self.app.uds_run["running"], 15)
        if getattr(self, "ecu", None):
            self.ecu.stop()
        self.app.shutdown()
        shutil.rmtree(self.ws, ignore_errors=True)

    def flash(self, **kw):
        self.app.start_flash(kw.pop("name", "VARCAS_test.bin"), kw.pop("confirm", "FLASH"))
        self.assertTrue(wait_for(lambda: not self.app.uds_run["running"], 40), "flash did not finish")
        return self.app.results.items[-1]


class TestFlash(UdsCase):
    def test_successful_update_is_verified_and_recorded(self):
        self.ecu = FakeEcu(self.chan)
        r = self.flash()
        self.assertEqual(r["verdict"], "PASS", r["note"])
        self.assertEqual(r["kind"], "firmware")
        self.assertTrue(all(c["ok"] for c in r["checks"]) and len(r["checks"]) == 11)
        self.assertEqual(bytes(self.ecu.received), self.image)
        self.assertIn("V0.0.2", r["note"])
        self.assertTrue(self.ecu.crc_ok)

    def test_identity_read_and_expert_gating(self):
        self.ecu = FakeEcu(self.chan, sw="V9.9.9")
        res = self.app.uds_identity()
        self.assertEqual(res["answered"], 3)
        self.assertEqual({i["did"]: i["text"] for i in res["items"]}["F187"], "V9.9.9")
        raw = self.app.uds_raw("22 F1 90")
        self.assertEqual(raw["text"], "DEV-0001")
        with self.assertRaises(AppError):
            self.app.uds_raw("31 01 FF 00")            # erase: needs expert mode
        with self.assertRaises(AppError):
            self.app.uds_raw("34 00 44 00 00 00 00 00 00 10 00")
        neg = self.app.uds_raw("22 12 34")             # unsupported DID -> NRC shown, not an exception
        self.assertTrue(neg["negative"])
        self.assertIn("requestOutOfRange", neg["text"])

    def test_refusals(self):
        self.ecu = FakeEcu(self.chan)
        with self.assertRaises(AppError) as c:
            self.app.start_flash("VARCAS_test.bin", "yes")
        self.assertIn("FLASH", str(c.exception))
        self.app.add_file("ATHER_other.bin", b"\x00" * 100 + b'AT+QMTPUBEX="x/ather" V2.0.1' + b"\x00" * 100)
        with self.assertRaises(AppError) as c:
            self.app.start_flash("ATHER_other.bin", "FLASH")
        self.assertIn("Product check failed", str(c.exception))
        self.app.set_uds_settings({"address": ""})
        with self.assertRaises(AppError) as c:
            self.app.start_flash("VARCAS_test.bin", "FLASH")
        self.assertIn("address", str(c.exception))
        self.app.set_uds_settings({"address": "0x10000"})
        self.app.engine.tx_on = True          # as if the bench were transmitting simulated frames
        with self.assertRaises(AppError) as c:
            self.app.start_flash("VARCAS_test.bin", "FLASH")
        self.assertIn("Stop transmitting", str(c.exception))
        self.assertEqual(self.ecu.log, [], "a refused update must not put a single UDS frame on the bus")

    def test_ecu_of_another_product_is_refused_before_anything_changes(self):
        self.ecu = FakeEcu(self.chan, sw="V2.0.1_ATHER")
        r = self.flash()
        self.assertEqual(r["verdict"], "FAIL")
        self.assertIn("does not contain 'VARCAS'", r["note"])      # keyword = what the image says it is meant for
        self.assertEqual(self.ecu.session, 1, "no session change, i.e. the ECU was only read")
        self.assertFalse(self.ecu.erased)

    def test_wrong_key_stops_before_erase(self):
        self.ecu = FakeEcu(self.chan, key_algo="invert")
        r = self.flash()
        self.assertEqual(r["verdict"], "FAIL")
        self.assertIn("invalidKey", r["note"])
        self.assertFalse(self.ecu.erased, "nothing may be erased when security access failed")

    def test_unchanged_version_is_a_failure_unless_allowed(self):
        self.ecu = FakeEcu(self.chan, ignore_version_change=True)
        r = self.flash()
        self.assertEqual(r["verdict"], "FAIL")
        self.assertIn("unchanged", r["note"])
        self.app.set_uds_settings({"allow_same_version": True})
        r2 = self.flash()
        self.assertEqual(r2["verdict"], "PASS")

    def test_target_version_already_installed_is_not_flashed(self):
        """File name says V0.0.1 and the ECU already reports V0.0.1 -> 'FW version is unchanged', nothing touched."""
        self.ecu = FakeEcu(self.chan, sw="V0.0.1_VARCAS_4G_60AH")
        self.app.add_file("VARCAS_4G_V0.0.1.bin", self.image)
        r = self.flash(name="VARCAS_4G_V0.0.1.bin")
        self.assertEqual(r["verdict"], "SKIP")
        self.assertIn("FW version is unchanged", r["note"])
        self.assertEqual(self.ecu.session, 1, "the ECU must only have been read")
        self.assertFalse(self.ecu.erased)
        self.assertEqual(len(self.ecu.received), 0)
        self.assertTrue(self.app.uds_run["skipped"])
        # V0.0.1 must not be confused with V0.0.10
        self.ecu.stop()
        self.ecu = FakeEcu(self.chan, sw="V0.0.10_VARCAS_4G_60AH")
        r2 = self.flash(name="VARCAS_4G_V0.0.1.bin")
        self.assertEqual(r2["verdict"], "PASS", r2["note"])

    def test_same_version_reflash_on_purpose(self):
        self.ecu = FakeEcu(self.chan, sw="V0.0.1_VARCAS_4G_60AH", new_sw="V0.0.1_VARCAS_4G_60AH")
        self.app.add_file("VARCAS_4G_V0.0.1.bin", self.image)
        self.app.set_uds_settings({"allow_same_version": True})
        r = self.flash(name="VARCAS_4G_V0.0.1.bin")
        self.assertEqual(r["verdict"], "PASS", r["note"])
        self.assertIn("FW version is unchanged", " ".join(c["detail"] for c in r["checks"]))
        self.assertEqual(bytes(self.ecu.received), self.image)

    def test_expected_version_equal_to_current_is_not_flashed(self):
        self.ecu = FakeEcu(self.chan, sw="V0.0.2_VARCAS")   # the product guard needs 'varcas' in the identity
        self.app.set_uds_settings({"expected_version": "V0.0.2"})
        r = self.flash()
        self.assertEqual(r["verdict"], "SKIP")
        self.assertFalse(self.ecu.erased)

    def test_export_names_and_csv_carry_the_project_name(self):
        from canbench.results import file_slug, to_csv
        self.assertEqual(file_slug("VARCAS Cluster V0.0.1 / bench #2"), "VARCAS_Cluster_V0.0.1_bench_2")
        self.assertEqual(file_slug("???"), "project")
        n = self.app.export_name("report", "html")
        self.assertTrue(n.startswith("report_uds_") and n.endswith(".html"), n)
        row = {"ts": time.time(), "title": "t", "verdict": "PASS"}
        lines = to_csv([row], "My Project").decode("utf-8-sig").splitlines()
        self.assertTrue(lines[0].startswith("Project,Time"))
        self.assertTrue(lines[1].startswith("My Project,"))
        rep = self.app.report_bytes().decode()
        self.assertIn("CAN test report — uds", rep)

    def test_expected_version(self):
        self.ecu = FakeEcu(self.chan, new_sw="V0.0.2")
        self.app.set_uds_settings({"expected_version": "V0.0.3"})
        r = self.flash()
        self.assertEqual(r["verdict"], "FAIL")
        self.assertIn("V0.0.3", r["note"])

    def test_abort_during_transfer(self):
        self.ecu = FakeEcu(self.chan, maxlen=32)       # ~100 blocks
        self.app.start_flash("VARCAS_test.bin", "FLASH")
        wait_for(lambda: self.app.uds_run.get("pct", 0) >= 5, 15)
        self.app.abort_flash()
        self.assertTrue(wait_for(lambda: not self.app.uds_run["running"], 15))
        r = self.app.results.items[-1]
        self.assertEqual(r["verdict"], "FAIL")
        self.assertIn("Aborted", r["note"])
        self.assertLess(len(self.ecu.received), len(self.image))

    def test_no_ecu_means_clear_error_and_no_flash(self):
        # nobody answers on the bus
        r = self.flash()
        self.assertEqual(r["verdict"], "FAIL")
        self.assertIn("Read identity", r["note"])

    def test_srec_image_is_accepted(self):
        self.ecu = FakeEcu(self.chan)
        data = b"\x00" * 50 + b"---This Firmware is meant for VARCAS Vehicle ---" + os.urandom(600)
        self.app.add_file("VARCAS_app.srec", make_srec(0x10000, data).encode())
        info = self.app.fw_inspect("VARCAS_app.srec")
        self.assertEqual((info["start_address"], info["size"]), ("0x00010000", len(data)))
        r = self.flash(name="VARCAS_app.srec")
        self.assertEqual(r["verdict"], "PASS", r["note"])
        self.assertEqual(bytes(self.ecu.received), data)


class TestRealHardwareLessons(UdsCase):
    """Things the first real-hardware runs showed (Motovolt M7 test unit)."""

    def test_half_booted_ecu_placeholder_is_never_accepted_as_the_new_version(self):
        self.ecu = FakeEcu(self.chan, startup_s=5.0)       # right after reset F187 answers a lone 00 for 5 s
        r = self.flash()
        self.assertEqual(r["verdict"], "PASS", r["note"])
        self.assertIn("V0.0.2", r["note"])
        self.assertNotIn("after: 00", r["note"])

    def test_identity_that_never_settles_is_a_failure_not_a_pass(self):
        self.app.set_uds_settings({"reboot_timeout_s": 4})
        self.ecu = FakeEcu(self.chan, startup_s=60.0)
        r = self.flash()
        self.assertEqual(r["verdict"], "FAIL")
        self.assertIn("never settled", r["note"])

    def test_identity_shown_as_text_even_with_a_terminator_byte(self):
        self.assertEqual(uds.as_text(b"V0.0.1_MOTOVOLT_M7_LITE\xA5"), "V0.0.1_MOTOVOLT_M7_LITE")
        self.assertEqual(uds.as_text(b"CM_866677078289707\xA5\x00"), "CM_866677078289707")
        self.assertEqual(uds.as_text(b"\x00\x01\x02\x03"), "")                    # binary stays hex
        self.assertEqual(uds.as_text(b"AB\xFF\xFE\xFD\xFC"), "")
        self.ecu = FakeEcu(self.chan, sw="V0.0.1_VARCAS", terminator=b"\xA5")
        res = self.app.uds_identity()
        self.assertEqual({i["did"]: i["text"] for i in res["items"]}["F187"], "V0.0.1_VARCAS")

    def test_printf_template_in_image_does_not_break_the_product_keyword(self):
        img = b"\x00" * 64 + b"---This Firmware is meant for %s MOTOVOLT - SCOOTER 24 pin HW ---\x00 V0.0.1 \x00" + b"\x01" * 64
        self.app.add_file("MOTOVOLT_M7_LITE_OTA_V0.0.1.bin", img)
        info = self.app.fw_inspect("MOTOVOLT_M7_LITE_OTA_V0.0.1.bin")
        self.assertEqual((info["keyword_used"], info["product"]["status"]), ("MOTOVOLT", "ok"))
        self.assertEqual(info["meant_for"], "MOTOVOLT - SCOOTER 24 pin HW")

    def test_error_frames_during_the_update_are_attributed_and_reported(self):
        self.ecu = FakeEcu(self.chan, maxlen=32)            # ~100 blocks: long enough to inject
        injector = can.Bus(interface="virtual", channel=self.chan)
        self.app.start_flash("VARCAS_test.bin", "FLASH")
        wait_for(lambda: self.app.uds_run.get("pct", 0) >= 5, 15)
        for _ in range(3):
            injector.send(can.Message(is_error_frame=True, is_extended_id=False, arbitration_id=0, data=[]))
        self.assertTrue(wait_for(lambda: not self.app.uds_run["running"], 40))
        injector.shutdown()
        r = self.app.results.items[-1]
        self.assertEqual(r["verdict"], "PASS", r["note"])
        self.assertIn("CAN error frames during the update: 3", r["note"])
        self.assertTrue(any("3 CAN error frame" in c["detail"] for c in r["checks"] if c["desc"] == "Transfer data"))

    def test_transfer_blocks_do_not_flood_the_live_log(self):
        """Thousands of per-block TX/RX lines froze the browser tab ('page isn't responding') during a real flash."""
        self.ecu = FakeEcu(self.chan, maxlen=32)            # ~100 blocks
        seen = []
        orig = self.app.emit
        self.app.emit = lambda kind, data: (seen.append(data) if kind == "uds" else None, orig(kind, data))[1]
        r = self.flash()
        self.assertEqual(r["verdict"], "PASS", r["note"])
        frames = [e for e in seen if e["type"] == "frame"]
        self.assertFalse(any(e["text"].startswith(("36 ", "76 ")) for e in frames), "TransferData exchanges must not be logged")
        self.assertTrue(any(e["text"].startswith("34 ") for e in frames), "other services are still logged")
        self.assertLess(len(frames), 60)
        self.assertEqual(self.app.uds_run["pct"], 100)

    def test_a_slow_page_is_told_to_resync_instead_of_silently_losing_events(self):
        from canbench.server import Hub
        hub = Hub()
        q = hub.subscribe()
        for i in range(2500):                               # the page is not reading
            hub.publish("uds", {"type": "frame", "i": i})
        self.assertTrue(q.overflow, "dropped events must be flagged so the page reloads its state (it would miss 'done')")
        hub.unsubscribe(q)

    def test_counters_describe_the_current_connection_only(self):
        e = self.app.engine
        e._note_err("CAN error frame received")
        e.tx_total = e.rx_total = 99
        self.assertEqual(e.err_count, 1)
        self.assertEqual(self.app.live()["error"], "CAN error frame received")     # recent -> shown
        e.last_err_t = time.time() - 60
        self.assertEqual(self.app.live()["error"], "")                              # old -> not shown in red any more
        self.app.disconnect()
        self.app.connect("virtual", self.chan, 500000)
        self.assertEqual((e.err_count, e.tx_total, e.rx_total), (0, 0, 0))


class TestScriptDefaults(unittest.TestCase):
    """The bench is configured exactly like the user's uds_client.py unless they change something."""

    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="canbench_def_")
        self.app = App(self.ws)
        self.app.new_project("defaults")

    def tearDown(self):
        self.app.shutdown()
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_defaults_are_the_scripts_values(self):
        s = self.app.uds_settings()
        self.assertEqual((s["tx_id"], s["rx_id"], s["address"]), ("0x7E0", "0x7E8", "0x08020000"))
        self.assertEqual((s["erase_rid"], s["check_rid"], s["key_algo"], s["dids"]), ("0xFF00", "0xFF01", "echo", "F187,F188,F190"))
        self.assertEqual((s["chunk_mode"], s["seq_mode"], s["reboot_min_s"]), ("reported", "orig", 30))
        self.assertFalse(s["allow_same_version"])

    def test_old_saved_values_are_replaced_once_and_reset_button_works(self):
        self.app.project["uds"] = {"address": "0x00010000", "product_keyword": "colour", "tx_id": "0x7E0"}   # saved by an old version
        s = self.app.uds_settings()
        self.assertEqual((s["address"], s["product_keyword"]), ("0x08020000", ""))
        self.app.set_uds_settings({"address": "0x20000"})
        self.assertEqual(self.app.uds_settings()["address"], "0x20000")      # a change made now is kept
        self.app.reset_uds_settings()
        self.assertEqual(self.app.uds_settings()["address"], "0x08020000")

    def test_keyword_comes_from_the_image_not_from_free_text_dut_name(self):
        """Regression: a DUT named 'Colour ...' made the keyword 'colour' and would have blocked every real flash."""
        self.app.set_meta({"dut": {"name": "Colour cluster", "serial": "", "firmware": ""}})
        img = b"\x00" * 64 + b"---This Firmware is meant for VARCAS Vehicle ---\x00 V0.0.1 \x00" + b"\x01" * 64
        self.app.add_file("VARCAS_4G_OTA_V0.0.1.bin", img)
        info = self.app.fw_inspect("VARCAS_4G_OTA_V0.0.1.bin")
        self.assertEqual((info["keyword_used"], info["keyword_source"], info["product"]["status"]), ("VARCAS", "image", "ok"))
        self.app.set_uds_settings({"product_keyword": "other"})                # what the tester typed always wins
        info = self.app.fw_inspect("VARCAS_4G_OTA_V0.0.1.bin")
        self.assertEqual((info["keyword_used"], info["product"]["status"]), ("other", "mismatch"))

    def test_image_without_a_product_falls_back_to_dut_name_and_is_still_checked(self):
        self.app.set_meta({"dut": {"name": "Varcas cluster", "serial": "", "firmware": ""}})
        self.app.add_file("ATHER_4G_OTA_V2.0.1.bin", b"\x00" * 100 + b'AT+QMTPUBEX="x/ather" V2.0.1' + b"\x00" * 100)
        info = self.app.fw_inspect("ATHER_4G_OTA_V2.0.1.bin")
        self.assertEqual((info["keyword_source"], info["product"]["status"]), ("project", "mismatch"))

    def test_large_raw_image_gets_a_warning(self):
        self.app.add_file("VARCAS_big.bin", b"\xFF" * 2_200_000 + b"meant for VARCAS Vehicle ---")
        w = self.app.fw_inspect("VARCAS_big.bin")["warnings"]
        self.assertTrue(w and "Large image" in w[0] and "OTA" in w[0])


class TestFrameDesigner(unittest.TestCase):
    """The DUT sends a frame nobody has a DBC for (0x519): discover it, describe it, decode it."""

    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="canbench_fd_")
        self.app = App(self.ws)
        self.app.new_project("fd")
        self.dut = can.Bus(interface="virtual", channel="t_fd")
        self.app.connect("virtual", "t_fd", 500000)
        self.stop = threading.Event()
        self.val = [0x6E]

        def dut():
            while not self.stop.is_set():
                self.dut.send(can.Message(arbitration_id=0x519, is_extended_id=False,
                                          data=bytes([0, 0, 0, 0, self.val[0], 0, 0, 0])))
                time.sleep(0.05)

        threading.Thread(target=dut, daemon=True).start()

    def tearDown(self):
        self.stop.set()
        self.dut.shutdown()
        self.app.shutdown()
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_unknown_frame_discovered_then_defined_and_decoded(self):
        a = self.app
        unk = wait_for(lambda: [u for u in a.engine.unknown_snapshot() if u["id"] == 0x519], 3)
        self.assertTrue(unk, "0x519 must be listed as an unknown frame")
        self.assertEqual(unk[0]["data"][4], 0x6E)
        self.assertEqual(unk[0]["id_hex"], "519")
        self.val[0] = 0x70
        time.sleep(0.3)
        ch = a.engine.unknown_snapshot()[0]["changed"]
        self.assertIsNotNone(ch[4], "byte 4 changed recently")
        self.assertIsNone(ch[0])

        a.define_frame({"name": "DUT_0x519", "id": 0x519, "ext": False, "dlc": 8, "period_ms": 400,
                        "signals": [{"name": "Value_B4", "start": 32, "length": 8, "little": True, "unit": "x"},
                                    {"name": "Flag_B0_0", "start": 0, "length": 1}]})
        self.assertEqual(a.project["roles"]["DUT_0x519"], "listen")
        self.assertEqual(a.model.by_name["DUT_0x519"].cycle_ms, 400)
        self.assertTrue(wait_for(lambda: a.engine.rx_value("DUT_0x519", "Value_B4") == 0x70, 3))
        self.assertFalse([u for u in a.engine.unknown_snapshot() if u["id"] == 0x519], "no longer unknown")
        # it is a normal DBC now: scenarios can check it
        sc = a.save_scenario({"title": "dut value", "dwell_ms": 50, "checks": [
            {"type": "can", "signal": "DUT_0x519.Value_B4", "op": "==", "value": 0x70, "timeout_ms": 1000}]})
        a.start_tx(False) if False else None
        # the project survives a restart
        a2 = App(self.ws)
        self.assertIn("DUT_0x519", a2.model.by_name)
        a2.shutdown()

    def test_definition_validation(self):
        a = self.app
        bad = [
            {"name": "1bad", "id": 1, "dlc": 8, "signals": [{"name": "a", "start": 0, "length": 8}]},
            {"name": "ok", "id": 1, "dlc": 8, "signals": []},
            {"name": "ok", "id": 1, "dlc": 2, "signals": [{"name": "a", "start": 8, "length": 16}]},
            {"name": "ok", "id": 1, "dlc": 8, "signals": [{"name": "a", "start": 0, "length": 8}, {"name": "b", "start": 4, "length": 8}]},
        ]
        for d in bad:
            with self.assertRaises(AppError):
                a.define_frame(d)
        a.define_frame({"name": "F", "id": 0x519, "dlc": 8, "signals": [{"name": "a", "start": 0, "length": 8}]})
        a.delete_frame("F")
        self.assertNotIn("F", a.model.by_name if a.model else {})


if __name__ == "__main__":
    unittest.main()
