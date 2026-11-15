"""Unit tests for the shared doc-comment capture helpers (no tree-sitter, no FalkorDB).

Covers the two engine-agnostic pieces: `clean_comment` (marker stripping) and
`leading_comment_from_source` (the AST-engine, source-text path used by Python/C#). The
tree-sitter `leading_comment` path is exercised end-to-end in the Go parser tests.
"""

from __future__ import annotations

from auryn.core.docstrings import clean_comment, leading_comment_from_source


def test_clean_strips_line_markers():
    assert clean_comment("// hello") == "hello"
    assert clean_comment("# hello") == "hello"
    assert clean_comment("-- hello") == "hello"
    assert clean_comment("/// doc line") == "doc line"
    assert clean_comment("//! inner doc") == "inner doc"


def test_clean_multiline_line_comments():
    raw = "// Store persists widgets.\n// It is safe for concurrent use."
    assert clean_comment(raw) == "Store persists widgets.\nIt is safe for concurrent use."


def test_clean_block_comment_with_stars():
    raw = "/*\n * Adds two numbers.\n * Returns the sum.\n */"
    assert clean_comment(raw) == "Adds two numbers.\nReturns the sum."


def test_clean_single_line_block_comment():
    assert clean_comment("/* inline note */") == "inline note"


def test_clean_collapses_blank_lines_and_trims():
    raw = "//\n// title\n//\n//\n// body\n//"
    assert clean_comment(raw) == "title\n\nbody"


def test_clean_does_not_touch_inprose_markers():
    # `--` and `*` only stripped when they *lead* a line, never mid-prose.
    assert clean_comment("// cost -- benefit tradeoff") == "cost -- benefit tradeoff"
    assert clean_comment("// a * b product") == "a * b product"


def test_leading_comment_from_source_python_block():
    text = "\n".join(
        [
            "import os",
            "",
            "# Connect to the database.",
            "# Retries on failure.",
            "def connect():",
            "    ...",
        ]
    )
    # def is on line 5 (1-based)
    assert leading_comment_from_source(text, 5) == "Connect to the database.\nRetries on failure."


def test_leading_comment_from_source_stops_at_blank_line():
    text = "\n".join(
        [
            "# unrelated module comment",
            "",
            "def f():",
            "    ...",
        ]
    )
    # blank line between the comment and `def` -> not a doc comment
    assert leading_comment_from_source(text, 3) == ""


def test_leading_comment_from_source_none_when_no_comment():
    text = "x = 1\ndef f():\n    ...\n"
    assert leading_comment_from_source(text, 2) == ""


def test_leading_comment_from_source_csharp_xml_doc():
    text = "\n".join(
        [
            "namespace N {",
            "    /// <summary>Gets the widget.</summary>",
            "    public Widget Get() { }",
        ]
    )
    assert leading_comment_from_source(text, 3, prefixes=("///", "//")) == "<summary>Gets the widget.</summary>"
