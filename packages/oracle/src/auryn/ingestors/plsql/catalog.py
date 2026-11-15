"""Raw Oracle catalog queries via python-oracledb. Returns plain dicts/lists -- no
ANTLR or graph-model code here, kept separate so ingestor.py can compose this with
source_analysis.py independently.

Every query is scoped to `owner IN (schemas)` -- ALL_* catalog views span every
schema the connected user can see, which is usually much broader than intended.

Shapes below were verified against a real Oracle XE 21c instance (gvenzl/oracle-xe),
not assumed -- a few things aren't what the Oracle docs alone would suggest:
- ALL_PROCEDURES doesn't distinguish packaged procedures from packaged functions
  (both show OBJECT_TYPE='PACKAGE') -- only ALL_ARGUMENTS does, via a POSITION=0 row
  (the return-value slot), present even for zero-argument functions.
- ALL_SOURCE stores source *without* the "CREATE OR REPLACE" prefix, one row per line.
- ALL_TRIGGERS.TRIGGER_BODY is the full body text directly (no ALL_SOURCE needed).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from auryn.core.plugins import ensure_importable

if TYPE_CHECKING:  # for type-checkers only -- never imported at runtime
    import oracledb

# oracledb (a ~15MB driver with a native/cryptography dependency chain) is an optional extra
# (`auryn-oracle[live]`): only the *live* Oracle ingestor needs it, never the file-based
# `plsql` path. It's imported on demand here so a lean, file-only install doesn't carry it --
# and auto-installed on the first live connection (see ensure_importable).


def connect(dsn: str) -> "oracledb.Connection":
    oracledb = ensure_importable("oracledb", "oracledb>=4.0.2")
    return oracledb.connect(dsn=dsn)


def current_schema(conn) -> str:
    cur = conn.cursor()
    cur.execute("SELECT USER FROM DUAL")
    return cur.fetchone()[0]


def _in_clause(schemas: list[str]) -> tuple[str, dict]:
    binds = {f"s{i}": schema for i, schema in enumerate(schemas)}
    return ", ".join(f":{key}" for key in binds), binds


def object_status(conn, schemas: list[str]) -> dict[tuple[str, str, str], str]:
    """(owner, object_name, object_type) -> STATUS ('VALID'/'INVALID')."""
    placeholder, binds = _in_clause(schemas)
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT owner, object_name, object_type, status
        FROM all_objects
        WHERE owner IN ({placeholder})
          AND object_type IN ('PROCEDURE','FUNCTION','PACKAGE','PACKAGE BODY','TRIGGER','TYPE')
        """,
        binds,
    )
    return {(owner, name, obj_type): status for owner, name, obj_type, status in cur}


def routines(conn, schemas: list[str]) -> list[dict]:
    """Standalone + packaged procedures/functions. `object_type='PACKAGE'` with a
    non-null `procedure_name` means a packaged member -- whether that member is a
    procedure or a function isn't in this view; join against `arguments()` for that
    (a POSITION=0 row means function)."""
    placeholder, binds = _in_clause(schemas)
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT owner, object_name, procedure_name, object_type, overload
        FROM all_procedures
        WHERE owner IN ({placeholder})
        """,
        binds,
    )
    return [
        {
            "owner": owner,
            "object_name": object_name,
            "procedure_name": procedure_name,
            "object_type": object_type,
            "overload": overload,
        }
        for owner, object_name, procedure_name, object_type, overload in cur
    ]


def arguments(conn, schemas: list[str]) -> list[dict]:
    placeholder, binds = _in_clause(schemas)
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT owner, object_name, package_name, argument_name, position,
               data_type, in_out, overload, defaulted
        FROM all_arguments
        WHERE owner IN ({placeholder})
        ORDER BY owner, package_name, object_name, overload, position
        """,
        binds,
    )
    return [
        {
            "owner": owner,
            "object_name": object_name,
            "package_name": package_name,
            "argument_name": argument_name,
            "position": position,
            "data_type": data_type,
            "in_out": in_out,
            "overload": overload,
            "defaulted": defaulted,
        }
        for (
            owner,
            object_name,
            package_name,
            argument_name,
            position,
            data_type,
            in_out,
            overload,
            defaulted,
        ) in cur
    ]


