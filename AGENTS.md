# AGENTS.md

## What this repo is

Multi-camera image streaming service for Unitree robot teleoperation. Captures from UVC/OpenCV/RealSense cameras (Jetson) or picamera2 (Raspberry Pi 5) and publishes over ZeroMQ PUB-SUB and WebRTC.

**Entrypoints**: `teleimager-server` (defined in `pyproject.toml` → `image_server.py:main`) and `teleimager-client` (`image_client.py:main`)

**Source structure**: All code in `src/teleimager/` — only two files: `image_server.py` (87KB), `image_client.py` (32KB)

---

## Install

```bash
# Jetson / generic Linux (server + client)
pip install -e ".[server]"

# Raspberry Pi 5 (server + client)
pip install -e ".[raspi]"

# Client only
pip install -e .
```

**Critical RPi 5 quirk**: `picamera2` MUST be installed via apt, NOT pip. The `[raspi]` extra intentionally omits it. Run `setup_raspi.sh` which:
- Installs `python3-picamera2` + `python3-numpy` + `python3-opencv` via apt
- Creates `.venv` with `--system-site-packages` (mandatory for libcamera native bindings)
- Uninstalls any pip-installed `numpy`/`opencv-python` from venv (they conflict with libcamera at runtime)

Installing picamera2 from PyPI breaks libcamera native bindings with ImportError.

**Setup scripts**:
```bash
bash setup_raspi.sh   # RPi 5: apt deps + udev + venv with system-site-packages
bash setup_uvc.sh     # Jetson/generic: udev rules + video group + passwordless modprobe
```

---

## Run commands

```bash
# Server — Jetson (default: cam_config_server.yaml)
teleimager-server
teleimager-server --rs          # add RealSense support
teleimager-server --cf          # camera discovery: lists all cameras with serials/paths

# Server — RPi 5 (uses cam_config_raspi.yaml)
teleimager-server --raspi
teleimager-server --config cam_config_raspi.yaml   # equivalent

# Custom config
teleimager-server --config /path/to/config.yaml

# Client (any machine, same network)
teleimager-client --host <server-ip>
teleimager-client --host 192.168.4.1   # typical RPi hotspot IP
```

**Camera discovery**: Always run `teleimager-server --cf` (or with `sudo` for full hardware metadata) before editing config files. Add `--rs` if RealSense cameras are connected.

---

## Config files

| File | Platform | Server flag |
|---|---|---|
| `cam_config_server.yaml` | Jetson / generic Linux | *(default)* |
| `cam_config_raspi.yaml` | Raspberry Pi 5 | `--raspi` |

**YAML structure**: Each top-level key is a camera topic (e.g. `head_camera`, `left_wrist_camera`). 

**Camera identifier priority**: `physical_path > serial_number > video_id`. Server matches cameras in that order. Set unused identifiers to `null`.

**Camera types**: `uvc`, `opencv`, `realsense` (Jetson/generic); `picamera2` (RPi 5 only)

**Per-camera settings**: `enable_zmq`, `zmq_port`, `enable_webrtc`, `webrtc_port`, `webrtc_codec` (h264/vp8), `image_shape`, `fps`

---

## WebRTC certificates (required for WebRTC)

Server checks in order:
1. Env vars: `XR_TELEOP_CERT` / `XR_TELEOP_KEY`
2. `~/.config/xr_teleoperate/cert.pem` / `key.pem`
3. Repo root: `cert.pem` / `key.pem`

**Generate self-signed** (testing only — browsers show warnings):
```bash
mkdir -p ~/.config/xr_teleoperate/
openssl req -x509 -newkey rsa:4096 -keyout ~/.config/xr_teleoperate/key.pem \
  -out ~/.config/xr_teleoperate/cert.pem -days 365 -nodes -subj "/CN=localhost"
```

`.gitignore` blocks: `*.pem`, `*.key`, `*.csr`, `*.cnf` — never commit certs.

---

## Client API (Python)

All public methods marked `# public api` in source.

```python
from teleimager.image_client import ImageClient

client = ImageClient(host="192.168.4.1", request_bgr=True)
cam_config = client.get_cam_config()          # dict: all camera topics + config
frame = client.get_frame("head_camera")       # TeleImage(fps, jpg, bgr)
frame = client.get_head_frame()               # shortcut
frame = client.get_left_wrist_frame()         # shortcut
frame = client.get_right_wrist_frame()        # shortcut
client.close()
```

**TeleImage fields**: `.bgr` is decoded numpy BGR array (only when `request_bgr=True`), `.jpg` is raw JPEG bytes, `.fps` is measured framerate.

---

## Test/verification

**Manual test script**:
```bash
python test_save_image.py --host <server-ip> --output-dir /tmp/captures
```
Connects via ZMQ, captures one frame per ZMQ-enabled camera, saves as PNG. Use to verify server + cameras streaming.

**No automated tooling**: No pytest, linter, type checker, formatter, CI, pre-commit hooks, or task runner. Verification is entirely manual (run server → connect client → check frames).

---

## Autostart (systemd)

```bash
bash setup_autostart.sh   # interactive: detects conda env, writes systemd unit
```

Service management:
```bash
sudo systemctl status teleimager.service
sudo journalctl -u teleimager.service -f      # tail logs
sudo systemctl restart teleimager.service
sudo systemctl disable teleimager.service
```

---

## Platform-specific quirks

**RPi 5 H264 encoding**: Auto-detects `h264_v4l2m2m` (V4L2 hardware encoder), falls back to `libx264` if unavailable.

**Jetson encoding**: Always uses software `libx264`.

**Python version**: `>=3.8,<3.13` (documented setup: conda with Python 3.10)

**UVC driver reload**: `setup_uvc.sh` grants passwordless sudo for `modprobe -r uvcvideo` / `modprobe uvcvideo`. Server may reload the driver before opening cameras.

---

## Camera identifier trade-offs

| Identifier | Stability | Flexibility | Use case |
|---|---|---|---|
| `physical_path` | ⭐⭐⭐⭐⭐ | ⭐ | Fixed robot deployments, cameras with duplicate serials |
| `serial_number` | ⭐⭐⭐⭐ | ⭐⭐⭐ | RealSense, cameras with unique serials |
| `video_id` | ⭐⭐ | ⭐⭐⭐⭐⭐ | Single camera or temporary testing |

**Why physical_path priority**: Low-cost UVC cameras often share serial numbers. Physical path is stable across reboots when cameras stay in same USB ports.
