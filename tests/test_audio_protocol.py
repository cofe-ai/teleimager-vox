"""Tests for the ZMQ audio frame wire protocol."""
import struct
import numpy as np
import pytest

# Import constants from server (single source of truth)
from teleimager.audio_server import (
    AUDIO_HEADER_FMT, AUDIO_HEADER_SIZE, AUDIO_MAGIC, CODEC_PCM
)


def test_header_size():
    """Header must be exactly 20 bytes."""
    assert AUDIO_HEADER_SIZE == 20
    assert struct.calcsize(AUDIO_HEADER_FMT) == 20


def test_header_roundtrip():
    """Pack and unpack a header — all fields must survive."""
    ts   = 123456789
    sr   = 16000
    plen = 61440
    hdr  = struct.pack(AUDIO_HEADER_FMT, AUDIO_MAGIC, 1, 6, CODEC_PCM, ts, sr, plen)
    magic, ver, ch, codec, ts2, sr2, plen2 = struct.unpack(AUDIO_HEADER_FMT, hdr)
    assert magic  == AUDIO_MAGIC
    assert ver    == 1
    assert ch     == 6
    assert codec  == CODEC_PCM
    assert ts2    == ts
    assert sr2    == sr
    assert plen2  == plen


def test_pcm_payload_size():
    """float32 PCM payload for 2560 samples × 6 channels must be 61440 bytes."""
    pcm     = np.zeros((2560, 6), dtype=np.float32)
    payload = pcm.tobytes()
    assert len(payload) == 2560 * 6 * 4  # == 61440


def test_full_frame_length():
    """Complete frame (header + PCM payload) must be 61460 bytes."""
    pcm     = np.zeros((2560, 6), dtype=np.float32)
    payload = pcm.tobytes()
    hdr     = struct.pack(AUDIO_HEADER_FMT, AUDIO_MAGIC, 1, 6, CODEC_PCM, 0, 16000, len(payload))
    assert len(hdr + payload) == AUDIO_HEADER_SIZE + 2560 * 6 * 4  # 20 + 61440 = 61460
