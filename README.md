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

### [Building a RAG Chatbot with Schema Registry as the Knowledge Backend](rag-with-schema-registry/)

Using Apicurio Registry to store versioned prompt templates as PROMPT_TEMPLATE artifacts, then feeding them to a RAG chatbot. A unique intersection of schema governance and LLM tooling — prompt versioning, rollback, and A/B testing using the same infrastructure you use for Kafka schemas.

**Topics:** LangChain4j, Ollama, RAG pipeline, prompt template versioning, embedding model selection, document chunking strategies

### [MCP Servers for Domain-Specific AI Tooling](mcp-servers-domain-ai/)

Building an MCP (Model Context Protocol) server that wraps Apicurio Registry for AI agent use. Designing the tool surface, security considerations, and patterns for wrapping any REST API as an MCP server.

**Topics:** MCP protocol, AI agent tooling, tool surface design, security for AI operations, REST-to-MCP patterns

### [Model Agreement as a Proxy for Ground Truth in Streaming ML](model-agreement-ground-truth/)

When you deploy ML models in a streaming pipeline, you don't have labels. Dual-model agreement rate, disagreement taxonomy, confidence calibration, and drift detection — all without ground truth.

**Topics:** Dual-model monitoring, agreement rate, disagreement zones, confidence calibration, model drift detection, Prometheus alerting

## Project Structure

```
distributed-deep-dives/
├── README.md
├── schema-references-avro/
│   ├── README.md
│   └── examples/
├── schema-evolution-events/
│   ├── README.md
│   └── examples/
├── realtime-vs-batch-inference/
│   ├── README.md
│   └── examples/
├── rag-with-schema-registry/
│   ├── README.md
│   └── examples/
├── mcp-servers-domain-ai/
│   ├── README.md
│   └── examples/
└── model-agreement-ground-truth/
    ├── README.md
    └── examples/
```

## Related Projects

- **[reddit-realtime-classification](https://github.com/carlesarnal/reddit-realtime-classification)** — The ML pipeline referenced throughout these articles
- **[Apicurio Registry](https://github.com/Apicurio/apicurio-registry)** — The schema registry I contribute to at IBM
- **[apicurio-registry support-chat](https://github.com/Apicurio/apicurio-registry/tree/main/support-chat)** — RAG chatbot using registry artifacts as the knowledge backend

## Author

**Carles Arnal** — Principal Software Engineer at IBM, Barcelona. Core contributor to Apicurio Registry.

[GitHub](https://github.com/carlesarnal) · [Website](https://carlesarnal.github.io)
