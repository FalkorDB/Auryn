"""Canonical id scheme for the Go graph, mirroring the Java ingestor's in spirit (root at
`go://<project>`, ids embed names/positions and -- for parity across ingestors -- the parameter
*type* text, never resolved/inferred types, so a future deep engine keying nodes by these same
functions stays interchangeable in one graph).

Go-specific choices (deliberate deviations from Java):

- **Packages are keyed by DIRECTORY, under a go.mod MODULE tier.** A Go package is one directory;
  its node id is `go://proj/package/<dir>` (the repo-relative dir). The directory -- not the bare
  `package` clause name -- is the key, because many directories legitimately share a clause name
  (`package main` in every `cmd/*`; common `util`/`handlers`/`models`), which keying by name
  collapsed onto one node. The dir is PURE PATH (no manifest), so a full ingest and an incremental
  resync derive it identically. A package's canonical IMPORT PATH (go.mod module path + dir) is
  carried as a property and is what cross-repo linkage keys use (see `extern_function_key`); the
  module (go.mod) it belongs to is a `go://proj/module/<dir>` node above it.
- **Case-preserving.** Go is case-sensitive and its export rule is *encoded in case*
  (`Put` is exported, `put` is not), so a type/func/method/field can legitimately have an
  exported and an unexported member differing only in case. We therefore do NOT lowercase
  identifiers in ids (Java can, and does; Go must not) -- lowercasing would collide `Foo`/`foo`.
- **Free functions** get a `.func.` segment off the package; **methods** get a `.method.`
  segment off their receiver type (Go has no overloading, but the param-type signature is kept
  for cross-ingestor id-shape parity and stability)."""

from __future__ import annotations

_KIND_SLUG = {"Struct": "struct", "Interface": "interface", "TypeAlias": "type"}


def project_id(project_name: str) -> str:
    return f"go://{project_name}"


def package_id(proj_id: str, package_dir: str) -> str:
    """One Go package, keyed by its repo-relative DIRECTORY ("" = repo root). Directories are
    unique per package, so two dirs that share a `package` clause name stay distinct."""
    return f"{proj_id}/package/{package_dir}" if package_dir else f"{proj_id}/package"


def module_id(proj_id: str, module_dir: str) -> str:
    """One go.mod module, keyed by its directory ("" = a repo-root go.mod)."""
    return f"{proj_id}/module/{module_dir}" if module_dir else f"{proj_id}/module"


def file_id(proj_id: str, rel_path: str) -> str:
    return f"{proj_id}/file/{rel_path.replace(chr(92), '/').lstrip('./')}"


def _arity(type_parameters: list[str]) -> str:
    # `Container` vs `Container[T]` -> distinct ids (a generic and its raw-name twin can't collide).
    return f"`{len(type_parameters)}" if type_parameters else ""


def type_id(pkg_id: str, kind: str, name: str, type_parameters: list[str]) -> str:
    return f"{pkg_id}.{_KIND_SLUG[kind]}.{name}{_arity(type_parameters)}"


def _signature(param_types: list[str]) -> str:
    return ",".join((t or "") for t in param_types)


def function_id(pkg_id: str, name: str, param_types: list[str]) -> str:
    return f"{pkg_id}.func.{name}({_signature(param_types)})"


def method_id(owner_type_id: str, name: str, param_types: list[str]) -> str:
    return f"{owner_type_id}.method.{name}({_signature(param_types)})"


def field_id(owner_type_id: str, name: str) -> str:
    return f"{owner_type_id}.field.{name}"


def parameter_id(owner_id: str, position: int, name: str) -> str:
    return f"{owner_id}.param.{position}.{name}"


def const_id(pkg_id: str, name: str) -> str:
    return f"{pkg_id}.const.{name}"


# -- cross-repo linkage (ADR 0004) -------------------------------------------------------
# Project-INDEPENDENT identity for a callable as an external *consumer* names it: `package.Func`
# for a free function (`fmt.Println`, `store.Save`) or `pkg.Type.Method` for a method
# (`io.Reader.Read`). NAME-based only -- never parameter/inferred types (a consumer can't know a
# callee's param names, and the id-stability rule forbids volatile types in ids). It is BOTH the
# id of the placeholder that stands in for an unresolved external call AND the `linkage_key`
# property stamped on the real, locally-defined callable -- so ingesting the defining repo into
# the same graph lets reconciliation match placeholder.id == real.linkage_key and upgrade in
# place. (Distinct from the project-scoped node id, which keeps two repos' same-named symbols
# separate; only the linkage crosses the boundary.)
def extern_function_key(qualified_name: str) -> str:
    return f"extern://go/{qualified_name}"
