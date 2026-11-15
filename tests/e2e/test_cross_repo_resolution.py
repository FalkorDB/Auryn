"""Cross-repo symbol resolution end-to-end (ADR 0004): two repos in one shared graph, a call
that crosses the boundary heals into a real edge regardless of ingest order, and `forget`-style
removal is covered separately.

repo `webshop` calls `charge` imported from repo `billing`. Ingested into ONE graph, the call
should bind to billing's real `charge` (not a placeholder), whichever repo lands first.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from auryn.core.graph_client import GraphWriter
from auryn.core.ingest import ingest_directory
from auryn.core.lifecycle import execute_forget, plan_forget


def _write_billing(root):
    (root / "billing").mkdir(parents=True)
    (root / "billing" / "__init__.py").write_text("")
    (root / "billing" / "core.py").write_text("def charge(amount):\n    return amount\n")


def _write_webshop(root):
    (root / "checkout.py").write_text(
        "from billing.core import charge\n\n\ndef place_order(total):\n    return charge(total)\n"
    )


def _place_order_calls(g):
    """(callee id, edge resolution, callee.resolved) for place_order's single CALLS edge."""
    rows = g.ro_query(
        "MATCH (f:Function {name:'place_order'})-[e:CALLS]->(c) "
        "RETURN c.id, e.resolution, c.resolved"
    ).result_set
    return rows


def _extern_count(g, needle):
    return g.ro_query(
        "MATCH (n) WHERE n.id STARTS WITH 'extern://' AND n.id CONTAINS $x RETURN count(n)",
        {"x": needle},
    ).result_set[0][0]


@pytest.mark.parametrize("order", ["billing_first", "webshop_first"])
def test_cross_repo_call_heals_either_order(order, falkordb_connection, tmp_path):
    billing = tmp_path / "billing"
    webshop = tmp_path / "webshop"
    billing.mkdir()
    webshop.mkdir()
    _write_billing(billing)
    _write_webshop(webshop)

    g = falkordb_connection.select_graph(f"xrepo_{uuid4().hex}")
    writer = GraphWriter(g)
    try:
        steps = [billing, webshop] if order == "billing_first" else [webshop, billing]
        for repo in steps:
            ingest_directory(writer, str(repo), deep="off", auto_install=False)

        # place_order's call now binds to billing's REAL charge -- resolved, not a placeholder.
        rows = _place_order_calls(g)
        assert len(rows) == 1, rows
        callee_id, resolution, resolved = rows[0]
        assert callee_id == "python://billing/module/billing.core.func.charge(amount)"
        assert resolution == "resolved"
        assert resolved is None  # a real definition carries no `resolved` flag

        # the placeholder that stood in for billing.core::charge is gone
        assert _extern_count(g, "billing.core") == 0
    finally:
        g.delete()


@pytest.mark.parametrize("order", ["definer_first", "consumer_first"])
def test_cross_repo_heals_when_definer_is_under_a_hyphenated_dir(order, falkordb_connection, tmp_path):
    # The Gap-1 regression: the definer repo's package `acme_payments` lives inside a hyphenated
    # distribution dir `acme-payments/` (a PyPI-style checkout, exactly the transcript's layout).
    # Before the fix, the definer's linkage key was polluted to
    # `extern://python/acme-payments.acme_payments.core::charge`, while the consumer's
    # `import acme_payments.core` minted `extern://python/acme_payments.core::charge` -- the keys
    # never matched, so reconciliation silently left a dangling placeholder and the call to the
    # DB-reaching `charge` was invisible to a reachability audit. With the fix the wrapper dir is
    # stripped, the keys agree, and the call heals to the real definition.
    payments = tmp_path / "payments"        # ingest root (project); package sits one dir deeper
    pkg = payments / "acme-payments" / "acme_payments"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text("def charge(amount):\n    return amount\n")

    webshop = tmp_path / "webshop"
    webshop.mkdir()
    (webshop / "checkout.py").write_text(
        "from acme_payments.core import charge\n\n\ndef place_order(total):\n    return charge(total)\n"
    )

    g = falkordb_connection.select_graph(f"xrepo_hyphen_{uuid4().hex}")
    try:
        steps = [payments, webshop] if order == "definer_first" else [webshop, payments]
        for repo in steps:
            ingest_directory(GraphWriter(g), str(repo), deep="off", auto_install=False)

        rows = _place_order_calls(g)
        assert len(rows) == 1, rows
        callee_id, resolution, resolved = rows[0]
        # bound to the REAL charge (the hyphenated dir is gone from its dotted path), not a placeholder
        assert callee_id == "python://payments/module/acme_payments.core.func.charge(amount)"
        assert resolution == "resolved"
        assert resolved is None
        # no dangling placeholder for the imported symbol survived reconciliation
        assert _extern_count(g, "acme_payments.core") == 0
    finally:
        g.delete()


