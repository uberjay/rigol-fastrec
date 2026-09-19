"""Single-frame register reference shared by validators and diagnostics."""

class RegisterTimestampScript:
    """Select the full-register reference path without changing public readback."""
    def __init__(self, script):
        self.script = script
        self.exports_sync = self
        self.enabled = False
    def __getattr__(self, key):
        return getattr(self.script.exports_sync, key)
    def read_frames(self, args):
        return self.script.exports_sync.read_frames(dict(args, timestampRegisters=self.enabled))


def ticks_from_status(status, first, count):
    if status.get('timestampTickFs') != 250000 or status.get('timestampBits') != 64:
        raise AssertionError(f'wrong timestamp format: {status.keys()}')
    ticks = [int(x) for x in status['frameTimestampTicks']]
    if any(x < 0 or x >= 1 << 64 for x in ticks):
        raise AssertionError('invalid 64-bit ticks')
    if len(ticks) != count:
        raise AssertionError(f'{len(ticks)} timestamps for {count} raw frames')
    return ticks


def register_reference(rec, count, samples, channels):
    real_script = rec.readback._script
    proxy = RegisterTimestampScript(real_script)
    proxy.enabled = True
    rec.readback._script = proxy
    try:
        values = rec.readback.read(count=count, samples_per_frame=samples, channels=channels)
        ticks = ticks_from_status(rec.readback.last_read_stats, 0, count)
        return values, ticks
    finally:
        rec.readback._script = real_script
