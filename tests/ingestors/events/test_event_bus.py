"""The message/event-bus stitch (auryn-events): a publisher and a subscriber that share a topic/queue
name are linked publisher -PUBLISHES_TO-> Topic <-SUBSCRIBES_TO- subscriber -- a decoupled connection
with no call edge in the graph. Needs FalkorDB (the conftest fixture)."""

from __future__ import annotations

import git

from auryn.core import ingest
from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphNode
from auryn.ingestors.events.stitch import _extract, _valid_topic, stitch


def _commit(repo_dir, files, message):
    repo = git.Repo.init(str(repo_dir), initial_branch="main") if not (repo_dir / ".git").exists() \
        else git.Repo(str(repo_dir))
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "t@e.com")
        cw.set_value("user", "name", "T")
    repo.index.add(files)
    return repo.index.commit(message).hexsha


def _fn(w, nid, name, source, label="Function"):
    w.merge_node(GraphNode(label, {"id": nid}, {"name": name, "source": source}))


def test_valid_topic_filters_non_topics():
    assert _valid_topic("orders.created") and _valid_topic("user_events") and _valid_topic("svc:topic-1")
    assert not _valid_topic("/api/users")          # an HTTP route, not a topic
    assert not _valid_topic("https://x/y")         # a URL
    assert not _valid_topic("")


def test_stitch_links_publisher_and_subscriber(graph):
    w = GraphWriter(graph)
    _fn(w, "py://app/publish_order", "publish_order",
        "def publish_order(p):\n    p.send('orders.created', b'x')\n")
    _fn(w, "py://app/on_order", "on_order",
        "def on_order():\n    consumer.subscribe(['orders.created'])\n")
    # an unrelated function must NOT produce a topic
    _fn(w, "py://app/unrelated", "unrelated", "def unrelated():\n    return http.send('/api/x')\n")

    report = stitch(w)
    assert report is not None and report["topics"] == 1

    def q(c):
        return [r[0] for r in graph.query(c).result_set]

    assert q("MATCH (:Function {name:'publish_order'})-[:PUBLISHES_TO]->(t:Topic) RETURN t.name") == ["orders.created"]
    assert q("MATCH (:Function {name:'on_order'})-[:SUBSCRIBES_TO]->(t:Topic) RETURN t.name") == ["orders.created"]
    # the http.send('/api/x') call is filtered out (route, not a topic)
    assert q("MATCH (:Function {name:'unrelated'})-[:PUBLISHES_TO]->() RETURN 1") == []
    # publisher and subscriber converge on ONE Topic node
    assert q("MATCH (t:Topic {name:'orders.created'})<-[:PUBLISHES_TO]-() RETURN 1") == [1]
    assert q("MATCH (t:Topic {name:'orders.created'})<-[:SUBSCRIBES_TO]-() RETURN 1") == [1]


def test_stitch_spring_annotations_and_amqp(graph):
    w = GraphWriter(graph)
    _fn(w, "java://app/Producer.emit", "emit",
        'public void emit() { kafkaTemplate.send("payments", msg); }', label="Method")
    _fn(w, "java://app/Listener.handle", "handle",
        '@KafkaListener(topics = "payments")\npublic void handle(String m) { }', label="Method")

    report = stitch(w)
    assert report is not None

    def q(c):
        return [r[0] for r in graph.query(c).result_set]

    assert q("MATCH (:Method {name:'emit'})-[:PUBLISHES_TO]->(t:Topic {name:'payments'}) RETURN 1") == [1]
    assert q("MATCH (:Method {name:'handle'})-[:SUBSCRIBES_TO]->(t:Topic {name:'payments'}) RETURN 1") == [1]


def test_stitch_is_idempotent(graph):
    w = GraphWriter(graph)
    _fn(w, "py://app/pub", "pub", "def pub(p):\n    p.publish('t.1', x)\n")
    stitch(w)
    first = graph.query("MATCH ()-[r:PUBLISHES_TO]->() RETURN count(r)").result_set[0][0]
    stitch(w)
    second = graph.query("MATCH ()-[r:PUBLISHES_TO]->() RETURN count(r)").result_set[0][0]
    assert first == second == 1


def test_extract_strips_comments():
    # a commented-out publish must NOT yield a topic (PR #25 review finding)
    assert _extract("# producer.send('orders.created', payload)\nreturn None\n") == (set(), set())
    assert _extract("  // kafkaTemplate.send('payments', m);\n") == (set(), set())
    # a real call with a trailing comment still counts
    pubs, _ = _extract("producer.send('orders', x)  # fire it\n")
    assert pubs == {"orders"}


def test_extract_ignores_concatenated_topic():
    # `"orders." + tenant` is not a complete literal -> no partial `orders.` topic (PR #25 review finding)
    assert _extract('producer.send("orders." + tenant, value)\n') == (set(), set())
    assert _extract('producer.send("orders.created", value)\n')[0] == {"orders.created"}


def test_extract_multi_topic_subscribe():
    # subscribe to several topics links EVERY topic, not just the first (PR #25 review finding)
    _, subs = _extract("consumer.subscribe(['orders', 'payments'])\n")
    assert subs == {"orders", "payments"}
    _, subs2 = _extract('@KafkaListener(topics = {"a", "b"})\nvoid h() {}\n')
    assert subs2 == {"a", "b"}


