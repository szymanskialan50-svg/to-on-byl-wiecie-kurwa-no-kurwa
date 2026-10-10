"""Fully automatic installation of everything the PC app needs (Windows only, no clicking).

    camera : virtual webcam "HD Camera iPhone"   (DirectShow filter, regsvr32)
    mic    : virtual microphone VB-CABLE          (silent install, publisher pre-trusted)
    usb    : Apple USB driver                     (Apple Mobile Device Support = usbmuxd)

Administrator rights are needed. If the app is started normally, main.py re-launches itself
elevated with `--setup` (ONE Windows prompt, only when something is missing). If the app is
started with "Run as administrator", the installation simply runs inside it.
Log: %TEMP%\\usc-cam-setup.log
"""
import ctypes
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

CAM_NAME = "HD Camera iPhone"
OLD_CAM_NAME = "HD Camera USC-CAM"
CREATE_NO_WINDOW = 0x08000000
USB_SERVICE = "Apple Mobile Device Service"

VBCABLE_URLS = [
    "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip",
    "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack43.zip",
]
ITUNES_URL = "https://www.apple.com/itunes/download/win64"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) USC-CAM"}

LOG = os.path.join(tempfile.gettempdir(), "usc-cam-setup.log")


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%H:%M:%S"), msg))
    except OSError:
        pass


def is_win():
    return sys.platform == "win32"


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:                            # noqa: BLE001
        return False


def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def run(cmd, timeout=600, cwd=None):
    """Runs a command without a console window. `cmd` may be a string (passed to Windows verbatim,
    needed for regsvr32 /i:Name="..." quoting) or a list. Returns the exit code (-1 = failed to run)."""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=cwd,
                           creationflags=CREATE_NO_WINDOW if is_win() else 0)
        log("run %s -> %s %s" % (cmd if isinstance(cmd, str) else " ".join(cmd), r.returncode,
                                  (r.stderr or b"")[:200].decode("utf-8", "replace").strip()))
        return r.returncode
    except Exception as e:                       # noqa: BLE001
        log("run %s failed: %s" % (cmd, e))
        return -1


# =============================================================================================
#  state checks (cheap, work without admin rights)
# =============================================================================================
def camera_installed():
    """True if a DirectShow video device called CAM_NAME is registered."""
    if not is_win():
        return False
    import winreg
    path = r"CLSID\{860BB310-5D01-11d0-BD3B-00A0C911CE86}\Instance"
    try:
        root = winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, path)
    except OSError:
        return False
    i = 0
    while True:
        try:
            sub = winreg.EnumKey(root, i)
        except OSError:
            return False
        i += 1
        try:
            with winreg.OpenKey(root, sub) as k:
                if winreg.QueryValueEx(k, "FriendlyName")[0] == CAM_NAME:
                    return True
        except OSError:
            pass


def cable_installed():
    """True if an ACTIVE playback endpoint 'CABLE Input' (VB-CABLE) exists."""
    if not is_win():
        return False
    import winreg
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render"
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return False
    i = 0
    while True:
        try:
            guid = winreg.EnumKey(root, i)
        except OSError:
            return False
        i += 1
        try:
            with winreg.OpenKey(root, guid) as ep:
                try:
                    state = winreg.QueryValueEx(ep, "DeviceState")[0]
                except OSError:
                    state = 1
                if state & 0xF != 1:
                    continue
                with winreg.OpenKey(ep, "Properties") as props:
                    j = 0
                    while True:
                        try:
                            _name, val, _t = winreg.EnumValue(props, j)
                        except OSError:
                            break
                        j += 1
                        if isinstance(val, str) and "cable input" in val.lower():
                            return True
        except OSError:
            pass


def usb_driver_installed():
    """True if the Apple Mobile Device Service (usbmuxd) is installed or already answering."""
    if not is_win():
        return True
    import winreg
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                       r"SYSTEM\CurrentControlSet\Services\%s" % USB_SERVICE))
        return True
    except OSError:
        pass
    try:
        socket.create_connection(("127.0.0.1", 27015), timeout=0.5).close()
        return True
    except OSError:
        return False


CHECKS = {"camera": camera_installed, "mic": cable_installed, "usb": usb_driver_installed}


def missing():
    if not is_win():
        return []
    return [name for name, check in CHECKS.items() if not check()]


# -- remember failures (e.g. no internet) so the Windows prompt does not appear on every start --
def _fail_key(name):
    return "SetupFail_" + name


def remember_failures(still_missing):
    if not is_win():
        return
    import winreg
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\USC-CAM") as k:
            for name in CHECKS:
                if name in still_missing:
                    winreg.SetValueEx(k, _fail_key(name), 0, winreg.REG_SZ, str(int(time.time())))
                else:
                    try:
                        winreg.DeleteValue(k, _fail_key(name))
                    except OSError:
                        pass
    except OSError:
        pass


