"""Call-site lines on CALLS edges and `file_path` on definitions (agent-evidence design §1, §3).

Offline: file_parser + resolution with the ast engine, no FalkorDB."""

from __future__ import annotations

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.python import resolution
from auryn.ingestors.python.file_parser import parse_module

_PROJ = "python://demo"


def _resolve(files: dict[str, str]):
    modules = [parse_module(src.encode(), path) for path, src in sorted(files.items())]
    return list(resolution.resolve(modules, _PROJ, oracle=None))


def _calls(items):
    return [i for i in items if isinstance(i, GraphEdge) and i.rel_type == "CALLS"]


def _calls_from(items, name_fragment):
    return [e for e in _calls(items) if f".{name_fragment}(" in e.from_key["id"]]


def test_repeated_calls_collapse_to_one_edge_with_every_line():
    items = _resolve({"pkg/m.py": "def b():\n    pass\n\n\ndef a():\n    b()\n    x = 1\n    b()\n"})
    edges = [e for e in _calls_from(items, "a") if ".b(" in e.to_key["id"]]
    assert len(edges) == 1
    assert edges[0].properties["lines"] == [6, 8]


def test_same_line_calls_are_deduplicated():
    items = _resolve({"pkg/m.py": "def b():\n    return 1\n\n\ndef a():\n    return b() + b()\n"})
    edges = [e for e in _calls_from(items, "a") if ".b(" in e.to_key["id"]]
    assert len(edges) == 1
    assert edges[0].properties["lines"] == [6]


def test_external_call_edge_carries_lines():
    items = _resolve({"pkg/m.py": "import os\n\n\ndef a():\n    os.getcwd()\n"})
    ext = [e for e in _calls_from(items, "a") if e.to_key["id"].startswith("extern://")]
    assert ext, "expected an external CALLS edge"
    assert ext[0].properties["lines"] == [5]
    assert ext[0].properties["resolution"] == "external"


def test_lines_are_capped_at_twenty():
    body = "".join("    b()\n" for _ in range(30))
    items = _resolve({"pkg/m.py": "def b():\n    pass\n\n\ndef a():\n" + body})
    edge = next(e for e in _calls_from(items, "a") if ".b(" in e.to_key["id"])
    assert len(edge.properties["lines"]) == 20
    assert edge.properties["lines"] == sorted(edge.properties["lines"])


def test_module_scope_calls_carry_lines():
    items = _resolve({"pkg/m.py": "def b():\n    pass\n\n\nb()\n"})
    edges = [e for e in _calls(items) if ".b(" in e.to_key["id"] and e.from_label in ("Module", "Package")]
    assert edges and edges[0].properties["lines"] == [5]


def test_definitions_carry_their_file_path():
    items = _resolve({"pkg/m.py": "class C:\n    def meth(self):\n        pass\n\n\ndef f():\n    pass\n"})
    defs = [n for n in items if isinstance(n, GraphNode) and n.label in ("Class", "Method", "Function")]
    assert {n.label for n in defs} == {"Class", "Method", "Function"}
    assert all(n.properties["file_path"] == "pkg/m.py" for n in defs)
