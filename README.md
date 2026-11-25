# CANBench

A DBC-driven CAN test bench. Give it a DBC (and, if you have them, the requirement documents) and it
builds the controls, the spec checklist and the test scenarios for you. Nothing in it is specific to
one vehicle or one DUT.

## Start

```
run_canbench.bat
```

First run installs `python-can`, `cantools`, `pyserial`, `pypdf`. A window opens (Edge/Chrome app
mode). Use the **virtual** interface to try everything without hardware; for a PEAK adapter pick
**pcan / PCAN_USBBUS1** (needs the PCAN-Basic driver).

Try it on the bundled samples: create a project, drop the files from `samples/` onto Setup.
`samples/cluster_output_0x519.dbc` describes the cluster's own output frame 0x519 (odometer, factor 0.1 km); its bit position is inferred, so check it against the display.

## Workflow

| Screen | What it does |
|---|---|
| **Setup** | Drop DBC + documents. Shows what was understood, DBC health findings, spec↔DBC mismatches, and the *role* of every message: **Simulate** (bench sends it), **Listen** (DUT sends it, bench decodes it), **Ignore**. Pick a *DUT node* if your DBC names nodes. |
| **Simulate** | One card per simulated message, controls generated from signal type (switch / enum / slider / raw box). Each signal shows the spec text that mentions it. Switch to **By requirement** to work through the document row by row with Pass / Fail / N/A. Live DUT output on the right. |
| **Scenarios** | Generated from the DBC (message loss, every enum value, every flag, min/max limits) and from documents (alert-code tables → stimulus per code). Run one, a group, or all. Add your own, or *Save as scenario* from the current Simulate state. |
| **Results** | Every verdict, saved to disk immediately. CSV and a self-contained HTML report (inputs with SHA-256, DUT, operator, bus settings, evidence per test, DBC findings). |
| **Send** | Put **any** CAN frame on the bus by hand: any ID (11/29-bit), up to 8 bytes, once or repeating every N ms for a duration / frame count. No DBC needed, independent of *Start TX*. Saved frames, a stop button per frame; E-STOP, Disconnect and closing the window stop them. Blocked while a firmware update runs. |
| **Trace / Serial** | Decoded CAN trace; debug-UART log with markers for every CAN change and scenario step. **Bus logs…** lists the recordings (see below). |

## DUT frames without a DBC (the frame designer)

Connect, and **Setup** / **Simulate** list every frame on the bus that no DBC describes, with live bytes
(recently changed bytes light up). **Define signals** opens a bit map: change something on the DUT, watch which
bits move, then give them signals (start bit, length, Intel/Motorola, signed, scale, offset, unit). The result is
written as a real DBC into the project and the frame becomes a **Listen** message, so scenarios can check it
and reports include it. If a DUT DBC exists, just drop it in instead.

## Firmware (UDS)

The **Firmware** screen reads and updates an ECU over UDS on the bench's own CAN connection (no second PCAN handle):
read identity (`22 F187/F188/F190`), then `10 02 → 27 → 31 erase → 34 → 36… → 37 → 31 CRC32 → 11`, wait, re-read.
Standard defaults (0x7E0 → 0x7E8, echo key, routines FF00/FF01, block counter 1…15, data per block as reported;
all editable, and *Reset to defaults* restores them). Behaviour, on purpose: 

* **Nothing is guessed:** the memory address must be set (default `0x08020000`; change it for your bootloader); an `.srec` shows its own start address.
* **Product guard (two-sided):** the image must contain the product keyword (e.g. `VARCAS`) *and* the ECU's own
  identity must contain it. A file for another product (the bundled `ATHER_…bin`) is refused, and so is an ECU of another product. Nothing is sent that changes the ECU before these pass.
* Typed **FLASH** confirmation, TX must be stopped, one UDS conversation at a time, abort button, quit is blocked mid-update.
* Negative responses are decoded (NRC names); *response pending* (0x78) is waited out instead of failing; a zero seed means "already unlocked".
* After reset it waits, then polls for the ECU; success = reports the **expected version**, or at least a *changed* identity.
* Every update is recorded in Results (all 11 steps as evidence) and appears in the HTML report.
* A read-only console (`22`, `3E`, `19`, `10 01/03`) is open; anything else needs “expert”.

`.bin` and `.srec`/`.s19` images are supported (S-record checksums verified, gaps filled with `FF` and reported).

## How verdicts are decided (so you can trust them)

