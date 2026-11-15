"""Canonical id scheme for the Bash graph, mirroring the other ingestors' shape (root at
`bash://<project>`, ids embed names/positions) so a single graph can hold every language with
interchangeable node ids. A future deep engine (a real shell resolver) would key nodes by these
same functions, so its output stays a pure delta over the fast pass.

Bash-specific choices:

- **No package/module tier and no type tier.** Bash has neither namespaces nor classes/types, so
  a `Function` hangs directly off the `File` that defines it (`File -[HAS_FUNCTION]-> Function`).
  A function is therefore keyed off its defining *file*, not a package.
- **Case-preserving.** Bash identifiers are case-sensitive (`Deploy` and `deploy` are two
  different functions), so ids are NOT lowercased (unlike Java/Rust, which can).
- **No parameter signature.** Bash functions declare no parameter list (arguments arrive as
  `$1`, `$2`, ...), so a function id carries only its name -- there is nothing to disambiguate."""

from __future__ import annotations


def project_id(project_name: str) -> str:
    return f"bash://{project_name}"


def file_id(proj_id: str, rel_path: str) -> str:
    return f"{proj_id}/file/{rel_path.replace(chr(92), '/').lstrip('./')}"


def function_id(file_id_: str, name: str) -> str:
    return f"{file_id_}.func.{name}"


# -- cross-repo linkage (ADR 0004) -------------------------------------------------------
# Project-INDEPENDENT identity for a callable as an external *consumer* names it. In Bash a
# function is reachable simply by its name once its defining file is `source`d, so the linkage is
# just the bare function/command name (there is no package/type qualifier). It is BOTH the id of
# the placeholder that stands in for an unresolved external command AND the `linkage_key` property
# stamped on every real, locally-defined function -- so ingesting a repo that defines `deploy`
# into the same graph lets reconciliation match `extern://bash/deploy` (a caller's placeholder)
# against the real `deploy` and upgrade the CALLS edge in place (e.g. across `source`d libraries).
def extern_function_key(name: str) -> str:
    return f"extern://bash/{name}"


def extern_file_key(path: str) -> str:
    """Placeholder id for a `source`d file that is not part of the ingested project (a system
    file like `/etc/profile`, or a path we cannot resolve to a project file). Distinct namespace
    from `extern_function_key` so a command name and a file path can never collide."""
    return f"extern://bash/file/{path.replace(chr(92), '/')}"
