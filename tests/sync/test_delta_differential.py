"""Differential delta-sync oracle: `apply_delta(full(A), diff A→B) == full(B)`.

The freshness claim ("the graph tracks HEAD") is only worth as much as the incremental
resync is *correct*: a delta bug doesn't throw, it leaves a silently-wrong graph (a stale
node, an orphaned edge, a caller pointed at a symbol that moved) and the agent then answers
confidently wrong. That is the failure mode this file exists to catch.

Rather than enumerate hand-picked before/after states (test_git_commit / test_git_checkout do
that), this drives ONE persistent graph through a *sequence* of commits — forward advances,
backward jumps, long leaps, and repeats — and at every hop asserts the incrementally-synced
graph is node-for-node / edge-for-edge identical to a graph that ingested that commit from
scratch. Because the target state is always recomputed in full and diffed against live state,
the invariant holds in every direction; residue left behind on a *backward* switch is exactly
what a linear "advance only" test misses.

THE DELTA-SYNC GATE. This file (single-repo) + test_delta_cross_repo.py (multi/cross-repo) are the
contract every language that gains delta-sync support MUST pass. A language plugs in a corpus and
inherits the whole battery: the forward walk, the adversarial backward/leap/repeat walk, and the
incremental-path-taken guard — each asserting `incremental == full` node-for-node / edge-for-edge.
`test_delta_languages_are_gated` fails if a language ships an `incremental` engine module but no
corpus here, so support can't be added without coverage.

Every language's corpus MUST exercise the CANONICAL CHANGE-TYPE MATRIX (see `_REQUIRED_CHANGE_TYPES`);
`test_corpus_covers_all_change_types` enforces it. The universal types (all languages):
  add_symbol, remove_symbol, retarget_call (A→B ⇒ A→C), add_file (+ a cross-file ref into it),
  delete_file, rename_file (symbol moves), call_becomes_unresolved (callee removed → placeholder),
  call_newly_resolves (callee added), aggregate_type (a class/struct/record + a member/reference),
  noop (docs-only → empty delta). The adversarial walk adds backward jumps, long leaps and repeats.
Authored-shared languages additionally exercise, in their corpus and cross-repo, the header/impl
(decl/def) split, the multi-definition collision (two `main`), and definition-removed-declaration-
remains; cross-repo bidirectional export/import + demote/upgrade lives in test_delta_cross_repo.py.

Currently gated: python, typescript, bash, c, go.

NOT covered here (by design): commits with *broken* files. Last-known-good keeps the prior
symbols on a parse failure, so delta != full at a broken commit — that invariant lives in
test_broken_code.py. Every commit below is syntactically valid in its language.
"""

from __future__ import annotations

import pytest

from ._helpers import GitRepo, lang_report, python_report, walk_and_assert


# ── corpora ──────────────────────────────────────────────────────────────────────────
def _build_python(repo: GitRepo) -> list[str]:
    """7 valid commits exercising cross-file calls, retarget, add/delete file, rename, class."""
    shas = []

    repo.write({
        "pkg/__init__.py": "",
        "pkg/util.py": "def helper(x):\n    return x + 1\n",
        "pkg/service.py": "from pkg.util import helper\n\n\ndef greet(v):\n    return helper(v)\n",
        "main.py": "from pkg.service import greet\n\n\ndef run():\n    return greet(1)\n",
    })
    shas.append(repo.commit("c1 base: greet->helper, run->greet (cross-file)"))

    # add a second symbol, retarget greet's call helper -> helper2
    repo.write({
        "pkg/util.py": "def helper(x):\n    return x + 1\n\n\ndef helper2(y):\n    return y * 2\n",
        "pkg/service.py": "from pkg.util import helper2\n\n\ndef greet(v):\n    return helper2(v)\n",
    })
    shas.append(repo.commit("c2 add helper2; greet retargets helper->helper2"))

    # add a new file + a new cross-file call into it
    repo.write({
        "pkg/extra.py": "def extra():\n    return 7\n",
        "pkg/service.py": "from pkg.util import helper2\nfrom pkg.extra import extra\n\n\n"
        "def greet(v):\n    return helper2(v) + extra()\n",
    })
    shas.append(repo.commit("c3 add extra.py; greet also calls extra"))

    # delete that file + remove its usage
    repo.rm("pkg/extra.py")
    repo.write({
        "pkg/service.py": "from pkg.util import helper2\n\n\ndef greet(v):\n    return helper2(v)\n",
    })
    shas.append(repo.commit("c4 delete extra.py; greet stops calling extra"))

    # rename util.py -> core.py (every symbol id under it moves) + fix the import
    repo.mv("pkg/util.py", "pkg/core.py")
    repo.write({
        "pkg/service.py": "from pkg.core import helper2\n\n\ndef greet(v):\n    return helper2(v)\n",
    })
    shas.append(repo.commit("c5 rename util.py->core.py; update import"))

    # add a class + method with its own call
    repo.write({
        "pkg/core.py": "def helper(x):\n    return x + 1\n\n\ndef helper2(y):\n    return y * 2\n\n\n"
        "class Widget:\n    def render(self):\n        return helper2(3)\n",
    })
    shas.append(repo.commit("c6 add Widget.render()->helper2"))

    # docs-only: no code change at all -> python delta must be empty
    repo.write({"README.md": "# demo\n"})
    shas.append(repo.commit("c7 docs-only (no python change)"))

    return shas


def _build_bash(repo: GitRepo) -> list[str]:
    """6 valid commits: function add/remove, call retarget, source add/drop, file rename."""
    shas = []

    repo.write({
        "lib.sh": "helper() {\n    echo hi\n}\n",
        "app.sh": "source ./lib.sh\n\nfoo() {\n    bar\n}\n\nbar() {\n    echo b\n}\n",
    })
    shas.append(repo.commit("c1 foo->bar; app sources lib"))

    repo.write({"app.sh": "foo() {\n    baz\n}\n\nbaz() {\n    echo z\n}\n"})
    shas.append(repo.commit("c2 remove bar, add baz; foo->baz; drop source"))

    repo.write({
        "util.sh": "util() {\n    echo u\n}\n",
        "app.sh": "source ./util.sh\n\nfoo() {\n    baz\n    util\n}\n\nbaz() {\n    echo z\n}\n",
    })
    shas.append(repo.commit("c3 add util.sh; foo also calls util; source util"))

    repo.rm("util.sh")
    repo.write({"app.sh": "foo() {\n    baz\n}\n\nbaz() {\n    echo z\n}\n"})
    shas.append(repo.commit("c4 delete util.sh; foo stops calling util"))

    repo.mv("app.sh", "main.sh")
    shas.append(repo.commit("c5 rename app.sh->main.sh"))

    repo.write({"main.sh": "foo() {\n    baz\n    qux\n}\n\nbaz() {\n    echo z\n}\n\nqux() {\n    echo q\n}\n"})
    shas.append(repo.commit("c6 add qux; foo calls qux"))

    repo.write({"README.md": "# demo\n"})  # docs-only: no bash change -> empty delta (noop coverage)
    shas.append(repo.commit("c7 docs-only (no bash change)"))

    return shas


