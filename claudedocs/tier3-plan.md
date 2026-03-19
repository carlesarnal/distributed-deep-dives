# Tier 3 Implementation Plan — Differentiator Phase

## Overview

Tier 3 has three workstreams that are deeply interconnected. The observability and failure handling work produces the real-world experience that feeds the deep-dive articles. The articles in turn document the *why* behind the engineering decisions, which is the actual differentiator for Staff+/L5 positioning.

**Execution order matters.** You can't write credible deep dives about failure handling if you haven't implemented it. You can't show observability screenshots if Grafana isn't wired up.

---

## Workstream A: Observability (reddit-realtime-classification)

### Why this matters
Right now the pipeline has Prometheus counters and 7 HTML dashboards, but no distributed tracing, no Grafana, and no way to trace a single Reddit post through Producer -> Kafka -> Spark -> Consumer. For a Staff engineer, observability isn't optional — it's how you prove you think about production.

### A1. OpenTelemetry Tracing — DONE (commit 7feeacf)

**Scope:** Add distributed tracing across all 3 components so a single post can be traced end-to-end.

**Components to instrument:**
| Component | Language | OTel approach |
|-----------|----------|---------------|
| Python Producer | Python | `opentelemetry-instrumentation-kafka-python` + manual spans for Reddit API calls |
| Spark Inference | PySpark | Manual spans around dual-model prediction UDF (OTel SDK, not auto-instrumentation — Spark UDFs are tricky) |
| Quarkus Consumer | Java | `quarkus-opentelemetry` extension (auto-instruments Kafka consumer + REST endpoints) |

**Trace propagation strategy:**
- Inject W3C `traceparent` header into Kafka record headers in the Producer
- Extract in Spark job, create child spans for each model inference
- Quarkus auto-extracts from Kafka headers via SmallRye

**Collector:**
- Deploy Jaeger (all-in-one) or OpenTelemetry Collector in `reddit-realtime` namespace
- K8s manifest: Deployment + Service
- Configure all 3 components to export to `http://jaeger-collector.reddit-realtime.svc:4317` (OTLP/gRPC)

**Tasks:**
1. Add `quarkus-opentelemetry` dependency to flair-consumer pom.xml
2. Add OTel SDK to Python producer (pip install + Dockerfile update)
3. Implement trace context propagation via Kafka headers in producer
4. Add manual spans in Spark inference job (wrap model prediction in spans)
5. Deploy Jaeger all-in-one to K8s
6. Verify end-to-end trace for a single post
7. Take screenshots for README

**Estimated complexity:** Medium-High. The Spark UDF tracing is the hardest part — PySpark UDFs execute on executors, not the driver, so OTel context must be serialized carefully.

**Risk:** Spark executor serialization of OTel context. Fallback: trace only Producer and Consumer (still valuable), log correlation IDs in Spark.

### A2. Grafana Dashboards — DONE (commit 7f0567b)

**Scope:** Replace the 7 custom HTML dashboards with proper Grafana dashboards backed by Prometheus.

**Components:**
1. Deploy Prometheus (or use existing K8s Prometheus if available)
2. Deploy Grafana with pre-configured dashboards (JSON provisioning)
3. Create 3-4 dashboards:
   - **Pipeline Overview**: message throughput, end-to-end latency, error rate
   - **Model Comparison**: agreement rate over time, per-flair confidence, confusion matrix heatmap
   - **System Health**: consumer lag, Spark job duration, producer API call latency
   - **Model Drift**: flair distribution changes, confidence score trends

**Approach:**
- Use `kube-prometheus-stack` Helm chart (bundles Prometheus + Grafana)
- Or lightweight: standalone Prometheus + Grafana Deployments
- Export Grafana dashboard JSON files into `runtime/monitoring/` for version control
- The existing Quarkus `/q/metrics` endpoint already exposes Prometheus counters — no code changes needed for basic metrics

**Decision to make:** Keep the HTML dashboards as a lightweight alternative, or remove them? Recommendation: keep them — they work without Prometheus/Grafana and are useful for demos.

