"""Extracts Package/Procedure/Function/Parameter/Trigger/Type structure (plus
dependency edges) from Oracle PL/SQL, via raw catalog queries (catalog.py) and
best-effort AST parsing (source_analysis.py) for finer-grained call/table-usage
detail than Oracle's own ALL_DEPENDENCIES provides.

Cross-ingestor id matching: SQLAlchemy's Oracle dialect lowercases ordinary
(unquoted) identifiers when reflecting them -- confirmed against a live instance --
so the SQL ingestor's Table/Schema ids use lowercase names even though Oracle's own
catalog views (ALL_TRIGGERS.TABLE_NAME, etc.) report everything in uppercase. Every
id built here from raw catalog data is lowercased to match.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterator

import click

from auryn.core.base_ingestor import BaseIngestor
from auryn.core.graph_client import GraphWriter, connect
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.registry import register_ingestor
from auryn.ingestors.plsql import catalog
from auryn.ingestors.plsql.config import PLSQLSourceConfig
from auryn.ingestors.plsql.schema import EDGE_SCHEMAS, NODE_SCHEMAS

logger = logging.getLogger(__name__)


def _sa():
    """Lazily import source_analysis (pulls the generated ANTLR grammar, ~160ms) so importing this
    module at CLI startup -- discover_ingestors does, to register the `run oracle` subcommand --
    stays light. Cached after first import, so repeated calls are free."""
    import auryn.ingestors.plsql.source_analysis as sa

    return sa

# Referenced-object types ALL_DEPENDENCIES can point at that we know how to turn into
# a node id. Others (SYNONYM, JAVA CLASS, ...) are skipped rather than guessed at.
_SQL_INGESTED_TYPES = {"TABLE", "VIEW", "SEQUENCE"}
_OWN_ROUTINE_TYPES = {"PROCEDURE", "FUNCTION"}
_OWN_PACKAGE_TYPES = {"PACKAGE", "PACKAGE BODY"}
_OWN_TYPE_TYPES = {"TYPE", "TYPE BODY"}


class _Routine:
    __slots__ = ("owner", "package_name", "name", "kind", "overload", "parameters", "return_type")

    def __init__(self, owner, package_name, name, kind, overload, parameters, return_type):
        self.owner = owner
        self.package_name = package_name  # None for standalone
        self.name = name
        self.kind = kind  # "PROCEDURE" | "FUNCTION"
        self.overload = overload  # None, or an overload number as Oracle reports it
        self.parameters = parameters  # position-1+ argument rows, sorted
        self.return_type = return_type  # data_type string, FUNCTION only


def _build_routines(routine_rows: list[dict], argument_rows: list[dict]) -> list[_Routine]:
    """Combines ALL_PROCEDURES + ALL_ARGUMENTS. ALL_PROCEDURES alone can't tell a
    packaged procedure from a packaged function (both show OBJECT_TYPE='PACKAGE') --
    a POSITION=0 argument row (the return-value slot, present even for zero-argument
    functions) is what actually distinguishes them, and also carries the return type."""
    args_by_routine: dict[tuple, list[dict]] = defaultdict(list)
    return_types: dict[tuple, str] = {}
    for arg in argument_rows:
        key = (arg["owner"], arg["package_name"], arg["object_name"], arg["overload"])
        if arg["position"] == 0:
            return_types[key] = arg["data_type"]
        else:
            args_by_routine[key].append(arg)

    routines = []
    for row in routine_rows:
        if row["object_type"] in _OWN_ROUTINE_TYPES:
            package_name, name = None, row["object_name"]
        elif row["object_type"] == "PACKAGE" and row["procedure_name"] is not None:
            package_name, name = row["object_name"], row["procedure_name"]
        else:
            continue  # the package-level placeholder row (procedure_name IS NULL)

        key = (row["owner"], package_name, name, row["overload"])
        kind = "FUNCTION" if row["object_type"] == "FUNCTION" or key in return_types else "PROCEDURE"
        params = sorted(args_by_routine.get(key, []), key=lambda a: a["position"])
        routines.append(
            _Routine(row["owner"], package_name, name, kind, row["overload"], params, return_types.get(key))
        )
    return routines


def _routine_id(parent_id: str, kind: str, name: str, overload) -> str:
    routine_id = f"{parent_id}.{kind.lower()}.{name.lower()}"
    if overload is not None:
        routine_id += f"#{overload}"
    return routine_id


@register_ingestor("oracle")
class PLSQLIngestor(BaseIngestor):
    """The Oracle DB ingestor: reads PL/SQL structure from a *live Oracle
    connection* (catalog ALL_* views). Registered as `oracle` (`auryn run
    oracle`) to distinguish it from the file-based `plsql` ingestor, which parses
    the same language from version-controlled source files instead of a database."""

    node_schemas = NODE_SCHEMAS
    edge_schemas = EDGE_SCHEMAS

    def __init__(self, config: PLSQLSourceConfig) -> None:
        self._config = config

    @classmethod
    def build_cli_command(cls) -> click.Command:
        @click.command(
            "oracle",
            help="Ingest Oracle PL/SQL from a live DB connection (packages, procedures, functions, triggers, types).",
        )
        @click.option(
            "--dsn", required=True, help="python-oracledb connect string, e.g. user/password@host:port/service_name."
        )
        @click.option(
            "--database-name",
            required=True,
            help="Logical name for this source -- MUST match the SQL ingestor's --database-name for this same database.",
        )
        @click.option(
            "--schema",
            "schemas",
            multiple=True,
            help="Restrict ingestion to this schema/owner (repeatable). Default: the connected user's own schema.",
        )
        @click.pass_context
        def command(ctx: click.Context, dsn: str, database_name: str, schemas: tuple[str, ...]) -> None:
            writer = GraphWriter(connect(ctx.obj["falkordb_config"]))
            ingestor = cls(PLSQLSourceConfig(dsn=dsn, database_name=database_name, schemas=schemas or None))
            ingestor._warn_if_database_missing(writer)
            stats = ingestor.run(writer)
            click.echo(
                f"Merged {stats.nodes_merged} nodes, {stats.edges_merged} edges "
                f"in {stats.elapsed_seconds:.2f}s"
            )

        return command

    def _warn_if_database_missing(self, writer: GraphWriter) -> None:
        database_id = f"oracle://{self._config.database_name}"
        if not writer.node_exists("Database", database_id):
            click.echo(
                f"Warning: no Database node found for id '{database_id}'. "
                "CALLS/READS/WRITES/DEPENDS_ON/HAS_TRIGGER edges to SQL-ingested "
                "tables/views/sequences "
                "won't link until the SQL ingestor has been run against this same "
                "database with a matching --database-name.",
                err=True,
            )

    def extract(self) -> Iterator[GraphNode | GraphEdge]:
        conn = catalog.connect(self._config.dsn)
        try:
            schemas = self._resolve_schemas(conn)
            database_id = f"oracle://{self._config.database_name}"

            statuses = catalog.object_status(conn, schemas)
            routines = _build_routines(catalog.routines(conn, schemas), catalog.arguments(conn, schemas))
            dependency_rows = catalog.dependencies(conn, schemas)
            trigger_rows = catalog.triggers(conn, schemas)
            type_rows = catalog.types(conn, schemas)

            # Pass 1: Package/Procedure/Function/Parameter/Type nodes -- everything
            # except HAS_TRIGGER/CALLS/READS/WRITES/DEPENDS_ON edges, so every node
            # this ingestor owns exists before pass 2 tries to link to one (same
            # two-pass reasoning as the SQL ingestor's FK-ordering fix).
            for owner in schemas:
                schema_id = f"{database_id}.{owner.lower()}"
                yield from self._extract_routines(conn, owner, schema_id, routines, statuses)
                yield from self._extract_types(owner, schema_id, type_rows, statuses)

            # Pass 2: edges onto nodes guaranteed to exist by now (this ingestor's own
            # Package/Procedure/Function, or the SQL ingestor's pre-existing Table).
            routine_by_key = {(r.owner, r.package_name, r.name): r for r in routines}
            for row in trigger_rows:
                yield from self._extract_trigger(database_id, row)
            yield from self._extract_catalog_dependencies(database_id, dependency_rows, routine_by_key)
            yield from self._extract_parsed_dependencies(conn, database_id, routines, trigger_rows, routine_by_key)
        finally:
            conn.close()

    def _resolve_schemas(self, conn) -> list[str]:
        if self._config.schemas is not None:
            return list(self._config.schemas)
        return [catalog.current_schema(conn)]

    # -- Packages / Procedures / Functions / Parameters --------------------------

    def _extract_routines(
        self, conn, owner: str, schema_id: str, routines: list[_Routine], statuses: dict
    ) -> Iterator[GraphNode | GraphEdge]:
        # package_name -> {(member_name, param_types): member_source}, populated once
        # per package (from the same body_source used for the Package node itself)
        # and handed to each of that package's routines to slice their own source out.
        package_member_sources: dict[str, dict] = {}
        # package_name -> was the PACKAGE BODY `wrap`-obfuscated? Members of a wrapped
        # body have no readable source of their own, so they're flagged wrapped too.
        package_body_wrapped: dict[str, bool] = {}
        for routine in routines:
            if routine.owner != owner:
                continue
            if routine.package_name is not None and routine.package_name not in package_member_sources:
                body_source = catalog.source_text(conn, owner, routine.package_name, "PACKAGE BODY")
                body_wrapped = catalog.is_wrapped_source(body_source)
                package_body_wrapped[routine.package_name] = body_wrapped
                # A wrapped body is bytecode, not parseable source -- don't even attempt
                # to slice per-member source out of it (the parse would just fail).
                package_member_sources[routine.package_name] = (
                    _sa().extract_package_member_sources(body_source) if body_source and not body_wrapped else {}
                )
                yield from self._extract_package(
                    conn, owner, schema_id, routine.package_name, statuses, body_source, body_wrapped
                )

            parent_id = (
                f"{schema_id}.package.{routine.package_name.lower()}" if routine.package_name else schema_id
            )
            parent_label = "Package" if routine.package_name else "Schema"
            member_sources = package_member_sources.get(routine.package_name, {})
            member_wrapped = package_body_wrapped.get(routine.package_name, False)
            yield from self._extract_routine(
                conn, owner, parent_label, parent_id, routine, statuses, member_sources, member_wrapped
            )

    def _extract_package(
        self,
        conn,
        owner: str,
        schema_id: str,
        package_name: str,
        statuses: dict,
        body_source: str | None,
        body_wrapped: bool,
    ) -> Iterator[GraphNode | GraphEdge]:
        package_id = f"{schema_id}.package.{package_name.lower()}"
        spec_source = catalog.source_text(conn, owner, package_name, "PACKAGE")
        spec_wrapped = catalog.is_wrapped_source(spec_source)
        yield GraphNode(
            label="Package",
            key={"id": package_id},
            properties={
                "name": package_name,
                # `wrap`-obfuscated source is bytecode, not readable PL/SQL: store the
                # flag, not the gibberish, so a consumer can tell an obfuscated unit apart
                # from one with genuinely no source. Spec and body are wrapped independently
                # (a common shape is a readable spec over a wrapped body).
                "spec_source": None if spec_wrapped else spec_source,
                "spec_wrapped": spec_wrapped,
                "body_source": None if body_wrapped else body_source,
                "body_wrapped": body_wrapped,
                "spec_valid": statuses.get((owner, package_name, "PACKAGE")) == "VALID",
                "body_valid": statuses.get((owner, package_name, "PACKAGE BODY")) == "VALID",
            },
        )
        yield GraphEdge(
            rel_type="HAS_PACKAGE",
            from_label="Schema",
            from_key={"id": schema_id},
            to_label="Package",
            to_key={"id": package_id},
        )

    def _extract_routine(
        self,
        conn,
        owner: str,
        parent_label: str,
        parent_id: str,
        routine: _Routine,
        statuses: dict,
        member_sources: dict | None = None,
        member_wrapped: bool = False,
    ) -> Iterator[GraphNode | GraphEdge]:
        routine_id = _routine_id(parent_id, routine.kind, routine.name, routine.overload)
        label = "Function" if routine.kind == "FUNCTION" else "Procedure"

        if routine.package_name is not None:
            valid = statuses.get((owner, routine.package_name, "PACKAGE BODY")) == "VALID"
            # Matched by (name, parameter types) rather than name alone, so an
            # overloaded member (e.g. two routines both named LOG_CHANGE) gets its
            # own correct source rather than either one's or neither's.
            param_types = tuple((arg["data_type"] or "").upper() for arg in routine.parameters)
            # A member of a `wrap`-obfuscated package body has no readable source of its
            # own (member_sources is empty for a wrapped body) -- flag it, source stays null.
            wrapped = member_wrapped
            source = (member_sources or {}).get((routine.name, param_types))
        else:
            valid = statuses.get((owner, routine.name, routine.kind)) == "VALID"
            standalone_source = catalog.source_text(conn, owner, routine.name, routine.kind)
            wrapped = catalog.is_wrapped_source(standalone_source)
            source = None if wrapped else standalone_source

        properties = {"name": routine.name, "valid": valid, "source": source, "wrapped": wrapped}
        if label == "Function":
            properties["return_type"] = routine.return_type

        yield GraphNode(label=label, key={"id": routine_id}, properties=properties)
        yield GraphEdge(
            rel_type=f"HAS_{label.upper()}",
            from_label=parent_label,
            from_key={"id": parent_id},
            to_label=label,
            to_key={"id": routine_id},
        )

        for arg in routine.parameters:
            if arg["position"] == 0:
                continue  # the return-value slot, not a real parameter
            param_name = (arg["argument_name"] or f"pos{arg['position']}").lower()
            param_id = f"{routine_id}.param.{param_name}"
            yield GraphNode(
                label="Parameter",
                key={"id": param_id},
                properties={
                    "name": arg["argument_name"],
                    "position": arg["position"],
                    "direction": arg["in_out"],
                    "type": arg["data_type"],  # shared Parameter `type` property (Python/C/C# all use `type`)
                    "has_default": arg["defaulted"] == "Y",
                },
            )
            yield GraphEdge(
                rel_type="HAS_PARAMETER",
                from_label=label,
                from_key={"id": routine_id},
                to_label="Parameter",
                to_key={"id": param_id},
                properties={"position": arg["position"]},
            )

    # -- Types --------------------------------------------------------------------

    def _extract_types(
        self, owner: str, schema_id: str, type_rows: list[dict], statuses: dict
    ) -> Iterator[GraphNode | GraphEdge]:
        for row in type_rows:
            if row["owner"] != owner:
                continue
            type_id = f"{schema_id}.type.{row['type_name'].lower()}"
            yield GraphNode(
                label="Type",
                key={"id": type_id},
                properties={
                    "name": row["type_name"],
                    "kind": row["typecode"],
                    "valid": statuses.get((owner, row["type_name"], "TYPE")) == "VALID",
                },
            )
            yield GraphEdge(
                rel_type="HAS_TYPE",
                from_label="Schema",
                from_key={"id": schema_id},
                to_label="Type",
                to_key={"id": type_id},
            )

    # -- Triggers -------------------------------------------------------------------

    def _extract_trigger(self, database_id: str, row: dict) -> Iterator[GraphNode | GraphEdge]:
        schema_id = f"{database_id}.{row['table_owner'].lower()}"
        table_id = f"{schema_id}.{row['table_name'].lower()}"
        trigger_id = f"{table_id}.trigger.{row['trigger_name'].lower()}"
        wrapped = catalog.is_wrapped_source(row["trigger_body"])
        yield GraphNode(
            label="Trigger",
            key={"id": trigger_id},
            properties={
                "name": row["trigger_name"],
                "timing": row["trigger_type"],
                "events": row["triggering_event"],
                "valid": row["status"] == "ENABLED",
                "source": None if wrapped else row["trigger_body"],
                "wrapped": wrapped,
            },
        )
        # If the SQL ingestor hasn't run yet for this Table, this MATCH finds nothing
        # and merge_edge logs its own warning -- see _warn_if_database_missing above
        # for why, and core/graph_client.py's merge_edge for the underlying mechanism.
        yield GraphEdge(
            rel_type="HAS_TRIGGER",
            from_label="Table",
            from_key={"id": table_id},
            to_label="Trigger",
            to_key={"id": trigger_id},
        )

    # -- Dependencies (catalog-derived, coarse but always available) --------------

    def _extract_catalog_dependencies(
        self, database_id: str, dependency_rows: list[dict], routine_by_key: dict
    ) -> Iterator[GraphEdge]:
        for row in dependency_rows:
            from_ref = self._resolve_dependent_ref(database_id, row["owner"], row["name"], row["type"])
            to_ref = self._resolve_referenced_ref(
                database_id, row["referenced_owner"], row["referenced_name"], row["referenced_type"]
            )
            if from_ref is None or to_ref is None:
                continue
            from_label, from_id = from_ref
            to_label, to_id = to_ref
            if from_label == to_label and from_id == to_id:
                continue  # e.g. a package body's self-reference to its own spec
            yield GraphEdge(
                rel_type="DEPENDS_ON",
                from_label=from_label,
                from_key={"id": from_id},
                to_label=to_label,
                to_key={"id": to_id},
                properties={"detected_via": "catalog"},
            )

    def _resolve_dependent_ref(self, database_id: str, owner: str, name: str, type_: str) -> tuple[str, str] | None:
        schema_id = f"{database_id}.{owner.lower()}"
        if type_ in _OWN_PACKAGE_TYPES:
            return "Package", f"{schema_id}.package.{name.lower()}"
        if type_ in _OWN_ROUTINE_TYPES:
            # Standalone only -- packaged routines' dependencies are always attributed
            # to the enclosing PACKAGE BODY by Oracle, never to the individual member.
            return type_.capitalize(), f"{schema_id}.{type_.lower()}.{name.lower()}"
        if type_ == "TRIGGER":
            return None  # ALL_DEPENDENCIES doesn't give us the trigger's table here;
            # handled instead by parsing trigger_body in _extract_parsed_dependencies.
        if type_ in _OWN_TYPE_TYPES:
            return "Type", f"{schema_id}.type.{name.lower()}"
        return None

    def _resolve_referenced_ref(
        self, database_id: str, owner: str, name: str, type_: str
    ) -> tuple[str, str] | None:
        schema_id = f"{database_id}.{owner.lower()}"
        if type_ in _SQL_INGESTED_TYPES:
            if type_ == "SEQUENCE":
                return "Sequence", f"{schema_id}.sequence.{name.lower()}"
            return type_.capitalize(), f"{schema_id}.{name.lower()}"
        if type_ in _OWN_PACKAGE_TYPES:
            return "Package", f"{schema_id}.package.{name.lower()}"
        if type_ in _OWN_ROUTINE_TYPES:
            return type_.capitalize(), f"{schema_id}.{type_.lower()}.{name.lower()}"
        if type_ in _OWN_TYPE_TYPES:
            return "Type", f"{schema_id}.type.{name.lower()}"
        return None  # SYNONYM, JAVA CLASS, and other types we don't model

    # -- Dependencies (parsed, fine-grained where parsing succeeds) ----------------

    def _extract_parsed_dependencies(
        self,
        conn,
        database_id: str,
        routines: list[_Routine],
        trigger_rows: list[dict],
        routine_by_key: dict,
    ) -> Iterator[GraphEdge]:
        for routine in routines:
            if routine.package_name is not None:
                continue  # source lives on the Package node; parse it once below instead
            schema_id = f"{database_id}.{routine.owner.lower()}"
            routine_id = _routine_id(schema_id, routine.kind, routine.name, routine.overload)
            source = catalog.source_text(conn, routine.owner, routine.name, routine.kind)
            if source is None or catalog.is_wrapped_source(source):
                continue  # wrapped bodies are bytecode -- the catalog DEPENDS_ON edge covers them
            from_label = "Function" if routine.kind == "FUNCTION" else "Procedure"
            yield from self._parsed_edges_for(
                database_id, routine.owner, from_label, routine_id, source, routine_by_key
            )

        packages_done: set[tuple[str, str]] = set()
        for routine in routines:
            if routine.package_name is None or (routine.owner, routine.package_name) in packages_done:
                continue
            packages_done.add((routine.owner, routine.package_name))
            schema_id = f"{database_id}.{routine.owner.lower()}"
            package_id = f"{schema_id}.package.{routine.package_name.lower()}"
            source = catalog.source_text(conn, routine.owner, routine.package_name, "PACKAGE BODY")
            if source is None or catalog.is_wrapped_source(source):
                continue  # wrapped bodies are bytecode -- the catalog DEPENDS_ON edge covers them
            yield from self._parsed_edges_for(
                database_id, routine.owner, "Package", package_id, source, routine_by_key
            )

        for row in trigger_rows:
            if not row["trigger_body"] or catalog.is_wrapped_source(row["trigger_body"]):
                continue  # wrapped/empty trigger body -- nothing parseable
            schema_id = f"{database_id}.{row['table_owner'].lower()}"
            table_id = f"{schema_id}.{row['table_name'].lower()}"
            trigger_id = f"{table_id}.trigger.{row['trigger_name'].lower()}"
            # "block" (optional DECLARE + BEGIN...END), not "body" (BEGIN...END only)
            # -- ALL_TRIGGERS.TRIGGER_BODY commonly includes a DECLARE section for
            # local variables, which the bare "body" rule rejects outright.
            yield from self._parsed_edges_for(
                database_id, row["owner"], "Trigger", trigger_id, row["trigger_body"], routine_by_key, rule="block"
            )

    def _parsed_edges_for(
        self,
        database_id: str,
        owner: str,
        from_label: str,
        from_id: str,
        source: str,
        routine_by_key: dict,
        rule: str = "sql_script",
    ) -> Iterator[GraphEdge]:
        parsed = _sa().parse_unit(source, rule=rule)
        schema_id = f"{database_id}.{owner.lower()}"

        for called_name in parsed.calls:
            target = self._resolve_call(database_id, owner, called_name, routine_by_key)
            if target is None:
                continue
            to_label, to_id = target
            yield GraphEdge(
                rel_type="CALLS",
                from_label=from_label,
                from_key={"id": from_id},
                to_label=to_label,
                to_key={"id": to_id},
                # `resolution` is the shared provenance key on CALLS across ingestors; "parsed"
                # = detected by parsing the routine body. (READS/WRITES/DEPENDS_ON below keep
                # `detected_via` -- Oracle-specific edges, not part of the common set.)
                properties={"resolution": "parsed"},
            )

        for table_ref in parsed.table_refs:
            to_id = f"{schema_id}.{table_ref.name.lower()}"
            if table_ref.operation == "SELECT":
                yield GraphEdge(
                    rel_type="READS",
                    from_label=from_label,
                    from_key={"id": from_id},
                    to_label="Table",
                    to_key={"id": to_id},
                    properties={"detected_via": "parsed"},
                )
            else:
                yield GraphEdge(
                    rel_type="WRITES",
                    from_label=from_label,
                    from_key={"id": from_id},
                    to_label="Table",
                    to_key={"id": to_id},
                    properties={"detected_via": "parsed", "operation": table_ref.operation},
                )

    @staticmethod
    def _resolve_call(
        database_id: str, owner: str, called_name: str, routine_by_key: dict
    ) -> tuple[str, str] | None:
        """Best-effort: only resolves calls to routines we already know about in the
        same schema (standalone, or a same-name packaged member -- ambiguous package
        qualification in the call text itself isn't resolved, and if the same name is
        overloaded, an arbitrary matching overload is picked since resolving the exact
        one would need argument-type matching, well beyond "best effort" here).
        Unmatched calls are skipped; they're still covered by catalog-derived edges."""
        name = called_name.upper()
        schema_id = f"{database_id}.{owner.lower()}"
        for (routine_owner, package_name, routine_name), routine in routine_by_key.items():
            if routine_owner != owner or routine_name != name:
                continue
            label = "Function" if routine.kind == "FUNCTION" else "Procedure"
            parent_id = f"{schema_id}.package.{package_name.lower()}" if package_name else schema_id
            return label, _routine_id(parent_id, routine.kind, routine.name, routine.overload)
        return None
