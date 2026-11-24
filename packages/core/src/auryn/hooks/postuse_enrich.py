"""PostToolUse enrichment hook -- inject code graph context into the agent's reflex tools.

The agent's reflex is Grep/Read/Bash, not our MCP tools. So instead of asking it to change
behaviour, we enrich the tools it already uses: after each matched tool call we look the subject
up in the code graph and, when there's a confident, high-signal answer, hand back a compact block
of what grep/read can't see -- callers, callees, blast radius, related code.

Design constraints (all of them serve the "don't bloat context / don't add latency" goal):

  * **Fail-silent.** Any error, missing graph, or timeout -> emit nothing, exit 0. The hook can
    never break or slow the agent beyond its own budget.
  * **Gated.** Fire only on a *confident* resolution (an exact or near-exact symbol match, and
    not too many of them). A grep for a common word that hits hundreds of symbols emits nothing.
  * **Capped.** Hard ceilings on callers/related shown and on total injected tokens.
  * **Deduped.** A symbol already enriched this session isn't enriched again.
  * **Self-labelling.** Everything is wrapped in a ``⟦auryn⟧ … ⟦/auryn⟧`` envelope
    whose first line says it came from the hook and is NOT part of the tool output -- because
    Claude Code injects ``additionalContext`` as an unattributed system-reminder.

The module splits into a pure ``enrich(event, ctx, cfg) -> EnrichResult`` core (no I/O; the
replay micro-benchmark calls it directly, reusing one ``GraphContext`` across thousands of
events) and a thin ``main()`` that does stdin JSON -> enrich -> stdout JSON + a metrics line.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# ── configuration ────────────────────────────────────────────────────────────────────────

_CALLABLE_LABELS = {"Function", "Method", "Procedure", "Constructor", "Routine"}
_TYPE_LABELS = {"Class", "Interface", "Struct", "Type", "Enum", "Trait", "Record"}
_SYMBOL_LABELS = _CALLABLE_LABELS | _TYPE_LABELS

# A plausible source-code identifier: word-ish, at least 3 chars so we don't fire on `id`/`os`.
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{2,}$")
_IDENT_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
# Regex metacharacters -- their presence means a Grep pattern is a regex, not a bare name.
_REGEX_META = set(r".^$*+?()[]{}|\\")

_ENVELOPE_OPEN = "⟦auryn⟧"
_ENVELOPE_CLOSE = "⟦/auryn⟧"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default


def _env_flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() not in ("0", "false", "no", "off", "")


@dataclass
class HookConfig:
    """All tunables come from the environment so the benchmark can vary them per arm without
    touching code. Defaults are chosen to keep the hook cheap and quiet."""

    enabled: bool = field(default_factory=lambda: _env_flag("AURYN_HOOK_ENABLED", True))
    graph_name: str | None = field(default_factory=lambda: os.environ.get("AURYN_HOOK_GRAPH") or None)
    host: str = field(default_factory=lambda: os.environ.get("FALKORDB_HOST", "localhost"))
    port: int = field(default_factory=lambda: _env_int("FALKORDB_PORT", 6379))
    # gating
    max_matches: int = field(default_factory=lambda: _env_int("AURYN_HOOK_MAX_MATCHES", 3))
    max_callers: int = field(default_factory=lambda: _env_int("AURYN_HOOK_MAX_CALLERS", 8))
    max_callees: int = field(default_factory=lambda: _env_int("AURYN_HOOK_MAX_CALLEES", 6))
    max_related: int = field(default_factory=lambda: _env_int("AURYN_HOOK_MAX_RELATED", 3))
    max_file_symbols: int = field(default_factory=lambda: _env_int("AURYN_HOOK_MAX_FILE_SYMBOLS", 6))
    max_tokens: int = field(default_factory=lambda: _env_int("AURYN_HOOK_MAX_TOKENS", 220))
    # feature toggles
    vector: bool = field(default_factory=lambda: _env_flag("AURYN_HOOK_VECTOR", False))
    related: bool = field(default_factory=lambda: _env_flag("AURYN_HOOK_RELATED", True))
    impact_on_edit: bool = field(default_factory=lambda: _env_flag("AURYN_HOOK_EDIT_IMPACT", True))
    dedup: bool = field(default_factory=lambda: _env_flag("AURYN_HOOK_DEDUP", True))
    metrics_path: str | None = field(default_factory=lambda: os.environ.get("AURYN_HOOK_METRICS") or None)


# ── result / metrics ─────────────────────────────────────────────────────────────────────


@dataclass
class EnrichResult:
    """Outcome of one tool event. ``context`` is the string to inject (None -> inject nothing).
    ``reason`` explains a no-fire ("not-a-symbol", "ambiguous", "no-graph-hit", "deduped",
    "empty-graph", ...) or is "ok" when fired -- the micro-benchmark's suppression breakdown."""

    context: str | None = None
    reason: str = "ok"
    symbols: list[str] = field(default_factory=list)

    @property
    def fired(self) -> bool:
        return self.context is not None

    @property
    def injected_chars(self) -> int:
        return len(self.context) if self.context else 0

    @property
    def injected_tokens(self) -> int:
        # ~4 chars/token is the usual rough estimate; good enough for an overhead budget.
        return (self.injected_chars + 3) // 4


