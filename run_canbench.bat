@echo off
rem CANBench launcher - installs missing Python packages on first run, then starts the bench.
cd /d "%~dp0"
python -c "import can, cantools" 2>nul
if errorlevel 1 (
  echo Installing required packages ^(python-can, cantools, pyserial, pypdf^)...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Could not install the packages. Check your internet connection and that Python is on PATH.
    pause
    exit /b 1
  )
)
python -m canbench %*
if errorlevel 1 pause
