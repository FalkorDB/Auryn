"""Pure-Python unit tests for resolution.py's phase-2 logic -- synthetic RawFile
trees fed directly in, no parsing and no FalkorDB involved. Covers EXTENDS/IMPLEMENTS
resolution (including the naming-convention fallback for external bases),
controller/endpoint detection, DTO classification (both the definitive and
heuristic paths), overload-safe method ids, and receiver-aware call resolution.
"""

from __future__ import annotations

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.csharp import resolution
from auryn.ingestors.csharp.raw_model import (
    RawAttribute,
    RawCall,
    RawField,
    RawFile,
    RawMethod,
    RawParameter,
    RawType,
)

PROJECT_ID = "csharp://demo"


def _resolve(raw_files, dto_name_suffixes=("Dto", "Request", "Response", "ViewModel")):
    return list(resolution.resolve(raw_files, PROJECT_ID, dto_name_suffixes))


def _nodes(items, label=None):
    return [i for i in items if isinstance(i, GraphNode) and (label is None or i.label == label)]


def _edges(items, rel_type=None):
    return [i for i in items if isinstance(i, GraphEdge) and (rel_type is None or i.rel_type == rel_type)]


def test_implements_resolves_against_an_indexed_interface():
    iface = RawType(kind="Interface", name="IRepo", namespace="Acme")
    impl = RawType(kind="Class", name="Repo", namespace="Acme", base_list=["IRepo"])
    items = _resolve([RawFile(path="a.cs", types=[iface, impl])])

    implements = _edges(items, "IMPLEMENTS")
    assert len(implements) == 1
    assert implements[0].from_label == "Class"
    assert implements[0].to_label == "Interface"


def test_extends_resolves_against_an_indexed_class():
    base = RawType(kind="Class", name="Base", namespace="Acme")
    derived = RawType(kind="Class", name="Derived", namespace="Acme", base_list=["Base"])
    items = _resolve([RawFile(path="a.cs", types=[base, derived])])

    extends = _edges(items, "EXTENDS")
    assert len(extends) == 1
    assert extends[0].from_label == "Class" and extends[0].to_label == "Class"


def test_unresolved_base_falls_back_to_naming_convention_and_emits_no_edge():
    # ControllerBase/IDisposable are never indexed (external/BCL) -- classification
    # still has to happen (for controller-like detection), but since there's no node
    # of ours to point to, no EXTENDS/IMPLEMENTS edge is emitted for either.
    widget = RawType(kind="Class", name="Widget", namespace="Acme", base_list=["ControllerBase", "IDisposable"])
    items = _resolve([RawFile(path="a.cs", types=[widget])])

    assert _edges(items, "EXTENDS") == []
    assert _edges(items, "IMPLEMENTS") == []


def test_interface_base_list_is_always_extends_even_when_name_says_otherwise():
    parent = RawType(kind="Interface", name="IParent", namespace="Acme")
    child = RawType(kind="Interface", name="IChild", namespace="Acme", base_list=["IParent"])
    items = _resolve([RawFile(path="a.cs", types=[parent, child])])

    assert len(_edges(items, "EXTENDS")) == 1
    assert _edges(items, "IMPLEMENTS") == []


def test_controller_like_transitive_through_an_intermediate_own_class():
    base_controller = RawType(kind="Class", name="BaseApiController", namespace="Acme", base_list=["ControllerBase"])
    widgets = RawType(
        kind="Class",
        name="WidgetsController",
        namespace="Acme",
        base_list=["BaseApiController"],
        methods=[
            RawMethod(
                name="Get",
                kind="ORDINARY",
                accessibility="public",
                return_type="int",
                # An attribute route (rather than a bare [HttpGet]) so a declared endpoint is emitted
                # -- a bare, convention-routed action is now left to the stitch (see _emit_endpoints).
                attributes=[RawAttribute(name="HttpGet", arguments=["widgets"])],
            )
        ],
    )
    items = _resolve([RawFile(path="a.cs", types=[base_controller, widgets])])

    endpoints = _nodes(items, "APIEndpoint")
    assert len(endpoints) == 1
    assert endpoints[0].properties["http_method"] == "GET"


def test_endpoint_route_combines_class_and_method_and_substitutes_controller_token():
    controller = RawType(
        kind="Class",
        name="WidgetsController",
        namespace="Acme",
        base_list=["ControllerBase"],
        attributes=[RawAttribute(name="Route", arguments=["api/[controller]"])],
        methods=[
            RawMethod(
                name="GetById",
                kind="ORDINARY",
                accessibility="public",
                return_type="int",
                attributes=[RawAttribute(name="HttpGet", arguments=["{id}"])],
            )
        ],
    )
    items = _resolve([RawFile(path="a.cs", types=[controller])])

    endpoint = _nodes(items, "APIEndpoint")[0]
    # Route is normalized (lowercased, leading slash) so a caller's `/api/widgets/5` keys to the
    # same node the declaration does (ADR 0006 dedup); the `{id}` template param is preserved.
    assert endpoint.properties["route"] == "/api/widgets/{id}"
    assert endpoint.key["id"] == "extern://http/GET:/api/widgets/{id}"