# ── graph context (lazy, reusable) ───────────────────────────────────────────────────────


class GraphContext:
    """Holds the (lazily opened) FalkorDB connection, the selected graph, cached status, and a
    lazily loaded embedder. One instance per hook invocation in ``main()``; one shared instance
    across the whole corpus in the replay micro-benchmark (so connection cost is paid once)."""

    def __init__(self, cfg: HookConfig):
        self.cfg = cfg
        self._graph = None
        self._status: dict | None = None
        self._embedder_tried = False
        self._embedder = None
        self._connect_failed = False

    @property
    def graph(self):
        if self._graph is None and not self._connect_failed:
            try:
                from auryn.core.graph_client import FalkorDBConfig, connect_client
                from auryn.core.project import default_graph_name

                client = connect_client(FalkorDBConfig(host=self.cfg.host, port=self.cfg.port))
                name = self.cfg.graph_name or default_graph_name()
                self._graph = client.select_graph(name)
            except Exception:
                self._connect_failed = True
                self._graph = None
        return self._graph

    def status(self) -> dict:
        if self._status is None:
            try:
                from auryn.core import graph_queries as gq

                self._status = gq.graph_status(self.graph) if self.graph is not None else {"has_data": False}
            except Exception:
                self._status = {"has_data": False}
        return self._status

    def embedder(self):
        """The vector embedder, or None if disabled/unavailable. Loaded at most once; a model
        that can't load (offline / not installed) degrades related-search to lexical-only."""
        if not self.cfg.vector:
            return None
        if not self._embedder_tried:
            self._embedder_tried = True
            try:
                from auryn.core.embeddings import embeddings_enabled, get_embedder

                self._embedder = get_embedder() if embeddings_enabled() else None
            except Exception:
                self._embedder = None
        return self._embedder


# ── small helpers ────────────────────────────────────────────────────────────────────────


def _loc(node: dict) -> str:
    """A ``path:line`` locator for a lean node dict, or ''."""
    props = node.get("properties") or {}
    path = props.get("file_path") or props.get("path") or props.get("module") or props.get("qualified_name")
    line = props.get("line_start") or props.get("line")
    if not path:
        return f"line {line}" if line else ""
    return f"{path}:{line}" if line else str(path)


def _is_real_definition(node: dict) -> bool:
    """True only for a symbol actually defined in an indexed repo -- not a cross-repo placeholder.

    ADR 0004 turns every unresolved reference into a first-class ``Function`` node keyed
    ``extern://`` with ``resolved=false`` and an ``origin`` (external / stdlib / builtin). Those are
    useful for "who calls this syscall", but enriching a *grep* with "``get`` → builtins.dict.get"
    is pure noise -- so symbol lookup fires only on real definitions."""
    nid = node.get("id") or ""
    props = node.get("properties") or {}
    if nid.startswith("extern://"):
        return False
    if props.get("resolved") is False:
        return False
    if props.get("origin"):
        return False
    return True


def _resolution(node: dict) -> str | None:
    props = node.get("properties") or {}
    return props.get("resolution")


def _names(nodes: list[dict], limit: int) -> tuple[list[str], int]:
    """Distinct display names of ``nodes`` (first ``limit``), plus the total distinct count."""
    seen: list[str] = []
    for n in nodes:
        nm = n.get("name")
        if nm and nm not in seen:
            seen.append(nm)
    return seen[:limit], len(seen)


