"""Configuration for the PL/SQL ingestors.

Two ingestors share this module: the Oracle DB ingestor (`oracle`, PLSQLSourceConfig
-- reads a live connection) and the file-based ingestor (`plsql`, PLSQLFileConfig /
GitPLSQLProjectConfig -- parses source files). They deliberately use different id
roots: the DB path roots at "oracle://{database}" (it knows the real schema), the
file path at "plsql://{project}" (files carry no schema)."""

from __future__ import annotations

from dataclasses import dataclass

# The file-based ingestor's source extensions -- the standard Oracle source-file
# suffixes plus .sql (best-effort: a plain-DDL .sql just parses to few/no
# declarations). Kept here so the local-dir ingestor and the git ingestor agree on
# exactly one list. Lowercased; matching is case-insensitive at the call sites.
PLSQL_FILE_SUFFIXES = (".pks", ".pkb", ".prc", ".fnc", ".trg", ".tps", ".tpb", ".pls", ".plb", ".sql")


@dataclass(frozen=True)
class PLSQLSourceConfig:
    dsn: str
    """A python-oracledb connect string, e.g. "user/password@host:port/service_name"."""

    database_name: str
    """Logical name for this source -- MUST match whatever the SQL ingestor used for
    the same database (via its --database-name), or DEPENDS_ON/HAS_TRIGGER edges won't
    find the Database/Schema/Table nodes the SQL ingestor already created."""

    schemas: tuple[str, ...] | None = None
    """Which Oracle schemas (users) to scan. None means "just the connected user's own
    schema" -- ALL_* catalog views span every schema the connected user can see, which
    could be much broader than intended without this restriction."""


@dataclass(frozen=True)
class PLSQLFileConfig:
    """Config for `auryn run plsql` -- a local directory of PL/SQL source files."""

    path: str
    project_name: str
    solution_name: str | None = None
    workers: int | None = None

    strict: bool = False
    """Fail-fast mode (for a first ingestion of unfamiliar/prospect data). When True, the
    first file that fails to decode or parse halts the whole run *before* anything is written
    to the graph, after writing a full diagnostic report (position, source window, dialect
    signals, progress) to `diagnostics_dir`. When False (default, restored after the prospect
    run), parse failures are logged and skipped, best-effort. Strict also forces single-worker
    parsing so the halt is deterministic and its logs coherent."""

    diagnostics_dir: str | None = None
    """Where strict-mode diagnostic reports are written. None -> "<cwd>/auryn-diagnostics"."""

    encodings: tuple[str, ...] = ("utf-8", "utf-8-sig", "cp1252", "latin-1")
    """Decode attempts, in order. Legacy Oracle exports are frequently cp1252/latin-1 rather
    than UTF-8; the first that succeeds is used and recorded. In strict mode a file that needs
    a non-UTF-8 fallback is logged (not halted -- encoding is recoverable, unlike a parse
    error)."""

    max_parse_seconds: int | None = None
    """Best-effort only: per-file parse timeout (Unix). A pathological file (the huge generated
    calc packages can take tens of seconds under the ANTLR Python runtime) that exceeds this is
    skipped and reported, so one slow file can't stall the whole run. None = no cap."""

    files: tuple[str, ...] | None = None
    """An explicit list of files to ingest (from `--files-from`), instead of scanning `path` by
    suffix. When set, these exact files are parsed -- the PLSQL_FILE_SUFFIXES filter is bypassed, so
    PL/SQL living in odd-extension files (found e.g. by grepping for CREATE PACKAGE/FUNCTION) is
    ingested too. `path` still serves as the id root: each listed file's id is keyed by its path
    relative to `path`, and any file that isn't under `path` is skipped with a warning. Entries may
    be absolute or relative to `path`."""


@dataclass(frozen=True)
class GitPLSQLProjectConfig:
    """Config for `auryn git-sync plsql` and the multi-language endpoint -- a
    git-tracked PL/SQL project."""

    repo_uri: str
    project_name: str
    solution_name: str | None = None
    workers: int | None = None


def project_id(project_name: str, solution_name: str | None) -> str:
    """Root id under which every Package/Procedure/Function/Table/... id for a
    file-based PL/SQL project is prefixed -- shared by the local-dir and git
    ingestors so this correctness-critical prefix lives in one place (mirrors the C#
    ingestor's project_id)."""
    if solution_name:
        return f"plsql://{solution_name}/{project_name}"
    return f"plsql://{project_name}"
