"""Cargo awareness for the Rust ingestor (issue #5): a repo can hold many crates (a Cargo
workspace with several `src/` folders), and two crates may define the same module path + function
name. Without crate awareness those collapse onto one node. This module supplies the two pieces
that fix that and model the "runtime building blocks" a query can ask about:

1. **Path-derived crate/target identity** -- `crate_target_for_path`. A pure function of the file
   path alone (the standard Cargo `src/` layout): no manifest read, no source access. That matters
   because the incremental delta-sync engine's per-file callbacks get only a path (no source), so a
   path-only rule is computed identically in a full ingest and an incremental resync -- the
   precondition for `incremental == full` (the differential oracle). It is what scopes a file's
   items under its crate, so `api::util::parse` and `core::util::parse` stop colliding.

2. **Manifest parsing** -- `parse_manifest`. Reads a `Cargo.toml` (edition, version, `[dependencies]`,
   `[workspace]` members, target overrides). The manifest is the ONLY source of the rich tier
   (editions, versions, crate-to-crate deps); because those facts aren't in any `.rs` file, they are
   applied by a deterministic reconcile in git_sync (which has source), NOT by the delta callbacks.

The crate/target the id layer uses is deliberately layout-derived (not manifest-derived): a crate
is keyed by its package directory + target kind/name, all read off the path. Manifest data only
*enriches* the already-identified crate (canonical name, edition) and adds dependency edges.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field

# Target kinds -- the "runtime building blocks". A package (one Cargo.toml) produces one library
# crate and/or one or more binary crates, plus integration tests / examples / benches.
KIND_LIB = "lib"
KIND_BIN = "bin"
KIND_TEST = "test"
KIND_EXAMPLE = "example"
KIND_BENCH = "bench"
KIND_BUILD = "build-script"

# Directory (relative to a package root) that holds each non-library target kind.
_TARGET_DIRS = {"bin": KIND_BIN, "tests": KIND_TEST, "examples": KIND_EXAMPLE, "benches": KIND_BENCH}


def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _stem(filename: str) -> str:
    return filename[:-3] if filename.endswith(".rs") else filename


@dataclass(frozen=True)
class CrateTarget:
    """The crate a `.rs` file compiles into, identified purely from its path. `package_dir` is the
    directory holding the owning `Cargo.toml` (""/repo-root for a single-crate repo); `kind` is one
    of the KIND_* constants; `name` is the target's crate name (the default library/binary takes the
    package directory's basename, which equals the Cargo package name in the overwhelmingly common
    case -- the manifest reconcile overrides it with the declared name when they differ)."""

    package_dir: str
    kind: str
    name: str


def crate_target_for_path(path: str) -> CrateTarget:
    """Map a `.rs` file path to its crate/target, from the standard Cargo layout alone:

        <pkg>/src/lib.rs              -> (pkg, lib,     <pkg-basename>)
        <pkg>/src/main.rs             -> (pkg, bin,     <pkg-basename>)      # the default binary
        <pkg>/src/**/*.rs             -> (pkg, lib,     <pkg-basename>)      # library modules
        <pkg>/src/bin/tool.rs         -> (pkg, bin,     tool)
        <pkg>/src/bin/tool/*.rs       -> (pkg, bin,     tool)
        <pkg>/tests|examples|benches/x.rs -> (pkg, test|example|bench, x)
        <pkg>/build.rs                -> (pkg, build-script, build)

    `pkg` is everything before the first `src/` (or `tests|examples|benches/`, or a top-level
    `build.rs`). A repo without a `Cargo.toml`/`src` layout still resolves (the file's directory
    becomes a lib crate) so ingestion never fails on an unconventional tree -- it just doesn't split.
    """
    p = _norm(path)
    parts = p.split("/")

    # Non-src target dirs (tests/examples/benches) and the default library/binary under src/.
    for i, seg in enumerate(parts[:-1]):
        if seg == "src":
            pkg = "/".join(parts[:i])
            rest = parts[i + 1:]
            return _target_under_src(pkg, rest)
        if seg in _TARGET_DIRS:
            pkg = "/".join(parts[:i])
            return CrateTarget(pkg, _TARGET_DIRS[seg], _stem(parts[i + 1]))

    # A top-level build script.
    if parts[-1] == "build.rs":
        return CrateTarget("/".join(parts[:-1]), KIND_BUILD, "build")

    # No recognizable layout: the file's own directory is a (degenerate) lib crate -- keeps distinct
    # directories distinct without inventing structure.
    pkg = "/".join(parts[:-1])
    return CrateTarget(pkg, KIND_LIB, _basename(pkg))


def _target_under_src(pkg: str, rest: list[str]) -> CrateTarget:
    default_name = _basename(pkg)
    if rest[:1] == ["bin"] and len(rest) >= 2:
        return CrateTarget(pkg, KIND_BIN, _stem(rest[1]))  # src/bin/tool.rs or src/bin/tool/*.rs
    if rest == ["main.rs"]:
        return CrateTarget(pkg, KIND_BIN, default_name)     # the package's default binary
    return CrateTarget(pkg, KIND_LIB, default_name)          # lib.rs and every other library module


def _basename(dir_path: str) -> str:
    """The crate name a default lib/bin takes: the package directory's basename, normalized the way
    Cargo normalizes a crate name (hyphens become underscores). Empty (repo root) -> "crate"."""
    base = dir_path.rsplit("/", 1)[-1] if dir_path else ""
    return base.replace("-", "_") if base else "crate"


@dataclass
class CargoDependency:
    name: str           # the dependency crate name as written in [dependencies]
    req: str            # version requirement or "" for a path/workspace dep
    path: str | None    # relative path for a `{ path = ... }` dep, else None
    kind: str           # "normal" | "dev" | "build"


@dataclass
class PackageManifest:
    """The parsed facts a `Cargo.toml` adds on top of the path-derived crate identity. `dir` is the
    manifest's directory (""/repo-root for a top-level manifest)."""

    dir: str
    name: str | None = None            # [package].name (None for a virtual workspace-root manifest)
    version: str | None = None
    edition: str | None = None
    is_workspace_root: bool = False
    workspace_members: list[str] = field(default_factory=list)  # globs, as written
    # [workspace.package] inheritable defaults (a member with `edition.workspace = true` inherits them).
    ws_package_edition: str | None = None
    ws_package_version: str | None = None
    dependencies: list[CargoDependency] = field(default_factory=list)
    # Explicit [lib]/[[bin]] name overrides, if any (path overrides are not honored -- the layout
    # heuristic covers the standard tree; a custom `path=` target degrades to the default mapping).
    lib_name: str | None = None
    bin_names: list[str] = field(default_factory=list)


def parse_manifest(dir_path: str, data: bytes) -> PackageManifest:
    """Parse one `Cargo.toml`. Never raises on a malformed manifest -- returns a best-effort
    PackageManifest (a parse error yields an empty one) so a single bad file can't fail ingestion."""
    m = PackageManifest(dir=_norm(dir_path))
    try:
        doc = tomllib.loads(data.decode("utf-8", "replace"))
    except (tomllib.TOMLDecodeError, ValueError):
        return m

    pkg = doc.get("package")
    if isinstance(pkg, dict):
        m.name = _as_str(pkg.get("name"))
        m.version = _as_str(pkg.get("version"))
        # edition can be `edition = "2021"` or `edition.workspace = true` (inherited) -- capture the
        # literal form; inheritance is resolved by the reconcile against the workspace root.
        m.edition = _as_str(pkg.get("edition"))

    ws = doc.get("workspace")
    if isinstance(ws, dict):
        m.is_workspace_root = True
        members = ws.get("members")
        if isinstance(members, list):
            m.workspace_members = [s for s in members if isinstance(s, str)]
        wspkg = ws.get("package")
        if isinstance(wspkg, dict):
            m.ws_package_edition = _as_str(wspkg.get("edition"))
            m.ws_package_version = _as_str(wspkg.get("version"))

    for table, kind in (("dependencies", "normal"), ("dev-dependencies", "dev"),
                        ("build-dependencies", "build")):
        deps = doc.get(table)
        if isinstance(deps, dict):
            for name, spec in deps.items():
                m.dependencies.append(_dependency(name, spec, kind))

    lib = doc.get("lib")
    if isinstance(lib, dict):
        m.lib_name = _as_str(lib.get("name"))
    bins = doc.get("bin")
    if isinstance(bins, list):
        m.bin_names = [n for b in bins if isinstance(b, dict) for n in (_as_str(b.get("name")),) if n]
    return m


def _dependency(name: str, spec: object, kind: str) -> CargoDependency:
    if isinstance(spec, str):
        return CargoDependency(name=name, req=spec, path=None, kind=kind)
    if isinstance(spec, dict):
        return CargoDependency(
            name=_as_str(spec.get("package")) or name,  # `foo = { package = "real" }` renames
            req=_as_str(spec.get("version")) or "",
            path=_as_str(spec.get("path")),
            kind=kind,
        )
    return CargoDependency(name=name, req="", path=None, kind=kind)


def _as_str(v: object) -> str | None:
    return v if isinstance(v, str) else None


# -- manifest model (the rich tier git_sync's reconcile applies) ---------------------------

@dataclass
class CargoPackage:
    dir: str
    name: str
    version: str | None
    edition: str | None
    is_workspace_root: bool
    external_dependencies: list[str]  # dep names that do NOT resolve to an in-repo package (sorted)


@dataclass
class CargoModel:
    """The manifest-derived facts the reconcile layers onto the path-derived graph: per-package
    metadata, and the intra-repo crate dependency graph (as package-directory pairs -- git_sync maps
    them to Package node ids). External (crates.io) deps are recorded per-package as names, not edges
    (the depended-on crate isn't in this graph)."""

    packages: list[CargoPackage]
    depends_on: list[tuple[str, str]]  # (dependent_dir, dependency_dir), intra-repo, deduped+sorted


def build_cargo_model(manifests: list[PackageManifest]) -> CargoModel:
    """Turn parsed manifests into the reconcile model. Resolves `[workspace.package]` edition/version
    inheritance, and classifies each runtime dependency as intra-repo (a path dep, or a name that
    matches a workspace member's package name -> a DEPENDS_ON edge) or external (-> a name on the
    package). Dev/build deps are excluded -- DEPENDS_ON models the runtime building blocks."""
    ws_edition = next((m.ws_package_edition for m in manifests if m.ws_package_edition), None)
    ws_version = next((m.ws_package_version for m in manifests if m.ws_package_version), None)
    dir_by_name = {m.name: m.dir for m in manifests if m.name}

    packages: list[CargoPackage] = []
    depends_on: set[tuple[str, str]] = set()
    for m in manifests:
        if m.name is None:
            continue  # a virtual workspace-root manifest (no [package]) contributes no Package node
        external: list[str] = []
        for d in m.dependencies:
            if d.kind != "normal":
                continue
            target = _dependency_dir(m.dir, d, dir_by_name)
            if target is None:
                external.append(d.name)
            elif target != m.dir:  # ignore a pathological self-dep
                depends_on.add((m.dir, target))
        packages.append(CargoPackage(
            dir=m.dir, name=m.name, version=m.version or ws_version,
            edition=m.edition or ws_edition, is_workspace_root=m.is_workspace_root,
            external_dependencies=sorted(set(external)),
        ))
    return CargoModel(packages=packages, depends_on=sorted(depends_on))


def _dependency_dir(from_dir: str, dep: CargoDependency, dir_by_name: dict[str, str]) -> str | None:
    """The in-repo package directory a dependency points at, or None if it's external. A `path` dep
    is resolved relative to the depending manifest's directory; otherwise a name match against a
    workspace member's package name wins (the common `foo = { workspace = true }` / member case)."""
    if dep.path is not None:
        return _join_rel(from_dir, dep.path)
    return dir_by_name.get(dep.name)


def _join_rel(base_dir: str, rel: str) -> str:
    """Normalize `base_dir/rel` with `.`/`..` resolved, without touching the filesystem -- so a
    `{ path = "../core" }` dep resolves to the same repo-relative directory a full and an incremental
    sync both see."""
    parts = [p for p in base_dir.split("/") if p] if base_dir else []
    # NB: split on the raw path -- do NOT run it through _norm(), whose lstrip("./") would eat a
    # leading ".." (the very segment a `path = "../core"` dep needs to climb out on).
    for seg in rel.replace("\\", "/").split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
        else:
            parts.append(seg)
    return "/".join(parts)