* A scenario with **automatic checks** (CAN signal comparisons on frames the DUT sends, or serial
  regexes) is judged by the bench: PASS only if *every* check succeeds in time, otherwise FAIL.
* A scenario **without** checks pauses and asks you (**P**ass / **F**ail / **S**kip keys). It never passes by itself.
* *Auto only* mode skips scenarios that need a human instead of guessing.
* Each result stores the stimulus, expected text, check outcomes, the DUT's output frames at that moment
  and the serial tail.

## Safety and reliability decisions

* **E-STOP** (top bar) stops all transmission and aborts any run. Closing the window while transmitting
  stops TX after 15 s (watchdog). Quitting stops TX.
* The server listens on `127.0.0.1` only, with a per-launch token and Host/Origin checks.
* **Values outside the DBC range can be sent** (negative testing). Only the bit-field width limits a value;
  a ▲ marks out-of-range values.
* DBC quirks are reported, not hidden, and handled in a documented way:
  signed 1-bit signals (`@1-`, which tools cannot encode as 1) are sent as raw −1;
  `…_Hi_Byte/…_Lw_Byte` pairs are merged into one 16-bit value (toggle in Setup);
  rolling counters auto-increment per frame; periods come from the DBC attribute or a
  `Periodicity = 500ms` comment.
* Results, projects and the DBC/document copies are written atomically / append-only
  under `workspace/<project>/`. Reports carry the SHA-256 of every input file.
* Windows timer resolution is raised to 1 ms so 10–20 ms cyclic frames keep their period
  (per-message jitter is shown on the Simulate card when it exceeds 8 ms).

## Document understanding (heuristic — always reviewable)

Reads `.docx .xlsx .csv .tsv .md .txt .pdf`. It finds signal names in table rows and text
(distinctive names only; `sig_` prefixes resolve to DBC names), builds the requirement checklist, extracts
"label → number" tables (e.g. alert codes), and matches labels to DBC enums or flags. Matches are tagged
**auto / review / unresolved**; unresolved ones are left for you to assign in the scenario editor.
Threshold labels such as `Controller Temperature Fault (> 65 °C)` become a stimulus just past the threshold,
marked *review*.

## Any project, any database

Nothing in CANBench is specific to one product. A project is just: a CAN database + optional documents.

