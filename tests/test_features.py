"""Bus log recording, manual frames, flash abort/recovery, one-instance guard, manual column mapping, About."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import can  # noqa: E402

from canbench import __main__ as mainmod  # noqa: E402
from canbench import buslog, instance, tablecan, uds  # noqa: E402
from canbench.app import App, AppError  # noqa: E402
from canbench.engine import Engine  # noqa: E402
from canbench.server import Server  # noqa: E402
from fake_ecu import FakeEcu  # noqa: E402
from test_core import needs_samples  # noqa: E402
from test_tables import SIGNAL_TABLE, write_xlsx  # noqa: E402


def wait_for(fn, timeout=5.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


class AppCase(unittest.TestCase):
    chan = "t_feat"

    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="canbench_feat_")
        self.app = App(self.ws)
        self.app.new_project("Feature bench V1.0")
        self.peer = can.Bus(interface="virtual", channel=self.chan)

    def tearDown(self):
        wait_for(lambda: not self.app.uds_run["running"], 15)
        self.peer.shutdown()
        if getattr(self, "ecu", None):
            self.ecu.stop()
        self.app.shutdown()
        shutil.rmtree(self.ws, ignore_errors=True)

    def peer_frames(self, t=0.3):
        out, end = [], time.time() + t
        while time.time() < end:
            m = self.peer.recv(0.02)
            if m is not None:
                out.append(m)
        return out


# ---------------------------------------------------------------------------------------------------------------------
class TestBusLog(AppCase):
    def test_every_frame_and_marker_is_recorded_and_readable(self):
        self.app.connect("virtual", self.chan, 500000)
        info = self.app.engine.recorder_info()
        self.assertTrue(info["active"])
        self.assertTrue(info["name"].startswith("canlog_") and info["name"].endswith(".asc"))
        self.app.manual_send({"id": "123", "data": "DE AD BE EF"})
        self.peer.send(can.Message(arbitration_id=0x456, data=b"\x01\x02\x03", is_extended_id=False))
        self.peer.send(can.Message(arbitration_id=0x18FF50E5, data=b"\xAA", is_extended_id=True))
        wait_for(lambda: self.app.engine.rx_total >= 2)
        self.app.serial.marker("MY MARKER")
        time.sleep(0.2)
        name = info["name"]
        self.app.disconnect()                      # closes the file
        path = buslog.safe_path(self.app._logs_dir(), name)
        self.assertTrue(path)
        frames = [m for m in can.ASCReader(path) if not m.is_error_frame]
        by_id = {m.arbitration_id: m for m in frames}
        self.assertEqual(bytes(by_id[0x123].data), b"\xDE\xAD\xBE\xEF")
        self.assertFalse(by_id[0x123].is_rx, "our own frame is marked Tx")
        self.assertTrue(by_id[0x456].is_rx)
        self.assertTrue(by_id[0x18FF50E5].is_extended_id)
        self.assertIn("MY MARKER", open(path, encoding="utf-8").read())

    def test_result_names_the_log_and_the_report_hashes_it(self):
        self.app.connect("virtual", self.chan, 500000)
        name = self.app.engine.recorder_info()["name"]
        r = self.app.record_manual({"verdict": "PASS", "title": "Something"})
        self.assertEqual(r["buslog"], name)
        self.app.disconnect()
        rep = self.app.report_bytes().decode()
        self.assertIn(name, rep)
        self.assertIn("CAN bus logs (evidence)", rep)
        self.assertIn("ConnectM", rep)                                  # logo + company in the header
        self.assertIn("data:image/png;base64,", rep)
        self.assertIn(name, self.app.csv_bytes().decode("utf-8-sig"))

    def test_disabled_means_no_file_and_toggle_needs_disconnect(self):
        self.app.set_buslog(False)
        self.app.connect("virtual", self.chan, 500000)
        self.assertFalse(self.app.engine.recorder_info()["active"])
        with self.assertRaises(AppError):
            self.app.set_buslog(True)
        self.app.disconnect()
        self.assertEqual(self.app.buslog_state()["files"], [])

    def test_no_path_tricks_and_old_logs_can_be_cleared(self):
        self.app.connect("virtual", self.chan, 500000)
        for bad in ("../project.json", "..\\project.json", "canlog_x/../../project.json", "canlog_.asc.exe", ""):
            with self.assertRaises(AppError):
                self.app.buslog_path(bad)
        self.app.disconnect()
        self.app.connect("virtual", self.chan, 500000)          # a second recording
        self.assertGreaterEqual(len(self.app.buslog_state()["files"]), 1)
        removed = self.app.clear_buslogs()
        left = self.app.buslog_state()["files"]
        self.assertEqual([f["name"] for f in left], [self.app.engine.recorder_info()["name"]], "the active recording is kept")
        self.assertGreaterEqual(removed, 0)

    def test_writer_never_blocks_the_bus_and_rolls_over(self):
        d = tempfile.mkdtemp()
        try:
            rec = buslog.BusRecorder(d, "x")
            old = buslog.MAX_FILE_BYTES
            buslog.MAX_FILE_BYTES = 20_000
            try:
                for i in range(2000):
                    m = can.Message(arbitration_id=0x100 + (i % 8), data=bytes(8), is_extended_id=False)
                    m.timestamp = time.time()
                    rec.add("RX", m)
                time.sleep(0.5)
                rec.stop()
            finally:
                buslog.MAX_FILE_BYTES = old
            files = buslog.list_logs(d)
            self.assertGreater(len(files), 1, "a long session continues in a new part file")
            n = sum(1 for f in files for _ in can.ASCReader(os.path.join(d, f["name"])))
            self.assertEqual(n + rec.dropped, 2000)
        finally:
            shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------------------------------------------------
class TestManualFrames(AppCase):
    def test_send_once_any_id_and_validation(self):
        self.app.connect("virtual", self.chan, 500000)
        self.peer_frames(0.1)
        self.assertTrue(self.app.manual_send({"id": "7DF", "data": "02 01 0C"})["once"])
        self.assertTrue(self.app.manual_send({"id": "0x18FEF100", "data": "00"})["once"])    # > 0x7FF -> 29-bit automatically
        got = {m.arbitration_id: m for m in self.peer_frames()}
        self.assertEqual(bytes(got[0x7DF].data), b"\x02\x01\x0C")
        self.assertTrue(got[0x18FEF100].is_extended_id)
        for bad in ({"id": "zz"}, {"id": "800", "ext": False}, {"id": "1", "data": "00 11 22 33 44 55 66 77 88"},
                    {"id": "1", "data": "xyz"}, {"id": "1", "period_ms": 1}, {"id": "1", "period_ms": 10, "duration_s": -1}):
            with self.assertRaises(AppError, msg=str(bad)):
                self.app.manual_send(bad)

    def test_repeating_stops_after_duration_and_count_without_start_tx(self):
        self.app.connect("virtual", self.chan, 500000)
        self.assertFalse(self.app.engine.tx_on)                 # independent of the simulated-frame switch
        self.app.manual_send({"id": "200", "data": "01", "period_ms": 20, "count": 5})
        wait_for(lambda: not self.app.engine.manual_active(), 3)
        frames = [m for m in self.peer_frames(0.2) if m.arbitration_id == 0x200]
        self.assertEqual(len(frames), 5)
        snap = self.app.engine.manual_snapshot()
        self.assertEqual((snap[0]["sent"], snap[0]["running"], snap[0]["why"]), (5, False, "finished"))
        self.app.manual_clear_finished()
        self.assertEqual(self.app.engine.manual_snapshot(), [])
        self.app.manual_send({"id": "201", "data": "02", "period_ms": 20, "duration_s": 0.3})
        wait_for(lambda: not self.app.engine.manual_active(), 3)
        n = sum(1 for m in self.peer_frames(0.2) if m.arbitration_id == 0x201)
        self.assertTrue(8 <= n <= 18, n)

    def test_stop_estop_and_disconnect_end_repeating_frames(self):
        self.app.connect("virtual", self.chan, 500000)
        self.app.manual_send({"id": "300", "data": "01", "period_ms": 20})
        self.app.manual_send({"id": "301", "data": "01", "period_ms": 20})
        self.assertEqual(len(self.app.engine.manual_snapshot()), 2)
        self.app.manual_stop("s300")
        self.assertEqual([m["key"] for m in self.app.engine.manual_snapshot()], ["s301"])
        self.app.estop()
        self.assertFalse(self.app.engine.manual_active())
        self.app.manual_send({"id": "302", "data": "01", "period_ms": 20})
        self.app.disconnect()
        self.assertEqual(self.app.engine.manual_snapshot(), [])

    def test_same_id_replaces_and_presets_persist(self):
        self.app.connect("virtual", self.chan, 500000)
        self.app.manual_send({"id": "400", "data": "01", "period_ms": 50})
        self.app.manual_send({"id": "400", "data": "02", "period_ms": 50})
        self.assertEqual(len(self.app.engine.manual_snapshot()), 1)
        self.app.manual_stop()
        self.app.manual_save({"name": "Wake", "id": "7DF", "data": "02 01 0C", "period_ms": 100, "duration_s": 5})
        pid = self.app.project["id"]
        app2 = App(self.ws)
        try:
            app2.open_project(pid)
            self.assertEqual(app2.state()["manual_frames"][0]["data"], "02 01 0C")
            app2.manual_delete("Wake")
            self.assertEqual(app2.state()["manual_frames"], [])
        finally:
            app2.shutdown()

    def test_blocked_while_flashing(self):
        self.app.connect("virtual", self.chan, 500000)
        self.app.uds_run["running"] = True
        with self.assertRaises(AppError):
            self.app.manual_send({"id": "1", "data": "00"})
        self.app.uds_run["running"] = False
        self.app.manual_send({"id": "500", "data": "01", "period_ms": 50})
        self.app.set_uds_settings({"address": "0x1000"})
        self.app.add_file("VARCAS_x.bin", b"\x00" * 64 + b"---This Firmware is meant for VARCAS Vehicle ---" + b"\x00" * 64)
        with self.assertRaises(AppError) as c:
            self.app.start_flash("VARCAS_x.bin", "FLASH")
        self.assertIn("by hand", str(c.exception))


# ---------------------------------------------------------------------------------------------------------------------
class TestFlashAbortAndRecovery(AppCase):
    chan = "t_rec"

    def setUp(self):
        super().setUp()
        self.image = b"\x00" * 100 + b"---This Firmware is meant for VARCAS Vehicle ---" + os.urandom(6000)
        self.app.add_file("VARCAS_test.bin", self.image)
        self.app.set_meta({"dut": {"name": "Varcas cluster", "serial": "", "firmware": ""}})
        self.app.set_uds_settings({"address": "0x00010000", "reboot_min_s": 1, "reboot_timeout_s": 10})
        self.app.connect("virtual", self.chan, 500000)

    def test_abort_mid_transfer_leaves_a_recovery_note_that_survives_restart(self):
        self.ecu = FakeEcu(self.chan, maxlen=16)             # many small blocks: time to abort in the middle
        self.app.start_flash("VARCAS_test.bin", "FLASH")
        self.assertTrue(wait_for(lambda: len(self.ecu.received) > 600, 10))
        self.app.abort_flash()
        self.assertTrue(wait_for(lambda: not self.app.uds_run["running"], 10))
        r = self.app.results.items[-1]
        self.assertEqual(r["verdict"], "FAIL")
        self.assertIn("Aborted by operator", r["note"])
        self.assertIn("re-flashed", r["note"])
        self.assertLess(len(self.ecu.received), len(self.image), "stopped before the end")
        rec = self.app.state()["uds"]["recovery"]
        self.assertEqual((rec["level"], rec["image"], rec["aborted"]), ("reflash", "VARCAS_test.bin", True))
        app2 = App(self.ws)                                   # a new process / next person opens the project
        try:
            app2.open_project(self.app.project["id"])
            self.assertEqual(app2.state()["uds"]["recovery"]["level"], "reflash")
        finally:
            app2.shutdown()

    def test_recovery_reflash_works_when_the_ecu_reports_no_identity_and_clears_the_note(self):
        self.ecu = FakeEcu(self.chan, maxlen=16)
        self.app.start_flash("VARCAS_test.bin", "FLASH")
        wait_for(lambda: len(self.ecu.received) > 600, 10)
        self.app.abort_flash()
        wait_for(lambda: not self.app.uds_run["running"], 10)
        self.ecu.did = {}                                     # bootloader: no application identity any more
        # a normal update refuses (and says so) ...
        self.app.start_flash("VARCAS_test.bin", "FLASH")
        wait_for(lambda: not self.app.uds_run["running"], 10)
        self.assertIn("did not answer any identity", self.app.results.items[-1]["note"])
        self.assertEqual(self.app.state()["uds"]["recovery"]["level"], "reflash", "a failure before any change keeps the old note")
        # ... recovery mode flashes it and the version check is satisfied by the identity that appears afterwards
        self.app.start_flash("VARCAS_test.bin", "FLASH", recovery=True)
        self.assertTrue(wait_for(lambda: not self.app.uds_run["running"], 60))
        r = self.app.results.items[-1]
        self.assertEqual(r["verdict"], "PASS", r["note"])
        self.assertIn("RECOVERY", r["stimulus"])
        self.assertEqual(bytes(self.ecu.received), self.image)
        self.assertIsNone(self.app.state()["uds"]["recovery"])

    def test_recovery_levels(self):
        lv = uds.recovery_level
        self.assertEqual(lv({"ok": True, "failed_at": -1}), "")
        self.assertEqual(lv({"ok": False, "failed_at": 1}), "")           # session / security: nothing erased
        self.assertEqual(lv({"ok": False, "failed_at": 2}), "")
        self.assertEqual(lv({"ok": False, "failed_at": 3}), "reflash")    # erase started
        self.assertEqual(lv({"ok": False, "failed_at": 5}), "reflash")    # transfer
        self.assertEqual(lv({"ok": False, "failed_at": 7}), "reflash")    # checksum not accepted
        self.assertEqual(lv({"ok": False, "failed_at": 9}), "verify")     # written, ECU did not come back
        self.assertEqual(lv({"ok": False, "skipped": True, "failed_at": 0}), "")

    def test_nothing_that_would_cut_a_flash_is_allowed_while_it_runs(self):
        self.ecu = FakeEcu(self.chan, maxlen=16)
        self.app.start_flash("VARCAS_test.bin", "FLASH")
        wait_for(lambda: len(self.ecu.received) > 100, 10)
        for call in (self.app.disconnect, lambda: self.app.connect("virtual", self.chan, 500000),
                     lambda: self.app.open_project(self.app.project["id"]),
                     lambda: self.app.delete_project(self.app.project["id"])):
            with self.assertRaises(AppError):
                call()
        toasts = []
        self.app.emit = lambda kind, data: toasts.append((kind, data))
        self.app.estop()                                      # the red button does not cut a flash ...
        self.assertTrue(self.app.uds_run["running"])
        self.assertTrue(any("still running" in d["text"] for k, d in toasts if k == "toast"))
        self.assertTrue(wait_for(lambda: not self.app.uds_run["running"], 60))      # ... which therefore finishes
        self.assertEqual(self.app.results.items[-1]["verdict"], "PASS")


# ---------------------------------------------------------------------------------------------------------------------
@needs_samples
class TestKeepTxDuringUpdate(AppCase):
    chan = "t_keeptx"

    def setUp(self):
        super().setUp()
        from test_core import BAT
        with open(BAT, "rb") as f:
            self.app.add_file("battery.dbc", f.read())
        self.app.add_file("VARCAS_test.bin", b"\x00" * 100 + b"---This Firmware is meant for VARCAS Vehicle ---" + os.urandom(3000))
        self.app.set_meta({"dut": {"name": "Varcas cluster", "serial": "", "firmware": ""}})
        self.app.set_uds_settings({"address": "0x00010000", "reboot_min_s": 1, "reboot_timeout_s": 10})
        self.app.connect("virtual", self.chan, 500000)
        self.ecu = FakeEcu(self.chan)

    def test_default_is_a_quiet_bus(self):
        self.assertFalse(self.app.uds_settings()["keep_tx"])
        self.app.start_tx(True)
        with self.assertRaises(AppError) as c:
            self.app.start_flash("VARCAS_test.bin", "FLASH")
        self.assertIn("Keep simulated frames running", str(c.exception))     # tells the user where the option is
        self.app.start_tx(False)

    def test_start_tx_in_the_middle_of_an_update_is_refused_unless_chosen(self):
        self.app.uds_run["running"] = True
        with self.assertRaises(AppError):
            self.app.start_tx(True)
        self.app.set_uds_settings({"keep_tx": True})
        self.app.start_tx(True)
        self.app.start_tx(False)
        self.app.uds_run["running"] = False

    def test_update_succeeds_with_frames_running_and_the_report_says_so(self):
        self.app.set_uds_settings({"keep_tx": True})
        self.app.start_tx(True)
        before = self.app.engine.tx_total
        self.app.start_flash("VARCAS_test.bin", "FLASH")
        self.assertTrue(wait_for(lambda: not self.app.uds_run["running"], 90))
        r = self.app.results.items[-1]
        self.assertEqual(r["verdict"], "PASS", r["note"])
        self.assertGreater(self.app.engine.tx_total - before, 20, "simulated frames kept going out during the update")
        self.assertIn("simulated frames were running", r["note"])
        self.assertIn("kept running", r["stimulus"])
        self.assertTrue(self.app.engine.tx_on, "and it is still transmitting afterwards")

    def test_a_frame_using_the_update_ids_is_refused_even_when_kept(self):
        st = next(iter(self.app.engine.tx.values()))
        other = 0x7E8 if st.msg.frame_id != 0x7E8 else 0x7E9
        self.app.set_uds_settings({"keep_tx": True, "ext": bool(st.msg.ext), "tx_id": f"0x{st.msg.frame_id:X}", "rx_id": f"0x{other:X}"})
        self.app.start_tx(True)
        with self.assertRaises(AppError) as c:
            self.app.start_flash("VARCAS_test.bin", "FLASH")
        self.assertIn("same CAN IDs", str(c.exception))
        self.assertFalse(self.app.uds_run["running"])
        self.app.start_tx(False)

    def test_manual_frame_on_the_update_ids_is_refused_too(self):
        self.app.set_uds_settings({"keep_tx": True})
        self.app.manual_send({"id": "7E0", "data": "00", "period_ms": 100})
        with self.assertRaises(AppError) as c:
            self.app.start_flash("VARCAS_test.bin", "FLASH")
        self.assertIn("same CAN IDs", str(c.exception))
        self.app.manual_stop_all() if hasattr(self.app, "manual_stop_all") else self.app.engine.stop_manual()


# ---------------------------------------------------------------------------------------------------------------------
class TestFormatMismatchAndClock(AppCase):
    chan = "t_fmt"

    def ecu_that_ignores_the_ide_bit(self):
        """The real ECU answers a 29-bit request on 0x7E0 as if it were 11-bit (its filter ignores the IDE bit): the trace of the
        user's bench shows requests going out EXT and replies coming back STD. Relay: re-send EXT 0x7E0 as STD for the fake ECU."""
        self.ecu = FakeEcu(self.chan)
        stop = threading.Event()

        def relay():
            while not stop.is_set():
                m = self.peer.recv(0.05)
                if m is not None and m.arbitration_id in (0x7E0, 0x7E1) and m.is_extended_id:
                    self.peer.send(can.Message(arbitration_id=m.arbitration_id, data=m.data, is_extended_id=False))
        t = threading.Thread(target=relay, daemon=True)
        t.start()
        self.addCleanup(lambda: (stop.set(), t.join(1)))

    def test_ecu_answering_in_the_other_id_length_is_explained_not_ignored(self):
        """Real case: 'UDS not working' - 29-bit IDs was ticked, the ECU answered with 11-bit IDs and every reply was dropped."""
        self.ecu_that_ignores_the_ide_bit()                     # answers on standard 0x7E8 whatever the request's ID length
        self.app.connect("virtual", self.chan, 500000)
        self.app.set_uds_settings({"ext": True, "p2_s": 0.4})
        with self.assertRaises(AppError) as c:
            self.app.uds_identity()
        msg = str(c.exception)
        self.assertIn("did answer", msg)
        self.assertIn("11-bit", msg)
        self.assertIn("Untick '29-bit IDs'", msg)
        self.app.set_uds_settings({"ext": False, "p2_s": 2.0})   # with the right setting the same ECU is read normally
        self.assertEqual(self.app.uds_identity()["answered"], 3)

    def test_a_silent_ecu_still_gets_the_plain_message(self):
        self.app.connect("virtual", self.chan, 500000)
        self.app.set_uds_settings({"p2_s": 0.3})
        res = self.app.uds_identity()
        self.assertEqual(res["answered"], 0)
        self.assertNotIn("BUT", " ".join(i["error"] for i in res["items"]))

    def test_flash_names_the_mismatch_too(self):
        self.ecu_that_ignores_the_ide_bit()
        self.app.add_file("VARCAS_x.bin", b"\x00" * 64 + b"---This Firmware is meant for VARCAS Vehicle ---" + b"\x00" * 64)
        self.app.set_uds_settings({"address": "0x1000", "ext": True, "p2_s": 0.4})
        self.app.connect("virtual", self.chan, 500000)
        self.app.start_flash("VARCAS_x.bin", "FLASH")
        wait_for(lambda: not self.app.uds_run["running"], 20)
        note = self.app.results.items[-1]["note"]
        self.assertIn("did answer", note + self.app.uds_run["summary"])

    def test_received_frames_use_the_same_clock_as_sent_frames(self):
        """PCAN stamps frames from the adapter start (a few hundred seconds), sent frames carry the wall clock: one clock, or the
        trace and the bus log come out of order."""
        class Stub:
            state = None
            def __init__(self):
                self.sent = []
                self.once = True
            def recv(self, t):
                if self.once:
                    self.once = False
                    return can.Message(arbitration_id=0x123, data=b"\x01", is_extended_id=False, timestamp=586.3)
                time.sleep(0.02)
                return None
            def send(self, m, timeout=None):
                self.sent.append(m)
            def shutdown(self):
                pass
        e = Engine()
        try:
            e.bus = Stub()
            self.assertTrue(wait_for(lambda: len(e.trace) >= 1))
            self.assertGreater(e.trace[0][1], 1e9, "an adapter-relative timestamp must be replaced by the wall clock")
            e.send_frame(0x200, b"\x02")
            self.assertTrue(abs(e.trace[-1][1] - e.trace[0][1]) < 5)
        finally:
            e.bus = None
            e.shutdown()