def _build_typescript(repo: GitRepo) -> list[str]:
    """7 valid commits mirroring the python corpus: cross-file import+call, call retarget, add/delete
    file, rename file (path-keyed ids move), a class + method, and a docs-only no-op."""
    shas = []

    repo.write({
        "util.ts": "export function helper(x: number): number {\n  return x + 1;\n}\n",
        "service.ts": 'import { helper } from "./util";\n\nexport function greet(v: number): number {\n  return helper(v);\n}\n',
        "main.ts": 'import { greet } from "./service";\n\nexport function run(): number {\n  return greet(1);\n}\n',
    })
    shas.append(repo.commit("c1 base: run->greet->helper (cross-file import+call)"))

    repo.write({
        "util.ts": "export function helper(x: number): number {\n  return x + 1;\n}\n\n"
        "export function helper2(y: number): number {\n  return y * 2;\n}\n",
        "service.ts": 'import { helper2 } from "./util";\n\nexport function greet(v: number): number {\n  return helper2(v);\n}\n',
    })
    shas.append(repo.commit("c2 add helper2; greet retargets helper->helper2"))

    repo.write({
        "extra.ts": "export function extra(): number {\n  return 7;\n}\n",
        "service.ts": 'import { helper2 } from "./util";\nimport { extra } from "./extra";\n\n'
        "export function greet(v: number): number {\n  return helper2(v) + extra();\n}\n",
    })
    shas.append(repo.commit("c3 add extra.ts; greet also calls extra"))

    repo.rm("extra.ts")
    repo.write({"service.ts": 'import { helper2 } from "./util";\n\nexport function greet(v: number): number {\n  return helper2(v);\n}\n'})
    shas.append(repo.commit("c4 delete extra.ts; greet stops calling extra"))

    repo.mv("util.ts", "core.ts")
    repo.write({"service.ts": 'import { helper2 } from "./core";\n\nexport function greet(v: number): number {\n  return helper2(v);\n}\n'})
    shas.append(repo.commit("c5 rename util.ts->core.ts; update import"))

    repo.write({
        "core.ts": "export function helper(x: number): number {\n  return x + 1;\n}\n\n"
        "export function helper2(y: number): number {\n  return y * 2;\n}\n\n"
        "export class Widget {\n  render(): number {\n    return helper2(3);\n  }\n}\n",
    })
    shas.append(repo.commit("c6 add Widget.render()->helper2"))

    repo.write({"README.md": "# demo\n"})
    shas.append(repo.commit("c7 docs-only (no ts change)"))

    return shas


def _build_c(repo: GitRepo) -> list[str]:
    """7 valid commits exercising C's program-global (authored-shared) model: a function
    declared in a .h and defined in a .c (one node, DECLARES+DEFINES from two files), cross-file
    calls, call retarget, add/delete file, header/impl split, file rename (DEFINES moves — prop
    reduction), and a struct + REFERENCES."""
    shas = []

    repo.write({
        "util.h": "int helper(int x);\n",
        "util.c": '#include "util.h"\nint helper(int x) { return x + 1; }\n',
        "service.c": '#include "util.h"\nint greet(int v) { return helper(v); }\n',
        "main.c": "int greet(int v);\nint run(void) { return greet(1); }\n",
    })
    shas.append(repo.commit("c1 base: run->greet->helper (cross-file, .h/.c split)"))

    # add helper2 (declared in .h, defined in .c); greet retargets helper -> helper2
    repo.write({
        "util.h": "int helper(int x);\nint helper2(int y);\n",
        "util.c": '#include "util.h"\nint helper(int x) { return x + 1; }\n'
        "int helper2(int y) { return y * 2; }\n",
        "service.c": '#include "util.h"\nint greet(int v) { return helper2(v); }\n',
    })
    shas.append(repo.commit("c2 add helper2; greet retargets helper->helper2"))

    # add a new file + a new cross-file call into it
    repo.write({
        "extra.h": "int extra(void);\n",
        "extra.c": '#include "extra.h"\nint extra(void) { return 7; }\n',
        "service.c": '#include "util.h"\n#include "extra.h"\n'
        "int greet(int v) { return helper2(v) + extra(); }\n",
    })
    shas.append(repo.commit("c3 add extra.[ch]; greet also calls extra"))

    # delete that file pair + remove its usage
    repo.rm("extra.c", "extra.h")
    repo.write({"service.c": '#include "util.h"\nint greet(int v) { return helper2(v); }\n'})
    shas.append(repo.commit("c4 delete extra.[ch]; greet stops calling extra"))

    # rename util.c -> core.c: the DEFINES edges for helper/helper2 move to a new file (the
    # program-global Function nodes and their ids are unchanged; file_path prop re-reduces).
    repo.mv("util.c", "core.c")
    shas.append(repo.commit("c5 rename util.c->core.c (DEFINES moves file)"))

    # add a struct + a function that REFERENCES it
    repo.write({
        "types.h": "struct Point { int x; int y; };\n",
        "geom.c": '#include "types.h"\nint area(struct Point p) { return p.x * p.y; }\n',
    })
    shas.append(repo.commit("c6 add struct Point + area(struct Point)"))

    # docs-only: no C change at all -> C delta must be empty
    repo.write({"README.md": "# demo\n"})
    shas.append(repo.commit("c7 docs-only (no C change)"))

    return shas


