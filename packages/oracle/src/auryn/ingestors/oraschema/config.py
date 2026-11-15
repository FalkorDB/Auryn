"""Configuration for the file-based Oracle schema-catalog ingestor (`oraschema`)."""

from __future__ import annotations

from dataclasses import dataclass

# Object-DDL script extension. A Redgate scripts-folder export writes exactly one
# `CREATE ...` statement per `.sql` file; the two metadata files are `.xml` (read
# separately), so a plain `*.sql` walk picks up the object DDL and nothing else.
OBJECT_FILE_SUFFIXES = (".sql",)


@dataclass(frozen=True)
class OraSchemaConfig:
    """Config for `auryn run oraschema` -- a directory of per-object Oracle DDL scripts."""

    path: str
    """Root of the export: the folder holding DatabaseInformation.xml and the per-schema
    subfolders (or, as a fallback, a single schema folder of object `.sql` files)."""

    database_name: str
    """Logical database name -- the root of every id (`oracle://<database_name>...`). MUST
    match whatever the live `sql`/`oracle` ingestor used (its --database-name) for the same
    database, or the reconstructed schema won't unify with that database's code graph."""

    strict: bool = False
    """Fail-fast: halt on the first `.sql` file that won't parse, reporting its path/position
    and the offending source line, before anything is written. Default (False) is best-effort
    -- parse gaps are logged per file and skipped so as much of the catalog as possible is
    reconstructed, which is what a schema-reconstruction pass usually wants."""

    workers: int | None = None
    """Best-effort only: number of parallel parse workers. None -> os.cpu_count(). A full schema
    export is thousands of tiny files, but the *views* carry whole SELECTs that dominate wall-clock
    under the ANTLR runtime, so parsing them across cores is the main speed lever. Strict mode
    ignores this and parses single-threaded so its halt is deterministic and its log coherent."""

    max_parse_seconds: int | None = None
    """Best-effort only: per-file parse timeout (Unix/SIGALRM). A pathological file -- typically a
    huge generated view whose SELECT trips the slow full-LL parse -- that exceeds this is skipped
    (reported, not merged) so one file can't stall the run. None = no cap (parse everything)."""

    encodings: tuple[str, ...] = ("utf-8", "utf-8-sig", "cp1252", "latin-1")
    """Decode attempts for the object `.sql` files, in order (they're normally UTF-8/ASCII;
    the fallbacks cover the occasional legacy-encoded export). The two *Information.xml
    metadata files are UTF-16 and decoded separately by the XML parser."""