def test_dto_classified_definitively_by_endpoint_usage_even_without_suffix():
    controller = RawType(
        kind="Class",
        name="WidgetsController",
        namespace="Acme",
        base_list=["ControllerBase"],
        methods=[
            RawMethod(
                name="GetById",
                kind="ORDINARY",
                accessibility="public",
                return_type="Widget",  # no "Dto"/"Request"/... suffix
                parameters=[RawParameter(name="id", type="int", direction="VALUE", position=0, has_default=False)],
                attributes=[RawAttribute(name="HttpGet")],
            )
        ],
    )
    widget = RawType(kind="Class", name="Widget", namespace="Acme")
    items = _resolve([RawFile(path="a.cs", types=[controller, widget])])

    dto_nodes = _nodes(items, "DTO")
    assert [n.properties["name"] for n in dto_nodes] == ["Widget"]
    assert _nodes(items, "Class")  # the controller itself stays a Class, not reclassified


def test_dto_classified_heuristically_by_name_suffix():
    request = RawType(kind="Class", name="CreateWidgetRequest", namespace="Acme")
    plain = RawType(kind="Class", name="Widget", namespace="Acme")
    items = _resolve([RawFile(path="a.cs", types=[request, plain])])

    dto_names = {n.properties["name"] for n in _nodes(items, "DTO")}
    assert dto_names == {"CreateWidgetRequest"}
    assert {n.properties["name"] for n in _nodes(items, "Class")} == {"Widget"}


def test_record_dto_keeps_is_record_flag():
    request = RawType(kind="Record", name="CreateWidgetRequest", namespace="Acme")
    items = _resolve([RawFile(path="a.cs", types=[request])])

    dto = _nodes(items, "DTO")[0]
    assert dto.properties["is_record"] is True


def test_overloaded_methods_get_distinct_signature_based_ids():
    widget = RawType(
        kind="Class",
        name="Widget",
        namespace="Acme",
        methods=[
            RawMethod(name="Notify", kind="ORDINARY", accessibility="public", return_type="void", parameters=[
                RawParameter(name="name", type="string", direction="VALUE", position=0, has_default=False)
            ]),
            RawMethod(name="Notify", kind="ORDINARY", accessibility="public", return_type="void", parameters=[
                RawParameter(name="id", type="int", direction="VALUE", position=0, has_default=False)
            ]),
        ],
    )
    items = _resolve([RawFile(path="a.cs", types=[widget])])

    method_ids = {n.key["id"] for n in _nodes(items, "Method")}
    assert len(method_ids) == 2


def test_call_with_field_receiver_resolves_against_the_fields_declared_type_not_same_name_self():
    service_iface = RawType(
        kind="Interface",
        name="IWidgetService",
        namespace="Acme",
        methods=[RawMethod(name="GetById", kind="ORDINARY", accessibility="public", return_type="int")],
    )
    controller = RawType(
        kind="Class",
        name="WidgetsController",
        namespace="Acme",
        fields=[RawField(name="_service", accessibility="private", type="IWidgetService")],
        methods=[
            RawMethod(
                name="GetById",  # deliberately same name as the field's own method
                kind="ORDINARY",
                accessibility="public",
                return_type="int",
                calls=[RawCall(name="GetById", receiver="_service")],
            )
        ],
    )
    items = _resolve([RawFile(path="a.cs", types=[service_iface, controller])])

    call_edges = _edges(items, "CALLS")
    assert len(call_edges) == 1
    assert call_edges[0].to_label == "Method"
    assert "iwidgetservice" in call_edges[0].to_key["id"]
    assert call_edges[0].from_key["id"] != call_edges[0].to_key["id"]  # not a spurious self-loop


def test_bare_recursive_call_resolves_to_self():
    widget = RawType(
        kind="Class",
        name="Widget",
        namespace="Acme",
        methods=[
            RawMethod(
                name="Factorial",
                kind="ORDINARY",
                accessibility="public",
                return_type="int",
                calls=[RawCall(name="Factorial", receiver=None)],
            )
        ],
    )
    items = _resolve([RawFile(path="a.cs", types=[widget])])

    call_edges = _edges(items, "CALLS")
    assert len(call_edges) == 1
    assert call_edges[0].from_key["id"] == call_edges[0].to_key["id"]


