"""Cross-repo symbol reconciliation (ADR 0004).

An ingestor emits an unresolved reference as a *placeholder* node keyed by its project-independent
`linkage` (an ``extern://<lang>/...`` id), and stamps that same `linkage` as a property on every
real, locally-defined symbol. When two repos share one graph, a placeholder and its real definer
therefore agree on one string. Reconciliation matches them and folds the placeholder into the real
node -- so `A -[:CALLS]-> charge{resolved:false}` becomes `A -[:CALLS]-> charge(in B)`.

Order-independent, and **no graph-wide scans**: the work is driven entirely by the `linkage` keys
*this ingest touched* (collected in-memory from the emitted nodes), and every graph read is an
index-backed ``id IN [...]`` / ``linkage IN [...]`` seek over that bounded list. Both directions
fall out of the same step:

* producer after consumer -- a real definer emitted now takes over a pre-existing placeholder;
* consumer after producer -- a placeholder emitted now binds to a pre-existing real definer.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass

from auryn.core.graph_client import SHARED_NODE_LABEL, GraphWriter
from auryn.core.graph_model import GraphNode

logger = logging.getLogger(__name__)

# Labels that can carry a `linkage_key` (externally-visible declarations across the language
# roster). Reconciliation queries are issued per-label -- intersected with the labels actually
# present in the graph (see `_present`) -- so each is an index seek and an absent label costs
# nothing. Every ingestor that stamps `linkage_key` on one of these MUST also index it (schema).
LINKABLE_LABELS = (
    "Function", "Method", "Class", "Module", "Package",  # python + most langs
    "Namespace", "Struct", "Union", "Enum", "Interface", "Trait",  # c/cpp/csharp/go/rust/php/…
)

# Edge types that can point at an absorbable symbol node (a reference/declaration from a consumer).
# Repointed onto the canonical definition when it appears; kept in sync with the ingestors'
# semantic edges. DECLARES is included so a C `extern` prototype's edge follows to the definition.
# LAUNCHES (CUDA host->kernel) is included so a launch of a kernel whose declaration is absorbed into
# its definition in another co-located project follows to the definition instead of being dropped.
_REF_RELS = ("CALLS", "IMPORTS", "EXTENDS", "IMPLEMENTS", "RETURNS", "REFERENCES", "DECLARES", "LAUNCHES")


@dataclass
class ReconcileStats:
    absorbed: int = 0        # placeholders folded into a real definer
    ambiguous: list[str] = None  # linkages with >1 real definer (surfaced, not merged)
    unresolved: list[str] = None  # placeholder linkages left unresolved that expect a definition
    #                               (origin `external`/unknown, not a terminal builtin/libc)
    near_misses: list[tuple[str, str]] = None  # (placeholder, nearest_def) differing ONLY by a
    #                               module prefix -- the package-root / layout-mismatch smoking gun

    def __post_init__(self) -> None:
        if self.ambiguous is None:
            self.ambiguous = []
        if self.unresolved is None:
            self.unresolved = []
        if self.near_misses is None:
            self.near_misses = []


def _present(graph, labels) -> list[str]:
    """The subset of `labels` that actually exist in this graph -- so a per-label index seek is
    never issued for a label another language owns (which would be an all-node scan or a no-op)."""
    try:
        have = {r[0] for r in graph.ro_query("CALL db.labels()").result_set}
    except Exception:
        return list(labels)
    return [lb for lb in labels if lb in have]


def collect_linkages(items) -> tuple[list[str], list[str]]:
    """Split this ingest's emitted nodes into (real_linkages, placeholder_ids) by their `linkage`.
    A placeholder is marked `resolved:false` and its id *is* its linkage; a real node carries
    `linkage` and no `resolved`."""
    real: list[str] = []
    placeholder: list[str] = []
    for n in items:
        if not isinstance(n, GraphNode) or "linkage_key" not in n.properties:
            continue
        if n.properties.get("resolved") is False:
            placeholder.append(str(n.key["id"]))
        else:
            real.append(str(n.properties["linkage_key"]))
            real.extend(reexport_alias_keys(n.properties))
    return real, placeholder


# Origins that will never resolve to an in-graph definition -- language builtins / stdlib / libc /
# syscalls. Expected-permanent placeholders, so excluded from the "unresolved reference" signal:
# the diagnostic is about references a *sibling repo* is expected to define, not `len`/`malloc`.
_TERMINAL_ORIGINS = frozenset({"builtin", "stdlib", "libc", "system"})


def _parse_linkage(link: str) -> tuple[str, tuple[str, ...], str] | None:
    """Best-effort split of an ``extern://<lang>/<qualifier><sep><name>`` linkage key into
    ``(lang, qualifier_components, name)``. `sep` is ``::`` (python/c++/go/rust/…), ``#`` (java
    member), or absent (C: a program-global name). Qualifier components are split on ``.``/``/``/
    ``::`` so the prefix comparison below is language-agnostic. Returns None if it doesn't parse."""
    if not link.startswith("extern://"):
        return None
    lang, _, tail = link[len("extern://"):].partition("/")
    if not lang or not tail:
        return None
    for sep in ("::", "#"):
        if sep in tail:
            qualifier, name = tail.rsplit(sep, 1)
            break
    else:
        qualifier, name = "", tail
    return lang, tuple(p for p in re.split(r"::|[./]", qualifier) if p), name


