"""Unit tests for the PostToolUse enrichment hook's pure logic (no graph required).

Graph-backed behaviour (callers/callees/impact rendering) is exercised by the benchmark against a
live graph; here we pin the gating, parsing, provenance-envelope, and dispatch that decide *whether*
and *how* the hook fires -- the parts that keep it cheap and unambiguous."""

from __future__ import annotations

from auryn.hooks import postuse_enrich as h


# ── identifier extraction (the Grep/Bash gate) ─────────────────────────────────────────────


def test_extract_identifier_bare_name():
    assert h._extract_identifier("get_connection") == "get_connection"


def test_extract_identifier_regex_single_token():
    # anchored / metachar-wrapped single identifier still resolves to that identifier
    assert h._extract_identifier(r"^get_connection\b") == "get_connection"
    assert h._extract_identifier(r"def reverse\(") == "reverse"


def test_extract_identifier_multi_token_regex_declines():
    # an alternation names several symbols -> we can't say which -> decline
    assert h._extract_identifier(r"connect|disconnect") is None


def test_extract_identifier_phrase_and_short_decline():
    assert h._extract_identifier("open a database connection") is None  # a phrase, not a symbol
    assert h._extract_identifier("id") is None  # too short (< 3 chars)
    assert h._extract_identifier("") is None


# ── bash parsing (the reflex-through-the-shell gate) ───────────────────────────────────────


def test_bash_query_rg_identifier():
    assert h._bash_query("rg get_connection packages/") == ("grep", "get_connection")


def test_bash_query_grep_with_flags():
    assert h._bash_query("grep -rn reverse django/urls/") == ("grep", "reverse")


def test_bash_query_cat_file():
    assert h._bash_query("cat app/db.py") == ("read", "app/db.py")


def test_bash_query_declines_non_search():
    assert h._bash_query("python manage.py test") is None
    assert h._bash_query("git status") is None
    assert h._bash_query("") is None


def test_bash_query_only_first_pipeline_stage():
    # a search piped into something else is still a search
    assert h._bash_query("rg reverse | head") == ("grep", "reverse")


def test_bash_query_pipe_inside_quoted_regex():
    # a `\|` alternation inside quotes must not be mistaken for a shell pipe (real agent behaviour)
    cmd = r'grep -rn "def hybrid_search\|hybrid_search(" --include="*.py" /x | grep -i hybrid_search'
    assert h._bash_query(cmd) == ("grep", "hybrid_search")


def test_bash_query_after_cd_and_sep():
    assert h._bash_query("cd /x && rg get_connection") == ("grep", "get_connection")


def test_bash_query_grep_dash_e():
    assert h._bash_query("grep -e get_connection app/db.py") == ("grep", "get_connection")


# ── placeholder filtering (don't enrich on cross-repo/stdlib placeholders) ──────────────────


def test_is_real_definition():
    real = {"id": "python://proj/module/app.db.func.get_connection()", "properties": {}}
    extern = {"id": "extern://python/os._Environ::get", "properties": {"resolved": False, "origin": "stdlib"}}
    unresolved = {"id": "python://x", "properties": {"resolved": False}}
    assert h._is_real_definition(real) is True
    assert h._is_real_definition(extern) is False
    assert h._is_real_definition(unresolved) is False


# ── twin union (recover caller edges stranded on a linkage_key twin) ────────────────────────


def test_traversal_ids_includes_linkage_twin():
    node = {"id": "python://proj/module/m.func.f()",
            "properties": {"linkage_key": "extern://python/m::f"}}
    ids = h._traversal_ids(node)
    assert node["id"] in ids and "extern://python/m::f" in ids


def test_traversal_ids_no_twin():
    node = {"id": "python://proj/module/m.func.f()", "properties": {}}
    assert h._traversal_ids(node) == ["python://proj/module/m.func.f()"]


# ── provenance envelope + token budget ─────────────────────────────────────────────────────


def test_envelope_is_self_labelling():
    ctx = h.GraphContext(h.HookConfig(graph_name="g"))
    ctx._status = {"has_data": True, "node_count": 100}  # avoid a real connection
    text = h._envelope(ctx, h.HookConfig(graph_name="g"), 'Grep "foo"', "foo → Function\n  called by (1): bar")
    assert text is not None
    assert text.startswith(h._ENVELOPE_OPEN) and text.rstrip().endswith(h._ENVELOPE_CLOSE)
    assert "NOT part of the tool result" in text
    assert 'Grep "foo"' in text  # trigger tied to the exact call


def test_envelope_respects_token_budget():
    ctx = h.GraphContext(h.HookConfig(graph_name="g"))
    ctx._status = {"has_data": True, "node_count": 100}
    tiny = h.HookConfig(graph_name="g", max_tokens=5)  # smaller than even the header
    assert h._envelope(ctx, tiny, "Grep \"foo\"", "x" * 4000) is None


# ── dispatch ───────────────────────────────────────────────────────────────────────────────


def test_enrich_disabled_is_silent():
    cfg = h.HookConfig(enabled=False)
    r = h.enrich({"tool_name": "Grep", "tool_input": {"pattern": "x"}}, h.GraphContext(cfg), cfg)
    assert not r.fired and r.reason == "disabled"


def test_enrich_unmatched_tool_is_silent():
    cfg = h.HookConfig()
    r = h.enrich({"tool_name": "TodoWrite", "tool_input": {}}, h.GraphContext(cfg), cfg)
    assert not r.fired and r.reason == "tool-not-matched"


def test_enrich_grep_non_symbol_is_silent():
    cfg = h.HookConfig()
    r = h.enrich({"tool_name": "Grep", "tool_input": {"pattern": "open a file"}}, h.GraphContext(cfg), cfg)
    assert not r.fired and r.reason == "not-a-symbol"


def test_result_token_estimate():
    assert h.EnrichResult(context="abcd" * 10).injected_tokens == 10
    assert h.EnrichResult(context=None).injected_tokens == 0
