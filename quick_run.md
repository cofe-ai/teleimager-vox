
# 环境安装-Linux (5090服务器)

```bash
conda create -n teleimager python=3.10 -y
conda activate teleimager

cd teleimager-vox
# 只使用客户端
pip install -e .
# 只使用服务端
pip install -e ".[server]"

# 可能存在部分包缺失
```

# 服务端操作

## 摄像头相关
* 确认设备已接入
    ```bash
    lsusb
    ```

* 添加 video 权限（非 root 用户运行）
    ```bash
    # 注意，尽量不要在tmux中执行
    sudo bash setup_uvc.sh
    ```

* 为WebRTC配置证书
> 严格来说不需要，但是不配置后期容易保存，暂时进行该部分配置。后续调试细节原因
    ```bash
    mkdir -p ~/.config/xr_teleoperate/
    openssl req -x509 -newkey rsa:4096 \
        -keyout ~/.config/xr_teleoperate/key.pem \
        -out ~/.config/xr_teleoperate/cert.pem \
        -days 365 -nodes \
        -subj "/CN=localhost"
    ```

* 查找已连接的摄像头
> 运行以下命令可以自动发现已连接摄像头：
    ```bash
    python -m teleimager.image_server --cf
    # 或
    teleimager-server --cf
    ```

> 可能获得一下返回结果
    ```bash
    22:18:12.815001 INFO     UVC driver reloaded successfully.
    22:18:13.707263 INFO     ======================= Camera Discovery Start ==================================
    22:18:13.707443 INFO     Found video devices: ['/dev/video0', '/dev/video1']
    22:18:13.707738 INFO     Found RGB video devices: ['/dev/video0']
    22:18:13.707773 INFO     ----------------------- OpenCV / UVC Camera 1 -----------------------------
    22:18:13.707875 INFO     video_path    : /dev/video0
    22:18:13.708018 INFO     video_id      : 0
    22:18:13.708042 INFO     serial_number : unknown
    22:18:13.708063 INFO     physical_path : /sys/devices/pci0000:00/0000:00:14.0/usb1/1-3/1-3.4/1-3.4:1.0
    22:18:13.708083 INFO     extra_info:
    22:18:13.708105 INFO         name: USB 2.0 Camera
    22:18:13.708124 INFO         manufacturer: Sonix Technology Co., Ltd.
    22:18:13.708143 INFO         serialNumber: unknown
    22:18:13.708161 INFO         idProduct: 25448
    22:18:13.708177 INFO         idVendor: 3141
    22:18:13.708194 INFO         device_address: 5
    22:18:13.708210 INFO         bus_number: 1
    22:18:13.708227 INFO         uid: 1:5
    22:18:13.788305 INFO         format: 240x320@120 MJPG
    22:18:13.788671 INFO         format: 480x640@120 MJPG
    22:18:13.788852 INFO         format: 600x800@60 MJPG
    22:18:13.788961 INFO         format: 768x1024@30 MJPG
    22:18:13.789051 INFO         format: 720x1280@60 MJPG
    22:18:13.789135 INFO         format: 1024x1280@30 MJPG
    22:18:13.789216 INFO         format: 1080x1920@30 MJPG
    22:18:13.849942 INFO     =========================== Camera Discovery End ================================
    ```

* 根据已经查到的信息，编辑一个config文件
> 例: 编辑`vox_cam_config_server.yaml`
    ```yaml
    # =====================================================
    # Head camera configuration
    # =====================================================
    # camera topic
    head_camera:
    # camera config

    # if enable_zmq and enable_webrtc are both false, the camera will not start
    # Set to true to enable ZMQ publishing, false to disable
    enable_zmq: true
    # Port to publish camera stream, e.g. zmq tcp://*:55555.  image_client.py should connect to the same port
    zmq_port : 55555

    # Set to true to enable WebRTC publishing, false to disable
    # webrtc其实可以不配置
    enable_webrtc: true
    # Port for WebRTC signaling server
    webrtc_port : 60001
    # webrtc codec preference, options: "vp8", "h264"
    webrtc_codec: h264

    # Type of camera:
    #   - "opencv"    → opencv driver
    #   - "realsense" → pyrealsense2 driver
    #   - "uvc"       → pyuvc driver
    type: uvc

    # 这里注意，需要和上述查询到的结果匹配上
    # Image Format
    # image resolution: [height, width]
    image_shape: [720, 1280]
    binocular: true
    # frame per second
    fps: 60

    # Camera identifiers (choose one or more):
    #   - video_id: X        → /dev/videoX  (e.g. 0 → /dev/video0)
    #   - serial_number: Y   → camera's hardware serial (e.g. 141722079879)
    #   - physical_path: Z   → sysfs physical USB path (e.g. /sys/devices/pci0000:00/.../1-11.2:1.0)
    #
    # Identifier priority:
    #   physical_path > serial_number > video_id
    #   if an identifier is not used, set it to null. The system will resolve the camera by priority.
    #
    # Notes:
    #   - type "realsense": supports serial_number only (but a RealSense can also be used as opencv/uvc if desired)
    #   - type "opencv":    supports video_id, serial_number, physical_path
    #   - type "uvc":       supports video_id, serial_number, physical_path
    video_id: 0
    serial_number: null
    physical_path: /sys/devices/pci0000:00/0000:00:14.0/usb1/1-3/1-3.4/1-3.4:1.0
    ```