def source_text(conn, owner: str, name: str, object_type: str) -> str | None:
    """Reconstructs full "CREATE OR REPLACE ..." source for a PROCEDURE/FUNCTION/
    PACKAGE/PACKAGE BODY object from ALL_SOURCE. Returns None if there's no source
    (shouldn't normally happen for a real object, but the view can lag DDL)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT text FROM all_source WHERE owner = :owner AND name = :name AND type = :object_type ORDER BY line",
        {"owner": owner, "name": name, "object_type": object_type},
    )
    lines = [row[0] for row in cur]
    if not lines:
        return None
    return "CREATE OR REPLACE " + "".join(lines)


# A `wrap`-obfuscated PL/SQL unit's stored source begins with a header line ending in
# the bare `wrapped` keyword -- e.g. reconstructed "CREATE OR REPLACE PACKAGE BODY \"X\"
# wrapped" (source_text prepends "CREATE OR REPLACE ") -- followed by base64-ish
# bytecode. The keyword only ever appears on that first header line, so inspecting the
# first non-blank line alone avoids false positives from body text or comments that
# merely contain the word "wrapped".
_WRAPPED_HEADER_RE = re.compile(r"\S\s+wrapped\s*$", re.IGNORECASE)


def is_wrapped_source(source: str | None) -> bool:
    """True if `source` (as reconstructed by source_text, or a raw ALL_TRIGGERS body) is
    Oracle `wrap`-obfuscated bytecode rather than readable PL/SQL. `wrap` is one-way, so
    the bytecode can't be turned back into source -- callers use this to flag the unit
    and to skip both storing the gibberish as a `source` property and trying to parse it
    for call/table edges. Cheap and side-effect-free."""
    if not source:
        return False
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return bool(_WRAPPED_HEADER_RE.search(stripped))
    return False


def dependencies(conn, schemas: list[str]) -> list[dict]:
    """Object-level dependencies, restricted to dependencies *within* the target
    schemas on both ends -- otherwise every PL/SQL unit's routine dependency on
    SYS.STANDARD (and similar Oracle-internal noise) would show up too."""
    placeholder, binds = _in_clause(schemas)
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT owner, name, type, referenced_owner, referenced_name, referenced_type
        FROM all_dependencies
        WHERE owner IN ({placeholder}) AND referenced_owner IN ({placeholder})
        """,
        binds,
    )
    return [
        {
            "owner": owner,
            "name": name,
            "type": obj_type,
            "referenced_owner": referenced_owner,
            "referenced_name": referenced_name,
            "referenced_type": referenced_type,
        }
        for owner, name, obj_type, referenced_owner, referenced_name, referenced_type in cur
    ]


def triggers(conn, schemas: list[str]) -> list[dict]:
    placeholder, binds = _in_clause(schemas)
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT owner, trigger_name, table_owner, table_name, trigger_type,
               triggering_event, status, trigger_body
        FROM all_triggers
        WHERE owner IN ({placeholder})
        """,
        binds,
    )
    return [
        {
            "owner": owner,
            "trigger_name": trigger_name,
            "table_owner": table_owner,
            "table_name": table_name,
            "trigger_type": trigger_type,
            "triggering_event": triggering_event,
            "status": status,
            "trigger_body": trigger_body,
        }
        for (
            owner,
            trigger_name,
            table_owner,
            table_name,
            trigger_type,
            triggering_event,
            status,
            trigger_body,
        ) in cur
    ]


def types(conn, schemas: list[str]) -> list[dict]:
    placeholder, binds = _in_clause(schemas)
    cur = conn.cursor()
    cur.execute(
        f"SELECT owner, type_name, typecode FROM all_types WHERE owner IN ({placeholder})",
        binds,
    )
    return [
        {"owner": owner, "type_name": type_name, "typecode": typecode}
        for owner, type_name, typecode in cur
    ]