def _recent_failure(name, hours=12):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\USC-CAM") as k:
            return time.time() - float(winreg.QueryValueEx(k, _fail_key(name))[0]) < hours * 3600
    except (OSError, ValueError):
        return False


def pending():
    """Components that are missing and worth trying now (not failed within the last 12 hours)."""
    return [n for n in missing() if not _recent_failure(n)]


# =============================================================================================
#  installers (need admin)
# =============================================================================================
def _download(url, dest, say, label):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        last = 0.0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if total and time.time() - last > 0.7:
                last = time.time()
                say("%s %d%%" % (label, got * 100 // total))
    return dest


def install_camera(say):
    src = resource_path("driver")
    if not os.path.isdir(src):
        log("driver folder missing: %s" % src)
        return False
    dst = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "HDCameraiPhone")
    os.makedirs(dst, exist_ok=True)
    for f in os.listdir(src):
        try:
            shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
        except OSError as e:                     # file in use by a running app: keep the old copy
            log("copy %s: %s" % (f, e))
    win = os.environ.get("SystemRoot", r"C:\Windows")
    reg64 = os.path.join(win, "System32", "regsvr32.exe")
    reg32 = os.path.join(win, "SysWOW64", "regsvr32.exe")
    d64 = os.path.join(dst, "HDCameraiPhone64.dll")
    d32 = os.path.join(dst, "HDCameraiPhone32.dll")
    # name used by older versions
    run('"%s" /s /u /n /i:UnityCaptureName="%s" "%s"' % (reg64, OLD_CAM_NAME, d64))
    run('"%s" /s /n /i:UnityCaptureName="%s" "%s"' % (reg64, CAM_NAME, d64))
    if os.path.exists(reg32) and os.path.exists(d32):          # 32-bit apps need the 32-bit filter
        run('"%s" /s /n /i:UnityCaptureName="%s" "%s"' % (reg32, CAM_NAME, d32))
    return camera_installed()


def _trust_publisher(folder):
    """Adds the signer of every driver file to 'Trusted Publishers' so Windows does not ask
    'Would you like to install this device software?'."""
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-ChildItem -LiteralPath '%s' -Recurse -Include *.cat,*.sys | ForEach-Object {"
        " $c=(Get-AuthenticodeSignature -FilePath $_.FullName).SignerCertificate;"
        " if($c){ $p=Join-Path $env:TEMP ('usccam_'+$c.Thumbprint+'.cer');"
        " [IO.File]::WriteAllBytes($p,$c.Export('Cert'));"
        " certutil -addstore -f TrustedPublisher $p | Out-Null } }" % folder.replace("'", "''"))
    run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], timeout=120)


def install_vbcable(say):
    tmp = tempfile.mkdtemp(prefix="usccam_vbcable_")
    zpath = os.path.join(tmp, "vbcable.zip")
    ok = False
    for url in VBCABLE_URLS:
        try:
            _download(url, zpath, say, "Downloading virtual microphone")
            ok = True
            break
        except Exception as e:                   # noqa: BLE001
            log("download %s failed: %s" % (url, e))
    if not ok:
        return False
    with zipfile.ZipFile(zpath) as z:
        z.extractall(tmp)
    setup = None
    for root, _dirs, files in os.walk(tmp):
        for name in files:
            if name.lower() == "vbcable_setup_x64.exe":
                setup = os.path.join(root, name)
    if setup is None:
        log("VBCABLE_Setup_x64.exe not found")
        return False
    say("Installing virtual microphone ...")
    _trust_publisher(os.path.dirname(setup))
    run([setup, "-i", "-h"], timeout=180, cwd=os.path.dirname(setup))
    for _ in range(40):                          # the new audio device needs a few seconds
        if cable_installed():
            return True
        time.sleep(1)
    return cable_installed()


def _wait_usb(seconds=40):
    for _ in range(seconds):
        if usb_driver_installed():
            run(["sc", "start", USB_SERVICE])
            for _ in range(15):
                try:
                    socket.create_connection(("127.0.0.1", 27015), timeout=0.5).close()
                    return True
                except OSError:
                    time.sleep(1)
            return True
        time.sleep(1)
    return usb_driver_installed()