def test_nested_type_gets_has_nested_type_edge_from_its_parent():
    outer = RawType(kind="Class", name="Outer", namespace="Acme")
    inner = RawType(kind="Class", name="Inner", namespace="Acme")
    outer.nested_types.append(inner)
    items = _resolve([RawFile(path="a.cs", types=[outer])])

    nested_edges = _edges(items, "HAS_NESTED_TYPE")
    assert len(nested_edges) == 1
    assert nested_edges[0].from_label == "Class" and nested_edges[0].to_label == "Class"


def test_namespace_nesting_produces_one_node_per_segment():
    widget = RawType(kind="Class", name="Widget", namespace="Acme.Widgets.Sub")
    items = _resolve([RawFile(path="a.cs", types=[widget])])

    namespace_names = {n.properties["name"] for n in _nodes(items, "Namespace")}
    assert namespace_names == {"Acme", "Widgets", "Sub"}


# -- ADR 0004: cross-repo linkage keys + unresolved-call placeholders ----------------


def _placeholders(items):
    return [n for n in _nodes(items, "Method") if n.properties.get("resolved") is False]


def test_externally_visible_real_method_carries_a_linkage_key():
    widget = RawType(
        kind="Class",
        name="Widget",
        namespace="Acme",
        methods=[
            RawMethod(name="Save", kind="ORDINARY", accessibility="public", return_type="void"),
            RawMethod(name="Helper", kind="ORDINARY", accessibility="private", return_type="void"),
        ],
    )
    items = _resolve([RawFile(path="a.cs", types=[widget])])

    by_name = {n.properties["name"]: n for n in _nodes(items, "Method")}
    # The linkage key is the fully-qualified consumer name (namespace + type + method), never a
    # parameter-typed id -- so a consumer's placeholder can heal onto it.
    assert by_name["Save"].properties["linkage_key"] == "extern://csharp/Acme.Widget.Save"
    # Private members can't be a cross-assembly target, so they carry no linkage key.
    assert "linkage_key" not in by_name["Helper"].properties


def test_unresolved_call_on_an_external_type_becomes_a_placeholder():
    # `Billing` is never declared in this ingest -> a call on it is an out-of-project reference.
    controller = RawType(
        kind="Class",
        name="WidgetsController",
        namespace="Acme",
        fields=[RawField(name="_billing", accessibility="private", type="Billing")],
        methods=[
            RawMethod(
                name="Charge",
                kind="ORDINARY",
                accessibility="public",
                return_type="void",
                calls=[RawCall(name="Pay", receiver="_billing")],
            )
        ],
    )
    items = _resolve([RawFile(path="a.cs", types=[controller])])

    placeholders = _placeholders(items)
    assert len(placeholders) == 1
    ph = placeholders[0]
    assert ph.key["id"] == "extern://csharp/Billing.Pay"
    assert ph.properties["name"] == "Pay"
    assert ph.properties["qualified_name"] == "Billing.Pay"
    assert ph.properties["origin"] == "external"
    assert ph.properties["linkage_key"] == "extern://csharp/Billing.Pay"

    calls = _edges(items, "CALLS")
    assert len(calls) == 1
    assert calls[0].to_key["id"] == "extern://csharp/Billing.Pay"
    assert calls[0].properties["resolution"] == "external"


def test_bcl_call_surfaces_as_a_stdlib_placeholder():
    # A call on a System.* member: unresolved (no such method in the project) yet nameable, so it
    # becomes a placeholder classified stdlib -- "who calls StringBuilder.Append" is now one hop.
    builder = RawType(
        kind="Class",
        name="ReportBuilder",
        namespace="Acme",
        fields=[RawField(name="_sb", accessibility="private", type="System.Text.StringBuilder")],
        methods=[
            RawMethod(
                name="Build",
                kind="ORDINARY",
                accessibility="public",
                return_type="string",
                calls=[RawCall(name="Append", receiver="_sb")],
            )
        ],
    )
    items = _resolve([RawFile(path="a.cs", types=[builder])])

    placeholders = _placeholders(items)
    assert [n.key["id"] for n in placeholders] == ["extern://csharp/System.Text.StringBuilder.Append"]
    assert placeholders[0].properties["origin"] == "stdlib"


def test_static_call_on_a_type_name_becomes_a_placeholder():
    greeter = RawType(
        kind="Class",
        name="Greeter",
        namespace="Acme",
        methods=[
            RawMethod(
                name="Hello",
                kind="ORDINARY",
                accessibility="public",
                return_type="void",
                calls=[RawCall(name="WriteLine", receiver="Console")],  # Console.WriteLine(...)
            )
        ],
    )
    items = _resolve([RawFile(path="a.cs", types=[greeter])])

    placeholders = _placeholders(items)
    assert [n.key["id"] for n in placeholders] == ["extern://csharp/Console.WriteLine"]
    assert placeholders[0].properties["name"] == "WriteLine"


