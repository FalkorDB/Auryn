"""Cross-repo healing by ENTITY KIND and by how the producer EXPOSES a symbol (ADR 0004).

`test_cross_repo_import_forms.py` covers *call* resolution across import syntaxes. This covers the
orthogonal axis: which kinds of cross-repo reference heal when the producer repo is ingested.

  * module import (`import billing.core`)         -> HEALS (external Module placeholder upgrades)
  * re-export through __init__.py                 -> KNOWN GAP (linkage-key mismatch)
  * class inheritance (base in another repo)      -> KNOWN GAP (no placeholder/edge emitted at all)
  * star import (`from billing.core import *`)    -> KNOWN GAP (parser skips `*`)

The xfails document real limitations so they're tracked (and flagged if they start passing). See the
per-test docstrings for the mechanism and the fix each would need.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from auryn.core.graph_client import GraphWriter
from auryn.core.ingest import ingest_directory


def _ingest(g, path):
    ingest_directory(GraphWriter(g), str(path), deep="off", auto_install=False)


def _mk_billing(root, init_body="", core_body="def charge(amount):\n    return amount\n"):
    (root / "billing").mkdir(parents=True)
    (root / "billing" / "__init__.py").write_text(init_body)
    (root / "billing" / "core.py").write_text(core_body)


def test_module_import_heals_cross_repo(falkordb_connection, tmp_path):
    # `import billing.core` -> external Module placeholder -> upgrades to the real Module when
    # billing is ingested (consumer-first, the harder order). This one WORKS; pin it.
    billing = tmp_path / "billing"
    _mk_billing(billing)
    webshop = tmp_path / "webshop"
    webshop.mkdir()
    (webshop / "checkout.py").write_text("import billing.core\n\n\ndef place_order(total):\n    return total\n")

    g = falkordb_connection.select_graph(f"ek_modimp_{uuid4().hex}")
    try:
        _ingest(g, webshop)   # placeholder Module first
        _ingest(g, billing)   # producer heals it
        rows = g.ro_query(
            "MATCH (m:Module {name:'checkout'})-[:IMPORTS]->(t) RETURN t.id, t.resolved"
        ).result_set
        assert any(r[0] == "python://billing/module/billing.core" for r in rows), rows
    finally:
        g.delete()


def test_reexport_via_init_is_a_known_gap(falkordb_connection, tmp_path):
    # billing exposes charge on its package surface (`from billing.core import charge` in __init__),
    # so the consumer writes `from billing import charge`. The consumer mints linkage
    # `billing::charge`, but the real definition is `billing.core::charge` -> no match. A fix needs
    # the producer's re-export (__init__ IMPORTS edge) to contribute an ALIAS linkage key
    # (`billing::charge`) onto the real symbol, i.e. multiple linkage keys per node.
    billing = tmp_path / "billing"
    _mk_billing(billing, init_body="from billing.core import charge\n")
    webshop = tmp_path / "webshop"
    webshop.mkdir()
    (webshop / "checkout.py").write_text(
        "from billing import charge\n\n\ndef place_order(total):\n    return charge(total)\n"
    )

    g = falkordb_connection.select_graph(f"ek_reexport_{uuid4().hex}")
    try:
        _ingest(g, billing)
        _ingest(g, webshop)
        rows = g.ro_query(
            "MATCH (:Function {name:'place_order'})-[:CALLS]->(c) RETURN c.id, c.resolved"
        ).result_set
        healed = any(r[0] == "python://billing/module/billing.core.func.charge(amount)" for r in rows)
        if healed:
            pytest.fail("re-export now heals -- promote this to a passing assertion")
        pytest.xfail(f"re-export through __init__.py doesn't heal (placeholder stays billing::charge): {rows}")
    finally:
        g.delete()


def test_cross_repo_inheritance_is_a_known_gap(falkordb_connection, tmp_path):
    # `class Wallet(Account)` where Account is defined in billing. Unlike a cross-repo CALL,
    # `_emit_class` records an unresolved base only as a string in `unresolved_bases` -- it emits
    # NO placeholder Class and NO EXTENDS edge -- so there is nothing for reconciliation to upgrade.
    # A fix: emit a placeholder Class (`extern://python/billing.models::Account`) + EXTENDS edge for
    # an unresolved base, mirroring the CALLS placeholder path (reconcile already heals EXTENDS).
    billing = tmp_path / "billing"
    (billing / "billing").mkdir(parents=True)
    (billing / "billing" / "__init__.py").write_text("")
    (billing / "billing" / "models.py").write_text("class Account:\n    def debit(self, n):\n        return n\n")
    webshop = tmp_path / "webshop"
    webshop.mkdir()
    (webshop / "wallet.py").write_text("from billing.models import Account\n\n\nclass Wallet(Account):\n    pass\n")

    g = falkordb_connection.select_graph(f"ek_inherit_{uuid4().hex}")
    try:
        _ingest(g, billing)
        _ingest(g, webshop)
        rows = g.ro_query("MATCH (:Class {name:'Wallet'})-[:EXTENDS]->(b) RETURN b.id").result_set
        healed = any(r[0] == "python://billing/module/billing.models.class.account" for r in rows)
        if healed:
            pytest.fail("cross-repo inheritance now heals -- promote this to a passing assertion")
        pytest.xfail(f"cross-repo base class emits no placeholder/EXTENDS edge (rows={rows})")
    finally:
        g.delete()


def test_star_import_is_a_known_gap(falkordb_connection, tmp_path):
    # `from billing.core import *; charge()` -- the ast parser skips `*` imports, so `charge` is an
    # unbound name and no reference is recorded. Statically resolving `*` needs the producer's
    # names, which aren't present when the consumer is ingested.
    billing = tmp_path / "billing"
    _mk_billing(billing)
    webshop = tmp_path / "webshop"
    webshop.mkdir()
    (webshop / "checkout.py").write_text(
        "from billing.core import *\n\n\ndef place_order(total):\n    return charge(total)\n"
    )

    g = falkordb_connection.select_graph(f"ek_star_{uuid4().hex}")
    try:
        _ingest(g, billing)
        _ingest(g, webshop)
        rows = g.ro_query("MATCH (:Function {name:'place_order'})-[:CALLS]->(c) RETURN c.id").result_set
        if any(r[0] == "python://billing/module/billing.core.func.charge(amount)" for r in rows):
            pytest.fail("star import now heals -- promote this to a passing assertion")
        pytest.xfail(f"star import isn't resolved (no reference recorded): {rows}")
    finally:
        g.delete()
