"""Canonical id scheme for the Java graph, mirroring the C# ingestor's (Java and C# share the
same shape: a package/namespace tier over types-with-members). A future deep (JDT/javac) engine
would key nodes by these same functions, so its output stays interchangeable in one graph --
the id-parity the C#/Python/C ingestors rely on.

Ids embed names/positions and, for overload disambiguation, parameter *type* text -- never
resolved/inferred types (the id-stability rule)."""

from __future__ import annotations

_KIND_SLUG = {"Class": "class", "Interface": "interface", "Enum": "enum", "Record": "record"}


def project_id(project_name: str) -> str:
    return f"java://{project_name}"


def module_id(proj_id: str, module: str) -> str:
    """A Maven/Gradle module (subproject) -- the per-file scope root over the package tier, keyed by
    its path-derived name (see javamod.module_for_path). A single-module/root project uses the
    project name as the module name."""
    return f"{proj_id}/module/{module}"


def package_id(scope_id: str, dotted: str) -> str:
    """`scope_id` is the containing module_id (every package now nests under its module, so two
    modules' identically-named packages stay distinct)."""
    return f"{scope_id}/package/{dotted.lower()}"


def file_id(proj_id: str, rel_path: str) -> str:
    return f"{proj_id}/file/{rel_path.replace(chr(92), '/').lstrip('./')}"


def _arity(type_parameters: list[str]) -> str:
    # `Foo` vs `Foo<T,U>` -> distinct ids (a generic and its raw-name twin can't collide).
    return f"`{len(type_parameters)}" if type_parameters else ""


def type_id(scope_id: str, kind: str, name: str, type_parameters: list[str]) -> str:
    """`scope_id` is the containing package_id / project_id (top-level) or the parent type_id
    (nested)."""
    return f"{scope_id}.{_KIND_SLUG[kind]}.{name.lower()}{_arity(type_parameters)}"


def method_id(owner_type_id: str, name: str, param_types: list[str]) -> str:
    signature = ",".join((t or "").lower() for t in param_types)
    return f"{owner_type_id}.method.{name.lower()}({signature})"


def field_id(owner_type_id: str, name: str) -> str:
    return f"{owner_type_id}.field.{name.lower()}"


def parameter_id(owner_id: str, position: int, name: str) -> str:
    return f"{owner_id}.param.{position}.{name.lower()}"


def enum_member_id(enum_type_id: str, name: str) -> str:
    return f"{enum_type_id}.member.{name.lower()}"


# -- cross-repo linkage (ADR 0004) -------------------------------------------------------
# Project-INDEPENDENT key for an externally-visible method, stored as the `linkage_key`
# property on real definitions and as the id of the placeholder that stands in for an
# unresolved call. `qualified_name` is the callee as a consumer would name it -- the owning
# type's fully-qualified name plus the member, NAME-based (never parameter types, which a
# caller can't know): e.g. `com.acme.Billing#charge` (a constructor uses the type's simple
# name as the member, so `com.acme.Widget#Widget`). So a `Widget#charge` call in repo A and
# `com.acme.Billing#charge`'s definition in repo B land on one node when both share a graph.
# (Distinct from a Method's own project-scoped `id`, which keeps two repos' same-named methods
# apart -- see ADR 0004 §2.)
def extern_method_key(qualified_name: str) -> str:
    return f"extern://java/{qualified_name}"