@pytest.mark.parametrize("order", ["definer_first", "consumer_first"])
def test_cross_repo_heals_for_a_src_layout_definer(order, falkordb_connection, tmp_path):
    # Option A: the definer uses a `src/` layout (`src/acme_payments/core.py`, with __init__.py).
    # `src/` has no __init__, so package-root detection anchors the dotted path at `acme_payments`
    # -- matching the consumer's `import acme_payments.core`. (Context-free stripping alone can't do
    # this, since `src` is a valid identifier; it needs the __init__.py-aware package context.)
    payments = tmp_path / "payments"
    pkg = payments / "src" / "acme_payments"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text("def charge(amount):\n    return amount\n")

    webshop = tmp_path / "webshop"
    webshop.mkdir()
    (webshop / "checkout.py").write_text(
        "from acme_payments.core import charge\n\n\ndef place_order(total):\n    return charge(total)\n"
    )

    g = falkordb_connection.select_graph(f"xrepo_src_{uuid4().hex}")
    try:
        steps = [payments, webshop] if order == "definer_first" else [webshop, payments]
        for repo in steps:
            ingest_directory(GraphWriter(g), str(repo), deep="off", auto_install=False)

        rows = _place_order_calls(g)
        assert len(rows) == 1, rows
        callee_id, resolution, resolved = rows[0]
        assert callee_id == "python://payments/module/acme_payments.core.func.charge(amount)"
        assert resolution == "resolved"
        assert resolved is None
        assert _extern_count(g, "acme_payments.core") == 0
    finally:
        g.delete()


def test_forget_producer_demotes_consumer_edge(falkordb_connection, tmp_path):
    # Ingest both (webshop's call heals to billing's real charge), then forget billing. The
    # consumer must NOT lose its edge -- it demotes back to a placeholder (ADR 0004), so
    # "webshop calls charge (unresolved)" survives the producer's removal.
    billing = tmp_path / "billing"
    webshop = tmp_path / "webshop"
    billing.mkdir()
    webshop.mkdir()
    _write_billing(billing)
    _write_webshop(webshop)

    graph_name = f"xrepo_forget_{uuid4().hex}"
    g = falkordb_connection.select_graph(graph_name)
    writer = GraphWriter(g)
    ingest_directory(writer, str(billing), deep="off", auto_install=False)
    ingest_directory(writer, str(webshop), deep="off", auto_install=False)
    # healed first
    assert _place_order_calls(g)[0][0] == "python://billing/module/billing.core.func.charge(amount)"

    try:
        plan = plan_forget(falkordb_connection, graph_name, project_name="billing")
        assert plan.mode == "drop_project"  # webshop still in the graph
        execute_forget(falkordb_connection, plan)

        # billing's real charge is gone; webshop's call demoted to a placeholder, edge intact
        rows = _place_order_calls(g)
        assert len(rows) == 1, rows
        callee_id, resolution, resolved = rows[0]
        assert callee_id == "extern://python/billing.core::charge"
        assert resolution == "external"
        assert resolved is False
        # billing's project subgraph is gone
        assert g.ro_query("MATCH (n) WHERE n.id STARTS WITH 'python://billing/' RETURN count(n)").result_set[0][0] == 0
    finally:
        g.delete()


def test_webshop_alone_keeps_a_placeholder(falkordb_connection, tmp_path):
    # Without billing in the graph, the call is honestly unresolved: a placeholder Function,
    # reached by an ordinary CALLS edge (so "what does webshop depend on" is still answerable).
    webshop = tmp_path / "webshop"
    webshop.mkdir()
    _write_webshop(webshop)

    g = falkordb_connection.select_graph(f"xrepo_solo_{uuid4().hex}")
    try:
        ingest_directory(GraphWriter(g), str(webshop), deep="off", auto_install=False)
        rows = _place_order_calls(g)
        assert len(rows) == 1
        callee_id, resolution, resolved = rows[0]
        assert callee_id == "extern://python/billing.core::charge"
        assert resolution == "external"
        assert resolved is False
    finally:
        g.delete()
