# Copyright 2025 YuShu TECHNOLOGY CO.,LTD ("Unitree Robotics")
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import time
import json
import queue
import struct
import logging
import threading
import signal
import argparse

import numpy as np
import yaml
import zmq

from teleimager.image_client import (
    TripleRingBuffer,
    ZMQ_PublisherManager,
    SimpleFPSMonitor,
)

try:
    import sounddevice as sd
    _SD_AVAILABLE = True
except ImportError:
    sd = None
    _SD_AVAILABLE = False

try:
    from teleimager.xvf3800 import DoaVadReader as _DoaVadReader
    _XVF3800_AVAILABLE = True
except ImportError:
    _DoaVadReader = None
    _XVF3800_AVAILABLE = False

logger = logging.getLogger(__name__)

AUDIO_HEADER_FMT  = "<HBBBxqHI"
AUDIO_HEADER_SIZE = struct.calcsize(AUDIO_HEADER_FMT)   # must equal 20
AUDIO_MAGIC       = 0xA0D1
CODEC_PCM         = 0x01


class BaseAudioDevice:
    """Abstract base class for audio capture devices.
    Mirrors the BaseCamera pattern from image_server.py.
    Uses TripleRingBuffer for lock-free producer-consumer frame passing.
    Thread model:
        Thread 1: _update_frame()  — hardware capture -> ring buffer
        Thread 2: _zmq_pub()       — ring buffer -> ZMQ PUB socket
    """

    def __init__(self, topic, cfg):
        self.topic          = topic
        self.enable_zmq     = bool(cfg.get("enable_zmq", True))
        self.enable_webrtc  = bool(cfg.get("enable_webrtc", False))
        self.zmq_port       = int(cfg.get("zmq_port", 55560))
        self.sample_rate    = int(cfg.get("sample_rate", 16000))
        self.channels       = int(cfg.get("channels", 6))
        self.chunk_samples  = int(cfg.get("chunk_samples", 2560))

        self.pcm_ring_buffer = TripleRingBuffer()
        self._fps_monitor    = SimpleFPSMonitor(window_size=10)
        self._running        = False

    def _update_frame(self):
        """Subclass must implement: capture PCM from hardware -> write to pcm_ring_buffer."""
        raise NotImplementedError

    def _zmq_pub(self):
        pub_mgr        = ZMQ_PublisherManager.get_instance()
        frame_interval = self.chunk_samples / self.sample_rate
        last_ts        = None

        while self._running:
            frame = self.pcm_ring_buffer.read()
            if frame is None or frame["timestamp_ns"] == last_ts:
                time.sleep(frame_interval * 0.5)
                continue

            last_ts  = frame["timestamp_ns"]
            pcm_data = frame["pcm"].astype(np.float32)
            payload  = pcm_data.tobytes()

            header = struct.pack(
                AUDIO_HEADER_FMT,
                AUDIO_MAGIC,
                1,
                self.channels,
                CODEC_PCM,
                last_ts,
                self.sample_rate,
                len(payload),
            )
            pub_mgr.publish(header + payload, self.zmq_port)

    def start(self):
        self._running = True

    def stop(self):
        self._running = False