def _fmt_list(names: list[str], total: int) -> str:
    shown = " · ".join(names)
    if total > len(names):
        shown += f" · +{total - len(names)} more"
    return shown


# ── symbol resolution ────────────────────────────────────────────────────────────────────


def _extract_identifier(pattern: str) -> str | None:
    """Turn a Grep pattern (possibly a regex) into a single confident identifier, or None.

    A bare identifier is used directly. A regex is only mined for an identifier if it contains
    exactly one word-ish token -- if it has several (an alternation, a multi-part pattern) we
    can't say which symbol the agent meant, so we decline (better silent than wrong)."""
    if not pattern:
        return None
    pattern = pattern.strip()
    if _IDENT.match(pattern):
        return pattern
    if any(c in _REGEX_META for c in pattern):
        tokens = [t for t in _IDENT_TOKEN.findall(pattern) if t.lower() not in _STOP_TOKENS]
        # keep it only if the regex is essentially one identifier wrapped in anchors/metachars
        if len(set(tokens)) == 1:
            return tokens[0]
        return None
    # a multi-word phrase (spaces) -- not a symbol lookup
    return None


_STOP_TOKENS = {"def", "class", "function", "return", "self", "import", "from", "the"}


def resolve_symbols(ctx: GraphContext, ident: str, cfg: HookConfig) -> list[dict] | None:
    """Resolve ``ident`` to graph symbols. Returns a small confident list, or None to suppress
    (ambiguous / no hit). Prefers exact-name matches; only falls back to substring matches when
    there's no exact hit, and suppresses when the result is too ambiguous to be useful."""
    from auryn.core import graph_queries as gq

    graph = ctx.graph
    if graph is None:
        return None
    try:
        hits = gq.search_nodes(graph, None, ident, limit=50)
    except Exception:
        return None
    if not hits:
        return None

    real = [h for h in hits if _is_real_definition(h)]
    if not real:
        return None  # only cross-repo placeholders matched -> nothing worth injecting
    symbols = [h for h in real if (h.get("label") in _SYMBOL_LABELS)] or real
    exact = [h for h in symbols if (h.get("name") or "").lower() == ident.lower()]
    chosen = exact or symbols

    # dedupe by (name,label); an identifier defined once (or a couple of overloads) is a
    # confident hit -- one matching hundreds of things is noise, so we bail.
    distinct: dict[tuple, dict] = {}
    for h in chosen:
        distinct.setdefault((h.get("name"), h.get("label")), h)
    picked = list(distinct.values())
    if not picked:
        return None
    if not exact and len(picked) > cfg.max_matches:
        return None  # substring match that's too broad -> ambiguous
    return picked[: cfg.max_matches]


# ── per-symbol enrichment ────────────────────────────────────────────────────────────────


def _traversal_ids(node: dict) -> list[str]:
    """The ids to traverse for one symbol: its own id plus its ``linkage_key`` twin, if any.

    Cross-repo/monorepo resolution can leave a symbol's *caller* edges on an ``extern://`` twin
    (id == the real node's ``linkage_key``) while its *callee* edges sit on the real def -- a known
    reconciliation gap. Unioning both ids recovers a complete caller+callee picture regardless of
    which twin an edge landed on, without pulling in unrelated same-named symbols."""
    ids = [node.get("id")]
    lk = (node.get("properties") or {}).get("linkage_key")
    if lk and lk != node.get("id"):
        ids.append(lk)
    return [i for i in ids if i]


def _union_neighbors(graph, fn, ids: list[str]) -> list[dict]:
    """Run a callers/callees query over several ids and merge the node lists (deduped by id)."""
    merged: dict[str, dict] = {}
    for nid in ids:
        try:
            res = fn(graph, nid, max_depth=1, limit=200) or {}
        except Exception:
            continue
        for n in res.get("nodes", []):
            if n.get("id"):
                merged.setdefault(n["id"], n)
    return list(merged.values())


