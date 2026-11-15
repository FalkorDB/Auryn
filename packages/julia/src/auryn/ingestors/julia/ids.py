"""Canonical id scheme for the Julia graph. Rooted at `julia://<project_name>`, mirroring
`java://`. Julia has no classes -- it has a module tier over types (structs / abstract /
primitive) and *generic functions* made of concrete *methods* (multiple dispatch) -- so the id
shape is a module/scope tier over types + a two-level function/method keying:

  - one `Function` per (scope, name)                    -> `func.<name>`
  - one `Method` per concrete definition, keyed by its  -> `func.<name>(<param-type-sig>)`
    parameter *type* text (the natural dispatch key).

A future deep engine (Julia type inference) would key nodes by these same functions so its
output stays interchangeable in one graph -- the id-parity the other ingestors rely on.

Ids embed names/positions and, for dispatch disambiguation, parameter *type* text as written
-- never resolved/inferred types (the id-stability rule)."""

from __future__ import annotations

_KIND_SLUG = {"Struct": "struct", "AbstractType": "abstract", "PrimitiveType": "primitive"}


def project_id(project_name: str) -> str:
    return f"julia://{project_name}"


def file_id(proj_id: str, rel_path: str) -> str:
    return f"{proj_id}/file/{rel_path.replace(chr(92), '/').lstrip('./')}"


def module_id(proj_id: str, qualified: str) -> str:
    """`qualified` is the dotted module path, e.g. `Shapes.Inner`."""
    return f"{proj_id}/module/{qualified.lower()}"


def _arity(type_parameters: list[str]) -> str:
    # `Foo` vs `Foo{T,U}` -> distinct ids (a parametric type and its bare-name twin can't collide).
    return f"`{len(type_parameters)}" if type_parameters else ""


def type_id(scope_id: str, kind: str, name: str, type_parameters: list[str]) -> str:
    """`scope_id` is the containing module_id (module-level) or the project_id (top-level, i.e.
    declared outside any `module` block)."""
    return f"{scope_id}.{_KIND_SLUG[kind]}.{name.lower()}{_arity(type_parameters)}"


def function_id(scope_id: str, name: str) -> str:
    """One generic function per (scope, name) -- every `f(...)` definition in the scope, whatever
    it dispatches on, is a Method of this single Function (Julia's real model)."""
    return f"{scope_id}.func.{name.lower()}"


def method_id(func_id: str, param_types: list[str]) -> str:
    """A concrete definition, keyed by its parameter *type* text so the methods of one generic
    function stay distinct (multiple dispatch). `area(::Circle)` and `area(::Square)` differ."""
    signature = ",".join((t or "").lower() for t in param_types)
    return f"{func_id}({signature})"


# -- cross-repo linkage (ADR 0004) -------------------------------------------------------
# Project-INDEPENDENT key for a generic function, stored as the `linkage_key` property on real
# Function definitions and as the id of the placeholder that stands in for an unresolved call.
# NAME-BASED: `qualified_name` is what a *consumer* writes -- a bare simple name (`push!`), a
# module-qualified name (`Base.println`, `MyMod.compute`) -- so a `println` call in repo A and a
# `println` definition exported under the same qualified name in repo B land on one node when both
# share a graph. (Distinct from a func_id, which is project-scoped and dispatch-agnostic.)
def extern_function_key(qualified_name: str) -> str:
    return f"extern://julia/{qualified_name}"


def field_id(struct_id: str, name: str) -> str:
    return f"{struct_id}.field.{name.lower()}"


def parameter_id(owner_id: str, position: int, name: str) -> str:
    return f"{owner_id}.param.{position}.{name.lower()}"