* 启动服务端
```bash
python -m teleimager.image_server --config vox_cam_config_server.yaml
```

# 客服端操作

## 摄像头相关
* webrtc
使用浏览器直接访问 `https://x.x.x.x:60001`

* ZMQ方式
```bash
python teleimager-client --host 192.168.1.156
```

---

# 音频服务端操作

## 安装音频依赖

```bash
conda activate teleimager
pip install "sounddevice>=0.4.6" "pyusb>=1.2.1" "libusb-package>=1.0.26"
pip install -e ".[audio]"
```

## 发现音频设备
```bash
teleimager-audio-server --audio-cf
```
> 列出所有音频输入设备，XVF3800 候选设备会标注 `*** XVF3800 CANDIDATE ***`

示例输出：
```
00:00:00 INFO  ==================================================================
00:00:00 INFO  Audio Device Discovery
00:00:00 INFO  ==================================================================
00:00:00 INFO    [ 0] XMOS XVF3800 (hw:1,0)          6 ch @  16000 Hz  *** XVF3800 CANDIDATE ***
00:00:00 INFO    [ 1] pulse                          32 ch @  44100 Hz
00:00:00 INFO  ==================================================================
```

## 编辑配置文件
根据 `--audio-cf` 输出，按需编辑 `audio_config.yaml`：
- 若自动检测失败，将 `device_id: null` 改为检测到的设备名称（如 `device_id: "XMOS XVF3800 (hw:1,0)"`）
- 修改 `zmq_port` 若默认端口被占用

## 启动音频服务端
```bash
# 使用默认配置文件 audio_config.yaml
teleimager-audio-server

# 指定配置文件
teleimager-audio-server --config audio_config.yaml
```

## DOA/VAD 支持
若 xvf3800_doa_vad 脚本不在默认路径（`~/projects/voxinsight-sensor-gateway/xvf3800_doa_vad/`），设置环境变量：
```bash
export XVF_DOA_VAD_PATH=/path/to/xvf3800_doa_vad
teleimager-audio-server
```

## 快速验证（无客户端）

```bash
# 验证音频 ZMQ 帧接收（需服务端已启动）
python3 -c "
import zmq, struct
ctx = zmq.Context()
sock = ctx.socket(zmq.SUB)
sock.connect('tcp://localhost:55560')
sock.setsockopt_string(zmq.SUBSCRIBE, '')
sock.setsockopt(zmq.RCVTIMEO, 3000)
data = sock.recv()
magic = struct.unpack_from('<H', data)[0]
print(f'Received {len(data)} bytes, magic=0x{magic:X}')
"

# 验证 DOA/VAD 接收
python3 -c "
import zmq, json
ctx = zmq.Context()
sock = ctx.socket(zmq.SUB)
sock.connect('tcp://localhost:55561')
sock.setsockopt_string(zmq.SUBSCRIBE, '')
sock.setsockopt(zmq.RCVTIMEO, 3000)
msg = json.loads(sock.recv())
print(f'DOA={msg[\"doa\"]}°  VAD={msg[\"vad\"]}')
"
```

## 客户端连接
```bash
teleimager-audio-client --host <server-ip>
teleimager-audio-client --host 192.168.4.1 --duration 10
```

## 端口规划

| 端口 | 用途 | 协议 |
|---|---|---|
| 55559 | 音频配置查询 | ZMQ REQ-REP |
| 55560 | head_audio PCM 音频帧 | ZMQ PUB-SUB |
| 55561 | head_audio DOA/VAD 元数据 | ZMQ PUB-SUB |