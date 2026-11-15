"""Canonical id scheme for the TypeScript/JavaScript graph.

In JS/TS a file *is* a module (the ES-module unit that `import` resolves to), so ids are keyed
off the repo-relative file path -- mirroring the Python ingestor's module-keyed scheme, but by
path rather than a dotted name (JS has no dotted-package concept, and `a.ts` vs `a.js` are
distinct modules, so the extension is kept for identity). The fast tree-sitter engine and a
future deep tsserver engine key nodes by these same functions, so their output is
interchangeable in one graph -- the same id-parity the C#/Python/C ingestors rely on.

Ids embed names/positions, never inferred/annotated types (the id-stability rule): a type in
an id would churn the id whenever an annotation changes."""

from __future__ import annotations


def _norm(rel_path: str) -> str:
    return rel_path.replace("\\", "/").lstrip("./")


def project_id(project_name: str) -> str:
    return f"ts://{project_name}"


def module_id(proj_id: str, rel_path: str) -> str:
    """A module == a file. Keyed by the full repo-relative path (extension included), so
    parallel `foo.ts`/`foo.js` are distinct nodes."""
    return f"{proj_id}/module/{_norm(rel_path)}"


# -- top-level (module-scoped) declarations ----------------------------------------------
def class_id(scope_id: str, name: str) -> str:
    """`scope_id` is the containing module_id (top-level) or a parent class_id (nested)."""
    return f"{scope_id}.class.{name}"


def interface_id(mod_id: str, name: str) -> str:
    return f"{mod_id}.iface.{name}"


def enum_id(mod_id: str, name: str) -> str:
    return f"{mod_id}.enum.{name}"


def type_alias_id(mod_id: str, name: str) -> str:
    return f"{mod_id}.type.{name}"


def _sig(param_names: list[str]) -> str:
    return ",".join(param_names)


def function_id(mod_id: str, name: str, param_names: list[str]) -> str:
    """Module-level function (a `function` declaration or a `const f = () => ...` binding).
    Params ride the id (as in Python) so an overload family stays distinguishable."""
    return f"{mod_id}.func.{name}({_sig(param_names)})"


# -- members: children of their owner ----------------------------------------------------
def method_id(owner_id: str, name: str, param_names: list[str]) -> str:
    """`owner_id` is the class_id or interface_id. `name` is "constructor" for the ctor and
    the accessor name for get/set (kind disambiguates on the node, not the id)."""
    return f"{owner_id}.method.{name}({_sig(param_names)})"


def property_id(owner_id: str, name: str) -> str:
    return f"{owner_id}.prop.{name}"


def enum_member_id(an_enum_id: str, name: str) -> str:
    return f"{an_enum_id}.member.{name}"


def parameter_id(owner_id: str, position: int, name: str) -> str:
    return f"{owner_id}.param.{position}.{name}"


# -- cross-repo linkage (ADR 0004) -------------------------------------------------------
# Project-independent key for an externally-visible symbol / imported module. TS module identity
# is a repo-relative path, so symbol keys are module-qualified; a bare import specifier (an npm
# package or another repo) keys a placeholder Module. See ADR 0004 (TS cross-repo *symbol*
# healing is limited until package/tsconfig path mapping is modelled -- imports still surface).
def extern_symbol_key(module_rel: str, name: str) -> str:
    return f"extern://typescript/{module_rel}:{name}"


def extern_module_key(specifier: str) -> str:
    return f"extern://typescript/{specifier}"


# NB: the HTTP endpoint join key (ADR 0006) is language-neutral and lives in core.http_seam
# (endpoint_placeholder_key) so C# and TS callers converge on one node -- not here.
