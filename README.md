# HD Camera iPhone - iPhone as a USB webcam

iPhone app streams camera video over the USB cable; the Windows app turns it into a virtual webcam
called **HD Camera iPhone**.

## Build
Push to GitHub -> Actions -> *Build USC-CAM* -> download `USC-CAM-Package`
(contains `USC-CAM.ipa`, `USC-CAM.exe`, `driver/` and `Install-HD-Camera.bat`).

## Install / use
1. iPhone: sideload `USC-CAM.ipa` (Sideloadly / AltStore - unsigned). Open the app, allow the camera.
2. PC: run `USC-CAM.exe` - on the first start it installs everything itself (see v10).
3. PC: nothing to install by hand any more (see v10).
4. Plug in the iPhone, tap *Trust*, keep USC-CAM open, run `USC-CAM.exe`.
5. In Zoom / Discord / Teams etc. pick **HD Camera iPhone**.

## On the phone
The app shows the live camera image. Pick **Resolution** (720p / 1080p / 4K) and **Quality**
(Low / Medium / High) right on the screen - lower values = less lag. Default: 1080p, Medium.
On the PC you can additionally downscale the output.

Note: iOS pauses the camera when the app is in the background - keep it open (use *Dim screen* to save battery).

## v3 changes
- Virtual webcam is called **HD Camera iPhone** only (OBS fallback removed). The .exe installs the driver itself on first start (one Windows prompt).
- When the phone is not connected, the virtual camera shows a **"Waiting for camera"** screen with the app icon
  instead of an error / black picture.
- Less lag in the .exe: receive, decode and send now run in 3 separate threads, always keeping only the newest frame.
  Default output is 1920x1080 (stable size).
- Fixed the GitHub workflow (Xcode version, path conversion on Windows, driver DLL copy).

## v5 changes (lag + 120 fps)
- iPhone streams up to **120 fps** (picker: 120 / 60 / 30; unsupported rates are hidden, the app falls back to the best the camera can do). 1080p is the best size for 120 fps on most iPhones.
- Camera format is chosen explicitly (widest field of view, 8-bit), **video stabilization and video HDR are off** (both buffer frames = lag).
- **Flow control**: the PC acknowledges every frame, the iPhone keeps max 3 frames in flight. No more old frames piling up in the USB cable. The newest frame always wins.
- PC: preallocated receive buffers, preview limited to 15 fps, 1 ms Windows timer, higher process priority, virtual cam announces 120 fps.
- JPEG quality values re-tuned (Low 0.45 / Medium 0.6 / High 0.8) so 1080p120 fits through USB 2.0.

## v6 changes (H.264 + Wi-Fi)
- **H.264 instead of per-frame JPEG** (default). The iPhone's hardware encoder (low-latency, no B-frames) sends ~25-60 Mbit/s for 1080p120 instead of ~300 Mbit/s of JPEG: much sharper picture, no USB/Wi-Fi saturation, PC decodes in ~2 ms. MJPEG stays available in the app (CODEC picker).
- Quality = bitrate: Low ~25, Medium ~40, High ~62 Mbit/s at 1080p120 (scales with resolution and fps).
- **Wi-Fi mode**: the app shows the iPhone's IP. In USC-CAM.exe choose *Connection: Wi-Fi* and type the IP. Both devices on the same network (5 GHz, no guest network). Allow "Local Network" on the iPhone when asked.
- The PC acknowledges a frame only after decoding it, so the iPhone never sends faster than the PC can show.
- Window shows codec, Mbit/s and decode time. If "decode" is above ~7 ms at 120 fps the PC is the bottleneck - lower fps or output size.

## v7 changes (lag hunting on the PC side)
- Virtual camera is fed at a **fixed rhythm** (selectable 120 / 60 / 30 fps) with the newest decoded frame, instead of in bursts. Set the SAME fps in your app (OBS: Video Capture Device -> Resolution/FPS Type "Custom", FPS = same value; Zoom/Discord/Teams use 30).
- OBS tip: in the Video Capture Device properties turn **Use Buffering** off, and make the OBS Base/Output resolution match the stream (1920x1080) so the driver does not rescale every frame.
- Window shows 3 lines of diagnostics: in/out fps, decode ms, **vcam send ms**, **frame age ms**. A per-second log is written to `%TEMP%\usc-cam.log`.
- **Live window** (dropdown): the full picture in its own window that bypasses the virtual camera. In OBS use *Window Capture*. If this is smooth and the virtual camera is not, the driver/app chain is the cause.

## v8 changes
- Fixed the GitHub workflow: finds the sources wherever they were uploaded, recreates `project.yml` if it is missing, and picks up the current UnityCapture DLL names (`UnityCaptureFilter64.dll` / `32.dll`).
- Default quality is now **High** (H.264, ~62 Mbit/s at 1080p120).

