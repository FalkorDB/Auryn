"""Turn parsed source files (raw_model.RawPlsqlFile) into graph nodes/edges, rooted
under one file-based project's id (config.project_id -> "plsql://...").

This is the file-based counterpart to the live ingestors, and it unifies both halves
of a database's source into ONE subgraph:

- SQL DDL -> Table/Column/View/Index/Constraint/Sequence (+ HAS_COLUMN, foreign-key
  REFERENCES, CONSTRAINS, INDEXES) -- the same labels the live `sql` ingestor writes.
- PL/SQL -> Package/Procedure/Function/Parameter/Trigger/Type (+ CALLS/READS/WRITES).

The point of doing both in one pass: a procedure that writes `employees` links to the
*actual* `employees` Table node parsed from its `CREATE TABLE` -- columns, constraints
and all -- not a name-only stub. A stub Table is emitted only for a table referenced
by code but never defined by DDL anywhere in the project. (This is also why DDL and
PL/SQL can't be two independent GitLanguages: each language's sync recomputes-and-
diffs its whole project subgraph, so two syncs sharing `Table` ids would delete each
other's tables. One ingestor, one project, one delta.)

Ids root at the project (`plsql://Proj.table.employees`, `.table.employees.id` for a
column), not at a Database/Schema -- files carry no Oracle owner/schema, so a file
project and a live-DB schema of the "same" database stay separate subgraphs. Every
node/edge is tagged provenance="source".

Emission order matters for the merge-only BaseIngestor path (`run plsql`), whose batch
writer only guarantees nodes-before-edges *within* a 2000-item batch: a node emitted
after an edge that references it, across a batch boundary, drops the edge. So all
nodes (and the HAS_* edges to a parent already emitted) come first, and the cross-
cutting edges whose endpoints live in different tables/routines (foreign keys,
CALLS/READS/WRITES) come in a second pass, once every endpoint exists.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.plsql.raw_model import (
    RawIndexDef,
    RawPackage,
    RawPlsqlFile,
    RawRoutine,
    RawSequence,
    RawTable,
    RawTrigger,
    RawType,
    RawView,
)


class _MergedPackage:
    """A package assembled across however many files declared it -- spec (.pks) and
    body (.pkb) usually live apart. Members are keyed by (kind, name, param-types) so
    a spec signature and its matching body collapse to one node, with the body
    (which has source + call/table edges) preferred over the spec."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.spec_source: str | None = None
        self.body_source: str | None = None
        self.doc: str = ""
        self._members: dict[tuple, RawRoutine] = {}

    def absorb(self, pkg: RawPackage) -> None:
        self.spec_source = self.spec_source or pkg.spec_source
        self.body_source = self.body_source or pkg.body_source
        self.doc = self.doc or pkg.doc  # first non-empty doc (spec or body) wins
        for member in pkg.members:
            key = (member.kind, member.name, _param_sig(member))
            existing = self._members.get(key)
            if existing is None or (existing.source is None and member.source is not None):
                self._members[key] = member

    @property
    def members(self) -> list[RawRoutine]:
        return list(self._members.values())


def resolve(raw_files: Iterable[RawPlsqlFile], proj_id: str) -> Iterator[GraphNode | GraphEdge]:
    """Yield every node/edge for a file-based project (everything except the
    Project/Solution root, which the calling ingestor emits with its own
    provenance). `proj_id` is config.project_id(...)."""
    files = list(raw_files)
    packages = _merge_packages(files)
    standalones = _dedupe([r for f in files for r in f.routines])
    triggers = [t for f in files for t in f.triggers]
    types = _dedupe_types([t for f in files for t in f.types])
    tables = _dedupe_tables([t for f in files for t in f.tables])
    _apply_alterations(tables, [a for f in files for a in f.alterations])
    views = _dedupe_by_name([v for f in files for v in f.views])
    indexes = [i for f in files for i in f.indexes]
    sequences = _dedupe_by_name([s for f in files for s in f.sequences])

    routine_index: dict[str, tuple[str, str]] = {}
    ddl_table_names = {t.name.upper() for t in tables}
    stub_names = {n for n in _referenced_tables(packages, standalones, triggers, views, indexes) if n.upper() not in ddl_table_names}

    # Pass 1 -- every node, plus HAS_* edges to a parent emitted earlier in the pass.
    yield from _emit_tables(tables, proj_id)
    yield from _emit_table_stubs(stub_names, proj_id)
    yield from _emit_views(views, proj_id)
    yield from _emit_sequences(sequences, proj_id)
    yield from _emit_packages(packages, proj_id, routine_index)
    yield from _emit_standalones(standalones, proj_id, routine_index)
    yield from _emit_trigger_nodes(triggers, proj_id)
    yield from _emit_types(types, proj_id)
    yield from _emit_indexes(indexes, proj_id)
    yield from _emit_constraints(tables, proj_id)

    # Pass 2 -- cross-cutting edges, once every endpoint node above exists.
    yield from _emit_foreign_keys(tables, proj_id)
    yield from _emit_calls_reads_writes(packages, standalones, proj_id, routine_index)
    yield from _emit_trigger_edges(triggers, proj_id, routine_index)
    yield from _emit_view_reads(views, proj_id)


