"""Small value types for the capture configuration."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Trigger:
    """Edge-trigger configuration.

    `level` is in probe-tip volts. The trigger's source channel is enabled
    implicitly (so it counts toward the interleave stride) and its vertical
    settings are configured from the ``channel_*`` fields so the edge is
    cleanly visible — you needn't list it in ``configure(channels=...)``.
    Defaults suit a 0–3.3 V CMOS edge on a direct (1x) input.
    """
    source: str = "CHAN2"
    level: float = 1.5
    slope: str = "POS"              # "POS" | "NEG"
    channel_range: float = 8.0      # full-scale V for the trigger channel
    channel_offset: float = 0.0
    channel_coupling: str = "DC"    # "DC" | "AC"
    channel_probe: float = 1.0      # attenuation ratio (10.0 for a 10x probe)


@dataclass(frozen=True)
class Channel:
    """Per-channel vertical settings for an enabled channel."""
    range: float = 0.5              # full-scale volts (8 div × V/div)
    coupling: str = "DC"            # "DC" | "AC"
    probe: float = 1.0              # attenuation ratio
    offset: float = 0.0             # volts
    bandwidth_limit: str = "OFF"    # "OFF" | "20M" | "250M" (model-dependent)


@dataclass(frozen=True)
class ChannelLayout:
    """How the FPGA interleaves the enabled channels into one stream.

    The engine emits ``stride`` interleaved samples per tick; each enabled
    channel sits at a fixed index (`offsets[ch]`) within that stride. The
    agent deinterleaves a requested channel by its offset.
    """
    stride: int                      # = number of enabled channels
    enabled: tuple[int, ...]         # enabled channel numbers, ascending
    offsets: dict[int, int]          # channel number -> index within a stride
    samples_per_frame: int           # per-channel samples (= MDEP; it's per channel)

    def offset_of(self, channel: int) -> int:
        try:
            return self.offsets[channel]
        except KeyError:
            raise ValueError(
                f"channel {channel} not enabled (enabled={list(self.enabled)})"
            ) from None