def _symbol_block(ctx: GraphContext, node: dict, cfg: HookConfig) -> str | None:
    """Render one symbol's enrichment lines (callers / callees / related), or None if the graph
    knows nothing beyond the definition itself (in which case grep already showed as much)."""
    from auryn.core import graph_queries as gq

    graph = ctx.graph
    name = node.get("name")
    label = node.get("label") or "Symbol"
    ids = _traversal_ids(node)
    lines: list[str] = []

    header = f"{name} → {label}"
    loc = _loc(node)
    if loc:
        header += f"  {loc}"
    res = _resolution(node)
    if res:
        header += f"   [resolution: {res}]"
    lines.append(header)

    self_ids = set(ids)
    caller_nodes = [n for n in _union_neighbors(graph, gq.callers, ids) if n.get("id") not in self_ids]
    caller_names, caller_total = _names(caller_nodes, cfg.max_callers)
    if caller_names:
        lines.append(f"  called by ({caller_total}): {_fmt_list(caller_names, caller_total)}")

    callee_nodes = [n for n in _union_neighbors(graph, gq.callees, ids) if n.get("id") not in self_ids]
    callee_names, callee_total = _names(callee_nodes, cfg.max_callees)
    if callee_names:
        lines.append(f"  calls ({callee_total}): {_fmt_list(callee_names, callee_total)}")

    if cfg.related and name:
        related = _related(ctx, name, node.get("id"), cfg)
        if related:
            lines.append(f"  related: {related}")

    # If all we have is the definition line, grep already conveyed that -> no signal to add.
    if len(lines) == 1:
        return None
    return "\n".join(lines)


def _related(ctx: GraphContext, name: str, self_id: str, cfg: HookConfig) -> str | None:
    """Semantically/lexically related symbols (hybrid search), excluding the symbol itself.
    Lexical-only unless the vector model is enabled+available -- keeps latency and cost down."""
    try:
        from auryn.core.embeddings import hybrid_search
        from auryn.core.graph_client import GraphWriter

        hits = hybrid_search(
            GraphWriter(ctx.graph),
            ctx.embedder(),
            name,
            k=cfg.max_related + 2,
            return_properties=("name",),
        )
    except Exception:
        return None
    out: list[str] = []
    for h in hits:
        if h.get("id") == self_id:
            continue
        nm = h.get("name")
        if nm:
            score = h.get("score")
            out.append(f"{nm} {score:.2f}" if isinstance(score, (int, float)) else str(nm))
        if len(out) >= cfg.max_related:
            break
    return " · ".join(out) if out else None


# ── tool dispatch ────────────────────────────────────────────────────────────────────────


_BASH_SEARCH = {"rg", "grep", "egrep", "fgrep", "ag", "ack"}
_BASH_READ = {"cat", "bat", "less", "head", "tail"}
_BASH_SEP = {"|", "||", "&&", ";", ">", ">>", "2>", "&"}


def _bash_query(command: str) -> tuple[str, str] | None:
    """Parse a Bash command into ('grep', identifier) or ('read', filepath) when it contains a
    recognisable ripgrep/grep/cat -- the reflex searches agents run through the shell instead of
    the Grep/Read tools. Conservative: returns None on anything ambiguous.

    Tokenised with shlex FIRST (so pipes/metachars inside a quoted regex don't fool us), then we
    scan for the first search/read program anywhere in the command (handles `cd x && rg foo`,
    `grep ... | head`) and read its first positional argument up to the next shell separator."""
    if not command:
        return None
    try:
        import shlex

        tokens = shlex.split(command)
    except ValueError:
        return None
    i = 0
    while i < len(tokens):
        prog = os.path.basename(tokens[i])
        if prog in _BASH_SEARCH or prog in _BASH_READ:
            positionals: list[str] = []
            j = i + 1
            while j < len(tokens) and tokens[j] not in _BASH_SEP:
                if not tokens[j].startswith("-"):
                    positionals.append(tokens[j])
                j += 1
            if not positionals:
                return None
            if prog in _BASH_SEARCH:
                ident = _extract_identifier(positionals[0])
                return ("grep", ident) if ident else None
            return ("read", positionals[0])
        i += 1
    return None