class XVF3800AudioDevice(BaseAudioDevice):
    """XVF3800 6-channel microphone array capture via sounddevice (PortAudio).

    Channel layout (read-only reference, not enforced by code):
        Ch 0: Conference Mix (omnidirectional)
        Ch 1: ASR Beam (beamformed, recommended for speech recognition)
        Ch 2-5: Raw microphone channels 0-3
    """

    def __init__(self, topic, cfg):
        super().__init__(topic, cfg)
        self._device_id     = cfg.get("device_id")   # None = auto-detect
        self._capture_queue = queue.Queue(maxsize=4)

    def _audio_callback(self, indata, frames, time_info, status):
        """PortAudio callback (runs in a C thread, must return fast).
        Bridges captured data to the Python capture thread via a queue.
        """
        if status:
            logger.warning("[%s] sounddevice callback status: %s", self.topic, status)
        try:
            self._capture_queue.put_nowait(indata.copy())
        except queue.Full:
            pass  # Drop frame rather than block the PortAudio callback thread

    def _update_frame(self):
        """Capture thread main loop: drain queue -> write to ring buffer."""
        if not _SD_AVAILABLE:
            logger.error("[%s] sounddevice is not installed. Cannot capture audio.", self.topic)
            return

        # Auto-detect XVF3800 if device_id not set
        dev = self._device_id
        if dev is None and sd is not None:
            for info in sd.query_devices():
                if info["max_input_channels"] > 0 and any(
                    k in info["name"] for k in ("XMOS", "XVF", "ReSpeaker")
                ):
                    dev = info["name"]
                    logger.info("[%s] Auto-detected XVF3800 device: %s", self.topic, dev)
                    break
            if dev is None:
                logger.warning(
                    "[%s] No XVF3800 device auto-detected. Using system default.", self.topic
                )

        try:
            sd_stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="float32",
                blocksize=self.chunk_samples,
                device=dev,
                callback=self._audio_callback,
            )
        except Exception as e:
            logger.error("[%s] Failed to open sounddevice stream: %s", self.topic, e)
            return

        with sd_stream:
            logger.info(
                "[%s] sounddevice stream started (device=%s, %d ch @ %d Hz, %d samples/chunk)",
                self.topic, dev, self.channels, self.sample_rate, self.chunk_samples,
            )
            while self._running:
                try:
                    pcm = self._capture_queue.get(timeout=1.0)
                    self.pcm_ring_buffer.write({
                        "pcm":          pcm,
                        "timestamp_ns": time.perf_counter_ns(),
                    })
                    self._fps_monitor.tick()
                except queue.Empty:
                    continue

        logger.info("[%s] sounddevice stream stopped.", self.topic)

    @staticmethod
    def list_devices():
        """List all audio input devices. Marks XVF3800 candidates.
        Used by --audio-cf CLI flag.
        Returns list of dicts: {index, name, channels, sample_rate, is_xvf3800}
        """
        if not _SD_AVAILABLE:
            return []
        result = []
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0:
                result.append({
                    "index":       i,
                    "name":        dev["name"],
                    "channels":    dev["max_input_channels"],
                    "sample_rate": int(dev["default_samplerate"]),
                    "is_xvf3800":  any(k in dev["name"] for k in ("XMOS", "XVF", "ReSpeaker")),
                })
        return result


class XVF3800DoaVadPoller:
    """Polls XVF3800 DOA/VAD data via USB control interface.

    Publishes JSON messages to a dedicated ZMQ port (default 55561).
    Message format: {"doa": 270, "vad": true, "timestamp_ns": 1234, "topic": "head_audio"}

    If xvf3800_doa_vad scripts are unavailable, self.available = False
    and all threads are silently skipped.
    """

    def __init__(self, topic, doa_cfg):
        self.topic         = topic
        self.enable_zmq    = bool(doa_cfg.get("enable", True))
        self.zmq_port      = int(doa_cfg.get("zmq_port", 55561))
        self.poll_interval = float(doa_cfg.get("poll_interval", 0.15))
        self.ring_buffer   = TripleRingBuffer()
        self._running      = False
        self.available     = _XVF3800_AVAILABLE

        if not self.available:
            logger.warning("[%s] teleimager.xvf3800 not available. DOA/VAD disabled.", topic)

    def _poll_loop(self):
        pub_mgr = ZMQ_PublisherManager.get_instance()

        try:
            reader = _DoaVadReader()
        except RuntimeError as e:
            logger.error("[%s] XVF3800 DOA/VAD device not found: %s", self.topic, e)
            return
        except Exception as e:
            logger.error("[%s] XVF3800 DOA/VAD init error: %s", self.topic, e)
            return

        logger.info(
            "[%s] DOA/VAD polling started (interval=%.2fs, port=%d)",
            self.topic, self.poll_interval, self.zmq_port,
        )

        with reader:
            while self._running:
                try:
                    doa_angle, vad_int = reader.read()
                    payload = {
                        "doa":          int(doa_angle),
                        "vad":          bool(vad_int),
                        "timestamp_ns": time.perf_counter_ns(),
                        "topic":        self.topic,
                    }
                    self.ring_buffer.write(payload)
                    if self.enable_zmq:
                        msg = json.dumps(payload).encode("utf-8")
                        pub_mgr.publish(msg, self.zmq_port)
                except Exception as e:
                    logger.warning("[%s] DOA/VAD read error (will retry): %s", self.topic, e)
                time.sleep(self.poll_interval)

        logger.info("[%s] DOA/VAD polling stopped.", self.topic)

    def start(self):
        self._running = True

    def stop(self):
        self._running = False


