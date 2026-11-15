"""Canonical id scheme for the C++ graph.

C++ is the structural union of the C ingestor (free functions, structs/unions/enums, macros,
includes) and the Java ingestor (a namespace tier over types-with-members, a class hierarchy).
The ids reflect that: a `Namespace` tier (like Java's Package / C#'s Namespace) scopes types
and free functions; types scope methods/fields; overloads and templates are disambiguated by
parameter *type* text and template *arity* respectively, so `foo()`/`foo(int)` and
`Box`/`Box<T>` never collide. A future deep (libclang) engine would key nodes by these same
functions, so its output stays interchangeable in one graph -- the id-parity the
C#/Python/C/Java ingestors rely on.

Ids embed names/positions and, for disambiguation, parameter *type* text and template arity --
never resolved/inferred types (the id-stability rule)."""

from __future__ import annotations

_KIND_SLUG = {"Class": "class", "Struct": "struct", "Union": "union", "Enum": "enum"}


def project_id(project_name: str) -> str:
    return f"cpp://{project_name}"


def namespace_id(proj_id: str, qualified: str) -> str:
    # `qualified` is the `::`-joined namespace path, e.g. "geo::detail".
    return f"{proj_id}/namespace/{qualified.lower()}"


def file_id(proj_id: str, rel_path: str) -> str:
    return f"{proj_id}/file/{rel_path.replace(chr(92), '/').lstrip('./')}"


def _arity(type_parameters: list[str]) -> str:
    # `Box` vs `Box<T,U>` -> distinct ids (a template and its raw-name twin can't collide).
    return f"`{len(type_parameters)}" if type_parameters else ""


def type_id(scope_id: str, kind: str, name: str, type_parameters: list[str]) -> str:
    """`scope_id` is the containing namespace_id / project_id (top-level) or the parent type_id
    (nested)."""
    return f"{scope_id}.{_KIND_SLUG[kind]}.{name.lower()}{_arity(type_parameters)}"


def method_id(owner_type_id: str, name: str, param_types: list[str]) -> str:
    signature = ",".join((t or "").lower() for t in param_types)
    return f"{owner_type_id}.method.{name.lower()}({signature})"


def function_id(scope_id: str, name: str, param_types: list[str]) -> str:
    """A free function, keyed by its scope (namespace/project) + name + parameter types (C++
    allows overloading, so the parameter types are part of the identity)."""
    signature = ",".join((t or "").lower() for t in param_types)
    return f"{scope_id}.func.{name.lower()}({signature})"


def field_id(owner_type_id: str, name: str) -> str:
    return f"{owner_type_id}.field.{name.lower()}"


def parameter_id(owner_id: str, position: int, name: str) -> str:
    return f"{owner_id}.param.{position}.{name.lower()}"


def enum_member_id(enum_type_id: str, name: str) -> str:
    return f"{enum_type_id}.member.{name.lower()}"


# -- preprocessor: file-local (mirrors the C ingestor's macro scoping) --------------------
def macro_id(a_file_id: str, name: str) -> str:
    return f"{a_file_id}.macro.{name}"


# -- cross-repo linkage (ADR 0004) -------------------------------------------------------
# Project-independent key for a free/namespaced function, name-based (no param types -- a
# consumer can't know an overload's types): `extern://cpp/<ns::name>` or `extern://cpp/<name>`.
# Stored as `linkage_key` on real definitions and as the id of the placeholder standing in for an
# unresolved call, so `mylib::compute` in repo B and a `mylib::compute()` call in repo A land on
# one node when both share a graph.
def extern_function_key(qualified_name: str) -> str:
    return f"extern://cpp/{qualified_name}"
