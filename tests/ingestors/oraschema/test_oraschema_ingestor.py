"""Tests for the file-based Oracle schema-catalog ingestor (`oraschema`).

Most are hermetic -- they drive `extract()` and assert on the emitted node/edge stream,
so no FalkorDB (and certainly no Oracle) is needed. One integration test runs the whole
ingestor against the real `graph` fixture, matching the sql/plsql ingestor tests, and
checks idempotency.

The export layout under test mirrors a Redgate "Schema Compare for Oracle" scripts
folder: UTF-16 DatabaseInformation.xml / SchemaInformation.xml metadata beside UTF-8
per-object `.sql` DDL.
"""

from __future__ import annotations

import pytest

from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.oraschema.config import OraSchemaConfig
from auryn.ingestors.oraschema.ingestor import OraSchemaIngestor
from auryn.ingestors.plsql.diagnostics import StrictHaltError

_PRODUCTS = """CREATE TABLE myschema.products (
  product_id NUMBER(10) NOT NULL,
  name VARCHAR2(80),
  CONSTRAINT pk_products PRIMARY KEY (product_id)
);"""

_PRODUCT_PRICES = """CREATE TABLE myschema.product_prices (
  product_id NUMBER(10) NOT NULL,
  schedule_seq NUMBER(9) NOT NULL,
  batch_pos NUMBER(4) NOT NULL,
  CONSTRAINT fk_pp_productid FOREIGN KEY (product_id) REFERENCES myschema.products (product_id)
);"""

_INDEX = "CREATE INDEX myschema.product_prices_index_1 ON myschema.product_prices(product_id);"
_SEQUENCE = "CREATE SEQUENCE myschema.order_seq MAXVALUE 9999999999 NOCACHE;"
_VIEW = (
    "CREATE OR REPLACE FORCE VIEW myschema.product_names (product_id, name) AS\n"
    "  SELECT product_id, name FROM myschema.products;"
)


def _write_export(root, *, database_version: str = "NineteenC", schema: str = "MYSCHEMA") -> None:
    """Build a Redgate scripts-folder export: UTF-16 metadata + UTF-8 object DDL."""
    (root / "DatabaseInformation.xml").write_text(
        '<?xml version="1.0" encoding="utf-16"?>'
        f"<ScriptsFolderInformation><DatabaseVersion>{database_version}</DatabaseVersion>"
        "</ScriptsFolderInformation>",
        encoding="utf-16",
    )
    schema_dir = root / schema
    for folder in ("Tables", "Indexes", "Sequences", "Views"):
        (schema_dir / folder).mkdir(parents=True, exist_ok=True)
    (schema_dir / "SchemaInformation.xml").write_text(
        '<?xml version="1.0" encoding="utf-16"?>'
        f"<SchemaInformation><Name>{schema}</Name></SchemaInformation>",
        encoding="utf-16",
    )
    (schema_dir / "Tables" / "PRODUCTS.sql").write_text(_PRODUCTS)
    (schema_dir / "Tables" / "PRODUCT_PRICES.sql").write_text(_PRODUCT_PRICES)
    (schema_dir / "Indexes" / "PRODUCT_PRICES_INDEX_1.sql").write_text(_INDEX)
    (schema_dir / "Sequences" / "ORDER_SEQ.sql").write_text(_SEQUENCE)
    (schema_dir / "Views" / "PRODUCT_NAMES.sql").write_text(_VIEW)


def _extract(tmp_path, **kwargs):
    _write_export(tmp_path)
    config = OraSchemaConfig(path=str(tmp_path), database_name="MYDB", **kwargs)
    return list(OraSchemaIngestor(config).extract())


def _nodes(items, label=None):
    return [
        i for i in items if isinstance(i, GraphNode) and (label is None or i.label == label)
    ]


def _edges(items, rel_type=None):
    return [
        i for i in items if isinstance(i, GraphEdge) and (rel_type is None or i.rel_type == rel_type)
    ]


def _ids(nodes):
    return {n.key["id"] for n in nodes}


# -- Metadata / roots ----------------------------------------------------------------


def test_database_root_carries_version_from_utf16_metadata(tmp_path):
    items = _extract(tmp_path)
    (database,) = _nodes(items, "Database")
    assert database.key["id"] == "oracle://MYDB"
    assert database.properties["dialect"] == "oracle"
    assert database.properties["version"] == "19c"  # NineteenC -> 19c


def test_schema_name_read_from_schemainformation_xml(tmp_path):
    items = _extract(tmp_path)
    (schema,) = _nodes(items, "Schema")
    assert schema.key["id"] == "oracle://MYDB.MYSCHEMA"
    assert schema.properties["name"] == "MYSCHEMA"
    assert _edges(items, "HAS_SCHEMA")[0].to_key["id"] == "oracle://MYDB.MYSCHEMA"


