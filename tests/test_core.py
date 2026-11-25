"""Core tests: python -m unittest discover -s tests -v   (run from the CANBench folder)"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import can  # noqa: E402

from canbench import docs as docmod  # noqa: E402
from canbench.app import App, AppError  # noqa: E402
from canbench.dbcmodel import Model  # noqa: E402
from canbench.engine import Engine  # noqa: E402
from canbench.server import Server  # noqa: E402

S = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")
BAT, MOT = os.path.join(S, "dbc for battery.dbc"), os.path.join(S, "DBC for motor.dbc")
DOCS = ["SOR -Cluster (1) (3).docx", "SOW_POC_Varcas_DCU_5.5.docx", "VARCAS_CLUSTER_BMS_MCU_Alert_Codes_DOC.pdf"]
# The customer's own DBCs and requirement documents are not part of the repository (private material). Tests that need them
# are skipped on a clone without a samples/ folder; put the files there (see README) to run them.
HAVE_SAMPLES = all(os.path.exists(p) for p in [BAT, MOT] + [os.path.join(S, f) for f in DOCS])
needs_samples = unittest.skipUnless(HAVE_SAMPLES, "customer sample files are not in samples/ (private, not in the repository)")


def wait_for(fn, timeout=3.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


@needs_samples
class TestModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = Model([BAT, MOT])

    def test_loads_everything(self):
        s = self.m.summary()
        self.assertEqual(s["messages"], 31)
        self.assertEqual(s["extended"], 3)
        self.assertEqual(s["composites"], 10)

    def test_lint_reports_known_defects(self):
        codes = {f["code"] for f in self.m.findings}
        for c in ("SIGNED_1BIT", "PLACEHOLDER", "ENUM_RANGE", "RANGE_BITS", "SPLIT_PAIR", "NO_SENDER"):
            self.assertIn(c, codes)

    def test_every_message_roundtrips(self):
        """Encode -> decode must give back what we set, for every signal of every message."""
        for m in self.m.messages:
            raw = m.default_raw()
            for s in m.signals:
                if s.hidden_by:
                    continue
                lo, hi = s.raw_range
                raw[s.name] = hi if s.length < 16 else (lo + hi) // 3
            data = m.encode(raw)
            back = m.decode_raw(data)
            for k, v in raw.items():
                self.assertEqual(back[k], v, f"{m.name}.{k}")

    def test_signed_one_bit_sets_and_reads_back(self):
        m = self.m.by_name["Controller_BMS"]
        s = m.by_name["Charging_status"]
        raw = m.default_raw()
        raw["Charging_status"] = s.to_raw(1)
        self.assertEqual(raw["Charging_status"], -1)
        vals = m.decode_values(m.encode(raw))
        self.assertEqual(vals["Charging_status"], 1)
        raw["Charging_status"] = s.to_raw(0)
        self.assertEqual(m.decode_values(m.encode(raw))["Charging_status"], 0)

    def test_composite_split(self):
        m = self.m.by_name["BMS_Volt_Curnt_Demand"]
        c = next(x for x in m.comps if x.name == "Voltage")
        raw = m.default_raw()
        raw.update(c.split(54.4))
        self.assertAlmostEqual(m.decode_values(m.encode(raw))["Voltage"], 54.4, places=3)
        raw.update(c.split(6553.5))
        self.assertAlmostEqual(m.decode_values(m.encode(raw))["Voltage"], 6553.5, places=3)

    def test_out_of_dbc_range_is_still_sendable(self):
        """Negative testing needs values outside the DBC's declared range (bit field is the only limit)."""
        m = self.m.by_name["Controller_BMS"]
        s = m.by_name["Throttle_Position"]  # DBC [0|100], 8 bit
        self.assertEqual(s.to_raw(200), 200)
        self.assertFalse(s.in_dbc_range(200))
        self.assertEqual(s.to_raw(9999), 255)  # clamped to the field, not wrapped

    def test_period_from_comment(self):
        self.assertEqual(self.m.by_name["BMS_Batt_Info_1"].cycle_ms, 500)
        self.assertEqual(self.m.by_name["Charger_Ready"].cycle_ms, 100)
        self.assertEqual(self.m.by_name["Charger_Stop"].cycle_ms, 1000)