def test_extract_ignores_concatenated_list_element():
    # a concatenated element in a subscribe list is bounded-checked on BOTH sides -> not a partial topic
    assert _extract('consumer.subscribe([prefix + "orders"])\n') == (set(), set())
    assert _extract("consumer.subscribe(['a', 'b'])\n")[1] == {"a", "b"}


def test_extract_convert_and_send_routing_key():
    # convertAndSend(exchange, routingKey, msg): the ROUTING KEY (last string) is the topic, not exchange
    pubs, _ = _extract('rabbitTemplate.convertAndSend("", "payments", msg);\n')
    assert pubs == {"payments"}
    pubs2, _ = _extract('rabbitTemplate.convertAndSend("payments", msg);\n')
    assert pubs2 == {"payments"}


def test_valid_topic_allows_single_char():
    assert _valid_topic("t")  # 1-char topics are valid (PR #25 review finding)


def test_stitch_no_events_returns_none(graph):
    w = GraphWriter(graph)
    _fn(w, "py://app/plain", "plain", "def plain():\n    return 1 + 1\n")
    assert stitch(w) is None


def test_stitch_end_to_end_python_kafka(graph, tmp_path):
    """End-to-end over the real Python ingestor + the enrichment in the ingest tail: a producer and a
    consumer sharing a topic converge on one Topic node."""
    (tmp_path / "svc.py").write_text(
        "def ship(producer):\n    producer.send('shipments', b'x')\n\n"
        "def watch(consumer):\n    consumer.subscribe(['shipments'])\n"
    )
    ingest.ingest_directory(GraphWriter(graph), str(tmp_path), project_name="shipsvc")

    pub = [r[0] for r in graph.query(
        "MATCH (:Function {name:'ship'})-[:PUBLISHES_TO]->(t:Topic) RETURN t.name").result_set]
    sub = [r[0] for r in graph.query(
        "MATCH (:Function {name:'watch'})-[:SUBSCRIBES_TO]->(t:Topic) RETURN t.name").result_set]
    assert pub == ["shipments"] and sub == ["shipments"]


def test_event_bus_layer_tracks_incremental_resync(graph, tmp_path):
    """Delta-sync: the pub/sub layer is re-derived on every resync (the enrichment re-runs in the ingest
    tail after the language's incremental sync). The producer and subscriber live in SEPARATE files and
    only the producer's file changes, so the resync must take the INCREMENTAL path (the unchanged
    subscriber file is cached, not re-parsed) -- `deep="off"` keeps the Python graph single-phase so the
    ast engine's incremental routing fires (a `deep="inline"` graph sits at depth="deep" and the second
    ingest would silently full-recompute, defeating the test -- PR #27 review finding). After the resync
    the stale PUBLISHES_TO->orders is gone, the producer is on the new topic, no duplicate edges."""
    w = GraphWriter(graph)
    repo_dir = tmp_path / "bus"
    repo_dir.mkdir()
    pub = repo_dir / "publisher.py"
    sub = repo_dir / "subscriber.py"
    pub.write_text("def ship(producer):\n    producer.send('orders', b'x')\n")
    sub.write_text("def watch(consumer):\n    consumer.subscribe(['orders'])\n")   # unchanged bystander
    _commit(repo_dir, ["publisher.py", "subscriber.py"], "v1")
    ingest.ingest_directory(w, str(repo_dir), project_name="bus", deep="off", auto_install=False)

    # v1: one Topic 'orders' with a publisher AND a subscriber (the golden path).
    assert graph.query(
        "MATCH (:Function {name:'ship'})-[:PUBLISHES_TO]->(:Topic {name:'orders'})"
        "<-[:SUBSCRIBES_TO]-(:Function {name:'watch'}) RETURN 1"
    ).result_set == [[1]]

    # v2: change ONLY the producer's topic; subscriber.py is untouched; commit + resync.
    pub.write_text("def ship(producer):\n    producer.send('shipments', b'x')\n")
    _commit(repo_dir, ["publisher.py"], "v2 -- producer moved topic")
    report = ingest.ingest_directory(w, str(repo_dir), project_name="bus", deep="off", auto_install=False)

    # PROVE the resync was INCREMENTAL, not a full recompute: the unchanged subscriber.py was cached
    # (a full recompute re-parses every file -> files_cached == 0).
    py = next(r for r in report.languages if r.language == "python")
    assert py.result.files_cached >= 1, f"expected a partial re-parse, cached {py.result.files_cached}"

    # the producer tracks the new topic; the stale 'orders' publish edge is gone; the subscriber unchanged.
    assert graph.query("MATCH (:Function {name:'ship'})-[:PUBLISHES_TO]->(t:Topic) RETURN t.name").result_set \
        == [["shipments"]]
    assert graph.query(
        "MATCH (:Function {name:'ship'})-[:PUBLISHES_TO]->(:Topic {name:'orders'}) RETURN count(*)"
    ).result_set == [[0]]
    # exactly one publish edge -- no duplicate accumulated across the resync.
    assert graph.query("MATCH (:Function {name:'ship'})-[:PUBLISHES_TO]->() RETURN count(*)").result_set == [[1]]
    assert graph.query(
        "MATCH (:Function {name:'watch'})-[:SUBSCRIBES_TO]->(:Topic {name:'orders'}) RETURN count(*)"
    ).result_set == [[1]]
