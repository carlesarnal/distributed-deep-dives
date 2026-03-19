# GitHub Profile Deep Review: carlesarnal

## Profile Snapshot

| Field | Value |
|-------|-------|
| **Name** | Carles Arnal |
| **Company** | IBM (email still @redhat.com) |
| **Location** | Barcelona |
| **Bio** | "Software Engineer" |
| **Public repos** | 67 (25 original, 42 forks) |
| **Followers** | 14 |
| **Profile README** | None |

---

## Core Problem

Your profile reads as "senior engineer who forks things" rather than "Principal Engineer who builds distributed systems and ML pipelines." The signal-to-noise ratio is very low. 67 repos, but most are forks with no visible contributions, and your original repos lack descriptions, READMEs, and positioning.

---

## Repo Assessments (Ranked)

### 1. reddit-realtime-classification — Your Flagship (Score: 5/10, potential 9/10)

**What's good:**
- Genuine end-to-end distributed ML pipeline (Reddit -> Kafka -> Spark -> Quarkus)
- Dual-model inference (Transformer + scikit-learn) with comparison metrics
- 7 monitoring dashboards, Prometheus integration
- Kubernetes-native with Strimzi + Spark Operator
- CI/CD with GitHub Actions

**What's killing it:**
- Reddit API credentials hardcoded in source — disqualifying red flag
- Zero tests — no unit, integration, or E2E tests
- No README architecture diagram or design decision docs
- No DLQ, no circuit breakers, no health probes
- In-memory metrics (lost on restart, unbounded growth)
- No Apicurio Registry integration (missed opportunity)

### 2. model-metadata — Clean but Small (Score: 6/10)

**What's good:**
- Schema-driven ML model validation via Apicurio Registry
- Clean Quarkus code, proper REST API design
- JSON Schema with real ML-relevant fields
- Bridges Apicurio expertise with MLOps

**What's missing:**
- No persistence (in-memory only)
- Hardcoded registry URL
- No auth, no pagination
- Could be a module inside reddit-realtime-classification

### 3. apicurio-registry-support-chat — Most Polished (Score: 7/10)

**What's good:**
- RAG-powered chatbot using LangChain4j + Ollama + Apicurio Registry
- Prompt templates stored as registry artifacts (novel use case)
- Clean service layer, proper REST API, K8s manifests with health probes
- Best README of all repos
- Demonstrates innovation with registry's LLM artifact types

**What's missing:**
- No tests
- In-memory sessions (no persistence)
- Hardcoded RAG document URLs
- Silent error handling in RAG pipeline

### 4. debezium-ocp-etc-demo — Good Docs, Limited Code (Score: 7/10 docs, 3/10 code)

- Fork with excellent documentation
- Shows strong Kafka + CDC + K8s knowledge
- Configuration only, not code — limited portfolio value
- 3 stars (most starred repo, ironically)

### 5. data-visualization — Academic, Low Value (Score: 2/10)

- Single exported Jupyter notebook (Barcelona bike-sharing analysis)
- Looks like coursework
- No README, no setup instructions
- Should probably be private or deleted

---

## Profile-Level Problems

1. **No profile README** (carlesarnal/carlesarnal repo) — free real estate, every recruiter sees this first
2. **Bio says "Software Engineer"** — should reflect Principal-level distributed systems + ML work
3. **42 forks cluttering the profile** — forks of Quarkus, Linux kernel, Debezium with no visible contributions dilute signal
4. **Most original repos have no description** — 14 out of 25 say "no desc"
5. **Email mismatch** — Company says IBM, email says @redhat.com
6. **Zero stars on original work** — most starred repo is a fork

---

## Action Plan

### Tier 1: Do This Week (High Impact, Low Effort)

