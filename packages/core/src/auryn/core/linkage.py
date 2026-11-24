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
    near_misses: list[tuple[str, str]] = None  # (placeholder, a near def) differing ONLY by a module
    #                               prefix but with 2+ candidate defs -- AMBIGUOUS, left unmerged
    resolved_near_misses: list[tuple[str, str]] = None  # (placeholder, def) auto-resolved by the
    #                               suffix match -- a UNIQUE layout-mismatch def, folded in

    def __post_init__(self) -> None:
        if self.ambiguous is None:
            self.ambiguous = []
        if self.unresolved is None:
            self.unresolved = []
        if self.near_misses is None:
            self.near_misses = []
        if self.resolved_near_misses is None:
            self.resolved_near_misses = []


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
    linkages = sorted(set(real_linkages) | set(placeholder_ids))
    if not linkages:
        return stats

    # All candidate nodes per linkage: (id, resolved, defined, origin). Per-label (index seek) and
    # only for labels present in this graph, so a label another language owns costs nothing here.
    by_link: dict[str, list[tuple[str, object, object, object]]] = {}
    for label in _present(writer, LINKABLE_LABELS):
        rows = writer.ro_query(
            f"MATCH (n:{label}) WHERE n.linkage_key IN $ls "
            "RETURN n.linkage_key, n.id, n.resolved, n.defined, n.origin",
            {"ls": linkages},
        ).result_set
        for link, nid, resolved, defined, origin in rows:
            by_link.setdefault(str(link), []).append((str(nid), resolved, defined, origin))

    canonical_links: list[str] = []
    for link, cands in by_link.items():
        canonical = [nid for nid, resolved, defined, _ in cands if resolved is not False and defined is not False]
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
        for src in absorbable:
            if src != target:
                _absorb(writer, absorbable_id=src, canonical_id=target)
                stats.absorbed += 1

    # Package-root / project-layout resolution: an unresolved reference whose symbol name matches a
    # real definition emitted in THIS ingest, where the two linkage keys differ ONLY by a leading
    # module prefix, is a package-root leak -- the SAME file resolves under two dotted identities (a
    # package root nested under, or put on sys.path beside, its longer file-path spelling, e.g.
    # `backend.llm_client` vs `plugins.com.esl.feltfixinator.backend.backend.llm_client`). The
    # reference SHOULD have resolved; the exact-string match just missed by the prefix.
    #   - EXACTLY ONE canonical def is suffix-compatible -> AUTO-RESOLVE (absorb). The uniqueness gate
    #     is what makes this safe: it's precisely the false-join risk ADR 0004 decision 6 worried about,
    #     so a loosened match is admitted ONLY when there is no ambiguity about which def is meant.
    #   - TWO+ candidates -> a genuine clash; left unresolved + warned, never merged.
    # NB: resolves only when both sides are touched in one ingest (a whole tree indexed as one project
    # -- the reported footgun). A definer ingested in a *separate* pass is a follow-up (Gap 1: needs a
    # name-indexed placeholder seek to stay scan-free).
    if stats.unresolved:
        parsed_canon = [(cl, _parse_linkage(cl)) for cl in canonical_links]
        still_unresolved: list[str] = []
        for ph in stats.unresolved:
            p = _parse_linkage(ph)
            if p is None:
                still_unresolved.append(ph)
                continue
            matches = [cl for cl, c in parsed_canon
                       if c is not None and p[0] == c[0] and p[2] == c[2] and _prefix_mismatch(p[1], c[1])]
            if len(matches) == 1:
                cl = matches[0]
                target = next(nid for nid, resolved, defined, _ in by_link[cl]
                              if resolved is not False and defined is not False)
                for src, resolved, defined, _ in by_link[ph]:
                    if (resolved is False or defined is False) and src != target:
                        _absorb(writer, absorbable_id=src, canonical_id=target)
                        stats.absorbed += 1
                stats.resolved_near_misses.append((ph, cl))
            elif matches:
                stats.near_misses.append((ph, matches[0]))  # 2+ candidate defs -> ambiguous, don't merge
                still_unresolved.append(ph)
            else:
                still_unresolved.append(ph)
        stats.unresolved = still_unresolved

    if stats.ambiguous:
        logger.warning("cross-repo linkage(s) with >1 definition, left unmerged: %s", sorted(set(stats.ambiguous)))
    if stats.resolved_near_misses:
        logger.info(
            "cross-repo: auto-resolved %d reference(s) to a definition that differed only by a module "
            "prefix (package-root / project-layout mismatch, unique match): %s",
            len(stats.resolved_near_misses),
            [f"{ph}  ->  {cl}" for ph, cl in stats.resolved_near_misses],
        )
    if stats.near_misses:
        logger.warning(
            "cross-repo resolution MISS: %d reference(s) matched MORE THAN ONE definition differing only "
            "by a module prefix -- an ambiguous package-root / project-layout mismatch, left unmerged to "
            "avoid a false join (a `resolved:false` placeholder remains): %s",
            len(stats.near_misses),
            [f"{ph}  ~  {cl}" for ph, cl in stats.near_misses],
        )
    if stats.unresolved:
        logger.info(
            "cross-repo: %d external reference(s) unresolved after reconciliation (no definition in "
            "graph yet)%s",
            len(stats.unresolved),
            f"; {len(stats.near_misses)} look like an ambiguous layout mismatch (see warning above)"
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


def _absorb(writer: GraphWriter, absorbable_id: str, canonical_id: str) -> None:
    """Repoint every reference edge from the absorbable node onto the canonical definition, then
    delete the absorbable node (a placeholder, or a cross-repo declaration) and any children it
    owned. Edge properties carry over; `resolution` becomes 'resolved' (it now targets a real
    definition). ON CREATE so a pre-existing direct edge isn't clobbered."""
    for rel in _REF_RELS:
        writer.query(
            f"""
            MATCH (c)-[e:{rel}]->(p:{SHARED_NODE_LABEL} {{id: $src}})
            MATCH (r:{SHARED_NODE_LABEL} {{id: $canon}})
            MERGE (c)-[e2:{rel}]->(r)
              ON CREATE SET e2 = e, e2.resolution = 'resolved'
            DELETE e
            """,
            {"src": absorbable_id, "canon": canonical_id},
        )
    for rel in _OWNED_CHILD_RELS:
        writer.query(
            f"MATCH (p:{SHARED_NODE_LABEL} {{id: $src}})-[:{rel}]->(child) DETACH DELETE child",
            {"src": absorbable_id},
        )
    writer.query(f"MATCH (p:{SHARED_NODE_LABEL} {{id: $src}}) DETACH DELETE p", {"src": absorbable_id})
