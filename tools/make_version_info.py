"""Writes assets/version_info.txt (the Windows 'Details' tab of CANBench.exe) from canbench/__init__.py, so the exe, the About
page and `CANBench.exe --version` always show the same version.   Called by build_exe.bat."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from canbench import APP_NAME, COMPANY, __version__  # noqa: E402


def main():
    parts = [int(x) for x in __version__.split(".")[:3]] + [0, 0, 0, 0]
    v = tuple(parts[:4])
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', '{COMPANY}'),
      StringStruct('FileDescription', '{APP_NAME} - CAN test bench and UDS firmware updater'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('InternalName', '{APP_NAME}'),
      StringStruct('LegalCopyright', '(c) {COMPANY}'),
      StringStruct('OriginalFilename', '{APP_NAME}.exe'),
      StringStruct('ProductName', '{APP_NAME}'),
      StringStruct('ProductVersion', '{__version__}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    out = os.path.join(ROOT, "assets")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "version_info.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    print("version info", __version__)


if __name__ == "__main__":
    main()
