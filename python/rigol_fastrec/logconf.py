"""App-side logging helpers (the library itself only emits records).

Two tiers, selected by level on the ``rigol_fastrec`` root logger:

* **INFO** — what the library is *doing*: connect/attach, ``configure``,
  channel layout, ``max_frames``, and each readback's frame count + MB/s. This
  is the "verbose, but not the firehose" tier.
* **DEBUG** — the above **plus** every raw SCPI command (with round-trip times)
  and the per-arm / socket detail. Use it to debug the scope conversation.

Submodule loggers (``rigol_fastrec.scpi`` / ``.readback`` / ``.recorder``)
propagate to the root, so one handler at the chosen level catches them all.
The library never configures handlers/levels itself — call ``enable_logging``
from your app/CLI, or wire your own ``logging`` config.
"""

from __future__ import annotations

import logging
import sys

_ROOT = "rigol_fastrec"
# Marks the handler we install so repeat calls replace it instead of stacking.
_OUR_HANDLER = "_rigol_fastrec_handler"


def enable_logging(level: int = logging.INFO, *, stream=None) -> logging.Logger:
    """Attach a stderr handler to the ``rigol_fastrec`` root logger at `level`.

    ``INFO`` (default) shows high-level operations without the per-command SCPI
    firehose; ``logging.DEBUG`` adds the raw SCPI commands. Idempotent — calling
    again replaces our handler (and re-sets the level) rather than stacking a
    second one. Returns the configured logger.
    """
    lg = logging.getLogger(_ROOT)
    for h in list(lg.handlers):
        if getattr(h, _OUR_HANDLER, False):
            lg.removeHandler(h)
    h = logging.StreamHandler(stream or sys.stderr)
    h.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-5s [%(name)s] %(message)s",
        "%H:%M:%S"))
    setattr(h, _OUR_HANDLER, True)
    lg.addHandler(h)
    lg.setLevel(level)
    # Our handler prints these records; don't let them propagate to the root
    # logger too (a dependency like chipwhisperer often installs a root handler,
    # which would double-print every line).
    lg.propagate = False
    return lg


def enable_scpi_logging(level: int = logging.DEBUG, *, stream=None) -> logging.Logger:
    """Deprecated alias for ``enable_logging`` (defaults to DEBUG, i.e. the raw
    SCPI firehose). Prefer ``enable_logging()`` for the INFO-tier ops view, or
    ``enable_logging(logging.DEBUG)`` for everything."""
    return enable_logging(level, stream=stream)
