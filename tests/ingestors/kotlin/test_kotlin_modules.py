"""Maven/Gradle module tier: a type is scoped by its module (the dir before `src/{main,test}/kotlin/`),
so two modules that both declare `com.acme.util.Helper` stay distinct -- and a reference (type or
top-level function) resolves within its own module, not the other's. Like Java, a Kotlin module is
NOT an import target (a consumer imports by FQCN), so it carries no linkage_key and heals nothing;
cross-repo healing stays FQCN-based. Pure resolution, no FalkorDB."""

from __future__ import annotations

from auryn.ingestors.kotlin import kotlinmod, resolution
from auryn.ingestors.kotlin.file_parser import parse_source
from auryn.ingestors.kotlin.ids import project_id


# -- pure: path-derived module -------------------------------------------------------------

def test_module_for_path():
    assert kotlinmod.module_for_path("modA/src/main/kotlin/com/acme/Helper.kt") == "modA"
    assert kotlinmod.module_for_path("libs/data/src/main/kotlin/app/Store.kt") == "libs/data"  # nested subproject
    assert kotlinmod.module_for_path("svc/src/test/kotlin/app/StoreTest.kt") == "svc"           # test set
    assert kotlinmod.module_for_path("api/src/main/java/app/Api.java") == "api"   # java leaf in a kt module
    assert kotlinmod.module_for_path("src/main/kotlin/app/App.kt") == ""  # root-level layout -> Project-scoped
    assert kotlinmod.module_for_path("App.kt") == ""                      # loose file, no src layout -> root


# -- pure: resolution ----------------------------------------------------------------------

def _resolve(files, project="demo"):
    proj = project_id(project)
    raw = [parse_source(src.encode(), path) for path, src in files.items()]
    items = list(resolution.resolve(raw, proj))
    nodes = [i for i in items if type(i).__name__ == "GraphNode"]
    edges = [i for i in items if type(i).__name__ == "GraphEdge"]
    return nodes, edges


def _pairs(edges, rel):
    return {(e.from_key["id"], e.to_key["id"]) for e in edges if e.rel_type == rel}


_A_HELPER = "kotlin://demo/module/modA/package/com.acme.util.class.helper"
_B_HELPER = "kotlin://demo/module/modB/package/com.acme.util.class.helper"


def test_two_modules_same_type_stay_distinct():
    # The bug: without a module tier, `com.acme.util.Helper` in module A and B collapse to one node.
    nodes, _ = _resolve({
        "modA/src/main/kotlin/com/acme/util/Helper.kt": "package com.acme.util\nclass Helper",
        "modB/src/main/kotlin/com/acme/util/Helper.kt": "package com.acme.util\nclass Helper",
    })
    assert sorted(n.key["id"] for n in nodes if n.label == "Class") == [_A_HELPER, _B_HELPER]


def test_module_tier_and_containment():
    nodes, edges = _resolve({"modA/src/main/kotlin/com/acme/util/Helper.kt":
                             "package com.acme.util\nclass Helper"})
    mod = next(n for n in nodes if n.label == "Module")
    assert mod.key["id"] == "kotlin://demo/module/modA"
    assert mod.properties["name"] == "modA"
    assert "linkage_key" not in mod.properties  # a Kotlin module is not an import target -> no healing key
    assert ("kotlin://demo", "kotlin://demo/module/modA") in _pairs(edges, "HAS_MODULE")
    assert ("kotlin://demo/module/modA", "kotlin://demo/module/modA/package/com") in _pairs(edges, "HAS_PACKAGE")
    assert ("kotlin://demo/module/modA/package/com.acme.util", _A_HELPER) in _pairs(edges, "HAS_TYPE")


def test_type_reference_resolves_within_declaring_module():
    # Both modules declare `com.acme.util.Helper`. modA's Client field type must bind to modA's
    # Helper, not modB's -- the same-module resolution bias.
    _, edges = _resolve({
        "modA/src/main/kotlin/com/acme/util/Helper.kt": "package com.acme.util\nclass Helper",
        "modB/src/main/kotlin/com/acme/util/Helper.kt": "package com.acme.util\nclass Helper",
        "modA/src/main/kotlin/com/acme/app/Client.kt": (
            "package com.acme.app\n"
            "import com.acme.util.Helper\n"
            "class Client {\n"
            "  val helper: Helper? = null\n"
            "}\n"
        ),
    })
    field = "kotlin://demo/module/modA/package/com.acme.app.class.client.field.helper"
    dep = _pairs(edges, "DEPENDS_ON")
    assert (field, _A_HELPER) in dep          # field type -> modA's Helper
    assert (field, _B_HELPER) not in dep


def test_top_level_function_call_resolves_within_module():
    # Two modules each declare a top-level `fun greet()`; a caller in modB binds to modB's greet,
    # not modA's (the free-function module bias -- the callable analog of the type bias).
    _, edges = _resolve({
        "modA/src/main/kotlin/app/a.kt": "package app\nfun greet() {}",
        "modB/src/main/kotlin/app/b.kt": "package app\nfun greet() {}\nfun run() { greet() }",
    })
    a_greet = "kotlin://demo/module/modA/package/app.fn.greet()"
    b_greet = "kotlin://demo/module/modB/package/app.fn.greet()"
    b_run = "kotlin://demo/module/modB/package/app.fn.run()"
    calls = _pairs(edges, "CALLS")
    assert (b_run, b_greet) in calls          # modB's run -> modB's greet
    assert (b_run, a_greet) not in calls


def test_root_file_and_same_named_subproject_stay_distinct():
    # PR #9 review #2: a root-level `src/main/kotlin/` file and a subproject directory literally named
    # after the project must NOT collide. The root file is Project-scoped (no Module node); the
    # subproject is module-scoped -- so their `app.Helper` types get distinct ids.
    nodes, _ = _resolve({
        "src/main/kotlin/app/Helper.kt": "package app\nclass Helper",       # root -> Project-scoped
        "demo/src/main/kotlin/app/Helper.kt": "package app\nclass Helper",   # subproject `demo`
    }, project="demo")
    assert sorted(n.key["id"] for n in nodes if n.label == "Class") == [
        "kotlin://demo/module/demo/package/app.class.helper",  # the subproject
        "kotlin://demo/package/app.class.helper",              # the root file, under the Project
    ]
    assert [n.properties["name"] for n in nodes if n.label == "Module"] == ["demo"]


def test_linkage_key_is_fqcn_not_module_qualified():
    # A public top-level function's cross-repo key is its package-qualified name -- module-INDEPENDENT
    # (a consumer imports by FQCN, never the module name).
    nodes, _ = _resolve({"modA/src/main/kotlin/com/acme/util/u.kt": "package com.acme.util\nfun helper() {}"})
    fn = next(n for n in nodes if n.label == "Function" and n.properties["name"] == "helper")
    assert fn.properties["linkage_key"] == "extern://kotlin/com.acme.util.helper"
