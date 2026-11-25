@echo off
rem Builds dist\CANBench.exe (one file, no Python needed on the target PC).
rem Needs Python 3.10+ on THIS PC only.
cd /d "%~dp0"
python -m pip install --quiet -r requirements.txt pyinstaller
if errorlevel 1 goto fail
python tools\make_version_info.py
if errorlevel 1 goto fail
python -m PyInstaller --noconfirm --clean --onefile --name CANBench ^
  --icon assets\CANBench.ico --version-file assets\version_info.txt ^
  --add-data "canbench\web;canbench\web" ^
  --collect-submodules can --copy-metadata python-can ^
  --collect-submodules cantools.database --copy-metadata cantools ^
  --hidden-import serial.tools.list_ports --hidden-import isotp --hidden-import pypdf ^
  --exclude-module tkinter --exclude-module matplotlib --exclude-module numpy --exclude-module PIL ^
  canbench_launcher.py
if errorlevel 1 goto fail
echo.
echo Built: %~dp0dist\CANBench.exe
exit /b 0
:fail
echo BUILD FAILED
exit /b 1