# -- Ids unify with the live `sql` ingestor ------------------------------------------


def test_ids_match_sql_ingestor_scheme(tmp_path):
    """The whole point of rooting at oracle:// is that a file-reconstructed schema shares
    node ids with a live reflection -- so assert the exact id shapes the sql ingestor uses."""
    items = _extract(tmp_path)
    table_ids = _ids(_nodes(items, "Table"))
    assert "oracle://MYDB.MYSCHEMA.PRODUCTS" in table_ids
    assert "oracle://MYDB.MYSCHEMA.PRODUCT_PRICES" in table_ids

    column_ids = _ids(_nodes(items, "Column"))
    assert "oracle://MYDB.MYSCHEMA.PRODUCTS.PRODUCT_ID" in column_ids

    assert "oracle://MYDB.MYSCHEMA.PRODUCT_PRICES.index.PRODUCT_PRICES_INDEX_1" in _ids(
        _nodes(items, "Index")
    )
    assert "oracle://MYDB.MYSCHEMA.PRODUCTS.constraint.PK_PRODUCTS" in _ids(_nodes(items, "Constraint"))
    assert "oracle://MYDB.MYSCHEMA.sequence.ORDER_SEQ" in _ids(_nodes(items, "Sequence"))
    assert "oracle://MYDB.MYSCHEMA.PRODUCT_NAMES" in _ids(_nodes(items, "View"))


def test_identifiers_are_uppercased_like_oracle_catalog(tmp_path):
    items = _extract(tmp_path)
    for column in _nodes(items, "Column"):
        assert column.properties["name"] == column.properties["name"].upper()


# -- Structure -----------------------------------------------------------------------


def test_columns_carry_type_nullability_and_primary_key(tmp_path):
    items = _extract(tmp_path)
    by_id = {n.key["id"]: n for n in _nodes(items, "Column")}
    pk = by_id["oracle://MYDB.MYSCHEMA.PRODUCTS.PRODUCT_ID"]
    assert pk.properties["data_type"] == "NUMBER(10)"
    assert pk.properties["nullable"] is False
    assert pk.properties["is_primary_key"] is True
    name = by_id["oracle://MYDB.MYSCHEMA.PRODUCTS.NAME"]
    assert name.properties["nullable"] is True
    assert name.properties["is_primary_key"] is False


def test_foreign_key_resolves_column_to_column_across_files(tmp_path):
    items = _extract(tmp_path)
    (fk,) = _edges(items, "REFERENCES")
    assert fk.from_key["id"] == "oracle://MYDB.MYSCHEMA.PRODUCT_PRICES.PRODUCT_ID"
    assert fk.to_key["id"] == "oracle://MYDB.MYSCHEMA.PRODUCTS.PRODUCT_ID"
    assert fk.properties["constraint_name"] == "FK_PP_PRODUCTID"


def test_index_and_constraint_edges(tmp_path):
    items = _extract(tmp_path)
    (indexes,) = _edges(items, "INDEXES")
    assert indexes.to_key["id"] == "oracle://MYDB.MYSCHEMA.PRODUCT_PRICES.PRODUCT_ID"
    (constrains,) = _edges(items, "CONSTRAINS")
    assert constrains.to_key["id"] == "oracle://MYDB.MYSCHEMA.PRODUCTS.PRODUCT_ID"


def test_view_reads_edge_points_at_the_underlying_table(tmp_path):
    items = _extract(tmp_path)
    (reads,) = _edges(items, "READS")
    assert reads.from_key["id"] == "oracle://MYDB.MYSCHEMA.PRODUCT_NAMES"
    assert reads.to_key["id"] == "oracle://MYDB.MYSCHEMA.PRODUCTS"


def test_nodes_precede_the_edges_that_reference_them(tmp_path):
    """BaseIngestor's batch writer only guarantees nodes-before-edges within a batch, so
    cross-cutting edges (FK/READS) must be emitted after their endpoint nodes."""
    items = _extract(tmp_path)
    node_positions = {
        i.key["id"]: pos for pos, i in enumerate(items) if isinstance(i, GraphNode)
    }
    for pos, item in enumerate(items):
        if isinstance(item, GraphEdge):
            assert node_positions.get(item.from_key["id"], 1 << 30) < pos
            assert node_positions.get(item.to_key["id"], 1 << 30) < pos


# -- Fallbacks / robustness ----------------------------------------------------------


def test_flat_layout_without_per_schema_nesting(tmp_path):
    """An export whose object files sit loose under one root (no <SCHEMA>/ folder) still
    reconstructs, using the folder name as the schema."""
    (tmp_path / "Tables").mkdir()
    (tmp_path / "Tables" / "WIDGETS.sql").write_text(
        "CREATE TABLE app.widgets (id NUMBER(10) NOT NULL, CONSTRAINT pk_w PRIMARY KEY (id));"
    )
    config = OraSchemaConfig(path=str(tmp_path), database_name="FLATDB")
    items = list(OraSchemaIngestor(config).extract())
    (table,) = _nodes(items, "Table")
    assert table.properties["name"] == "WIDGETS"
    assert table.key["id"].startswith("oracle://FLATDB.")


