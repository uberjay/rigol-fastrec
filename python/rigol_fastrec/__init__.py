"""rigol_fastrec — Rigol MHO900-series scope as a fast streaming ADC.

Public API: the WaveRecorder facade plus the ScpiControl / Readback layers for
advanced use.
"""

from __future__ import annotations

import logging

from .config import Channel, ChannelLayout, Trigger
from .exceptions import (
    AgentError,
    ReadbackShortRead,
    RigolFastrecError,
    ScopeNotFound,
    ScopeRunTimeout,
    UnsupportedFirmware,
)
from .logconf import enable_logging, enable_scpi_logging
from .readback import Readback
from .recorder import WaveRecorder
from .scpi import ScpiControl

# Library best practice: emit log records, never configure handlers/levels.
# A NullHandler keeps an unconfigured app from emitting "No handlers" noise;
# the app opts in via enable_logging() (INFO ops / DEBUG firehose) or its own
# logging config.
logging.getLogger("rigol_fastrec").addHandler(logging.NullHandler())

__version__ = "0.1.0"

__all__ = [
    "WaveRecorder",
    "ScpiControl",
    "Readback",
    "Trigger",
    "Channel",
    "ChannelLayout",
    "enable_logging",
    "enable_scpi_logging",
    "RigolFastrecError",
    "ScopeNotFound",
    "UnsupportedFirmware",
    "ScopeRunTimeout",
    "ReadbackShortRead",
    "AgentError",
]
