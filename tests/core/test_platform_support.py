"""Native Windows can't run the local (falkordb-lite) backend, so the local path is gated:
ensure_server()/run_server() raise a clear error and install's prompt drops the local option --
rather than falling through to an fcntl ImportError. WSL2 presents as Linux (os.name == "posix")
and is *not* flagged, so the local backend works there unchanged.

These simulate native Windows by flipping platform_support.IS_NATIVE_WINDOWS, so they run on any
host and don't need the embedded extra installed (the whole point is that they never reach it).
"""

from __future__ import annotations

import click
import pytest

from auryn.core import platform_support
from auryn.core import server_manager as sm
from auryn.core.app_config import Backend
from auryn.core.platform_support import local_backend_supported


@pytest.fixture
def native_windows(monkeypatch):
    """Make local_backend_supported() report native Windows for the duration of a test."""
    monkeypatch.setattr(platform_support, "IS_NATIVE_WINDOWS", True)
    assert not local_backend_supported()


def test_supported_off_native_windows():
    # Mirrors the real host: local is supported everywhere EXCEPT native Windows. On the POSIX
    # runners (Linux/macOS/WSL2) that's True; on the native-Windows CI runner it's False -- either
    # way local_backend_supported() must agree with platform_support.IS_NATIVE_WINDOWS.
    assert local_backend_supported() is (not platform_support.IS_NATIVE_WINDOWS)


def test_ensure_server_local_raises_on_native_windows(native_windows, tmp_path, monkeypatch):
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "cg"))
    with pytest.raises(RuntimeError, match="native Windows"):
        sm.ensure_server(Backend(mode="local"))
    assert sm._read_server_info() is None  # never tried to start / write a rendezvous


def test_ensure_server_remote_still_works_on_native_windows(native_windows):
    # Remote has no POSIX dependency -- it must keep working on native Windows.
    cfg = sm.ensure_server(Backend(mode="remote", host="db.example", port=6400, ssl=True))
    assert (cfg.host, cfg.port, cfg.ssl) == ("db.example", 6400, True)


def test_run_server_raises_on_native_windows(native_windows):
    with pytest.raises(RuntimeError, match="native Windows"):
        sm.run_server()


def test_prompt_backend_forces_remote_interactively_on_native_windows(native_windows, monkeypatch):
    from auryn.cli import _prompt_backend

    # No --backend, interactive: local isn't offered; it goes straight to remote with the given conn.
    # host/port/user/pass are all supplied so no field triggers a stdin prompt.
    b = _prompt_backend(None, "db.example", 6400, "u", "p", False, assume_yes=False)
    assert (b.mode, b.host, b.port, b.username) == ("remote", "db.example", 6400, "u")


def test_prompt_backend_explicit_local_errors_on_native_windows(native_windows):
    from auryn.cli import _prompt_backend

    with pytest.raises(click.ClickException, match="native Windows"):
        _prompt_backend("local", None, None, None, None, False, assume_yes=False)


def test_prompt_backend_yes_without_remote_errors_on_native_windows(native_windows):
    from auryn.cli import _prompt_backend

    # -y can't silently pick local on Windows and has no remote to fall back to -> clear error.
    with pytest.raises(click.ClickException, match="auto-select"):
        _prompt_backend(None, None, None, None, None, False, assume_yes=True)


def test_prompt_backend_yes_with_explicit_remote_ok_on_native_windows(native_windows):
    from auryn.cli import _prompt_backend

    b = _prompt_backend("remote", "h", 6379, None, None, False, assume_yes=True)
    assert (b.mode, b.host, b.port) == ("remote", "h", 6379)


def test_module_imports_and_guards_with_fcntl_absent():
    """The one real-native-Windows condition the monkeypatch tests can't hit on POSIX: there
    `import fcntl` *raises*. Reproduce it (sys.modules['fcntl'] = None) in a subprocess -- so
    blocking the import can't pollute this interpreter -- and confirm server_manager still imports
    (fcntl -> None) and the local guard fires while remote keeps working."""
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent(
        """
        import sys
        sys.modules["fcntl"] = None  # make `import fcntl` raise ImportError, exactly as on Windows
        import auryn.core.platform_support as ps
        ps.IS_NATIVE_WINDOWS = True
        import auryn.core.server_manager as sm  # must import despite fcntl being unavailable
        from auryn.core.app_config import Backend

        assert sm.fcntl is None
        try:
            sm.ensure_server(Backend(mode="local"))
            raise SystemExit("local backend did not raise")
        except RuntimeError:
            pass
        assert sm.ensure_server(Backend(mode="remote", host="h", port=6379)).host == "h"
        print("OK")
        """
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr
