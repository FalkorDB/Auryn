"""Canonical id scheme for the PHP graph, mirroring the Java/C# ingestors' (a
namespace/package tier over types-with-members, plus free functions like Python/C). A future
deep (full name-resolution) engine would key nodes by these same functions, so its output stays
interchangeable in one graph -- the id-parity the other ingestors rely on.

Ids embed names/positions and, for cross-ingestor consistency, parameter *type* text in callable
signatures -- never resolved/inferred types (the id-stability rule). PHP has no method overloading
(one name per class), so the signature is effectively redundant here, but it is kept so a PHP
callable id has the same shape as Java's/C's.

PHP namespaces are backslash-separated (`App\\Models`); the namespace id segment normalizes the
separator to `.` and lowercases (as Java does for dotted packages), so ids never carry a
backslash -- the authentic `App\\Models` form is preserved in the node's `qualified_name`."""

from __future__ import annotations

_KIND_SLUG = {"Class": "class", "Interface": "interface", "Trait": "trait", "Enum": "enum"}


def project_id(project_name: str) -> str:
    return f"php://{project_name}"


def namespace_id(proj_id: str, backslashed: str) -> str:
    return f"{proj_id}/namespace/{backslashed.replace(chr(92), '.').lower()}"


def file_id(proj_id: str, rel_path: str) -> str:
    return f"{proj_id}/file/{rel_path.replace(chr(92), '/').lstrip('./')}"


def type_id(scope_id: str, kind: str, name: str) -> str:
    """`scope_id` is the containing namespace_id or project_id (types with no namespace)."""
    return f"{scope_id}.{_KIND_SLUG[kind]}.{name.lower()}"


def _signature(param_types: list[str]) -> str:
    return ",".join((t or "").lower() for t in param_types)


def function_id(scope_id: str, name: str, param_types: list[str]) -> str:
    return f"{scope_id}.function.{name.lower()}({_signature(param_types)})"


def method_id(owner_type_id: str, name: str, param_types: list[str]) -> str:
    return f"{owner_type_id}.method.{name.lower()}({_signature(param_types)})"


def property_id(owner_type_id: str, name: str) -> str:
    return f"{owner_type_id}.property.{name.lower()}"


def constant_id(owner_type_id: str, name: str) -> str:
    return f"{owner_type_id}.const.{name.lower()}"


def parameter_id(owner_id: str, position: int, name: str) -> str:
    return f"{owner_id}.param.{position}.{name.lower()}"


def enum_case_id(enum_type_id: str, name: str) -> str:
    return f"{enum_type_id}.case.{name.lower()}"


# -- cross-repo linkage (ADR 0004) -------------------------------------------------------
# Project-INDEPENDENT key for an externally-visible callable: stamped as the `linkage_key`
# property on every real Function/Method and used as the id of the placeholder that stands in
# for an unresolved call. NAME-BASED -- never param types (a consumer can't know a callee's param
# names). `qualified_name` is what a *consumer* writes: `App\\Billing::charge` (a method),
# `App\\helper` (a namespaced free function), or `strlen` (a global builtin). So `charge` defined
# in repo B and a `charge()` call in repo A land on one node when both share a graph. (Distinct
# from a node's project-scoped `id`, which keeps two repos' same-named locals from colliding.)
def extern_function_key(qualified_name: str) -> str:
    return f"extern://php/{qualified_name}"
