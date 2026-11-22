"""The shared SCIP runner plumbing (`core.scip_runner`): trusted-tool resolution, the in-tree containment
refusal, and the indexer subprocess wrapper. These are the security-sensitive bits every language's deep
pass shares, so they're pinned here directly rather than only through each language's deep suite."""

from __future__ import annotations

import os
import stat
import sys

import pytest
from auryn.core import scip_runner
from auryn.core.scip_runner import ScipIndexerUnavailable


class _Boom(ScipIndexerUnavailable):
    pass


def _make_exe(path: str) -> str:
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


# -- is_inside -----------------------------------------------------------------------------------

def test_is_inside_detects_containment_and_symlinks(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    inside = root / "sub" / "tool"
    inside.write_text("x")
    assert scip_runner.is_inside(str(inside), str(root)) is True
    outside = tmp_path / "elsewhere" / "tool"
    outside.parent.mkdir()
    outside.write_text("x")
    assert scip_runner.is_inside(str(outside), str(root)) is False
    link = tmp_path / "link_to_inside"  # a symlink that points INTO root must be caught
    link.symlink_to(inside)
    assert scip_runner.is_inside(str(link), str(root)) is True


# -- resolve_trusted_tool ------------------------------------------------------------------------

def test_env_override_wins_and_is_realpathed(tmp_path, monkeypatch):
    real = _make_exe(tmp_path / "scip-thing")
    monkeypatch.setenv("AURYN_THING", real)
    assert scip_runner.resolve_trusted_tool(
        env_var="AURYN_THING", which="scip-thing", unavailable=_Boom, hint="h") == [os.path.realpath(real)]


def test_path_lookup_used_only_when_which_given(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _make_exe(bindir / "scip-thing")
    monkeypatch.delenv("AURYN_THING", raising=False)
    monkeypatch.setenv("PATH", str(bindir))
    # which given -> found on PATH
    got = scip_runner.resolve_trusted_tool(env_var="AURYN_THING", which="scip-thing", unavailable=_Boom, hint="h")
    assert got == [os.path.realpath(bindir / "scip-thing")]
    # which=None -> opt-in only, PATH is NOT consulted even though the binary is right there
    with pytest.raises(_Boom):
        scip_runner.resolve_trusted_tool(env_var="AURYN_THING", which=None, unavailable=_Boom, hint="opt-in")


def test_missing_tool_raises_the_given_subclass_with_hint(monkeypatch):
    monkeypatch.delenv("AURYN_THING", raising=False)
    monkeypatch.setenv("PATH", "")
    with pytest.raises(_Boom, match="install me"):
        scip_runner.resolve_trusted_tool(env_var="AURYN_THING", which="nope-xyz", unavailable=_Boom, hint="install me")


def test_env_override_to_a_non_file_falls_through(tmp_path, monkeypatch):
    monkeypatch.setenv("AURYN_THING", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("PATH", "")
    with pytest.raises(_Boom):
        scip_runner.resolve_trusted_tool(env_var="AURYN_THING", which="nope", unavailable=_Boom, hint="h")


# -- run_indexer ---------------------------------------------------------------------------------

def _writes(out_path: str, data: str, exit_code: int = 0) -> list[str]:
    return [sys.executable, "-c",
            f"open({out_path!r},'wb').write({data!r}.encode()); import sys; sys.exit({exit_code})"]


def test_run_indexer_returns_the_index_bytes(tmp_path):
    out = tmp_path / "index.scip"
    got = scip_runner.run_indexer(_writes(str(out), "SCIP"), cwd=str(tmp_path), out_path=str(out),
                                  timeout=30, tool="t", unavailable=_Boom)
    assert got == b"SCIP"


def test_run_indexer_rejects_nonzero_exit_only_when_required(tmp_path):
    out = tmp_path / "index.scip"
    argv = _writes(str(out), "PARTIAL", exit_code=2)
    # lenient (go): a non-zero exit with output present is accepted
    assert scip_runner.run_indexer(argv, cwd=str(tmp_path), out_path=str(out), timeout=30,
                                   tool="t", unavailable=_Boom) == b"PARTIAL"
    # strict (clang): a non-zero exit is a real failure
    with pytest.raises(_Boom, match="exit 2"):
        scip_runner.run_indexer(argv, cwd=str(tmp_path), out_path=str(out), timeout=30,
                                tool="t", unavailable=_Boom, require_zero_exit=True)


def test_run_indexer_rejects_missing_or_empty_output(tmp_path):
    out = tmp_path / "index.scip"
    with pytest.raises(_Boom):  # the subprocess writes nothing
        scip_runner.run_indexer([sys.executable, "-c", "pass"], cwd=str(tmp_path), out_path=str(out),
                                timeout=30, tool="t", unavailable=_Boom)
    with pytest.raises(_Boom):  # an empty index file is not usable
        scip_runner.run_indexer(_writes(str(out), ""), cwd=str(tmp_path), out_path=str(out),
                                timeout=30, tool="t", unavailable=_Boom)


def test_run_indexer_wraps_a_missing_executable(tmp_path):
    out = tmp_path / "index.scip"
    with pytest.raises(_Boom, match="could not execute"):
        scip_runner.run_indexer([str(tmp_path / "nope")], cwd=str(tmp_path), out_path=str(out),
                                timeout=30, tool="t", unavailable=_Boom)
