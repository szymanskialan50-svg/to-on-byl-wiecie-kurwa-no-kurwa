import gc
import multiprocessing as mp
import os
import socket
import struct
import sys
import threading
import time

import cv2
import numpy as np
from PyQt5.QtCore import Qt, QRectF, QSettings, QThread, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPixmap
from PyQt5.QtWidgets import (QApplication, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
                             QLineEdit, QMainWindow, QMessageBox, QPushButton, QVBoxLayout, QWidget)

import audio as audiomod
import autosetup
import guard
import procs
import usbmux
from procs import (H_AGEUS, H_CODEC, H_DECODED, H_DECUS, H_KEYREQ, H_OUTFPS, H_QUIT, H_ROT, H_SENDUS,
                   H_SENT, H_TH, H_TW, H_VSTATE, H_BLUR, H_BLURSTATE, H_FACES)

PORT = 9999
CAM_NAME = procs.CAM_NAME                # name of the virtual webcam (installed automatically, see autosetup.py)
MAX_W, MAX_H = 3840, 2160
PREVIEW_INTERVAL = 1 / 15                # window preview refresh (the preview costs CPU, keep it low)
LIVE_INTERVAL = 1 / 60                   # live window refresh
OUT_FPS = [("Output 120 fps", 120), ("Output 60 fps", 60), ("Output 30 fps", 30)]
PLACEHOLDER_SIZE = (1920, 1080)
SIZE_SIMPLE = 600                        # window height: simple view
SIZE_ADVANCED = 850                      # window height: with the advanced settings
PREVIEW_SIZE = (464, 261)                # shown 1:1 in the window (no pixelated upscaling)
DEFAULT_RES_INDEX = 3                    # 1920x1080 - stable size, same as the placeholder
RESOLUTIONS = [("Native (as sent by iPhone)", None),
               ("3840x2160  (4K UHD)", (3840, 2160)),
               ("2560x1440  (QHD)", (2560, 1440)),
               ("1920x1080  (FHD)", (1920, 1080)),
               ("1280x720  (HD)", (1280, 720))]
CONN_MODES = ["Auto (USB / Wi-Fi)", "USB cable only", "Wi-Fi (type IP)"]


