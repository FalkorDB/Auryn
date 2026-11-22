"""The message-bus stitch on Go: NATS, go-redis, RabbitMQ (amqp091) and the Kafka clients (kafka-go,
sarama, confluent-kafka-go, franz-go) publish and subscribe with capitalised methods or name the topic
in a composite literal. Those patterns run on `go://` callables only. The graph tests need FalkorDB (the
conftest fixture)."""

from __future__ import annotations

import pytest

from auryn.core import ingest
from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphNode
from auryn.ingestors.events.stitch import _extract, stitch


def _go(src):
    return _extract(src, go=True)


@pytest.mark.parametrize("src, pubs", [
    ('return nc.Publish("order.placed", data)', {"order.placed"}),                     # NATS
    ('js.Publish(ctx, "ORDERS.new", []byte("x"))', {"ORDERS.new"}),                     # JetStream (ctx first)
    ('js.PublishAsync("ORDERS.new", d)', {"ORDERS.new"}),
    ('rdb.Publish(r.Context(), "chat", "hello")', {"chat"}),                            # go-redis
    ('ch.Publish("", "orders", false, false, amqp.Publishing{Body: b})', {"orders"}),   # amqp091: routing key
    ('ch.PublishWithContext(ctx, "ex", "order.created", false, false, msg)', {"order.created"}),
    ('w.WriteMessages(ctx, kafka.Message{Key: []byte("k"), Topic: "shipped",\n'
     '\tHeaders: []kafka.Header{{Key: "a", Value: []byte("b")}}})', {"shipped"}),        # kafka-go
    ('w := &kafka.Writer{Addr: kafka.TCP("localhost:9092"), Topic: "orders", Balancer: &kafka.LeastBytes{}}',
     {"orders"}),
    ('kafka.NewWriter(kafka.WriterConfig{Brokers: []string{"b:9092"}, Topic: "orders"})', {"orders"}),
    ('msg := &sarama.ProducerMessage{Topic: "payments", Value: sarama.StringEncoder("x")}', {"payments"}),
    ('cl.Produce(ctx, &kgo.Record{Topic: "audit"}, nil)', {"audit"}),                    # franz-go
])
def test_extract_go_publishers(src, pubs):
    assert _go(src) == (pubs, set())


@pytest.mark.parametrize("src, subs", [
    ('nc.Subscribe("order.cancelled", func(m *nats.Msg) {})', {"order.cancelled"}),     # NATS
    ('nc.QueueSubscribe("orders", "workers", h)', {"orders"}),                          # not the queue group
    ('rdb.Subscribe(ctx, "a", "b")', {"a", "b"}),                                       # go-redis, every channel
    ('ch.Consume("orders", "", false, false, false, false, nil)', {"orders"}),          # amqp091
    ('kafka.NewReader(kafka.ReaderConfig{Brokers: []string{"b:9092"}, Topic: "orders", GroupID: "g"})',
     {"orders"}),                                                                       # kafka-go
    ('kafka.NewReader(kafka.ReaderConfig{\n\tGroupTopics: []string{"a", "b"},\n})', {"a", "b"}),
    ('pc, _ := consumer.ConsumePartition("payments", 0, sarama.OffsetNewest)', {"payments"}),  # sarama
    ('group.Consume(ctx, []string{"payments", "refunds"}, handler)', {"payments", "refunds"}),
    ('c.SubscribeTopics([]string{"a", "b"}, nil)', {"a", "b"}),                         # confluent-kafka-go
    ('c.Subscribe("conf", nil)', {"conf"}),
    ('kgo.NewClient(kgo.SeedBrokers("b:9092"), kgo.ConsumeTopics("a", "b"))', {"a", "b"}),  # franz-go
])
def test_extract_go_subscribers(src, subs):
    assert _go(src) == (set(), subs)


@pytest.mark.parametrize("src", [
    'expvar.Publish("requests", counter)',                    # a metric name, not a topic
    'nc.Publish("orders." + id, data)',                       # concatenated -> not a complete literal
    'kafka.Message{Topic: "orders." + tenant}',
    '// nc.Publish("orders", data)',                          # commented out
    '/* rdb.Subscribe(ctx, "orders") */',
    'nc.Publish(subject, data)',                              # a variable topic is not resolved
    'p.Produce(&kafka.Message{TopicPartition: kafka.TopicPartition{Topic: &topic}}, nil)',  # confluent
    'srv.Publish("/api/orders", d)',                          # a route, not a topic (_valid_topic)
    'js.PullSubscribe("ORDERS.*", "durable")',                # a wildcard subject is no join key
])
def test_extract_go_rejects_non_topics(src):
    assert _go(src) == (set(), set())


