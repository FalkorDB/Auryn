"""Maven/Gradle module tier: a type is scoped by its module (the dir before `src/{main,test}/java/`),
so two modules that both declare `com.acme.util.Helper` stay distinct -- and a reference resolves
within its own module, not the other's. Unlike Go/Swift modules, a Java module is NOT an import
target (a consumer imports by FQCN), so it carries no linkage_key and heals nothing; cross-repo
healing stays FQCN-based (covered by tests/sync/test_delta_cross_repo.py). Pure resolution, no FalkorDB."""

from __future__ import annotations

from auryn.ingestors.java import javamod, resolution
from auryn.ingestors.java.file_parser import parse_source
from auryn.ingestors.java.ids import project_id


# -- pure: path-derived module -------------------------------------------------------------

def test_module_for_path():
    assert javamod.module_for_path("modA/src/main/java/com/acme/Helper.java") == "modA"
    assert javamod.module_for_path("libs/data/src/main/java/app/Store.java") == "libs/data"  # nested subproject
    assert javamod.module_for_path("svc/src/test/java/app/StoreTest.java") == "svc"           # test set
    assert javamod.module_for_path("src/main/java/app/App.java") == ""  # root-level layout -> Project-scoped
    assert javamod.module_for_path("App.java") == ""                    # loose file, no src layout -> root


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


_A_HELPER = "java://demo/module/modA/package/com.acme.util.class.helper"
_B_HELPER = "java://demo/module/modB/package/com.acme.util.class.helper"


def test_two_modules_same_type_stay_distinct():
    # The bug: without a module tier, `com.acme.util.Helper` in module A and B collapse to one node.
    nodes, _ = _resolve({
        "modA/src/main/java/com/acme/util/Helper.java": "package com.acme.util; public class Helper {}",
        "modB/src/main/java/com/acme/util/Helper.java": "package com.acme.util; public class Helper {}",
    })
    assert sorted(n.key["id"] for n in nodes if n.label == "Class") == [_A_HELPER, _B_HELPER]


def test_module_tier_and_containment():
    nodes, edges = _resolve({"modA/src/main/java/com/acme/util/Helper.java":
                             "package com.acme.util; public class Helper {}"})
    mod = next(n for n in nodes if n.label == "Module")
    assert mod.key["id"] == "java://demo/module/modA"
    assert mod.properties["name"] == "modA"
    assert "linkage_key" not in mod.properties  # a Java module is not an import target -> no healing key
    assert ("java://demo", "java://demo/module/modA") in _pairs(edges, "HAS_MODULE")
    # the package prefix chain now roots at the Module, not the Project.
    assert ("java://demo/module/modA", "java://demo/module/modA/package/com") in _pairs(edges, "HAS_PACKAGE")
    assert ("java://demo/module/modA/package/com.acme.util", _A_HELPER) in _pairs(edges, "HAS_TYPE")


def test_reference_resolves_within_declaring_module():
    # Both modules declare `com.acme.util.Helper`. modA's Client (field + call) must bind to modA's
    # Helper, not modB's -- the same-module resolution bias (the Java analog of PR #8 review #2).
    _, edges = _resolve({
        "modA/src/main/java/com/acme/util/Helper.java":
            "package com.acme.util; public class Helper { public int work() { return 1; } }",
        "modB/src/main/java/com/acme/util/Helper.java":
            "package com.acme.util; public class Helper { public int work() { return 2; } }",
        "modA/src/main/java/com/acme/app/Client.java": (
            "package com.acme.app;\n"
            "import com.acme.util.Helper;\n"
            "public class Client {\n"
            "  private final Helper helper;\n"
            "  public Client(Helper h) { this.helper = h; }\n"
            "  public int go() { return helper.work(); }\n"
            "}\n"
        ),
    })
    client = "java://demo/module/modA/package/com.acme.app.class.client"
    dep = _pairs(edges, "DEPENDS_ON")
    assert (client + ".field.helper", _A_HELPER) in dep       # field type -> modA's Helper
    assert (client + ".field.helper", _B_HELPER) not in dep
    calls = _pairs(edges, "CALLS")
    assert (client + ".method.go()", _A_HELPER + ".method.work()") in calls     # helper.work() -> modA's
    assert (client + ".method.go()", _B_HELPER + ".method.work()") not in calls


def test_root_file_and_same_named_subproject_stay_distinct():
    # PR #9 review #2: a root-level `src/main/java/` file and a subproject directory literally named
    # after the project must NOT collide. The root file is Project-scoped (no Module node); the
    # subproject is module-scoped -- so their `app.Helper` types get distinct ids.
    nodes, _ = _resolve({
        "src/main/java/app/Helper.java": "package app; public class Helper {}",       # root -> Project-scoped
        "demo/src/main/java/app/Helper.java": "package app; public class Helper {}",   # subproject `demo`
    }, project="demo")
    assert sorted(n.key["id"] for n in nodes if n.label == "Class") == [
        "java://demo/module/demo/package/app.class.helper",  # the subproject
        "java://demo/package/app.class.helper",              # the root file, under the Project
    ]
    # exactly one Module node is emitted -- the subproject; the root file hangs off the Project.
    assert [n.properties["name"] for n in nodes if n.label == "Module"] == ["demo"]


def test_linkage_key_is_fqcn_not_module_qualified():
    # A public method's cross-repo key is owner-FQCN#member -- module-INDEPENDENT (two modules'
    # `com.acme.util.Helper#work` share a key; a consumer imports by FQCN, never the module name).
    nodes, _ = _resolve({"modA/src/main/java/com/acme/util/Helper.java":
                         "package com.acme.util; public class Helper { public int work() { return 1; } }"})
    work = next(n for n in nodes if n.label == "Method" and n.properties["name"] == "work")
    assert work.properties["linkage_key"] == "extern://java/com.acme.util.Helper#work"