@needs_samples
class TestDocs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = Model([BAT, MOT])
        cls.docs = [docmod.read_document(os.path.join(S, f)) for f in DOCS]
        cls.a = docmod.analyze(cls.m, cls.docs)

    def test_readers(self):
        for d in self.docs:
            self.assertEqual(d["error"], "")
            self.assertTrue(d["blocks"])

    def test_requirements_link_to_signals(self):
        by = {r["title"]: r for r in self.a["requirements"]}
        self.assertIn("Motor_Error", [u.split(".")[1] for u in by["MOTOR ALERT"]["signals"]])
        soc = [u.split(".")[1] for u in by["SOC"]["signals"]]
        self.assertEqual(soc, ["SOC"])  # via the sig_soc alias
        self.assertTrue(by["SIDE STAND"]["na"])

    def test_value_map_resolution(self):
        vm = {v["title"]: v for v in self.a["value_maps"]}
        bms = vm["1. BMS Alerts"]
        t = {e["label"]: e["target"] for e in bms["entries"]}
        self.assertEqual((t["Cell Over Voltage"]["signal"], t["Cell Over Voltage"]["value"]),
                         ("BMS_Batt_Info_3.Bms_Flt_sts", 6))
        self.assertEqual(t["Discharge Over Current"]["value"], 8)
        mcu = {e["label"]: e for e in vm["2. MCU Alerts"]["entries"]}
        thr = mcu["Controller Temperature Fault (> 65 °C)"]["target"]
        self.assertEqual(thr["signal"], "Controller_BMS.Controller_Temperature")
        self.assertEqual(thr["value"], 66)

    def test_mismatch_suggestions(self):
        g = {x["token"]: x["guesses"] for x in self.a["mismatches"]}
        self.assertIn("Throttle_Position", g.get("sig_throttle_position_unsigned", []))


@needs_samples
class TestEngineLoopback(unittest.TestCase):
    def test_tx_period_counter_and_rx(self):
        m = Model([BAT, MOT])
        chan = "t_engine"
        eng = Engine()
        other = can.Bus(interface="virtual", channel=chan)
        try:
            roles = {x.name: "ignore" for x in m.messages}
            roles["BMS_Batt_Info_1"] = "sim"      # periodic 500 ms
            roles["Controller_BMS"] = "sim"
            eng.configure(m, roles, {"Controller_BMS": 20})
            eng.open("virtual", chan, 500000)
            eng.set_raw("Controller_BMS", {"Throttle_Position": 55})
            eng.start_tx(True)
            time.sleep(1.0)
            eng.stop_all()
            got = []
            while True:
                f = other.recv(0.05)
                if f is None:
                    break
                got.append(f)
            cb = m.by_name["Controller_BMS"]
            frames = [f for f in got if f.arbitration_id == cb.frame_id and f.is_extended_id]
            self.assertGreater(len(frames), 35, "20 ms period should give ~50 frames/s")
            self.assertEqual(cb.decode_values(frames[-1].data)["Throttle_Position"], 55)
            slow = [f for f in got if f.arbitration_id == 0x201]
            self.assertTrue(1 <= len(slow) <= 4)
            n = len(got)
            time.sleep(0.2)
            self.assertIsNone(other.recv(0.1), "E-stop must silence the bus")
        finally:
            other.shutdown()
            eng.shutdown()

    def test_rx_decoding_of_listen_message(self):
        m = Model([BAT, MOT])
        chan = "t_rx"
        eng = Engine()
        dut = can.Bus(interface="virtual", channel=chan)
        try:
            roles = {x.name: "ignore" for x in m.messages}
            roles["Controller_BMS"] = "listen"
            eng.configure(m, roles, {})
            eng.open("virtual", chan, 500000)
            cb = m.by_name["Controller_BMS"]
            raw = cb.default_raw()
            raw["Motor_Error"] = 1
            raw["Charging_status"] = -1
            dut.send(can.Message(arbitration_id=cb.frame_id, is_extended_id=True, data=cb.encode(raw)))
            self.assertEqual(wait_for(lambda: eng.rx_value("Controller_BMS", "Motor_Error")), 1)
            self.assertEqual(eng.rx_value("Controller_BMS", "Charging_status"), 1)
            self.assertLess(eng.rx_age("Controller_BMS"), 1.0)
        finally:
            dut.shutdown()
            eng.shutdown()


