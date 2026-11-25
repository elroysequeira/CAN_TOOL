"""Any project: CAN IDs from an Excel/CSV table (no DBC), other database formats, no VARCAS-specific behaviour."""
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import cantools  # noqa: E402

from canbench import docs, tablecan  # noqa: E402
from canbench.app import App, AppError  # noqa: E402


def write_xlsx(path, sheets):
    """Smallest valid-enough .xlsx (inline strings) - enough for our reader and for Excel itself."""
    def col(i):
        s = ""
        i += 1
        while i:
            i, r = divmod(i - 1, 26)
            s = chr(65 + r) + s
        return s
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        for n, rows in enumerate(sheets, 1):
            xml = ['<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
            for ri, row in enumerate(rows, 1):
                xml.append(f'<row r="{ri}">')
                for ci, v in enumerate(row):
                    if v != "":
                        xml.append(f'<c r="{col(ci)}{ri}" t="inlineStr"><is><t>{escape(str(v))}</t></is></c>')
                xml.append("</row>")
            xml.append("</sheetData></worksheet>")
            z.writestr(f"xl/worksheets/sheet{n}.xml", "".join(xml))


SIGNAL_TABLE = [
    ["Vehicle CAN matrix - rev B", "", "", "", "", "", "", "", "", "", "", "", "", ""],
    ["CAN ID (hex)", "Message Name", "DLC", "Cycle Time (ms)", "Direction", "Signal Name", "Start Bit", "Length (bits)",
     "Byte Order", "Signed", "Factor", "Offset", "Unit", "Value Table"],
    ["0x201", "Pump_Status", "8", "100", "Tx", "Pump_Speed", "0", "16", "Intel", "unsigned", "0.5", "0", "rpm", ""],
    ["", "", "", "", "", "Pump_Mode", "16", "2", "Intel", "unsigned", "1", "0", "", "0=Off; 1=Eco; 2=Boost"],
    ["", "", "", "", "", "Pump_Temp", "24", "8", "Intel", "signed", "1", "-40", "degC", ""],
    ["0x301", "Pump_Command", "4", "50", "Rx", "Cmd_Enable", "0", "1", "Intel", "unsigned", "1", "0", "", "0=Disable;1=Enable"],
    ["", "", "", "", "", "Cmd_Setpoint", "8", "12", "Motorola", "unsigned", "0.1", "0", "bar", ""],
    ["0x18FF50E5", "Ext_Diag", "8", "1000", "Tx", "Diag_Code", "0", "16", "Intel", "unsigned", "1", "0", "", ""],
]


class TestTableParsing(unittest.TestCase):
    def test_signal_table_with_blank_group_cells(self):
        r = tablecan.parse_table(SIGNAL_TABLE)
        self.assertEqual(r["layout"], "signals")
        self.assertEqual([f["name"] for f in r["frames"]], ["Pump_Status", "Pump_Command", "Ext_Diag"])
        ps, pc, ex = r["frames"]
        self.assertEqual((ps["id"], ps["ext"], ps["dlc"], ps["period_ms"], ps["dir"]), (0x201, False, 8, 100, "tx"))
        self.assertEqual([s["name"] for s in ps["signals"]], ["Pump_Speed", "Pump_Mode", "Pump_Temp"])
        self.assertEqual(ps["signals"][1]["choices"], {0: "Off", 1: "Eco", 2: "Boost"})
        self.assertTrue(ps["signals"][2]["signed"] and ps["signals"][2]["offset"] == -40)
        self.assertFalse(pc["signals"][1]["little"])
        self.assertEqual((ex["id"], ex["ext"]), (0x18FF50E5, True))
        self.assertTrue(r["has_direction"])
        self.assertEqual(r["id_base"], "hex")

    def test_database_decodes_like_the_table_says(self):
        r = tablecan.parse_table(SIGNAL_TABLE)
        db = cantools.database.Database()
        path = os.path.join(tempfile.mkdtemp(), "x.dbc")
        open(path, "wb").write(tablecan.to_dbc_bytes(r))
        db.add_dbc_file(path)
        m = db.get_message_by_name("Pump_Status")
        raw = m.encode({"Pump_Speed": 1500.0, "Pump_Mode": "Boost", "Pump_Temp": 25})
        self.assertEqual(raw[0] | raw[1] << 8, 3000)               # factor 0.5
        self.assertEqual(raw[3], 25 + 40)                          # offset -40
        back = m.decode(raw)
        self.assertEqual(back["Pump_Speed"], 1500.0)
        self.assertEqual(str(back["Pump_Mode"]), "Boost")
        self.assertEqual(db.get_message_by_name("Ext_Diag").is_extended_frame, True)
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    def test_header_synonyms(self):
        rows = [["Msg ID", "Frame", "Signal", "Bit Position", "Size", "Endianness", "Scaling", "Min", "Max", "Units"],
                ["0x100", "Foo", "Bar", "8", "8", "Little", "2", "0", "500", "V"]]
        r = tablecan.parse_table(rows)
        s = r["frames"][0]["signals"][0]
        self.assertEqual((s["start"], s["length"], s["scale"], s["min"], s["max"], s["unit"]), (8, 8, 2.0, 0.0, 500.0, "V"))

    def test_id_base(self):
        rows = [["CAN ID", "Message", "Signal", "Start", "Length"], ["519", "Odo", "Dist", "0", "32"], ["201", "Soc", "S", "0", "8"]]
        r = tablecan.parse_table(rows)
        self.assertEqual(r["id_base"], "hex")
        self.assertEqual(r["frames"][0]["id"], 0x519)
        self.assertTrue(any("HEX" in w for w in r["warnings"]))     # the assumption is shown, not hidden
        d = tablecan.parse_table(rows, "dec")
        self.assertEqual(d["frames"][0]["id"], 519)
        self.assertEqual(tablecan.parse_table([["CAN ID (dec)", "Message"], ["519", "Odo"]])["frames"][0]["id"], 519)
        self.assertEqual(tablecan.parse_table([["CAN ID", "Message"], ["519h", "Odo"], ["0x20", "X"]])["frames"][0]["id"], 0x519)

    def test_frame_only_table_gives_raw_bytes(self):
        r = tablecan.parse_table([["ID", "Name", "DLC", "Period"], ["0x7DF", "Diag_Req", "8", "0"], ["0x123", "Hb", "2", "20"]])
        self.assertEqual(r["layout"], "frames")
        self.assertEqual([len(f["signals"]) for f in r["frames"]], [8, 2])
        self.assertEqual(r["frames"][1]["period_ms"], 20)
        self.assertTrue(any("no signal layout" in w for w in r["warnings"]))

    def test_bad_rows_are_reported_not_hidden(self):
        rows = [["CAN ID", "Message", "Signal", "Start", "Length"],
                ["0x10", "A", "x", "0", "8"], ["", "", "y", "4", "8"],             # overlaps x
                ["0x11", "B", "z", "60", "16"],                                     # does not fit
                ["zzz", "C", "w", "0", "1"]]                                        # bad id
        r = tablecan.parse_table(rows)
        txt = " | ".join(r["warnings"])
        self.assertIn("overlaps", txt)
        self.assertIn("does not fit", txt)
        self.assertIn("not a number", txt)
        self.assertEqual(r["skipped"], 3)

    def test_requirement_tables_are_not_mistaken_for_can_tables(self):
        self.assertIsNone(tablecan.parse_table([["No", "Requirement", "Expected"], ["1", "Show SOC", "80 %"]]))
        self.assertIsNone(tablecan.parse_table([["Alert name", "Value sent to cluster"], ["Low SOC", "12"]]))


H = ["CAN ID", "Signal Name", "Periodicity", "Byte Position", "Signal Size (Bits)", "Resolution", "Unit", "Remarks"]
# A real-world style sheet: title + node/baud lines, two blocks with their own header, no message names, byte positions
# in one block and bit positions (with a stray "Motorola") in another, flags described in remark text, 8-digit IDs.
MESSY = [
    ["MCU to VCU communication"],
    ["Motor Controller", "TX"], ["VCU", "Rx"], ["Communication baud rate", "250Kbps"], [""], H,
    ["0x000000A2", "Speed mode", "50 msec", "0", "8", "1", "NA", "1-ECO,2 -POWER,3-SPORT"],
    ["", "rpm", "50 msec", "1", "16", "1", "rpm", ""],
    ["", "Status Feedback1", "50 msec", "3", "8", "NA", "NA",
     "Status flag\nbit[6] : Motor LOCK [0- unlock,1- lock)\nbit[4]: Forward or Reverse [0 Forward，1 Reverse ]\nbit[0]: REGEN"],
    ["", "Faults/Alarms", "50 msec", "4", "8", "NA", "NA", "bit[3] :ECU fault\nbit[0]: throttle fault"],
    ["", "Faults/Alarms", "50 msec", "5", "8", "NA", "NA", "alarm flag\nbit[7]: under voltage\nbit[6]: over voltage\n(0: no fault 1: fault)"],
    ["", "DC current", "50 msec", "6", "16", "0.01", "Amp", ""],
    ["0x000000A4", "Throttle Position", "50 msec", "0", "8", "1", "%", "0-100%"],
    ["", "Motor temperature", "50 msec", "1", "8", "1", "deg C", "-128 to +127 deg C"],
    ["0x533", "Reverse Max speed fault", "", "45", "1", "Motorola", "", ""],
    ["", "Foward Max speed fault", "", "44", "1", "", "", ""],
    ["0x532", "Motor RPM", "", "36", "15", "", "", "Motor shaft speed in RPM (+ve for forward, -ve reverse)"],
    ["", "Torque Estimate Nm", "", "53", "14", "", "", ""],
    ["0x420", "MCS id", "", "", "", "", "", ""],
    ["", "Software version", "", "", "", "", "", ""],
    [""],
    ["VCU to MCU Communication:"],
    ["VCU", "TX"], ["Motor Controller", "Rx"], [""], H,
    ["0x000000A3", "SOC", "500ms", "0", "8", "", "", "1 - only ECO\n0 - All modes enable"],
    ["", "Lock", "500", "1", "8", "1", "1", "1-Lock motor \n2-Unlock the motor"],
]


class TestMessySheet(unittest.TestCase):
    def setUp(self):
        self.r = tablecan.parse_table(MESSY)
        self.f = {x["id"]: x for x in self.r["frames"]}

    def sig(self, fid, name):
        return next(s for s in self.f[fid]["signals"] if s["name"] == name)

    def test_blocks_nodes_bitrate_and_naming(self):
        r = self.r
        self.assertEqual(r["bitrate"], 250000)
        self.assertEqual(r["nodes"], ["Motor_Controller", "VCU"])
        self.assertEqual(self.f[0xA2]["sender"], "Motor_Controller")
        self.assertEqual(self.f[0xA3]["sender"], "VCU")                   # second block has its own header and sender
        self.assertEqual(self.f[0xA2]["name"], "Motor_Controller_0x0A2")  # no message-name column: sender + ID
        self.assertEqual(self.f[0xA2]["period_ms"], 50)
        self.assertEqual(self.f[0xA3]["period_ms"], 500)

    def test_id_width_decides_frame_format_and_can_be_overridden(self):
        self.assertTrue(self.f[0xA2]["ext"])
        self.assertFalse(self.f[0x533]["ext"])
        self.assertTrue(any("8 hex digits" in w for w in self.r["warnings"]))
        std = {x["id"]: x for x in tablecan.parse_table(MESSY, id_format="std")["frames"]}
        self.assertFalse(std[0xA2]["ext"])
        allext = {x["id"]: x for x in tablecan.parse_table(MESSY, id_format="ext")["frames"]}
        self.assertTrue(allext[0x533]["ext"])

    def test_byte_positions_become_bit_starts(self):
        self.assertEqual((self.sig(0xA2, "rpm")["start"], self.sig(0xA2, "rpm")["length"]), (8, 16))
        self.assertEqual(self.sig(0xA2, "DC_current")["start"], 48)
        self.assertEqual(self.sig(0xA2, "DC_current")["scale"], 0.01)
        self.assertEqual(self.sig(0xA2, "Speed_mode")["choices"], {1: "ECO", 2: "POWER", 3: "SPORT"})

    def test_flag_bytes_are_split_into_bit_signals(self):
        self.assertEqual(self.sig(0xA2, "Motor_LOCK")["start"], 24 + 6)             # byte 3, bit 6
        self.assertEqual(self.sig(0xA2, "Motor_LOCK")["choices"], {0: "unlock", 1: "lock"})
        self.assertEqual(self.sig(0xA2, "Forward_or_Reverse")["choices"], {0: "Forward", 1: "Reverse"})
        self.assertEqual(self.sig(0xA2, "throttle_fault")["start"], 32)             # byte 4 bit 0
        self.assertEqual(self.sig(0xA2, "under_voltage")["start"], 40 + 7)          # byte 5 bit 7
        self.assertEqual(self.sig(0xA2, "under_voltage")["choices"], {0: "no fault", 1: "fault"})   # '(0: no fault 1: fault)'
        self.assertNotIn("Status_Feedback1", [s["name"] for s in self.f[0xA2]["signals"]])
        # two "Faults/Alarms" rows must not collide, and nothing overlaps
        names = [s["name"] for s in self.f[0xA2]["signals"]]
        self.assertEqual(len(names), len(set(names)))

    def test_ranges_and_signedness_from_remarks(self):
        t = self.sig(0xA4, "Motor_temperature")
        self.assertTrue(t["signed"])
        self.assertEqual((t["min"], t["max"]), (-128, 127))
        th = self.sig(0xA4, "Throttle_Position")
        self.assertFalse(th["signed"])
        self.assertEqual((th["min"], th["max"], th["unit"]), (0, 100, "%"))
        self.assertTrue(self.sig(0x532, "Motor_RPM")["signed"])                      # '-ve reverse'

    def test_bit_position_frames_with_stray_motorola(self):
        self.assertTrue(any("above 7" in w for w in self.r["warnings"]))
        for n in ("Reverse_Max_speed_fault", "Foward_Max_speed_fault"):
            self.assertFalse(self.sig(0x533, n)["little"])
        tq, rpm = self.sig(0x532, "Torque_Estimate_Nm"), self.sig(0x532, "Motor_RPM")
        self.assertEqual((tq["start"], tq["length"], tq["little"]), (53, 14, False))
        self.assertEqual((rpm["start"], rpm["length"], rpm["little"]), (36, 15, False))

    def test_order_is_inferred_when_only_one_fits(self):
        """Torque at bit 53 with 14 bits cannot be Intel (it would pass bit 63); no 'Motorola' word needed."""
        rows = [["CAN ID", "Signal", "Byte Position", "Size"], ["0x10", "A", "36", "15"], ["", "B", "53", "14"]]
        f = tablecan.parse_table(rows)["frames"][0]
        self.assertEqual([s["little"] for s in f["signals"]], [False, False])
        self.assertTrue(any("only Motorola fits" in w or "verify" in w.lower() for w in tablecan.parse_table(rows)["warnings"]))

    def test_unlaid_signals_become_raw_bytes_not_silence(self):
        self.assertEqual([s["name"] for s in self.f[0x420]["signals"]][:2], ["Byte0", "Byte1"])
        self.assertTrue(any("MCS_id" in w for w in self.r["warnings"]))

    def test_decodes_correctly_through_a_real_dbc(self):
        db = cantools.database.Database()
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "m.dbc")
            open(p, "wb").write(tablecan.to_dbc_bytes(self.r))
            db.add_dbc_file(p)
            m = db.get_message_by_frame_id(0x532)
            raw = m.encode({"Motor_RPM": -1234, "Torque_Estimate_Nm": 300})
            back = m.decode(raw)
            self.assertEqual((back["Motor_RPM"], back["Torque_Estimate_Nm"]), (-1234, 300))
            a2 = db.get_message_by_frame_id(0xA2 | 0x80000000)   # extended frame
            self.assertTrue(a2.is_extended_frame)
            raw = a2.encode({n.name: 0 for n in a2.signals} | {"rpm": 3000, "Motor_LOCK": 1, "DC_current": 12.5})
            self.assertEqual(raw[1] | raw[2] << 8, 3000)
            self.assertEqual(raw[3], 1 << 6)
            self.assertEqual(raw[6] | raw[7] << 8, 1250)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_app_import_sets_bitrate_and_offers_dut_choice(self):
        ws = tempfile.mkdtemp(prefix="canbench_mv_")
        try:
            app = App(ws)
            app.new_project("Motovolt MCU")
            x = os.path.join(ws, "can protocol (1).xlsx")
            write_xlsx(x, [MESSY])
            app.add_file("can protocol (1).xlsx", open(x, "rb").read())
            pv = app.preview_table("can_protocol_1_.xlsx") if "can_protocol_1_.xlsx" in app.tables else app.preview_table(next(iter(app.tables)))
            self.assertEqual(pv["bitrate"], 250000)
            name = next(iter(app.tables))
            res = app.import_table(name)
            self.assertEqual(res["nodes"], ["Motor_Controller", "VCU"])
            self.assertEqual(app.project["bus"]["bitrate"], 250000)
            st = app.state()
            self.assertIn("Motor_Controller", st["model"]["summary"]["nodes"])
            app.set_dut_node("Motor_Controller")
            roles = {m["name"]: m["role"] for m in app.state()["model"]["messages"]}
            self.assertEqual(roles["Motor_Controller_0x0A2"], "listen")
            self.assertEqual(roles["VCU_0x0A3"], "sim")
            app.shutdown()
        finally:
            shutil.rmtree(ws, ignore_errors=True)


