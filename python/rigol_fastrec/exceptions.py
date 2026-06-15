"""Typed exceptions for rigol_fastrec."""

from __future__ import annotations


class RigolFastrecError(Exception):
    """Base class for all rigol_fastrec errors."""


class ScopeNotFound(RigolFastrecError):
    """The scope / its Frida agent process could not be reached."""


class UnsupportedFirmware(RigolFastrecError):
    """The scope or its libscope-auklet.so is not a supported MHO900 build.

    Raised by the host ``*IDN?`` check and surfaced from the agent's
    fingerprint check. Fail-closed: we refuse to run rather than apply
    hardcoded firmware offsets to an unknown binary.
    """


class ScopeRunTimeout(RigolFastrecError):
    """WaveRecord did not come up (FMAX never reached 1) within the deadline."""


class ReadbackShortRead(RigolFastrecError):
    """A readback chunk returned fewer frames than requested and could not be
    recovered by re-issuing the SetRun."""


class AgentError(RigolFastrecError):
    """The Frida agent reported an error. Carries any structured detail the
    agent attached (e.g. failOffset/retries)."""

    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}
