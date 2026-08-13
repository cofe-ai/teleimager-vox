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
import struct
import logging
import threading
import argparse
from dataclasses import dataclass, field
from typing import Optional, List

import numpy as np
import zmq

from teleimager.image_client import TripleRingBuffer, SimpleFPSMonitor

logger = logging.getLogger(__name__)

# Must match audio_server.py exactly
# ZMQ audio frame header (little-endian, 20 bytes total):
# magic(u16=2B) | version(u8=1B) | channels(u8=1B) | codec(u8=1B) | pad(1B)
# | timestamp_ns(i64=8B) | sample_rate(u16=2B) | payload_len(u32=4B)
# Layout: H(2)+B(1)+B(1)+B(1)+x(1)+q(8)+H(2)+I(4) = 20 bytes
AUDIO_HEADER_FMT  = "<HBBBxqHI"
AUDIO_HEADER_SIZE = struct.calcsize(AUDIO_HEADER_FMT)   # 20
AUDIO_MAGIC       = 0xA0D1
CODEC_PCM         = 0x01

AUDIO_CONFIG_REQ_PORT = 55559
AUDIO_CONFIG_REQ_CMD  = b"get_audio_config"


# ========================================================
# Data classes
# ========================================================

@dataclass
class TeleAudio:
    """Single audio frame received from the audio server."""
    fps:          float             = 0.0
    pcm:          Optional[np.ndarray] = None  # shape: [chunk_samples, channels] float32, or [chunk_samples, N] if selected_channels
    timestamp_ns: int               = 0
    sample_rate:  int               = 16000
    channels:     int               = 6


@dataclass
class TeleDoaVad:
    """DOA/VAD metadata received from the audio server."""
    doa:          int   = 0      # direction-of-arrival in degrees (0-359)
    vad:          bool  = False  # voice activity detection result
    timestamp_ns: int   = 0
    topic:        str   = ""


# ========================================================
# Background subscriber threads
# ========================================================

class _AudioZMQSubscriberThread(threading.Thread):
    """Background thread that subscribes to ZMQ PUB audio stream and writes to a TripleRingBuffer."""

    def __init__(self, host, port, ring_buffer, fps_monitor, selected_channels=None):
        super().__init__(daemon=True)
        self._host     = host
        self._port     = port
        self._ring     = ring_buffer
        self._fps      = fps_monitor
        self._selected = selected_channels  # list of int channel indices, or None for all
        self._running  = True
        ctx  = zmq.Context.instance()
        self._sock = ctx.socket(zmq.SUB)
        self._sock.connect(f"tcp://{host}:{port}")
        self._sock.setsockopt_string(zmq.SUBSCRIBE, "")
        self._sock.setsockopt(zmq.RCVTIMEO, 100)  # 100ms timeout for shutdown check

    def run(self):
        while self._running:
            try:
                msg = self._sock.recv()
            except zmq.Again:
                continue
            except Exception as e:
                logger.warning("Audio subscriber recv error: %s", e)
                continue

            if len(msg) < AUDIO_HEADER_SIZE:
                continue

            magic, version, channels, codec, ts_ns, sample_rate, payload_len = \
                struct.unpack_from(AUDIO_HEADER_FMT, msg, 0)

            if magic != AUDIO_MAGIC:
                logger.debug("Audio frame: bad magic 0x%X, skipping", magic)
                continue

            payload = msg[AUDIO_HEADER_SIZE:]

            if codec == CODEC_PCM:
                try:
                    pcm = np.frombuffer(payload, dtype=np.float32).reshape(-1, channels)
                    if self._selected is not None:
                        pcm = pcm[:, self._selected]
                    self._ring.write(TeleAudio(
                        fps=self._fps.fps,
                        pcm=pcm,
                        timestamp_ns=ts_ns,
                        sample_rate=sample_rate,
                        channels=pcm.shape[1] if pcm.ndim == 2 else 1,
                    ))
                    self._fps.tick()
                except Exception as e:
                    logger.warning("Audio frame decode error: %s", e)
            else:
                logger.debug("Unknown codec 0x%X, skipping", codec)

    def stop(self):
        self._running = False
        self._sock.close()


class _DoaVadZMQSubscriberThread(threading.Thread):
    """Background thread that subscribes to ZMQ PUB DOA/VAD metadata stream."""

    def __init__(self, host, port, ring_buffer):
        super().__init__(daemon=True)
        self._port    = port
        self._ring    = ring_buffer
        self._running = True
        ctx  = zmq.Context.instance()
        self._sock = ctx.socket(zmq.SUB)
        self._sock.connect(f"tcp://{host}:{port}")
        self._sock.setsockopt_string(zmq.SUBSCRIBE, "")
        self._sock.setsockopt(zmq.RCVTIMEO, 200)

    def run(self):
        while self._running:
            try:
                msg = self._sock.recv()
            except zmq.Again:
                continue
            except Exception as e:
                logger.warning("DOA/VAD subscriber recv error: %s", e)
                continue
            try:
                data = json.loads(msg.decode("utf-8"))
                self._ring.write(TeleDoaVad(
                    doa=int(data.get("doa", 0)),
                    vad=bool(data.get("vad", False)),
                    timestamp_ns=int(data.get("timestamp_ns", 0)),
                    topic=str(data.get("topic", "")),
                ))
            except Exception as e:
                logger.warning("DOA/VAD decode error: %s", e)

    def stop(self):
        self._running = False
        self._sock.close()