# -- Merging / de-duplication -------------------------------------------------------


def _merge_packages(files: list[RawPlsqlFile]) -> list[_MergedPackage]:
    merged: dict[str, _MergedPackage] = {}
    for f in files:
        for pkg in f.packages:
            merged.setdefault(pkg.name, _MergedPackage(pkg.name)).absorb(pkg)
    return list(merged.values())


def _dedupe(routines: list[RawRoutine]) -> list[RawRoutine]:
    out: dict[tuple, RawRoutine] = {}
    for r in routines:
        out.setdefault((r.kind, r.name, _param_sig(r)), r)
    return list(out.values())


def _dedupe_types(types: list[RawType]) -> list[RawType]:
    out: dict[str, RawType] = {}
    for t in types:
        out.setdefault(t.name, t)
    return list(out.values())


def _dedupe_tables(tables: list[RawTable]) -> list[RawTable]:
    """One RawTable per name -- the last CREATE wins if a table is defined twice."""
    out: dict[str, RawTable] = {}
    for t in tables:
        out[t.name.upper()] = t
    return list(out.values())


def _apply_alterations(tables: list[RawTable], alterations) -> None:
    """Merge `ALTER TABLE ... ADD ...` additions onto the by-name table (creating a
    partial table if it was only ALTERed here, defined by a CREATE elsewhere) so a
    schema split across CREATE + ALTER scripts reconstructs fully. Mutates `tables`."""
    by_name = {t.name.upper(): t for t in tables}
    for alteration in alterations:
        table = by_name.get(alteration.table.upper())
        if table is None:
            table = RawTable(name=alteration.table)
            by_name[alteration.table.upper()] = table
            tables.append(table)
        for column in alteration.columns:
            column.position = len(table.columns) + 1  # append after the existing columns
            table.columns.append(column)
        table.constraints.extend(alteration.constraints)
        table.foreign_keys.extend(alteration.foreign_keys)


def _dedupe_by_name(items: list) -> list:
    out: dict[str, object] = {}
    for item in items:
        out.setdefault(item.name.upper(), item)
    return list(out.values())


def _referenced_tables(packages, standalones, triggers, views, indexes) -> set[str]:
    """Every table name referenced by anything -- so an endpoint node exists before
    any READS/WRITES/HAS_TRIGGER/HAS_INDEX/READS(view) edge points at it, even when
    no DDL defines the table."""
    names: set[str] = set()
    for pkg in packages:
        for member in pkg.members:
            names.update(name for name, _ in member.table_refs)
    for routine in standalones:
        names.update(name for name, _ in routine.table_refs)
    for trigger in triggers:
        names.update(name for name, _ in trigger.table_refs)
        if trigger.table:
            names.add(trigger.table)
    for view in views:
        names.update(view.reads)
    for index in indexes:
        names.add(index.table)
    return names


# -- DDL emitters -------------------------------------------------------------------


def _emit_tables(tables: list[RawTable], proj_id: str) -> Iterator[GraphNode | GraphEdge]:
    for table in tables:
        table_id = _table_id(proj_id, table.name)
        yield GraphNode("Table", {"id": table_id}, {"name": table.name, "provenance": "source"})
        yield GraphEdge("HAS_TABLE", "Project", {"id": proj_id}, "Table", {"id": table_id})
        for column in table.columns:
            column_id = f"{table_id}.{column.name.lower()}"
            yield GraphNode(
                "Column",
                {"id": column_id},
                {
                    "name": column.name,
                    "ordinal_position": column.position,
                    "data_type": column.data_type,
                    "nullable": column.nullable,
                    "default_value": column.default_value,
                    "is_primary_key": column.is_primary_key,
                    "provenance": "source",
                },
            )
            yield GraphEdge("HAS_COLUMN", "Table", {"id": table_id}, "Column", {"id": column_id})