# ---------------------------------------------------------------------------------------------------------------------
@needs_samples
class TestSendPeriods(AppCase):
    chan = "t_per"

    def setUp(self):
        super().setUp()
        from test_core import BAT
        with open(BAT, "rb") as f:
            self.app.add_file("battery.dbc", f.read())

    def periods(self):
        return {m["name"]: m["tx_period_ms"] for m in self.app.state()["model"]["messages"] if m["role"] == "sim"}

    def test_general_period_then_back_to_the_database_values(self):
        db = self.periods()
        self.assertGreater(len(set(db.values())), 1, "the sample database has different periods per message")
        n = self.app.set_all_periods(50)
        self.assertGreater(n, 10)
        self.assertEqual(set(self.periods().values()), {50})
        self.assertEqual({round(st.period * 1000) for st in self.app.engine.tx.values()}, {50}, "the running scheduler uses it")
        self.app.set_all_periods(None)
        self.assertEqual(self.periods(), db)
        self.assertEqual({n: round(st.period * 1000) for n, st in self.app.engine.tx.items()},
                         {n: db[n] for n in self.app.engine.tx})

    def test_a_single_message_can_still_differ_and_survives_restart(self):
        self.app.set_all_periods(200)
        name = next(iter(self.periods()))
        self.app.set_period(name, 30)
        p = self.periods()
        self.assertEqual((p[name], {v for k, v in p.items() if k != name}), (30, {200}))
        app2 = App(self.ws)
        try:
            app2.open_project(self.app.project["id"])
            self.assertEqual(app2.project["periods"][name], 30)
        finally:
            app2.shutdown()

    def test_limits_and_garbage_are_refused(self):
        self.assertEqual(set(self.app.state() and [self.app.set_all_periods(1) and 0]), {0})   # too small is clamped to 5 ms, not refused
        self.assertEqual(set(self.periods().values()), {5})
        self.app.set_all_periods(10 ** 9)
        self.assertEqual(set(self.periods().values()), {60000})
        with self.assertRaises(AppError):
            self.app.set_all_periods("fast")

    def test_frames_really_go_out_at_the_general_period(self):
        self.app.connect("virtual", self.chan, 250000)
        for n in list(self.app.project["roles"]):
            self.app.set_role(n, "ignore")
        name = next(iter(self.periods().keys() or ["x"]), None)
        first = [m for m in self.app.state()["model"]["messages"] if not m["placeholder"]][0]
        self.app.set_role(first["name"], "sim")
        self.app.set_all_periods(40)
        self.peer_frames(0.05)
        self.app.start_tx(True)
        got = [m for m in self.peer_frames(1.0) if m.arbitration_id == first["id"]]
        self.app.start_tx(False)
        self.assertTrue(18 <= len(got) <= 30, len(got))


