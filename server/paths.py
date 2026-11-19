"""Single owner of runtime data locations.

Every store resolves its directory through :func:`data_dir`. The default is the
original ``<repo>/server/data`` location, so production behaviour is unchanged;
``OPENPOKE_DATA_DIR`` redirects all state (tests, evals, parity runs) to a
throwaway directory so nothing ever touches the user's real data.

This module must not import anything from ``server`` so it can be imported first.
"""

from __future__ import annotations

import os
from pathlib import Path

DATA_DIR_ENV = "OPENPOKE_DATA_DIR"
_DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"


def default_data_dir() -> Path:
    """Return the historical data location (``<repo>/server/data``)."""
    return _DEFAULT_DATA_DIR


def data_dir() -> Path:
    """Root for all runtime state. OPENPOKE_DATA_DIR overrides; default is <repo>/server/data (unchanged).

    The override is resolved to an absolute path and created if missing, so a
    relative or not-yet-existing directory works. The default directory is not
    created here: existing stores already create their own sub-directories.
    """
    raw = os.environ.get(DATA_DIR_ENV, "").strip()
    if not raw:
        return _DEFAULT_DATA_DIR
    path = Path(raw).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


__all__ = ["DATA_DIR_ENV", "data_dir", "default_data_dir"]