def _build_go(repo: GitRepo) -> list[str]:
    """13 valid commits exercising Go's authored-shared model with the go.mod/DIR package tier:
    import-path-keyed resolution, a go.mod Module over DIR-keyed Packages, cross-package call
    retarget, add/delete/rename file, a method on a struct, cross-package DEPENDS_ON, TWO
    `package main` files that collide on one `func main` id (order-independent winner + co-definer
    closure), the package LIFECYCLE (a single-file package created then fully removed -> Package GC'd
    via File-[:DEFINES]->Package), and the go.mod tier LIFECYCLE (a require added, a nested module
    added then removed -> Module tier + the full-recompute fallback on a module-path change)."""
    shas = []

    repo.write({
        "go.mod": "module x\n\ngo 1.22\n",
        "util/util.go": "package util\n\nfunc Helper(x int) int {\n\treturn x + 1\n}\n",
        "app.go": 'package main\n\nimport "x/util"\n\nfunc run() int {\n\treturn util.Helper(1)\n}\n\nfunc main() {\n\trun()\n}\n',
        "tool.go": 'package main\n\nimport "x/util"\n\nfunc toolRun() int {\n\treturn util.Helper(2)\n}\n\nfunc main() {\n\ttoolRun()\n}\n',
    })
    shas.append(repo.commit("c1 base: go.mod module x; two package-main files (main collision) call util.Helper"))

    repo.write({
        "util/util.go": "package util\n\nfunc Helper(x int) int {\n\treturn x + 1\n}\n\nfunc Helper2(y int) int {\n\treturn y * 2\n}\n",
        "app.go": 'package main\n\nimport "x/util"\n\nfunc run() int {\n\treturn util.Helper2(1)\n}\n\nfunc main() {\n\trun()\n}\n',
    })
    shas.append(repo.commit("c2 add util.Helper2; app.run retargets Helper->Helper2"))

    repo.write({
        "util/extra.go": "package util\n\nfunc Extra() int {\n\treturn 7\n}\n",
        "app.go": 'package main\n\nimport "x/util"\n\nfunc run() int {\n\treturn util.Helper2(1) + util.Extra()\n}\n\nfunc main() {\n\trun()\n}\n',
    })
    shas.append(repo.commit("c3 add util/extra.go; run also calls util.Extra"))

    repo.rm("util/extra.go")
    repo.write({"app.go": 'package main\n\nimport "x/util"\n\nfunc run() int {\n\treturn util.Helper2(1)\n}\n\nfunc main() {\n\trun()\n}\n'})
    shas.append(repo.commit("c4 delete util/extra.go; run stops calling Extra"))

    repo.mv("util/util.go", "util/core.go")  # Helper/Helper2 ids unchanged (package-keyed); DEFINES moves file
    shas.append(repo.commit("c5 rename util/util.go->util/core.go"))

    repo.write({
        "util/store.go": "package util\n\ntype Store struct {\n\tn int\n}\n\nfunc (s *Store) Inc() int {\n\ts.n = s.n + 1\n\treturn s.n\n}\n",
        "app.go": 'package main\n\nimport "x/util"\n\nfunc run() int {\n\treturn util.Helper2(1)\n}\n\nfunc use(s *util.Store) int {\n\treturn util.Helper2(2)\n}\n\nfunc main() {\n\trun()\n}\n',
    })
    shas.append(repo.commit("c6 add util.Store + method Inc; app.use DEPENDS_ON *util.Store"))

    # package LIFECYCLE: a new single-file package created then fully removed -> its Package node must
    # be reclaimed (the gap a package-scoped aggregation node exposed before File-DEFINES-Package).
    repo.write({"svc/svc.go": "package svc\n\nfunc Do() int {\n\treturn 1\n}\n"})
    shas.append(repo.commit("c7 add package svc (svc/svc.go)"))

    repo.rm("svc/svc.go")  # last (only) file of package svc -> Package svc must be GC'd
    shas.append(repo.commit("c8 delete svc/svc.go (Package svc fully removed)"))

    # go.mod tier LIFECYCLE: a require is added (an external dep -> recorded on the Module); then a
    # SECOND (nested) module is added and removed. Adding/removing a go.mod shifts untouched
    # packages' import paths, so git_sync forces a FULL recompute on those hops -- the oracle checks
    # full == that full, and that the Module tier (go version, external_requires) is reconciled on
    # both the incremental (require-only change) and full-fallback (module add/remove) paths.
    repo.write({
        "go.mod": "module x\n\ngo 1.22\n\nrequire github.com/pkg/errors v0.9.1\n",
        "util/wrap.go": 'package util\n\nimport "github.com/pkg/errors"\n\n'
                        'func Wrap() error {\n\treturn errors.Wrap(nil, "x")\n}\n',
    })
    shas.append(repo.commit("c9 root go.mod requires github.com/pkg/errors; util.Wrap uses it"))

    repo.write({
        "sub/go.mod": "module x/sub\n\ngo 1.22\n",
        "sub/sub.go": "package sub\n\nfunc Sub() int {\n\treturn 2\n}\n",
    })
    shas.append(repo.commit("c10 add nested module x/sub (second go.mod -> full-recompute fallback)"))

    # Remove the module's last .go file but KEEP its go.mod: the manifest-backed Module must SURVIVE
    # (a full ingest still emits it from the go.mod set), while its now-empty Package is GC'd. The
    # go.mod set is unchanged, so NO full fallback -- this exercises the incremental refcount GC of a
    # Package + the Module re-MERGE directly.
    repo.rm("sub/sub.go")
    shas.append(repo.commit("c11 remove sub/sub.go, keep sub/go.mod (Module survives, Package GC'd)"))

    repo.rm("sub/go.mod")
    shas.append(repo.commit("c12 remove sub/go.mod (module fully gone -> full-recompute fallback)"))

    repo.write({"README.md": "# demo\n"})
    shas.append(repo.commit("c13 docs-only (no Go change)"))

    # TYPE MOVES OFF A SURVIVING FILE: a type's DEFINED_IN (type->File) is source-owned by the type, so
    # when the type moves to another file while its OLD file SURVIVES (keeps other defs), the stale
    # DEFINED_IN into the old file must be diffed away. The old file isn't deleted, so the removed-file
    # cleanup can't reach it -- only reading DEFINED_IN back with the type does (regression: spf13/viper
    # moved error types into a new errors.go, leaving 4 stale DEFINED_IN->viper.go edges).
    repo.write({
        "mv/a.go": "package mv\n\ntype Moved struct {\n\tn int\n}\n\nfunc Filler() int {\n\treturn 0\n}\n",
        "mv/b.go": "package mv\n\nfunc Other() int {\n\treturn 1\n}\n",
    })
    shas.append(repo.commit("c14 add package mv: type Moved in a.go, Other in b.go"))

    repo.write({
        "mv/a.go": "package mv\n\nfunc Filler() int {\n\treturn 0\n}\n",  # a.go SURVIVES (keeps Filler)
        "mv/b.go": "package mv\n\ntype Moved struct {\n\tn int\n}\n\nfunc Other() int {\n\treturn 1\n}\n",  # Moved now here
    })
    shas.append(repo.commit("c15 move type Moved a.go->b.go (a.go kept); DEFINED_IN must retarget, no stale edge"))

    return shas