def enrich(event: dict, ctx: GraphContext, cfg: HookConfig) -> EnrichResult:
    """Pure enrichment core: PostToolUse event -> EnrichResult. No stdin/stdout, no metrics I/O,
    no process exit -- so it's directly unit-testable and replayable."""
    if not cfg.enabled:
        return EnrichResult(reason="disabled")
    tool = event.get("tool_name") or ""
    ti = event.get("tool_input") or {}

    if tool == "Grep":
        ident = _extract_identifier(ti.get("pattern", ""))
        if not ident:
            return EnrichResult(reason="not-a-symbol")
        return _enrich_symbol_lookup(event, ident, ctx, cfg, trigger=f'Grep "{ti.get("pattern", "")}"')

    if tool == "Bash":
        parsed = _bash_query(ti.get("command", ""))
        if not parsed:
            return EnrichResult(reason="bash-not-search")
        kind, arg = parsed
        if kind == "grep":
            return _enrich_symbol_lookup(event, arg, ctx, cfg, trigger=f'Bash grep "{arg}"')
        return _enrich_read(event, arg, ctx, cfg, trigger=f"Bash cat {arg}")

    if tool == "Read":
        fp = ti.get("file_path")
        if not fp:
            return EnrichResult(reason="no-file")
        return _enrich_read(event, fp, ctx, cfg, trigger=f"Read {os.path.basename(fp)}")

    if tool in ("Edit", "MultiEdit", "Write") and cfg.impact_on_edit:
        return _enrich_edit(event, ctx, cfg)

    return EnrichResult(reason="tool-not-matched")


def _enrich_symbol_lookup(event: dict, ident: str, ctx: GraphContext, cfg: HookConfig, *, trigger: str) -> EnrichResult:
    if not ctx.status().get("has_data"):
        return EnrichResult(reason="empty-graph")
    symbols = resolve_symbols(ctx, ident, cfg)
    if not symbols:
        return EnrichResult(reason="no-graph-hit")

    seen = _load_seen(event, cfg)
    blocks: list[str] = []
    used: list[str] = []
    for sym in symbols:
        if cfg.dedup and sym.get("id") in seen:
            continue
        block = _symbol_block(ctx, sym, cfg)
        if block:
            blocks.append(block)
            used.append(sym.get("name"))
            seen.add(sym.get("id"))
    if not blocks:
        return EnrichResult(reason="deduped" if symbols else "no-signal", symbols=[s.get("name") for s in symbols])

    _save_seen(event, cfg, seen)
    body = "\n".join(blocks)
    return EnrichResult(context=_envelope(ctx, cfg, trigger, body), reason="ok", symbols=used)


def _enrich_read(event: dict, file_path: str, ctx: GraphContext, cfg: HookConfig, *, trigger: str) -> EnrichResult:
    """After a Read/cat: what the file *exposes* structurally -- the symbols it defines and who,
    outside the file, depends on them (the file's blast radius). That's the thing a file read
    can't show you."""
    if not ctx.status().get("has_data"):
        return EnrichResult(reason="empty-graph")
    from auryn.core import impact as impact_mod

    rel = _repo_relative(event, file_path) or file_path
    try:
        syms = _file_defined_symbols(ctx.graph, rel)
    except Exception:
        return EnrichResult(reason="no-graph-hit")
    if not syms:
        return EnrichResult(reason="no-graph-hit")

    seen = _load_seen(event, cfg)
    from auryn.core import graph_queries as gq

    # each symbol's linkage_key twin (where cross-repo caller edges may live) -> unioned in below
    twins: dict[str, str] = {}
    try:
        rows = ctx.graph.ro_query(
            "MATCH (n) WHERE n.id IN $ids AND n.linkage_key IS NOT NULL RETURN n.id, n.linkage_key",
            {"ids": [s["id"] for s in syms]},
        ).result_set
        twins = {nid: lk for nid, lk in rows if lk}
    except Exception:
        pass
    file_ids = {s["id"] for s in syms} | set(twins.values())

    # rank the file's symbols by how many *external* callers they have -> the file's real surface
    ranked: list[tuple[int, dict, list[str]]] = []
    for s in syms:
        ids = [s["id"]] + ([twins[s["id"]]] if s["id"] in twins else [])
        callers = _union_neighbors(ctx.graph, gq.callers, ids)
        external = [c for c in callers if c.get("id") not in file_ids]
        names, total = _names(external, cfg.max_callers)
        ranked.append((total, s, names))
    ranked.sort(key=lambda r: -r[0])

    lines: list[str] = [f"defines {len(syms)} symbol(s); external dependents:"]
    shown = 0
    for total, s, names in ranked:
        if shown >= cfg.max_file_symbols:
            break
        if total == 0:
            continue
        lines.append(f"  {s['name']} ({s['kind']}) ← {total} caller(s): {_fmt_list(names, total)}")
        shown += 1
    if shown == 0:
        # nothing outside the file depends on it -- low signal, stay quiet
        return EnrichResult(reason="no-external-deps")

    _save_seen(event, cfg, seen)
    return EnrichResult(context=_envelope(ctx, cfg, trigger, "\n".join(lines)), reason="ok",
                        symbols=[s["name"] for _t, s, _n in ranked[:shown]])