def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def make_placeholder(icon_path):
    """Renders the 'Waiting for camera' screen (app icon + text) as a BGR numpy image."""
    w, h = PLACEHOLDER_SIZE
    img = QImage(w, h, QImage.Format_RGB888)
    img.fill(QColor("#0A0A0A"))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    p.setRenderHint(QPainter.TextAntialiasing)
    size = 340
    if os.path.exists(icon_path):
        pm = QIcon(icon_path).pixmap(512, 512)
        if not pm.isNull():
            pm = pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            p.drawPixmap((w - pm.width()) // 2, h // 2 - pm.height() // 2 - 90, pm)
    p.setPen(QColor("#FFFFFF"))
    f = QFont("Segoe UI", 44)
    f.setBold(True)
    f.setLetterSpacing(QFont.AbsoluteSpacing, 2)
    p.setFont(f)
    p.drawText(QRectF(0, h // 2 + 120, w, 90), Qt.AlignCenter, "Waiting for camera")
    p.setPen(QColor("#8A8A8A"))
    p.setFont(QFont("Segoe UI", 20))
    p.drawText(QRectF(0, h // 2 + 210, w, 50), Qt.AlignCenter,
               "Connect your iPhone with USB and open USC-CAM")
    p.end()
    ptr = img.constBits()
    ptr.setsize(img.byteCount())
    arr = np.frombuffer(ptr, np.uint8).reshape(h, img.bytesPerLine())[:, :w * 3]
    return cv2.cvtColor(np.ascontiguousarray(arr).reshape(h, w, 3), cv2.COLOR_RGB2BGR)




class Discovery:
    """Finds the iPhone on the local network (Bonjour / mDNS) - no IP typing needed."""

    def __init__(self):
        self.ip = None
        self.zc = None
        try:
            from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
            outer = self

            class Listener(ServiceListener):
                def add_service(self, zc, type_, name):
                    outer._found(zc, type_, name)

                def update_service(self, zc, type_, name):
                    outer._found(zc, type_, name)

                def remove_service(self, zc, type_, name):
                    outer.ip = None

            self.zc = Zeroconf()
            self.browser = ServiceBrowser(self.zc, "_usccam._tcp.local.", Listener())
        except Exception:                        # noqa: BLE001 - discovery is optional
            self.zc = None

    def _found(self, zc, type_, name):
        try:
            info = zc.get_service_info(type_, name, timeout=2000)
            if info:
                addrs = [a for a in info.parsed_addresses() if ":" not in a]
                if addrs:
                    self.ip = addrs[0]
        except Exception:                        # noqa: BLE001
            pass

    def close(self):
        try:
            if self.zc:
                self.zc.close()
        except Exception:                        # noqa: BLE001
            pass


class FrameReceiver(threading.Thread):
    """Drains the socket as fast as possible. Packet: [4B len][1B kind][payload]
    kind 0 = JPEG, 1 = H.264 (-> decoder process), 2 = microphone PCM (-> virtual cable).
    Every video frame is acknowledged immediately so the phone never stalls on our decode speed."""

    def __init__(self, sock, dec_conn, bus, audio):
        super().__init__(daemon=True)
        self.sock = sock
        self.dec_conn = dec_conn
        self.bus = bus
        self.audio = audio
        self.send_lock = threading.Lock()
        self.halt = False
        self.bytes = 0

    def send(self, data):
        try:
            with self.send_lock:
                self.sock.sendall(data)
        except OSError:
            pass

    def run(self):
        hdr = self.bus.hdr
        try:
            while not self.halt:
                size = struct.unpack(">I", usbmux.recv_exact(self.sock, 4))[0]
                if size < 2 or size > 64 * 1024 * 1024:
                    raise ConnectionError("bad frame header")
                kind = usbmux.recv_exact(self.sock, 1)[0]
                n = size - 1
                self.bytes += size + 4
                if kind in (0, 1):
                    msg = bytearray(9 + n)
                    usbmux.recv_into(self.sock, memoryview(msg)[9:])
                    msg[0] = 74 if kind == 0 else 72           # b"J" / b"H"
                    struct.pack_into("<d", msg, 1, time.time())
                    self.send(b"\x01")                         # ack: phone may send the next frame
                    self.dec_conn.send_bytes(msg)              # blocks if the decoder is behind = backpressure
                    if hdr[H_KEYREQ]:
                        hdr[H_KEYREQ] = 0
                        self.send(b"\x02")                     # decoder lost sync: ask for a key frame
                else:
                    buf = usbmux.recv_exact(self.sock, n)
                    if kind == 2 and n > 4:
                        rate = struct.unpack(">I", buf[:4])[0]
                        self.audio.mic_write(rate, bytes(buf[4:]))
        except Exception:                        # noqa: BLE001 - any error ends the stream
            pass
        finally:
            self.halt = True


def precise_sleep(seconds):
    if seconds > 0.0005:
        time.sleep(seconds)


def to_qimage(frame):
    """Exactly the frame that goes to the virtual camera, scaled down smoothly for the window."""
    small = cv2.resize(frame, PREVIEW_SIZE, interpolation=cv2.INTER_AREA)
    rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
    return QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format_RGB888).copy()


class StreamWorker(QThread):
    status = pyqtSignal(str, str)      # text, state: wait | live | error
    stats = pyqtSignal(str)
    preview = pyqtSignal(QImage)
    live = pyqtSignal(object)          # full-size frame for the live window
    note = pyqtSignal(str)             # audio / driver messages
    blurnote = pyqtSignal(str)         # warning when face blur cannot work

    def __init__(self, placeholder):
        super().__init__()
        self._running = True
        self.target = None             # None = native, or (w, h)
        self.rotate180 = False         # the phone already sends the picture upright (it follows how you hold it)
        self.blur_faces = False        # blur every detected face (done in the decoder process, last step)
        self.mode = 0                  # 0 auto (USB, then Wi-Fi), 1 USB only, 2 Wi-Fi with typed IP
        self.wifi_ip = ""
        self.out_fps = 120             # rate the virtual camera is fed at (set the same fps in OBS)
        self.show_live = False         # also push frames to the separate live window
        self.mic_on = False
        self.placeholder = placeholder
        self.bus = None
        self.dec_conn = None
        self.procs = []
        self.rx = None
        self.discovery = None
        self.audio = audiomod.AudioBridge()
        self._ph_cache = {}
        self._ph_preview = None

    def stop(self):
        self._running = False

    # -- worker processes ---------------------------------------------------------------
    def _start_procs(self):
        ctx = mp.get_context("spawn")
        self.bus = procs.SharedBus(MAX_W, MAX_H)
        recv, send = ctx.Pipe(duplex=False)
        self.dec_conn = send
        self.procs = [
            ctx.Process(target=procs.decoder_main, args=(self.bus.name, recv, MAX_W, MAX_H), daemon=True),
            ctx.Process(target=procs.vcam_main, args=(self.bus.name, MAX_W, MAX_H), daemon=True),
        ]
        for p in self.procs:
            p.start()
        self.bus.hdr[H_ROT] = int(self.rotate180)

    def _stop_procs(self):
        if self.bus is not None:
            self.bus.hdr[H_QUIT] = 1
        for p in self.procs:
            p.join(1.5)
            if p.is_alive():
                p.terminate()
        if self.dec_conn is not None:
            try:
                self.dec_conn.close()
            except Exception:                    # noqa: BLE001
                pass
        if self.bus is not None:
            self.bus.close()

    def _sync_settings(self):
        hdr = self.bus.hdr
        hdr[H_OUTFPS] = self.out_fps
        hdr[H_ROT] = int(self.rotate180)
        hdr[H_BLUR] = int(self.blur_faces)
        t = self.target
        hdr[H_TW], hdr[H_TH] = (t if t else (0, 0))

    # -- 'Waiting for camera' screen shown in Zoom/OBS/etc. while the phone is not streaming
    def _placeholder_for(self, size):
        if size not in self._ph_cache:
            self._ph_cache[size] = (self.placeholder if size == PLACEHOLDER_SIZE else
                                    cv2.resize(self.placeholder, size, interpolation=cv2.INTER_AREA))
        return self._ph_cache[size]

    def _wait(self, seconds):
        """Sleeps while feeding the placeholder to the virtual camera."""
        end = time.time() + seconds
        size = self.target or PLACEHOLDER_SIZE
        while self._running and time.time() < end:
            self._sync_settings()
            self.bus.publish(self._placeholder_for(size))
            if self._ph_preview is None:
                self._ph_preview = to_qimage(self.placeholder)
            self.preview.emit(self._ph_preview)
            time.sleep(0.25)

    # -- audio ---------------------------------------------------------------------------
    def apply_audio(self):
        rx = self.rx
        if rx is None or rx.halt:
            return
        rx.send(b"\x04" + (b"\x01" if self.mic_on else b"\x00"))
        if not self.mic_on:
            self.note.emit("")
        elif not self.audio.available():
            self.note.emit("Microphone is available on Windows only")

    def mic_note(self):
        """One line under the controls that says what the microphone is doing (called every second)."""
        if not self.mic_on or not self.audio.available():
            return
        kb, opened, err = self.audio.mic_status()
        if kb < 1:
            self.note.emit("Microphone ON, but no sound arrives from the iPhone - allow the microphone "
                           "for USC-CAM in iPhone Settings")
        elif not opened:
            self.note.emit("Sound arrives, but the virtual cable is not ready (%s) - restart the app" % (err or "opening"))
        else:
            self.note.emit("Microphone OK - pick 'CABLE Output (VB-Audio Virtual Cable)' as microphone in your app")

    # -- main loop: find phone -> connect -> stream -> repeat -------------------------------
    def ensure_components(self):
        """Installs the virtual camera, virtual microphone and Apple USB driver by itself.
        Administrator rights: started as admin -> installs right here; otherwise the app
        restarts itself elevated for the installation (ONE Windows prompt, only when needed)."""
        if sys.platform != "win32":
            return
        todo = autosetup.pending()
        if not todo:
            return

        def say(text):
            self.status.emit(text, "wait")

        if autosetup.is_admin():
            autosetup.run_setup(say, todo)
        else:
            say("First start: installing camera, microphone and USB driver - accept the Windows prompt")
            if not autosetup.run_elevated(say, todo):
                self.status.emit("Installation needs administrator rights - restart the app and accept the prompt",
                                 "error")
                time.sleep(3)
        still = autosetup.missing()
        autosetup.remember_failures(still)
        self.audio.refresh_devices()
        if "camera" in still:
            self.status.emit("Camera driver could not be installed - see %s" % autosetup.LOG, "error")
            time.sleep(4)
        elif still:
            self.note.emit("Not installed yet (%s) - needs internet, will retry later" % ", ".join(still))

    def run(self):
        try:
            self.ensure_components()
            self._start_procs()
            self.discovery = Discovery()
            self._loop()
        finally:
            self.audio.close()
            if self.discovery:
                self.discovery.close()
            self._stop_procs()

    def _connect(self):
        """Returns (socket or None, waiting-message)."""
        mode = self.mode
        if mode == 2:
            ip = self.wifi_ip.strip()
            if not ip:
                return None, "Wi-Fi mode: type the iPhone IP shown in the app"
            try:
                return usbmux.connect_tcp(ip, PORT), ""
            except OSError:
                return None, "Waiting for iPhone on Wi-Fi (%s) - open USC-CAM, same network" % ip
        msg = "Waiting for iPhone - connect the USB cable or open USC-CAM on the same Wi-Fi"
        usb_missing = False
        try:
            devices = usbmux.list_usb_devices()
        except OSError:
            devices = []
            usb_missing = True
        if devices:
            try:
                return usbmux.connect(devices[0], PORT), ""
            except (usbmux.UsbmuxError, OSError):
                msg = "Waiting for camera - open USC-CAM on your iPhone (tap Trust)"
        elif usb_missing and mode == 1:
            msg = "Apple USB driver is still installing - or use Wi-Fi (Auto mode)"
        if mode == 0 and self.discovery is not None and self.discovery.ip:
            try:
                return usbmux.connect_tcp(self.discovery.ip, PORT), ""
            except OSError:
                pass
        return None, msg

    def _loop(self):
        last_fix = time.time()
        while self._running:
            if sys.platform == "win32" and self.bus.hdr[H_VSTATE] == 2:
                if autosetup.camera_installed():
                    self.status.emit("Camera driver is installed but cannot start - restart the app or Windows", "error")
                else:
                    self.status.emit("Camera driver missing - installing it", "error")
                if time.time() - last_fix > 60:                  # try the automatic installation again
                    last_fix = time.time()
                    self.ensure_components()
                self._wait(2)
                continue
            sock, msg = self._connect()
            if sock is None:
                self.status.emit(msg, "wait")
                self._wait(1.0)
                continue
            try:
                self._stream(sock)
            except Exception:                    # noqa: BLE001
                pass
            finally:
                sock.close()
            self.stats.emit("")
            self.note.emit("")
            self.status.emit("Waiting for camera - stream stopped", "wait")
            self._wait(1)

    def _stream(self, sock):
        hdr = self.bus.hdr
        self.dec_conn.send_bytes(b"R")                       # new stream: reset the decoder
        self.audio.mic_reset()
        rx = FrameReceiver(sock, self.dec_conn, self.bus, self.audio)
        self.rx = rx
        rx.start()
        self.apply_audio()
        is_live = False
        last_seq = 0
        tick = last_prev = last_live = last_beat = time.time()
        last_bytes = 0
        d0, s0 = int(hdr[H_DECODED]), int(hdr[H_SENT])
        try:
            while self._running and not rx.halt:
                time.sleep(0.004)
                self._sync_settings()
                now = time.time()
                seq, frame = self.bus.latest()
                if frame is not None and seq != last_seq:
                    last_seq = seq
                    if int(hdr[H_DECODED]) > d0 and not is_live:
                        is_live = True
                        self.status.emit("LIVE  -  select '%s' in your app" % CAM_NAME, "live")
                    if now - last_prev >= PREVIEW_INTERVAL:
                        last_prev = now
                        self.preview.emit(to_qimage(frame))
                    if self.show_live and now - last_live >= LIVE_INTERVAL:
                        last_live = now
                        self.live.emit(frame.copy())
                if now - last_beat >= 2:                         # keep the phone's microphone state in sync
                    last_beat = now
                    rx.send(b"\x04" + (b"\x01" if self.mic_on else b"\x00"))
                if now - tick >= 1:
                    self.mic_note()
                    dt = now - tick
                    d1, s1 = int(hdr[H_DECODED]), int(hdr[H_SENT])
                    mbit = (rx.bytes - last_bytes) * 8 / 1e6 / dt
                    last_bytes = rx.bytes
                    codec = {1: "MJPEG", 2: "H.264"}.get(int(hdr[H_CODEC]), "?")
                    text = ("%dx%d  -  in %d fps / out %d fps  -  %s  -  %.0f Mbit/s\n"
                            "decode %.1f ms  -  vcam send %.1f ms  -  frame age %.0f ms" % (
                                int(hdr[2]), int(hdr[3]), round((d1 - d0) / dt), round((s1 - s0) / dt),
                                codec, mbit, hdr[H_DECUS] / 1000, hdr[H_SENDUS] / 1000,
                                hdr[H_AGEUS] / 1000))
                    if self.blur_faces:
                        text += "  -  blurring %d face(s)%s" % (int(hdr[H_FACES]),
                                                       "" if int(hdr[H_BLURSTATE]) == 3 else "  [basic detector]")
                    self.blurnote.emit("Face blur is NOT working (detector file missing) - faces are visible!"
                                       if self.blur_faces and int(hdr[H_BLURSTATE]) == 2 else "")
                    self.stats.emit(text)
                    d0, s0, tick = d1, s1, now
        finally:
            rx.halt = True
            self.rx = None


class LiveWindow(QWidget):
    """Full-quality picture in its own window, bypassing the virtual camera.
    In OBS add it with 'Window Capture' (or Game Capture) - low latency, no driver in between."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("USC-CAM Live")
        self.resize(960, 540)
        self.setStyleSheet("background-color: #000000;")
        self.label = QLabel(self)
        self.label.setAlignment(Qt.AlignCenter)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.label)

    def show_frame(self, frame):
        h, w = frame.shape[:2]
        img = QImage(frame.data, w, h, frame.strides[0], QImage.Format_BGR888)
        pm = QPixmap.fromImage(img)
        self.label.setPixmap(pm.scaled(self.label.size(), Qt.KeepAspectRatio, Qt.FastTransformation))


class MainWindow(QMainWindow):
    COLORS = {"wait": "#E8A33D", "live": "#3DDC84", "error": "#FF5A5A"}
    BOX = ("QComboBox, QLineEdit { background-color: #1A1A1A; color: #FFFFFF; border: 1px solid #333333;"
           " border-radius: 8px; padding: 9px 12px; font-size: 13px; font-weight: 600; }"
           " QComboBox::drop-down { border: none; }"
           " QComboBox QAbstractItemView { background-color: #1A1A1A; color: #FFFFFF;"
           " selection-background-color: #333333; border: 1px solid #333333; }"
           " QPushButton { background-color: #FFFFFF; color: #000000; border-radius: 8px; padding: 9px 12px;"
           " font-size: 13px; font-weight: 700; }"
           " QPushButton:disabled { background-color: #555555; color: #222222; }")

    def __init__(self):
        super().__init__()
        self.setWindowTitle(CAM_NAME)
        self.setFixedSize(560, SIZE_SIMPLE)
        icon_file = resource_path("icon.ico")
        if os.path.exists(icon_file):
            self.setWindowIcon(QIcon(icon_file))
        self.setStyleSheet("""
            QMainWindow { background-color: #0A0A0A; }
            QLabel { color: #FFFFFF; font-family: 'Segoe UI', sans-serif; }
            QLabel#Title { font-size: 24px; font-weight: bold; letter-spacing: 3px; }
            QLabel#Sub { font-size: 11px; color: #888888; font-weight: 600; letter-spacing: 3px; }
            QLabel#Stats { font-size: 12px; color: #BBBBBB; }
            QLabel#Note { font-size: 12px; color: #E8A33D; }
            QFrame#Pill { background-color: #1A1A1A; border-radius: 18px; }
            QLabel#Preview { background-color: #000000; border: 1px solid #262626; border-radius: 10px; }
        """ + self.BOX)

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)

        title = QLabel(CAM_NAME)
        title.setObjectName("Title")
        title.setAlignment(Qt.AlignCenter)
        sub = QLabel("USB + WI-FI WEBCAM AND MICROPHONE")
        sub.setObjectName("Sub")
        sub.setAlignment(Qt.AlignCenter)
        root.addWidget(title)
        root.addWidget(sub)

        self.preview = QLabel("Waiting for camera")
        self.preview.setObjectName("Preview")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setFixedHeight(270)
        root.addWidget(self.preview)

        pill = QFrame()
        pill.setObjectName("Pill")
        pl = QHBoxLayout(pill)
        pl.setContentsMargins(16, 10, 16, 10)
        self.dot = QLabel("●")
        self.status = QLabel("Starting...")
        self.status.setStyleSheet("font-size: 13px; font-weight: 600;")
        pl.addWidget(self.dot)
        pl.addWidget(self.status, 1)
        root.addWidget(pill)

        self.settings = QSettings("USC-CAM", "USC-CAM")

        # ---- simple part: the only two switches most people ever need ----------------------
        self.blurbox = QComboBox()
        self.blurbox.addItems(["Blur faces: off", "Blur faces: on"])
        self.blurbox.setCurrentIndex(self.settings.value("blur", 0, type=int))
        self.micbox = QComboBox()
        self.micbox.addItems(["Phone mic -> PC: off", "Phone mic -> PC: on"])
        self.micbox.setCurrentIndex(self.settings.value("mic", 0, type=int))
        simple = QGridLayout()
        simple.setHorizontalSpacing(10)
        simple.addWidget(self.blurbox, 0, 0)
        simple.addWidget(self.micbox, 0, 1)
        root.addLayout(simple)

        self.adv_btn = QPushButton("Advanced settings  \u25BE")
        self.adv_btn.setStyleSheet("QPushButton { background-color: #1A1A1A; color: #BBBBBB;"
                                   " border: 1px solid #333333; }")
        root.addWidget(self.adv_btn)

        # ---- advanced part (hidden until asked for) -----------------------------------------
        self.adv = QWidget()
        al = QVBoxLayout(self.adv)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(10)
        self.stats = QLabel("")
        self.stats.setObjectName("Stats")
        self.stats.setAlignment(Qt.AlignCenter)
        self.stats.setMinimumHeight(34)
        al.addWidget(self.stats)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)

        self.res = QComboBox()
        for label, _ in RESOLUTIONS:
            self.res.addItem(label)
        idx = self.settings.value("res", DEFAULT_RES_INDEX, type=int)
        self.res.setCurrentIndex(idx if 0 <= idx < len(RESOLUTIONS) else DEFAULT_RES_INDEX)
        self.outfps = QComboBox()
        for label, _ in OUT_FPS:
            self.outfps.addItem(label)
        oi = self.settings.value("outfps", 0, type=int)
        self.outfps.setCurrentIndex(oi if 0 <= oi < len(OUT_FPS) else 0)

        self.conn = QComboBox()
        self.conn.addItems(CONN_MODES)
        ci = self.settings.value("conn", 0, type=int)
        self.conn.setCurrentIndex(ci if 0 <= ci < len(CONN_MODES) else 0)
        self.ip = QLineEdit(self.settings.value("ip", "", type=str))
        self.ip.setPlaceholderText("iPhone IP, e.g. 192.168.0.13")

        self.livebox = QComboBox()
        self.livebox.addItems(["Live window: off", "Live window: on"])
        self.flipbox = QComboBox()
        self.flipbox.addItems(["Picture: normal", "Picture: flipped 180\u00B0"])
        self.flipbox.setCurrentIndex(self.settings.value("flip", 0, type=int))

        grid.addWidget(self.res, 0, 0)
        grid.addWidget(self.outfps, 0, 1)
        grid.addWidget(self.conn, 1, 0)
        grid.addWidget(self.ip, 1, 1)
        grid.addWidget(self.flipbox, 2, 0)
        grid.addWidget(self.livebox, 2, 1)
        al.addLayout(grid)
        wa = QLabel("WhatsApp: the Windows app (Microsoft Store) cannot see this kind of camera - a Windows "
                    "limit. Use WhatsApp Web in Chrome or Edge instead (web.whatsapp.com).")
        wa.setObjectName("Note")
        wa.setWordWrap(True)
        wa.setAlignment(Qt.AlignCenter)
        al.addWidget(wa)
        self.adv.setVisible(False)
        root.addWidget(self.adv)

        self.note = QLabel("")
        self.note.setObjectName("Note")
        self.note.setWordWrap(True)
        self.note.setAlignment(Qt.AlignCenter)
        root.addWidget(self.note)
        self.blurnote = QLabel("")
        self.blurnote.setObjectName("Note")
        self.blurnote.setStyleSheet("color: #FF5A5A; font-size: 12px; font-weight: 600;")
        self.blurnote.setWordWrap(True)
        self.blurnote.setAlignment(Qt.AlignCenter)
        root.addWidget(self.blurnote)
        root.addStretch(1)
        self.setCentralWidget(central)

        self.livewin = LiveWindow()
        self.set_status("Starting...", "wait")
        self.worker = StreamWorker(make_placeholder(resource_path("icon.ico")))
        self.worker.target = RESOLUTIONS[self.res.currentIndex()][1]
        self.on_conn()
        self.on_outfps()
        self.on_audio()
        self.on_blur()
        self.on_flip()
        self.worker.status.connect(self.set_status)
        self.worker.stats.connect(self.stats.setText)
        self.worker.preview.connect(self.set_preview)
        self.worker.live.connect(self.livewin.show_frame)
        self.worker.note.connect(self.note.setText)
        self.worker.blurnote.connect(self.blurnote.setText)

        self.res.currentIndexChanged.connect(self.on_res)
        self.outfps.currentIndexChanged.connect(self.on_outfps)
        self.conn.currentIndexChanged.connect(self.on_conn)
        self.ip.editingFinished.connect(self.on_conn)
        self.livebox.currentIndexChanged.connect(self.on_live)
        self.micbox.currentIndexChanged.connect(self.on_audio)
        self.blurbox.currentIndexChanged.connect(self.on_blur)
        self.flipbox.currentIndexChanged.connect(self.on_flip)
        self.adv_btn.clicked.connect(self.on_adv)
        self.worker.start()

    def on_res(self, i):
        self.settings.setValue("res", i)
        self.worker.target = RESOLUTIONS[i][1]

    def on_conn(self, *_):
        self.settings.setValue("conn", self.conn.currentIndex())
        self.settings.setValue("ip", self.ip.text().strip())
        self.ip.setEnabled(self.conn.currentIndex() == 2)
        self.worker.mode = self.conn.currentIndex()
        self.worker.wifi_ip = self.ip.text().strip()

    def on_outfps(self, *_):
        i = self.outfps.currentIndex()
        self.settings.setValue("outfps", i)
        self.worker.out_fps = OUT_FPS[i][1]

    def on_blur(self, *_):
        self.settings.setValue("blur", self.blurbox.currentIndex())
        self.worker.blur_faces = bool(self.blurbox.currentIndex())
        if not self.worker.blur_faces:
            self.blurnote.setText("")

    def on_flip(self, *_):
        self.settings.setValue("flip", self.flipbox.currentIndex())
        self.worker.rotate180 = bool(self.flipbox.currentIndex())

    def on_adv(self):
        show = self.adv.isHidden()
        self.adv.setVisible(show)
        self.adv_btn.setText("Hide advanced settings  \u25B4" if show else "Advanced settings  \u25BE")
        self.setFixedSize(560, SIZE_ADVANCED if show else SIZE_SIMPLE)

    def on_live(self, i):
        self.worker.show_live = bool(i)
        self.livewin.setVisible(bool(i))

    def on_audio(self, *_):
        self.settings.setValue("mic", self.micbox.currentIndex())
        self.worker.mic_on = bool(self.micbox.currentIndex())
        self.worker.apply_audio()

    def set_status(self, text, state):
        self.status.setText(text)
        self.dot.setStyleSheet("color: %s; font-size: 14px;" % self.COLORS.get(state, "#888"))

    def set_preview(self, img):
        self.preview.setPixmap(QPixmap.fromImage(img))

    def closeEvent(self, event):
        self.livewin.close()
        self.worker.stop()
        self.worker.wait(4000)
        event.accept()


def tune_process():
    """Lower scheduling latency: 1 ms Windows timer, higher priority, faster thread hand-off."""
    sys.setswitchinterval(0.001)
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)
            k32 = ctypes.windll.kernel32
            k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00008000)   # ABOVE_NORMAL_PRIORITY_CLASS
        except Exception:                        # noqa: BLE001
            pass


if __name__ == "__main__":
    mp.freeze_support()                          # required for the frozen .exe (worker processes)
    if "--setup" in sys.argv:                    # elevated helper: installs drivers, shows nothing
        sys.exit(autosetup.run_setup_cli(sys.argv))
    gc.collect()
    gc.freeze()                                  # fewer / shorter garbage-collection pauses = fewer micro-stutters
    gc.set_threshold(50000, 20, 20)
    tune_process()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    instance_lock = guard.acquire()              # keep this reference alive: it is the "already running" lock
    if instance_lock is None:
        QMessageBox.warning(None, CAM_NAME, "USC-CAM is already running.\n\nTwo copies would both feed the virtual "
                                            "camera and the picture would flicker. Close the other one first.")
        sys.exit(0)
    guard.kill_stale()                           # leftovers of an earlier run would flicker the picture too
    w = MainWindow()
    w.show()
    sys.exit(app.exec_())