class TestAppFromTable(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="canbench_tbl_")
        self.app = App(self.ws)
        self.app.new_project("Pump bench V1.2")

    def tearDown(self):
        self.app.shutdown()
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_excel_without_dbc_becomes_a_working_bench(self):
        x = os.path.join(self.ws, "pump matrix.xlsx")
        write_xlsx(x, [SIGNAL_TABLE])
        self.app.add_file("pump matrix.xlsx", open(x, "rb").read())
        st = self.app.state()
        self.assertEqual(st["model"]["summary"]["messages"], 0, "a spreadsheet alone is not a database yet")
        t = st["tables"]["pump_matrix.xlsx"]
        self.assertEqual((t["frames"], t["signals"]), (3, 6))
        res = self.app.import_table("pump_matrix.xlsx")
        self.assertEqual((res["frames"], res["signals"]), (3, 6))
        st = self.app.state()
        self.assertEqual(st["model"]["summary"]["messages"], 3)
        roles = {m["name"]: m["role"] for m in st["model"]["messages"]}
        self.assertEqual(roles, {"Pump_Status": "listen", "Pump_Command": "sim", "Ext_Diag": "listen"})   # Tx/Rx column used
        self.assertEqual(len(st["analysis"]["requirements"]), 0, "the imported matrix is the database, not a spec to check against")
        names = {m["name"]: m for m in st["model"]["messages"]}
        self.assertEqual(names["Pump_Command"]["period_ms"], 50)
        # it simulates: set a signal, TX goes out
        self.app.connect("virtual", "tbl_test", 500000)
        self.app.set_value("Pump_Command.Cmd_Enable", 1)
        self.app.start_tx(True)
        import time
        time.sleep(0.4)
        self.assertGreater(self.app.engine.tx["Pump_Command"].count, 0)
        self.app.start_tx(False)

    def test_not_a_can_table_is_refused_clearly(self):
        self.app.add_file("notes.csv", b"Topic,Owner\nFoo,Bar\n")
        with self.assertRaises(AppError) as c:
            self.app.import_table("notes.csv")
        self.assertIn("No CAN ID table", str(c.exception))

    def test_decimal_option(self):
        self.app.add_file("ids.csv", b"CAN ID,Message,Signal,Start Bit,Length\n1305,Odo,Dist,0,32\n")
        self.app.import_table("ids.csv", "dec")
        self.assertEqual(self.app.model.by_name["Odo"].frame_id, 1305)

    def test_other_database_formats_load(self):
        db = cantools.database.load_string(
            'VERSION ""\nNS_:\nBS_:\nBU_: A B\nBO_ 256 Hello: 8 A\n SG_ Val : 0|8@1+ (1,0) [0|255] "" B\n', "dbc")
        for ext, fmt in ((".kcd", "kcd"), (".sym", "sym")):
            d = tempfile.mkdtemp()
            try:
                path = os.path.join(d, "net" + ext)
                cantools.database.dump_file(db, path, database_format=fmt)
                app = App(os.path.join(d, "w"))
                app.new_project("fmt")
                app.add_file("net" + ext, open(path, "rb").read())
                self.assertEqual(app.state()["model"]["summary"]["messages"], 1, ext)
                app.shutdown()
            finally:
                shutil.rmtree(d, ignore_errors=True)

    def test_dut_node_decides_roles_for_any_project(self):
        """No name-prefix rules: whichever node you call the DUT, its frames are Listen."""
        dbc = ('VERSION ""\nNS_:\nBS_:\nBU_: ECU_X TESTER_Y\nBO_ 256 Cluster_Alpha: 8 TESTER_Y\n SG_ A : 0|8@1+ (1,0) [0|255] "" ECU_X\n'
               'BO_ 257 Other_Beta: 8 ECU_X\n SG_ B : 0|8@1+ (1,0) [0|255] "" TESTER_Y\n')
        self.app.add_file("net.dbc", dbc.encode())
        roles = {m["name"]: m["role"] for m in self.app.state()["model"]["messages"]}
        self.assertEqual(roles, {"Cluster_Alpha": "sim", "Other_Beta": "sim"}, "no hidden 'cluster_' rule any more")
        self.app.set_dut_node("ECU_X")
        roles = {m["name"]: m["role"] for m in self.app.state()["model"]["messages"]}
        self.assertEqual(roles, {"Cluster_Alpha": "sim", "Other_Beta": "listen"})


if __name__ == "__main__":
    unittest.main()
