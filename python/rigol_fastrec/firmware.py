"""Host-side firmware check.

Parses ``*IDN?`` and decides whether this scope is a supported MHO900-series
build *before* we attach Frida or touch the scope. The deeper check — the
agent fingerprinting libscope-auklet.so — lives in the agent.

Pure functions, so the check is unit-testable without a scope.
"""

from __future__ import annotations

from .exceptions import UnsupportedFirmware

#: Series prefix we target. The fail-closed scope: MHO900 only.
SERIES_PREFIX = "MHO9"

#: (model, firmware_version) pairs we have validated. Seeded with the only
#: hardware in scope; grow it as new builds are validated.
SUPPORTED_FIRMWARE: frozenset[tuple[str, str]] = frozenset({
    ("MHO98", "00.01.00"),
})


def parse_idn(idn: str) -> tuple[str, str, str, str]:
    """Split a SCPI ``*IDN?`` reply into (vendor, model, serial, firmware).

    Rigol form: ``RIGOL TECHNOLOGIES,MHO98,MHO9A27CM00217,00.01.00``.
    """
    parts = [p.strip() for p in idn.strip().split(",")]
    if len(parts) < 4:
        raise UnsupportedFirmware(f"unparseable *IDN?: {idn!r}")
    vendor, model, serial, firmware = parts[0], parts[1], parts[2], parts[3]
    return vendor, model, serial, firmware


def check_idn(idn: str) -> tuple[str, str]:
    """Check ``*IDN?``. Returns (model, firmware) if supported, else raises
    UnsupportedFirmware. Targets the MHO900 series only."""
    _vendor, model, _serial, firmware = parse_idn(idn)
    if not model.startswith(SERIES_PREFIX):
        raise UnsupportedFirmware(
            f"{model!r} is not an {SERIES_PREFIX}xx (MHO900-series) scope; "
            f"rigol-fastrec targets that series only.")
    if (model, firmware) not in SUPPORTED_FIRMWARE:
        supported = ", ".join(f"{m}/{f}" for m, f in sorted(SUPPORTED_FIRMWARE))
        raise UnsupportedFirmware(
            f"{model}/{firmware} is not a validated build (supported: "
            f"{supported}). The agent will refuse to run on it; add a "
            f"validated profile first.")
    return model, firmware
