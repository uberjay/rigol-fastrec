"""Host-side unpacking of the 12-bit packed wire encoding (`transport="packed"`).

The agent packs the top 12 bits of each code 2-samples-per-3-bytes; `_unpack12`
must invert that and reconstruct to uint16 in the 16-bit domain (low 4 bits
zeroed), so the result is `code & 0xFFF0`.
"""

import numpy as np
import pytest

from rigol_fastrec.readback import _unpack12


def _agent_pack12(codes: np.ndarray) -> bytes:
    """Mirror the agent's fastrec_send_frames(outBits=12) packing in Python."""
    s = (codes >> 4).astype(np.uint16)          # top 12 bits
    out = bytearray()
    n = len(s)
    for j in range(0, n - (n & 1), 2):
        s0, s1 = int(s[j]), int(s[j + 1])
        out += bytes([s0 & 0xFF, (s0 >> 8) | ((s1 & 0x0F) << 4), s1 >> 4])
    if n & 1:                                   # odd tail: pad 2nd sample with 0
        s0 = int(s[-1])
        out += bytes([s0 & 0xFF, s0 >> 8, 0])
    return bytes(out)


@pytest.mark.parametrize("n", [1, 2, 3, 7, 8, 9, 255, 1000, 1001])
def test_unpack12_roundtrip(n):
    codes = np.random.default_rng(n).integers(0, 65536, n, dtype=np.uint16)
    packed = _agent_pack12(codes)
    assert len(packed) == ((n + 1) // 2) * 3        # 1.5 bytes/sample (+pad)
    got = _unpack12(packed, n)
    assert got.dtype == np.uint16
    assert np.array_equal(got, codes & 0xFFF0)      # top 12 bits, <<4 back


def test_unpack12_byte_layout():
    # Pin the wire layout: s0=0xABC, s1=0x123 (12-bit) → b0,b1,b2 below.
    packed = bytes([0xBC, 0x3A, 0x12])
    got = _unpack12(packed, 2)
    assert list(got) == [0xABC0, 0x1230]


def test_unpack12_full_scale_extremes():
    codes = np.array([0x0000, 0xFFFF, 0x8000], dtype=np.uint16)
    got = _unpack12(_agent_pack12(codes), len(codes))
    assert list(got) == [0x0000, 0xFFF0, 0x8000]