* **DBC** (`.dbc`), also `.kcd`, `.sym`, `.arxml`.
* **No DBC? An Excel/CSV table of CAN IDs.** Drop it into the documents box; if it has a header row with a CAN ID column and
  either message names (frames only) or signal name + start bit + length (signals), a *Use as CAN database* button appears.
  Recognised columns (many spellings each): CAN ID, message name, DLC, cycle time, direction (Tx/Rx), sender node,
  signal name, start bit, length, byte order, signed, factor, offset, min, max, unit, value table (`0=Off; 1=On`), comment.
  Message cells may be blank under the first row of a group. A frame-only table gives raw bytes (Byte0..Byte7).
  Sheets with several blocks are handled: each header row starts a block, "Motor Controller TX / VCU Rx" lines name the sender of
  the block, a "baud rate 250Kbps" line sets the project bitrate, frames without a name are called `<Sender>_0x<ID>`,
  "Byte Position" columns are read as byte indexes (or bit positions when a frame's values exceed 7), byte order is taken from a
  column / a stray "Motorola" / or whichever order fits without overlap, and remark text is mined for flag bits
  (`bit[6]: Motor LOCK [0-unlock,1-lock]`), value tables (`1-ECO, 2-POWER`) and ranges (`-128 to +127`).
  Press **Preview** to see every frame and signal plus the list of assumptions before using the table.
  **Sheet not read right?** Press **Map columns…**: click the row that holds the column titles, choose what each column contains
  (CAN ID, message name, start bit …) and optionally the node that sends the frames. The choice is recorded with the file.
  Every assumption (hex vs decimal IDs, byte order, rows skipped, direction read from the DUT's side) is listed next to the button.
* **Who is the DUT?** Pick the DUT node in *Message roles* (frames it sends become Listen, all others Simulate). A table with a
  Tx/Rx column sets this for you. There are no product-specific naming rules.
* Product names appear only where you type them (project name, DUT, product keyword for firmware files).

## Evidence: the bus log

Every connection is recorded to a **Vector ASC file** (`<project>/logs/canlog_<time>_<project>.asc`): every frame the bench sent
or received, error frames, and the bench's own markers (TX started, RESULT PASS …, FIRMWARE UPDATE …). Open it in CANalyzer,
PCAN-View or BUSMASTER. Each result records the name of the file that was recording; the report lists those files with their
SHA-256. Writing is on its own thread (it cannot slow the bus) and rolls over at 64 MB. Switch it off per project under
Trace → Bus logs… (only while disconnected). The status bar shows `● REC` while recording.

## One copy at a time

Two copies of CANBench would fight over the CAN adapter (a PCAN channel cannot be opened twice). Starting a second one opens the
window of the first instead. `--allow-multiple` overrides this (developers only). `CANBench.exe --version` prints the version;
click the logo in the window for the About page (version, build, libraries, workspace).

## Sharing with the team (.exe)

Run `build_exe.bat` once (needs Python on that PC). It produces `dist\CANBench.exe` (about 24 MB, one file).
Colleagues need **no Python**: copy the .exe anywhere and double-click it. Projects are kept in a
`CANBench_workspace` folder next to the .exe (or in Documents if that folder is read-only).
They do need the PEAK PCAN driver for a PCAN adapter (the driver is not part of the .exe).
The .exe is unsigned, so Windows SmartScreen or antivirus may ask for confirmation the first time.
Rebuild after every code change; the .exe does not update itself.

## Firmware update (UDS) in short

1. *Read identity* is read-only (UDS 0x22). It may run while the bench transmits.
2. *Start update* needs: connected, **TX stopped** (unless you tick *Keep simulated frames running during the update* in the parameters:
   for an ECU that needs them to stay in programming mode; the report then says so, and any frame that uses the update's own request/response
   IDs is refused), no scenario running, memory address set, product check passed, and the word `FLASH` typed.
3. If the ECU already reports the target version (typed in "expected version", else the `Vx.y.z` in the file name) the update is **not run**: "FW version is unchanged" (result: SKIP). Tick *Allow same version* to re-flash on purpose.
4. After the reboot the identity is read again; an identical identity is reported as "FW version is unchanged" (FAIL unless same-version is allowed).
5. **Abort** asks first and explains what it means at this step (before the erase: harmless; during the transfer: the ECU may not start).
   It stops at the next block boundary. E-STOP does *not* cut a firmware update (it stops simulated/manual frames); disconnecting,
   switching project and closing the program are refused while one runs.
6. An update that stops after the erase leaves a **recovery note in the project** (survives closing the program): the Firmware page
   shows a banner. **Recovery mode** re-flashes the same image to an ECU that reports no identity (bootloader): the identity checks
   before the update are skipped, the product check on the image and the CRC check stay, and the same version afterwards is accepted.

## Running the tests

```
python -m unittest discover -s tests
```

The tests use a virtual CAN bus and a fake ECU, so no hardware is needed. Some of them read real customer files (DBCs and requirement
documents) from a `samples/` folder. Those files are private and are **not in this repository**, so on a fresh clone those tests are
reported as *skipped*. To run them, copy the files named in `tests/test_core.py` (`S`, `BAT`, `MOT`, `DOCS`) into `samples/`.
The `.gitignore` keeps `samples/`, `workspace/`, firmware images, spreadsheets, documents and build output out of git.

## Layout

```
canbench/dbcmodel.py   DBC model, codec, health checks
canbench/engine.py     TX scheduler + RX decoder (threads)
canbench/docs.py       document readers + linking to the DBC
canbench/scenarios.py  scenario generation + runner
canbench/uds.py        UDS/ISO-TP client + firmware update job
canbench/tablecan.py   Excel/CSV CAN-ID table -> DBC (automatic + manual column mapping)
canbench/buslog.py     bus recorder (ASC files)      canbench/instance.py  one-copy-at-a-time guard
assets/                ConnectM logo, icon, exe version info (tools/make_brand_assets.py regenerates them)
canbench/app.py        controller        canbench/server.py  HTTP + live stream
canbench/web/          the UI (no build step, no CDN)
tests/                 python -m unittest discover -s tests
samples/               sample DBCs and documents
workspace/             your projects (created on first run)
```

## Limits (known)

* Classic CAN only (no CAN FD). UDS: 11-bit/29-bit normal addressing only. Multiplexed messages send the default page only.
* It cannot see the DUT's display: unless the DUT reports its state on CAN or UART, a human judges.
* Discrete IO (turn indicators, high beam, ...) is out of scope for a CAN tool; those spec rows appear as manual checks.
* Document linking is heuristic; check the *review* and *unresolved* rows.
