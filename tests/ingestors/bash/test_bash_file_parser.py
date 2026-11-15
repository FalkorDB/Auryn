"""Unit tests for the Bash tree-sitter parser (no FalkorDB). Confirms the empirically-derived
grammar handling: both function spellings (`foo() {}` and `function foo {}`), call extraction
from function bodies (bare, nested in pipelines/conditionals/command-substitutions), builtins
skipped, unnameable (`$cmd`) calls dropped, nested-function calls not attributed to the enclosing
function, `source`/`.` includes (static + dynamic), and the shared `doc` property (leading `#`
comment, shebang stripped, blank-line gap not attached)."""

from __future__ import annotations

from auryn.ingestors.bash.file_parser import parse_source


def _parse(src: str, path: str = "t.sh"):
    return parse_source(src.encode(), path)


def test_function_both_spellings():
    rf = _parse(
        """
        foo() { echo hi; }
        function bar { echo bye; }
        """
    )
    assert {f.name for f in rf.functions} == {"foo", "bar"}


def test_calls_to_functions_and_commands():
    rf = _parse(
        """
        greet() {
          echo "hello"
          log "greeting"
          git status
        }
        log() { printf '%s' "$1"; }
        """
    )
    greet = next(f for f in rf.functions if f.name == "greet")
    names = {c.name for c in greet.calls}
    assert "log" in names   # another defined function
    assert "git" in names   # external command
    assert "echo" not in names and "printf" not in names  # builtins skipped


def test_calls_in_pipeline_subshell_and_conditional():
    rf = _parse(
        """
        run() {
          if some_check; then
            do_thing
          fi
          result=$(compute 5)
          ls -la | grep foo
        }
        """
    )
    run = next(f for f in rf.functions if f.name == "run")
    names = {c.name for c in run.calls}
    # nested in conditional, command-substitution, and pipeline all count.
    assert {"some_check", "do_thing", "compute", "grep"} <= names
    assert "ls" in names  # left side of the pipe
    assert "if" not in names and "[" not in names  # keywords/test are not commands


def test_unnameable_dynamic_command_dropped():
    rf = _parse(
        """
        dispatch() {
          $cmd arg
          "${handler}" run
          real_command
        }
        """
    )
    dispatch = next(f for f in rf.functions if f.name == "dispatch")
    names = {c.name for c in dispatch.calls}
    assert names == {"real_command"}  # $cmd / ${handler} are not static words -> dropped


def test_nested_function_calls_not_double_counted():
    rf = _parse(
        """
        outer() {
          outer_call
          inner() {
            inner_call
          }
        }
        """
    )
    outer = next(f for f in rf.functions if f.name == "outer")
    inner = next(f for f in rf.functions if f.name == "inner")
    assert {c.name for c in outer.calls} == {"outer_call"}   # NOT inner_call
    assert {c.name for c in inner.calls} == {"inner_call"}


def test_call_deduplicated_within_function():
    rf = _parse("f() { helper; helper; helper; }")
    (f,) = rf.functions
    assert [c.name for c in f.calls] == ["helper"]


def test_source_includes_static_and_dynamic():
    rf = _parse(
        """
        source ./lib/util.sh
        . /etc/profile
        source "$DIR/common.sh"
        """
    )
    by_path = {s.path: s for s in rf.sources}
    assert set(by_path) == {"./lib/util.sh", "/etc/profile", "$DIR/common.sh"}
    assert by_path["./lib/util.sh"].is_dynamic is False
    assert by_path["/etc/profile"].is_dynamic is False
    assert by_path["$DIR/common.sh"].is_dynamic is True  # contains an expansion


def test_source_inside_function_is_captured_at_file_level():
    rf = _parse("bootstrap() { source ./setup.sh; }")
    assert [s.path for s in rf.sources] == ["./setup.sh"]


# -- doc comments (shared `doc` property via core.docstrings.leading_comment) ---------------


def test_function_doc_single_and_stacked():
    rf = _parse(
        """
        # greet prints a greeting.
        # It takes one argument.
        greet() { echo hi; }
        undocumented() { echo x; }
        """
    )
    by_name = {f.name: f for f in rf.functions}
    assert by_name["greet"].doc == "greet prints a greeting.\nIt takes one argument."
    assert by_name["undocumented"].doc == ""


def test_shebang_is_not_doc():
    rf = _parse(
        "#!/usr/bin/env bash\n"
        "main() { echo hi; }\n"
    )
    assert next(f for f in rf.functions if f.name == "main").doc == ""


def test_blank_line_between_comment_and_decl_is_not_doc():
    rf = _parse(
        """
        # stray comment, not attached

        loose() { echo x; }
        """
    )
    assert next(f for f in rf.functions if f.name == "loose").doc == ""


def test_source_and_line_span():
    rf = _parse("f() {\n  echo hi\n}\n")
    (f,) = rf.functions
    assert f.source.startswith("f()")
    assert f.line_start == 1 and f.line_end == 3