def _build_java(repo: GitRepo) -> list[str]:
    """9 valid commits, class-per-file (Java's file==public-class rule): cross-file static call,
    call retarget, add/delete file, a file+class rename (type id changes -> callers retarget), an
    IMPLEMENTS relationship, and -- the package LIFECYCLE -- a class in a deeper package `app.sub`
    created then removed, so Package `app.sub` must be GC'd while `app` survives (its other files
    remain): the prefix-chain aggregation nodes are provenance-owned (File-[:DEFINES]->Package, ADR
    0005), one edge per declaring file to every prefix."""
    shas = []

    repo.write({
        "Util.java": "package app;\n\npublic class Util {\n    public static int helper(int x) {\n        return x + 1;\n    }\n}\n",
        "Service.java": "package app;\n\npublic class Service {\n    public int greet(int v) {\n        return Util.helper(v);\n    }\n}\n",
        "Main.java": "package app;\n\npublic class Main {\n    public int run() {\n        return new Service().greet(1);\n    }\n}\n",
    })
    shas.append(repo.commit("c1 base: run->greet->Util.helper (cross-file static call)"))

    repo.write({
        "Util.java": "package app;\n\npublic class Util {\n    public static int helper(int x) {\n        return x + 1;\n    }\n\n"
        "    public static int helper2(int y) {\n        return y * 2;\n    }\n}\n",
        "Service.java": "package app;\n\npublic class Service {\n    public int greet(int v) {\n        return Util.helper2(v);\n    }\n}\n",
    })
    shas.append(repo.commit("c2 add Util.helper2; greet retargets helper->helper2"))

    repo.write({
        "Extra.java": "package app;\n\npublic class Extra {\n    public static int extra() {\n        return 7;\n    }\n}\n",
        "Service.java": "package app;\n\npublic class Service {\n    public int greet(int v) {\n        return Util.helper2(v) + Extra.extra();\n    }\n}\n",
    })
    shas.append(repo.commit("c3 add Extra.java; greet also calls Extra.extra"))

    repo.rm("Extra.java")
    repo.write({"Service.java": "package app;\n\npublic class Service {\n    public int greet(int v) {\n        return Util.helper2(v);\n    }\n}\n"})
    shas.append(repo.commit("c4 delete Extra.java; greet stops calling Extra"))

    # rename the file AND the public class (Java couples them): Util -> Core. The type id changes,
    # so this is a type remove + add + caller retarget.
    repo.mv("Util.java", "Core.java")
    repo.write({
        "Core.java": "package app;\n\npublic class Core {\n    public static int helper(int x) {\n        return x + 1;\n    }\n\n"
        "    public static int helper2(int y) {\n        return y * 2;\n    }\n}\n",
        "Service.java": "package app;\n\npublic class Service {\n    public int greet(int v) {\n        return Core.helper2(v);\n    }\n}\n",
    })
    shas.append(repo.commit("c5 rename Util->Core (class+file); greet retargets to Core.helper2"))

    repo.write({
        "Shape.java": "package app;\n\npublic interface Shape {\n    int area();\n}\n",
        "Circle.java": "package app;\n\npublic class Circle implements Shape {\n    public int area() {\n        return 3;\n    }\n}\n",
    })
    shas.append(repo.commit("c6 add interface Shape + Circle implements Shape (IMPLEMENTS)"))

    # package LIFECYCLE: a class in a DEEPER package, then removed -> Package app.sub must be GC'd
    # while app survives (Service/Main/etc. keep it) -- prefix-chain provenance (File-DEFINES-Package).
    repo.write({"Deep.java": "package app.sub;\n\npublic class Deep {\n    public int d() {\n        return 9;\n    }\n}\n"})
    shas.append(repo.commit("c7 add class Deep in package app.sub"))

    repo.rm("Deep.java")  # only file in app.sub -> Package app.sub reclaimed; app kept
    shas.append(repo.commit("c8 delete Deep.java (Package app.sub removed, app kept)"))

    # module LIFECYCLE: a Maven/Gradle SUBPROJECT (its own src/main/java layout) declaring package
    # `app` with a class `Store` -> a distinct, module-scoped Package/Module tier that must not collide
    # with the ROOT files' `app` (which are Project-scoped: the root has no Module node). Added then
    # removed, so Module `libs/data` and its module-scoped Package `app` are GC'd while the root `app`
    # (Service/Main/etc.) survives.
    repo.write({"libs/data/src/main/java/app/Store.java":
                "package app;\n\npublic class Store {\n    public int size() {\n        return 0;\n    }\n}\n"})
    shas.append(repo.commit("c9 add subproject libs/data (src/main/java, package app) -> module scope root"))

    repo.rm("libs/data/src/main/java/app/Store.java")  # last file of libs/data -> Module + its app GC'd
    shas.append(repo.commit("c10 delete libs/data's Store (Module libs/data + its Package app removed)"))

    # SIBLING-MODULE GC (the dot-prefix hazard): two subprojects whose module names are dot-prefixed
    # siblings -- `api` and `api.v2`, both package `lib`. Removing `api`'s last file must reclaim ONLY
    # Module `api` (+ its `lib`), leaving `api.v2` fully intact. A range/subtree delete keyed on
    # `api` + `.` would wrongly sweep `api.v2`'s whole subtree; the reclaim must point-delete by id.
    repo.write({
        "api/src/main/java/lib/A.java": "package lib;\n\npublic class A {\n    public int a() {\n        return 1;\n    }\n}\n",
        "api.v2/src/main/java/lib/B.java": "package lib;\n\npublic class B {\n    public int b() {\n        return 2;\n    }\n}\n",
    })
    shas.append(repo.commit("c11 add sibling modules api + api.v2 (both package lib)"))

    repo.rm("api/src/main/java/lib/A.java")  # last file of module api -> reclaim api ONLY; api.v2 lives
    shas.append(repo.commit("c12 delete api's A.java (Module api GC'd; sibling api.v2 survives)"))

    # CROSS-MODULE DISPLACEMENT: two subprojects share package `svc`. modB's Client EXTENDS `Helper`,
    # which (no local Helper yet) binds cross-module to modA's svc.Helper. Then modB adds its OWN
    # svc.Helper, which a full resolve prefers (same module) -- so Client must RETARGET to modB's. The
    # incremental closure wouldn't revisit Client on its own (it's no one's reverse-dep); the
    # displacement hook pulls it. This is the whole full==incremental point of ADR 0005.
    repo.write({
        "modA/src/main/java/svc/Helper.java": "package svc;\n\npublic class Helper {\n    public int work() {\n        return 1;\n    }\n}\n",
        "modB/src/main/java/svc/Client.java": "package svc;\n\npublic class Client extends Helper {\n}\n",
    })
    shas.append(repo.commit("c13 add modA.svc.Helper + modB.svc.Client extends Helper (binds cross-module)"))

    repo.write({"modB/src/main/java/svc/Helper.java":
                "package svc;\n\npublic class Helper {\n    public int work() {\n        return 9;\n    }\n}\n"})
    shas.append(repo.commit("c14 add modB.svc.Helper -> Client retargets to modB's (displacement)"))

    # CROSS-PACKAGE, SAME-MODULE displacement (PR #9 #2): modQ's `beta.Sub extends Base` binds (no local
    # Base) cross-module to modP's `alpha.Base`. Adding modQ's OWN `gamma.Base` -- a DIFFERENT package
    # but the SAME module as Sub -- makes a full resolve prefer it (same-module bias beats "anywhere"),
    # so Sub must retarget. The displaced reference shares only the SIMPLE name with the added type, not
    # its fully-qualified name, so detection must key on the simple name + resolver precedence.
    repo.write({
        "mp/src/main/java/alpha/Base.java": "package alpha;\n\npublic class Base {\n}\n",
        "mq/src/main/java/beta/Sub.java": "package beta;\n\npublic class Sub extends Base {\n}\n",
    })
    shas.append(repo.commit("c15 add mp.alpha.Base + mq.beta.Sub extends Base (binds cross-module)"))

    repo.write({"mq/src/main/java/gamma/Base.java": "package gamma;\n\npublic class Base {\n}\n"})
    shas.append(repo.commit("c16 add mq.gamma.Base -> Sub retargets same-module cross-package"))

    # ROOT-SCOPED caller displaced by a ROOT add (PR #9 #1): a root-level `app.Leaf` with a FIELD of
    # type `Node` binds (no local Node) to subproject `lib`'s `tree.Node`. Adding a ROOT `tree.Node`
    # makes a full resolve prefer it (both root -> same-module bias), so Leaf's field DEPENDS_ON must
    # retarget. A root-scoped caller has no Module node, so a module-keyed lookup would miss it. Using a
    # field (not `extends`) also exercises the DEPENDS_ON path, whose source is a Field under a Type.
    repo.write({
        "lib/src/main/java/tree/Node.java": "package tree;\n\npublic class Node {\n}\n",
        "Leaf.java": "package app;\n\npublic class Leaf {\n    Node n;\n}\n",
    })
    shas.append(repo.commit("c17 add lib.tree.Node + root app.Leaf with field Node n (binds to subproject)"))

    repo.write({"tree/Node.java": "package tree;\n\npublic class Node {\n}\n"})  # ROOT tree.Node
    shas.append(repo.commit("c18 add root tree.Node -> root Leaf retargets to it (root displacement)"))

    # CONSTRUCTOR-ONLY caller displaced (PR #9 follow-up #1): modR's `app.Maker` only does `new Widget()`
    # -- Widget appears in no field/param/extends, so the caller has a CALLS to Widget's CONSTRUCTOR and
    # NO DEPENDS_ON. It binds cross-module to modS's `box.Widget`. Adding modR's OWN `gui.Widget` (same
    # module) makes a full resolve prefer its constructor, so Maker must retarget. Detection must follow
    # the constructor CALLS (through the ctor's owning type), not just type-reference edges.
    repo.write({
        "ms/src/main/java/box/Widget.java": "package box;\n\npublic class Widget {\n    public Widget(int n) {\n    }\n}\n",
        "mr/src/main/java/app/Maker.java": "package app;\n\npublic class Maker {\n    public void make() {\n        new Widget(1);\n    }\n}\n",
    })
    shas.append(repo.commit("c19 add ms.box.Widget + mr.app.Maker (new Widget() -> ctor, binds cross-module)"))

    repo.write({"mr/src/main/java/gui/Widget.java": "package gui;\n\npublic class Widget {\n    public Widget(int n) {\n    }\n}\n"})
    shas.append(repo.commit("c20 add mr.gui.Widget -> Maker's new Widget() retargets same-module (ctor displacement)"))

    repo.write({"README.md": "# demo\n"})
    shas.append(repo.commit("c21 docs-only (no java change)"))

    # TYPE MOVES OFF A SURVIVING FILE (DEFINED_IN is source-owned by the type; see the Go corpus note).
    # Java couples a PUBLIC class to its file, but a package-private top-level class can move between files
    # whose public class survives. `Moved` (package-private) moves MvHolder.java->MvOther.java; both files
    # keep their public class, so neither is deleted -- the stale DEFINED_IN->MvHolder.java must be diffed off.
    repo.write({
        "MvHolder.java": "package app;\n\npublic class MvHolder {\n    public int h() {\n        return 0;\n    }\n}\n\nclass Moved {\n    int n;\n}\n",
        "MvOther.java": "package app;\n\npublic class MvOther {\n    public int o() {\n        return 1;\n    }\n}\n",
    })
    shas.append(repo.commit("c22 add MvHolder (public + package-private Moved) + MvOther"))

    repo.write({
        "MvHolder.java": "package app;\n\npublic class MvHolder {\n    public int h() {\n        return 0;\n    }\n}\n",  # survives
        "MvOther.java": "package app;\n\npublic class MvOther {\n    public int o() {\n        return 1;\n    }\n}\n\nclass Moved {\n    int n;\n}\n",
    })
    shas.append(repo.commit("c23 move class Moved MvHolder.java->MvOther.java (both kept); DEFINED_IN retargets"))

    return shas


