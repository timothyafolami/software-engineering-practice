"""Thin Kafka helpers. The broker here is Redpanda; nothing in Topic 6 depends
on Kafka semantics beyond 'a broker that can be stopped', which is exactly the
part the local fallback cannot do."""
import os
from confluent_kafka import Producer, Consumer

BROKER = os.environ.get("BROKER", "redpanda:9092")
TOPIC = os.environ.get("TOPIC", "payment.succeeded")


def producer() -> Producer:
    return Producer({"bootstrap.servers": BROKER,
                     "socket.timeout.ms": 2000,
                     "message.timeout.ms": 3000,
                     "retries": 0,
                     "delivery.timeout.ms": 3000})


def consumer(group: str) -> Consumer:
    c = Consumer({"bootstrap.servers": BROKER, "group.id": group,
                  "auto.offset.reset": "earliest",
                  "enable.auto.commit": True,
                  "socket.timeout.ms": 2000})
    c.subscribe([TOPIC])
    return c