## v9 - fixes the "7 fps on the PC" problem, adds Wi-Fi auto-connect, microphone and sound
- **Root cause of the low PC frame rate:** decoding and the virtual-camera copy shared one Python process (one GIL) and starved each other. Now the PC app runs three separate processes: GUI + socket, **decoder**, **virtual camera feeder**, connected through shared memory. Frames are acknowledged on arrival, so the phone sends at full speed; if the PC ever falls behind it skips to the next key frame instead of building lag.
- The phone screen shows live diagnostics: `camera N/s` (frames from the sensor), `sent N/s`, `dropped`, `encode ms`, `queue`. The PC window shows in/out fps, decode ms, vcam send ms, frame age.
- **Auto connection like iVCam:** *Auto* mode uses the USB cable if present, otherwise finds the iPhone on the Wi-Fi by itself (Bonjour). No IP typing. (*Wi-Fi (type IP)* is still there as a fallback.)
- **Drivers from the app:** the virtual camera driver installs itself on first start (one Windows prompt). *Install USB driver* opens Apple's "Apple Devices" page (only needed for the USB cable). *Install virtual microphone* downloads and installs VB-CABLE from vb-audio.com.
- **Phone microphone -> PC:** switch "Phone mic -> PC" on, then choose **CABLE Output (VB-Audio Virtual Cable)** as the microphone in Discord / OBS / Zoom.
- **

## v12 - picture follows the phone, face blur, simple screens
- **The picture turns with the phone.** Hold the iPhone in portrait, landscape or upside down: the picture on the PC is always the right way up (rear and front camera). A portrait picture is shown in the middle of the fixed-size virtual camera with black bars at the sides (never stretched). Face up / face down keeps the last orientation.
- **Blur faces** (PC window, top row): detects faces and blurs them in the picture that goes to Zoom / Discord / OBS (and the Live window). It is the very last step, after rotation and scaling. Detection runs about 20x per second in its own thread; a face that is lost for a moment stays blurred for 0.7 s. Limits: the detector (OpenCV Haar) finds faces that look roughly at the camera - a head turned far to the side or strongly tilted can be missed. The preview on the iPhone screen itself is not blurred. If the detector file is missing the PC window shows a red warning.
- **Simple by default.** iPhone: camera button, dim button and status only. PC: preview, status, *Blur faces* and *Phone mic*. Everything else (resolution, codec, frame rate, quality, output size, connection mode, live window, flip 180, diagnostics) is behind **Advanced settings**.
- *Advanced -> Picture: flipped 180* turns the PC picture around if you mount the phone in a way that needs it (before v12 this was always on).

- **Flickering between two pictures in OBS / Zoom fixed.** Cause: an old helper process (the part that writes into the virtual camera) survived a crash or forced close and kept sending its last picture while the new run sent the real one - the live window looked fine because it bypasses the virtual camera. Now: helpers stop by themselves when the app that started them is gone; only ONE USC-CAM window can run (a second start shows a message); at start every leftover `USC-CAM.exe` from an earlier run (any version) is ended automatically.

- **Blur faces is now much better.** It uses OpenCV's YuNet neural detector (the build downloads the ~230 KB model; it is bundled into the .exe) which finds tilted, small, dark and partly covered faces far better than the old one; if the model is missing the app falls back to the basic detector and the Advanced line shows `[basic detector]`. Detection runs continuously in its own thread and a lost face stays blurred for 1 s. Limits: a face cut in half by the edge of the picture or fully covered may still be missed.
- **WhatsApp.** The WhatsApp *Windows app* uses Windows' newer camera system (Media Foundation) and does not list DirectShow virtual cameras such as USC-CAM / OBS Virtual Camera - this is a Windows limitation, not a bug. Use **WhatsApp Web** (web.whatsapp.com) in Chrome or Edge: calls there work with USC-CAM (pick "HD Camera iPhone" in the browser's camera list).

## v11 - microphone fix, crash fixes, camera orientation, headphones removed
- **Microphone**: rewritten on the phone. v10 rebuilt the audio engine on its own route-change echoes (restart loop) and installed the input tap with a fixed format (random crash when the hardware format differed). Now: audio session set up once, tap with automatic format, one debounced restart if iOS stops the engine, and the PC's 2-second heartbeat repairs the rest. The phone screen shows `level %` and `pkt/s`; the PC window says in plain words whether sound arrives and whether the virtual cable is ready.
- **"PC sound -> phone headphones" removed** on both sides (phone and PC window).
- **Random crashes**: the network connection object was read from several threads at once (now behind a lock), `start()` can no longer run twice, NaN microphone samples can no longer crash the Int16 conversion.
- **Front / rear camera**: the label now always shows the camera really in use ("Rear (no face)" / "Front (selfie)"), the button says which camera it switches TO, and the output is pinned to one orientation so the PC picture is the right way up with BOTH cameras (before, the front camera arrived upside down).

## v10.1 - workflow fix: camera driver is fetched with a sparse checkout (Windows "Filename too long").

## v10 - one-click setup, calmer Wi-Fi
- **Everything installs itself.** Start `USC-CAM.exe` once. If something is missing it asks Windows for administrator rights ONCE and silently installs the virtual camera *HD Camera iPhone*, the virtual microphone *VB-CABLE* and the *Apple USB driver*. Log: `%TEMP%\usc-cam-setup.log`.
- **Wi-Fi lag**: on Wi-Fi the phone caps the video at ~28 Mbit/s (USB keeps full quality), marks the traffic as video, disables AWDL and adapts the bitrate live (`rate NN%` on the phone screen). Key frames every 2 s.
- **Small stutters**: Python garbage collection is tuned in all PC processes.