def _file_defined_symbols(graph, path: str) -> list[dict]:
    """Symbols defined in ``path`` -- exact file match first, then a path-suffix fallback so a
    graph indexed under a different root (src-layout, monorepo subdir) still resolves. Covers the
    same three file<->symbol attachments as ``impact.symbols_in_ranges``."""
    from auryn.core import impact as impact_mod

    syms, _mapped = impact_mod.symbols_in_ranges(graph, {path: [(0, 10**9)]})
    if syms:
        return syms
    segs = [s for s in path.replace("\\", "/").split("/") if s]
    if not segs:
        return []
    suffix = "/" + "/".join(segs[-2:])  # last two components -> specific enough for a hint
    found: dict[str, dict] = {}
    for q in (
        "MATCH (n) WHERE n.file_path ENDS WITH $s AND n.line_start > 0 RETURN n.id, n.name, " + semantic_label("n"),
        "MATCH (m)-[:HAS_FUNCTION|HAS_CLASS|HAS_METHOD|HAS_PROCEDURE|HAS_TYPE]->(n) "
        "WHERE m.file_path ENDS WITH $s OR m.path ENDS WITH $s RETURN n.id, n.name, " + semantic_label("n"),
        "MATCH (n)-[:DEFINED_IN]->(f) WHERE f.path ENDS WITH $s OR f.file_path ENDS WITH $s "
        "RETURN n.id, n.name, " + semantic_label("n"),
    ):
        try:
            for nid, name, kind in graph.ro_query(q, {"s": suffix}).result_set:
                if nid:
                    found[nid] = {"id": nid, "name": name, "kind": kind}
        except Exception:
            pass
    return list(found.values())


def _enrich_edit(event: dict, ctx: GraphContext, cfg: HookConfig) -> EnrichResult:
    """After an Edit/Write: the blast radius of what just changed (feature #4, fired proactively
    at the moment of the change). Diffs the working tree, maps the changed lines to symbols, and
    reports who transitively depends on them + tests + cross-service reach."""
    if not ctx.status().get("has_data"):
        return EnrichResult(reason="empty-graph")
    from auryn.core import impact as impact_mod

    repo = _repo_root(event)
    if not repo:
        return EnrichResult(reason="no-repo-root")
    try:
        diff = impact_mod.git_diff(str(repo))
        changed, _deleted = impact_mod.parse_unified_diff(diff)
        report = impact_mod.impact_of_diff(ctx.graph, changed, max_depth=4, limit=500,
                                           repo=impact_mod.git_toplevel(str(repo)))
    except Exception:
        return EnrichResult(reason="no-diff")
    changed_syms = report.get("changed_symbols") or []
    if not changed_syms:
        return EnrichResult(reason="no-changed-symbols")
    imp = report.get("impact") or {}
    if imp.get("total_affected", 0) == 0 and not report.get("flags"):
        return EnrichResult(reason="no-impact")

    names = [s["name"] for s in changed_syms][:5]
    lines = [f"changed: {' · '.join(names)}"]
    direct = imp.get("direct_callers") or []
    if direct:
        lines.append(f"  direct callers ({len(direct)}): {_fmt_list(direct[:cfg.max_callers], len(direct))}")
    trans = imp.get("transitive") or []
    if trans:
        lines.append(f"  transitively affects {len(trans)} more")
    if imp.get("api_endpoints_reached"):
        lines.append(f"  reaches endpoints: {' · '.join(imp['api_endpoints_reached'][:5])}")
    if imp.get("tests_covering"):
        lines.append(f"  tests covering: {' · '.join(imp['tests_covering'][:5])}")
    elif imp.get("total_affected", 0) > 0:
        lines.append("  ⚠ no tests found on the blast radius")
    if imp.get("cross_service_consumers"):
        lines.append(f"  ⚠ cross-service: {' · '.join(imp['cross_service_consumers'])}")

    return EnrichResult(context=_envelope(ctx, cfg, "Edit (working-tree diff)", "\n".join(lines)),
                        reason="ok", symbols=names)


# ── envelope + budget ────────────────────────────────────────────────────────────────────