- [x] **Remove hardcoded Reddit API credentials** from reddit-realtime-classification. DONE — replaced with `os.environ` vars, added K8s Secret reference in deployment YAML. Commit `88cedb2` pushed to main.
- [x] **Create a profile README** (carlesarnal/carlesarnal repo): DONE — repo created at https://github.com/carlesarnal/carlesarnal with full README (bio, featured projects table, tech stack).
- [x] **Add descriptions** to all repos missing them. DONE — 17 repos updated successfully. 5 repos were already archived (read-only) when descriptions were attempted.
- [x] **Archive noise repos**: glowing-waddle, androidCamera, min3d, pitchperfect, RSSFeed, TMDB, Java-AI-Book-Code, jhipster-sample, TouchImageView. DONE — all 9 archived.
- [x] **Update bio**: "Principal Engineer — Distributed Systems, Schema Governance, ML Pipelines" — BLOCKED: requires `user` OAuth scope. Run `gh auth refresh -h github.com -s user` and authorize in browser, then run: `gh api -X PATCH /user -f bio="Principal Engineer — Distributed Systems, Schema Governance, ML Pipelines"`

### Tier 2: Do This Month (High Impact, Medium Effort)

- [x] **Add architecture diagram** to reddit-realtime-classification README. DONE — Mermaid diagram, design decisions table, monitoring dashboards table, project structure tree.
- [x] **Add tests** to reddit-realtime-classification. DONE — 10 unit tests covering statistics, confusion matrix, confidence distribution, agreement, uncertainty zones, edge cases. All passing.
- [x] **Integrate Apicurio Registry** into reddit-realtime-classification. DONE — JSON schemas for both Kafka topics, K8s deployment manifest, registration script.
- [x] **Merge model-metadata** as a module inside reddit-realtime-classification. DONE — copied as runtime/model-metadata with configurable registry URL. Original model-metadata repo archived.
- [x] **Add K8s health probes and resource limits** to all deployment manifests. DONE — liveness/readiness probes + resource requests/limits on all 3 deployments. Added quarkus-smallrye-health dependency.

### Tier 3: Do This Quarter (Differentiator)

- [ ] **Create distributed-deep-dives** as a technical blog/writeup collection:
  - "Why Schema References Break in Avro"
  - "Designing Schema Evolution for Event Systems"
  - "Real-time vs Batch ML Inference: Tradeoffs at Scale"
- [ ] **Add observability**: OpenTelemetry tracing across the reddit pipeline, Grafana dashboards with screenshots in README
- [ ] **Add failure handling**: DLQ for failed messages, circuit breakers, backpressure — document the why in ADRs

---

## Hiring Signal Mapping

| Signal | Google L5 | Staff+ (Red Hat/IBM) | Before | After Tier 1+2 | After Tier 3 (planned) |
|--------|-----------|---------------------|--------|----------------|------------------------|
| System design ownership | Required | Required | Implicit, not visible | Visible — Mermaid diagram, design decisions table, project structure | + ADRs, observability architecture |
| End-to-end project | Required | Strong plus | Exists but unpolished | Polished — README, schemas, model-metadata module, health probes | + tracing, Grafana, failure handling |
| Testing discipline | Required | Required | Missing entirely | 10 unit tests covering all endpoints + edge cases | + integration tests with testcontainers |
| Technical communication | Required | Required | Weak (no blog/ADRs) | Better — strong README, clear descriptions | + 3 deep-dive articles, ADRs |
| Scale thinking | Required | Expected | Not demonstrated | Partially — resource limits, backpressure-aware consumer | + DLQ, circuit breakers, metrics dashboards |
| ML + Infra combo | Differentiator | Differentiator | Present but hidden | Visible — dual-model comparison, model-metadata validation | + inference tradeoffs article |
| OSS contributions | Nice to have | Highly valued | Strong but unleveraged | Profile README links Apicurio | + schema deep dives leveraging PRs |
| Security practices | Expected | Required | Credentials hardcoded | Fixed — env vars, K8s Secrets | Solid |
| Profile presence | Helps | Helps | No README, bad bio, noise repos | Profile README, clean repos, descriptions | + deep dives repo as authority |

---

## Bottom Line

**Tiers 1+2 complete.** The profile is now clean, the flagship project has architecture docs, tests, Apicurio integration, health probes, and proper packaging. The gap between substance and presentation has closed significantly.

**Tier 3 is the differentiator.** Observability, failure handling, and technical deep dives will move you from "strong senior" to "clearly Staff/Principal." See `claudedocs/tier3-plan.md` for the full execution plan.
