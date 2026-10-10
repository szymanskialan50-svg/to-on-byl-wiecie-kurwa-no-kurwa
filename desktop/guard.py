"""Protects the virtual camera from having TWO programs write into it at the same time.

Symptom of that problem: in OBS / Zoom the picture flips between two different images (the live
window of USC-CAM looks fine, because it does not use the virtual camera). It happens when an old
USC-CAM helper process survives a crash / forced close (nothing told it to stop) and keeps feeding
its last picture into 'HD Camera iPhone' while the new run feeds the real one.

 - acquire(): only ONE USC-CAM window may run at a time.
 - kill_stale(): at start, ends every other USC-CAM.exe left over from an earlier run (any version).
"""
import csv
import os
import socket
import subprocess
import sys

LOCK_PORT = 49213
_NO_WINDOW = 0x08000000


def acquire():
    """Returns a socket that must be kept alive for the whole run, or None if another copy is running."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def kill_stale():
    """Windows, installed .exe only: ends other USC-CAM.exe processes (not this one, not its launcher).
    Call it only after acquire() succeeded, so no other real window can exist."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return 0
    exe = os.path.basename(sys.executable)
    me, parent = os.getpid(), os.getppid()
    killed = 0
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s" % exe, "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=10).stdout
        for row in csv.reader(out.splitlines()):
            if len(row) > 1 and row[0].lower() == exe.lower() and row[1].isdigit():
                pid = int(row[1])
                if pid in (me, parent):
                    continue
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                               creationflags=_NO_WINDOW, timeout=10)
                killed += 1
    except Exception:                            # noqa: BLE001 - best effort only
        pass
    return killed
