# Distributed Deep Dives

Technical deep dives into distributed systems, schema governance, and ML pipelines. Each article is rooted in real engineering experience — not tutorials. They reference actual code from the [reddit-realtime-classification](https://github.com/carlesarnal/reddit-realtime-classification) pipeline, contributions to [Apicurio Registry](https://www.apicur.io/registry/), and production architecture decisions.

Every article includes runnable examples so you can reproduce the scenarios discussed.

## Articles

### [Why Schema References Break in Avro](schema-references-avro/)

Avro schema references promise reusable types across schemas. In practice, they break in subtle ways — circular dependencies, version pinning drift, namespace collisions, and registry-specific resolution differences. Covers what I learned contributing to Apicurio Registry's schema handling internals.

**Topics:** Avro named types, Confluent vs Apicurio reference resolution, content canonicalization, compatibility checking with references

### [Designing Schema Evolution for Event-Driven Systems](schema-evolution-events/)

Schema evolution is the hardest coordination problem in event-driven architectures. Covers compatibility modes in practice (not just definitions), common mistakes with code examples, and real-world evolution patterns from the reddit pipeline's Kafka topics.

**Topics:** BACKWARD/FORWARD/FULL compatibility, JSON Schema evolution, Apicurio Registry governance, expand-and-contract pattern, multi-team schema ownership

### [Real-Time vs. Batch ML Inference: Engineering Tradeoffs](realtime-vs-batch-inference/)

The inference spectrum isn't binary. Covers why micro-batch was the right choice for this pipeline, the engineering reality of dual-model inference in PySpark UDFs, production concerns nobody talks about (checkpoint traps, OTel tracing in executors, model drift detection), and what changes at 100x scale.

**Topics:** Spark Structured Streaming, dual-model agreement rate, PySpark UDF serialization, OpenTelemetry tracing challenges, backpressure design, cost-accuracy tradeoffs

## Project Structure

```
distributed-deep-dives/
├── README.md
├── schema-references-avro/
│   ├── README.md                              # Full article
│   └── examples/
│       ├── broken-circular-ref.avsc           # Circular dependency failure
│       ├── broken-version-pin.avsc            # Version pinning drift
│       ├── fixed-self-contained.avsc          # Self-contained design pattern
│       └── register-order.sh                  # Bottom-up registration script
├── schema-evolution-events/
│   ├── README.md                              # Full article
│   └── examples/
│       ├── reddit-stream-v1.json              # Original pipeline schema
│       ├── reddit-stream-v2-valid.json        # Valid BACKWARD evolution
│       ├── reddit-stream-v2-invalid.json      # Invalid evolution (breaks compatibility)
│       ├── kafka-predictions-v2.json          # Adding a third model
│       └── validate-compatibility.sh          # Apicurio compatibility check script
├── realtime-vs-batch-inference/
│   ├── README.md                              # Full article
│   └── examples/
│       ├── dual-model-udf.py                  # Annotated dual-model UDF pattern
│       ├── backpressure-config.py             # Spark streaming backpressure config
│       └── model-serving-migration.py         # Refactor to model serving endpoints
└── claudedocs/                                # Planning documents
```

## Related Projects

- **[reddit-realtime-classification](https://github.com/carlesarnal/reddit-realtime-classification)** — The ML pipeline referenced throughout these articles
- **[Apicurio Registry](https://github.com/Apicurio/apicurio-registry)** — The schema registry I contribute to at IBM
- **[apicurio-registry-support-chat](https://github.com/carlesarnal/apicurio-registry-support-chat)** — RAG chatbot using registry artifacts as the knowledge backend

## Author

**Carles Arnal** — Principal Software Engineer at IBM, Barcelona. Core contributor to Apicurio Registry.

[GitHub](https://github.com/carlesarnal) · [Website](https://carlesarnal.github.io)
