# auryn-events

Language-agnostic message/event-bus stitch for Auryn: a post-ingestion hook that ties a publisher to a subscriber through the topic/queue they share (publisher -PUBLISHES_TO-> Topic <-SUBSCRIBES_TO- subscriber) across Kafka/RabbitMQ/NATS/Redis-style brokers -- a decoupled connection that has no call edge in the graph. Depends only on auryn-core.

Go services are linked too: NATS/JetStream, go-redis, RabbitMQ (amqp091) and the Kafka clients kafka-go, sarama, confluent-kafka-go and franz-go, so a Go publisher and a Java or Python subscriber meet on one Topic. As in every language, only a string-literal topic is linked; a topic held in a variable (confluent-kafka-go's producer always takes `Topic: &topic`) is not.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install falkordb-auryn                  # Auryn with every plugin
pip install "auryn-core[events]"            # Auryn with only this plugin
```

## License

MIT. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/packages/events/LICENSE).
