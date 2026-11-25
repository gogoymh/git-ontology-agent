"""Minimal `.env` loader (no dependency). Existing environment variables always win."""
from __future__ import annotations

import os
import re
from typing import Dict, List

from . import log

_log = log.get("env")
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def parse(path: str) -> Dict[str, str]:
    """KEY=value pairs of an env file ({} when it is missing). Quotes and trailing ` # comments` are stripped."""
    out: Dict[str, str] = {}
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = _LINE.match(line)
            if not m:
                continue
            key, val = m.groups()
            if val[:1] in "\"'" and val.endswith(val[:1]) and len(val) > 1:
                val = val[1:-1]
            else:
                val = val.split(" #", 1)[0].rstrip()
            if val:
                out[key] = val
    return out


def load(path: str = ".env") -> List[str]:
    """Set each KEY=value from `path` that is not already set; returns the keys it set."""
    if not os.path.isfile(path):
        return []
    added = []
    for key, val in parse(path).items():
        if not os.environ.get(key):
            os.environ[key] = val
            added.append(key)
    _log.info("loaded %d variables from %s", len(added), os.path.abspath(path))
    return added