def _emit_table_stubs(stub_names: set[str], proj_id: str) -> Iterator[GraphNode | GraphEdge]:
    for name in sorted(stub_names):
        table_id = _table_id(proj_id, name)
        yield GraphNode(
            "Table", {"id": table_id}, {"name": name, "provenance": "source", "reference_only": True}
        )
        yield GraphEdge("HAS_TABLE", "Project", {"id": proj_id}, "Table", {"id": table_id})


def _emit_constraints(tables: list[RawTable], proj_id: str) -> Iterator[GraphNode | GraphEdge]:
    for table in tables:
        table_id = _table_id(proj_id, table.name)
        for ordinal, constraint in enumerate(table.constraints):
            name = constraint.name or f"{constraint.type.lower()}_{ordinal}"
            constraint_id = f"{table_id}.constraint.{name.lower()}"
            yield GraphNode(
                "Constraint",
                {"id": constraint_id},
                {"name": name, "type": constraint.type, "expression": constraint.expression, "provenance": "source"},
            )
            yield GraphEdge("HAS_CONSTRAINT", "Table", {"id": table_id}, "Constraint", {"id": constraint_id})
            for position, column in enumerate(constraint.columns, start=1):
                yield GraphEdge(
                    "CONSTRAINS",
                    "Constraint",
                    {"id": constraint_id},
                    "Column",
                    {"id": f"{table_id}.{column.lower()}"},
                    {"position": position},
                )


def _emit_foreign_keys(tables: list[RawTable], proj_id: str) -> Iterator[GraphEdge]:
    for table in tables:
        table_id = _table_id(proj_id, table.name)
        for fk in table.foreign_keys:
            ref_table_id = _table_id(proj_id, fk.ref_table)
            for constrained, referred in zip(fk.columns, fk.ref_columns):
                yield GraphEdge(
                    "REFERENCES",
                    "Column",
                    {"id": f"{table_id}.{constrained.lower()}"},
                    "Column",
                    {"id": f"{ref_table_id}.{referred.lower()}"},
                    {"constraint_name": fk.name, "provenance": "source"},
                )


def _emit_indexes(indexes: list[RawIndexDef], proj_id: str) -> Iterator[GraphNode | GraphEdge]:
    for index in indexes:
        table_id = _table_id(proj_id, index.table)
        index_id = f"{table_id}.index.{index.name.lower()}"
        yield GraphNode(
            "Index", {"id": index_id}, {"name": index.name, "unique": index.unique, "provenance": "source"}
        )
        yield GraphEdge("HAS_INDEX", "Table", {"id": table_id}, "Index", {"id": index_id})
        for position, column in enumerate(index.columns, start=1):
            yield GraphEdge(
                "INDEXES",
                "Index",
                {"id": index_id},
                "Column",
                {"id": f"{table_id}.{column.lower()}"},
                {"position": position},
            )


def _emit_views(views: list[RawView], proj_id: str) -> Iterator[GraphNode | GraphEdge]:
    for view in views:
        view_id = f"{proj_id}.view.{view.name.lower()}"
        yield GraphNode(
            "View", {"id": view_id}, {"name": view.name, "definition": view.definition, "provenance": "source"}
        )
        yield GraphEdge("HAS_VIEW", "Project", {"id": proj_id}, "View", {"id": view_id})


def _emit_view_reads(views: list[RawView], proj_id: str) -> Iterator[GraphEdge]:
    for view in views:
        view_id = f"{proj_id}.view.{view.name.lower()}"
        for table_name in dict.fromkeys(view.reads):  # a view can name the same table twice

            yield GraphEdge(
                "READS",
                "View",
                {"id": view_id},
                "Table",
                {"id": _table_id(proj_id, table_name)},
                {"provenance": "source"},
            )


def _emit_sequences(sequences: list[RawSequence], proj_id: str) -> Iterator[GraphNode | GraphEdge]:
    for sequence in sequences:
        sequence_id = f"{proj_id}.sequence.{sequence.name.lower()}"
        yield GraphNode("Sequence", {"id": sequence_id}, {"name": sequence.name, "provenance": "source"})
        yield GraphEdge("HAS_SEQUENCE", "Project", {"id": proj_id}, "Sequence", {"id": sequence_id})