def _prefix_mismatch(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """True when two qualifier component-tuples differ *only* by leading components -- one is a
    proper suffix of the other, e.g. ``('acme_payments','core')`` vs
    ``('acme-payments','acme_payments','core')``. That is the package-root / project-layout
    mismatch signature: identical trailing module path, extra directory levels bolted on the front
    (a `src/`- or hyphen-wrapped root leaking into the definer's dotted path). Unrelated same-name
    symbols, whose qualifiers share no tail, are rejected."""
    if a == b or not a or not b:
        return False
    short, long = (a, b) if len(a) < len(b) else (b, a)
    return long[-len(short):] == short


def _padded(row, n: int) -> list:
    """A result row as exactly `n` values (missing trailing columns -> None)."""
    vals = list(row)[:n]
    return vals + [None] * (n - len(vals))


# Bound on re-export chains followed (pkg -> pkg.sub -> pkg.sub.mod): real chains are 1-2 deep; the
# bound also stops a cycle.
_MAX_ALIAS_HOPS = 5


def reexport_alias_keys(props: dict) -> list[str]:
    """The alias linkage keys a Package node answers to: `<its linkage_key>::<Name>` for each
    `Name=<target>` entry of its `reexports` table (Python package `__init__` re-exports)."""
    base = props.get("linkage_key")
    if not base:
        return []
    return [f"{base}::{str(e).partition('=')[0]}" for e in props.get("reexports") or () if "=" in str(e)]


_PY_EXTERN = "extern://python/"  # only Python packages publish `reexports`


def _canonical_rows(writer: GraphWriter, keys, present, meta: dict) -> dict[str, list]:
    """linkage key -> its canonical (genuine definition) rows `(id, resolved, defined, origin)`, for
    the given `keys`; fills `meta` (qualified_name, decorators) for overload collapse. Index seeks."""
    keys = sorted(set(keys))
    found: dict[str, list[tuple[str, object, object, object]]] = {}
    if not keys:
        return found
    for label in present:
        rows = writer.ro_query(
            f"MATCH (n:{label}) WHERE n.linkage_key IN $ls "
            "RETURN n.linkage_key, n.id, n.resolved, n.defined, n.origin, n.qualified_name, n.decorators",
            {"ls": keys},
        ).result_set
        for row in rows:
            link, nid, resolved, defined, origin, qname, decorators = _padded(row, 7)
            if resolved is not False and defined is not False:
                found.setdefault(str(link), []).append((str(nid), resolved, defined, origin))
                meta[str(nid)] = (qname, decorators)
    return found


def _reexport_tables(writer: GraphWriter, pkgkeys) -> dict[str, str]:
    """`<pkgkey>::<Name>` -> the linkage key it re-exports, for the Packages whose `linkage_key` is
    in `pkgkeys` (an index seek on Package.linkage_key, never a scan)."""
    table: dict[str, str] = {}
    pkgkeys = sorted(set(pkgkeys))
    if not pkgkeys:
        return table
    rows = writer.ro_query(
        "MATCH (p:Package) WHERE p.linkage_key IN $pk AND p.reexports IS NOT NULL "
        "RETURN p.linkage_key, p.reexports",
        {"pk": pkgkeys},
    ).result_set
    for row in rows:
        pk, entries = _padded(row, 2)
        if isinstance(entries, (list, tuple)):
            for entry in entries:
                name, _, target = str(entry).partition("=")
                if name and target:
                    table[f"{pk}::{name}"] = target
    return table


def _alias_candidates(writer: GraphWriter, keys: list[str], present, meta: dict) -> dict[str, tuple[list, list]]:
    """alias key -> (canonical candidate rows of the definition it re-exports, the package keys the
    re-export chain passed through). Follows `<pkg>::Name` through each declaring Package's
    `reexports` table, up to _MAX_ALIAS_HOPS; at every hop an EXACT definition of the current key
    wins over following that package's own re-export entry (so a package that defines `Name` itself
    stops the chain there; several exact definitions are returned as-is and stay ambiguous)."""
    frontier = {k: k for k in keys if "::" in k}
    chain: dict[str, list[str]] = {k: [] for k in frontier}
    seen: dict[str, set[str]] = {k: {k} for k in frontier}
    out: dict[str, tuple[list, list]] = {}
    for hop in range(_MAX_ALIAS_HOPS + 1):
        if not frontier:
            break
        if hop:  # the original key has no exact definition (the caller checked); later hops may
            exact = _canonical_rows(writer, frontier.values(), present, meta)
            for orig, cur in list(frontier.items()):
                if exact.get(cur):
                    out[orig] = (exact[cur], chain[orig])
                    del frontier[orig]
        if hop == _MAX_ALIAS_HOPS or not frontier:
            break
        table = _reexport_tables(writer, {cur.rsplit("::", 1)[0] for cur in frontier.values()})
        nxt = {}
        for orig, cur in frontier.items():
            target = table.get(cur)
            if target and target not in seen[orig]:
                seen[orig].add(target)
                chain[orig].append(cur.rsplit("::", 1)[0])
                nxt[orig] = target
        frontier = nxt
    return out


def _reverse_alias_keys(writer: GraphWriter, targets) -> list[str]:
    """Alias keys (`<pkg>::Name`) whose re-export chain ends at one of `targets`, so a definition
    that arrives AFTER its re-exporting package (and its consumers) still heals their `<pkg>::Name`
    placeholders. Reads only Packages that carry a `reexports` table."""
    targets = {t for t in targets if t.startswith(_PY_EXTERN) and "::" in t}
    if not targets:
        return []
    rows = writer.ro_query(
        "MATCH (p:Package) WHERE p.reexports IS NOT NULL RETURN p.linkage_key, p.reexports"
    ).result_set
    rev: dict[str, list[str]] = {}
    for row in rows:
        pk, entries = _padded(row, 2)
        if isinstance(entries, (list, tuple)):
            for entry in entries:
                name, _, target = str(entry).partition("=")
                if name and target:
                    rev.setdefault(target, []).append(f"{pk}::{name}")
    found: set[str] = set()
    frontier = set(targets)
    for _ in range(_MAX_ALIAS_HOPS):
        nxt = {a for t in frontier for a in rev.get(t, ()) if a not in found and a not in targets}
        if not nxt:
            break
        found |= nxt
        frontier = nxt
    return sorted(found)


def _alias_placeholder(writer: GraphWriter, caller: str, target: str, label: str, alias: str,
                       lines) -> None:
    """Give `caller` an unresolved `alias` placeholder edge carrying `lines` (the alias's call
    sites), the way demote_node keeps a cross-repo reference retryable: a later ingest that brings
    the re-export back reconciles it again."""
    qname = alias.split("/", 3)[-1]
    writer.query(
        f"""
        MATCH (c:{SHARED_NODE_LABEL} {{id: $c}})-[e:CALLS]->(:{SHARED_NODE_LABEL} {{id: $t}})
        MERGE (ph:{label} {{id: $lk}})
          ON CREATE SET ph.name = $name, ph.qualified_name = $qname, ph.linkage_key = $lk,
                        ph.resolved = false, ph.origin = 'external', ph:{SHARED_NODE_LABEL}
        MERGE (c)-[e2:CALLS]->(ph)
          ON CREATE SET e2 = e
        SET e2.resolution = 'external', e2.lines = $lines, e2._via_alias = NULL,
            e2._alias_lines = NULL, e2._alias_pkgs = NULL, e2._alias_exact = NULL
        """,
        {"c": caller, "t": target, "lk": alias, "name": alias.rsplit("::", 1)[-1], "qname": qname,
         "lines": lines},
    )


def _revalidate_alias_edges(writer: GraphWriter, real_linkages, present) -> None:
    """Re-check edges healed THROUGH a re-export alias (marked `_via_alias`) when any Package their
    re-export chain passed through (`_alias_pkgs`, or the alias's own package) was (re)written in
    this ingest. An exact definition of the alias key now wins; otherwise the alias is followed
    again. A different target moves the alias's call sites there; no target demotes them to an
    `<alias>` placeholder -- the same outcome demote_node gives an exact cross-repo link whose
    definition disappears -- so the call can heal again later. An edge that also carries an exact
    (non-alias) call to the same target (`_alias_exact`) keeps that contribution where it is: only
    the alias's own call-site lines (`_alias_lines`) move or are demoted."""
    pkg_keys = [k for k in set(real_linkages) if k.startswith(_PY_EXTERN) and "::" not in k]
    if not pkg_keys or "Package" not in present:
        return
    # Only packages some healed edge's re-export chain passed through (`_alias_used`, set when the
    # edge was healed) can invalidate one, so an ingest that touches none of them never pays for the
    # scan of alias-healed CALLS edges below.
    touched = {str(r[0]) for r in writer.ro_query(
        "MATCH (p:Package) WHERE p.linkage_key IN $ls AND p._alias_used = true RETURN p.linkage_key",
        {"ls": sorted(pkg_keys)},
    ).result_set}
    if not touched:
        return
    rows = writer.ro_query(
        "MATCH (c)-[e:CALLS]->(t) WHERE e._via_alias IS NOT NULL "
        "RETURN c.id, t.id, [l IN labels(t) WHERE l IN $labels][0], e._via_alias, e._alias_pkgs, "
        "e._alias_exact, e.lines, e._alias_lines",
        {"labels": list(LINKABLE_LABELS)},
    ).result_set
    stale = []
    for row in rows:
        c, t, label, alias, pkgs, mixed, lines, alias_lines = _padded(row, 8)
        alias = str(alias)
        chain = {alias.rsplit("::", 1)[0], *(pkgs or ())}
        if chain & touched:
            stale.append((str(c), str(t), str(label or "Function"), alias, bool(mixed),
                          list(lines or []), list(alias_lines if alias_lines is not None else (lines or []))))
    if not stale:
        return
    meta: dict = {}
    aliases = sorted({s[3] for s in stale})
    exact = _canonical_rows(writer, aliases, present, meta)
    via = _alias_candidates(writer, [a for a in aliases if not exact.get(a)], present, meta)
    used = sorted({pk for a, (_, chain) in via.items() for pk in (a.rsplit("::", 1)[0], *chain)})
    if used:
        writer.query("MATCH (p:Package) WHERE p.linkage_key IN $pk SET p._alias_used = true", {"pk": used})
    for caller, target, label, alias, mixed, lines, alias_lines in stale:
        if exact.get(alias):
            rows_now, pkgs_now, still_alias = exact[alias], [], False
        else:
            rows_now, pkgs_now = via.get(alias, ([], []))
            still_alias = True
        canonical = _collapse_overloads([nid for nid, *_ in rows_now], meta)
        now = canonical[0] if len(canonical) == 1 else None
        if now == target:
            writer.query(
                f"MATCH (:{SHARED_NODE_LABEL} {{id: $c}})-[e:CALLS]->(:{SHARED_NODE_LABEL} {{id: $t}}) "
                "SET e._alias_pkgs = $pk, e._via_alias = CASE WHEN $still THEN e._via_alias ELSE NULL END",
                {"c": caller, "t": target, "pk": pkgs_now or None, "still": still_alias},
            )
            continue
        if mixed:  # keep the exact call where it is; only the alias's own sites move/demote
            writer.query(
                f"MATCH (:{SHARED_NODE_LABEL} {{id: $c}})-[e:CALLS]->(:{SHARED_NODE_LABEL} {{id: $t}}) "
                "SET e.lines = $rest, e._via_alias = NULL, e._alias_lines = NULL, e._alias_pkgs = NULL, "
                "e._alias_exact = NULL",
                {"c": caller, "t": target, "rest": sorted(x for x in lines if x not in set(alias_lines))},
            )
        if now is None:
            if mixed:
                # the exact edge stays; give the alias its own placeholder edge, cloned from it
                _alias_placeholder(writer, caller, target, label, alias, sorted(alias_lines))
            else:
                _alias_placeholder(writer, caller, target, label, alias, sorted(alias_lines))
                writer.query(
                    f"MATCH (:{SHARED_NODE_LABEL} {{id: $c}})-[e:CALLS]->(:{SHARED_NODE_LABEL} {{id: $t}}) "
                    "WHERE e._via_alias = $a DELETE e",
                    {"c": caller, "t": target, "a": alias},
                )
            continue
        writer.query(
            f"""
            MATCH (c:{SHARED_NODE_LABEL} {{id: $c}})-[e:CALLS]->(:{SHARED_NODE_LABEL} {{id: $t}})
            MATCH (n:{SHARED_NODE_LABEL} {{id: $n}})
            MERGE (c)-[e2:CALLS]->(n)
              ON CREATE SET e2 = e, e2.lines = $al, e2._alias_exact = NULL, e2._via_alias = $via,
                            e2._alias_lines = CASE WHEN $via IS NULL THEN NULL ELSE $al END,
                            e2._alias_pkgs = CASE WHEN $via IS NULL THEN NULL ELSE $pk END
              ON MATCH SET e2.lines = [x IN coalesce(e2.lines, []) WHERE NOT x IN $al] + $al,
                           e2._alias_exact = CASE WHEN $via IS NULL THEN e2._alias_exact ELSE true END,
                           e2._via_alias = coalesce($via, e2._via_alias),
                           e2._alias_lines = CASE WHEN $via IS NULL THEN e2._alias_lines ELSE $al END,
                           e2._alias_pkgs = CASE WHEN $via IS NULL THEN e2._alias_pkgs ELSE $pk END
            WITH e, $mixed AS mixed WHERE NOT mixed
            DELETE e
            """,
            {"c": caller, "t": target, "n": now, "al": sorted(alias_lines), "mixed": mixed,
             "via": alias if still_alias else None, "pk": pkgs_now or None},
        )


def _is_overload(decorators) -> bool:
    return any(str(d).rsplit(".", 1)[-1] == "overload" for d in (decorators or ()))


def _collapse_overloads(canonical: list[str], meta: dict) -> list[str]:
    """Several canonicals that are one function's `@overload` stubs plus its implementation (same
    `qualified_name`) are ONE symbol: keep the implementation -- the single candidate without an
    `overload` decorator. Anything else (different qualified names, or not exactly one
    implementation) is returned unchanged and stays a genuine clash."""
    if len(canonical) < 2:
        return canonical
    qnames = {meta.get(nid, (None, None))[0] for nid in canonical}
    if len(qnames) != 1 or None in qnames:
        return canonical
    impls = [nid for nid in canonical if not _is_overload(meta.get(nid, (None, None))[1])]
    return impls if len(impls) == 1 else canonical


def reconcile_linkages(writer: GraphWriter, real_linkages, placeholder_ids) -> ReconcileStats:
    """Fold, per linkage this ingest touched, every *absorbable* node into the one *canonical*
    definition that shares it. Order-independent, no scan (index-backed `linkage_key IN […]`).

    * **canonical** = a genuine definition: `resolved` is not false AND `defined` is not false.
      (A Python real has neither prop; a C real has `defined:true`.)
    * **absorbable** = a stand-in for the same symbol: a placeholder (`resolved:false`, Python/libc)
      OR a cross-repo declaration (`defined:false`, a C `extern` prototype in another repo).

    Absorb repoints the absorbable node's reference edges onto the canonical and deletes it. With
    exactly one canonical the merge is unambiguous; with two+ it's a genuine namespace clash
    (surfaced, not merged); with none the symbol stays unresolved everywhere. `real_linkages` /
    `placeholder_ids` are just the keys to look at -- their canonical/absorbable roles are decided
    here from the graph, so either ingest order converges."""
    stats = ReconcileStats()
    pass_token = uuid.uuid4().hex
    linkages = sorted(set(real_linkages) | set(placeholder_ids))
    if not linkages:
        return stats
    present = _present(writer, LINKABLE_LABELS)
    if "Package" in present:
        # A definition that arrives after the package re-exporting it: revisit those alias keys too.
        linkages = sorted(set(linkages) | set(_reverse_alias_keys(writer, real_linkages)))

    # All candidate nodes per linkage: (id, resolved, defined, origin). Per-label (index seek) and
    # only for labels present in this graph, so a label another language owns costs nothing here.
    by_link: dict[str, list[tuple[str, object, object, object]]] = {}
    meta: dict[str, tuple[object, object]] = {}  # node id -> (qualified_name, decorators)
    for label in present:
        rows = writer.ro_query(
            f"MATCH (n:{label}) WHERE n.linkage_key IN $ls "
            "RETURN n.linkage_key, n.id, n.resolved, n.defined, n.origin, n.qualified_name, n.decorators",
            {"ls": linkages},
        ).result_set
        for row in rows:
            link, nid, resolved, defined, origin, qname, decorators = _padded(row, 7)
            by_link.setdefault(str(link), []).append((str(nid), resolved, defined, origin))
            meta[str(nid)] = (qname, decorators)

    # Re-export aliases (a package `__init__` exposing `pkg.Name` for a symbol defined in
    # `pkg.sub.mod`): consulted ONLY for linkages with no exact-key definition, so an exact match
    # always wins. Resolved through the declaring Package node's `reexports` table (index seeks on
    # Package.linkage_key, chains followed) -- exact keys only, no loosening (ADR 0004 decision 6).
    def _is_canonical(resolved, defined) -> bool:
        return resolved is not False and defined is not False

    need_alias = [ln for ln in linkages if ln.startswith(_PY_EXTERN)
                  and not any(_is_canonical(r, d) for _, r, d, _ in by_link.get(ln, ()))]
    alias_cands = _alias_candidates(writer, need_alias, present, meta) if "Package" in present else {}
    for alias, (cands, _chain) in alias_cands.items():
        by_link.setdefault(alias, []).extend(cands)

    canonical_links: list[str] = []
    absorbed_into: set[str] = set()
    try:
        for link, cands in by_link.items():
            canonical = [nid for nid, resolved, defined, _ in cands if resolved is not False and defined is not False]
            canonical = _collapse_overloads(canonical, meta)
            absorbable = [nid for nid, resolved, defined, _ in cands if resolved is False or defined is False]
            if len(canonical) != 1:
                if len(canonical) > 1:
                    stats.ambiguous.append(link)
                elif any(resolved is False and str(origin or "external") not in _TERMINAL_ORIGINS
                         for _, resolved, _, origin in cands):
                    # 0 canonical AND an unresolved placeholder that is NOT a known-terminal builtin/libc
                    # -> a reference expecting a sibling-repo definition that never arrived. Candidate
                    # for the silent-miss diagnostic below.
                    stats.unresolved.append(link)
                continue  # 0 canonical -> unresolved everywhere; >1 -> genuine clash, leave it
            canonical_links.append(link)
            target = canonical[0]
            via = link if link in alias_cands else None
            pkgs = alias_cands[link][1] if via else None
            if via:  # mark the chain's packages so a later change to any of them revalidates this
                writer.query(
                    "MATCH (p:Package) WHERE p.linkage_key IN $pk SET p._alias_used = true",
                    {"pk": sorted({via.rsplit("::", 1)[0], *(pkgs or ())})},
                )
            for src in absorbable:
                if src != target:
                    _absorb(writer, absorbable_id=src, canonical_id=target, pass_token=pass_token,
                            via_alias=via, alias_pkgs=pkgs)
                    absorbed_into.add(target)
                    stats.absorbed += 1
    finally:
        # The per-pass line-union marker (see _absorb) must not outlive this pass, even on an error:
        # clear it on the edges into each canonical we absorbed onto (an id seek per canonical, no
        # scan), sorting the unioned lines so the result equals a fresh build in any ingest order.
        for canon in sorted(absorbed_into):
            for caller, rel, lines in writer.ro_query(
                f"MATCH (c)-[e]->(:{SHARED_NODE_LABEL} {{id: $canon}}) WHERE e._lines_pass IS NOT NULL "
                "RETURN c.id, type(e), e.lines",
                {"canon": canon},
            ).result_set:
                writer.query(
                    f"MATCH (:{SHARED_NODE_LABEL} {{id: $c}})-[e:{rel}]->(:{SHARED_NODE_LABEL} {{id: $canon}}) "
                    "SET e.lines = $lines, e._lines_pass = NULL",
                    {"c": caller, "canon": canon,
                     "lines": sorted(set(lines))[:20] if isinstance(lines, list) else lines},
                )

    _revalidate_alias_edges(writer, real_linkages, present)

    # Silent-miss diagnostic (the Gap-2 fix): an unresolved reference whose symbol name matches a
    # real definition emitted in THIS ingest, where the two linkage keys differ ONLY by a leading
    # module prefix, is almost always a package-root / project-layout bug -- a `src/` or wrapper dir
    # leaking into the definer's dotted path (see ADR 0004; `ids.dotted_module_path`). The reference
    # SHOULD have resolved; the exact-string match just missed by a prefix. Surface it, don't merge
    # (per ADR 0004 decision 6, a loosened match risks false joins -- the fix belongs in the key).
    # NB: catches a mismatch only when both sides are touched in one ingest (e.g. a whole tree
    # indexed as one project -- the exact footgun that stalled the audit). A definer ingested in a
    # *separate* pass from its consumer is a documented follow-up (needs a name-indexed placeholder
    # seek to stay scan-free), tracked with Gap 1.
    if stats.unresolved:
        parsed_canon = [(cl, _parse_linkage(cl)) for cl in canonical_links]
        for ph in stats.unresolved:
            p = _parse_linkage(ph)
            if p is None:
                continue
            for cl, c in parsed_canon:
                if c is not None and p[0] == c[0] and p[2] == c[2] and _prefix_mismatch(p[1], c[1]):
                    stats.near_misses.append((ph, cl))
                    break

    if stats.ambiguous:
        logger.warning("cross-repo linkage(s) with >1 definition, left unmerged: %s", sorted(set(stats.ambiguous)))
    if stats.near_misses:
        logger.warning(
            "cross-repo resolution MISS: %d reference(s) matched a definition that differs ONLY by a "
            "module prefix -- likely a package-root / project-layout mismatch (the defining repo's "
            "dotted path has an extra leading dir, e.g. a `src/`- or hyphen-wrapped root). These did "
            "NOT resolve and left a dangling `resolved:false` placeholder: %s",
            len(stats.near_misses),
            [f"{ph}  ~  {cl}" for ph, cl in stats.near_misses],
        )
    if stats.unresolved:
        logger.info(
            "cross-repo: %d external reference(s) unresolved after reconciliation (no definition in "
            "graph yet)%s",
            len(stats.unresolved),
            f"; {len(stats.near_misses)} look like a layout mismatch (see warning above)"
            if stats.near_misses else "",
        )
    return stats


def demote_cross_repo(graph, project_prefixes: list[str]) -> None:
    """Reverse of `_absorb`, run BEFORE a project's subgraph is deleted (`forget`, ADR 0003/0004):
    preserve references from *other* repos into this one. For each externally-visible node in this
    project that a node outside it still points at, re-materialize a shallow placeholder at the
    node's `linkage` and repoint those inbound edges onto it -- so the consumer keeps an (now
    unresolved) reference instead of losing the edge when the definer is deleted. Intra-project
    edges are left to die with the subgraph. Point/range seeks per (prefix, label, rel); no scan.

    `project_prefixes` is the project's `<lang>://<name>` roots (one per language)."""
    exacts = list(project_prefixes)
    rngs = [p + "/" for p in project_prefixes]
    labels = _present(graph, LINKABLE_LABELS)
    for exact in exacts:
        lo, hi = exact + "/", exact + "/" + chr(0xFFFF)
        for label in labels:
            for rel in _REF_RELS:
                # `real` is index-seeked by a pure id range (linkable nodes always live under the
                # `<prefix>/` subtree, never at the Project root) -- deliberately NOT `id = exact OR
                # (range)`, whose OR form over-matches sibling projects. `c` is filtered to nodes
                # OUTSIDE this project (its inbound edge is the cross-repo one worth preserving).
                graph.query(
                    f"""
                    MATCH (c)-[e:{rel}]->(real:{label})
                    WHERE real.id >= $lo AND real.id < $hi
                      AND real.linkage_key IS NOT NULL
                      AND NOT (c.id IN $exacts OR any(pfx IN $rngs WHERE c.id STARTS WITH pfx))
                    MERGE (ph:{label} {{id: real.linkage_key}})
                      ON CREATE SET ph.name = real.name, ph.linkage_key = real.linkage_key,
                                    ph.resolved = false, ph.origin = 'external', ph:{SHARED_NODE_LABEL}
                    MERGE (c)-[e2:{rel}]->(ph)
                      ON CREATE SET e2 = e, e2.resolution = 'external'
                    DELETE e
                    """,
                    {"lo": lo, "hi": hi, "exacts": exacts, "rngs": rngs},
                )


def demote_node(writer: GraphWriter, proj_prefix: str, label: str, node_id: str, child_sep: str = ".") -> str | None:
    """Per-node reverse of `_absorb`, for the incremental delete path (ADR 0005). A linkable node
    about to be deleted that still has inbound reference edges from *outside* this project is
    re-materialized as its `extern://` placeholder, and only those cross-repo edges are repointed
    onto it (intra-project edges die with the node -- their sources are in the emit set and
    re-resolve). Returns the placeholder's linkage key if one was materialized (so the caller can
    feed it to `reconcile_linkages` -- a *third* repo may define the same symbol, in which case the
    consumer must re-point onto that real definition, not the placeholder), else None.

    FalkorDB removes ALL of a node's edges when the node is deleted (no DETACH/plain distinction),
    so this MUST run BEFORE the node is deleted or the cross-repo edge is lost."""
    rows = writer.ro_query(
        f"MATCH (n:{label} {{id: $id}}) RETURN n.linkage_key, n.name", {"id": node_id}
    ).result_set
    if not rows or not rows[0][0]:
        return None  # no cross-repo identity -> nothing to preserve (e.g. a file-local static)
    linkage_key, name = str(rows[0][0]), rows[0][1]
    # The placeholder's qualified_name is the linkage-key tail (`extern://<lang>/<qualified>`), which
    # is exactly what each ingestor's own external-placeholder emitter writes (C: the bare name; Go:
    # `pkg.Name`). Deriving it here -- rather than from the real node's qualified_name, which some
    # ingestors don't set on callables (Go) -- keeps a demoted placeholder byte-identical to a
    # from-scratch one across languages.
    qname = linkage_key.split("/", 3)[-1] if linkage_key.startswith("extern://") else name
    lo, hi = proj_prefix + "/", proj_prefix + "/" + chr(0xFFFF)
    # Intra-project test for the caller `c`. Most languages key every entity under `<proj>/…`, so the
    # `[lo, hi)` range suffices. Rust (and any future crate-root-collapse layout) hangs crate-root
    # items directly off the Project node -- `rust://p.fn.x` -- where the char after the project id is
    # the child separator `.` (0x2E), which sorts *below* `/` (0x2F) and so falls OUTSIDE `[lo, hi)`.
    # Such an intra-crate caller would be misclassified as cross-repo and its removed callee spuriously
    # demoted to an `extern://` placeholder a full recompute never has. `STARTS WITH proj+child_sep`
    # catches those; it's a no-op for `/`-only layouts (nothing keys at `<proj>.`) and can't match a
    # sibling project (`rust://p2` does not start with `rust://p.`). Same class of fix as Go's demote.
    projsep = proj_prefix + child_sep
    # Seed on the id-bound node's index then expand INBOUND to the caller `c`: the two-stage
    # `MATCH (n {id}) WITH n MATCH (n)<-[e]-(c)` form plans as Node By Index Scan(n) + Conditional
    # Traverse (verified via GRAPH.EXPLAIN); a single combined pattern all-node-scans the unlabeled c.
    ext = writer.ro_query(
        f"MATCH (n:{label} {{id: $id}}) WITH n MATCH (n)<-[e]-(c) WHERE type(e) IN $rels "
        "AND NOT (c.id = $proj OR (c.id >= $lo AND c.id < $hi) OR c.id STARTS WITH $projsep) RETURN count(e)",
        {"id": node_id, "rels": list(_REF_RELS), "proj": proj_prefix, "lo": lo, "hi": hi, "projsep": projsep},
    ).result_set
    if not ext or not ext[0][0]:
        return None  # only intra-project (emit-set) callers -> they re-resolve; plain delete is fine
    for rel in _REF_RELS:
        writer.query(
            f"""
            MATCH (real:{label} {{id: $id}}) WITH real
            MATCH (real)<-[e:{rel}]-(c)
            WHERE NOT (c.id = $proj OR (c.id >= $lo AND c.id < $hi) OR c.id STARTS WITH $projsep)
            MERGE (ph:{label} {{id: $lk}})
              ON CREATE SET ph.name = $name, ph.qualified_name = $qname, ph.linkage_key = $lk,
                            ph.resolved = false, ph.origin = 'external', ph:{SHARED_NODE_LABEL}
            MERGE (c)-[e2:{rel}]->(ph) ON CREATE SET e2 = e, e2.resolution = 'external'
            DELETE e
            """,
            {"id": node_id, "lk": linkage_key, "name": name, "qname": qname, "proj": proj_prefix,
             "lo": lo, "hi": hi, "projsep": projsep},
        )
    return linkage_key


# Containment edges whose child nodes an absorbed declaration owns (a C `extern` prototype brings
# its own Parameters) -- deleted with it so no orphan children linger.
_OWNED_CHILD_RELS = ("HAS_PARAMETER", "HAS_FIELD")


def _absorb(writer: GraphWriter, absorbable_id: str, canonical_id: str, pass_token: str = "",
            via_alias: str | None = None, alias_pkgs: list[str] | None = None) -> None:
    """Repoint every reference edge from the absorbable node onto the canonical definition, then
    delete the absorbable node (a placeholder, or a cross-repo declaration) and any children it
    owned. Edge properties carry over; `resolution` becomes 'resolved' (it now targets a real
    definition).

    Call-site `lines` when the edge already exists:
      * same reconcile pass (`pass_token`): several placeholders folding onto one edge UNION;
      * an exact placeholder onto an edge from an earlier pass: its lines are fresh from the
        caller's current source and REPLACE the stale ones (the call moved), keeping any alias sites;
      * an alias placeholder onto an edge with no alias yet (a direct call the resolver emitted, or
        an exact link): that edge's lines are current exact evidence, so they are UNIONED.
    An alias contribution (`via_alias`) keeps its own sites in `_alias_lines` and the packages its
    re-export chain passed through in `_alias_pkgs`; when the edge also carries an exact call
    (`_alias_exact`), only those alias sites are swapped on a later pass, so revalidation can move or
    demote the alias without touching the exact call."""
    for rel in _REF_RELS:
        writer.query(
            f"""
            MATCH (c)-[e:{rel}]->(p:{SHARED_NODE_LABEL} {{id: $src}})
            MATCH (r:{SHARED_NODE_LABEL} {{id: $canon}})
            MERGE (c)-[e2:{rel}]->(r)
              ON CREATE SET e2 = e, e2.resolution = 'resolved',
                            e2._via_alias = $via, e2._alias_pkgs = $pkgs,
                            e2._alias_lines = CASE WHEN $via IS NULL THEN NULL ELSE e.lines END,
                            e2._lines_pass = CASE WHEN e.lines IS NULL THEN NULL ELSE $pass END
              ON MATCH SET e2.lines = CASE
                  WHEN e.lines IS NULL THEN e2.lines
                  WHEN e2._lines_pass = $pass OR ($via IS NOT NULL AND e2._via_alias IS NULL)
                    THEN coalesce(e2.lines, []) + [x IN e.lines WHERE NOT x IN coalesce(e2.lines, [])]
                  WHEN $via IS NOT NULL AND NOT coalesce(e2._alias_exact, false)
                    THEN e.lines
                  WHEN $via IS NOT NULL
                    THEN [x IN coalesce(e2.lines, []) WHERE NOT x IN coalesce(e2._alias_lines, [])]
                         + [x IN e.lines WHERE NOT x IN [y IN coalesce(e2.lines, [])
                                                         WHERE NOT y IN coalesce(e2._alias_lines, [])]]
                  WHEN e2._via_alias IS NOT NULL
                    THEN coalesce(e2._alias_lines, []) + [x IN e.lines WHERE NOT x IN coalesce(e2._alias_lines, [])]
                  ELSE e.lines END,
                e2._alias_lines = CASE
                  WHEN $via IS NULL OR e.lines IS NULL THEN e2._alias_lines
                  WHEN e2._lines_pass = $pass AND e2._alias_lines IS NOT NULL
                    THEN e2._alias_lines + [x IN e.lines WHERE NOT x IN e2._alias_lines]
                  ELSE e.lines END,
                e2._alias_exact = CASE
                  WHEN $via IS NOT NULL AND e2._via_alias IS NULL THEN true
                  WHEN $via IS NULL AND e2._via_alias IS NOT NULL THEN true
                  ELSE e2._alias_exact END,
                e2._via_alias = coalesce($via, e2._via_alias),
                e2._alias_pkgs = CASE WHEN $via IS NULL THEN e2._alias_pkgs ELSE $pkgs END,
                e2._lines_pass = CASE WHEN e.lines IS NULL THEN e2._lines_pass ELSE $pass END
            DELETE e
            """,
            {"src": absorbable_id, "canon": canonical_id, "pass": pass_token, "via": via_alias,
             "pkgs": alias_pkgs or None},
        )
    for rel in _OWNED_CHILD_RELS:
        writer.query(
            f"MATCH (p:{SHARED_NODE_LABEL} {{id: $src}})-[:{rel}]->(child) DETACH DELETE child",
            {"src": absorbable_id},
        )
    writer.query(f"MATCH (p:{SHARED_NODE_LABEL} {{id: $src}}) DETACH DELETE p", {"src": absorbable_id})
