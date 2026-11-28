"""Message/event-bus stitch -- ties a PUBLISHER to a SUBSCRIBER through the topic/queue they share, an
edge that otherwise does NOT exist in the graph at all (there is no call between them -- they are
decoupled by the broker). Unlike the gRPC/HTTP seams there is no generated contract to key off, so the
join key is the TOPIC NAME as written in the pub/sub call, and detection is deliberately a curated set
of higher-confidence broker patterns (Kafka/RabbitMQ/NATS/Redis/SNS-style) rather than every `.send(`.

Go callables (`go://` ids) get a second pattern set (see _extract_go): Go's broker APIs are capitalised
(`nc.Publish("order.placed", data)`, `rdb.Subscribe(ctx, "a", "b")`) and its Kafka clients name the topic
in a composite literal (`kafka.Message{Topic: "orders"}`, `sarama.ProducerMessage{Topic: "orders"}`).
That set never runs on other languages, where a capitalised `Publish("x", ...)` is not reliably a broker
call.

Golden path:

    publisher -PUBLISHES_TO-> Topic <-SUBSCRIBES_TO- subscriber

Reads each callable's stored `source` (no file I/O, like auryn-orm). Comments are stripped first (a
commented-out publish must not mint an edge), only a COMPLETE string literal is accepted as a topic (a
concatenated `"orders." + tenant` is ignored, not captured as `orders.`), and a multi-topic
`subscribe(['a','b'])` / `@KafkaListener(topics={'a','b'})` links to EVERY topic. Idempotent +
order-independent (owns its whole Topic + PUBLISHES_TO/SUBSCRIBES_TO layer). Heuristic by nature: only a
string-literal topic is resolved (a topic held in a variable/constant is not)."""

from __future__ import annotations

import logging
import re

from auryn.core.graph_client import GraphWriter, semantic_label
from auryn.core.graph_model import EdgeSchema, GraphEdge, GraphNode, NodeSchema

logger = logging.getLogger("auryn.events")

_SCHEMA = [NodeSchema(label="Topic", indexed_properties=("name",))]
_EDGES = [EdgeSchema(rel_type="PUBLISHES_TO"), EdgeSchema(rel_type="SUBSCRIBES_TO")]

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

# Go -- run on `go://` callables only (see _extract). A Go string topic is a double-quoted literal ('x' is
# a rune). A leading context argument is skipped: go-redis, JetStream and amqp091 take `(ctx, ...)`.
_GO_CTX = r"(?:\w*[cC]tx|context\.(?:Background|TODO)\(\)|\w+\.Context\(\))\s*,\s*"
# RabbitMQ amqp091 `ch.Publish("exchange", "routing.key", mandatory, immediate, msg)`: the routing key.
_GO_AMQP_PUB = re.compile(
    rf'\.Publish(?:WithContext)?\s*\(\s*(?:{_GO_CTX})?"[^"]*"\s*,\s*"([^"]+)"\s*,\s*(?:true|false)\b')
# NATS/JetStream `nc.Publish("subj", data)`, go-redis `rdb.Publish(ctx, "ch", msg)`, MQTT: the first
# string, unless the call has amqp091's shape. `expvar.Publish("name", v)` names a metric, not a topic.
_GO_SEND = re.compile(rf'(?<!expvar)\.(?:Publish|PublishAsync)\s*\(\s*(?:{_GO_CTX})?"([^"]+)"\s*(?=[,)])'
                      r'(?!\s*,\s*"[^"]*"\s*,\s*(?:true|false)\b)')
# The first string only (NATS QueueSubscribe's second string is the queue group): NATS/JetStream,
# confluent-kafka-go `c.Subscribe("t", nil)`, sarama `ConsumePartition("t", 0, offset)`, amqp091
# `ch.Consume("queue", "consumer", ...)`.
_GO_SUBSCRIBE = re.compile(r"\.(?:Subscribe|SubscribeSync|QueueSubscribe|QueueSubscribeSync|ChanSubscribe"
                           r'|ChanQueueSubscribe|PullSubscribe|ConsumePartition|Consume)\s*\(\s*"([^"]+)"\s*(?=[,)])')
# Every string argument: go-redis `rdb.Subscribe(ctx, "a", "b")`, franz-go `kgo.ConsumeTopics("a", "b")`.
_GO_SUB_ARGS = re.compile(rf'(?:\.Subscribe\s*\(\s*{_GO_CTX}|\bConsumeTopics\s*\(\s*)((?:"[^"]*"\s*,\s*)*"[^"]*")\s*\)')
# A `[]string{...}` list: confluent `c.SubscribeTopics([]string{"a"}, nil)`, sarama consumer group
# `group.Consume(ctx, []string{"a"}, handler)`.
_GO_SUB_LIST = re.compile(rf"\.(?:SubscribeTopics\s*\(|Consume\s*\(\s*(?:{_GO_CTX})?)\s*\[\]string\s*(\{{[^}}]*\}})")
# Kafka composite literals, package-qualified: kafka-go `kafka.Message{Topic: "x"}`, `kafka.Writer{...}`
# and `kafka.WriterConfig{...}`, sarama `sarama.ProducerMessage{...}` and franz-go `kgo.Record{...}`
# publish; kafka-go `kafka.ReaderConfig{Topic: "x"}` (or `GroupTopics: []string{...}`) subscribes.
# confluent-kafka-go takes the topic by pointer (`TopicPartition{Topic: &topic}`), a variable, so it is
# not resolved.
_GO_LITERAL = re.compile(
    r"\b[A-Za-z_]\w*\.(Message|ProducerMessage|Record|Writer|WriterConfig|Reader|ReaderConfig)\s*\{")