# ========================================================
# AudioClient
# ========================================================

class AudioClient:
    """Client for receiving audio streams from a teleimager-audio-server.

    Mirrors ImageClient API style: connect, subscribe in background threads,
    expose get_*() methods that return the latest frame from a TripleRingBuffer.

    Public API methods are marked # public api
    """

    def __init__(self, host: str, request_pcm: bool = False, selected_channels=None):
        """
        Args:
            host: IP address of the audio server.
            request_pcm: If True, subscribe to the PCM audio stream.
            selected_channels: List of channel indices to extract (e.g. [0,1]).
                               None means all channels. Ignored if request_pcm is False.
        """
        self._host              = host
        self._request_pcm       = request_pcm
        self._selected_channels = selected_channels
        self._audio_config      = {}
        self._audio_buffers     = {}
        self._doa_vad_buffers   = {}
        self._fps_monitors      = {}
        self._threads           = []

        # Fetch config from server
        try:
            ctx  = zmq.Context.instance()
            sock = ctx.socket(zmq.REQ)
            sock.setsockopt(zmq.RCVTIMEO, 3000)
            sock.setsockopt(zmq.SNDTIMEO, 3000)
            sock.connect(f"tcp://{host}:{AUDIO_CONFIG_REQ_PORT}")
            sock.send(AUDIO_CONFIG_REQ_CMD)
            resp = sock.recv()
            sock.close()
            self._audio_config = json.loads(resp.decode("utf-8"))
            logger.info("AudioClient: got config with topics: %s", list(self._audio_config.keys()))
        except Exception as e:
            logger.error("AudioClient: failed to fetch audio config from %s: %s", host, e)
            self._audio_config = {}

        # Subscribe to streams
        for topic, dev_cfg in self._audio_config.items():
            if not dev_cfg.get("enable_zmq", True):
                continue

            # Audio PCM stream
            buf = TripleRingBuffer()
            fps = SimpleFPSMonitor(window_size=10)
            self._audio_buffers[topic] = buf
            self._fps_monitors[topic]  = fps

            if request_pcm:
                t = _AudioZMQSubscriberThread(
                    host, dev_cfg.get("zmq_port", 55560), buf, fps, selected_channels
                )
                t.start()
                self._threads.append(t)

            # DOA/VAD stream
            doa_cfg = dev_cfg.get("doa_vad", {})
            if doa_cfg.get("enable", True):
                doa_buf = TripleRingBuffer()
                self._doa_vad_buffers[topic] = doa_buf
                t_doa = _DoaVadZMQSubscriberThread(
                    host, doa_cfg.get("zmq_port", 55561), doa_buf
                )
                t_doa.start()
                self._threads.append(t_doa)

    def get_audio_config(self) -> dict:  # public api
        """Return the full audio config dict fetched from the server."""
        return self._audio_config

    def get_audio_frame(self, topic: str) -> "TeleAudio":  # public api
        """Return the latest audio frame for a topic. Returns empty TeleAudio if no frame yet."""
        if topic not in self._audio_buffers:
            raise ValueError(f"Unknown audio topic: {topic!r}. Known: {list(self._audio_buffers.keys())}")
        frame = self._audio_buffers[topic].read()
        return frame if frame is not None else TeleAudio()

    def get_doa_vad(self, topic: str) -> "TeleDoaVad":  # public api
        """Return the latest DOA/VAD metadata for a topic."""
        if topic not in self._doa_vad_buffers:
            raise ValueError(f"Unknown DOA/VAD topic: {topic!r}. Known: {list(self._doa_vad_buffers.keys())}")
        meta = self._doa_vad_buffers[topic].read()
        return meta if meta is not None else TeleDoaVad()

    def get_head_audio(self) -> "TeleAudio":  # public api
        """Convenience method: get_audio_frame('head_audio')."""
        return self.get_audio_frame("head_audio")

    def close(self):  # public api
        """Stop all subscriber threads and release ZMQ resources."""
        for t in self._threads:
            t.stop()
        for t in self._threads:
            t.join(timeout=1.0)
        logger.info("AudioClient closed.")


# ========================================================
# CLI entry point
# ========================================================

def main():
    parser = argparse.ArgumentParser(
        description="TeleImager Audio Client — receive and display audio stream info"
    )
    parser.add_argument("--host",     required=True, help="Audio server IP address")
    parser.add_argument("--topic",    default="head_audio", help="Audio topic name (default: head_audio)")
    parser.add_argument("--duration", type=float, default=5.0, help="How long to run in seconds (default: 5.0)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)06d %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    client = AudioClient(host=args.host, request_pcm=True)
    logger.info("Connected to %s. Monitoring for %.1fs ...", args.host, args.duration)

    end_time = time.time() + args.duration
    while time.time() < end_time:
        try:
            audio = client.get_audio_frame(args.topic)
            doa   = client.get_doa_vad(args.topic)
            shape_str = str(audio.pcm.shape) if audio.pcm is not None else "None"
            logger.info(
                "[%s] fps=%.1f  shape=%s  DOA=%3d°  VAD=%s",
                args.topic, audio.fps, shape_str, doa.doa, "YES" if doa.vad else "no",
            )
        except ValueError as e:
            logger.warning("%s", e)
        time.sleep(1.0)

    client.close()


if __name__ == "__main__":
    main()