@needs_samples
class TestAppEndToEnd(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="canbench_ws_")
        self.app = App(self.ws)
        self.app.new_project("E2E")
        for f in (BAT, MOT):
            self.app.add_file(os.path.basename(f), open(f, "rb").read())
        for f in DOCS:
            self.app.add_file(f, open(os.path.join(S, f), "rb").read())
        self.chan = "t_e2e"
        self.dut = can.Bus(interface="virtual", channel=self.chan)

    def tearDown(self):
        self.dut.shutdown()
        self.app.shutdown()
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_project_setup_derives_everything(self):
        st = self.app.state()
        self.assertEqual(st["model"]["summary"]["messages"], 31)
        self.assertGreater(len(st["analysis"]["requirements"]), 30)
        self.assertGreater(len(st["scenarios"]), 80)
        self.assertEqual(self.app.project["roles"]["New_Message_28"], "ignore")
        groups = {s["group"] for s in st["scenarios"]}
        self.assertIn("Message loss", groups)
        self.assertTrue(any("BMS Alerts" in g for g in groups))

    def test_signal_goes_on_the_wire_and_estop(self):
        a = self.app
        a.connect("virtual", self.chan, 500000)
        a.start_tx(True)
        r = a.set_value("Controller_BMS.Throttle_Position", 77)
        self.assertEqual(r["value"], 77)
        a.set_value("Controller_BMS.Charging_status", 1)
        cb = a.model.by_name["Controller_BMS"]

        def last_frame():
            f = None
            while True:
                x = self.dut.recv(0.05)
                if x is None:
                    return f
                if x.arbitration_id == cb.frame_id:
                    f = x

        time.sleep(0.4)
        vals = cb.decode_values(last_frame().data)
        self.assertEqual(vals["Throttle_Position"], 77)
        self.assertEqual(vals["Charging_status"], 1)
        a.estop()
        last_frame()
        self.assertIsNone(self.dut.recv(0.3))

    def test_scenarios_auto_pass_and_fail_from_checks(self):
        a = self.app
        a.set_role("Controller_BMS", "listen")  # DUT echo: the 'DUT' mirrors BMS errors back as Controller_BMS
        a.connect("virtual", self.chan, 500000)
        a.start_tx(True)
        cb = a.model.by_name["Controller_BMS"]
        stop = threading.Event()

        def dut_echo():
            raw = cb.default_raw()
            raw["Motor_Error"] = 1
            while not stop.is_set():
                self.dut.send(can.Message(arbitration_id=cb.frame_id, is_extended_id=True, data=cb.encode(raw)))
                time.sleep(0.05)

        threading.Thread(target=dut_echo, daemon=True).start()
        try:
            ok = a.save_scenario({"title": "echo ok", "set": [], "dwell_ms": 100,
                                  "checks": [{"type": "can", "signal": "Controller_BMS.Motor_Error", "op": "==",
                                              "value": 1, "timeout_ms": 1000}]})
            bad = a.save_scenario({"title": "echo wrong", "set": [], "dwell_ms": 100,
                                   "checks": [{"type": "can", "signal": "Controller_BMS.Overload_Error", "op": "==",
                                               "value": 1, "timeout_ms": 400}]})
            a.run([ok["id"], bad["id"]], manual="skip")
            wait_for(lambda: not a.runner.state["running"], 10)
            res = {r["title"]: r for r in a.results.items}
            self.assertEqual(res["echo ok"]["verdict"], "PASS")
            self.assertEqual(res["echo ok"]["mode"], "auto")
            self.assertEqual(res["echo wrong"]["verdict"], "FAIL")
            self.assertFalse(res["echo wrong"]["checks"][0]["ok"])
        finally:
            stop.set()

    def test_manual_scenario_waits_for_operator_and_is_never_pass_by_default(self):
        a = self.app
        a.connect("virtual", self.chan, 500000)
        a.start_tx(True)
        sc = a.save_scenario({"title": "manual one", "set": [{"uid": "Controller_BMS.Motor_Error", "value": 1}],
                              "dwell_ms": 100})
        a.run([sc["id"]], manual="prompt")
        self.assertTrue(wait_for(lambda: a.runner.state["awaiting"], 5))
        time.sleep(0.3)
        self.assertEqual(len(a.results.items), 0, "must not decide on its own")
        a.runner.verdict("fail", "LED stayed off")
        wait_for(lambda: not a.runner.state["running"], 5)
        r = a.results.items[-1]
        self.assertEqual((r["verdict"], r["mode"], r["note"]), ("FAIL", "manual", "LED stayed off"))
        self.assertIn("Motor_Error=1", r["stimulus"])

    def test_abort_restores_baseline_and_results_persist(self):
        a = self.app
        a.connect("virtual", self.chan, 500000)
        a.start_tx(True)
        sc = a.save_scenario({"title": "to abort", "set": [{"uid": "Controller_BMS.Motor_Error", "value": 1}],
                              "dwell_ms": 100})
        a.run([sc["id"]])
        wait_for(lambda: a.runner.state["awaiting"], 5)
        a.runner.abort()
        wait_for(lambda: not a.runner.state["running"], 5)
        self.assertEqual(a.values["Controller_BMS.Motor_Error"], 0)
        a.record_manual({"title": "x", "verdict": "pass"})
        a2 = App(self.ws)  # reopen from disk
        self.assertEqual(len(a2.results.items), 1)
        a2.shutdown()

    def test_run_requires_tx(self):
        with self.assertRaises(AppError):
            self.app.run(["x"])

    def test_capture_scenario_from_current_state(self):
        a = self.app
        a.connect("virtual", self.chan, 500000)
        a.set_value("Controller_BMS.Throttle_Position", 40)
        a.set_enabled("Charger_Ready", False)
        sc = a.capture_scenario("cap", "expect", "G", 500)
        self.assertEqual(sc["set"], [{"uid": "Controller_BMS.Throttle_Position", "value": 40}])
        self.assertEqual(sc["tx_off"], ["Charger_Ready"])

    def test_report_and_csv(self):
        a = self.app
        a.record_manual({"title": "t1", "verdict": "pass", "expected": "x"})
        a.record_manual({"title": "t2 <b>", "verdict": "fail", "note": "n"})
        self.assertIn(b"t1", a.csv_bytes())
        rep = a.report_bytes().decode()
        self.assertIn("t2 &lt;b&gt;", rep)          # escaped
        self.assertIn("SHA-256", rep)
        self.assertNotIn("t2 <b>", rep)


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ws = tempfile.mkdtemp(prefix="canbench_srv_")
        cls.srv = Server(cls.ws, 18765)
        cls.srv.start()
        cls.base = f"http://127.0.0.1:{cls.srv.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        shutil.rmtree(cls.ws, ignore_errors=True)

    def call(self, path, body=None, token=True, headers=None, method=None):
        url = self.base + path + (("&" if "?" in path else "?") + "t=" + self.srv.token if token and body is None
                                  and method != "POST" else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"))
        if token:
            req.add_header("X-Token", self.srv.token)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_page_has_token_and_api_is_guarded(self):
        code, body = self.call("/", token=False)
        self.assertEqual(code, 200)
        self.assertIn(self.srv.token.encode(), body)
        self.assertEqual(self.call("/api/state", token=False)[0], 401)
        self.assertEqual(self.call("/api/state?t=wrong", token=False)[0], 401)
        self.assertEqual(self.call("/api/state", headers={"Host": "evil.example.com"})[0], 403)
        self.assertEqual(self.call("/api/state", headers={"Origin": "http://evil.example.com"})[0], 403)

    @needs_samples
    def test_project_flow_and_upload(self):
        code, body = self.call("/api/project/new", {"name": "api test"})
        self.assertEqual(code, 200)
        url = f"{self.base}/api/file?name=battery.dbc&t={self.srv.token}"
        req = urllib.request.Request(url, data=open(BAT, "rb").read(), method="POST")
        req.add_header("X-Token", self.srv.token)
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.status, 200)
        code, body = self.call("/api/state")
        st = json.loads(body)
        self.assertEqual(st["model"]["summary"]["messages"], 28)
        code, body = self.call("/api/signal", {"uid": "nope.nope", "value": 1})
        self.assertEqual(code, 400)
        self.assertIn("error", json.loads(body))
        # bad upload type is refused cleanly
        req = urllib.request.Request(f"{self.base}/api/file?name=evil.exe&t={self.srv.token}", data=b"MZ", method="POST")
        req.add_header("X-Token", self.srv.token)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)

    def test_ui_bundle_contains_every_view(self):
        code, body = self.call("/static/app.js", token=False)
        self.assertEqual(code, 200)
        text = body.decode()
        for marker in ("CB.views.setup", "CB.views.simulate", "CB.views.scenarios", "CB.views.results",
                       "CB.views.trace", "CB.views.serial", "CB.views.firmware", "function buildUnknownCard",
                       "const FW ="):
            self.assertIn(marker, text)
        page = self.call("/", token=False)[1].decode()
        self.assertEqual(page.count("<script src="), 1, "the page must load exactly one bundle")

    def test_sse_stream_delivers_live_events(self):
        req = urllib.request.Request(f"{self.base}/events?t={self.srv.token}")
        got = []
        with urllib.request.urlopen(req, timeout=5) as r:
            end = time.time() + 3
            while time.time() < end and not any("event: live" in x for x in got):
                got.append(r.readline().decode())
        self.assertTrue(any("event: live" in x for x in got))


