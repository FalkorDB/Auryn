"""Cross-repo resolution across the ways a consumer can IMPORT the external symbol (ADR 0004).

`test_cross_repo_resolution.py` proves the heal mechanism for one import shape (`from mod import
fn; fn()`). But the linkage key a consumer mints is derived from *how it imported* the symbol, so a
form the resolver doesn't normalize to the producer's real `<module>::<name>` key silently fails to
detect the reference at all -- no placeholder, no edge, invisible to a reachability audit. This pins
the whole matrix of import forms x ingest order x engine.

Covered forms (all must heal to the producer's real `charge`, whichever repo is ingested first and
under both the fast `ast` and default `jedi` engines):
  A  from billing.core import charge      ; charge(x)
  B  from billing.core import charge as p ; p(x)            (symbol alias)
  C  import billing.core as bc            ; bc.charge(x)    (module alias via `as`)
  D  from billing import core             ; core.charge(x)  (submodule imported from its package)

Known gap (xfail): E  import billing.core ; billing.core.charge(x) -- the fully-qualified call. The
`ast` call extractor records only a single-Name receiver, so the `billing.core` receiver chain is
lost; and `import billing.core` binds the local name `billing`, not `billing.core`. Fixing it is a
parser-level change (multi-level receiver capture) tracked separately.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from auryn.core.graph_client import GraphWriter
from auryn.core.ingest import ingest_directory

_REAL = "python://billing/module/billing.core.func.charge(amount)"

_CONSUMERS = {
    "A_from_import": "from billing.core import charge\n\n\ndef place_order(total):\n    return charge(total)\n",
    "B_symbol_alias": "from billing.core import charge as pay\n\n\ndef place_order(total):\n    return pay(total)\n",
    "C_module_alias": "import billing.core as bc\n\n\ndef place_order(total):\n    return bc.charge(total)\n",
    "D_from_submodule": "from billing import core\n\n\ndef place_order(total):\n    return core.charge(total)\n",
    "E_fully_qualified": "import billing.core\n\n\ndef place_order(total):\n    return billing.core.charge(total)\n",
}


def _place_order_call(g):
    rows = g.ro_query(
        "MATCH (f:Function {name:'place_order'})-[e:CALLS]->(c) RETURN c.id, e.resolution, c.resolved"
    ).result_set
    return rows[0] if rows else None


def _ingest_both(falkordb_connection, tmp_path, consumer_src: str, order: str, deep: str):
    billing = tmp_path / "billing"
    (billing / "billing").mkdir(parents=True)
    (billing / "billing" / "__init__.py").write_text("")
    (billing / "billing" / "core.py").write_text("def charge(amount):\n    return amount\n")

    webshop = tmp_path / "webshop"
    webshop.mkdir()
    (webshop / "checkout.py").write_text(consumer_src)

    g = falkordb_connection.select_graph(f"xrepo_forms_{uuid4().hex}")
    steps = [billing, webshop] if order == "producer_first" else [webshop, billing]
    for repo in steps:
        ingest_directory(GraphWriter(g), str(repo), deep=deep, auto_install=False)
    return g


@pytest.mark.parametrize("form", ["A_from_import", "B_symbol_alias", "C_module_alias", "D_from_submodule"])
@pytest.mark.parametrize("order", ["producer_first", "consumer_first"])
@pytest.mark.parametrize("deep", ["off", "inline"])
def test_import_form_heals_cross_repo(form, order, deep, falkordb_connection, tmp_path):
    g = _ingest_both(falkordb_connection, tmp_path, _CONSUMERS[form], order, deep)
    try:
        row = _place_order_call(g)
        assert row is not None, f"{form}: no CALLS edge from place_order (reference undetected)"
        callee_id, resolution, resolved = row
        assert callee_id == _REAL, f"{form}: bound to {callee_id!r}, not the real charge"
        assert resolution == "resolved" and resolved is None
    finally:
        g.delete()


@pytest.mark.parametrize("order", ["producer_first", "consumer_first"])
def test_fully_qualified_call_is_a_known_gap(order, falkordb_connection, tmp_path):
    # E: `import billing.core; billing.core.charge()` -- the reference is not detected (see module
    # docstring). If this ever starts passing, the parser gap was fixed; promote it to the matrix.
    g = _ingest_both(falkordb_connection, tmp_path, _CONSUMERS["E_fully_qualified"], order, "off")
    try:
        row = _place_order_call(g)
        if row is not None and row[0] == _REAL:
            pytest.fail("fully-qualified cross-repo call now heals -- move form E into the passing matrix")
        pytest.xfail("fully-qualified `pkg.sub.fn()` receiver chain isn't captured by the ast extractor")
    finally:
        g.delete()
