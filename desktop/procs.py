"""Heavy work lives in separate PROCESSES (each with its own Python GIL):

    GUI process:  socket receive + acks + UI                (light)
    decoder proc: H.264 / JPEG decode, rotate, resize       (CPU heavy, native code)
    vcam proc:    feeds the DirectShow virtual camera       (holds its GIL while copying frames)

Frames travel decoder -> vcam / GUI through one shared-memory "bus" (3 slots, newest wins).
Running everything as threads of one process made the decoder wait for the virtual-camera
copy on every GIL hand-over, which capped the whole pipeline at a few fps.
"""
import os
import struct
import sys
import threading
import time
from multiprocessing import shared_memory

import numpy as np

# ---- header slots (int64) ----------------------------------------------------------------
H_SEQ, H_SLOT, H_W, H_H, H_OUTFPS, H_QUIT, H_VSTATE, H_SENDUS, H_SENT, H_KEYREQ, \
    H_DECUS, H_CODEC, H_DECODED, H_AGEUS, H_TW, H_TH, H_ROT, H_BLUR, H_BLURSTATE, H_FACES = range(20)
HEADER_INTS = 32
HEADER_BYTES = HEADER_INTS * 8
NSLOT = 3
CAM_NAME = "HD Camera iPhone"


class SharedBus:
    def __init__(self, max_w=3840, max_h=2160, name=None):
        self.slot_bytes = max_w * max_h * 3
        size = HEADER_BYTES + NSLOT * self.slot_bytes
        if name is None:
            self.shm = shared_memory.SharedMemory(create=True, size=size)
            self.owner = True
        else:
            self.shm = shared_memory.SharedMemory(name=name)
            self.owner = False
        self.hdr = np.ndarray((HEADER_INTS,), np.int64, buffer=self.shm.buf)
        if self.owner:
            self.hdr[:] = 0
            self.hdr[H_OUTFPS] = 30
            self.hdr[H_ROT] = 0              # the phone already sends the picture upright (auto-rotate)

    @property
    def name(self):
        return self.shm.name

    def _view(self, slot, w, h):
        return np.ndarray((h, w, 3), np.uint8, buffer=self.shm.buf,
                          offset=HEADER_BYTES + slot * self.slot_bytes)

    def publish(self, frame, age_us=0):
        h, w = frame.shape[:2]
        if w * h * 3 > self.slot_bytes:
            return
        slot = (int(self.hdr[H_SLOT]) + 1) % NSLOT
        np.copyto(self._view(slot, w, h), frame)
        hdr = self.hdr
        hdr[H_W], hdr[H_H], hdr[H_AGEUS] = w, h, age_us
        hdr[H_SLOT] = slot
        hdr[H_SEQ] += 1

    def latest(self):
        """(seq, frame view or None). The view stays valid for about two more frames."""
        seq = int(self.hdr[H_SEQ])
        w, h = int(self.hdr[H_W]), int(self.hdr[H_H])
        if seq == 0 or w <= 0 or h <= 0:
            return seq, None
        return seq, self._view(int(self.hdr[H_SLOT]), w, h)

    def close(self):
        try:
            self.hdr = None
            self.shm.close()
            if self.owner:
                self.shm.unlink()
        except Exception:                        # noqa: BLE001
            pass


def parent_gone():
    """True when the app that started this helper process has died (crash, killed in Task Manager).
    Without this the helper would live on and keep writing its last picture into the virtual camera."""
    try:
        import multiprocessing as mp
        par = mp.parent_process()
        return par is not None and not par.is_alive()
    except Exception:                            # noqa: BLE001
        return False


def _timer_1ms():
    import gc
    gc.collect()
    gc.freeze()
    gc.set_threshold(50000, 20, 20)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:                        # noqa: BLE001
            pass


# ---- fitting the picture into the output size ---------------------------------------------
# The phone now sends the picture upright whichever way it is held, so a portrait picture is
# 1080x1920 and a landscape one 1920x1080. The virtual camera keeps ONE fixed size: the picture is
# scaled to fit and centred (black bars at the sides for portrait) - never stretched.
def fit_size(sw, sh, target):
    if not target:
        return sw, sh
    tw, th = target
    if sw * th == sh * tw:
        return tw, th
    s = min(tw / sw, th / sh)
    return max(2, int(sw * s) & ~1), max(2, int(sh * s) & ~1)


