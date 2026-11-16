# auryn-events

Language-agnostic message/event-bus stitch for Auryn: a post-ingestion hook that ties a publisher to a subscriber through the topic/queue they share (publisher -PUBLISHES_TO-> Topic <-SUBSCRIBES_TO- subscriber) across Kafka/RabbitMQ/NATS/Redis-style brokers -- a decoupled connection that has no call edge in the graph. Depends only on auryn-core.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install auryn-events
```

To install Auryn with every language plugin, use `pip install falkordb-auryn`.

## License

SSPL-1.0. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/LICENSE).
