"""Header decoding checks byte order, frame identity, and physical channel lanes."""
from pathlib import Path
import struct

import numpy as np
import pytest

TOOLS = Path(__file__).resolve().parents[2]/'tools'
if not TOOLS.exists():
    pytest.skip('scope tools are checkout-only', allow_module_level=True)

import tools.diagnostics.replay_headers as probe


def block(stride=4):
    # First header is deliberately unrelated. Frame index wraps at 16 bits;
    # timestamp words are high-to-low, each word little-endian on the wire.
    headers = [(0xfa05, 0, 0, 0, 65535, 0xeeee, 0xdddd, 0xcccc),
               (0xfa05, 0, 0, 0, 0, 0x1234, 0x5678, 0x9abc),
               (0xfa05, 0, 0, 0, 1, 0x1234, 0x5678, 0x9def)]
    values = np.arange(3*2*stride, dtype='<u2').reshape(3, 2, stride) + 100
    data = b''.join(struct.pack('<8H', *h)+v.tobytes() for h, v in zip(headers, values))
    return data, values


def decode(data, stride=4, channels=(1, 2, 4)):
    return probe.decode_block(data, first=65535, count=3, samples=2,
        layout=dict(stride=stride, enabledList=list(channels)), last_tag=(1<<60)+0x123456789fff)


def test_timestamp_word_order_shift_final_register_and_index_wrap():
    headers, ticks, values = decode(block()[0])
    assert [h[4] for h in headers] == [65535, 0, 1]
    assert ticks == [0x123456789abc, 0x123456789def, 0x123456789fff]
    np.testing.assert_array_equal(values[4], [[103, 107], [111, 115], [119, 123]])


@pytest.mark.parametrize('stride,channels', [(1, (3,)), (2, (1, 3))])
def test_compacted_lanes(stride, channels):
    data, expected = block(stride)
    _, _, values = decode(data, stride, channels)
    for lane, ch in enumerate(channels):
        np.testing.assert_array_equal(values[ch], expected[:, :, lane])


@pytest.mark.parametrize('delta', [-1, 1])
def test_rejects_truncated_or_extra_dma_bytes(delta):
    data = block()[0]
    data = data[:-1] if delta < 0 else data+b'\0'
    with pytest.raises(ValueError, match='incomplete'):
        decode(data)


@pytest.mark.parametrize('offset', [0, 8, 32, 40])
def test_rejects_bad_marker_or_frame_index(offset):
    data = bytearray(block()[0])
    data[offset] ^= 1
    with pytest.raises(ValueError, match='marker or frame index'):
        decode(data)