def _build_rust(repo: GitRepo) -> list[str]:
    """17 valid commits exercising Rust's authored-shared model with a hierarchical MODULE tier:
    crate-root free functions (now crate-scoped ids), an inline `mod geo` (Module tier + a Struct/
    Field + a Trait + `impl Shape for Point` -> IMPLEMENTS + a supertrait -> EXTENDS + a Method),
    cross-file calls, call retarget, add/delete file, a crate-root file rename (DEFINES moves; ids
    stable), an enum + DEPENDS_ON, TWO crate-root `fn main` files that collide on one `main` id
    (order-independent winner + co-definer closure), the module LIFECYCLE -- a `mod feature_*`
    created, RENAMED and DELETED -- and (issue #5) the CARGO tier lifecycle: a Cargo workspace +
    a second crate are added, a crate->crate dependency is added then dropped, and the crate is
    removed. Module/Crate/Package are all provenance-owned aggregation nodes (File-[:DEFINES]->,
    ADR 0005), so a renamed/deleted one must be reclaimed exactly like a full recompute; the Cargo
    tier (Package props + DEPENDS_ON) is reconciled deterministically from the manifests, so it too
    must stay incremental == full at every hop."""
    shas = []

    shapes = (
        "pub mod geo {\n"
        "    pub struct Point {\n        pub x: i32,\n    }\n\n"
        "    pub trait Shape {\n        fn area(&self) -> i32;\n    }\n\n"
        "    pub trait Colored: Shape {}\n\n"
        "    impl Shape for Point {\n        fn area(&self) -> i32 {\n            self.x\n        }\n    }\n"
        "}\n"
    )
    repo.write({
        "util.rs": "pub fn helper(x: i32) -> i32 {\n    x + 1\n}\n",
        "app.rs": "fn run() -> i32 {\n    helper(1)\n}\n\nfn main() {\n    run();\n}\n",
        "tool.rs": "fn tool_run() -> i32 {\n    helper(2)\n}\n\nfn main() {\n    tool_run();\n}\n",
        "shapes.rs": shapes,
    })
    shas.append(repo.commit("c1 base: two crate-root fn main (collision) call helper; mod geo (IMPLEMENTS/EXTENDS)"))

    # add helper2 (crate root); app.run retargets helper -> helper2 (tool still calls helper)
    repo.write({
        "util.rs": "pub fn helper(x: i32) -> i32 {\n    x + 1\n}\n\npub fn helper2(y: i32) -> i32 {\n    y * 2\n}\n",
        "app.rs": "fn run() -> i32 {\n    helper2(1)\n}\n\nfn main() {\n    run();\n}\n",
    })
    shas.append(repo.commit("c2 add helper2; app.run retargets helper->helper2"))

    # add a new file + a new cross-file call into it
    repo.write({
        "extra.rs": "pub fn extra() -> i32 {\n    7\n}\n",
        "app.rs": "fn run() -> i32 {\n    helper2(1) + extra()\n}\n\nfn main() {\n    run();\n}\n",
    })
    shas.append(repo.commit("c3 add extra.rs; app.run also calls extra"))

    # delete that file + remove its usage
    repo.rm("extra.rs")
    repo.write({"app.rs": "fn run() -> i32 {\n    helper2(1)\n}\n\nfn main() {\n    run();\n}\n"})
    shas.append(repo.commit("c4 delete extra.rs; app.run stops calling extra"))

    # rename util.rs -> core.rs: helper/helper2 ids unchanged (crate-root scope); DEFINES moves file
    repo.mv("util.rs", "core.rs")
    shas.append(repo.commit("c5 rename util.rs->core.rs (DEFINES moves file)"))

    # add an enum (aggregate type) + a function returning it (DEPENDS_ON)
    repo.write({
        "colors.rs": "pub enum Color {\n    Red,\n    Green,\n}\n\npub fn pick() -> Color {\n    Color::Red\n}\n",
    })
    shas.append(repo.commit("c6 add enum Color + pick()->Color (DEPENDS_ON)"))

    # module LIFECYCLE (the aggregation tier is provenance-owned now): create an inline module.
    repo.write({"feature.rs": "pub mod feature_a {\n    pub fn fa() -> i32 {\n        1\n    }\n}\n"})
    shas.append(repo.commit("c7 add inline mod feature_a"))

    # RENAME the module: old Module feature_a (+ its fn fa, whose id is scoped by it) must be GC'd and
    # feature_b created -- the exact rename/delete gap a real Rust repo exposed before File-DEFINES->Module.
    repo.write({"feature.rs": "pub mod feature_b {\n    pub fn fb() -> i32 {\n        2\n    }\n}\n"})
    shas.append(repo.commit("c8 rename mod feature_a -> feature_b (old Module GC'd)"))

    # DELETE the module file entirely -> Module feature_b + its fn fully removed.
    repo.rm("feature.rs")
    shas.append(repo.commit("c9 delete feature.rs (Module fully removed)"))

    # docs-only: no Rust change at all -> Rust delta must be empty
    repo.write({"README.md": "# demo\n"})
    shas.append(repo.commit("c10 docs-only (no Rust change)"))

    # -- Cargo tier lifecycle (issue #5) ---------------------------------------------------
    # Introduce a workspace: the repo root becomes package `demo`, plus a second crate `helperlib`
    # under crates/ with its own src/lib.rs (a new Package + Crate + Module/fn). The root .rs files
    # stay in the root crate; both crates now carry reconciled edition/version.
    repo.write({
        "Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n\n'
                      '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n',
        "crates/helperlib/Cargo.toml": '[package]\nname = "helperlib"\nversion = "0.2.0"\nedition = "2018"\n',
        "crates/helperlib/src/lib.rs": "pub mod util {\n    pub fn lib_fn() -> i32 {\n        42\n    }\n}\n",
    })
    shas.append(repo.commit("c11 add Cargo workspace + crate helperlib (Package/Crate tier)"))

    # Add a crate->crate dependency: Package demo DEPENDS_ON Package helperlib (reconciled from the
    # manifest, not from any .rs file -- the delta path must still pick it up).
    repo.write({
        "Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n\n'
                      '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n\n'
                      '[dependencies]\nhelperlib = { path = "crates/helperlib" }\n',
    })
    shas.append(repo.commit("c12 demo depends on helperlib (Package DEPENDS_ON Package)"))

    # Drop the dependency -> the DEPENDS_ON edge must be pruned (manifest-only change, no .rs diff).
    repo.write({
        "Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n\n'
                      '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n',
    })
    shas.append(repo.commit("c13 drop the helperlib dependency (DEPENDS_ON pruned)"))

    # Remove the crate entirely -> its Package/Crate/Module/fn are refcount-GC'd like a full recompute.
    repo.rm("crates/helperlib/src/lib.rs")
    repo.rm("crates/helperlib/Cargo.toml")
    shas.append(repo.commit("c14 remove crate helperlib (Package/Crate GC'd)"))

    # -- methodless cross-file impl, orphan in a THIRD file (PR #13 Copilot finding) --------------
    # `Base`'s method lives in shape_impl.rs (cross-file). That file ALSO declares `Widget`. `Widget`'s
    # ONLY contribution is a METHODLESS `impl Draw for Widget {}` that lives in shape_orphan.rs -- a file
    # that defines NEITHER Widget (shape_impl.rs) NOR the trait Draw (shape_traits.rs). So no closure can
    # reach shape_orphan.rs from Widget: it has no method node, and the edge's endpoints are defined
    # elsewhere. Editing Base's file (c16) pulls shape_impl.rs via the co-member closure (Base's method),
    # which re-authors Widget; the read-back then sees Widget's IMPLEMENTS edge. Only the `_src_file` stamp
    # (= shape_orphan.rs, outside the emit set) lets the read-back keep it in place instead of deleting it
    # (incremental != full). c15 adds all four together (no trigger); c16 touches ONLY Base.
    repo.write({
        "shape_base.rs": "pub struct Base {\n    pub v: i32,\n}\n",
        "shape_impl.rs": "impl Base {\n    pub fn val(&self) -> i32 {\n        self.v\n    }\n}\n\npub struct Widget;\n",
        "shape_traits.rs": "pub trait Draw {}\n",
        "shape_orphan.rs": "impl Draw for Widget {}\n",
    })
    shas.append(repo.commit("c15 add Base (+cross-file method), Widget, trait Draw, orphan impl in a THIRD file"))

    # touch ONLY Base's declaration -> co-member pulls shape_impl.rs (Base's method) -> Widget re-authored
    # -> its IMPLEMENTS edge (authored only by the unpulled shape_orphan.rs) must be preserved.
    repo.write({"shape_base.rs": "pub struct Base {\n    pub v: i32,\n    pub w: i32,\n}\n"})
    shas.append(repo.commit("c16 touch Base -> transitively re-author Widget; its orphan IMPLEMENTS survives"))

    # DELETE the orphan impl file: Widget (its source type) isn't otherwise touched and the methodless
    # impl leaves no node, so the DETACH DELETE of shape_orphan.rs can't reach the IMPLEMENTS edge --
    # `_delete_authored_edges` must drop it by its `_src_file` stamp so incremental == full (edge gone).
    repo.rm("shape_orphan.rs")
    shas.append(repo.commit("c17 delete shape_orphan.rs -> Widget's orphan IMPLEMENTS edge is GC'd"))

    # TYPE MOVES OFF A SURVIVING FILE (DEFINED_IN is source-owned by the type; see the Go corpus note).
    # Moving a struct to another file while its old file survives must retarget DEFINED_IN with no stale edge.
    repo.write({
        "mv_a.rs": "pub struct Moved {\n    pub n: i32,\n}\n\npub fn filler() -> i32 {\n    0\n}\n",
        "mv_b.rs": "pub fn other() -> i32 {\n    1\n}\n",
    })
    shas.append(repo.commit("c18 add Moved in mv_a.rs, other in mv_b.rs"))

    repo.write({
        "mv_a.rs": "pub fn filler() -> i32 {\n    0\n}\n",  # mv_a.rs SURVIVES (keeps filler)
        "mv_b.rs": "pub struct Moved {\n    pub n: i32,\n}\n\npub fn other() -> i32 {\n    1\n}\n",  # Moved now here
    })
    shas.append(repo.commit("c19 move struct Moved mv_a.rs->mv_b.rs (mv_a kept); DEFINED_IN retargets, no stale edge"))

    # PROJECT-MODULE guard across the seed/emit split: an unresolved `mod_seg::fn()` whose leading segment
    # names a PROJECT MODULE is an intra-project dangling ref the full path DROPS (no placeholder). The
    # incremental must recognize that module even when it's declared in a NON-emit file -- else it
    # materializes a stale `extern://rust/mod_seg::fn` the full recompute never creates (clap's `mod str` +
    # a reparsed file calling `str::from_utf8`). modhost.rs declares `mod widgets`; caller.rs calls a
    # nonexistent `widgets::missing()`. Editing ONLY caller.rs later keeps modhost.rs in the seed.
    repo.write({
        "modhost.rs": "pub mod widgets {\n    pub fn make() -> i32 {\n        0\n    }\n}\n",
        "caller.rs": "pub fn use_it() -> i32 {\n    widgets::missing()\n}\n",
    })
    shas.append(repo.commit("c20 add mod widgets (modhost.rs) + caller.rs calling unresolved widgets::missing()"))

    repo.write({"caller.rs": "pub fn use_it() -> i32 {\n    widgets::missing() + 1\n}\n"})  # edit caller ONLY; modhost stays seed
    shas.append(repo.commit("c21 edit caller.rs only -> widgets (seed module) guard must drop the call, no stale extern"))

    return shas