def test_go_patterns_skip_other_languages():
    """The capitalised forms only run on Go: C#'s `bus.Publish("orders", m)` is not reliably a broker
    call, and the lowercase patterns that every language gets are unchanged."""
    assert _extract('bus.Publish("orders", m);') == (set(), set())
    assert _extract('var m = new kafka.Message{Topic: "orders"};') == (set(), set())
    assert _go("producer.send('orders', b'x')") == ({"orders"}, set())


def test_stitch_reads_go_patterns_for_go_callables_only(graph):
    """The keyword pre-filter and the Go patterns key off the `go://` id: the same source on a
    non-Go callable mints nothing."""
    w = GraphWriter(graph)
    src = 'func Place(d []byte) error {\n\treturn nc.Publish("order.placed", d)\n}\n'
    w.merge_node(GraphNode("Function", {"id": "go://orders/package.func.Place([]byte)"},
                           {"name": "Place", "source": src}))
    w.merge_node(GraphNode("Method", {"id": "csharp://orders/Bus.Place(byte[])"},
                           {"name": "CsPlace", "source": src}))
    report = stitch(w)
    assert report["topics"] == 1 and report["publishes_edges"] == 1
    assert graph.query("MATCH (f)-[:PUBLISHES_TO]->(:Topic {name:'order.placed'}) RETURN f.name"
                       ).result_set == [["Place"]]


_GO_MOD = "module example.com/orders\n\ngo 1.22\n"
_GO_SRC = '''package main

import (
	"github.com/IBM/sarama"
	"github.com/nats-io/nats.go"
	"github.com/segmentio/kafka-go"
)

var nc *nats.Conn

func PlaceOrder(data []byte) error {
	return nc.Publish("order.placed", data)
}

func WatchCancels() {
	nc.Subscribe("order.cancelled", func(m *nats.Msg) {})
}

func Ship(w *kafka.Writer) error {
	return w.WriteMessages(ctx, kafka.Message{Topic: "order.shipped", Value: []byte("x")})
}

func Charge(p sarama.SyncProducer) {
	p.SendMessage(&sarama.ProducerMessage{Topic: "payments", Value: sarama.StringEncoder("x")})
}
'''
_PY_SRC = '''async def on_placed(nc):
    await nc.subscribe("order.placed", cb=handle)


async def cancel(nc):
    await nc.publish("order.cancelled", b"x")


def track(consumer):
    consumer.subscribe(["order.shipped"])
'''
_JAVA_SRC = '''package com.acme.billing;

import org.springframework.kafka.annotation.KafkaListener;

public class PaymentListener {
    @KafkaListener(topics = "payments")
    public void onPayment(String message) { }
}
'''


def test_go_services_join_python_and_java_in_one_graph(graph, tmp_path):
    """End-to-end over the real Go, Python and Java ingestors, three projects in one graph: a Go NATS
    publisher and subscriber meet Python's nats-py calls, and Go's kafka-go and sarama producers meet a
    Python consumer and a Java @KafkaListener on the shared Topic nodes."""
    w = GraphWriter(graph)
    for name, files in (("orders", {"go.mod": _GO_MOD, "main.go": _GO_SRC}),
                        ("notify", {"notify.py": _PY_SRC}),
                        ("billing", {"PaymentListener.java": _JAVA_SRC})):
        root = tmp_path / name
        root.mkdir()
        for fname, text in files.items():
            (root / fname).write_text(text)
        ingest.ingest_directory(w, str(root), project_name=name, deep="off", auto_install=False)

    def joined(topic):
        return sorted(graph.query(
            "MATCH (p)-[:PUBLISHES_TO]->(:Topic {name: $t})<-[:SUBSCRIBES_TO]-(s) RETURN p.name, s.name",
            {"t": topic}).result_set)

    assert joined("order.placed") == [["PlaceOrder", "on_placed"]]
    assert joined("order.cancelled") == [["cancel", "WatchCancels"]]
    assert joined("order.shipped") == [["Ship", "track"]]
    assert joined("payments") == [["Charge", "onPayment"]]
