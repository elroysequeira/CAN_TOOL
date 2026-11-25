"""Entry point for the packaged .exe (PyInstaller). From source use:  python -m canbench"""
import sys

from canbench.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
