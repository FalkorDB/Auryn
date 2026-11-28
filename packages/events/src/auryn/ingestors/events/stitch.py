"""Message/event-bus stitch -- ties a PUBLISHER to a SUBSCRIBER through the topic/queue they share, an
edge that otherwise does NOT exist in the graph at all (there is no call between them -- they are
decoupled by the broker). Unlike the gRPC/HTTP seams there is no generated contract to key off, so the
join key is the TOPIC NAME as written in the pub/sub call, and detection is deliberately a curated set
of higher-confidence broker patterns (Kafka/RabbitMQ/NATS/Redis/SNS-style) rather than every `.send(`.

Golden path:

    publisher -PUBLISHES_TO-> Topic <-SUBSCRIBES_TO- subscriber
    publisher -NOTIFIES-> subscriber                              (derived, ADR 0008)

Reads each callable's stored `source` (no file I/O, like auryn-orm). Comments are stripped first (a
commented-out publish must not mint an edge), only a COMPLETE string literal is accepted as a topic (a
concatenated `"orders." + tenant` is ignored, not captured as `orders.`), and a multi-topic
`subscribe(['a','b'])` / `@KafkaListener(topics={'a','b'})` links to EVERY topic. Idempotent +
order-independent (owns its whole Topic + PUBLISHES_TO/SUBSCRIBES_TO layer and its `message-bus`
NOTIFIES edges). Heuristic by nature: only a string-literal topic is resolved (a topic held in a
variable/constant is not)."""

from __future__ import annotations

import logging
import re
from collections import defaultdict

from auryn.core.graph_client import GraphWriter, semantic_label
from auryn.core.graph_model import EdgeSchema, GraphEdge, GraphNode, NodeSchema

logger = logging.getLogger("auryn.events")

_SCHEMA = [NodeSchema(label="Topic", indexed_properties=("name",))]
_EDGES = [EdgeSchema(rel_type="PUBLISHES_TO"), EdgeSchema(rel_type="SUBSCRIBES_TO"),
          EdgeSchema(rel_type="NOTIFIES")]

# A COMPLETE string-literal argument/element: a quoted run bounded on BOTH sides by an arg/list delimiter
# (or string ends), so a concatenation is never captured -- neither a trailing `"orders." + x` (no right
# delimiter) nor a leading `prefix + "orders"` inside a list (no left delimiter). The left boundary char
# is consumed; finditer's non-overlap shares the comma between adjacent list elements.
_ARG_STR = re.compile(r"""(?:^|[(\[{,=])\s*["']([^"']+)["']\s*(?=[)\]},]|$)""")
# Publish calls: producer.send/produce + generic/NATS/Redis .publish, each with a complete first string.
_SEND = re.compile(r"""\.(?:send|produce|publish)\s*\(\s*["']([^"']+)["']\s*(?=[,)])""")
_CONVERT = re.compile(r"\bconvertAndSend\s*\(([^)]*)\)")                         # Spring AMQP (routing key = last string)
_BASIC_PUB = re.compile(r"""\bbasic_publish\s*\([^)]*routing_key\s*=\s*["']([^"']+)["']""")  # pika
# Subscribe calls: a list or single topic; @KafkaListener/@RabbitListener topic/queue set; pika consume.
_SUBSCRIBE = re.compile(r"""\.subscribe\s*\(\s*(\[[^\]]*\]|["'][^"']*["'])""")
_LISTENER = re.compile(r"""@(?:KafkaListener|RabbitListener)\s*\([^)]*(?:topics|queues)\s*=\s*(\{[^}]*\}|["'][^"']*["'])""")
_BASIC_CONSUME = re.compile(r"""\bbasic_consume\s*\([^)]*queue\s*=\s*["']([^"']+)["']""")

_KEYWORDS = (".send(", ".produce(", ".publish(", "convertAndSend", "basic_publish",
             ".subscribe(", "KafkaListener", "RabbitListener", "basic_consume")

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"(?m)^[ \t]*(?:#|//).*$")       # a FULL-line comment (a commented-out call)
_TOPIC_RE = re.compile(r"^[A-Za-z0-9_][\w.\-:]{0,127}$")    # a topic name, not a path/URL; 1-char allowed


def _valid_topic(t: str) -> bool:
    """A captured string is plausibly a topic/queue name, not an HTTP route or arbitrary arg -- keeps the
    broader `.send(`/`.publish(` patterns from minting junk Topic nodes."""
    return bool(_TOPIC_RE.match(t)) and not t.lower().startswith(("http:", "https:"))


def _strip_comments(src: str) -> str:
    """Remove block comments and full-line `#`/`//` comments so a commented-out publish/subscribe call
    doesn't mint an edge. A trailing comment after real code is left (its line isn't comment-only)."""
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub(" ", src))