def install_apple_usb(say):
    if usb_driver_installed():
        run(["sc", "start", USB_SERVICE])
        return True
    tmp = tempfile.mkdtemp(prefix="usccam_apple_")
    exe = os.path.join(tmp, "iTunes64Setup.exe")
    try:
        _download(ITUNES_URL, exe, say, "Downloading Apple USB driver")
    except Exception as e:                       # noqa: BLE001
        log("iTunes download failed: %s" % e)
        exe = None
    if exe:
        # A) extract the installer and install only the USB driver packages (no iTunes)
        say("Installing Apple USB driver ...")
        run([exe, "/extract"], timeout=300, cwd=tmp)
        msis = {}
        for root, _dirs, files in os.walk(tmp):
            for name in files:
                if name.lower().endswith(".msi"):
                    msis[name.lower()] = os.path.join(root, name)
        for key in ("appleapplicationsupport64.msi", "applemobiledevicesupport64.msi"):
            if key in msis:
                run(["msiexec", "/i", msis[key], "/qn", "/norestart"], timeout=600)
        if _wait_usb(25):
            return True
        # B) full silent iTunes setup (installs the same driver)
        say("Installing Apple USB driver (full package) ...")
        run([exe, "REBOOT=ReallySuppress", "/qn"], timeout=900, cwd=tmp)
        if _wait_usb(40):
            return True
    # C) Microsoft Store "Apple Devices" through winget
    say("Installing Apple Devices ...")
    run(["winget", "install", "-e", "--id", "9NP83LWLPZ9K", "--source", "msstore", "--silent",
         "--accept-package-agreements", "--accept-source-agreements"], timeout=900)
    return _wait_usb(40)


STEPS = [("camera", "Installing virtual camera ...", install_camera),
         ("mic", "Installing virtual microphone ...", install_vbcable),
         ("usb", "Installing Apple USB driver ...", install_apple_usb)]


def run_setup(say, only=None):
    """Installs what is missing (blocking). Needs administrator rights. Returns {name: ok}."""
    result = {}
    log("=== setup start, only=%s admin=%s" % (only, is_admin()))
    for name, text, fn in STEPS:
        if only and name not in only:
            continue
        if CHECKS[name]():
            result[name] = True
            continue
        say(text)
        try:
            result[name] = bool(fn(say))
        except Exception as e:                   # noqa: BLE001
            log("%s failed: %s" % (name, e))
            result[name] = False
        log("%s -> %s" % (name, result[name]))
    return result


# =============================================================================================
#  elevated child process:  USC-CAM.exe --setup --status <file> --only camera,mic,usb
# =============================================================================================
def run_setup_cli(argv):
    status = None
    only = None
    for i, a in enumerate(argv):
        if a == "--status" and i + 1 < len(argv):
            status = argv[i + 1]
        if a == "--only" and i + 1 < len(argv):
            only = [x for x in argv[i + 1].split(",") if x]

    def say(text):
        log("status: " + text)
        if status:
            try:
                with open(status, "w", encoding="utf-8") as f:
                    f.write(text)
            except OSError:
                pass

    run_setup(say, only)
    say("done")
    return 0


class _SEI(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("fMask", ctypes.c_ulong), ("hwnd", ctypes.c_void_p),
                ("lpVerb", ctypes.c_wchar_p), ("lpFile", ctypes.c_wchar_p),
                ("lpParameters", ctypes.c_wchar_p), ("lpDirectory", ctypes.c_wchar_p),
                ("nShow", ctypes.c_int), ("hInstApp", ctypes.c_void_p), ("lpIDList", ctypes.c_void_p),
                ("lpClass", ctypes.c_wchar_p), ("hkeyClass", ctypes.c_void_p),
                ("dwHotKey", ctypes.c_ulong), ("hIcon", ctypes.c_void_p), ("hProcess", ctypes.c_void_p)]


def run_elevated(say, only):
    """Starts this program again with administrator rights (one Windows prompt) in setup mode and
    waits for it, relaying its progress text. Returns False if the prompt was declined."""
    status = os.path.join(tempfile.gettempdir(), "usc-cam-setup-status.txt")
    try:
        os.remove(status)
    except OSError:
        pass
    if getattr(sys, "frozen", False):
        exe = sys.executable
        params = ""
    else:
        exe = sys.executable
        params = '"%s" ' % os.path.abspath(sys.argv[0])
    params += '--setup --status "%s" --only %s' % (status, ",".join(only))
    sei = _SEI()
    sei.cbSize = ctypes.sizeof(_SEI)
    sei.fMask = 0x40                              # SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = exe
    sei.lpParameters = params
    sei.nShow = 0                                 # hidden
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)) or not sei.hProcess:
        log("elevation declined or failed")
        return False
    k32 = ctypes.windll.kernel32
    k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    last = ""
    deadline = time.time() + 40 * 60
    while time.time() < deadline:
        if k32.WaitForSingleObject(sei.hProcess, 400) == 0:
            break
        try:
            with open(status, encoding="utf-8") as f:
                text = f.read().strip()
            if text and text != last:
                last = text
                say(text)
        except OSError:
            pass
    k32.CloseHandle(sei.hProcess)
    return True