def _build_cpp(repo: GitRepo) -> list[str]:
    """8 valid commits exercising C++'s authored-shared model -- the structural union of C and Java PLUS
    the feature no other authored language has: OUT-OF-LINE method definitions (a method declared in a
    class body in a .hpp, defined `Class::method(){..}` in a separate .cc, so one method node is
    co-authored by two files). Headers use `.hpp` so the C++ ingestor OWNS them (plain `.h` is the C
    ingestor's on this branch), which is what actually exercises the class/method/EXTENDS machinery.

    Key hops: EDIT ONLY the .cc of an existing method (reverse member-owner closure pulls the .hpp);
    ADD A NEW .cc defining a method the .hpp only DECLARED, header untouched (the new-file member-owner
    pull -- graph provenance alone can't find the owner, so a parse-driven pull brings in the owner type's
    whole contributor set); RENAME the .cc (out-of-line DEFINES moves file); plus namespace tier, inline
    method, free-function proto/def split, cross-file EXTENDS, add/delete file, and a docs-only no-op."""
    shas = []

    repo.write({
        "geo.hpp": (
            "#pragma once\n"
            "namespace geo {\n"
            "class Shape {\n"
            "public:\n"
            "  double area() const;\n"            # declared in-class, defined out-of-line in geo.cc
            "  void render() const;\n"            # DECLARATION-ONLY until c6 adds its def in a NEW .cc
            "  int sides() const { return 4; }\n"  # inline-defined in the header
            "};\n"
            "double scale(double x);\n"            # free-function prototype
            "}\n"
        ),
        "geo.cc": (
            '#include "geo.hpp"\n'
            "namespace geo {\n"
            "double Shape::area() const { return scale(3.14); }\n"  # out-of-line def; calls free scale
            "double scale(double x) { return x * 2.0; }\n"          # free-function definition
            "}\n"
        ),
        "main.cc": (
            '#include "geo.hpp"\n'
            "int run() {\n"
            "  geo::Shape s;\n"
            "  return (int) s.area();\n"           # receiver call -> Shape::area
            "}\n"
        ),
    })
    shas.append(repo.commit("c1 base: namespace, out-of-line method, free fn, decl-only render(), cross-file calls"))

    # EDIT ONLY geo.cc: area() now also calls a brand-new free function clamp(). The reverse member-owner
    # closure MUST pull geo.hpp so Shape's node + sides() + the HAS_METHOD(Shape->area) edge survive and
    # the method merges its in-class declaration -- without it, area's containment edge would be diffed away.
    repo.write({
        "geo.cc": (
            '#include "geo.hpp"\n'
            "namespace geo {\n"
            "double clamp(double x) { return x < 0.0 ? 0.0 : x; }\n"
            "double Shape::area() const { return clamp(scale(3.14)); }\n"
            "double scale(double x) { return x * 2.0; }\n"
            "}\n"
        ),
    })
    shas.append(repo.commit("c2 edit ONLY geo.cc: area calls new free clamp() (reverse closure pulls geo.hpp)"))

    # add Shape::perimeter -- declared in geo.hpp, defined out-of-line in geo.cc (add_symbol across .hpp/.cc).
    repo.write({
        "geo.hpp": (
            "#pragma once\n"
            "namespace geo {\n"
            "class Shape {\n"
            "public:\n"
            "  double area() const;\n"
            "  double perimeter() const;\n"
            "  void render() const;\n"
            "  int sides() const { return 4; }\n"
            "};\n"
            "double scale(double x);\n"
            "}\n"
        ),
        "geo.cc": (
            '#include "geo.hpp"\n'
            "namespace geo {\n"
            "double clamp(double x) { return x < 0.0 ? 0.0 : x; }\n"
            "double Shape::area() const { return clamp(scale(3.14)); }\n"
            "double Shape::perimeter() const { return 4.0 * scale(1.0); }\n"
            "double scale(double x) { return x * 2.0; }\n"
            "}\n"
        ),
    })
    shas.append(repo.commit("c3 add Shape::perimeter (decl in .hpp + out-of-line def in .cc)"))

    # add a new file pair: class Circle : public Shape (cross-file EXTENDS) + its own out-of-line method.
    repo.write({
        "circle.hpp": (
            "#pragma once\n"
            '#include "geo.hpp"\n'
            "namespace geo {\n"
            "class Circle : public Shape {\n"
            "public:\n"
            "  double radius() const;\n"
            "};\n"
            "}\n"
        ),
        "circle.cc": (
            '#include "circle.hpp"\n'
            "namespace geo {\n"
            "double Circle::radius() const { return scale(1.0); }\n"
            "}\n"
        ),
        "main.cc": (
            '#include "geo.hpp"\n'
            '#include "circle.hpp"\n'
            "int run() {\n"
            "  geo::Shape s;\n"
            "  geo::Circle c;\n"
            "  return (int) (s.area() + c.radius());\n"
            "}\n"
        ),
    })
    shas.append(repo.commit("c4 add Circle : Shape (cross-file EXTENDS + new file pair)"))

    # delete the Circle file pair; main stops using it (delete_file + removed nodes/edges).
    repo.rm("circle.hpp", "circle.cc")
    repo.write({
        "main.cc": (
            '#include "geo.hpp"\n'
            "int run() {\n"
            "  geo::Shape s;\n"
            "  return (int) s.area();\n"
            "}\n"
        ),
    })
    shas.append(repo.commit("c5 delete circle.[hpp/cc]; main stops using Circle"))

    # ADD A NEW .cc defining Shape::render() -- declared in geo.hpp since c1, never defined until now, and
    # geo.hpp is UNCHANGED this commit. The owner type lives in a file with no fresh git change, so only a
    # parse-driven member-owner pull (not graph provenance of the new .cc) can bring geo.hpp into the emit
    # set to merge render's body. Without it, render stays declaration-only -> incremental != full.
    repo.write({
        "render.cc": (
            '#include "geo.hpp"\n'
            "namespace geo {\n"
            "void Shape::render() const { (void) scale(2.0); }\n"
            "}\n"
        ),
    })
    shas.append(repo.commit("c6 add render.cc: first out-of-line def for an existing-declared method (header untouched)"))

    # rename geo.cc -> geo_impl.cc: every out-of-line method/free-function DEFINES edge moves to a new file
    # (the program-global node ids are unchanged; the closures pull geo.hpp so each method re-merges).
    repo.mv("geo.cc", "geo_impl.cc")
    shas.append(repo.commit("c7 rename geo.cc->geo_impl.cc (out-of-line DEFINES moves file)"))

    # docs-only: no C++ change at all -> C++ delta must be empty.
    repo.write({"README.md": "# demo\n"})
    shas.append(repo.commit("c8 docs-only (no C++ change)"))

    return shas


