"""Unit tests for the PHP tree-sitter parser (no FalkorDB). Confirms the empirically-derived
grammar handling: namespace (statement + block form) and `use` imports (incl. aliases), class
heritage (single superclass vs interface list, trait `use`), abstract/final modifiers, typed
properties (visibility/static/readonly + constructor promotion), class constants, methods (return
type, params, variadic), constructors, enums with backed cases, interfaces, traits, top-level
functions, and call/`new` extraction with receivers across the four call node kinds."""

from __future__ import annotations

from auryn.ingestors.php.file_parser import parse_source


def _parse(src: str, path: str = "T.php"):
    return parse_source(src.encode(), path)


def test_namespace_and_uses():
    rf = _parse(
        """<?php
        namespace App\\Models;
        use App\\Contracts\\Repository;
        use App\\Support\\Logger as Log;
        class T {}
        """
    )
    assert rf.namespace == "App\\Models"
    by_name = {u.name: u for u in rf.uses}
    assert by_name["App\\Contracts\\Repository"].alias is None
    assert by_name["App\\Support\\Logger"].alias == "Log"
    (t,) = rf.types
    assert t.namespace == "App\\Models"


def test_block_namespace_form():
    rf = _parse(
        """<?php
        namespace App\\Web {
            class Page {}
        }
        """
    )
    (t,) = rf.types
    assert t.kind == "Class" and t.name == "Page" and t.namespace == "App\\Web"


def test_class_heritage_modifiers_and_trait_use():
    rf = _parse(
        """<?php
        abstract class Widget extends Base implements Named, Comparable {
            use LoggerTrait, Sluggable;
        }
        final class Boxed {}
        """
    )
    by_name = {t.name: t for t in rf.types}
    w = by_name["Widget"]
    assert w.kind == "Class" and w.is_abstract is True and w.is_final is False
    assert w.superclass == "Base"
    assert w.interfaces == ["Named", "Comparable"]
    assert w.traits == ["LoggerTrait", "Sluggable"]
    assert by_name["Boxed"].is_final is True


def test_interface_extends_multiple():
    rf = _parse("<?php interface Named extends Base, Other {}")
    (i,) = rf.types
    assert i.kind == "Interface" and i.superclass is None and i.interfaces == ["Base", "Other"]


def test_properties_visibility_static_readonly_and_promotion():
    rf = _parse(
        """<?php
        class C {
            public int $count = 0;
            private ?Store $store;
            protected static string $shared = 'x';
            public readonly int $ro;
            public function __construct(private Logger $logger, string $name) {}
        }
        """
    )
    (c,) = rf.types
    props = {p.name: p for p in c.properties}
    assert props["count"].type == "int" and props["count"].visibility == "public"
    assert props["store"].type == "?Store" and props["store"].visibility == "private"
    assert props["shared"].is_static is True
    assert props["ro"].is_readonly is True
    # constructor-promoted param becomes a Property too (private $logger), the plain param does not.
    assert props["logger"].is_promoted is True and props["logger"].type == "Logger"
    assert "name" not in props


def test_constants():
    rf = _parse("<?php class C { public const MAX = 10; const NAME = 'x'; }")
    (c,) = rf.types
    consts = {k.name: k for k in c.constants}
    assert set(consts) == {"MAX", "NAME"}
    assert consts["MAX"].value == "10" and consts["MAX"].visibility == "public"


def test_methods_params_variadic_and_constructor():
    rf = _parse(
        """<?php
        class C {
            public function __construct(Store $store) {}
            public function f(Map $m, string ...$rest): array { return []; }
            abstract protected function g(): void;
        }
        """
    )
    (c,) = rf.types
    methods = {m.name: m for m in c.methods}
    assert methods["__construct"].kind == "CONSTRUCTOR" and methods["__construct"].return_type is None
    f = methods["f"]
    assert f.return_type == "array"
    assert [(p.name, p.type, p.is_variadic) for p in f.parameters] == [("m", "Map", False), ("rest", "string", True)]
    assert methods["g"].is_abstract is True and methods["g"].visibility == "protected"


