"""The SCIP descriptor parser -- kind/name/hierarchy decoded from the symbol grammar, cross-indexer."""

from __future__ import annotations

from auryn.core import scip_descriptor as sd


def test_parses_kinds_and_names_across_indexers():
    ts = "scip-typescript npm tsprobe 1.0.0 src/`lib.ts`/Fast#run()."
    comps = sd.parse(ts)
    assert [(c.name, c.kind) for c in comps] == [
        ("src", sd.NAMESPACE), ("lib.ts", sd.NAMESPACE), ("Fast", sd.TYPE), ("run", sd.METHOD)]

    # a top-level function (no type ancestor); scip-clang packs an overload hash in the parens
    assert [(c.name, c.kind) for c in sd.parse("cxx . . $ compute(8566de10eab6f7d0).")] == [("compute", sd.METHOD)]
    # a Go interface method spec is a TERM (`Read.`), not a method
    assert sd.parse("scip-go gomod m v0 `m`/Reader#Read.")[-1].kind == sd.TERM
    # a backtick-escaped name (a C++ destructor) keeps its special characters
    assert sd.parse("cxx . . $ Shape#`~Shape`(h).")[-1].name == "~Shape"
    # a type descriptor
    assert sd.parse("scip-typescript npm tsprobe 1.0.0 src/`lib.ts`/Fast#")[-1].kind == sd.TYPE


def test_locals_and_malformed_yield_no_components():
    assert sd.parse("local 0") == []
    assert sd.parse("") == []
    assert sd.is_local("local 42")
    assert not sd.is_local("scip-go gomod m v0 `m`/F().")


def test_ancestor_symbol_rebuilds_the_exact_parent_symbol():
    # the method's enclosing type must rebuild to the type's OWN definition symbol, so a HAS_METHOD edge lands.
    sym = "scip-go gomod example.com/acme/proj v0 `example.com/acme/proj/app`/Service#Save()."
    comps = sd.parse(sym)
    type_comp = next(c for c in comps if c.kind == sd.TYPE)
    assert sd.ancestor_symbol(sym, type_comp) == \
        "scip-go gomod example.com/acme/proj v0 `example.com/acme/proj/app`/Service#"


def test_split_symbol_keeps_the_descriptor_intact():
    prefix, desc = sd.split_symbol("scip-typescript npm pkg 1.0.0 src/`a.ts`/f().")
    assert prefix == "scip-typescript npm pkg 1.0.0 " and desc == "src/`a.ts`/f()."
    assert prefix + desc == "scip-typescript npm pkg 1.0.0 src/`a.ts`/f()."
    # a bare/malformed symbol (fewer than four spaces) -> no prefix
    assert sd.split_symbol("local 0") == ("", "local 0")