# -- PL/SQL emitters ----------------------------------------------------------------


def _emit_packages(packages, proj_id, routine_index) -> Iterator[GraphNode | GraphEdge]:
    for pkg in packages:
        package_id = f"{proj_id}.package.{pkg.name.lower()}"
        yield GraphNode(
            "Package",
            {"id": package_id},
            {"name": pkg.name, "spec_source": pkg.spec_source, "body_source": pkg.body_source, "doc": pkg.doc, "provenance": "source"},
        )
        yield GraphEdge("HAS_PACKAGE", "Project", {"id": proj_id}, "Package", {"id": package_id})
        for routine, routine_id in _assign_ids(package_id, pkg.members):
            yield from _emit_routine("Package", package_id, routine, routine_id, routine_index)


def _emit_standalones(standalones, proj_id, routine_index) -> Iterator[GraphNode | GraphEdge]:
    for routine, routine_id in _assign_ids(proj_id, standalones):
        yield from _emit_routine("Project", proj_id, routine, routine_id, routine_index)


def _emit_routine(parent_label, parent_id, routine: RawRoutine, routine_id, routine_index) -> Iterator[GraphNode | GraphEdge]:
    label = "Function" if routine.kind == "FUNCTION" else "Procedure"
    properties = {"name": routine.name, "source": routine.source, "doc": routine.doc, "provenance": "source"}
    if label == "Function":
        properties["return_type"] = routine.return_type

    routine_index.setdefault(routine.name.upper(), (label, routine_id))

    yield GraphNode(label, {"id": routine_id}, properties)
    yield GraphEdge(f"HAS_{label.upper()}", parent_label, {"id": parent_id}, label, {"id": routine_id})
    yield from _emit_parameters(label, routine_id, routine)


def _emit_parameters(label, routine_id, routine: RawRoutine) -> Iterator[GraphNode | GraphEdge]:
    for param in routine.parameters:
        param_name = (param.name or f"pos{param.position}").lower()
        param_id = f"{routine_id}.param.{param_name}"
        yield GraphNode(
            "Parameter",
            {"id": param_id},
            {
                "name": param.name,
                "position": param.position,
                "direction": param.direction,
                "type": param.data_type,  # shared Parameter `type` property (Python/C/C# all use `type`)
                "has_default": param.has_default,
                "provenance": "source",
            },
        )
        yield GraphEdge(
            "HAS_PARAMETER", label, {"id": routine_id}, "Parameter", {"id": param_id}, {"position": param.position}
        )


def _emit_calls_reads_writes(packages, standalones, proj_id, routine_index) -> Iterator[GraphEdge]:
    for pkg in packages:
        package_id = f"{proj_id}.package.{pkg.name.lower()}"
        for routine, routine_id in _assign_ids(package_id, pkg.members):
            yield from _emit_edges_for(_routine_label(routine), routine_id, routine, proj_id, routine_index)
    for routine, routine_id in _assign_ids(proj_id, standalones):
        yield from _emit_edges_for(_routine_label(routine), routine_id, routine, proj_id, routine_index)


def _emit_edges_for(from_label, from_id, unit, proj_id, routine_index) -> Iterator[GraphEdge]:
    # A unit that calls the same routine (or touches the same table) several times -- the
    # norm in real code -- would otherwise emit several edges with an identical key. Dedupe
    # per unit, last-wins, keyed by (rel_type, to_id): all of a unit's edges share this
    # from_id, so this is exactly the set of duplicate keys, and last-wins matches how the
    # graph resolves a repeated MERGE (e.g. a table both INSERTed and UPDATEd keeps the last
    # `operation`). Emitting each key once keeps the target duplicate-free, so a streaming
    # writer (graph_sync.stream_apply) that doesn't re-collapse the target still writes each
    # edge exactly once, with accurate counts.
    edges: dict[tuple[str, str], GraphEdge] = {}
    for called in unit.calls:
        target = routine_index.get(called.upper())
        if target is None:
            continue  # unresolved -- built-in, or something in another schema we didn't parse
        to_label, to_id = target
        if to_id == from_id:
            continue  # ignore trivial self-reference (recursion)
        # `resolution` is the shared provenance key on CALLS across ingestors (Python/C/C#);
        # "source" = derived from parsing source text. (READS/WRITES below are Oracle-specific
        # edges, not part of the cross-ingestor common set, so they keep `provenance`.)
        edges[("CALLS", to_id)] = GraphEdge(
            "CALLS", from_label, {"id": from_id}, to_label, {"id": to_id}, {"resolution": "source"}
        )
    for table_name, operation in unit.table_refs:
        table_id = _table_id(proj_id, table_name)
        if operation == "SELECT":
            edges[("READS", table_id)] = GraphEdge(
                "READS", from_label, {"id": from_id}, "Table", {"id": table_id}, {"provenance": "source"}
            )
        else:
            edges[("WRITES", table_id)] = GraphEdge(
                "WRITES",
                from_label,
                {"id": from_id},
                "Table",
                {"id": table_id},
                {"provenance": "source", "operation": operation},
            )
    yield from edges.values()


