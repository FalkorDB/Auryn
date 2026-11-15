"""Canonical id scheme for the Rust graph, mirroring the Java/C# ingestors' shape (a
module/namespace tier over types-with-members) so a single graph can hold every language with
interchangeable node ids. A future deep (rust-analyzer / rustc HIR) engine would key nodes by
these same functions, so its output stays a pure delta over the fast pass -- the id-parity the
Java/C#/Python/C ingestors rely on.

Ids embed names/positions and, for disambiguation, parameter *type* text and (for trait-impl
methods) the implemented trait's written name -- never resolved/inferred types (the id-stability
rule). Rust has no overloading, but a type may carry both an inherent `fn area` and a trait
impl's `fn area`; the trait suffix keeps those two Method ids from colliding."""

from __future__ import annotations

_KIND_SLUG = {"Struct": "struct", "Enum": "enum", "Trait": "trait"}


def project_id(project_name: str) -> str:
    return f"rust://{project_name}"


# -- Cargo tier (issue #5) -----------------------------------------------------------------
# A repo can hold many crates (a Cargo workspace / multiple `src/` folders). The crate is the
# per-file SCOPE ROOT -- it replaces the single project root the module/type/function ids used to
# hang off -- so two crates' identically-named items no longer collide. `package_id`/`crate_id`
# embed the package DIRECTORY + target kind/name (all path-derived, so a full ingest and an
# incremental resync compute the identical id with no manifest/source access). `/crate/` marks a
# crate root; a module under it is `{crate_id}/module/...` (so it also contains `/module/`).

def package_id(proj_id: str, package_dir: str) -> str:
    """One Cargo package (one `Cargo.toml`). `package_dir` is its directory ("" = repo root)."""
    return f"{proj_id}/package/{package_dir}" if package_dir else f"{proj_id}/package"


def crate_id(proj_id: str, package_dir: str, kind: str, name: str) -> str:
    """One build target -- a library / binary / test / example / bench crate. The per-file scope
    root. `kind` is a cargo.KIND_* constant; `name` the target's crate name."""
    slug = f"{package_dir}/{kind}.{name}" if package_dir else f"{kind}.{name}"
    return f"{proj_id}/crate/{slug.lower()}"


def module_id(root_id: str, dotted_path: str) -> str:
    """`root_id` is the owning crate id (crate-rooted items) or a parent module id; `dotted_path`
    is the crate-relative module path, e.g. "geometry" or "geometry.shapes"."""
    return f"{root_id}/module/{dotted_path.lower()}"


def file_id(proj_id: str, rel_path: str) -> str:
    return f"{proj_id}/file/{rel_path.replace(chr(92), '/').lstrip('./')}"


def _arity(type_parameters: list[str]) -> str:
    # `Foo` vs `Foo<T,U>` -> distinct ids (a generic and its raw-name twin can't collide).
    return f"`{len(type_parameters)}" if type_parameters else ""


def type_id(scope_id: str, kind: str, name: str, type_parameters: list[str]) -> str:
    """`scope_id` is the containing module_id or the project_id (crate-root item)."""
    return f"{scope_id}.{_KIND_SLUG[kind]}.{name.lower()}{_arity(type_parameters)}"


def function_id(scope_id: str, name: str, param_types: list[str]) -> str:
    signature = ",".join((t or "").lower() for t in param_types)
    return f"{scope_id}.fn.{name.lower()}({signature})"


def method_id(owner_type_id: str, name: str, param_types: list[str], trait_name: str | None = None) -> str:
    signature = ",".join((t or "").lower() for t in param_types)
    base = f"{owner_type_id}.method.{name.lower()}({signature})"
    # A trait-impl method is discriminated by the trait so it can't collide with an inherent one.
    return f"{base}#{trait_name.lower()}" if trait_name else base


def field_id(owner_type_id: str, name: str) -> str:
    return f"{owner_type_id}.field.{name.lower()}"


def variant_id(enum_type_id: str, name: str) -> str:
    return f"{enum_type_id}.variant.{name.lower()}"


def parameter_id(owner_id: str, position: int, name: str) -> str:
    return f"{owner_id}.param.{position}.{name.lower()}"


def const_id(scope_id: str, name: str) -> str:
    return f"{scope_id}.const.{name.lower()}"


# -- cross-repo linkage (ADR 0004) -------------------------------------------------------
# Project-INDEPENDENT key for an externally-visible callable, stored as the `linkage_key`
# property on real `pub` definitions and as the id of the placeholder that stands in for an
# unresolved call. `qualified_name` is what a *consumer* writes to reach the symbol -- a
# `::`-joined path, e.g. `std::mem::swap`, `mycrate::compute`, or `Type::method`. It is
# NAME-BASED only (module/crate/type path + name), never parameter types: a consumer can't
# know the callee's param types. So `mycrate::compute()` called from repo A and the real
# `pub fn compute` in repo B land on one node when both share a graph. (Distinct from the id
# scheme above, which is project-scoped and embeds param types for local disambiguation.)
def extern_function_key(qualified_name: str) -> str:
    return f"extern://rust/{qualified_name}"
