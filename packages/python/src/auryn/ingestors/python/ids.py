"""Canonical id scheme for the Python graph (see GRAPH_MODEL.md).

Both in-process engines -- `ast` (resolution.py with no oracle) and `jedi` -- key nodes by
these functions, so their output is interchangeable in one graph (like the C#
roslyn/treesitter id parity).

Ids embed parameter *names*, never inferred types (the id-stability rule): inferred types
are volatile, so a type in an id would churn the id on every re-sync.
"""

from __future__ import annotations


def project_id(project_name: str) -> str:
    return f"python://{project_name}"


def package_dirs_from_paths(paths) -> frozenset[str]:
    """The set of directories that are regular packages -- those containing an ``__init__.py`` --
    given the full list of repo-relative ``.py`` paths in an ingest. The repo root is spelled `""`.
    This is the package context `dotted_module_path` uses to find a file's true import root; it is
    computed once per ingest from the complete file list and threaded through the full AND
    incremental engines identically (so both map a path to the same dotted name)."""
    dirs: set[str] = set()
    for p in paths:
        p = p.replace("\\", "/")
        if p == "__init__.py" or p.endswith("/__init__.py"):
            dirs.add(p[: -len("__init__.py")].rstrip("/"))
    return frozenset(dirs)


def _import_root_index(dirs: list[str], package_dirs: frozenset[str] | None) -> int:
    """Index into a module's ancestor-directory chain `dirs` where the importable dotted name
    begins -- i.e. the first directory that is part of the import path.

    With `package_dirs` (the set of `__init__.py`-bearing dirs) AND the innermost dir being a
    regular package, walk UP the contiguous package chain and stop at the first ancestor that is
    NOT a package (or not an identifier): that ancestor is the import root. This drops a `src/`
    layout root and any repo/distribution wrapper dir above the top package -- e.g.
    ``src/pkg/mod.py`` -> start at ``pkg``; ``acme-payments/acme_payments/core.py`` -> start at
    ``acme_payments``.

    Without package evidence (no `__init__.py` context, or a PEP-420 namespace package whose
    innermost dir isn't a regular package) fall back to the always-safe rule: drop the leading run
    up to and including the last non-identifier dir (a segment with a hyphen/space/etc. can never be
    imported, so it and everything above it is above the root), and keep every identifier segment.
    """
    if package_dirs is not None and dirs and "/".join(dirs) in package_dirs and dirs[-1].isidentifier():
        i = len(dirs) - 1
        while i > 0 and dirs[i - 1].isidentifier() and "/".join(dirs[:i]) in package_dirs:
            i -= 1
        return i
    last_non_ident = -1
    for j, seg in enumerate(dirs):
        if not seg.isidentifier():
            last_non_ident = j
    return last_non_ident + 1


def dotted_module_path(rel_path: str, package_dirs: frozenset[str] | None = None) -> str:
    """Repo-relative file path -> importable dotted module name, anchored at the file's true import
    root so the dotted name equals what an importing module writes (the cross-repo linkage key,
    ADR 0004, depends on this).

    'app/widget.py'                    -> 'app.widget'
    'app/__init__.py'                  -> 'app'         (the package the __init__ defines)
    'main.py'                          -> 'main'
    'acme-payments/acme_pay/x.py'      -> 'acme_pay.x'  (hyphenated distribution dir isn't importable)
    'src/pkg/mod.py' (pkg is a package) -> 'pkg.mod'    (only with package_dirs: `src/` is the root)

    `package_dirs` (from `package_dirs_from_paths`) enables `__init__.py`-aware root detection for
    `src/` layouts and identifier-named wrapper dirs. Without it the derivation is context-free (only
    provably-non-importable leading segments are stripped) -- REQUIRED as the fallback because the
    id, scope-key and scope-id derivations must all map a given path to the SAME dotted name, in
    both the full and incremental engines, or a scoped resync would target the wrong nodes (see
    tests/ingestors/test_python_incremental_equivalence.py). The engines pass the same
    ingest-wide `package_dirs` to all three, so they stay in agreement. For a clean or namespace
    layout the two modes produce identical output.
    """
    p = rel_path.replace("\\", "/")
    if p.endswith(".py"):
        p = p[: -len(".py")]
    is_init = False
    if p.endswith("/__init__"):
        p = p[: -len("/__init__")]
        is_init = True
    elif p == "__init__":
        p, is_init = "", True
    p = p.strip("/")
    if not p:
        return ""
    parts = p.split("/")
    # For an __init__, the module IS the package: all components are directories, none is a leaf.
    dirs, leaf = (parts, None) if is_init else (parts[:-1], parts[-1])
    start = _import_root_index(dirs, package_dirs)
    kept = dirs[start:]
    if leaf is not None:
        kept = kept + [leaf]
    return ".".join(kept)


def is_package_init(rel_path: str) -> bool:
    p = rel_path.replace("\\", "/")
    return p == "__init__.py" or p.endswith("/__init__.py")


def package_id(proj_id: str, dotted: str) -> str:
    return f"{proj_id}/package/{dotted}"


def module_id(proj_id: str, dotted: str) -> str:
    return f"{proj_id}/module/{dotted}"


def class_id(scope_id: str, name: str) -> str:
    """`scope_id` is the containing module_id (top-level class) or the parent class_id
    (nested class)."""
    return f"{scope_id}.class.{name.lower()}"


def _sig(param_names: list[str]) -> str:
    return ",".join(param_names)


def function_id(mod_id: str, name: str, param_names: list[str]) -> str:
    return f"{mod_id}.func.{name.lower()}({_sig(param_names)})"


def method_id(cls_id: str, name: str, param_names: list[str]) -> str:
    return f"{cls_id}.method.{name.lower()}({_sig(param_names)})"


def attribute_id(cls_id: str, name: str) -> str:
    return f"{cls_id}.attr.{name.lower()}"


def parameter_id(owner_id: str, position: int, name: str) -> str:
    return f"{owner_id}.param.{position}.{name.lower()}"


# -- cross-repo linkage (ADR 0004) -------------------------------------------------
# Project-INDEPENDENT identity for a symbol as an external consumer would name it: the
# module/class path plus the name (never params/types -- a consumer can't know a callee's param
# names, and the id-stability rule forbids volatile types in ids). It is BOTH the id of the
# placeholder node that stands in for an unresolved reference AND the `linkage` property stamped
# on the real, locally-defined node -- so ingesting the defining repo into the same graph lets
# resolution match placeholder.id == real.linkage and upgrade in place. `::` separates the
# qualifier from the symbol so it can't collide with a module placeholder's id.
_EXTERN = "extern://python"


def extern_symbol_key(qualifier: str, name: str) -> str:
    """`qualifier` is the dotted module (top-level function/class) or the class qname (method).
    e.g. ('app.db','get_connection') -> 'extern://python/app.db::get_connection'."""
    return f"{_EXTERN}/{qualifier}::{name}" if qualifier else f"{_EXTERN}/::{name}"


def extern_module_key(dotted: str) -> str:
    """Placeholder id / linkage for an imported module not in this ingest (e.g. 'json')."""
    return f"{_EXTERN}/{dotted}"
