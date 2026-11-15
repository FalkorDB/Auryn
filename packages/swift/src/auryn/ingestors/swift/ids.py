"""Canonical id scheme for the Swift graph, mirroring the Rust/Java ingestors' shape (types with
members, free functions hanging off the project root) so a single graph can hold every language
with interchangeable node ids. A future deep (SourceKit-LSP / swiftc) engine would key nodes by
these same functions, so its output stays a pure delta over the fast pass -- the id-parity the
Rust/Java/C#/Python/C ingestors rely on.

Ids embed names/positions and, for overload disambiguation, parameter *type* text -- never
resolved/inferred types (the id-stability rule). Swift forbids re-declaring a member with the
same name + parameter types on one type (even across extensions), so an owner_type_id + name +
signature is unique without any extension/trait suffix (contrast Rust, where an inherent and a
trait-impl method can share a name)."""

from __future__ import annotations

_KIND_SLUG = {"Class": "class", "Struct": "struct", "Enum": "enum", "Protocol": "protocol"}


def project_id(project_name: str) -> str:
    return f"swift://{project_name}"


def file_id(proj_id: str, rel_path: str) -> str:
    return f"{proj_id}/file/{rel_path.replace(chr(92), '/').lstrip('./')}"


def module_id(proj_id: str, module_name: str) -> str:
    """One SwiftPM module/target -- the scope root for its top-level types/functions, so two targets
    that both declare `Foo` stay distinct. CASE-PRESERVED (it is what `import <Target>` writes)."""
    return f"{proj_id}/module/{module_name}"


def _arity(type_parameters: list[str]) -> str:
    # `Foo` vs `Foo<T,U>` -> distinct ids (a generic and its raw-name twin can't collide).
    return f"`{len(type_parameters)}" if type_parameters else ""


def type_id(scope_id: str, kind: str, name: str, type_parameters: list[str]) -> str:
    """`scope_id` is the containing project_id (top-level) or the parent type_id (nested)."""
    return f"{scope_id}.{_KIND_SLUG[kind]}.{name.lower()}{_arity(type_parameters)}"


def method_id(owner_type_id: str, name: str, param_types: list[str]) -> str:
    signature = ",".join((t or "").lower() for t in param_types)
    return f"{owner_type_id}.method.{name.lower()}({signature})"


def function_id(scope_id: str, name: str, param_types: list[str]) -> str:
    signature = ",".join((t or "").lower() for t in param_types)
    return f"{scope_id}.func.{name.lower()}({signature})"


def property_id(owner_type_id: str, name: str) -> str:
    return f"{owner_type_id}.property.{name.lower()}"


def enum_member_id(enum_type_id: str, name: str) -> str:
    return f"{enum_type_id}.case.{name.lower()}"


def parameter_id(owner_id: str, position: int, name: str) -> str:
    return f"{owner_id}.param.{position}.{name.lower()}"


# -- cross-repo linkage (ADR 0004) -------------------------------------------------------
# Project-INDEPENDENT key for an externally-visible callable/module, stored as the `linkage_key`
# property on real `public`/`open` definitions and as the id of the placeholder that stands in
# for an unresolved call/import. `qualified_name` is what a *consumer* writes to reach the symbol
# -- a `.`-joined path: `Type.method` for a method (a caller has the type in scope and writes
# `x.method()` / `Type.method()`), `<module>.name` for a free function, `<Module>` for an import.
# It is NAME-BASED only (type/module path + name), never parameter types (a consumer can't know
# the callee's param types). So a `Widget.charge` call in repo A and the real `charge` method in
# repo B land on one node when both share a graph. (Distinct from the id scheme above, which is
# project-scoped and embeds param types for local disambiguation.)
def extern_function_key(qualified_name: str) -> str:
    return f"extern://swift/{qualified_name}"


def extern_module_key(module_name: str) -> str:
    return f"extern://swift/module/{module_name}"