GENERIC_DBC = '''VERSION ""

NS_ :

BS_:

BU_: VCU DUT_ECU

BO_ 256 VCU_Cmd: 8 VCU
 SG_ Cmd_Counter : 56|4@1+ (1,0) [0|15] "" DUT_ECU
 SG_ Gear : 0|2@1+ (1,0) [0|3] "" DUT_ECU
 SG_ Torque_Request : 8|12@1- (0.5,0) [-1000|1000] "Nm" DUT_ECU
 SG_ Park_Brake : 2|1@1+ (1,0) [0|1] "" DUT_ECU

BO_ 2566844672 DUT_Status: 4 DUT_ECU
 SG_ Fault_Active : 0|1@1+ (1,0) [0|1] "" VCU
 SG_ Mode : 8|3@1+ (1,0) [0|7] "" VCU

CM_ BO_ 256 "Cycle 20 ms";
VAL_ 256 Gear 0 "Park" 1 "Reverse" 2 "Neutral" 3 "Drive" ;
VAL_ 2566844672 Mode 0 "Off" 1 "Eco" 2 "Sport" ;
'''


class TestGenericDbc(unittest.TestCase):
    """The bench must work on a DBC it has never seen, with no project-specific code."""

    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="canbench_gen_")
        self.app = App(self.ws)
        self.app.new_project("generic")
        self.app.add_file("toy.dbc", GENERIC_DBC.encode())
        self.dut = can.Bus(interface="virtual", channel="t_generic")

    def tearDown(self):
        self.dut.shutdown()
        self.app.shutdown()
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_dut_node_assigns_roles_and_scenarios_follow(self):
        a = self.app
        self.assertEqual(a.model.nodes(), ["DUT_ECU", "VCU"])
        a.set_dut_node("DUT_ECU")
        self.assertEqual(a.project["roles"], {"VCU_Cmd": "sim", "DUT_Status": "listen"})
        titles = {s["title"] for s in a.scenarios()}
        self.assertIn("Gear = Drive (3)", titles)
        self.assertIn("Loss of VCU_Cmd", titles)
        self.assertIn("Park_Brake = 1", titles)
        self.assertNotIn("Loss of DUT_Status", titles, "the DUT's own frames cannot be stopped by the bench")

    def test_period_counter_and_negative_range_on_the_wire(self):
        a = self.app
        a.set_dut_node("DUT_ECU")
        a.connect("virtual", "t_generic", 500000)
        a.start_tx(True)
        a.set_value("VCU_Cmd.Torque_Request", -250)
        msg = a.model.by_name["VCU_Cmd"]
        self.assertEqual(msg.cycle_ms, 20)
        frames = []
        end = time.time() + 0.6
        while time.time() < end:
            f = self.dut.recv(0.05)
            if f and f.arbitration_id == 256:
                frames.append(f)
        self.assertGreater(len(frames), 15)
        vals = [msg.decode_values(f.data) for f in frames]
        self.assertEqual(vals[-1]["Torque_Request"], -250)
        counters = [v["Cmd_Counter"] for v in vals]
        steps = {(b - a_) % 16 for a_, b in zip(counters, counters[1:])}
        self.assertEqual(steps, {1}, "rolling counter must increment by one per frame and wrap at 15")

    def test_auto_check_on_dut_frame(self):
        a = self.app
        a.set_dut_node("DUT_ECU")
        a.connect("virtual", "t_generic", 500000)
        a.start_tx(True)
        st = a.model.by_name["DUT_Status"]
        stop = threading.Event()

        def dut():  # a fake DUT: raises Fault_Active whenever the bench commands Reverse
            while not stop.is_set():
                g = a.engine.get_raw("VCU_Cmd").get("Gear", 0)
                raw = st.default_raw()
                raw["Fault_Active"] = 1 if g == 1 else 0
                self.dut.send(can.Message(arbitration_id=st.frame_id, is_extended_id=True, data=st.encode(raw)))
                time.sleep(0.03)

        threading.Thread(target=dut, daemon=True).start()
        try:
            chk = {"type": "can", "signal": "DUT_Status.Fault_Active", "op": "==", "value": 1, "timeout_ms": 1500}
            good = a.save_scenario({"title": "reverse -> fault", "set": [{"uid": "VCU_Cmd.Gear", "value": 1}],
                                    "dwell_ms": 100, "checks": [chk]})
            bad = a.save_scenario({"title": "drive -> fault?", "set": [{"uid": "VCU_Cmd.Gear", "value": 3}],
                                   "dwell_ms": 100, "checks": [dict(chk, timeout_ms=500)]})
            a.run([good["id"], bad["id"]], manual="skip")
            wait_for(lambda: not a.runner.state["running"], 10)
            v = {r["title"]: r["verdict"] for r in a.results.items}
            self.assertEqual(v, {"reverse -> fault": "PASS", "drive -> fault?": "FAIL"})
        finally:
            stop.set()


