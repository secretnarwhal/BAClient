"""Reopen the client after the panel's Restart: wait for the old process to end, close its panel, start a new one.

Started by the client just before it quits. It does nothing if the old client doesn't exit.
"""
import ctypes
import sys

from .. import control, journal

SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0
EXIT_WAIT_MS = 40000
PROFILE = journal.ROOT / "data" / "ui" / "chrome-profile"


def wait_exit(pid, timeout_ms=EXIT_WAIT_MS):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel.OpenProcess(SYNCHRONIZE, False, int(pid))
    if not handle:
        return True                     # already gone
    try:
        return kernel.WaitForSingleObject(handle, timeout_ms) == WAIT_OBJECT_0
    finally:
        kernel.CloseHandle(handle)


def close_panels():
    """The old panel points at the old client's address, so it would otherwise stay as a dead second window."""
    import win32con
    import win32gui

    from . import panelwin
    for hwnd, _ in panelwin.panel_states(PROFILE):
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)


def run(pid, wait=wait_exit, close=close_panels, spawn=None):
    if journal.read_only():
        return control.EXIT_REFUSED
    if not wait(pid):
        return control.EXIT_ERROR
    try:
        close()
    except Exception:                   # a leftover window is harmless
        pass
    (spawn or control.spawn_client)()
    return control.EXIT_OK


if __name__ == "__main__":
    sys.exit(run(int(sys.argv[1])))