def _extract(src: str) -> tuple[set[str], set[str]]:
    """(published topics, subscribed topics) for one callable's source -- comment-stripped, complete
    literals only, multi-topic lists expanded."""
    src = _strip_comments(src)
    pubs: set[str] = set()
    subs: set[str] = set()
    for m in _SEND.finditer(src):
        pubs.add(m.group(1))
    for m in _CONVERT.finditer(src):
        strs = _ARG_STR.findall(m.group(1))
        if strs:
            pubs.add(strs[-1])  # convertAndSend(exchange, routingKey, msg): the routing key is the last string
    for m in _BASIC_PUB.finditer(src):
        pubs.add(m.group(1))
    for m in _SUBSCRIBE.finditer(src):
        subs.update(_ARG_STR.findall(m.group(1)))
    for m in _LISTENER.finditer(src):
        subs.update(_ARG_STR.findall(m.group(1)))
    for m in _BASIC_CONSUME.finditer(src):
        subs.add(m.group(1))
    return {t for t in pubs if _valid_topic(t)}, {t for t in subs if _valid_topic(t)}


def run(writer: GraphWriter, source, target_sha: str) -> dict | None:
    """GraphEnrichment entrypoint. `source`/`target_sha` unused (reads callables' `source` from the graph)."""
    return stitch(writer)


def stitch(writer: GraphWriter) -> dict | None:
    _clear_prior_layer(writer)
    callables = _read_callables(writer)
    if not callables:
        return None

    topics: set[str] = set()
    edges: list[GraphEdge] = []
    n_pub = n_sub = 0
    # topic -> the (id, label) of every callable that publishes / subscribes to it, for NOTIFIES
    ends = {"PUBLISHES_TO": defaultdict(set), "SUBSCRIBES_TO": defaultdict(set)}
    for cid, clabel, src in callables:
        pubs, subs = _extract(src)
        for rel, found in (("PUBLISHES_TO", pubs), ("SUBSCRIBES_TO", subs)):
            for topic in found:
                topics.add(topic)
                edges.append(GraphEdge(rel, clabel, {"id": cid}, "Topic", {"id": f"topic://{topic}"},
                                       {"provenance": "message-bus", "topic": topic}))
                ends[rel][topic].add((cid, clabel))
                n_pub += rel == "PUBLISHES_TO"
                n_sub += rel == "SUBSCRIBES_TO"
    if not edges:
        return None

    notifies = _notifies_edges(ends["PUBLISHES_TO"], ends["SUBSCRIBES_TO"])
    writer.setup_schema(_SCHEMA)
    writer.merge_nodes([GraphNode("Topic", {"id": f"topic://{t}"}, {"name": t, "provenance": "message-bus"})
                        for t in sorted(topics)])
    writer.merge_edges(edges + notifies)
    report = {"topics": len(topics), "publishes_edges": n_pub, "subscribes_edges": n_sub,
              "notifies_edges": len(notifies)}
    logger.info("message-bus stitch: %s", report)
    return report


def _notifies_edges(publishers: dict[str, set[tuple[str, str]]],
                    subscribers: dict[str, set[tuple[str, str]]]) -> list[GraphEdge]:
    """A direct, traversable `publisher -[:NOTIFIES]-> subscriber` edge for every pair that shares a
    topic (ADR 0008). The Topic node forms a "V" -- both sides point INTO it -- so a single-direction
    walk (who_calls, callees, impact_of, trace_entrypoint) stops at the topic. This collapses the V into
    one edge, the async sibling of the HTTP stitch's INVOKES: a separate type because the publisher
    never waits on, or depends on, its subscribers, so a dependency walk (impact_of_diff) can leave it
    out while the reachability walks follow it.

    One edge per pair, `topics` lists every topic they share. A callable that subscribes to its own
    topic gets no self-loop. Built from the same extraction as the raw edges, so it is a pure function
    of the current callables' source."""
    shared: dict[tuple[tuple[str, str], tuple[str, str]], set[str]] = defaultdict(set)
    for topic, pubs in publishers.items():
        for pub in pubs:
            for sub in subscribers.get(topic, ()):
                if pub[0] != sub[0]:
                    shared[(pub, sub)].add(topic)
    return [
        GraphEdge("NOTIFIES", pub_label, {"id": pub_id}, sub_label, {"id": sub_id},
                  {"provenance": "message-bus", "topics": sorted(found)})
        for ((pub_id, pub_label), (sub_id, sub_label)), found in sorted(shared.items())
    ]


def _read_callables(writer: GraphWriter):
    """(id, label, source) for Function/Method nodes whose source mentions a broker call -- the cheap
    substring pre-filter keeps this from reading every function body in the graph."""
    conds = " OR ".join(f"c.source CONTAINS '{k}'" for k in _KEYWORDS)
    return [
        (str(cid), str(lbl), str(src)) for cid, lbl, src in writer.ro_query(
            f"MATCH (c) WHERE (c:Function OR c:Method) AND c.source IS NOT NULL AND ({conds}) "
            f"RETURN c.id, {semantic_label('c')}, c.source"
        ).result_set if src
    ]


def _clear_prior_layer(writer: GraphWriter) -> None:
    """Drop this hook's whole layer so a re-run rebuilds from the current graph (idempotent). Runs
    before the early returns, so the last publish or subscribe removed from the code takes its
    derived NOTIFIES edges with it."""
    writer.query("MATCH ()-[r:PUBLISHES_TO]->() DELETE r")
    writer.query("MATCH ()-[r:SUBSCRIBES_TO]->() DELETE r")
    writer.query("MATCH ()-[r:NOTIFIES]->() WHERE r.provenance = 'message-bus' DELETE r")
    writer.query("MATCH (n:Topic) DETACH DELETE n")