def test_missing_database_information_xml_yields_null_version(tmp_path):
    _write_export(tmp_path)
    (tmp_path / "DatabaseInformation.xml").unlink()
    config = OraSchemaConfig(path=str(tmp_path), database_name="MYDB")
    items = list(OraSchemaIngestor(config).extract())
    (database,) = _nodes(items, "Database")
    assert database.properties["version"] is None


def test_unknown_database_version_passes_through_verbatim(tmp_path):
    _write_export(tmp_path, database_version="ThirtyFiveZ")
    config = OraSchemaConfig(path=str(tmp_path), database_name="MYDB")
    (database,) = _nodes(list(OraSchemaIngestor(config).extract()), "Database")
    assert database.properties["version"] == "ThirtyFiveZ"


# -- Strict mode ---------------------------------------------------------------------


def test_strict_halts_and_emits_nothing_on_bad_ddl(tmp_path):
    _write_export(tmp_path)
    (tmp_path / "MYSCHEMA" / "Tables" / "BROKEN.sql").write_text(
        "CREATE TABLE myschema.broken (a NUMBER(10),,,, GARBAGE !!!"
    )
    config = OraSchemaConfig(path=str(tmp_path), database_name="MYDB", strict=True)
    ingestor = OraSchemaIngestor(config, diagnostics_dir=str(tmp_path / "diag"))

    emitted = []
    with pytest.raises(StrictHaltError) as excinfo:
        for item in ingestor.extract():  # phase-1 parse halts before phase-2 yields anything
            emitted.append(item)

    assert emitted == []  # nothing reached the graph
    assert excinfo.value.report_path is not None
    assert excinfo.value.report_path.exists()


def test_best_effort_skips_the_bad_file_and_keeps_the_rest(tmp_path):
    _write_export(tmp_path)
    (tmp_path / "MYSCHEMA" / "Tables" / "BROKEN.sql").write_text(
        "CREATE TABLE myschema.broken (a NUMBER(10),,,, GARBAGE !!!"
    )
    config = OraSchemaConfig(path=str(tmp_path), database_name="MYDB", strict=False)
    items = list(OraSchemaIngestor(config).extract())
    # The good tables still come through despite the one unparseable file.
    assert "oracle://MYDB.MYSCHEMA.PRODUCTS" in _ids(_nodes(items, "Table"))


# -- Parallel parsing ----------------------------------------------------------------


def test_parallel_path_reconstructs_a_large_schema(tmp_path):
    """Above the inline threshold the ingestor parses across a process pool; the result must be
    identical to the sequential path -- every table present, with its columns."""
    tables_dir = tmp_path / "MYSCHEMA" / "Tables"
    tables_dir.mkdir(parents=True)
    (tmp_path / "MYSCHEMA" / "SchemaInformation.xml").write_text(
        '<?xml version="1.0" encoding="utf-16"?>'
        "<SchemaInformation><Name>MYSCHEMA</Name></SchemaInformation>",
        encoding="utf-16",
    )
    count = 80  # > _PARALLEL_THRESHOLD (64), so the process pool is used
    for i in range(count):
        (tables_dir / f"T_{i:03}.sql").write_text(
            f"CREATE TABLE myschema.t_{i:03} (id NUMBER(10) NOT NULL, "
            f"CONSTRAINT pk_t_{i:03} PRIMARY KEY (id));"
        )
    config = OraSchemaConfig(path=str(tmp_path), database_name="MYDB")
    items = list(OraSchemaIngestor(config).extract())

    table_ids = _ids(_nodes(items, "Table"))
    assert len(table_ids) == count
    assert "oracle://MYDB.MYSCHEMA.T_000" in table_ids
    assert "oracle://MYDB.MYSCHEMA.T_079" in table_ids
    # every table kept its column through the pool round-trip
    assert len(_nodes(items, "Column")) == count


def test_single_worker_forced_matches_default(tmp_path):
    """Explicit --workers 1 (inline) and the default parallel path agree node-for-node."""
    _write_export(tmp_path)
    default = list(OraSchemaIngestor(OraSchemaConfig(path=str(tmp_path), database_name="MYDB")).extract())
    one = list(
        OraSchemaIngestor(
            OraSchemaConfig(path=str(tmp_path), database_name="MYDB", workers=1)
        ).extract()
    )
    assert _ids(_nodes(default, "Table")) == _ids(_nodes(one, "Table"))
    assert _ids(_nodes(default, "Column")) == _ids(_nodes(one, "Column"))


