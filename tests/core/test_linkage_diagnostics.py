"""Gap-2 diagnostic: `reconcile_linkages` must no longer *silently* drop a cross-repo reference
whose only problem is a package-root / project-layout mismatch. When a `resolved:false` placeholder
matches a real definition emitted in the same ingest whose linkage key differs ONLY by a leading
module prefix, it is recorded in `near_misses` and logged loudly (it is NOT auto-merged -- per
ADR 0004 decision 6 the fix belongs in the key, a loosened match would risk false joins).

These are pure-unit tests over a fake writer -- no live FalkorDB -- so they pin the diagnostic logic
independent of the graph backend.
"""

from __future__ import annotations

import logging

from auryn.core.linkage import reconcile_linkages

# Row shape the seek returns / the fake serves: (linkage_key, id, resolved, defined, origin).
# A real (canonical) node: resolved/defined are None (python) -> "not False"; a placeholder:
# resolved=False and its id == its linkage_key.
_PH_KEY = "extern://python/acme_payments.core::charge"      # what `import acme_payments.core` mints
_REAL_KEY = "extern://python/acme-payments.acme_payments.core::charge"  # dotted path polluted by the
_REAL_ID = "python://acme-payments/module/acme-payments.acme_payments.core.charge(amount)"  # wrapper dir


class _Result:
    def __init__(self, rows):
        self.result_set = rows


class _FakeWriter:
    """Minimal GraphWriter stand-in: answers `CALL db.labels()` with the seeded labels, serves the
    `linkage_key IN $ls` seek from canned rows filtered to the requested keys, and records writes
    (the absorb path) without touching a database."""

    def __init__(self, rows, labels=("Function",)):
        self._rows = rows
        self._labels = list(labels)
        self.writes: list[tuple[str, dict]] = []

    def ro_query(self, cypher, params=None):
        if "db.labels()" in cypher:
            return _Result([[lb] for lb in self._labels])
        sel = set((params or {}).get("ls", []))
        return _Result([list(r) for r in self._rows if r[0] in sel])

    def query(self, cypher, params=None):
        self.writes.append((cypher, params or {}))
        return _Result([])


def test_package_root_mismatch_is_reported_not_silently_dropped(caplog):
    writer = _FakeWriter([
        (_REAL_KEY, _REAL_ID, None, None, "local"),
        (_PH_KEY, _PH_KEY, False, None, "external"),
    ])
    with caplog.at_level(logging.WARNING, logger="auryn.core.linkage"):
        stats = reconcile_linkages(writer, real_linkages=[_REAL_KEY], placeholder_ids=[_PH_KEY])

    assert stats.absorbed == 0                       # keys differ -> nothing merged (correct)
    assert stats.unresolved == [_PH_KEY]             # the reference is flagged unresolved
    assert stats.near_misses == [(_PH_KEY, _REAL_KEY)]  # ...and diagnosed as a layout mismatch
    assert writer.writes == []                        # no _absorb attempted
    assert any("resolution MISS" in r.message and r.levelno == logging.WARNING for r in caplog.records)


def test_exact_match_still_absorbs_cleanly():
    key = "extern://python/acme_payments.core::charge"
    real_id = "python://acme_payments/module/acme_payments.core.charge(amount)"
    writer = _FakeWriter([
        (key, real_id, None, None, "local"),
        (key, key, False, None, "external"),
    ])
    stats = reconcile_linkages(writer, real_linkages=[key], placeholder_ids=[key])

    assert stats.absorbed == 1
    assert stats.unresolved == []
    assert stats.near_misses == []
    assert writer.writes  # _absorb ran


def test_builtin_placeholder_is_not_counted_as_unresolved():
    # `os.path.join` will never resolve to an in-graph definition -- terminal origin, so it is not
    # noise in the unresolved signal.
    key = "extern://python/os.path::join"
    writer = _FakeWriter([(key, key, False, None, "stdlib")])
    stats = reconcile_linkages(writer, real_linkages=[], placeholder_ids=[key])

    assert stats.unresolved == []
    assert stats.near_misses == []


def test_unrelated_same_name_symbol_is_not_a_near_miss(caplog):
    # Same symbol name (`run`) but qualifiers share no tail -> a genuine unresolved external, NOT a
    # layout mismatch. It is counted as unresolved (info) but never flagged as a near-miss (warning).
    ph = "extern://python/foo::run"
    real = "extern://python/bar::run"
    writer = _FakeWriter([
        (real, "python://bar/module/bar.run()", None, None, "local"),
        (ph, ph, False, None, "external"),
    ])
    with caplog.at_level(logging.WARNING, logger="auryn.core.linkage"):
        stats = reconcile_linkages(writer, real_linkages=[real], placeholder_ids=[ph])

    assert stats.unresolved == [ph]
    assert stats.near_misses == []
    assert not any("resolution MISS" in r.message for r in caplog.records)