_CORPORA = {
    "python": (_build_python, "diff_py"),
    "typescript": (_build_typescript, "diff_ts"),
    "bash": (_build_bash, "diff_bash"),
    "c": (_build_c, "diff_c"),
    "cpp": (_build_cpp, "diff_cpp"),
    "go": (_build_go, "diff_go"),
    "java": (_build_java, "diff_java"),
    "rust": (_build_rust, "diff_rust"),
}


@pytest.fixture(params=sorted(_CORPORA))
def corpus(request, tmp_path):
    lang = request.param
    builder, project = _CORPORA[lang]
    repo = GitRepo.init(tmp_path / "repo")
    shas = builder(repo)
    return lang, repo, shas, tmp_path / "cache", project


# ── the oracle, three ways ─────────────────────────────────────────────────────────────
def test_forward_walk_matches_full(corpus, falkordb_connection):
    """Advance c1→c2→…→cN; every hop's delta must equal a full ingest at that commit AND the corpus
    must exercise every delta SHAPE. The equality oracle alone can pass *vacuously* on a lazy corpus
    (e.g. one that never removes a node); these assertions make the gate meaningful -- a corpus that
    doesn't add, remove, retarget, and no-op isn't comprehensive enough to certify a language."""
    lang, repo, shas, cache, project = corpus
    reports = walk_and_assert(falkordb_connection, repo, cache, shas, project=project, label=f"{lang} forward")

    hops = [lang_report(r, lang) for r in reports[1:]]  # skip the cold c1 (full) ingest
    results = [h.result for h in hops if h is not None and h.result is not None]
    assert results, f"{lang}: language produced no per-hop result"
    assert any(r.nodes_added for r in results), f"{lang}: no hop ADDED a node (add symbol / add file)"
    assert any(r.nodes_removed for r in results), f"{lang}: no hop REMOVED a node (remove symbol / delete file)"
    assert any(r.edges_added for r in results), f"{lang}: no hop ADDED an edge (new / newly-resolved call)"
    assert any(r.edges_removed for r in results), f"{lang}: no hop REMOVED an edge (retarget / removed call)"
    # (The docs-only NO-OP hop is validated directly by walk_and_assert's equality check at that
    # commit -- its "empty" delta still re-stamps the Project node's commit_sha, so it isn't an
    # all-zero *stat*, hence no separate stat assertion for it.)
    # The delta path was actually taken (a cached file == it wasn't reparsed). At minimum the
    # docs-only hop caches every file; a real delta hop caches all-but-the-blast-radius.
    assert any(r.files_cached for r in results), f"{lang}: no hop cached a file -- delta path not exercised"


