"""File-based Oracle *schema-catalog* ingestor (`auryn run oraschema`).

Reads a directory of per-object DDL scripts -- the layout a Redgate "Schema Compare
for Oracle" scripts-folder export produces (DatabaseInformation.xml at the root, then
one `<SCHEMA>/` folder per schema holding SchemaInformation.xml + Tables/ Views/
Indexes/ Sequences/ with one `CREATE ...` per `.sql` file) -- and reconstructs the
Database -> Schema -> Table/Column/Constraint/Index/Sequence/View subgraph *without a
live connection*.

It roots ids at `oracle://<database-name>` and uses the exact id scheme the live `sql`
ingestor writes (`oracle://db.SCHEMA.TABLE.COLUMN`, ...), so a schema reconstructed
from files here unifies with -- rather than shadows -- the same database's code graph.
The DDL itself is parsed by the same vendored ANTLR grammar the `plsql`/`oracle`
ingestors use (source_analysis.parse_declarations), so no live-DB drivers are needed.
"""