**Tasks:**
1. Choose deployment approach (Helm chart vs standalone)
2. Deploy Prometheus + Grafana to K8s
3. Configure Prometheus to scrape Quarkus metrics endpoint
4. Design and build 3-4 Grafana dashboards
5. Export dashboard JSON to repo for version control
6. Add Grafana screenshots to README
7. Document setup in README

**Estimated complexity:** Medium. Mostly configuration, no application code changes.

### A3. Custom Metrics Enhancement (prerequisite for good dashboards) — DONE (commit d826d65)

Before Grafana dashboards are useful, enhance the Prometheus metrics in the Quarkus consumer:

- Add `flair_processing_latency_seconds` histogram (how long each message takes to process)
- Add `kafka_consumer_lag` gauge (if not auto-provided by SmallRye)
- Add `model_agreement_rate` gauge (computed rolling average)
- Add `pipeline_messages_total` counter with labels for stage (produced/inferred/consumed)

**Tasks:**
1. Add histogram and gauge metrics to BaseResource
2. Expose processing time per message
3. Test metrics show up at `/q/metrics`

**Estimated complexity:** Low

---

## Workstream B: Failure Handling (reddit-realtime-classification)

### Why this matters
The current pipeline has zero failure handling. If Spark inference fails on a message, it's lost. If the consumer can't parse a prediction, it logs an error and moves on. No DLQ, no retries, no circuit breakers. This is the #1 thing a Staff interviewer will probe on.

### B1. Dead Letter Queue (DLQ) — DONE (commit 78baed7)

**Scope:** Messages that fail processing should go to a DLQ topic instead of being silently dropped.

**Where DLQ applies:**
| Component | Failure mode | DLQ strategy |
|-----------|-------------|--------------|
| Quarkus Consumer | Malformed JSON, missing fields | SmallRye `failure-strategy=dead-letter-queue` (built-in) |
| Spark Inference | Model exception, OOM on large text | Write failed records to `reddit-predictions-dlq` topic |
| Producer | Reddit API errors | Already has retry logic; add DLQ for persistent failures |

**Tasks:**
1. **Consumer DLQ** (easiest — SmallRye has built-in support):
   - Add to `application.properties`: `mp.messaging.incoming.kafka-predictions.failure-strategy=dead-letter-queue`
   - Configure DLQ topic name: `mp.messaging.incoming.kafka-predictions.dead-letter-queue.topic=predictions-dlq`
   - Create the DLQ KafkaTopic YAML
2. **Spark DLQ**:
   - Wrap the `dual_model_prediction` UDF in try/except
   - On failure, write to `reddit-inference-dlq` topic with the original message + error reason
   - Create the DLQ KafkaTopic YAML
3. **Producer DLQ**:
   - After N retries, write failed posts to a local file or DLQ topic
4. Add a simple DLQ consumer/viewer (optional — could be a Quarkus endpoint that reads from DLQ topics)

**Estimated complexity:** Low-Medium. SmallRye DLQ is config-only. Spark DLQ requires UDF modification.

### B2. Circuit Breaker / Backpressure — DONE (commit bf8a8a5)

**Scope:** Prevent cascading failures when downstream systems are slow or down.

**Where it applies:**
| Component | Scenario | Strategy |
|-----------|----------|----------|
| Producer | Reddit API rate limit / down | Already has sleep-retry; add exponential backoff with jitter |
| Spark | Kafka sink slow | Spark Structured Streaming has built-in backpressure via `maxOffsetsPerTrigger` |
| Consumer | Processing overload | SmallRye reactive messaging has built-in backpressure (pull-based) |

**Tasks:**
1. **Producer**: Replace fixed `time.sleep(60)` with exponential backoff (1s, 2s, 4s, 8s... capped at 5min)
2. **Spark**: Add `maxOffsetsPerTrigger` option to limit batch size
3. **Consumer**: Already pull-based via SmallRye — document this in ADR
4. **Registry unavailable**: Add retry/fallback in model-metadata module when Apicurio Registry is down

**Estimated complexity:** Low