class TestUiTextAndLayoutRules(unittest.TestCase):
    """Rules the user asked for that are about wording / defaults and can be checked in the sources the browser receives."""

    @classmethod
    def setUpClass(cls):
        web = os.path.join(os.path.dirname(HERE), "canbench", "web")
        parts = []
        for n in sorted(os.listdir(web)):
            if n.endswith(".js"):
                with open(os.path.join(web, n), encoding="utf-8") as f:
                    parts.append(f.read())
        cls.js = chr(10).join(parts)

    def test_results_sits_just_above_info_in_the_menu(self):
        import re
        nav = re.search(r"const NAV = \[(.*?)\];", self.js, re.S).group(1)
        ids = re.findall(r'\["(\w+)",', nav)
        self.assertEqual(ids[-2:], ["results", "info"])

    def test_general_period_box_shows_the_period_really_in_use(self):
        # it must not fall back to a fixed 100 when the messages were set to something else
        self.assertIn("cur.length === 1 ? cur[0]", self.js)
        self.assertNotIn('id: "per-all-fixed"', self.js)

    def test_firmware_page_does_not_talk_about_a_script(self):
        low = self.js.lower()
        for word in ("uds_client", "original script", "your script", "the script"):
            self.assertNotIn(word, low)
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "big.bin")
            with open(p, "wb") as f:
                f.write(b"\x00" * 3_000_000)
            warn = " ".join(uds.inspect_firmware(p, "x")["warnings"]).lower()
            self.assertIn("large image", warn)
            self.assertNotIn("uds_client", warn)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_simulate_starts_folded_and_unknown_frames_can_be_minimised_or_hidden(self):
        self.assertIn("setAllOpen", self.js)                       # Expand all / Collapse all
        self.assertIn('pref.get("simOpen:"', self.js)               # open state remembered per project, default = folded
        self.assertNotIn('pref.get("collapsed"', self.js)           # the old default (everything open) is gone
        self.assertIn('pref.get("unkOpen"', self.js)                # unknown-frames card minimise state
        self.assertIn("unkHidden:", self.js)                        # per-frame hide


