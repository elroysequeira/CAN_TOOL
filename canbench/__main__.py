"""python -m canbench  -> starts the bench and opens its window."""
import argparse
import os
import shutil
import subprocess
import sys
import webbrowser

from . import APP_NAME, COMPANY, __version__
from . import instance
from .server import Server


def find_browser():
    cands = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        shutil.which("msedge") or "", shutil.which("chrome") or "", shutil.which("google-chrome") or "",
    ]
    return next((c for c in cands if c and os.path.isfile(c)), None)


def open_window(url: str):
    b = find_browser()
    if b:
        subprocess.Popen([b, f"--app={url}", "--window-size=1500,940"])
    else:
        webbrowser.open(url)


def default_workspace() -> str:
    """Source checkout: <CANBench>/workspace.  Packaged .exe: a 'CANBench_workspace' folder next to the .exe
    (so a team can keep it with the tool), or in Documents when that folder is not writable."""
    if getattr(sys, "frozen", False):
        beside = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "CANBench_workspace")
        try:
            os.makedirs(beside, exist_ok=True)
            probe = os.path.join(beside, ".w")
            with open(probe, "w") as f:
                f.write("x")
            os.remove(probe)
            return beside
        except OSError:
            return os.path.join(os.path.expanduser("~"), "Documents", "CANBench_workspace")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workspace")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="canbench", description="DBC-driven CAN test bench")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--workspace", default=default_workspace())
    ap.add_argument("--no-browser", action="store_true", help="do not open a window")
    ap.add_argument("--allow-multiple", action="store_true",
                    help="start even if another CANBench is already running (two copies cannot share one CAN adapter)")
    ap.add_argument("--version", action="store_true", help="print the version and exit")
    args = ap.parse_args(argv)

    if args.version:
        print(f"{APP_NAME} {__version__} ({COMPANY})")
        return 0

    if not args.allow_multiple:
        other = instance.find_running()
        if other:
            if other["port"]:
                print(f"{APP_NAME} is already running ({other['url']}). Opening that window instead of starting a second copy -")
                print("two copies cannot share the CAN adapter. Use --allow-multiple only if you know you need it.")
                if not args.no_browser:
                    open_window(other["url"])
            else:
                print(f"{APP_NAME} is just starting in another window - wait a few seconds and try again.")
            return 0

    srv = Server(args.workspace, args.port)
    srv.start()
    if not args.allow_multiple:
        instance.claim(srv.port)
    url = f"http://127.0.0.1:{srv.port}/"
    print(f"{APP_NAME} {__version__}  ·  {COMPANY}  ·  {url}")
    print(f"workspace: {srv.app.store.root}")
    print("Close this window or press Ctrl+C to quit (all CAN transmission stops).")
    if not args.no_browser:
        open_window(url)
    try:
        while not srv.quit_evt.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()
        if not args.allow_multiple:
            instance.release()
        print("bye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