def _envelope(ctx: GraphContext, cfg: HookConfig, trigger: str, body: str) -> str | None:
    """Wrap ``body`` in the self-labelling provenance envelope, honouring the token budget. The
    first line makes the source explicit and warns it is NOT part of the tool output. When the body
    is over budget we keep the header plus as many body lines as fit (dropping the lowest-ranked
    tail), rather than collapsing to a useless header-only block."""
    status = ctx.status()
    name = cfg.graph_name or "auryn"
    nodes = status.get("node_count")
    stamp = f"graph={name}" + (f" ({nodes} nodes)" if nodes else "")
    head = (
        f"{_ENVELOPE_OPEN}  (auto-injected by the Auryn hook — NOT part of the tool result; "
        f"structural facts, verify before acting)\n"
        f"  trigger: {trigger}   ·   {stamp}"
    )

    def render(lines: list[str]) -> str:
        return head + "\n" + "\n".join(lines) + "\n" + _ENVELOPE_CLOSE

    def over(text: str) -> bool:
        return (len(text) + 3) // 4 > cfg.max_tokens

    lines = body.splitlines()
    text = render(lines)
    if not over(text):
        return text
    kept: list[str] = []
    for ln in lines:
        if over(render(kept + [ln, "  …(truncated)"])):
            break
        kept.append(ln)
    if not kept:
        return None
    return render(kept + ["  …(truncated)"] if len(kept) < len(lines) else kept)


# ── per-session dedup state ──────────────────────────────────────────────────────────────


def _state_path(event: dict, cfg: HookConfig) -> Path | None:
    session = event.get("session_id") or "default"
    session = re.sub(r"[^A-Za-z0-9_-]", "_", str(session))
    try:
        from auryn.core.home import home_dir

        d = home_dir() / "hook-state"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{session}.json"
    except Exception:
        return None


def _load_seen(event: dict, cfg: HookConfig) -> set:
    if not cfg.dedup:
        return set()
    p = _state_path(event, cfg)
    if p and p.exists():
        try:
            return set(json.loads(p.read_text()))
        except Exception:
            return set()
    return set()


def _save_seen(event: dict, cfg: HookConfig, seen: set) -> None:
    if not cfg.dedup:
        return
    p = _state_path(event, cfg)
    if p:
        try:
            p.write_text(json.dumps(sorted(x for x in seen if x)))
        except Exception:
            pass


# ── repo root resolution ─────────────────────────────────────────────────────────────────


def _repo_root(event: dict) -> Path | None:
    root = os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd")
    if not root:
        return None
    p = Path(root).expanduser()
    return p if p.exists() else None


def _repo_relative(event: dict, file_path: str) -> str | None:
    root = _repo_root(event)
    fp = Path(file_path).expanduser()
    if root:
        try:
            return os.path.relpath(fp.resolve(), root.resolve())
        except Exception:
            pass
    return file_path if not fp.is_absolute() else None


# ── metrics ──────────────────────────────────────────────────────────────────────────────


def _log_metrics(event: dict, cfg: HookConfig, result: EnrichResult, latency_ms: float) -> None:
    """Append one metrics line -- the micro-benchmark and the A/B harness both read this to
    account for fire rate, suppression breakdown, injected tokens, and hook latency. Best-effort;
    never raises."""
    path = cfg.metrics_path
    if not path:
        try:
            from auryn.core.home import home_dir

            path = str(home_dir() / "hook-metrics.jsonl")
        except Exception:
            return
    rec = {
        "session": event.get("session_id"),
        "tool": event.get("tool_name"),
        "fired": result.fired,
        "reason": result.reason,
        "symbols": result.symbols,
        "injected_chars": result.injected_chars,
        "injected_tokens": result.injected_tokens,
        "latency_ms": round(latency_ms, 1),
    }
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


# ── entrypoint ───────────────────────────────────────────────────────────────────────────


def main() -> int:
    """stdin JSON -> enrich -> stdout hookSpecificOutput JSON (+ a metrics line). Always exits 0
    and prints nothing on a no-fire, so it can never block or slow the agent."""
    started = time.monotonic()
    cfg = HookConfig()
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0
    result = EnrichResult(reason="error")
    try:
        ctx = GraphContext(cfg)
        result = enrich(event, ctx, cfg)
    except Exception:
        result = EnrichResult(reason="error")
    finally:
        _log_metrics(event, cfg, result, (time.monotonic() - started) * 1000)

    if result.context:
        json.dump(
            {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": result.context}},
            sys.stdout,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
