# Future Deep Dive Ideas

Ranked by differentiation potential — how uniquely Carles's experience positions each article.

---

## Tier A: High Differentiation (nobody else has this angle)

### 1. "Building a RAG Chatbot with Schema Registry as the Knowledge Backend" -- DONE

**Source project:** [apicurio-registry/support-chat](https://github.com/Apicurio/apicurio-registry/tree/main/support-chat)

Nobody else has done this — using a schema registry to store versioned prompt templates as registry artifacts, then feeding them to a RAG chatbot. This is a unique intersection of schema governance and LLM tooling.

**Outline:**
- Why store prompt templates in a schema registry (versioning, governance, rollback)
- LangChain4j + Ollama + Apicurio integration architecture
- RAG document ingestion from Apicurio's web documentation
- Prompt template evolution: how to version prompts like schemas
- Session management and embedding model choice
- When this pattern makes sense vs. just hardcoding prompts

**Unique angle:** Schema registries as a general-purpose artifact store, not just for Kafka schemas.

**Effort:** Medium (2-3 sessions). Project already exists.

---

### 2. "MCP Servers for Domain-Specific AI Tooling: Lessons from Apicurio Registry" -- DONE

**Source:** The Apicurio Registry MCP server used in this project.

MCP (Model Context Protocol) is new and underexplored. Building an MCP server that wraps a schema registry for AI agent use is a novel contribution.

**Outline:**
- What MCP is and why it matters for domain-specific AI agents
- Designing the tool surface: which registry operations to expose to an LLM
- Schema search, validation, and compatibility checking as AI tools
- Prompt templates stored as registry artifacts and rendered via MCP
- Security considerations: what an AI agent should and shouldn't be able to do with your registry
- Patterns for wrapping any REST API as an MCP server

**Unique angle:** Practical MCP server building from real experience, not a toy example.

**Effort:** Medium (2-3 sessions).

---

### 3. "Model Agreement as a Proxy for Ground Truth in Streaming ML" -- DONE

**Source:** reddit-realtime-classification dual-model architecture, ADR-001.

Most ML content focuses on metrics that require ground truth labels. This article explores what you can learn when you don't have labels — using model agreement, confidence gap analysis, and disagreement patterns as signals.

**Outline:**
- The ground truth problem in streaming: you can't wait for labels
- Agreement rate: what it tells you and what it doesn't
- Disagreement taxonomy: both uncertain, one confident, both confident but different
- Per-category analysis: which flairs are genuinely ambiguous vs. model-specific weaknesses
- Confidence calibration: when 0.8 confidence doesn't mean 80% accuracy
- Building alerting on agreement rate drops (model drift proxy)
- When to stop running both models (decision framework)

**Unique angle:** Practical ML monitoring without ground truth, from a systems engineer's perspective (not ML researcher).

**Effort:** Medium (2-3 sessions). Data and code already exist.

---

### 4. "Content Canonicalization: The Hardest Problem in Schema Registries"

**Source:** Apicurio Registry contributions.

When a registry checks compatibility between two schema versions, it must normalize them to a canonical form first. For Avro, this means sorting fields, resolving aliases, expanding references. For JSON Schema, it means handling `$ref` resolution, `allOf` merging, and keyword ordering. This is an unsolved problem in the general case.

**Outline:**
- Why canonicalization matters: the same logical schema can have many textual representations
- Avro canonicalization: the parsing canonical form (PCF) spec and its gaps
- JSON Schema canonicalization: no standard exists — what Apicurio does
- Protobuf canonicalization: descriptor normalization challenges
- Edge cases: `additionalProperties`, default value handling, documentation fields
- Performance: canonicalization on every version registration vs. caching strategies
- The tradeoff between strict normalization and human readability

**Unique angle:** Deep internals of schema registry engineering, from a contributor who worked on this code.

**Effort:** High (3-4 sessions). Requires careful research into Apicurio internals.

---

## Tier B: Strong Differentiation (real experience, popular topic)

### 5. "OpenTelemetry in Polyglot Pipelines: Python, PySpark, and Java"

**Source:** reddit-realtime-classification OTel instrumentation, ADR-005.

Most OTel content covers a single language. This article covers the reality of instrumenting a pipeline that spans Python (producer), PySpark (inference), and Java/Quarkus (consumer) — each with different OTel approaches and different problems.

**Outline:**
- The three OTel approaches: manual (Python), manual-in-UDF (Spark), auto (Quarkus)
- W3C TraceContext propagation via Kafka headers: inject in Python, extract in Java
- The PySpark UDF tracing gap: why spans don't inherit parent context across driver/executor
- Correlation strategies when parent-child spans aren't possible
- Jaeger vs. OpenTelemetry Collector: when to use which
- Span attributes as the bridge between metrics and traces
- What I wish I'd known before instrumenting a polyglot pipeline

**Unique angle:** Real cross-language OTel experience with Kafka as the propagation medium.

**Effort:** Medium (2-3 sessions).

---

### 6. "Dead Letter Queues: Three Strategies for Three Failure Modes"

**Source:** reddit-realtime-classification DLQ implementation, ADR-002.

The pipeline uses three different DLQ strategies: SmallRye built-in (consumer), UDF flag splitting (Spark), and local file (producer). Each was chosen for a specific reason. Most DLQ content shows one pattern — this shows why one size doesn't fit all.

**Outline:**
- Why "just use a DLQ" is too simplistic
- Strategy 1: Framework-managed DLQ (SmallRye — zero code, config-only)
- Strategy 2: Application-level DLQ routing (Spark — UDF flag + DataFrame split)
- Strategy 3: Local file DLQ (when Kafka itself is the thing that's down)
- DLQ schema design: what metadata to include for debugging
- DLQ consumer patterns: alerting, replay, dead letter dashboard
- The DLQ anti-pattern: using DLQ as a permanent error store instead of fixing root causes

**Unique angle:** Comparative analysis of three strategies in the same pipeline, with clear rationale for each.

**Effort:** Low-Medium (1-2 sessions). Code and ADR already exist.

---

### 7. "Kafka Schema Validation at the Proxy Layer with Kroxylicious"

**Source:** [kroxylicious-schema-validation](https://github.com/carlesarnal/kroxylicious-schema-validation)

Kroxylicious is a Kafka proxy that can intercept and transform messages. Using it for schema validation means enforcing schemas at the broker level without changing producers or consumers — a different approach than client-side SerDes.

**Outline:**
- The schema enforcement gap: registries store schemas, but who enforces them?
- Client-side enforcement (SerDes) vs. proxy-side enforcement (Kroxylicious)
- Building a schema validation filter: architecture and implementation
- Performance impact: what proxy-level validation costs in latency
- Failure handling: reject, pass-through, or DLQ on validation failure?
- When proxy enforcement makes sense vs. client enforcement

**Unique angle:** Kroxylicious is niche and underexplored. This would be one of very few articles on proxy-level schema enforcement.

**Effort:** Medium (2-3 sessions).

---

## Tier C: Good Topics (solid content, more competitive space)

### 8. "Architecture Decision Records: The Missing Engineering Practice"

**Source:** 5 ADRs written for reddit-realtime-classification.

A meta-article about ADRs as an engineering practice — why most teams don't write them, what makes a good ADR, and how they signal engineering maturity to hiring committees.

**Outline:**
- What ADRs are and why they matter more than design docs
- The Michael Nygard format and when to deviate from it
- Five ADRs from the reddit pipeline as case studies
- ADRs as interview artifacts: showing your decision-making process
- Common ADR anti-patterns: too verbose, too vague, not maintained
- How to introduce ADRs to a team that doesn't write them

**Effort:** Low (1-2 sessions).

---

### 9. "Strimzi vs. Confluent for Kafka on Kubernetes: An Operator Comparison"

**Source:** reddit-realtime-classification uses Strimzi.

Practical comparison from someone who runs Strimzi in a real pipeline, not a vendor comparison matrix.

**Outline:**
- Operator philosophy differences: Strimzi (CRDs for everything) vs. Confluent (Helm + CRDs)
- KRaft mode support: where each operator stands
- Topic management: KafkaTopic CRD vs. CLI/API
- Security: mTLS, SCRAM, OAuth — what each operator makes easy
- Monitoring integration: JMX, Prometheus, metrics exposure
- The upgrade experience: operator versions, Kafka versions, rolling updates
- When to use Strimzi, when to use Confluent, when to use neither

**Effort:** Medium-High (3 sessions). Requires hands-on Confluent Operator testing for fair comparison.

---

### 10. "Dynamic Configuration in Quarkus: Beyond application.properties"

**Source:** [dynamic-config-properties](https://github.com/carlesarnal/dynamic-config-properties)

Quarkus configuration is powerful but opinionated. This article covers the edge cases: runtime vs. build-time properties, config sources, and dynamic reconfiguration patterns.

**Effort:** Low (1-2 sessions). Narrower audience.

---

## Suggested Next Batch (3 articles)

Based on differentiation and effort, the recommended next three are:

1. **"Building a RAG Chatbot with Schema Registry as the Knowledge Backend"** — Unique, project exists, high interest topic (RAG + LLM)
2. **"MCP Servers for Domain-Specific AI Tooling"** — Bleeding edge, positions as early adopter
3. **"Model Agreement as a Proxy for Ground Truth"** — Bridges ML and systems engineering, unique perspective

These three complement the existing articles by expanding from schema/infra into AI/ML tooling territory while staying rooted in real projects.