# ---------------------------------------------------------------------------------------------------------------------
class TestOneInstance(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="canbench_inst_")
        self.lock = os.path.join(self.d, "lock.json")
        self.child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.srv = Server(os.path.join(self.d, "ws"), 18911)
        self.srv.start()

    def tearDown(self):
        self.srv.stop()
        self.child.kill()
        self.child.wait()
        shutil.rmtree(self.d, ignore_errors=True)

    def write(self, **kw):
        with open(self.lock, "w") as f:
            json.dump({"started": time.time(), **kw} if "started" not in kw else kw, f)

    def test_a_live_canbench_is_found(self):
        self.write(pid=self.child.pid, port=self.srv.port)
        f = instance.find_running(self.lock)
        self.assertEqual((f["port"], f["url"]), (self.srv.port, f"http://127.0.0.1:{self.srv.port}/"))

    def test_stale_and_foreign_locks_are_ignored(self):
        self.assertIsNone(instance.find_running(self.lock), "no file")
        self.write(pid=self.child.pid, port=self.srv.port + 1)          # nothing answers there
        self.assertIsNone(instance.find_running(self.lock))
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.write(pid=dead.pid, port=self.srv.port)                    # the process is gone (crash left the file)
        self.assertIsNone(instance.find_running(self.lock))
        self.write(pid=os.getpid(), port=self.srv.port)                 # our own lock is not "another" instance
        self.assertIsNone(instance.find_running(self.lock))

        class Other(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"app": "SomethingElse"}')

            def log_message(self, *a):
                pass
        other = HTTPServer(("127.0.0.1", 0), Other)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        try:
            self.write(pid=self.child.pid, port=other.server_address[1])                  # live process, live port, but not CANBench
            self.assertIsNone(instance.find_running(self.lock))
        finally:
            other.shutdown()

    def test_starting_instance_blocks_a_twin_briefly(self):
        self.write(pid=self.child.pid, port=0)
        self.assertEqual(instance.find_running(self.lock)["port"], 0)
        self.write(pid=self.child.pid, port=0, started=time.time() - 60)
        self.assertIsNone(instance.find_running(self.lock), "a start-up that never finished does not block forever")

    def test_claim_and_release_only_touch_our_own_file(self):
        instance.claim(1234, self.lock)
        with open(self.lock) as f:
            self.assertEqual(json.load(f)["pid"], os.getpid())
        instance.release(self.lock)
        self.assertFalse(os.path.exists(self.lock))
        self.write(pid=self.child.pid, port=1)
        instance.release(self.lock)
        self.assertTrue(os.path.exists(self.lock), "someone else's lock stays")

    def test_second_launch_opens_the_first_window_instead_of_starting(self):
        opened, real = [], mainmod.open_window
        mainmod.open_window = opened.append
        orig_find, orig_server = instance.find_running, mainmod.Server
        instance.find_running = lambda path=None: {"pid": 1, "port": 8765, "url": "http://127.0.0.1:8765/", "version": "1"}
        mainmod.Server = lambda *a, **k: self.fail("a second bench must not be started")
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = mainmod.main(["--workspace", self.d])
            self.assertEqual(rc, 0)
            self.assertEqual(opened, ["http://127.0.0.1:8765/"])
            self.assertIn("already running", buf.getvalue())
        finally:
            mainmod.open_window = real
            instance.find_running, mainmod.Server = orig_find, orig_server

    def test_version_flag(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(mainmod.main(["--version"]), 0)
        self.assertIn("CANBench 1.", buf.getvalue())


# ---------------------------------------------------------------------------------------------------------------------
ODD_SHEET = [      # headers no synonym list would ever know; the title row and a notes block sit above
    ["Bus matrix, Rev C (internal)", "", "", ""],
    ["", "", "", ""],
    ["Nr", "Kennung", "Botschaft", "Zyklus ms"],
    ["1", "0x100", "Drive_Cmd", "20"],
    ["2", "0x200", "Drive_State", "50"],
]
ODD_SIGNALS = [
    ["Kennung", "Botschaft", "Signal", "Bit", "Breite", "Skalierung"],
    ["0x100", "Drive_Cmd", "Torque", "0", "12", "0.5"],
    ["", "", "Enable", "12", "1", "1"],
]


class TestManualColumnMapping(unittest.TestCase):
    def test_odd_headers_are_not_recognised_automatically_but_can_be_mapped(self):
        self.assertIsNone(tablecan.parse_table(ODD_SHEET))
        r = tablecan.parse_table(ODD_SHEET, mapping={"header_row": 3, "columns": {"id": 1, "msg": 2, "cycle": 3}})
        self.assertEqual([(f["name"], f["id"], f["period_ms"]) for f in r["frames"]], [("Drive_Cmd", 0x100, 20), ("Drive_State", 0x200, 50)])

    def test_signal_columns_by_hand(self):
        r = tablecan.parse_table(ODD_SIGNALS, mapping={"header_row": 1, "sender": "VCU",
                                                       "columns": {"id": 0, "msg": 1, "sig": 2, "start": 3, "length": 4, "factor": 5}})
        f = r["frames"][0]
        self.assertEqual((f["name"], f["sender"]), ("Drive_Cmd", "VCU"))
        sig = {s["name"]: s for s in f["signals"]}
        self.assertEqual((sig["Torque"]["start"], sig["Torque"]["length"], sig["Torque"]["scale"]), (0, 12, 0.5))
        self.assertEqual((sig["Enable"]["start"], sig["Enable"]["length"]), (12, 1))

    def test_a_wrong_mapping_says_what_is_missing(self):
        for m, word in (({"header_row": 3, "columns": {"msg": 2}}, "CAN ID"),
                        ({"header_row": 3, "columns": {"id": 1}}, "message-name"),
                        ({"header_row": 99, "columns": {"id": 1, "msg": 2}}, "outside"),
                        ({"header_row": 3, "columns": {"id": 9, "msg": 2}}, "outside"),
                        ({"header_row": 3, "columns": {"id": 1, "bogus": 2}}, "Unknown column"),
                        ({"columns": {"id": 1, "msg": 2}}, "header row")):
            with self.assertRaises(ValueError, msg=str(m)) as c:
                tablecan.parse_table(ODD_SHEET, mapping=m)
            self.assertIn(word, str(c.exception))

    def test_automatic_reading_is_unchanged_and_grid_proposes_the_columns(self):
        auto = tablecan.parse_table(SIGNAL_TABLE)
        self.assertEqual(len(auto["frames"]), 3)
        g = tablecan.grid({"blocks": [{"type": "table", "rows": SIGNAL_TABLE}]})[0]
        self.assertEqual(g["auto_header"], 2)
        self.assertEqual((g["auto_columns"]["id"], g["auto_columns"]["sig"]), (0, 5))
        self.assertIsNone(tablecan.grid({"blocks": [{"type": "table", "rows": ODD_SHEET}]})[0]["auto_header"])


class TestMappingThroughTheApp(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="canbench_map_")
        self.app = App(self.ws)
        self.app.new_project("Odd sheet")
        x = os.path.join(self.ws, "odd.xlsx")
        write_xlsx(x, [ODD_SHEET])
        with open(x, "rb") as f:
            self.app.add_file("odd.xlsx", f.read())

    def tearDown(self):
        self.app.shutdown()
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_grid_preview_import_and_record(self):
        self.assertNotIn("odd.xlsx", self.app.state()["tables"], "not detected automatically")
        with self.assertRaises(AppError) as c:
            self.app.import_table("odd.xlsx")
        self.assertIn("Map columns", str(c.exception))
        g = self.app.table_grid("odd.xlsx")
        rows = g["tables"][0]["rows"]
        hdr = next(i for i, r in enumerate(rows, 1) if len(r) > 1 and r[1] == "Kennung")      # blank rows are not part of the grid
        self.assertEqual(hdr, 2)
        self.assertIn("id", [f["key"] for f in g["fields"]])
        mp = {"table": 0, "header_row": hdr, "columns": {"id": 1, "msg": 2, "cycle": 3}}
        pv = self.app.preview_table("odd.xlsx", "auto", "auto", mp)
        self.assertEqual(len(pv["frames"]), 2)
        res = self.app.import_table("odd.xlsx", "auto", "auto", mp)
        self.assertEqual(res["frames"], 2)
        entry = [f for f in self.app.project["files"] if f["name"].endswith("_from_table.dbc")][0]
        self.assertEqual(entry["mapping"]["columns"]["id"], 1)
        self.assertEqual({m["name"] for m in self.app.state()["model"]["messages"]}, {"Drive_Cmd", "Drive_State"})
        with self.assertRaises(AppError):
            self.app.table_grid("nope.xlsx")
        with self.assertRaises(AppError):
            self.app.import_table("odd.xlsx", "auto", "auto", {"table": 5, "header_row": hdr, "columns": {"id": 1, "msg": 2}})


# ---------------------------------------------------------------------------------------------------------------------
class TestAboutAndBranding(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="canbench_about_")
        self.srv = Server(self.ws, 18921)
        self.srv.start()

    def tearDown(self):
        self.srv.stop()
        shutil.rmtree(self.ws, ignore_errors=True)

    def get(self, path, tok=True):
        u = f"http://127.0.0.1:{self.srv.port}{path}"
        req = urllib.request.Request(u, headers={"X-Token": self.srv.token} if tok else {})
        return urllib.request.urlopen(req, timeout=5)

    def test_about_ping_and_logo(self):
        a = json.loads(self.get("/api/about").read())
        self.assertEqual((a["name"], a["company"]), ("CANBench", "ConnectM"))
        self.assertRegex(a["version"], r"^\d+\.\d+\.\d+$")
        self.assertIn("python-can", a["libs"])
        p = json.loads(self.get("/api/ping", tok=False).read())          # no token: a second launch must be able to ask
        self.assertEqual((p["app"], p["port"]), ("CANBench", self.srv.port))
        self.assertTrue(self.get("/static/connectm_logo.png", tok=False).read().startswith(b"\x89PNG"))
        self.assertTrue(self.get("/static/connectm_mark.png", tok=False).read().startswith(b"\x89PNG"))
        with self.assertRaises(urllib.error.HTTPError) as c:
            self.get("/api/about", tok=False)
        self.assertEqual(c.exception.code, 401)

    def test_paths_open_folder_and_info_page(self):
        from unittest import mock
        self.srv.app.new_project("Info test")
        a = json.loads(self.get("/api/about").read())
        p = a["paths"]
        self.assertTrue(p["logs"].endswith("logs") and p["project"] in p["logs"] and p["workspace"] in p["project"])
        self.assertTrue(p["project_file"].endswith("project.json"))
        opened = []
        with mock.patch.object(os, "startfile", opened.append, create=True):
            self.assertEqual(self.srv.app.open_folder("logs"), p["logs"])
        self.assertEqual(opened, [p["logs"]] if sys.platform == "win32" else [])
        self.assertTrue(os.path.isdir(p["logs"]), "the folder is created so 'Open folder' always works")
        for bad in ("..", "C:/Windows", "logs/../..", "", "project_file"):
            with self.assertRaises(AppError):
                self.srv.app.open_folder(bad)
        js = self.get("/static/app.js", tok=False).read().decode()
        self.assertIn("CB.views.info", js)
        self.assertNotIn("confirm(", js.replace("function ask", ""), "no browser confirm boxes are left")

    def test_state_carries_the_version_for_the_header(self):
        self.assertEqual(self.srv.app.state()["version"], json.loads(self.get("/api/about").read())["version"])


if __name__ == "__main__":
    unittest.main()
