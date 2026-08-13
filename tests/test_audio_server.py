"""Tests for AudioServer, BaseAudioDevice, XVF3800AudioDevice, XVF3800DoaVadPoller."""
import struct
import threading
import time
import textwrap
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from teleimager.audio_server import (
    BaseAudioDevice,
    XVF3800AudioDevice,
    XVF3800DoaVadPoller,
    AudioServer,
    AUDIO_HEADER_SIZE,
    AUDIO_MAGIC,
    AUDIO_HEADER_FMT,
    CODEC_PCM,
)


# ---------------------------------------------------------------------------
# Test 1: AUDIO_HEADER_SIZE constant is exactly 20
# ---------------------------------------------------------------------------

def test_header_size_constant():
    assert AUDIO_HEADER_SIZE == 20


# ---------------------------------------------------------------------------
# Test 2: BaseAudioDevice can be initialised via XVF3800AudioDevice
# ---------------------------------------------------------------------------

def test_base_audio_device_init():
    """Instantiate via XVF3800AudioDevice (concrete subclass of BaseAudioDevice)."""
    cfg = {
        "zmq_port":      55560,
        "channels":      6,
        "sample_rate":   16000,
        "chunk_samples": 2560,
    }
    dev = XVF3800AudioDevice("test_topic", cfg)
    assert dev.topic          == "test_topic"
    assert dev.zmq_port       == 55560
    assert dev.channels       == 6
    assert dev.sample_rate    == 16000
    assert dev.chunk_samples  == 2560
    assert dev.pcm_ring_buffer is not None
    assert dev._running       is False


# ---------------------------------------------------------------------------
# Test 5: _zmq_pub calls publish with the right port + header bytes
# ---------------------------------------------------------------------------

def test_zmq_pub_publishes_frame():
    """_zmq_pub reads from ring buffer and calls publish at least once."""
    mock_pub_mgr = MagicMock()

    with patch(
        "teleimager.audio_server.ZMQ_PublisherManager.get_instance",
        return_value=mock_pub_mgr,
    ):
        dev = XVF3800AudioDevice("t", {"zmq_port": 55560, "channels": 6,
                                       "sample_rate": 16000, "chunk_samples": 2560})
        dev.pcm_ring_buffer.write({
            "pcm":          np.zeros((2560, 6), dtype=np.float32),
            "timestamp_ns": 12345,
        })
        dev._running = True

        t = threading.Thread(target=dev._zmq_pub, daemon=True)
        t.start()
        time.sleep(0.3)
        dev._running = False
        t.join(timeout=1.0)

    assert mock_pub_mgr.publish.call_count >= 1
    first_call_args = mock_pub_mgr.publish.call_args_list[0][0]
    frame_bytes = first_call_args[0]
    assert first_call_args[1] == dev.zmq_port
    assert len(frame_bytes) >= AUDIO_HEADER_SIZE


# ---------------------------------------------------------------------------
# Test 6: First 2 bytes of published frame carry the correct AUDIO_MAGIC
# ---------------------------------------------------------------------------

def test_zmq_pub_magic_in_header():
    """Published frame starts with AUDIO_MAGIC as little-endian u16."""
    mock_pub_mgr = MagicMock()

    with patch(
        "teleimager.audio_server.ZMQ_PublisherManager.get_instance",
        return_value=mock_pub_mgr,
    ):
        dev = XVF3800AudioDevice("t", {"zmq_port": 55560, "channels": 6,
                                       "sample_rate": 16000, "chunk_samples": 2560})
        dev.pcm_ring_buffer.write({
            "pcm":          np.zeros((2560, 6), dtype=np.float32),
            "timestamp_ns": 99,
        })
        dev._running = True

        t = threading.Thread(target=dev._zmq_pub, daemon=True)
        t.start()
        time.sleep(0.3)
        dev._running = False
        t.join(timeout=1.0)

    frame_bytes = mock_pub_mgr.publish.call_args_list[0][0][0]
    magic_val   = struct.unpack_from("<H", frame_bytes, 0)[0]
    assert magic_val == AUDIO_MAGIC


# ---------------------------------------------------------------------------
# Test 7: XVF3800AudioDevice defaults
# ---------------------------------------------------------------------------

def test_xvf3800_audio_device_defaults():
    """Empty config yields sensible defaults."""
    dev = XVF3800AudioDevice("cam", {})
    assert dev.channels      == 6
    assert dev.sample_rate   == 16000
    assert dev.chunk_samples == 2560
    assert dev._device_id    is None


# ---------------------------------------------------------------------------
# Test 8: XVF3800DoaVadPoller sets available=False when xvf3800 package unavailable
# ---------------------------------------------------------------------------

def test_doa_vad_poller_unavailable_when_xvf3800_missing():
    with patch("teleimager.audio_server._XVF3800_AVAILABLE", False):
        poller = XVF3800DoaVadPoller("t", {})
    assert poller.available is False


# ---------------------------------------------------------------------------
# Test 9: AudioServer loads config and populates _audio_devices
# ---------------------------------------------------------------------------

_HEAD_AUDIO_YAML = textwrap.dedent("""\
    head_audio:
      zmq_port: 55560
      channels: 6
      sample_rate: 16000
      chunk_samples: 2560
      enable_zmq: true
      doa_vad:
        enable: false
""")


def test_audio_server_loads_config(tmp_path):
    yaml_file = tmp_path / "audio_cfg.yaml"
    yaml_file.write_text(_HEAD_AUDIO_YAML)
    server = AudioServer(config_path=str(yaml_file))
    assert "head_audio" in server._audio_devices


# ---------------------------------------------------------------------------
# Test 10: AudioServer creates XVF3800DoaVadPoller when doa_vad.enable=true
# ---------------------------------------------------------------------------

_HEAD_AUDIO_DOA_YAML = textwrap.dedent("""\
    head_audio:
      zmq_port: 55560
      channels: 6
      sample_rate: 16000
      chunk_samples: 2560
      enable_zmq: true
      doa_vad:
        enable: true
        zmq_port: 55561
""")


def test_audio_server_creates_doa_vad_poller(tmp_path):
    yaml_file = tmp_path / "audio_cfg_doa.yaml"
    yaml_file.write_text(_HEAD_AUDIO_DOA_YAML)
    server = AudioServer(config_path=str(yaml_file))
    assert "head_audio" in server._doa_vad_pollers