### B3. Architecture Decision Records (ADRs) — DONE (commit 98dbdab)

**Scope:** Document the *why* behind failure handling and observability decisions.

**ADRs to write:**
1. `adr-001-dual-model-inference.md` — Why two models? What does agreement rate tell us?
2. `adr-002-dlq-strategy.md` — Why DLQ over retry-forever? Per-component DLQ design
3. `adr-003-backpressure-design.md` — How each component handles overload
4. `adr-004-schema-governance.md` — Why Apicurio Registry for Kafka topic schemas
5. `adr-005-observability-strategy.md` — OTel tracing + Prometheus metrics + Grafana approach

**Format:** Use Michael Nygard's ADR format (Status, Context, Decision, Consequences).

**Location:** `docs/adr/` in the reddit-realtime-classification repo.

**Estimated complexity:** Low (writing, not coding), but high value.

---

## Workstream C: distributed-deep-dives Technical Articles

### Why this matters
This is the thought leadership play. The articles position you as someone who *understands* these systems deeply, not just someone who *uses* them. This is the difference between "I set up Kafka" and "I understand why schema references break in Avro and how to design around it."

### Content Strategy

These articles should be **rooted in real experience**, not generic tutorials. Each one should reference your actual work (Apicurio contributions, reddit pipeline, production debugging).

### Article 1: "Why Schema References Break in Avro" — DONE (commit 33eeb40)

**Target audience:** Engineers using Avro + Schema Registry who hit reference resolution failures.

**Outline:**
1. The problem: Avro `$ref` / named types across schemas
2. How schema registries resolve references (Confluent vs Apicurio approach)
3. The edge cases that break: circular refs, cross-group refs, version pinning
4. What I learned contributing to Apicurio Registry (cite specific PRs/issues)
5. Practical recommendations

**Research needed:**
- Gather your Apicurio PRs/issues related to schema references
- Review Confluent Schema Registry's reference handling for comparison
- Test specific failure scenarios and document them

**Estimated effort:** 2-3 focused writing sessions

### Article 2: "Designing Schema Evolution for Event Systems" — DONE (commit 33eeb40)

**Target audience:** Engineers building event-driven architectures who need to evolve schemas without breaking consumers.

**Outline:**
1. The compatibility modes (BACKWARD, FORWARD, FULL) — what they actually mean in practice
2. Common mistakes: adding required fields, changing types, removing defaults
3. Schema evolution strategy for the reddit pipeline (reddit-stream and kafka-predictions topics)
4. How Apicurio Registry enforces compatibility rules
5. When to break compatibility (and how to manage the migration)
6. Multi-schema governance patterns for teams

**Research needed:**
- Document the actual schemas from the reddit pipeline
- Show examples of valid and invalid evolutions
- Reference the Apicurio compatibility rule configuration

**Estimated effort:** 2-3 writing sessions

### Article 3: "Real-time vs Batch ML Inference: Tradeoffs at Scale" — DONE (commit 33eeb40)

**Target audience:** ML engineers and platform engineers deciding between real-time and batch inference.

**Outline:**
1. The tradeoff space: latency vs throughput vs cost vs complexity
2. The reddit pipeline as a case study: why Spark Structured Streaming (micro-batch) is a middle ground
3. Dual-model inference: comparing Transformer (GPU-hungry, high accuracy) vs sklearn (CPU-only, fast)
4. When real-time matters: model freshness, user experience, feedback loops
5. Production concerns: model serving infrastructure, model versioning, rollback
6. What I'd change if building this at 100x scale

**Research needed:**
- Gather actual latency/throughput numbers from the pipeline
- Compare with alternative architectures (TensorFlow Serving, Triton, KServe)
- Reference your model-metadata schema validation work

**Estimated effort:** 3-4 writing sessions (most complex article)

### Potential Article 4 (bonus): "Building a RAG Chatbot with Schema Registry as the Knowledge Backend"

Based on your apicurio-registry-support-chat project. This is unique — nobody else has done this.

**Outline:**
1. Why store prompt templates in a schema registry (versioning, governance)
2. LangChain4j + Ollama + Apicurio integration
3. RAG document ingestion from web documentation
4. Production lessons: session management, embedding model choice