class AudioServer:
    """Audio server main class. Manages XVF3800AudioDevice and XVF3800DoaVadPoller lifecycle.
    Provides ZMQ REP socket for config queries (port 55559).
    Mirrors ImageServer structure from image_server.py.
    """

    CONFIG_REQ_PORT = 55559
    CONFIG_REQ_CMD  = b"get_audio_config"

    def __init__(self, config_path):
        self._audio_devices    = {}
        self._doa_vad_pollers  = {}
        self._threads          = []
        self._stop_event       = threading.Event()
        self._audio_config_raw = {}
        self._load_config(config_path)

    def _load_config(self, config_path):
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        self._audio_config_raw = cfg
        for topic, dev_cfg in cfg.items():
            device = XVF3800AudioDevice(topic, dev_cfg)
            self._audio_devices[topic] = device

            doa_cfg = dev_cfg.get("doa_vad", {})
            if doa_cfg.get("enable", True):
                poller = XVF3800DoaVadPoller(topic, doa_cfg)
                self._doa_vad_pollers[topic] = poller

    def start(self):
        """Start all capture/publish threads, block until SIGINT/SIGTERM."""
        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGINT,  lambda s, f: self._stop_event.set())
        signal.signal(signal.SIGTERM, lambda s, f: self._stop_event.set())

        # Start audio capture + ZMQ publish threads
        for topic, dev in self._audio_devices.items():
            dev.start()
            t_cap = threading.Thread(
                target=dev._update_frame, name=f"{topic}_capture", daemon=True
            )
            self._threads.append(t_cap)
            if dev.enable_zmq:
                t_pub = threading.Thread(
                    target=dev._zmq_pub, name=f"{topic}_zmq_pub", daemon=True
                )
                self._threads.append(t_pub)

        # Start DOA/VAD polling threads
        for topic, poller in self._doa_vad_pollers.items():
            if not poller.available:
                continue
            poller.start()
            t_poll = threading.Thread(
                target=poller._poll_loop, name=f"{topic}_doa_poll", daemon=True
            )
            self._threads.append(t_poll)

        # Config responder (ZMQ REP) in its own daemon thread
        def _config_responder():
            ctx  = zmq.Context.instance()
            sock = ctx.socket(zmq.REP)
            sock.bind(f"tcp://*:{self.CONFIG_REQ_PORT}")
            sock.setsockopt(zmq.RCVTIMEO, 1000)  # 1s poll for shutdown check
            while not self._stop_event.is_set():
                try:
                    msg = sock.recv()
                    if msg == self.CONFIG_REQ_CMD:
                        sock.send(json.dumps(self._audio_config_raw).encode("utf-8"))
                    else:
                        sock.send(b"unknown_command")
                except zmq.Again:
                    continue
                except Exception as e:
                    logger.warning("Config responder error: %s", e)
            sock.close()

        self._threads.append(
            threading.Thread(target=_config_responder, name="audio_config_rep", daemon=True)
        )

        # Start all threads
        for t in self._threads:
            t.start()

        logger.info(
            "AudioServer started. %d device(s), %d DOA/VAD poller(s). Press Ctrl+C to stop.",
            len(self._audio_devices),
            len(self._doa_vad_pollers),
        )

        self._stop_event.wait()  # Block main thread until shutdown signal

        # Graceful shutdown
        for dev    in self._audio_devices.values():   dev.stop()
        for poller in self._doa_vad_pollers.values(): poller.stop()
        logger.info("AudioServer stopped.")


def main():
    parser = argparse.ArgumentParser(
        description="TeleImager Audio Server — XVF3800 multi-channel audio streaming"
    )
    parser.add_argument(
        "--config",
        default="audio_config.yaml",
        help="Path to audio config YAML (default: audio_config.yaml)",
    )
    parser.add_argument(
        "--audio-cf",
        action="store_true",
        help="List all available audio input devices and exit",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)06d %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.audio_cf:
        devices = XVF3800AudioDevice.list_devices()
        if not devices:
            logger.info("No audio input devices found (sounddevice not available or no input devices).")
            return
        logger.info("=" * 70)
        logger.info("Audio Device Discovery")
        logger.info("=" * 70)
        for d in devices:
            tag = "  *** XVF3800 CANDIDATE ***" if d["is_xvf3800"] else ""
            logger.info(
                "  [%2d] %-45s  %2d ch @ %5d Hz%s",
                d["index"], d["name"], d["channels"], d["sample_rate"], tag,
            )
        logger.info("=" * 70)
        return

    server = AudioServer(config_path=args.config)
    server.start()


if __name__ == "__main__":
    main()
