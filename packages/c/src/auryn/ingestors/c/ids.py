"""Canonical id scheme for the C graph.

C's linkage rules drive the ids: an entity with **external linkage** is program-global (the
whole project has at most one `add` function, one `struct Widget` tag), so it's keyed by the
project + name and shared across the header that declares it and the .c that defines it. An
entity with **internal linkage** (`static` functions/globals) or preprocessor scope (macros)
is file-local, so its id embeds the file. The fast (tree-sitter) engine and a future deep
(libclang) engine key nodes by these same functions, so their output is interchangeable in
one graph -- the same id-parity the C#/Python ingestors rely on.

Ids embed names/positions, never resolved types (the id-stability rule)."""

from __future__ import annotations


def _norm(rel_path: str) -> str:
    return rel_path.replace("\\", "/").lstrip("./")


def project_id(project_name: str) -> str:
    return f"c://{project_name}"


def file_id(proj_id: str, rel_path: str) -> str:
    return f"{proj_id}/file/{_norm(rel_path)}"


# -- external linkage / tag namespace: program-global, keyed by name ----------------------
def function_id(proj_id: str, name: str) -> str:
    return f"{proj_id}/func/{name}"


def struct_id(proj_id: str, name: str) -> str:
    return f"{proj_id}/struct/{name}"


def union_id(proj_id: str, name: str) -> str:
    return f"{proj_id}/union/{name}"


def enum_id(proj_id: str, name: str) -> str:
    return f"{proj_id}/enum/{name}"


def typedef_id(proj_id: str, name: str) -> str:
    return f"{proj_id}/typedef/{name}"


def global_id(proj_id: str, name: str) -> str:
    return f"{proj_id}/global/{name}"


# -- internal linkage / preprocessor: file-local -----------------------------------------
def static_function_id(a_file_id: str, name: str) -> str:
    return f"{a_file_id}.func.{name}"


def static_global_id(a_file_id: str, name: str) -> str:
    return f"{a_file_id}.global.{name}"


def macro_id(a_file_id: str, name: str) -> str:
    return f"{a_file_id}.macro.{name}"


# -- members: children of their owner ----------------------------------------------------
def field_id(record_id: str, name: str) -> str:
    return f"{record_id}.field.{name}"


def parameter_id(func_id: str, position: int, name: str) -> str:
    return f"{func_id}.param.{position}.{name}"


# -- cross-repo linkage (ADR 0004) -------------------------------------------------------
# Project-INDEPENDENT key for an external-linkage function, stored as the `linkage_key`
# property on real definitions and as the id of the placeholder that stands in for an
# unresolved call. C external linkage is program-global and flat (no module), so the key is
# just the name -- so `charge` in repo B and a `charge()` call in repo A land on one node when
# both share a graph. (Distinct from the C `linkage` property, which is the internal/external
# linkage *class*.)
def extern_function_key(name: str) -> str:
    return f"extern://c/{name}"