def _emit_trigger_nodes(triggers, proj_id) -> Iterator[GraphNode | GraphEdge]:
    for trigger in triggers:
        trigger_id, parent_label, parent_id = _trigger_ids(trigger, proj_id)
        yield GraphNode(
            "Trigger", {"id": trigger_id}, {"name": trigger.name, "source": trigger.source, "doc": trigger.doc, "provenance": "source"}
        )
        yield GraphEdge("HAS_TRIGGER", parent_label, {"id": parent_id}, "Trigger", {"id": trigger_id})


def _emit_trigger_edges(triggers, proj_id, routine_index) -> Iterator[GraphEdge]:
    for trigger in triggers:
        trigger_id, _, _ = _trigger_ids(trigger, proj_id)
        yield from _emit_edges_for("Trigger", trigger_id, trigger, proj_id, routine_index)


def _emit_types(types, proj_id) -> Iterator[GraphNode | GraphEdge]:
    for t in types:
        type_id = f"{proj_id}.type.{t.name.lower()}"
        yield GraphNode("Type", {"id": type_id}, {"name": t.name, "kind": t.kind, "provenance": "source"})
        yield GraphEdge("HAS_TYPE", "Project", {"id": proj_id}, "Type", {"id": type_id})


# -- Ids / helpers ------------------------------------------------------------------


def _table_id(proj_id: str, name: str) -> str:
    return f"{proj_id}.table.{name.lower()}"


def _trigger_ids(trigger: RawTrigger, proj_id: str) -> tuple[str, str, str]:
    """A trigger hangs off its table when it names one (matching the DB ingestor's
    (Table)-[:HAS_TRIGGER]->(Trigger)); otherwise off the Project so it's never
    orphaned."""
    if trigger.table:
        table_id = _table_id(proj_id, trigger.table)
        return f"{table_id}.trigger.{trigger.name.lower()}", "Table", table_id
    return f"{proj_id}.trigger.{trigger.name.lower()}", "Project", proj_id


def _routine_label(routine: RawRoutine) -> str:
    return "Function" if routine.kind == "FUNCTION" else "Procedure"


def _assign_ids(parent_id: str, routines: list[RawRoutine]) -> list[tuple[RawRoutine, str]]:
    """Stable id per routine under `parent_id`. A unique (kind, name) gets
    "{parent}.{kind}.{name}"; overloaded names (same kind+name, different signature)
    get a "#n" suffix ordered by signature, so assignment is deterministic across
    syncs regardless of source order."""
    groups: dict[tuple[str, str], list[RawRoutine]] = {}
    for r in routines:
        groups.setdefault((r.kind, r.name), []).append(r)

    assigned: list[tuple[RawRoutine, str]] = []
    for (kind, name), group in groups.items():
        base = f"{parent_id}.{kind.lower()}.{name.lower()}"
        if len(group) == 1:
            assigned.append((group[0], base))
            continue
        for i, r in enumerate(sorted(group, key=_param_sig)):
            assigned.append((r, f"{base}#{i}"))
    return assigned


def _param_sig(routine: RawRoutine) -> tuple[str, ...]:
    return tuple((p.data_type or "").upper() for p in routine.parameters)


# -- Streaming resolution (memory-bounded: source stays on disk, loaded per entity) ---
#
# resolve() above holds every parsed file (all source text) in memory at once. For a
# large first ingest that dominates peak RSS. The two functions below split the work so
# the whole corpus is never resident: build_plan() scans each cached file once and keeps
# only a lightweight global index (routine name->id, stub table names, and which blob
# sha carries each entity's payload), dropping the source; emit_streaming() then emits
# the SAME graph as resolve() but (re)loads only the one-or-few files each entity needs.
# Peak memory is O(index + one entity's files), independent of corpus size. Reuses the
# emit/merge/id-assignment helpers above, so output is identical (verified by parity
# tests). Driven by git_sync, which supplies load_fn over the blob-sha parse cache.