def test_backward_and_leap_walk_matches_full(corpus, falkordb_connection):
    """Backward jumps, long leaps and repeats — where stale-residue bugs hide. Full-ingest
    oracles are cached per commit inside walk_and_assert, so revisits are cheap and the walk
    can be long: 11 transitions over 6 distinct commits, including big leaps (c1→c6, c6→c2,
    c6→c4), a rename boundary in both directions (…→c5→…), and repeated commits."""
    lang, repo, shas, cache, project = corpus
    # indices bounded by 5 so the same walk is valid across all corpora (all now have ≥7 commits).
    idx = [0, 2, 0, 5, 1, 5, 3, 4, 1, 5, 0]  # c1,c3,c1,c6,c2,c6,c4,c5,c2,c6,c1
    refs = [shas[i] for i in idx]
    walk_and_assert(falkordb_connection, repo, cache, refs, project=project, label=f"{lang} adversarial")


def test_delta_languages_are_gated():
    """The gate's teeth: any registered language that ships an `incremental` engine module (i.e. has
    delta-sync support) MUST have a corpus here. Fails loudly if delta support is added to a language
    without adding it to this gate -- so coverage can never silently lag the feature."""
    import importlib.util

    from auryn.core.ingest import discover_git_languages

    gated = set(_CORPORA)
    delta_capable = []
    for name in discover_git_languages():
        try:
            has_incremental = importlib.util.find_spec(f"auryn.ingestors.{name}.incremental") is not None
        except (ImportError, ValueError):
            has_incremental = False
        if has_incremental:
            delta_capable.append(name)
    ungated = sorted(set(delta_capable) - gated)
    assert not ungated, (
        f"languages with an incremental engine but NO delta-sync gate corpus: {ungated}. "
        f"Add a _build_<lang> corpus (covering the full change-type matrix) to _CORPORA."
    )
    # Sanity: the known-gated set is actually present (guards a typo/regression in _CORPORA).
    assert {"python", "typescript", "bash", "c", "go"} <= gated
