"""Unit tests for the Ruby tree-sitter parser (no FalkorDB). Confirms the empirically-derived
grammar handling: require/require_relative, top-level constants + functions, modules and classes
(superclass, attr_* fields, constants), instance/class/singleton methods (`def self.x`,
`class << self`), the full parameter shape (required/optional/splat/keyword/double-splat/block) and
reconstructed signature, mixins (include/prepend/extend), visibility toggling (`private`,
`private def`), namespaced names (`A::B`), operator/setter/predicate method names, call extraction
of every shape (command / self / constant / instance / chain / super), and leading `#` doc comments."""

from __future__ import annotations

from auryn.ingestors.ruby.file_parser import parse_source


def _parse(src: str, path: str = "t.rb"):
    return parse_source(src.encode(), path)


def test_requires_constants_and_top_level_functions():
    rf = _parse(
        """
        require "set"
        require_relative "../lib/helper"
        MAX = 10
        NAME = "auryn"
        def top_fn(x)
          x
        end
        """
    )
    assert [(r.target, r.kind) for r in rf.requires] == [("set", "require"), ("../lib/helper", "require_relative")]
    consts = {c.name: c.value for c in rf.consts}
    assert consts["MAX"] == "10" and consts["NAME"] == '"auryn"'
    (fn,) = rf.methods
    assert fn.name == "top_fn" and fn.kind == "FUNCTION" and fn.signature == "top_fn(x)"


def test_module_with_consts_instance_and_class_methods():
    rf = _parse(
        """
        module Greetings
          DEFAULT = "hi"
          def wave
            "wave"
          end
          def self.shout(msg)
            msg.upcase
          end
        end
        """
    )
    (m,) = rf.modules
    assert m.kind == "Module" and m.name == "Greetings"
    assert [c.name for c in m.consts] == ["DEFAULT"]
    by_name = {x.name: x for x in m.methods}
    assert by_name["wave"].is_singleton is False and by_name["wave"].kind == "METHOD"
    assert by_name["shout"].is_singleton is True  # `def self.shout`


def test_class_superclass_attrs_and_initialize():
    rf = _parse(
        """
        class Dog < Animal
          attr_accessor :name, :age
          attr_reader :species
          attr_writer :owner
          def initialize(name)
            @name = name
          end
        end
        """
    )
    (c,) = rf.classes
    assert c.kind == "Class" and c.name == "Dog" and c.superclass == "Animal"
    fields = {f.name: f.access for f in c.fields}
    assert fields == {"name": "accessor", "age": "accessor", "species": "reader", "owner": "writer"}
    assert [m.name for m in c.methods] == ["initialize"]


def test_parameter_kinds_and_signature():
    rf = _parse(
        """
        def m(a, b = 1, *rest, kw:, kw2: 2, **opts, &blk)
        end
        """
    )
    (m,) = rf.methods
    kinds = [(p.name, p.kind, p.position) for p in m.parameters]
    assert kinds == [
        ("a", "required", 0),
        ("b", "optional", 1),
        ("rest", "splat", 2),
        ("kw", "keyword", 3),
        ("kw2", "keyword_optional", 4),
        ("opts", "hash_splat", 5),
        ("blk", "block", 6),
    ]
    assert m.parameters[1].default == "1" and m.parameters[4].default == "2"
    assert m.signature == "m(a, b = 1, *rest, kw:, kw2: 2, **opts, &blk)"


def test_mixins_include_prepend_extend():
    rf = _parse(
        """
        class C
          include Enumerable
          prepend Loud
          extend Comparable
        end
        """
    )
    (c,) = rf.classes
    assert [(i.name, i.kind) for i in c.includes] == [
        ("Enumerable", "include"),
        ("Loud", "prepend"),
        ("Comparable", "extend"),
    ]


def test_visibility_toggle_and_private_def():
    rf = _parse(
        """
        class C
          def pub; end
          private
          def sec; end
          public
          def pub2; end
          private def wrapped; end
        end
        """
    )
    (c,) = rf.classes
    vis = {m.name: m.visibility for m in c.methods}
    assert vis["pub"] == "public"
    assert vis["sec"] == "private"
    assert vis["pub2"] == "public"
    assert vis["wrapped"] == "private"  # `private def wrapped`


def test_singleton_class_methods_are_class_methods():
    rf = _parse(
        """
        class C
          class << self
            def built; end
          end
        end
        """
    )
    (c,) = rf.classes
    (m,) = c.methods
    assert m.name == "built" and m.is_singleton is True


def test_namespaced_class_and_module_names():
    rf = _parse(
        """
        module Outer
          class Inner < Base::Thing
          end
        end
        class A::B
        end
        """
    )
    outer = next(m for m in rf.modules if m.name == "Outer")
    (inner,) = outer.classes
    assert inner.name == "Inner" and inner.superclass == "Base::Thing"
    ab = next(c for c in rf.classes if c.name == "A::B")
    assert ab.kind == "Class"


def test_operator_setter_and_predicate_method_names():
    rf = _parse(
        """
        class C
          def ==(o); end
          def <=>(o); end
          def [](k); end
          def valid?; end
          def save!; end
          def name=(v); end
        end
        """
    )
    (c,) = rf.classes
    names = {m.name for m in c.methods}
    assert names == {"==", "<=>", "[]", "valid?", "save!", "name="}


def test_call_extraction_all_shapes():
    rf = _parse(
        """
        class Dog < Animal
          def speak
            bark              # command (bare, receiverless)
            self.wag          # self
            Greetings.shout   # constant receiver
            @store.load       # instance (@ivar receiver)
            other.load        # instance (local receiver)
            a.b.c             # chain (complex receiver)
            super             # super
          end
        end
        """
    )
    (c,) = rf.classes
    (speak,) = c.methods
    calls = {(x.name, x.kind, x.receiver, x.qualifier) for x in speak.calls}
    assert ("bark", "command", None, None) in calls
    assert ("wag", "self", None, None) in calls
    assert ("shout", "constant", None, "Greetings") in calls
    assert ("load", "instance", "@store", None) in calls
    assert ("load", "instance", "other", None) in calls
    assert ("c", "chain", None, None) in calls
    assert ("super", "super", None, None) in calls


def test_bare_command_call_excludes_local_variables():
    # A paren-less bare identifier is a command call UNLESS it is a local assigned in the same body
    # (a local read is syntactically identical to a no-arg call -- Ruby dynamism).
    rf = _parse(
        """
        def m
          total = 1
          compute
          total
        end
        """
    )
    (m,) = rf.methods
    names = {(c.name, c.kind) for c in m.calls}
    assert ("compute", "command") in names
    assert not any(n == "total" for n, _ in names)  # local read, not a call


def test_doc_comments_on_class_module_and_methods():
    rf = _parse(
        """
        # Animal is a base class.
        class Animal
          # speak makes a noise.
          def speak; end
        end

        # A greeter module.
        module Greetings
        end

        # Stray comment.

        def detached; end
        """
    )
    animal = next(c for c in rf.classes if c.name == "Animal")
    assert animal.doc == "Animal is a base class."
    assert animal.methods[0].doc == "speak makes a noise."
    greet = next(m for m in rf.modules if m.name == "Greetings")
    assert greet.doc == "A greeter module."
    # a blank line between comment and def detaches it
    assert next(m for m in rf.methods if m.name == "detached").doc == ""