class StreamPlan:
    """The lightweight global index build_plan() produces and emit_streaming() consumes.
    Holds ids, names, and blob-sha groupings only -- never any source text."""

    def __init__(self) -> None:
        self.routine_index: dict[str, tuple[str, str]] = {}   # NAME(upper) -> (label, id)
        self.stub_names: set[str] = set()
        self.package_shas: dict[str, list[str]] = defaultdict(list)      # name -> [blob_sha]
        self.table_shas: dict[str, list[str]] = defaultdict(list)        # NAME(upper) -> [blob_sha]
        self.standalone_groups: dict[tuple[str, str], list[str]] = defaultdict(list)  # (kind,name)->shas
        self.trigger_shas: list[str] = []                               # blob_shas carrying >=1 trigger
        self.view_winner: dict[str, str] = {}                           # NAME(upper) -> blob_sha (first wins)
        self.seq_objs: list[RawSequence] = []                           # deduped (no source)
        self.type_objs: list[RawType] = []                              # deduped (no source)
        self.index_objs: list[RawIndexDef] = []                         # all (no source)


def _light_routine(r: RawRoutine) -> RawRoutine:
    # keep name/kind/params (params carry the overload signature); drop source/calls/refs
    return RawRoutine(name=r.name, kind=r.kind, parameters=r.parameters, return_type=r.return_type)


def build_plan(
    file_refs: Iterable[tuple[str, str]], load_fn: Callable[[str], RawPlsqlFile], proj_id: str
) -> StreamPlan:
    """Phase 1: scan each cached file once into a StreamPlan, dropping source as we go.
    `file_refs` is (path, blob_sha) pairs; `load_fn(blob_sha)` returns a FRESH RawPlsqlFile
    each call -- freshness matters because _apply_alterations mutates in place (see
    _merged_full_table). Ids are assigned here (not during emission) because a name's
    overloads, and the routine_index used to resolve CALLS, are global and must exist
    before emit_streaming runs -- and because stream_apply re-runs emission, so nothing
    built during emission would survive to the next pass."""
    plan = StreamPlan()
    merged_light: dict[str, _MergedPackage] = {}
    standalone_light: list[RawRoutine] = []
    ddl_names: set[str] = set()
    referenced: set[str] = set()
    seen_seq: dict[str, RawSequence] = {}
    seen_type: dict[str, RawType] = {}

    for _path, sha in file_refs:
        f = load_fn(sha)
        for pkg in f.packages:
            merged_light.setdefault(pkg.name, _MergedPackage(pkg.name)).absorb(
                RawPackage(pkg.name, members=[_light_routine(m) for m in pkg.members])
            )
            plan.package_shas[pkg.name].append(sha)
            for m in pkg.members:
                referenced.update(n for n, _ in m.table_refs)
        for r in f.routines:
            standalone_light.append(_light_routine(r))
            plan.standalone_groups[(r.kind, r.name)].append(sha)
            referenced.update(n for n, _ in r.table_refs)
        for t in f.tables:
            ddl_names.add(t.name.upper())
            plan.table_shas[t.name.upper()].append(sha)
        for a in f.alterations:
            ddl_names.add(a.table.upper())
            plan.table_shas[a.table.upper()].append(sha)
        if f.triggers:
            plan.trigger_shas.append(sha)
            for tr in f.triggers:
                referenced.update(n for n, _ in tr.table_refs)
                if tr.table:
                    referenced.add(tr.table)
        for v in f.views:
            plan.view_winner.setdefault(v.name.upper(), sha)
            referenced.update(v.reads)
        for s in f.sequences:
            seen_seq.setdefault(s.name.upper(), s)
        for ty in f.types:
            seen_type.setdefault(ty.name.upper(), ty)
        for ix in f.indexes:
            plan.index_objs.append(ix)
            referenced.add(ix.table)
        del f  # drop the heavy payload before loading the next file

    plan.seq_objs = list(seen_seq.values())
    plan.type_objs = list(seen_type.values())
    plan.stub_names = {n for n in referenced if n.upper() not in ddl_names}

    # Assign ids + build routine_index first-wins, mirroring resolve()'s emission order
    # (packages, then standalones). Ids depend only on (kind,name,sig), so the light
    # members give exactly the ids the full members will in emit_streaming.
    for name, mpkg in merged_light.items():
        package_id = f"{proj_id}.package.{name.lower()}"
        for routine, rid in _assign_ids(package_id, mpkg.members):
            plan.routine_index.setdefault(routine.name.upper(), (_routine_label(routine), rid))
    for routine, rid in _assign_ids(proj_id, _dedupe(standalone_light)):
        plan.routine_index.setdefault(routine.name.upper(), (_routine_label(routine), rid))
    return plan