class TestProjectNames(unittest.TestCase):
    """Regression: names with dots (e.g. 'Cluster V0.0.1') used to fail with 'bad project id'."""

    def test_any_reasonable_name_creates_a_project(self):
        ws = tempfile.mkdtemp(prefix="canbench_names_")
        try:
            a = App(ws)
            names = ["Cluster bring-up", "Varcas cluster V0.0.1", "FirmwareVer_0.0.1", "..", "a", "   x   ",
                     "Varcas – ÉV test", "a/b\\c:d*e?", "VARCAS_4G", "ñ" * 80, "x" * 300]
            for n in names:
                p = a.new_project(n)
                self.assertTrue(os.path.isfile(os.path.join(ws, p["id"], "project.json")), n)
                a.add_file("t.dbc", GENERIC_DBC.encode())
            self.assertEqual(len(a.projects()), len(names))
            a.shutdown()
            b = App(ws)  # reopening picks one up
            self.assertIsNotNone(b.project)
            b.shutdown()
        finally:
            shutil.rmtree(ws, ignore_errors=True)


class TestWatchdog(unittest.TestCase):
    def test_tx_stops_when_the_ui_disappears(self):
        ws = tempfile.mkdtemp(prefix="canbench_wd_")
        srv = Server(ws, 18790)
        srv.NO_UI_TIMEOUT = 0.6
        srv.start()
        try:
            a = srv.app
            a.new_project("wd")
            a.add_file("toy.dbc", GENERIC_DBC.encode())
            a.connect("virtual", "t_wd", 500000)
            a.start_tx(True)
            self.assertTrue(a.engine.tx_on)
            self.assertTrue(wait_for(lambda: not a.engine.tx_on, 3.0), "watchdog must stop TX with no UI connected")
        finally:
            srv.stop()
            shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
