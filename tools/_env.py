"""Load the service's environment file, so the tools test what the app runs.

`Settings()` reads the process environment. systemd reads
`/etc/moto-route.env`. A diagnostic run from a plain shell therefore sees none
of the deployment's configuration — which was harmless while every setting had
a sensible default, and stopped being harmless the moment one of them named a
self-hosted Overpass.

Without this, `check_services.py` on a box with a local instance quietly
measures the *public* servers and prints PASS, and the timings look like
evidence that the local setup works. A diagnostic that confidently checks the
wrong thing is worse than no diagnostic.

Anything already set in the environment wins, so a one-off override on the
command line still behaves as expected.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = Path("/etc/moto-route.env")


def load(path: Path = ENV_FILE) -> list[str]:
    """Apply KEY=value lines from the unit's env file. Returns the names set."""
    if not path.is_file():
        return []

    applied: list[str] = []
    try:
        text = path.read_text()
    except OSError:
        # Readable by root only on some setups. Not fatal: the caller reports
        # which endpoints it is really using, so a miss is visible, not silent.
        return []

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key.startswith("MOTO_") or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")
        applied.append(key)
    return applied