**Estimated effort:** 2 writing sessions

### Repo Structure for distributed-deep-dives

```
distributed-deep-dives/
├── README.md                    # Index of all articles with summaries
├── claudedocs/                  # Planning docs (existing)
├── schema-references-avro/
│   ├── README.md                # The article
│   └── examples/                # Runnable code examples showing breakage/fixes
├── schema-evolution-events/
│   ├── README.md
│   └── examples/
├── realtime-vs-batch-inference/
│   ├── README.md
│   └── examples/
└── rag-with-schema-registry/    # Optional 4th article
    ├── README.md
    └── examples/
```

Each article directory includes runnable examples so readers can reproduce the scenarios. This is what separates a blog post from a deep dive.

---

## Execution Order

```
Phase 1 (Week 1-2): Observability foundations
├── A3: Enhance Prometheus metrics in Quarkus consumer ✅ DONE
├── A2: Deploy Prometheus + Grafana, build dashboards ✅ DONE
└── A1: Add OpenTelemetry tracing (Producer + Consumer first, Spark later) ✅ DONE

Phase 2 (Week 3-4): Failure handling
├── B1: DLQ for Consumer (config-only) and Spark (UDF change) ✅ DONE
├── B2: Backpressure config (Spark maxOffsetsPerTrigger, Producer exponential backoff) ✅ DONE
└── B3: Write ADRs for all decisions made in Phase 1 and 2 ✅ DONE

Phase 3 (Week 5-8): Deep dive articles
├── C1: "Why Schema References Break in Avro" (draw from Apicurio experience) ✅ DONE
├── C2: "Designing Schema Evolution for Event Systems" (reference reddit pipeline schemas) ✅ DONE
├── C3: "Real-time vs Batch ML Inference" (reference pipeline metrics from Phase 1) ✅ DONE
└── C4: (Optional) "RAG with Schema Registry"

Phase 4 (Week 8): Polish
├── Update reddit-realtime-classification README ✅ DONE
├── Update distributed-deep-dives README as article index ✅ DONE
├── Update GitHub profile README to link deep dives
├── Build personal website (carlesarnal.github.io) with deep dive blog ✅ DONE
└── Update hiring signal mapping in plan doc
```

---

## Dependencies

```
A3 (metrics) ──> A2 (Grafana dashboards) ──> C3 (inference article needs metrics)
A1 (tracing) ──────────────────────────────> C3 (latency data for article)
B1 (DLQ) ──> B3 (ADRs) ──> C2 (schema evolution article references ADRs)
B2 (backpressure) ──> B3 (ADRs)
Apicurio PRs research ──> C1 (schema references article)
Reddit pipeline schemas ──> C2 (schema evolution article)
```

---

## Risk Register

| Risk | Impact | Mitigation |
|------|--------|------------|
| Spark OTel tracing too complex | Incomplete end-to-end traces | Trace Producer + Consumer only; use correlation IDs in Spark logs |
| Grafana setup takes too long on Minikube | Delayed dashboards | Use lightweight standalone Grafana (no Helm chart) |
| Articles feel generic / tutorial-like | No differentiation | Root every article in specific Apicurio PRs, reddit pipeline code, or production incidents |
| Scope creep on failure handling | Never-ending polish | Timebox: DLQ + backpressure only. No retry frameworks, no Saga patterns |
| Pipeline not running (can't gather real metrics) | Articles lack data | Use synthetic load for metrics; document expected vs actual |

---

## Success Criteria

After Tier 3, your GitHub profile should answer these questions for any visitor:

1. **"What does this person build?"** — The reddit pipeline with full observability and failure handling
2. **"Do they understand production?"** — ADRs + DLQ + tracing say yes
3. **"Can they communicate technical depth?"** — 3 deep-dive articles with runnable examples
4. **"Do they think at scale?"** — Metrics dashboards, backpressure design, architecture tradeoffs
5. **"Are they a domain expert?"** — Schema references article leverages real Apicurio contributions
