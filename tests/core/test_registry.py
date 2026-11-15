"""Registry/auto-discovery tests -- pure Python, no FalkorDB involved."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

from auryn.core.base_ingestor import BaseIngestor
from auryn.core.registry import (
    GraphEnrichment,
    discover_git_languages,
    discover_ingestors,
    select_enrichments,
)


def test_discover_ingestors_finds_builtin_sql_ingestor():
    registry = discover_ingestors()

    assert "sql" in registry
    assert issubclass(registry["sql"], BaseIngestor)
    assert registry["sql"].name == "sql"


def test_discover_ingestors_registers_both_plsql_ingestors_under_distinct_names():
    registry = discover_ingestors()

    # The catalog/connection ingestor is `oracle`; the file-based one is `plsql`.
    assert "oracle" in registry
    assert "plsql" in registry
    assert registry["oracle"].name == "oracle"
    assert registry["plsql"].name == "plsql"


def test_discover_git_languages_finds_csharp_and_plsql():
    languages = discover_git_languages()

    assert set(languages) >= {"csharp", "plsql", "c"}
    # C# owns both .cs and .cshtml (the Kendo/MVC view tier is a stage of C# git-sync).
    assert set(languages["csharp"].file_suffixes) == {".cs", ".cshtml"}
    assert ".pkb" in languages["plsql"].file_suffixes
    assert ".sql" in languages["plsql"].file_suffixes
    assert set(languages["c"].file_suffixes) == {".c", ".h"}
    # Every registered language must expose a callable sync entrypoint.
    assert all(callable(lang.sync) for lang in languages.values())


def test_only_dynamic_languages_are_two_phase():
    # Two-phase = a fast structural pass plus a deep semantic pass. Python (fast ast -> deep jedi)
    # and C# (fast tree-sitter -> deep Roslyn) both declare one; the others are fast-only for now.
    languages = discover_git_languages()
    assert languages["python"].is_two_phase()
    assert languages["python"].deep_sync is not None
    assert languages["csharp"].is_two_phase()  # fast tree-sitter -> deep Roslyn (perf: --deep off is fast)
    assert languages["csharp"].deep_sync is not None
    assert not languages["plsql"].is_two_phase()
    assert not languages["c"].is_two_phase()  # fast tree-sitter only for now; libclang deep is future


def test_ingest_path_discovery_is_import_light():
    """The whole point of the lazy-by-suffix split: discovering file-based languages and
    enrichments (the ingest path) must NOT import any language's heavy extraction engine --
    tree-sitter, the ANTLR grammar/oracledb, SQLAlchemy, or Jedi -- so a repo with none of a
    language's files never pays that language's import cost. Run in a fresh interpreter so
    the assertion can't be masked by another test having already imported an engine."""
    code = textwrap.dedent(
        """
        import sys
        from auryn.core.registry import discover_git_languages, discover_graph_enrichments
        langs = discover_git_languages()
        enrich = discover_graph_enrichments()
        assert set(langs) >= {"python", "csharp", "plsql", "c"}, sorted(langs)
        assert set(enrich) >= {"orm-tables", "python-routes"}, sorted(enrich)
        heavy = [m for m in ("jedi", "tree_sitter", "tree_sitter_c_sharp", "antlr4",
                             "oracledb", "sqlalchemy") if m in sys.modules]
        print(",".join(heavy))
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "", f"ingest-path discovery imported heavy engine modules: {out}"


# -- select_enrichments (pure, in-process) ---------------------------------------------------


def _hooks(*specs) -> dict[str, GraphEnrichment]:
    """Build a name->GraphEnrichment registry from (name, order[, phase]) tuples."""
    out: dict[str, GraphEnrichment] = {}
    for spec in specs:
        name, order = spec[0], spec[1]
        phase = spec[2] if len(spec) > 2 else "post"
        out[name] = GraphEnrichment(name=name, run=lambda *a, **k: None, phase=phase, order=order)
    return out


def test_select_enrichments_orders_by_order_then_name():
    hooks = _hooks(("b", 100), ("a", 100), ("early", 10))
    assert [h.name for h in select_enrichments(hooks)] == ["early", "a", "b"]


def test_select_enrichments_excludes_disabled_and_skip():
    hooks = _hooks(("a", 100), ("b", 100), ("c", 100))
    assert [h.name for h in select_enrichments(hooks, disabled=["b"], skip=["c"])] == ["a"]


def test_select_enrichments_only_overrides_disabled():
    # An explicit per-run `only` wins even over a stored disable.
    hooks = _hooks(("a", 100), ("b", 100))
    assert [h.name for h in select_enrichments(hooks, only=["b"], disabled=["b"])] == ["b"]


def test_select_enrichments_drops_pre_phase():
    hooks = _hooks(("post-one", 100), ("pre-one", 100, "pre"))
    assert [h.name for h in select_enrichments(hooks)] == ["post-one"]
    # even when explicitly requested, a pre hook is never executed
    assert select_enrichments(hooks, only=["pre-one"]) == []


# -- local hook discovery (subprocess, isolated AURYN_HOME) -------------------------------


def _discover_with_local_hooks(tmp_path, files: dict[str, str]) -> dict:
    """Write hook files into a temp AURYN_HOME/hooks and discover them in a fresh
    interpreter (so the module-level load-once guard and the shared registry stay clean)."""
    hooks = tmp_path / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (hooks / name).write_text(textwrap.dedent(content))
    code = textwrap.dedent(
        """
        import json
        from auryn.core.registry import discover_graph_enrichments, local_hook_errors
        reg = discover_graph_enrichments()
        print(json.dumps({
            "hooks": {n: {"source": h.source, "order": h.order, "phase": h.phase}
                      for n, h in reg.items()},
            "errors": local_hook_errors(),
        }))
        """
    )
    env = {**os.environ, "AURYN_HOME": str(tmp_path)}
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_local_hook_convention_autoregister(tmp_path):
    out = _discover_with_local_hooks(tmp_path, {
        "link_tests.py": "def run(writer, repo, target_sha):\n    return {'ok': 1}\n",
    })
    assert out["hooks"]["link_tests"] == {"source": "local", "order": 100, "phase": "post"}
    assert out["errors"] == {}


def test_local_hook_name_and_order_overrides(tmp_path):
    out = _discover_with_local_hooks(tmp_path, {
        "myfile.py": "NAME = 'fancy'\nORDER = 5\ndef run(writer, repo, target_sha):\n    return None\n",
    })
    assert "fancy" in out["hooks"]
    assert out["hooks"]["fancy"] == {"source": "local", "order": 5, "phase": "post"}
    assert "myfile" not in out["hooks"]


def test_local_hook_explicit_register_tagged_local(tmp_path):
    out = _discover_with_local_hooks(tmp_path, {
        "explicit.py": textwrap.dedent(
            """
            from auryn.core.registry import GraphEnrichment, register_graph_enrichment
            register_graph_enrichment(GraphEnrichment(name='explicit-hook', run=lambda *a, **k: None, order=7))
            """
        ),
    })
    assert out["hooks"]["explicit-hook"] == {"source": "local", "order": 7, "phase": "post"}


def test_broken_local_hook_is_recorded_not_fatal(tmp_path):
    out = _discover_with_local_hooks(tmp_path, {
        "good.py": "def run(writer, repo, target_sha):\n    return None\n",
        "broken.py": "def run(writer, repo, target_sha):\n    return None\nraise ValueError('boom')\n",
        "no_run.py": "X = 1  # neither a run() nor a register call\n",
    })
    assert "good" in out["hooks"]                       # the healthy one still loads
    assert "broken" not in out["hooks"]
    assert "ValueError" in out["errors"]["broken"]
    assert "no_run" in out["errors"]                    # missing run() is an error, not a silent skip


def test_local_hook_cannot_shadow_plugin(tmp_path):
    # A local file naming itself after a shipped plugin hook must NOT hijack it.
    out = _discover_with_local_hooks(tmp_path, {
        "orm_tables.py": "NAME = 'orm-tables'\ndef run(writer, repo, target_sha):\n    return {'hijacked': 1}\n",
    })
    assert out["hooks"]["orm-tables"]["source"] == "plugin"  # plugin wins the collision


def test_local_hook_explicit_register_cannot_shadow_plugin(tmp_path):
    # Even an explicit register_graph_enrichment() with a shipped name is restored to the plugin.
    out = _discover_with_local_hooks(tmp_path, {
        "evil.py": (
            "from auryn.core.registry import GraphEnrichment, register_graph_enrichment\n"
            "register_graph_enrichment(GraphEnrichment(name='orm-tables', run=lambda *a, **k: {'x': 1}))\n"
        ),
    })
    assert out["hooks"]["orm-tables"]["source"] == "plugin"


def test_local_hook_underscore_prefixed_is_ignored(tmp_path):
    # Files starting with `_` are skipped entirely (not loaded, not reported as broken).
    out = _discover_with_local_hooks(tmp_path, {
        "_helper.py": "raise RuntimeError('should never be imported')\n",
        "real.py": "def run(writer, repo, target_sha):\n    return None\n",
    })
    assert "real" in out["hooks"]
    assert "_helper" not in out["hooks"]
    assert "_helper" not in out["errors"]