def test_enum_with_backed_cases():
    rf = _parse(
        """<?php
        enum Status: string implements Named {
            case Active = 'active';
            case Inactive = 'inactive';
            public function label(): string { return $this->value; }
        }
        """
    )
    (e,) = rf.types
    assert e.kind == "Enum" and e.backing_type == "string" and e.interfaces == ["Named"]
    assert [c.name for c in e.enum_cases] == ["Active", "Inactive"]
    assert e.enum_cases[0].value == "'active'"
    assert {m.name for m in e.methods} == {"label"}


def test_trait_definition():
    rf = _parse("<?php trait LoggerTrait { protected array $logs = []; public function log(string $m): void {} }")
    (t,) = rf.types
    assert t.kind == "Trait" and t.name == "LoggerTrait"
    assert {m.name for m in t.methods} == {"log"}
    assert {p.name for p in t.properties} == {"logs"}


def test_top_level_function():
    rf = _parse("<?php function helperFn(int $x): int { return $x + 1; }")
    (fn,) = rf.functions
    assert fn.name == "helperFn" and fn.return_type == "int"
    assert [(p.name, p.type) for p in fn.parameters] == [("x", "int")]


def test_call_extraction_kinds_and_receivers():
    rf = _parse(
        """<?php
        class C {
            private Store $store;
            public function m(Widget $w): void {
                helperFn();
                $this->render();
                $this->store->put();
                $w->save();
                Helper::stat();
                self::internal();
                new Widget();
            }
        }
        """
    )
    (c,) = rf.types
    (m,) = c.methods
    calls = {(x.name, x.receiver, x.kind) for x in m.calls}
    assert ("helperFn", None, "function") in calls
    assert ("render", "this", "method") in calls
    assert ("put", "store", "method") in calls           # $this->store->put() -> receiver is property "store"
    assert ("save", "w", "method") in calls              # $w->save() -> receiver is param "w"
    assert ("stat", "Helper", "static") in calls
    assert ("internal", "self", "static") in calls
    assert ("Widget", None, "new") in calls


# -- doc comments (shared `doc` property via core.docstrings.leading_comment) ---------------


def test_function_doc_single_and_stacked():
    rf = _parse(
        """<?php
        // makeStore builds a Store.
        // The returned Store is empty.
        function makeStore() {}
        function undocumented() {}
        """
    )
    by_name = {f.name: f for f in rf.functions}
    assert by_name["makeStore"].doc == "makeStore builds a Store.\nThe returned Store is empty."
    assert by_name["undocumented"].doc == ""


def test_method_doc_phpdoc_and_hash():
    rf = _parse(
        """<?php
        class C {
            /**
             * Put stores a value.
             */
            public function put(string $v): void {}

            # render draws the widget.
            public function render(): void {}

            public function bare(): void {}
        }
        """
    )
    (c,) = rf.types
    methods = {m.name: m for m in c.methods}
    assert methods["put"].doc == "Put stores a value."   # PHPDoc /** */
    assert methods["render"].doc == "render draws the widget."  # `#` line comment
    assert methods["bare"].doc == ""


def test_class_doc_phpdoc():
    rf = _parse(
        """<?php
        /** Store persists widgets. */
        class Store { public int $count = 0; }
        """
    )
    (s,) = rf.types
    assert s.doc == "Store persists widgets."


def test_interface_trait_enum_doc():
    rf = _parse(
        """<?php
        /** Repo reads and writes. */
        interface Repo {}
        // LoggerTrait adds logging.
        trait LoggerTrait {}
        /**
         * Status of a widget.
         */
        enum Status { case Active; }
        """
    )
    by_name = {t.name: t for t in rf.types}
    assert by_name["Repo"].doc == "Repo reads and writes."
    assert by_name["LoggerTrait"].doc == "LoggerTrait adds logging."
    assert by_name["Status"].doc == "Status of a widget."


def test_blank_line_between_comment_and_decl_is_not_doc():
    rf = _parse(
        """<?php
        // This is a stray comment, not attached.

        function loose() {}
        """
    )
    assert next(f for f in rf.functions if f.name == "loose").doc == ""