def test_missing_member_of_a_project_type_stays_dropped_not_a_placeholder():
    # `_repo`'s type IS a project type; a call to a member it doesn't have is intra-project
    # dangling (not cross-repo), so per the ADR guard it is left dropped -- no placeholder, no edge.
    repo = RawType(
        kind="Class",
        name="Repo",
        namespace="Acme",
        methods=[RawMethod(name="Get", kind="ORDINARY", accessibility="public", return_type="int")],
    )
    user = RawType(
        kind="Class",
        name="User",
        namespace="Acme",
        fields=[RawField(name="_repo", accessibility="private", type="Repo")],
        methods=[
            RawMethod(
                name="Do",
                kind="ORDINARY",
                accessibility="public",
                return_type="void",
                calls=[RawCall(name="Nope", receiver="_repo")],
            )
        ],
    )
    items = _resolve([RawFile(path="a.cs", types=[repo, user])])

    assert _placeholders(items) == []
    assert _edges(items, "CALLS") == []


def test_unnameable_unresolved_call_is_not_a_placeholder():
    # A bare (receiver-less) call we can't resolve is a dangling project reference, not external.
    widget = RawType(
        kind="Class",
        name="Widget",
        namespace="Acme",
        methods=[
            RawMethod(
                name="Run",
                kind="ORDINARY",
                accessibility="public",
                return_type="void",
                calls=[RawCall(name="Missing", receiver=None)],
            )
        ],
    )
    items = _resolve([RawFile(path="a.cs", types=[widget])])

    assert _placeholders(items) == []
    assert _edges(items, "CALLS") == []


def test_multiple_callers_merge_onto_one_placeholder():
    def caller(type_name, method_name):
        return RawType(
            kind="Class",
            name=type_name,
            namespace="Acme",
            fields=[RawField(name="_c", accessibility="private", type="Client")],
            methods=[
                RawMethod(
                    name=method_name,
                    kind="ORDINARY",
                    accessibility="public",
                    return_type="void",
                    calls=[RawCall(name="Send", receiver="_c")],
                )
            ],
        )

    items = _resolve([RawFile(path="a.cs", types=[caller("A", "M1"), caller("B", "M2")])])

    assert len(_placeholders(items)) == 1  # one shared placeholder node...
    external_calls = [e for e in _edges(items, "CALLS") if e.to_key["id"] == "extern://csharp/Client.Send"]
    assert len(external_calls) == 2  # ...reached by an edge from each caller


def test_doc_property_reaches_type_and_method_nodes():
    # The `doc` captured in phase 1 (file_parser) must survive onto the emitted
    # Class and Method node properties, alongside `source` -- the natural-language
    # half of the shared `doc + source` document (see core.docstrings).
    widget = RawType(
        kind="Class",
        name="Widget",
        namespace="Acme",
        doc="Widget does things.",
        methods=[
            RawMethod(
                name="Get",
                kind="ORDINARY",
                accessibility="public",
                return_type="int",
                doc="Gets the value.",
            )
        ],
    )
    items = _resolve([RawFile(path="a.cs", types=[widget])])

    class_node = next(n for n in _nodes(items, "Class") if n.properties["name"] == "Widget")
    assert class_node.properties["doc"] == "Widget does things."
    method_node = next(n for n in _nodes(items, "Method") if n.properties["name"] == "Get")
    assert method_node.properties["doc"] == "Gets the value."


def test_httpclient_call_materializes_route_keyed_endpoint():
    m = RawMethod(name="Fetch", kind="ORDINARY", accessibility="public",
                  calls=[RawCall(name="GetAsync", receiver="_client",
                                 http_url="/api/widgets/5", http_method="GET")])
    t = RawType(kind="Class", name="Gateway", namespace="Bff", methods=[m])
    items = _resolve([RawFile(path="g.cs", types=[t])])

    eps = _nodes(items, "APIEndpoint")
    assert [e.key["id"] for e in eps] == ["extern://http/GET:/api/widgets/5"]  # shared core key
    assert eps[0].properties["resolved"] is False and eps[0].properties["provenance"] == "http-derived"
    calls_api = _edges(items, "CALLS_API")
    assert len(calls_api) == 1 and calls_api[0].to_key["id"] == "extern://http/GET:/api/widgets/5"
    # no spurious CALLS to a GetAsync placeholder for the HTTP call
    assert not _edges(items, "CALLS")
