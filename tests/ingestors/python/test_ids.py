"""`dotted_module_path` must anchor a file at its true import root so the dotted name equals what an
importing module writes -- the cross-repo linkage key (ADR 0004) depends on it. A leading directory
segment that can't appear in an `import` (a hyphenated distribution dir, etc.) is dropped; ordinary
and PEP-420 namespace packages are untouched.
"""

from __future__ import annotations

from auryn.ingestors.python import ids


def test_ordinary_layout_is_unchanged():
    assert ids.dotted_module_path("app/widget.py") == "app.widget"
    assert ids.dotted_module_path("app/__init__.py") == "app"
    assert ids.dotted_module_path("main.py") == "main"
    assert ids.dotted_module_path("a/b/c/mod.py") == "a.b.c.mod"


def test_hyphenated_distribution_dir_is_stripped():
    # The Gap-1 fix: `acme-payments/` is not importable, so it (and anything above it) is above the
    # import root -- the dotted name must match the consumer's `import acme_payments.core`.
    assert ids.dotted_module_path("acme-payments/acme_payments/core.py") == "acme_payments.core"
    assert ids.dotted_module_path("libs/my-lib/my_lib/util.py") == "my_lib.util"


def test_linkage_key_now_matches_consumer_import():
    # The end-to-end point of the fix: definer key == the placeholder an importer mints.
    dotted = ids.dotted_module_path("acme-payments/acme_payments/core.py")
    definer_key = ids.extern_symbol_key(dotted, "charge")
    consumer_key = ids.extern_symbol_key("acme_payments.core", "charge")  # from `import acme_payments.core`
    assert definer_key == consumer_key == "extern://python/acme_payments.core::charge"


def test_namespace_package_identifier_dirs_are_not_stripped():
    # No non-identifier prefix -> nothing removed (PEP-420 namespace or a plain package tree).
    assert ids.dotted_module_path("pkg/sub/mod.py") == "pkg.sub.mod"


def test_import_root_is_below_the_deepest_non_importable_dir():
    # A non-identifier *directory* forces the import root BELOW it -- nothing above `weird-dir/` can
    # be imported, so the only importable name is anchored just under it. Even an identifier dir
    # (`ok/`) above the non-importable one is dropped: `ok.weird-dir.mod` is not a valid import.
    assert ids.dotted_module_path("ok/weird-dir/mod.py") == "mod"
    # Everything up to and including the LAST non-identifier dir is dropped; the tail is kept.
    assert ids.dotted_module_path("build-1/dist-2/pkg/sub/mod.py") == "pkg.sub.mod"
    # The real monorepo shape: an identifier repo dir wrapping a hyphenated distribution dir.
    assert ids.dotted_module_path("myproj/acme-payments/acme_payments/core.py") == "acme_payments.core"


def test_leaf_module_name_is_preserved_even_if_odd():
    # Only directory segments are considered; the module leaf is never dropped.
    assert ids.dotted_module_path("weird-name.py") == "weird-name"


# -- package_dirs-aware detection (Option A): __init__.py finds the true import root -----------


def test_package_dirs_from_paths_collects_init_bearing_dirs():
    paths = ["src/pkg/__init__.py", "src/pkg/mod.py", "src/pkg/sub/__init__.py", "top.py", "__init__.py"]
    assert ids.package_dirs_from_paths(paths) == frozenset({"src/pkg", "src/pkg/sub", ""})


def test_src_layout_root_is_stripped_with_package_context():
    # `src/` has no __init__.py, `src/pkg` does -> `src/` is the import root, not part of the name.
    pkgs = ids.package_dirs_from_paths(["src/pkg/__init__.py", "src/pkg/mod.py"])
    assert ids.dotted_module_path("src/pkg/mod.py", pkgs) == "pkg.mod"
    assert ids.dotted_module_path("src/pkg/__init__.py", pkgs) == "pkg"


def test_identifier_named_wrapper_dir_is_stripped_with_package_context():
    # An ordinary identifier dir (`repo/`) that isn't a package is still above the import root --
    # context-free stripping can't catch this, but __init__.py awareness does.
    pkgs = ids.package_dirs_from_paths(["repo/app/__init__.py", "repo/app/core.py"])
    assert ids.dotted_module_path("repo/app/core.py", pkgs) == "app.core"


def test_nested_packages_keep_their_full_chain():
    pkgs = ids.package_dirs_from_paths(["a/__init__.py", "a/b/__init__.py", "a/b/c.py"])
    assert ids.dotted_module_path("a/b/c.py", pkgs) == "a.b.c"


def test_namespace_package_with_context_is_unchanged():
    # No __init__.py anywhere -> no evidence to strip an identifier dir; behaves like context-free.
    pkgs = ids.package_dirs_from_paths(["pkg/mod.py"])  # empty set
    assert ids.dotted_module_path("pkg/mod.py", pkgs) == "pkg.mod"


def test_clean_layout_identical_with_and_without_context():
    # The invariant that keeps incremental-equivalence and existing golden ids stable: for a clean
    # layout, package_dirs-aware and context-free derivations agree.
    for path in ("app/widget.py", "app/__init__.py", "a/b/c/mod.py", "main.py"):
        pkgs = ids.package_dirs_from_paths(["app/__init__.py", "a/__init__.py", "a/b/__init__.py",
                                            "a/b/c/__init__.py"])
        assert ids.dotted_module_path(path, pkgs) == ids.dotted_module_path(path)
