"""Raw timestamp experiment metadata must preserve every integer and frame."""
from pathlib import Path
import pytest
TOOLS = Path(__file__).resolve().parents[2]/'tools'
if not TOOLS.exists():
    pytest.skip('scope tools are checkout-only', allow_module_level=True)

import tools._support.timestamps as probe

def status(values):
    return dict(timestampTickFs=250000,timestampBits=64,frameTimestampTicks=values)

def test_preserves_counter_bits_above_float_precision():
    values=[(1<<60)+1,(1<<60)+2]
    assert probe.ticks_from_status(status(list(map(str,values))),0,2)==values

@pytest.mark.parametrize('value',[-1,1<<64])
def test_rejects_out_of_range_counters(value):
    with pytest.raises(AssertionError,match='invalid 64-bit'):
        probe.ticks_from_status(status([str(value)]),0,1)

def test_requires_one_timestamp_per_requested_frame():
    with pytest.raises(AssertionError,match='2 timestamps for 7 raw frames'):
        probe.ticks_from_status(status(['1','2']),10,7)

@pytest.mark.parametrize('field,value',[('timestampTickFs',1000),('timestampBits',48)])
def test_rejects_wrong_units_or_width(field,value):
    data=status(['1']);data[field]=value
    with pytest.raises(AssertionError,match='wrong timestamp format'):
        probe.ticks_from_status(data,0,1)