# -- sqlglot fast-path + ANTLR fallback (hybrid) -------------------------------------


def test_storage_clause_table_falls_back_to_antlr(tmp_path):
    """sqlglot ParseErrors on Oracle physical-storage DDL (USING INDEX TABLESPACE / SEGMENT
    CREATION); the file must fall back to the ANTLR parser so the table still lands."""
    _write_export(tmp_path)
    (tmp_path / "MYSCHEMA" / "Tables" / "STORAGE_TBL.sql").write_text(
        "CREATE TABLE myschema.storage_tbl (id NUMBER(10) NOT NULL, "
        "CONSTRAINT pk_st PRIMARY KEY (id) USING INDEX TABLESPACE ts_idx) "
        "SEGMENT CREATION IMMEDIATE PCTFREE 10 TABLESPACE ts_data;"
    )
    items = list(
        OraSchemaIngestor(OraSchemaConfig(path=str(tmp_path), database_name="MYDB")).extract()
    )
    assert "oracle://MYDB.MYSCHEMA.STORAGE_TBL" in _ids(_nodes(items, "Table"))
    assert "oracle://MYDB.MYSCHEMA.STORAGE_TBL.ID" in _ids(_nodes(items, "Column"))


def test_sqlglot_parses_19c_constructs_the_antlr_grammar_rejects(tmp_path):
    """The whole point of adding sqlglot: it parses modern Oracle constructs the vendored ANTLR
    grammar rejects (INCREMENT BY -1, LISTAGG ... ON OVERFLOW)."""
    _write_export(tmp_path)
    (tmp_path / "MYSCHEMA" / "Sequences" / "NEG_SEQ.sql").write_text(
        "CREATE SEQUENCE myschema.neg_seq INCREMENT BY -1 NOCACHE;"
    )
    (tmp_path / "MYSCHEMA" / "Views" / "ORDER_SUMMARY.sql").write_text(
        "CREATE OR REPLACE FORCE VIEW myschema.order_summary (parent_ref) AS "
        "SELECT listagg(order_ref, '|' on overflow truncate '...' without count) "
        "within group (order by order_ref) parent_ref FROM myschema.orders;"
    )
    items = list(
        OraSchemaIngestor(OraSchemaConfig(path=str(tmp_path), database_name="MYDB")).extract()
    )
    assert "oracle://MYDB.MYSCHEMA.sequence.NEG_SEQ" in _ids(_nodes(items, "Sequence"))
    assert "oracle://MYDB.MYSCHEMA.ORDER_SUMMARY" in _ids(_nodes(items, "View"))


def test_create_index_regex_path_extracts_name_and_columns(tmp_path):
    """sqlglot returns CREATE INDEX as an opaque Command; the regex path still yields an Index node
    with its INDEXES edge to the column."""
    items = _extract(tmp_path)  # the standard export includes PRODUCT_PRICES_INDEX_1 on (product_id)
    assert (
        "oracle://MYDB.MYSCHEMA.PRODUCT_PRICES.index.PRODUCT_PRICES_INDEX_1"
        in _ids(_nodes(items, "Index"))
    )
    indexes_edge = next(e for e in _edges(items, "INDEXES"))
    assert indexes_edge.to_key["id"] == "oracle://MYDB.MYSCHEMA.PRODUCT_PRICES.PRODUCT_ID"


# -- Integration against a real graph ------------------------------------------------


def test_run_merges_into_graph_and_is_idempotent(graph, tmp_path):
    _write_export(tmp_path)
    config = OraSchemaConfig(path=str(tmp_path), database_name="MYDB")

    first = OraSchemaIngestor(config).run(GraphWriter(graph))
    assert first.nodes_merged > 0

    # The foreign key lands as a Column->Column REFERENCES between the two tables.
    assert graph.query(
        "MATCH (:Column {id:'oracle://MYDB.MYSCHEMA.PRODUCT_PRICES.PRODUCT_ID'})"
        "-[:REFERENCES]->(c:Column {id:'oracle://MYDB.MYSCHEMA.PRODUCTS.PRODUCT_ID'}) RETURN c"
    ).result_set != []
    # Database -> Schema -> Table chain exists.
    assert graph.query(
        "MATCH (:Database {id:'oracle://MYDB'})-[:HAS_SCHEMA]->(:Schema)"
        "-[:HAS_TABLE]->(t:Table {name:'PRODUCTS'}) RETURN t"
    ).result_set != []

    # Re-running merges onto the same nodes -- no duplicates.
    before = graph.query("MATCH (n) RETURN count(n)").result_set[0][0]
    OraSchemaIngestor(config).run(GraphWriter(graph))
    after = graph.query("MATCH (n) RETURN count(n)").result_set[0][0]
    assert before == after
