CANBench  -  CAN test bench and UDS firmware updater          (ConnectM)
=====================================================================

WHAT YOU GET
  CANBench.exe   one file. No installation, no Python needed.

REQUIREMENTS
  * Windows 10 or 11 (64-bit) with Microsoft Edge or Chrome (Edge is already on Windows).
  * A PEAK PCAN-USB adapter needs the free PCAN driver (PEAK-System "PCAN-Basic" / device driver)
    installed once on the PC. Without hardware you can still try everything with interface "virtual".

START
  1. Put CANBench.exe in its OWN folder, e.g.  C:\CANBench\   (not in Program Files, not on a read-only share).
  2. Double-click it. A window opens. A black console window stays open behind it - closing that
     console (or the power button in the window) stops the program and all CAN transmission.
  3. First time only: Windows SmartScreen may say "Windows protected your PC" because the program is
     not code-signed. Click "More info" -> "Run anyway". If your antivirus asks, allow it.

YOUR DATA
  Everything is saved in the folder  CANBench_workspace  next to CANBench.exe:
      CANBench_workspace\<project>\project.json    settings
                                   \assets\        your database / documents / firmware images (copies)
                                   \results.jsonl  every Pass/Fail, saved immediately
                                   \logs\          CAN bus recordings (.asc)
  The Info page in the program shows the exact paths and has "Open folder" buttons.
  To share a project with a colleague: copy its folder into THEIR CANBench_workspace folder.
  To back up: copy the CANBench_workspace folder.

UPDATING
  Close CANBench, replace CANBench.exe with the new one. Your CANBench_workspace folder is untouched.

GOOD TO KNOW
  * Only ONE copy of CANBench can run per PC (two cannot share the CAN adapter). Starting it a
    second time just brings up the first window.
  * Do not flash firmware, disconnect or switch project while an update is running.
  * Reporting a problem: open the Info page, press "Copy version details" and include it,
    plus the bus log (.asc) from the logs folder if it is about CAN traffic.