def pad_to(img, target):
    if not target:
        return img
    tw, th = target
    h, w = img.shape[:2]
    if (w, h) == (tw, th):
        return img
    canvas = np.zeros((th, tw, 3), np.uint8)
    x, y = (tw - w) // 2, (th - h) // 2
    canvas[y:y + h, x:x + w] = img
    return canvas


# ---- face blur ----------------------------------------------------------------------------
class FaceBlur:
    """Finds faces (OpenCV Haar cascade, runs in its own thread) and blurs them in every frame.

    Detection is done ~20x per second on a small copy of the picture; between two detections the
    last known face positions are used, and a face that is not found for a moment stays blurred
    for HOLD seconds so the blur does not flicker when the head turns."""
    HOLD = 1.0
    INTERVAL = 0.025
    DETECT_W = 640
    YUNET_FILE = "yunet.onnx"

    def __init__(self):
        import cv2
        self.cv2 = cv2
        self.kind = ""                           # "yunet" (best) | "haar" (fallback) | "" = nothing found
        self.yunet = self._load_yunet()
        self.haar = None
        if self.yunet is not None:
            self.kind = "yunet"
        else:
            self.haar = self._load_haar()
            if self.haar is not None:
                self.kind = "haar"
        self.det = self.yunet or self.haar       # None -> blur impossible (the GUI shows a red warning)
        self.lock = threading.Lock()
        self.boxes = []                          # [cx, cy, w, h, last_seen] as fractions of the frame
        self.busy = False
        self.last_submit = 0.0
        self.job = None
        self.wake = threading.Event()
        if self.det is not None:
            threading.Thread(target=self._worker, daemon=True).start()

    # -- detectors -------------------------------------------------------------------------
    def _search_dirs(self):
        dirs = []
        base = getattr(sys, "_MEIPASS", None)
        if base:
            dirs += [base, os.path.join(base, "cv2", "data")]
        here = os.path.dirname(os.path.abspath(__file__))
        dirs += [here, os.path.join(here, "models")]
        return dirs

    def _load_yunet(self):
        """YuNet (OpenCV's neural face detector): finds tilted, small, dark and partly covered faces
        much better than the old Haar cascade. The model file is added to the .exe by the build."""
        cv2 = self.cv2
        try:
            if not hasattr(cv2, "FaceDetectorYN"):
                return None
            for d in self._search_dirs():
                path = os.path.join(d, self.YUNET_FILE)
                if os.path.exists(path):
                    return cv2.FaceDetectorYN.create(path, "", (320, 320), 0.5, 0.3, 50)
        except Exception:                        # noqa: BLE001 - fall back to Haar
            pass
        return None

    def _load_haar(self):
        cv2 = self.cv2
        dirs = []
        try:
            dirs.append(cv2.data.haarcascades)
        except Exception:                        # noqa: BLE001
            pass
        dirs += self._search_dirs()
        cascades = {}
        for key, name in (("front", "haarcascade_frontalface_alt2.xml"), ("profile", "haarcascade_profileface.xml")):
            for d in dirs:
                path = os.path.join(d, name)
                if os.path.exists(path):
                    c = cv2.CascadeClassifier(path)
                    if not c.empty():
                        cascades[key] = c
                        break
        return cascades if "front" in cascades else None

    def _detect(self, img):
        """img: small BGR picture. Returns [(x, y, w, h)] in its pixels. Never raises."""
        cv2 = self.cv2
        h, w = img.shape[:2]
        if self.kind == "yunet":
            try:
                self.yunet.setInputSize((w, h))
                _, faces = self.yunet.detect(img)
                return self._parse_yunet(faces)
            except Exception:                    # noqa: BLE001 - YuNet failed on this machine: use Haar
                self.yunet = None
                self.haar = self.haar or self._load_haar()
                self.kind = "haar" if self.haar is not None else ""
                if not self.kind:
                    return []
        if self.kind == "haar":
            gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
            ms = max(24, w // 24)
            rects = [tuple(r) for r in self.haar["front"].detectMultiScale(gray, 1.1, 4, minSize=(ms, ms))]
            prof = self.haar.get("profile")
            if prof is not None:                 # head turned to one side (both directions)
                for r in prof.detectMultiScale(gray, 1.1, 4, minSize=(ms, ms)):
                    rects.append(tuple(r))
                for (x, y, bw, bh) in prof.detectMultiScale(cv2.flip(gray, 1), 1.1, 4, minSize=(ms, ms)):
                    rects.append((w - x - bw, y, bw, bh))
            return self._dedupe(rects)
        return []

    @staticmethod
    def _parse_yunet(faces):
        if faces is None:
            return []
        return [(float(r[0]), float(r[1]), float(r[2]), float(r[3])) for r in faces]

    @staticmethod
    def _dedupe(rects):
        """Drops boxes whose centre lies inside an earlier (front-face first) box."""
        out = []
        for (x, y, w, h) in rects:
            cx, cy = x + w / 2, y + h / 2
            if not any(ox <= cx <= ox + ow and oy <= cy <= oy + oh for (ox, oy, ow, oh) in out):
                out.append((x, y, w, h))
        return out

    # -- detection thread
    def _worker(self):
        while True:
            self.wake.wait()
            self.wake.clear()
            job, self.job = self.job, None
            if job is None:
                continue
            try:
                sh, sw = job.shape[:2]
                self._merge(self._detect(job), sw, sh)
            except Exception:                    # noqa: BLE001 - never kill the thread
                pass
            self.busy = False

    def _merge(self, rects, sw, sh):
        now = time.time()
        new = [[(x + w / 2) / sw, (y + h / 2) / sh, w / sw, h / sh, now] for (x, y, w, h) in rects]
        with self.lock:
            old = [b for b in self.boxes if now - b[4] < self.HOLD]
            used = set()
            out = []
            for n in new:
                for i, o in enumerate(old):
                    if i not in used and abs(o[0] - n[0]) < 0.6 * max(o[2], n[2]) \
                            and abs(o[1] - n[1]) < 0.6 * max(o[3], n[3]):
                        used.add(i)
                        n = [(o[k] + n[k]) / 2 for k in range(4)] + [now]    # smooth the jitter
                        break
                out.append(n)
            out += [o for i, o in enumerate(old) if i not in used]            # briefly missed faces
            self.boxes = out

    # -- per frame (decoder process)
    def process(self, frame, on, hdr):
        if not on:
            if self.boxes:
                with self.lock:
                    self.boxes = []
            hdr[H_BLURSTATE] = 0
            hdr[H_FACES] = 0
            return frame
        if self.det is None or not self.kind:
            hdr[H_BLURSTATE] = 2                 # no detector available: the GUI warns about it
            return frame
        hdr[H_BLURSTATE] = 3 if self.kind == "yunet" else 1     # 3 = YuNet, 1 = Haar
        cv2 = self.cv2
        now = time.time()
        h, w = frame.shape[:2]
        if not self.busy and now - self.last_submit >= self.INTERVAL:
            self.last_submit = now
            small = frame
            if w > self.DETECT_W:
                small = cv2.resize(frame, (self.DETECT_W, max(2, int(h * self.DETECT_W / w))),
                                   interpolation=cv2.INTER_AREA)
            self.job = small.copy()                  # the frame is blurred in place below
            self.busy = True
            self.wake.set()
        with self.lock:
            self.boxes = [b for b in self.boxes if now - b[4] < self.HOLD]
            boxes = list(self.boxes)
        for cx, cy, bw, bh, _ in boxes:
            self._blur_one(frame, cx * w, (cy - 0.04 * bh) * h, bw * w * 1.8, bh * h * 1.9)
        hdr[H_FACES] = len(boxes)
        return frame

    def _blur_one(self, frame, cx, cy, bw, bh):
        cv2 = self.cv2
        fh, fw = frame.shape[:2]
        x0, y0 = max(0, int(cx - bw / 2)), max(0, int(cy - bh / 2))
        x1, y1 = min(fw, int(cx + bw / 2)), min(fh, int(cy + bh / 2))
        w, h = x1 - x0, y1 - y0
        if w < 8 or h < 8:
            return
        roi = frame[y0:y1, x0:x1]
        d = max(2, min(w, h) // 12)              # shrink -> blur -> enlarge: very strong, very fast
        small = cv2.resize(roi, (max(2, w // d), max(2, h // d)), interpolation=cv2.INTER_AREA)
        small = cv2.GaussianBlur(small, (5, 5), 0)
        blurred = cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)
        # soft oval mask, built small and enlarged (a big Gaussian on the full-size mask is slow)
        k = max(1, max(w, h) // 64)
        mw, mh = max(4, w // k), max(4, h // k)
        mask = np.zeros((mh, mw), np.uint8)
        sigma = max(1.2, min(mw, mh) / 22)
        pad = int(2.5 * sigma) + 1                # the soft edge must fade out before the box ends
        cv2.ellipse(mask, (mw // 2, mh // 2), (max(1, mw // 2 - pad), max(1, mh // 2 - pad)), 0, 0, 360, 255, -1)
        mask = cv2.GaussianBlur(mask, (0, 0), sigma)
        mask = cv2.cvtColor(cv2.resize(mask, (w, h), interpolation=cv2.INTER_LINEAR), cv2.COLOR_GRAY2BGR)
        roi[:] = cv2.add(cv2.multiply(blurred, mask, scale=1 / 255.0),
                         cv2.multiply(roi, cv2.bitwise_not(mask), scale=1 / 255.0))


# ---- H.264 --------------------------------------------------------------------------------
class H264Decoder:
    """Low-latency H.264 decoding with PyAV (FFmpeg): one access unit in -> one frame out."""

    def __init__(self):
        import av
        self.av = av
        self.reset()

    def reset(self):
        ctx = self.av.CodecContext.create("h264", "r")
        try:
            ctx.thread_type = "SLICE"            # no frame threading = no extra frames of delay
            ctx.options = {"flags": "low_delay"}
        except Exception:                        # noqa: BLE001
            pass
        self.ctx = ctx

    def decode(self, data, target):
        frames = self.ctx.decode(self.av.Packet(bytes(data)))
        if not frames:
            return None
        fr = frames[-1]
        w, h = fit_size(fr.width, fr.height, target)
        try:
            img = fr.reformat(width=w, height=h, format="bgr24", src_colorspace="ITU709").to_ndarray()
        except Exception:                        # noqa: BLE001 - older PyAV without colorspace args
            img = fr.reformat(width=w, height=h, format="bgr24").to_ndarray()
        return pad_to(img, target)


def is_keyframe(payload):
    """Our access units start with SPS (NAL type 7) on key frames."""
    return len(payload) > 4 and (payload[4] & 0x1F) == 7


# ---- decoder process ----------------------------------------------------------------------
# message from the GUI process: b"J"|b"H" + 8 bytes (time.time() at arrival) + payload,
#                               b"R" (new stream: reset decoder)
def decoder_main(bus_name, conn, max_w, max_h):
    import cv2
    _timer_1ms()
    bus = SharedBus(max_w, max_h, name=bus_name)
    hdr = bus.hdr
    h264 = None
    need_key = True
    src_w = 0
    blurer = FaceBlur()
    last_chk = time.time()
    while not hdr[H_QUIT]:
        try:
            if time.time() - last_chk > 0.5:
                last_chk = time.time()
                if parent_gone():
                    break
            if not conn.poll(0.1):
                continue
            msgs = [conn.recv_bytes()]
            while len(msgs) < 64 and conn.poll(0):
                msgs.append(conn.recv_bytes())

            # a reset starts a new stream: forget everything before it
            for i in range(len(msgs) - 1, -1, -1):
                if msgs[i][:1] == b"R":
                    msgs = msgs[i + 1:]
                    h264 = None
                    need_key = True
                    break

            target = (int(hdr[H_TW]), int(hdr[H_TH])) if hdr[H_TW] else None
            hs = [m for m in msgs if m[:1] == b"H"]
            js = [m for m in msgs if m[:1] == b"J"]
            frame = None
            t_rx = time.time()

            if hs:
                # backlog: H.264 frames depend on each other, so skip ahead to the newest key frame
                if len(hs) > 4:
                    keys = [i for i, m in enumerate(hs) if is_keyframe(memoryview(m)[9:])]
                    if keys:
                        hs = hs[keys[-1]:]
                    else:
                        hs = []
                        need_key = True
                        hdr[H_KEYREQ] = 1
                if h264 is None:
                    h264 = H264Decoder()
                for m in hs:
                    payload = memoryview(m)[9:]
                    if need_key:
                        if not is_keyframe(payload):
                            hdr[H_KEYREQ] = 1
                            continue
                        need_key = False
                    t0 = time.perf_counter()
                    try:
                        f = h264.decode(payload, target)
                    except Exception:            # noqa: BLE001 - wait for the next key frame
                        h264.reset()
                        need_key = True
                        hdr[H_KEYREQ] = 1
                        continue
                    hdr[H_DECUS] = int(hdr[H_DECUS] * 0.9 + (time.perf_counter() - t0) * 1e6 * 0.1)
                    if f is not None:
                        frame = f
                        t_rx = struct.unpack("<d", m[1:9])[0]
                        hdr[H_CODEC] = 2
            elif js:
                m = js[-1]                       # MJPEG: only the newest picture matters
                t0 = time.perf_counter()
                flag = cv2.IMREAD_COLOR
                if target and src_w >= 2 * target[0]:
                    flag = cv2.IMREAD_REDUCED_COLOR_2
                f = cv2.imdecode(np.frombuffer(memoryview(m)[9:], np.uint8), flag)
                if f is not None:
                    src_w = f.shape[1] * (2 if flag == cv2.IMREAD_REDUCED_COLOR_2 else 1)
                    if target and (f.shape[1], f.shape[0]) != target:
                        fw, fh = fit_size(f.shape[1], f.shape[0], target)
                        if (fw, fh) != (f.shape[1], f.shape[0]):
                            f = cv2.resize(f, (fw, fh), interpolation=cv2.INTER_LINEAR)
                        f = pad_to(f, target)
                    frame = f
                    t_rx = struct.unpack("<d", m[1:9])[0]
                    hdr[H_CODEC] = 1
                    hdr[H_DECUS] = int(hdr[H_DECUS] * 0.9 + (time.perf_counter() - t0) * 1e6 * 0.1)

            if frame is not None:
                if hdr[H_ROT]:
                    frame = cv2.rotate(frame, cv2.ROTATE_180)
                frame = blurer.process(frame, bool(hdr[H_BLUR]), hdr)    # last step: blur faces
                bus.publish(frame, int(max(0.0, time.time() - t_rx) * 1e6))
                hdr[H_DECODED] += 1
        except (EOFError, OSError):
            break
        except Exception:                        # noqa: BLE001 - never die, ask for a key frame
            need_key = True
            hdr[H_KEYREQ] = 1
    bus.close()


# ---- virtual camera process ---------------------------------------------------------------
def vcam_main(bus_name, max_w, max_h):
    import pyvirtualcam
    _timer_1ms()
    bus = SharedBus(max_w, max_h, name=bus_name)
    hdr = bus.hdr
    cam = None
    key = None
    swap = False
    send_ms = 0.0
    next_t = time.perf_counter()
    last_chk = time.time()
    while not hdr[H_QUIT]:
        if time.time() - last_chk > 0.5:
            last_chk = time.time()
            if parent_gone():
                break
        fps = max(1, int(hdr[H_OUTFPS]) or 30)
        interval = 1.0 / fps
        wait = next_t - time.perf_counter()
        if wait > 0.0005:
            time.sleep(wait)
        next_t = max(next_t + interval, time.perf_counter() - interval)

        seq, frame = bus.latest()
        if frame is None:
            time.sleep(0.01)
            continue
        h, w = frame.shape[:2]
        if cam is None or key != (w, h, fps):
            if cam is not None:
                try:
                    cam.close()
                except Exception:                # noqa: BLE001
                    pass
                cam = None
            for fmt, sw in ((pyvirtualcam.PixelFormat.BGR, False), (pyvirtualcam.PixelFormat.RGB, True)):
                try:
                    cam = pyvirtualcam.Camera(width=w, height=h, fps=fps, fmt=fmt, backend="unitycapture")
                    swap = sw
                    key = (w, h, fps)
                    hdr[H_VSTATE] = 1
                    break
                except Exception:                # noqa: BLE001
                    cam = None
            if cam is None:
                hdr[H_VSTATE] = 2                # driver missing
                time.sleep(0.5)
                continue
        t0 = time.perf_counter()
        try:
            if swap:
                import cv2
                cam.send(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            else:
                cam.send(frame)
        except Exception:                        # noqa: BLE001 - reopen on the next round
            try:
                cam.close()
            except Exception:                    # noqa: BLE001
                pass
            cam = None
            continue
        send_ms = send_ms * 0.9 + (time.perf_counter() - t0) * 1000 * 0.1
        hdr[H_SENDUS] = int(send_ms * 1000)
        hdr[H_SENT] += 1
    if cam is not None:
        try:
            cam.close()
        except Exception:                        # noqa: BLE001
            pass
    bus.close()
