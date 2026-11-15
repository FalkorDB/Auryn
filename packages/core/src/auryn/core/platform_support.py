"""Where the local (bundled falkordb-lite) backend can and can't run.

The local backend is POSIX-only. Two independent reasons:

- falkordb-lite builds and runs a real ``redis-server`` via redislite, and FalkorDB itself is a
  Redis module (``.so``) -- there is no native-Windows ``redis-server`` to bundle.
- the daemon lifecycle in :mod:`auryn.core.server_manager` relies on ``fcntl``/``os.kill``/
  POSIX signals, none of which exist on native Windows.

WSL2 runs a *Linux* interpreter, so ``os.name`` there is ``"posix"`` and the local backend works
unchanged -- only native-Windows Python (``os.name == "nt"``) is excluded. Callers use
:func:`local_backend_supported` to gate the local path and steer a native-Windows user to a
remote backend (or WSL2), rather than letting an ``fcntl`` ImportError surface as a traceback.
"""

from __future__ import annotations

import os

# Native Windows Python. WSL2 is a Linux interpreter (os.name == "posix"), so it is NOT flagged
# here and the local backend runs there as on any Linux box.
IS_NATIVE_WINDOWS = os.name == "nt"

LOCAL_UNSUPPORTED_MESSAGE = (
    "The local (bundled falkordb-lite) backend isn't supported on native Windows -- it needs a "
    "POSIX redis-server. Options:\n"
    "  * Run Auryn inside WSL2 (a Linux environment) -- local works there unchanged.\n"
    "  * Point at a remote FalkorDB:\n"
    "      auryn install --backend remote --host HOST --port PORT\n"
    "  * Start one in Docker and point at it:\n"
    "      docker run -p 6379:6379 falkordb/falkordb\n"
    "      auryn install --backend remote --host localhost --port 6379"
)


def local_backend_supported() -> bool:
    """False on native Windows, where the local backend cannot run; True everywhere else
    (Linux, macOS, and WSL2 -- which presents as Linux)."""
    return not IS_NATIVE_WINDOWS