_GO_SUB_LITERALS = frozenset({"Reader", "ReaderConfig"})
_GO_TOPIC_FIELD = re.compile(r'(?:^|[\s,])Topic\s*:\s*"([^"]+)"\s*(?=[,}]|$)')
_GO_GROUP_TOPICS = re.compile(r"(?:^|[\s,])GroupTopics\s*:\s*\[\]string\s*(\{[^}]*\})")
_INNER_LITERAL = re.compile(r"\{[^{}]*\}")

_GO_KEYWORDS = (".Publish", "Subscribe", ".Consume", "Topic:", "GroupTopics")

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


def _extract(src: str, go: bool = False) -> tuple[set[str], set[str]]:
    """(published topics, subscribed topics) for one callable's source -- comment-stripped, complete
    literals only, multi-topic lists expanded. `go` adds the Go pattern set (see _extract_go)."""
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
    if go:
        _extract_go(src, pubs, subs)
    return {t for t in pubs if _valid_topic(t)}, {t for t in subs if _valid_topic(t)}


def _extract_go(src: str, pubs: set[str], subs: set[str]) -> None:
    """Add the topics the Go pattern set finds in `src` (comment-stripped) to `pubs` and `subs`."""
    pubs.update(m.group(1) for m in _GO_SEND.finditer(src))
    pubs.update(m.group(1) for m in _GO_AMQP_PUB.finditer(src))
    subs.update(m.group(1) for m in _GO_SUBSCRIBE.finditer(src))
    for pattern in (_GO_SUB_ARGS, _GO_SUB_LIST):
        for m in pattern.finditer(src):
            subs.update(_ARG_STR.findall(m.group(1)))
    for type_name, body in _go_literals(src):
        found = subs if type_name in _GO_SUB_LITERALS else pubs
        for m in _GO_GROUP_TOPICS.finditer(body):
            found.update(_ARG_STR.findall(m.group(1)))
        found.update(_GO_TOPIC_FIELD.findall(_top_level(body)))


def _go_literals(src: str):
    """Yield (type name, body) for each Kafka composite literal (see _GO_LITERAL). The body runs to the
    matching brace, so a nested literal (`Brokers: []string{...}`) stays inside it."""
    for m in _GO_LITERAL.finditer(src):
        depth, i = 1, m.end()
        while depth and i < len(src):
            depth += {"{": 1, "}": -1}.get(src[i], 0)
            i += 1
        if not depth:
            yield m.group(1), src[m.end():i - 1]


def _top_level(body: str) -> str:
    """A composite literal's body with its nested literals removed, so a `Topic:` match is one of the
    literal's own fields."""
    while (flat := _INNER_LITERAL.sub("", body)) != body:
        body = flat
    return body


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
    for cid, clabel, src in callables:
        pubs, subs = _extract(src, go=cid.startswith("go://"))
        for rel, found in (("PUBLISHES_TO", pubs), ("SUBSCRIBES_TO", subs)):
            for topic in found:
                topics.add(topic)
                edges.append(GraphEdge(rel, clabel, {"id": cid}, "Topic", {"id": f"topic://{topic}"},
                                       {"provenance": "message-bus", "topic": topic}))
                n_pub += rel == "PUBLISHES_TO"
                n_sub += rel == "SUBSCRIBES_TO"
    if not edges:
        return None

    writer.setup_schema(_SCHEMA)
    writer.merge_nodes([GraphNode("Topic", {"id": f"topic://{t}"}, {"name": t, "provenance": "message-bus"})
                        for t in sorted(topics)])
    writer.merge_edges(edges)
    report = {"topics": len(topics), "publishes_edges": n_pub, "subscribes_edges": n_sub}
    logger.info("message-bus stitch: %s", report)
    return report


def _read_callables(writer: GraphWriter):
    """(id, label, source) for Function/Method nodes whose source mentions a broker call -- the cheap
    substring pre-filter keeps this from reading every function body in the graph. A Go callable also
    passes on a Go keyword."""
    conds = " OR ".join(f"c.source CONTAINS '{k}'" for k in _KEYWORDS)
    go_conds = " OR ".join(f"c.source CONTAINS '{k}'" for k in _GO_KEYWORDS)
    return [
        (str(cid), str(lbl), str(src)) for cid, lbl, src in writer.ro_query(
            f"MATCH (c) WHERE (c:Function OR c:Method) AND c.source IS NOT NULL "
            f"AND ({conds} OR (c.id STARTS WITH 'go://' AND ({go_conds}))) "
            f"RETURN c.id, {semantic_label('c')}, c.source"
        ).result_set if src
    ]


def _clear_prior_layer(writer: GraphWriter) -> None:
    """Drop this hook's whole layer so a re-run rebuilds from the current graph (idempotent)."""
    writer.query("MATCH ()-[r:PUBLISHES_TO]->() DELETE r")
    writer.query("MATCH ()-[r:SUBSCRIBES_TO]->() DELETE r")
    writer.query("MATCH (n:Topic) DETACH DELETE n")
