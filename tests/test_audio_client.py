"""Tests for AudioClient, TeleAudio, TeleDoaVad, subscriber threads."""
import json
import struct
import threading
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import zmq

from teleimager.audio_client import (
    TeleAudio,
    TeleDoaVad,
    _AudioZMQSubscriberThread,
    _DoaVadZMQSubscriberThread,
    AudioClient,
    AUDIO_HEADER_FMT,
    AUDIO_HEADER_SIZE,
    AUDIO_MAGIC,
    CODEC_PCM,
)
from teleimager.image_client import TripleRingBuffer, SimpleFPSMonitor


# ---------------------------------------------------------------------------
# Test 1: TeleAudio default values
# ---------------------------------------------------------------------------

def test_tele_audio_defaults():
    a = TeleAudio()
    assert a.fps         == 0.0
    assert a.pcm         is None
    assert a.timestamp_ns == 0
    assert a.sample_rate == 16000
    assert a.channels    == 6


# ---------------------------------------------------------------------------
# Test 2: TeleDoaVad default values
# ---------------------------------------------------------------------------

def test_tele_doa_vad_defaults():
    d = TeleDoaVad()
    assert d.doa          == 0
    assert d.vad          is False
    assert d.timestamp_ns == 0


# ---------------------------------------------------------------------------
# Helper: build a valid raw audio frame bytes
# ---------------------------------------------------------------------------

def _make_audio_frame(channels=6, chunk_samples=2560):
    pcm     = np.zeros((chunk_samples, channels), dtype=np.float32)
    payload = pcm.tobytes()
    hdr     = struct.pack(
        AUDIO_HEADER_FMT,
        AUDIO_MAGIC, 1, channels, CODEC_PCM,
        111,        # ts_ns
        16000,      # sample_rate
        len(payload),
    )
    return hdr + payload


# ---------------------------------------------------------------------------
# Test 3: _AudioZMQSubscriberThread parses a PCM frame
# ---------------------------------------------------------------------------

def test_audio_zmq_subscriber_parses_pcm_frame():
    frame = _make_audio_frame(channels=6)
    ring  = TripleRingBuffer()
    fps   = SimpleFPSMonitor(window_size=10)

    call_count = [0]

    def fake_recv():
        if call_count[0] == 0:
            call_count[0] += 1
            return frame
        raise zmq.Again

    mock_sock = MagicMock()
    mock_sock.recv.side_effect = fake_recv

    with patch("zmq.Context.instance") as mock_ctx:
        mock_ctx.return_value.socket.return_value = mock_sock
        t = _AudioZMQSubscriberThread("localhost", 55560, ring, fps)
        t.start()
        time.sleep(0.2)
        t.stop()
        t.join(timeout=1.0)

    result = ring.read()
    assert result is not None
    assert isinstance(result, TeleAudio)
    assert result.pcm.shape == (2560, 6)


# ---------------------------------------------------------------------------
# Test 4: _AudioZMQSubscriberThread with channel selection
# ---------------------------------------------------------------------------

def test_audio_zmq_subscriber_channel_selection():
    frame = _make_audio_frame(channels=6)
    ring  = TripleRingBuffer()
    fps   = SimpleFPSMonitor(window_size=10)

    call_count = [0]

    def fake_recv():
        if call_count[0] == 0:
            call_count[0] += 1
            return frame
        raise zmq.Again

    mock_sock = MagicMock()
    mock_sock.recv.side_effect = fake_recv

    with patch("zmq.Context.instance") as mock_ctx:
        mock_ctx.return_value.socket.return_value = mock_sock
        t = _AudioZMQSubscriberThread(
            "localhost", 55560, ring, fps, selected_channels=[0, 1]
        )
        t.start()
        time.sleep(0.2)
        t.stop()
        t.join(timeout=1.0)

    result = ring.read()
    assert result is not None
    assert result.pcm.shape == (2560, 2)


# ---------------------------------------------------------------------------
# Test 5: _DoaVadZMQSubscriberThread parses JSON
# ---------------------------------------------------------------------------

def test_doa_vad_subscriber_parses_json():
    payload = json.dumps({
        "doa":          270,
        "vad":          True,
        "timestamp_ns": 999,
        "topic":        "head_audio",
    }).encode()

    ring = TripleRingBuffer()

    call_count = [0]

    def fake_recv():
        if call_count[0] == 0:
            call_count[0] += 1
            return payload
        raise zmq.Again

    mock_sock = MagicMock()
    mock_sock.recv.side_effect = fake_recv

    with patch("zmq.Context.instance") as mock_ctx:
        mock_ctx.return_value.socket.return_value = mock_sock
        t = _DoaVadZMQSubscriberThread("localhost", 55561, ring)
        t.start()
        time.sleep(0.2)
        t.stop()
        t.join(timeout=1.0)

    result = ring.read()
    assert result is not None
    assert isinstance(result, TeleDoaVad)
    assert result.doa          == 270
    assert result.vad          is True
    assert result.timestamp_ns == 999


# ---------------------------------------------------------------------------
# Test 6: AudioClient.get_audio_frame raises ValueError for unknown topic
# ---------------------------------------------------------------------------

def test_audio_client_get_audio_frame_empty():
    mock_sock = MagicMock()
    mock_sock.recv.return_value = json.dumps({}).encode()

    with patch("zmq.Context.instance") as mock_ctx:
        mock_ctx.return_value.socket.return_value = mock_sock
        client = AudioClient(host="127.0.0.1")

    with pytest.raises(ValueError):
        client.get_audio_frame("nonexistent")


# ---------------------------------------------------------------------------
# Test 7: AudioClient.get_doa_vad raises ValueError for unknown topic
# ---------------------------------------------------------------------------

def test_audio_client_get_doa_vad_empty():
    mock_sock = MagicMock()
    mock_sock.recv.return_value = json.dumps({}).encode()

    with patch("zmq.Context.instance") as mock_ctx:
        mock_ctx.return_value.socket.return_value = mock_sock
        client = AudioClient(host="127.0.0.1")

    with pytest.raises(ValueError):
        client.get_doa_vad("nonexistent")
