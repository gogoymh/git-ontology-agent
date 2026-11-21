"""One English logger for every module. GOA_LOG=debug|info|warning (default warning)."""
from __future__ import annotations

import logging
import os

_configured = False


def get(name: str) -> logging.Logger:
    global _configured
    if not _configured:
        level = os.environ.get("GOA_LOG", "warning").upper()
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root = logging.getLogger("goa")
        root.addHandler(handler)
        root.setLevel(getattr(logging, level, logging.WARNING))
        root.propagate = False
        _configured = True
    return logging.getLogger("goa." + name)
