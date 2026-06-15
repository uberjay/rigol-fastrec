"""Value types: Trigger / Channel defaults and ChannelLayout lane lookup."""

import pytest

from rigol_fastrec.config import Channel, ChannelLayout, Trigger


def test_trigger_channel_defaults():
    assert Trigger().source == "CHAN2"
    assert Trigger().slope == "POS"
    assert Channel().coupling == "DC"
    assert Channel().bandwidth_limit == "OFF"


def test_channel_layout_offset_of():
    # non-contiguous enable: CH4 at dense index 2 within {1,2,4}
    lay = ChannelLayout(stride=4, enabled=(1, 2, 4),
                        offsets={1: 0, 2: 1, 4: 2}, samples_per_frame=1000)
    assert lay.offset_of(1) == 0
    assert lay.offset_of(4) == 2


def test_channel_layout_offset_of_disabled_raises():
    lay = ChannelLayout(stride=4, enabled=(1, 2, 4),
                        offsets={1: 0, 2: 1, 4: 2}, samples_per_frame=1000)
    with pytest.raises(ValueError):
        lay.offset_of(3)