def _merged_full_package(name: str, shas: list[str], load_fn) -> _MergedPackage:
    merged = _MergedPackage(name)
    for sha in dict.fromkeys(shas):  # unique blob shas, order preserved
        for pkg in load_fn(sha).packages:
            if pkg.name == name:
                merged.absorb(pkg)
    return merged


def _merged_full_table(upper: str, shas: list[str], load_fn) -> list[RawTable]:
    tables: list[RawTable] = []
    alters = []
    for sha in dict.fromkeys(shas):
        f = load_fn(sha)
        tables.extend(t for t in f.tables if t.name.upper() == upper)
        alters.extend(a for a in f.alterations if a.table.upper() == upper)
    merged = _dedupe_tables(tables)
    _apply_alterations(merged, alters)  # mutates `merged` -- fine, these are fresh loads
    return [t for t in merged if t.name.upper() == upper]


def _load_standalone_group(kind: str, name: str, shas: list[str], load_fn) -> list[RawRoutine]:
    routines: list[RawRoutine] = []
    for sha in dict.fromkeys(shas):
        routines.extend(r for r in load_fn(sha).routines if r.kind == kind and r.name == name)
    return _dedupe(routines)


def emit_streaming(
    plan: StreamPlan, load_fn: Callable[[str], RawPlsqlFile], proj_id: str
) -> Iterator[GraphNode | GraphEdge]:
    """Phase 2: yield the same nodes/edges as resolve(), loading only the file(s) each
    entity needs from `load_fn` and dropping them after. Safe to iterate repeatedly (each
    call reloads; nothing retained between) -- stream_apply iterates it twice.

    One loop per entity: each entity's file(s) are loaded ONCE and its nodes AND all its
    edges (HAS_*, FK, CALLS/READS/WRITES, trigger edges, view reads) are emitted together.
    We deliberately do NOT split this into a node pass then a cross-edge pass the way
    resolve() does -- that split would reload every entity a second time here, whereas
    resolve() pays nothing because its files are already in memory. Ordering is safe
    regardless: stream_apply writes all nodes (its first pass over this generator) before
    any edge (its second pass), so a cross-entity edge -- an FK or CALLS whose endpoint
    lives in another entity -- is still written only once every node exists. Emitting each
    entity from a single load roughly halves the parse-cache reloads a full sync does."""
    ri = plan.routine_index
    for upper, shas in plan.table_shas.items():
        merged = _merged_full_table(upper, shas, load_fn)
        yield from _emit_tables(merged, proj_id)
        yield from _emit_constraints(merged, proj_id)
        yield from _emit_foreign_keys(merged, proj_id)
    yield from _emit_table_stubs(plan.stub_names, proj_id)
    for upper, sha in plan.view_winner.items():
        views = [v for v in load_fn(sha).views if v.name.upper() == upper]
        yield from _emit_views(views, proj_id)
        yield from _emit_view_reads(views, proj_id)
    yield from _emit_sequences(plan.seq_objs, proj_id)
    for name, shas in plan.package_shas.items():
        merged = _merged_full_package(name, shas, load_fn)
        yield from _emit_packages([merged], proj_id, ri)
        yield from _emit_calls_reads_writes([merged], [], proj_id, ri)
    for (kind, name), shas in plan.standalone_groups.items():
        group = _load_standalone_group(kind, name, shas, load_fn)
        yield from _emit_standalones(group, proj_id, ri)
        yield from _emit_calls_reads_writes([], group, proj_id, ri)
    for sha in plan.trigger_shas:
        triggers = load_fn(sha).triggers
        yield from _emit_trigger_nodes(triggers, proj_id)
        yield from _emit_trigger_edges(triggers, proj_id, ri)
    yield from _emit_types(plan.type_objs, proj_id)
    yield from _emit_indexes(plan.index_objs, proj_id)
