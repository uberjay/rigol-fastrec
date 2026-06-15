"""Locate the built Frida agent bundle that ships as package data."""

from __future__ import annotations

from importlib import resources

_BUNDLE = "_agent.js"


def load_agent_source() -> str:
    """Return the bundled agent JavaScript.

    ``rigol_fastrec/_agent.js`` is committed and ships as package data (built
    from ``agent/`` with frida-compile). If it's somehow absent, raise an
    explicit, actionable error.
    """
    res = resources.files("rigol_fastrec") / _BUNDLE
    try:
        return res.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as e:
        raise RuntimeError(
            "agent bundle rigol_fastrec/_agent.js is missing — it should be "
            "committed. Rebuild it with `make build` (or `cd agent && "
            "npm install && npm run build`)."
        ) from e
