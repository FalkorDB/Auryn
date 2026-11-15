"""Canonical id scheme for the Ruby graph, mirroring the Rust/Python ingestors' shape (a
namespace tier over classes/modules with members) so a single graph can hold every language with
interchangeable node ids. A future deep (Ruby loader / Sorbet / RBS) engine would key nodes by
these same functions, so its output stays a pure delta over the fast pass.

Ids embed names and parameter *names* (never inferred types -- Ruby has none statically, and the
id-stability rule forbids volatile types in ids). Ruby has no method overloading, but a class
method and an instance method may share a name; the `#singleton` suffix keeps their Method ids
from colliding. Namespaces are keyed by their fully-qualified `::`-path (lowercased, `::`->`.`),
so a class reopened in two files lands on one node."""

from __future__ import annotations


def project_id(project_name: str) -> str:
    return f"ruby://{project_name}"


def _qual_slug(qualified: str) -> str:
    # "A::B::Dog" -> "a.b.dog"; a plain "Dog" -> "dog".
    return ".".join(p for p in qualified.replace("::", ".").split(".") if p).lower()


def class_id(proj_id: str, qualified: str) -> str:
    return f"{proj_id}/class/{_qual_slug(qualified)}"


def module_id(proj_id: str, qualified: str) -> str:
    return f"{proj_id}/module/{_qual_slug(qualified)}"


def file_id(proj_id: str, rel_path: str) -> str:
    return f"{proj_id}/file/{rel_path.replace(chr(92), '/').lstrip('./')}"


def _sig(param_names: list[str]) -> str:
    return ",".join((n or "").lower() for n in param_names)


def function_id(scope_id: str, name: str, param_names: list[str]) -> str:
    return f"{scope_id}.func.{name.lower()}({_sig(param_names)})"


def method_id(owner_id: str, name: str, param_names: list[str], is_singleton: bool = False) -> str:
    base = f"{owner_id}.method.{name.lower()}({_sig(param_names)})"
    return f"{base}#singleton" if is_singleton else base


def parameter_id(owner_id: str, position: int, name: str) -> str:
    return f"{owner_id}.param.{position}.{name.lower()}"


def field_id(owner_id: str, name: str) -> str:
    return f"{owner_id}.field.{name.lower()}"


def const_id(scope_id: str, name: str) -> str:
    return f"{scope_id}.const.{name.lower()}"


# -- cross-repo linkage (ADR 0004) -------------------------------------------------------
# Project-INDEPENDENT key for an externally-visible symbol, in the form a *consumer* writes to
# reach it: `Class#method` (instance), `Class.method` (class/singleton), `Module.method`, or a
# bare `name` for a top-level function. It is BOTH the id of the placeholder standing in for an
# unresolved constant-receiver call AND the `linkage_key` stamped on a real definition, so
# ingesting the defining repo into the same graph heals a placeholder onto its definition.
def extern_symbol_key(qualified_name: str) -> str:
    return f"extern://ruby/{qualified_name}"
