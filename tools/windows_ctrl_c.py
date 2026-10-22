"""Send Ctrl+C to a transfer's isolated hidden console on Windows."""
import argparse
import ctypes
import time


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pid", type=int)
    args = parser.parse_args()
    if args.pid <= 0:
        raise SystemExit("invalid process ID")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.FreeConsole()
    if not kernel.AttachConsole(args.pid):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # Protect this helper, while the transfer's SIGINT handler exits cleanly.
        kernel.SetConsoleCtrlHandler(None, True)
        if not kernel.GenerateConsoleCtrlEvent(0, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        time.sleep(0.2)
    finally:
        kernel.FreeConsole()
