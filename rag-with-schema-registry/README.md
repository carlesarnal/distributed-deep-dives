# Building a RAG Chatbot with Schema Registry as the Knowledge Backend

**Carles Arnal** — Principal Software Engineer, IBM | Core contributor, Apicurio Registry

---

## Table of Contents

1. [Introduction](#introduction)
2. [Why Store Prompts in a Schema Registry](#why-store-prompts-in-a-schema-registry)
3. [Architecture Deep Dive](#architecture-deep-dive)
4. [Prompt Template as a Registry Artifact](#prompt-template-as-a-registry-artifact)
5. [RAG Pipeline: From Web Docs to Embeddings](#rag-pipeline-from-web-docs-to-embeddings)
6. [Multi-Turn Conversation Management](#multi-turn-conversation-management)
7. [Production Lessons](#production-lessons)
8. [Conclusion](#conclusion)

---

## Introduction

Last year I built a support chatbot for the Apicurio Registry project. Nothing unusual there
-- every open-source project eventually gets asked the same questions often enough that
automation starts to look attractive. What made this project different was the constraint
I imposed on myself: every piece of configuration that shapes the chatbot's behavior --
its system prompt, its chat prompt template, its model metadata -- must live in a schema
registry, versioned and governed like any other contract in a distributed system.

The result is [apicurio-registry-support-chat](https://github.com/Apicurio/apicurio-registry-support-chat),
a Quarkus application that uses Retrieval-Augmented Generation (RAG) to answer questions
about Apicurio Registry, with Apicurio Registry itself serving as the backend for prompt
management and model metadata. Yes, the chatbot uses the product it supports to manage
its own prompts. There is a pleasant symmetry to that.

This article is not a tutorial. You will not find step-by-step instructions for reproducing
the project. Instead, this is a deep dive into the engineering decisions behind it: why a
schema registry is a surprisingly good fit for prompt management, how the RAG pipeline was
designed, what tradeoffs were made in conversation management, and what I learned about
embedding models, chunk sizes, and similarity thresholds along the way.

If you have ever pasted a prompt template into a YAML file, committed it, deployed it,
and then spent an afternoon figuring out which version of the prompt is running in
production -- this article is for you.

---

## Why Store Prompts in a Schema Registry

### The Problem: Prompts Are Code, But We Treat Them Like Strings

A prompt template is a contract. It defines the interface between your application logic
and the language model. It has input variables that must be satisfied. It has structure
that, if broken, produces garbage output. It evolves over time, and different versions
produce measurably different results.

And yet, in most LLM applications, prompt templates live in one of these places:

- **Hardcoded strings** in source code, buried in a service class
- **Configuration files** (YAML, properties) that get baked into container images
- **Environment variables** that nobody documents
- **A database table** with a `text` column and no versioning

Every one of these approaches has the same fundamental problem: they conflate the prompt
lifecycle with the application lifecycle. When your prompt is a string in your Java class,
changing the prompt means recompiling, rebuilding the container image, and redeploying.
When your prompt is in a config file, you might get hot-reload, but you lose any notion
of version history, rollback, or governance.

This is a solved problem. We solved it years ago for a different kind of contract: data
schemas.

### Schema Registries Already Solve This

In event-driven architectures, schema registries exist precisely to decouple contract
evolution from application deployment. A Kafka producer does not hardcode its Avro schema
-- it registers the schema, gets a schema ID, and embeds that ID in messages. Consumers
resolve the ID back to the schema at runtime. The schema can evolve independently of the
producer and consumer code. You get versioning, compatibility checking, rollback, and
a central catalog of all contracts in your system.

Prompt templates have the same characteristics:

| Schema Registry Feature | Schema Use Case | Prompt Use Case |
|-------------------------|----------------|-----------------|
| **Versioning** | Schema v1 vs v2 | Prompt v1 vs v2 |
| **Compatibility rules** | Backward/forward compat | Variable contract stability |
| **Rollback** | Revert to last known good | Revert to last known good prompt |
| **Central catalog** | All schemas in one place | All prompts in one place |
| **Runtime resolution** | Resolve schema by ID | Resolve prompt by ID |
| **Governance** | Who changed what, when | Who changed the prompt, when |
| **A/B testing** | Route to schema version | Route to prompt version |

The insight is not that schema registries are the only way to manage prompts. The insight
is that schema registries are purpose-built for exactly the lifecycle management that
prompts need, and if you already have one in your architecture, you are duplicating
infrastructure by building a separate prompt management system.

### What Apicurio Registry Brings to the Table

Apicurio Registry is an open-source schema and API artifact registry. It supports Avro,
Protobuf, JSON Schema, OpenAPI, AsyncAPI, GraphQL, and -- crucially for this project --
custom artifact types. When we added the `PROMPT_TEMPLATE` artifact type, we did not
build a new system. We extended an existing one. A prompt template stored in Apicurio
Registry gets the same versioning, metadata, search, and governance capabilities as any
Avro schema.

We also added `MODEL_SCHEMA` as an artifact type for storing model metadata -- things
like which provider hosts a model, what its context window is, what capabilities it
supports. This turns the registry into a catalog not just of prompts but of the models
those prompts target.

The key realization: prompt management is not an LLM problem. It is a contract management
problem. And contract management is what registries do.

---

## Architecture Deep Dive

### The Full System

The apicurio-registry-support-chat system has four components:

```
+------------------+     +-------------------+     +------------------+
|                  |     |                   |     |                  |
|  Quarkus App     |---->| Apicurio Registry |     |     Ollama       |
|  (support-chat)  |     |   (port 8080)     |     |   (port 11434)   |
|  (port 8081)     |---->|                   |     |                  |
|                  |     +-------------------+     +------------------+
|                  |           ^                         ^
|                  |           |                         |
|                  |-----------+-------------------------+
|                  |     fetch prompts            LLM chat +
|                  |     fetch model metadata     embeddings
+------------------+
        |
        v
  REST API clients
  /support/chat/{sessionId}
  /support/ask
  /support/prompts/{artifactId}
  /support/models
```

**Quarkus application** -- the orchestrator. It handles HTTP requests, manages conversation
sessions, runs the RAG pipeline, and coordinates between the registry and the LLM. It is
built with Quarkus because that is the framework I know best, and because `quarkus-langchain4j`
provides first-class integration with LangChain4j, which handles the LLM abstraction layer.

**Apicurio Registry** -- the knowledge backend for prompt templates and model metadata. It
runs as a separate service, typically in a container. The Quarkus app fetches prompt
templates from it at runtime, meaning prompts can be updated without redeploying the
application.

**Ollama** -- the LLM provider. It runs locally and serves two models: `llama3.2` for
chat completion and `nomic-embed-text` for generating embeddings. More on why these
specific models below.

**Docker Compose** -- ties the three services together. The compose file is simple: three
services, two exposed ports (8080 for the registry, 8081 for the chat app), and Ollama
on 11434.

### Why Ollama Over OpenAI

This is a question I get asked frequently. The answer has three parts.

**First, cost.** During development, I was iterating on prompts, embeddings, and chunking
strategies. Each iteration meant re-ingesting documents, re-embedding chunks, and running
dozens of test conversations. With OpenAI, that adds up. With Ollama, it is free.

**Second, reproducibility.** When I change a prompt template and want to measure the
impact, I need the model to be deterministic (or as close to deterministic as possible).
A local Ollama instance with a pinned model version gives me that. OpenAI's models change
under the hood -- same model name, different behavior over time.

**Third, simplicity.** No API keys. No rate limits. No network latency. No privacy
concerns about sending documentation content to a third-party API. For a support chatbot
that answers questions about open-source documentation, these concerns are manageable,
but eliminating them entirely made development faster.

The tradeoff is obvious: `llama3.2` is not GPT-4. The quality of responses is lower for
complex reasoning tasks. For a documentation support chatbot, this tradeoff is acceptable.
Most questions are factual retrieval: "How do I configure Apicurio Registry with Kafka
storage?" The RAG pipeline provides the relevant context, and `llama3.2` is more than
capable of synthesizing a coherent answer from that context.

If you need higher quality, swapping Ollama for OpenAI is a configuration change in
`application.properties`. The architecture does not change.

### Why LangChain4j Over Raw HTTP Calls

LangChain4j provides three things that would be tedious to build from scratch:

1. **Embedding model abstraction.** The `EmbeddingModel` interface lets me swap between
   `nomic-embed-text`, `all-MiniLM-L6-v2`, or any OpenAI embedding model without changing
   application code. I used this extensively during development to compare embedding
   quality.

2. **EmbeddingStore with similarity search.** LangChain4j's in-memory `EmbeddingStore`
   handles vector storage and cosine similarity search. For a chatbot that ingests 12
   HTML pages, an in-memory store is sufficient. If the corpus grew to thousands of
   documents, I would swap in a Chroma or Milvus store -- again, a configuration change,
   not an architecture change.

3. **Document abstraction.** The `Document` and `TextSegment` classes provide a clean
   model for chunked text with metadata. The `DocumentSplitter` implementations handle
   token-based splitting with overlap, which is surprisingly tricky to get right with
   raw string manipulation.

What LangChain4j does not provide -- and what I built myself -- is the prompt registry
integration and the multi-turn conversation management. LangChain4j has its own prompt
template system, but it does not support fetching templates from an external registry
with version resolution. That is the custom layer this project adds.

---

## Prompt Template as a Registry Artifact

### How PROMPT_TEMPLATE Works

A `PROMPT_TEMPLATE` artifact in Apicurio Registry is, at its core, a text document with
variable placeholders. Here is what a system prompt looks like when stored as a registry
artifact:

```json
{
  "name": "apicurio-support-system-prompt",
  "description": "System prompt for the Apicurio Registry support chatbot",
  "template": "You are a helpful technical support assistant for Apicurio Registry, an open-source schema and API registry. Your role is to help users with questions about installation, configuration, usage, and troubleshooting of Apicurio Registry.\n\nYou should:\n- Provide accurate, technically detailed answers based on the provided context\n- Reference specific configuration properties, API endpoints, or CLI commands when relevant\n- Acknowledge when you don't have enough information to answer fully\n- Suggest consulting the official documentation for the most up-to-date information\n\nYou must not:\n- Make up features or configuration options that don't exist\n- Provide advice about competing products\n- Answer questions unrelated to Apicurio Registry",
  "variables": [],
  "metadata": {
    "category": "system",
    "model_target": "llama3.2",
    "author": "carles.arnal"
  }
}
```

And here is the chat prompt template, which is where variable substitution comes in:

```json
{
  "name": "apicurio-support-chat-prompt",
  "description": "Chat prompt template with RAG context and conversation history",
  "template": "Given the following context from the Apicurio Registry documentation:\n\n---\n{{context}}\n---\n\nAnd the following conversation history:\n{{history}}\n\nPlease answer the following question:\n{{question}}",
  "variables": ["context", "history", "question"],
  "metadata": {
    "category": "chat",
    "model_target": "llama3.2",
    "author": "carles.arnal"
  }
}
```

The `variables` array is not just documentation -- it is a contract. When the application
calls `template.apply(Map.of("question", question, "context", ragContext, "history", historyStr))`,
the template engine validates that all required variables are provided. If you add a new
variable to the template but forget to update the application code, you get a clear error
instead of a prompt with unresolved `{{variable}}` placeholders being sent to the LLM.

### Version Management

This is where the registry model pays off. Consider this evolution:

**Version 1** of the system prompt is generic:

```
You are a helpful technical support assistant for Apicurio Registry...
```

**Version 2** adds constraints based on production experience -- users were asking about
pricing (Apicurio is free), about Red Hat Service Registry specifics (related but different
product), and about deprecated v1 APIs:

```
You are a helpful technical support assistant for Apicurio Registry,
an open-source schema and API registry maintained by Red Hat and the
community...

Additional context for this session:
- Registry version: {{registry_version}}
- Deployment mode: {{deployment_mode}}

Important distinctions:
- Apicurio Registry is the open-source upstream project
- Red Hat Service Registry is the downstream, supported product
- When asked about "Service Registry," clarify which product the user means
- Always specify which API version (v2 or v3) you are referencing
```

In a traditional setup, deploying v2 means updating the string in your code, rebuilding,
and deploying. Rolling back means reverting the commit, rebuilding, and redeploying.

With the registry, deploying v2 means creating a new version of the artifact. Rolling back
means pointing the application to version 1. The application code does not change:

```java
// Fetch the latest version
ApicurioPromptTemplate template = promptRegistry.getTemplate(SYSTEM_PROMPT_ARTIFACT);

// Or fetch a specific version for A/B testing
ApicurioPromptTemplate templateV1 = promptRegistry.getTemplate(SYSTEM_PROMPT_ARTIFACT, "1");
ApicurioPromptTemplate templateV2 = promptRegistry.getTemplate(SYSTEM_PROMPT_ARTIFACT, "2");
```

This is the same pattern as schema version resolution in Kafka. The producer does not
decide which schema version to use at compile time -- it resolves it at runtime. The same
principle applies here.

### The ApicurioPromptRegistry API

The `ApicurioPromptRegistry` class is the bridge between the Quarkus application and the
registry. It wraps the Apicurio Registry REST client and provides a typed API for prompt
operations:

```java
public class ApicurioPromptRegistry {

    @Inject
    RegistryClient registryClient;

    public ApicurioPromptTemplate getTemplate(String artifactId) {
        // Fetches the latest version of the prompt template
        // Parses the JSON content into an ApicurioPromptTemplate
        // Caches the result (with TTL-based invalidation)
    }

    public ApicurioPromptTemplate getTemplate(String artifactId, String version) {
        // Fetches a specific version
        // Useful for A/B testing or rollback
    }

    public List<PromptMetadata> listTemplates() {
        // Lists all PROMPT_TEMPLATE artifacts in the registry
        // Returns metadata (name, description, version count)
    }
}
```

The `ApicurioPromptTemplate` class wraps the template content and provides the `apply`
method for variable substitution:

```java
public class ApicurioPromptTemplate {

    private final String template;
    private final List<String> variables;
    private final Map<String, String> metadata;

    public String apply(Map<String, Object> values) {
        String result = template;
        for (String variable : variables) {
            Object value = values.get(variable);
            if (value == null) {
                throw new MissingVariableException(variable);
            }
            result = result.replace("{{" + variable + "}}", value.toString());
        }
        return result;
    }
}
```

This is deliberately simple. The template engine does not support conditionals, loops,
or filters. Prompt templates should be flat text with variable substitution. If you need
complex logic in your prompt construction, that logic belongs in your application code,
not in the template. The template is the contract; the application is the implementation.

### The Cache Invalidation Problem

Fetching a prompt template from the registry on every chat request adds latency. The
registry is fast -- a few milliseconds for a REST call -- but those milliseconds add up
when you are already paying hundreds of milliseconds for embedding search and seconds for
LLM inference.

The naive solution is to cache aggressively: fetch the template once at startup and never
check again. This defeats the purpose of using a registry -- you cannot update prompts
without restarting the application.

The solution I chose is TTL-based caching with a configurable refresh interval:

```java
private final Map<String, CachedTemplate> cache = new ConcurrentHashMap<>();
private static final Duration CACHE_TTL = Duration.ofMinutes(5);

public ApicurioPromptTemplate getTemplate(String artifactId) {
    CachedTemplate cached = cache.get(artifactId);
    if (cached != null && !cached.isExpired()) {
        return cached.template();
    }
    // Fetch from registry, update cache
    ApicurioPromptTemplate template = fetchFromRegistry(artifactId);
    cache.put(artifactId, new CachedTemplate(template, Instant.now()));
    return template;
}
```

A five-minute TTL means prompt updates take at most five minutes to propagate. For a
support chatbot, this is acceptable. For a latency-sensitive production system, you
might use registry webhooks to invalidate the cache immediately on artifact update.
Apicurio Registry supports webhooks for artifact lifecycle events, but I did not need
that level of responsiveness for this project.

The important point is that the caching strategy is independent of the prompt management
strategy. You can tune the cache without changing how prompts are stored, versioned, or
resolved.

---

## RAG Pipeline: From Web Docs to Embeddings

### DocumentIngestionService Architecture

The RAG pipeline has one job: given a user question, find the most relevant chunks of
Apicurio Registry documentation and provide them as context to the LLM. The pipeline
has four stages:

1. **Fetch** -- Download HTML pages from the Apicurio documentation site
2. **Parse** -- Extract text content from the HTML using JSoup
3. **Chunk** -- Split the text into segments of 500 tokens with 50-token overlap
4. **Embed** -- Generate vector embeddings for each chunk using `nomic-embed-text`

These stages run once at application startup, triggered by a CDI `StartupEvent`:

```java
@ApplicationScoped
public class DocumentIngestionService {

    @Inject
    EmbeddingModel embeddingModel;

    @Inject
    EmbeddingStore<TextSegment> embeddingStore;

    private static final List<String> DOC_URLS = List.of(
        "https://www.apicur.io/registry/docs/apicurio-registry/3.x/getting-started/assembly-intro-to-the-registry.html",
        "https://www.apicur.io/registry/docs/apicurio-registry/3.x/getting-started/assembly-installing-registry-docker.html",
        // ... 10 more URLs
    );

    void onStartup(@Observes StartupEvent event) {
        CompletableFuture.runAsync(this::ingestDocumentation);
    }

    private void ingestDocumentation() {
        for (String url : DOC_URLS) {
            try {
                Document doc = fetchAndParse(url);
                List<TextSegment> segments = chunk(doc);
                List<Embedding> embeddings = embeddingModel.embedAll(
                    segments.stream().map(TextSegment::text).toList()
                ).content();
                for (int i = 0; i < segments.size(); i++) {
                    embeddingStore.add(embeddings.get(i), segments.get(i));
                }
            } catch (Exception e) {
                log.warn("Failed to ingest: " + url, e);
            }
        }
        log.info("Documentation ingestion complete");
    }
}
```

### Why Async Ingestion Matters

The `CompletableFuture.runAsync` call is not incidental -- it is a deliberate design
decision. Document ingestion involves network I/O (fetching 12 HTML pages), CPU-intensive
work (parsing HTML, tokenizing text), and model inference (generating embeddings for
hundreds of text segments). On my development machine, the full ingestion takes 30-45
seconds.

If ingestion ran synchronously on the startup event, the Quarkus application would not
accept HTTP requests for 30-45 seconds after starting. In a Kubernetes environment with
health checks, this could cause the pod to be killed before it finishes starting.

Running ingestion asynchronously means the application starts accepting requests immediately.
If a user sends a question before ingestion completes, the embedding store is empty, and
the RAG retriever returns no results. The LLM will answer based on its training data alone,
which is a graceful degradation -- not ideal, but not a crash.

A more sophisticated approach would track ingestion progress and return a "warming up"
response to users during the ingestion window. I did not implement this because, in
practice, nobody sends a question to a support chatbot within 30 seconds of deployment.

### JSoup HTML Parsing

The Apicurio documentation is published as static HTML pages. These pages contain
navigation bars, footers, sidebars, breadcrumbs, and other structural elements that
are noise for a RAG pipeline. If you embed the raw HTML, your chunks will contain
`<nav>`, `<footer>`, and CSS class names that waste token budget and confuse the
embedding model.

JSoup handles the parsing with CSS selectors:

```java
private Document fetchAndParse(String url) {
    org.jsoup.nodes.Document htmlDoc = Jsoup.connect(url).get();

    // Extract only the main content area
    Elements content = htmlDoc.select("div.sect1, div.sect2, div.paragraph, div.listingblock");

    StringBuilder text = new StringBuilder();
    for (Element element : content) {
        text.append(element.text()).append("\n\n");
    }

    return new Document(text.toString(), Metadata.from("source", url));
}
```

The CSS selectors (`div.sect1`, `div.sect2`, `div.paragraph`, `div.listingblock`) target
the AsciiDoc-generated HTML structure of the Apicurio documentation. These selectors
extract section headings, paragraphs, and code listings while skipping navigation,
footers, and other chrome.

This is fragile. If the documentation site changes its HTML structure, the selectors
break. A more robust approach would use a headless browser or a purpose-built documentation
API. For a project that targets a specific, stable documentation site, CSS selectors are
pragmatic.

### Chunking Strategy: 500 Tokens, 50 Overlap

The chunking parameters -- 500 tokens per chunk, 50 tokens of overlap between consecutive
chunks -- are the result of experimentation, not theory.

**Why 500 tokens?** This is a middle ground. Smaller chunks (100-200 tokens) produce more
precise retrieval but lose context. A chunk that says "Set the `registry.storage.kind`
property to `sql`" is precise but useless without the surrounding explanation of what
SQL storage mode does and when to use it. Larger chunks (1000+ tokens) preserve context
but reduce retrieval precision -- a 1000-token chunk about "installation" might match a
query about Docker installation even though only 50 tokens in the chunk are about Docker.

With 500 tokens, a typical chunk covers one complete concept: a configuration option with
its description, a step in a procedure with its explanation, or a code example with its
surrounding narrative. This is large enough to be self-contained and small enough to be
specific.

**Why 50-token overlap?** Overlap ensures that concepts spanning a chunk boundary are not
lost. Consider a paragraph that starts in chunk N and ends in chunk N+1. Without overlap,
a query about that paragraph might partially match both chunks but strongly match neither.
With 50 tokens of overlap, the end of chunk N and the beginning of chunk N+1 share content,
increasing the chance that at least one chunk strongly matches the query.

50 tokens is 10% of the chunk size. This is a common ratio. Higher overlap (20-30%)
increases recall but also increases storage and embedding costs. Lower overlap (1-5%)
provides minimal benefit. 10% is the sweet spot I found through testing.

**The tokenizer matters.** "500 tokens" is model-dependent. LangChain4j's
`DocumentSplitters.recursive()` uses a tokenizer that approximates the target model's
tokenization. For `nomic-embed-text`, this means the actual character count per chunk
varies, but the semantic density is consistent. This is preferable to splitting by
character count, which can produce chunks that are semantically unbalanced.

### Why nomic-embed-text

The embedding model choice is one of the most consequential decisions in a RAG pipeline,
and it deserves more attention than it typically gets.

`nomic-embed-text` is a 137M-parameter embedding model that produces 768-dimensional
vectors. It is available through Ollama, which means it runs locally alongside the chat
model. Here is why I chose it:

**Performance on retrieval benchmarks.** On the MTEB (Massive Text Embedding Benchmark),
`nomic-embed-text` scores competitively with models 3-5x its size. For technical
documentation retrieval specifically, it outperforms `all-MiniLM-L6-v2` (the default
in many tutorials) by a meaningful margin. The difference is visible in practice: queries
like "How do I configure Kafka storage in Apicurio Registry" reliably return the correct
documentation section with `nomic-embed-text`, while `all-MiniLM-L6-v2` sometimes returns
sections about storage in general.

**Inference speed.** At 137M parameters, embedding generation is fast even on CPU. The
full ingestion of 12 HTML pages (approximately 300-400 chunks) completes in under 30
seconds. With a GPU, it is under 5 seconds.

**Dimensionality.** 768 dimensions is a good balance between representation capacity and
memory usage. Higher-dimensional models (1024, 1536) capture more nuance but use more
memory for the in-memory embedding store. For a corpus of 300-400 chunks, memory is not a
concern, but the habit of choosing appropriately-sized models pays off when the corpus
grows.

**Availability through Ollama.** This is a practical consideration. Using Ollama for both
chat and embeddings means one fewer service to deploy and manage. The alternative --
running a separate embedding service like TEI (Text Embeddings Inference) -- adds
operational complexity that is not justified for this project's scale.

### Similarity Thresholds

The `ContentRetriever` is configured with two parameters:

```java
ContentRetriever retriever = EmbeddingStoreContentRetriever.builder()
    .embeddingStore(embeddingStore)
    .embeddingModel(embeddingModel)
    .maxResults(5)
    .minScore(0.6)
    .build();
```

**`maxResults(5)`** -- return at most 5 chunks. This limits the context size sent to the
LLM. Five chunks of 500 tokens each is 2,500 tokens of context, which leaves ample room
for the system prompt, conversation history, and the model's response within `llama3.2`'s
context window.

**`minScore(0.6)`** -- only return chunks with a cosine similarity score of 0.6 or higher.
This is the more interesting parameter.

Without a minimum score, the retriever always returns 5 results, even when the query has
nothing to do with the documentation. A user asking "What is the weather today?" would
get 5 chunks of Apicurio Registry documentation as context. The LLM, being helpful, would
try to incorporate that irrelevant context into its response, producing confused and
misleading answers.

With `minScore(0.6)`, a query about the weather returns zero chunks (no documentation
chunk is similar to a weather query), and the LLM responds based on the system prompt
alone, which instructs it to acknowledge when it does not have enough information.

The value 0.6 was determined empirically. I tested queries that should match documentation
(similarity scores typically 0.7-0.9) and queries that should not match (scores typically
0.2-0.4). A threshold of 0.6 cleanly separates the two distributions with no false
positives or false negatives in my test set.

This threshold is model-dependent. Different embedding models produce different score
distributions. If you swap `nomic-embed-text` for another model, you must recalibrate
the threshold. This is one reason why embedding model choice and similarity threshold
should be documented together -- they are coupled parameters.

---

## Multi-Turn Conversation Management

### Session-Based Memory

A support chatbot needs to maintain context across multiple messages. When a user asks
"How do I install Apicurio Registry?" and then follows up with "What about with Docker?",
the chatbot needs to understand that "it" refers to Apicurio Registry and the user wants
Docker-specific installation instructions.

The conversation state is stored in a `ConcurrentHashMap`:

```java
private final Map<String, List<ConversationTurn>> sessions = new ConcurrentHashMap<>();

public record ConversationTurn(String role, String content, Instant timestamp) {}
```

Each session is identified by a string ID (provided by the client in the URL path) and
contains an ordered list of `ConversationTurn` records. A turn has a role (`user` or
`assistant`), the message content, and a timestamp.

### Why ConcurrentHashMap

This is the decision that draws the most skepticism. "You should use Redis." "You should
use a database." "What about horizontal scaling?"

All valid points. Here is why I chose an in-memory map anyway:

**This is a support chatbot, not a banking system.** If the application restarts and
conversation history is lost, the user asks their question again. This is mildly
inconvenient, not catastrophic. The cost of losing a conversation is a few seconds of
the user's time. The cost of adding Redis is a new service to deploy, monitor, and
maintain, plus serialization logic, connection management, and failure handling.

**Horizontal scaling is not a requirement.** This chatbot serves the Apicurio community.
It does not need to handle thousands of concurrent conversations. A single Quarkus
instance with an in-memory map handles the expected load comfortably.

**ConcurrentHashMap is correct for the concurrency model.** Multiple users can chat
simultaneously, each with their own session ID. `ConcurrentHashMap` provides thread-safe
access to different sessions without blocking. Within a single session, messages are
sequential (a user sends a message, waits for a response, sends another message), so
there is no contention on individual session lists.

**Simplicity has operational value.** Every external dependency is a potential point of
failure. An in-memory map cannot experience a connection timeout, an authentication
failure, or a data format incompatibility after an upgrade. It just works.

If the chatbot needed durable conversations (e.g., for analytics or compliance), or if it
needed to scale horizontally, I would use a database. The `ConversationTurn` record is
trivially serializable -- migrating to a database would be a localized change in the
session management code, not an architectural overhaul.

### History Injection into Prompts

The conversation history is formatted as text and injected into the chat prompt template
via the `{{history}}` variable:

```java
private String formatHistory(List<ConversationTurn> turns) {
    if (turns.isEmpty()) {
        return "No previous conversation.";
    }
    StringBuilder sb = new StringBuilder();
    for (ConversationTurn turn : turns) {
        sb.append(turn.role().equals("user") ? "User: " : "Assistant: ");
        sb.append(turn.content());
        sb.append("\n\n");
    }
    return sb.toString();
}
```

This is a simple approach: the entire conversation history is rendered as a flat string.
For long conversations, this can consume a significant portion of the model's context
window. A more sophisticated approach would summarize older turns or use a sliding window
that keeps only the last N turns.

For a support chatbot, conversations are typically short -- 3-5 turns. The user asks a
question, gets an answer, maybe asks a follow-up or two, and moves on. A sliding window
is unnecessary for this use case. If it became necessary, the change would be in the
`formatHistory` method, not in the architecture.

### Session Lifecycle

The REST API exposes three session-related operations:

**Create/Chat** -- `POST /support/chat/{sessionId}` with the question in the request body.
If the session does not exist, it is created implicitly. This is simpler than requiring a
separate "create session" call.

```java
@POST
@Path("/chat/{sessionId}")
public String chat(@PathParam("sessionId") String sessionId, String question) {
    sessions.computeIfAbsent(sessionId, k -> new ArrayList<>());
    // ... RAG retrieval, prompt construction, LLM call ...
    sessions.get(sessionId).add(new ConversationTurn("user", question, Instant.now()));
    sessions.get(sessionId).add(new ConversationTurn("assistant", response, Instant.now()));
    return response;
}
```

**Quick Ask** -- `POST /support/ask` for one-off questions without session management.
This is a convenience endpoint for users who do not need multi-turn conversations. It
creates a temporary session, processes the question, and discards the session.

**Model/Prompt Inspection** -- `GET /support/prompts/{artifactId}` and `GET /support/models`
for inspecting the current prompt templates and model metadata. These are debugging
endpoints that expose the registry content without going through the LLM.

There is no explicit "delete session" endpoint. Sessions accumulate in memory until the
application restarts. For the expected usage pattern (short conversations, low volume),
this is acceptable. For a higher-volume deployment, you would add a TTL-based eviction
policy -- remove sessions that have been inactive for more than an hour, for example.

---

## Production Lessons

### Embedding Model Choice Matters More Than LLM Choice

This is the most counterintuitive lesson from this project. When I started, I spent most
of my time evaluating chat models: llama3.2 vs. mistral vs. phi-3. The differences were
noticeable in conversational fluency but minimal in answer accuracy for documentation
questions.

Then I swapped the embedding model from `all-MiniLM-L6-v2` to `nomic-embed-text`, keeping
everything else constant. The improvement in answer accuracy was dramatic. Questions that
previously returned irrelevant context -- and therefore produced incorrect answers -- now
returned the correct documentation sections.

The reason is straightforward: in a RAG system, the LLM can only work with the context it
is given. If the retriever returns the wrong chunks, even GPT-4 will produce a wrong
answer (it will just produce it more fluently). If the retriever returns the right chunks,
even a smaller model like `llama3.2` will produce a correct answer.

**The embedding model determines what the LLM sees. The LLM determines how the LLM says
it.** For factual question-answering over documentation, what the model sees matters more
than how it says it.

This does not generalize to all LLM applications. For creative writing, code generation,
or complex reasoning, the LLM choice dominates. But for RAG over technical documentation,
invest your evaluation time in embedding models.

### Chunk Size Is the Most Impactful Hyperparameter

I tested chunk sizes of 100, 200, 300, 500, 750, and 1000 tokens. The results were
non-linear:

- **100 tokens**: High precision, low recall. Chunks are too small to be self-contained.
  The retriever finds the right sentence but without enough context for the LLM to
  generate a complete answer.

- **200 tokens**: Better, but still fragmented. Configuration examples often span 200+
  tokens, so they get split across chunks.

- **300 tokens**: Usable, but noticeable gaps in context for complex topics.

- **500 tokens**: The sweet spot for this documentation corpus. Chunks are self-contained,
  covering one complete concept each. Retrieval precision remains high.

- **750 tokens**: Slightly lower precision but more context per chunk. The LLM answers
  are more complete but occasionally include irrelevant information from the chunk.

- **1000 tokens**: Precision drops noticeably. Chunks cover multiple concepts, and the
  retriever starts returning chunks where only a small portion is relevant.

The optimal chunk size depends on the structure of your documents. Technical documentation
with clear section boundaries works well with 400-600 token chunks. Narrative text with
flowing paragraphs works better with larger chunks. Code-heavy documentation might benefit
from smaller chunks with special handling for code blocks.

The key insight is that chunk size is not a "set and forget" parameter. It should be
evaluated empirically for your specific corpus, using a set of test queries with known
correct answers.

### Prompt Versioning Prevents "It Worked Yesterday" Debugging

Before implementing the registry-backed prompt system, I experienced a debugging nightmare
that is probably familiar to anyone building LLM applications.

The chatbot was producing great answers on Monday. By Wednesday, the answers had degraded.
No code changes had been deployed. The model had not changed. The documentation had not
changed. What changed was a prompt tweak that a colleague had made directly in the
configuration file, committed to main, and deployed as part of an unrelated change.

The prompt change was well-intentioned -- adding a constraint to prevent the chatbot from
answering questions about competitor products. But the phrasing of the constraint
inadvertently made the chatbot overly cautious, refusing to answer legitimate questions
about Apicurio Registry features that had analogues in other products.

With prompt versioning in the registry, this scenario plays out differently:

1. The prompt change is a new version in the registry, with metadata about who made the
   change and why.
2. The application logs which prompt version it is using for each request.
3. When answers degrade, you check the prompt version history. You see the new version.
   You read the diff. You understand the regression.
4. You roll back to the previous version. The chatbot immediately returns to its previous
   behavior, without a code deployment.

This is not hypothetical. I implemented prompt versioning specifically because of this
debugging experience. The time saved on the first rollback paid for the implementation
effort.

### The Min Similarity Threshold Prevents Hallucination-Inducing Irrelevant Context

Early in development, I did not use a minimum similarity threshold. The retriever always
returned 5 results, regardless of relevance. This led to a subtle failure mode:

A user would ask a question that was tangentially related to the documentation -- for
example, "How do I deploy a Quarkus application to Kubernetes?" The retriever would return
5 chunks about Apicurio Registry deployment, which mention Kubernetes but in the context
of deploying the registry, not a generic Quarkus application.

The LLM, seeing this context, would try to be helpful and synthesize an answer that
blended Apicurio-specific deployment instructions with general Quarkus deployment knowledge
from its training data. The result was a confident, detailed, and wrong answer. The user
would follow instructions that deployed Apicurio Registry instead of their application.

The `minScore(0.6)` threshold eliminates this failure mode. When the query does not
strongly match any documentation chunk, the retriever returns zero results. The LLM,
receiving an empty context, falls back to its system prompt instruction to acknowledge
when it lacks information. "I don't have specific documentation about deploying generic
Quarkus applications. I can help with Apicurio Registry deployment -- would you like
instructions for that?"

This is a better outcome than a confidently wrong answer. The threshold converts a
dangerous failure mode (plausible but incorrect advice) into a benign one (a polite
admission of ignorance with a redirect).

### Why This Pattern Generalizes Beyond Chatbots

The architecture described in this article -- registry-managed prompts, RAG over
domain-specific documentation, versioned templates with variable substitution -- is not
chatbot-specific. I see the same pattern applying to:

**Automated code review.** The prompt template defines what the reviewer should look for.
Different teams can use different prompt versions. The RAG corpus is the team's coding
standards and past review comments.

**Incident response.** The prompt template defines how to analyze a PagerDuty alert. The
RAG corpus is the runbook collection. Different prompt versions can target different
severity levels or services.

**Data quality checks.** The prompt template defines the validation rules in natural
language. The RAG corpus is the data dictionary and schema documentation. Prompt versioning
lets you tighten or relax rules without code changes.

**Customer email classification.** The prompt template defines the classification taxonomy.
The RAG corpus is the knowledge base of past classifications. A/B testing different prompt
versions measures classification accuracy.

In all these cases, the core insight is the same: the prompt is a contract, the registry
manages contracts, and the RAG corpus provides domain-specific context that the LLM needs
to do useful work.

---

## Putting It All Together: The Request Flow

To make the architecture concrete, here is the complete flow for a single chat request:

```
1. User sends POST /support/chat/session-42
   Body: "How do I configure SQL storage in Apicurio Registry?"

2. Application resolves session "session-42"
   - ConcurrentHashMap lookup
   - Creates new session if absent

3. Application fetches prompt templates from Apicurio Registry
   - System prompt: PROMPT_TEMPLATE artifact "system-prompt" (latest version)
   - Chat prompt: PROMPT_TEMPLATE artifact "chat-prompt" (latest version)
   - Both may be served from cache if TTL has not expired

4. RAG retrieval
   - Embed the question using nomic-embed-text via Ollama
   - Search the EmbeddingStore for similar chunks
   - Filter by minScore >= 0.6
   - Return up to 5 matching chunks
   - Concatenate chunk texts as "context"

5. Prompt construction
   - Format conversation history for session-42
   - Apply chat prompt template:
     template.apply(Map.of(
       "question", "How do I configure SQL storage...",
       "context", "<concatenated RAG results>",
       "history", "<formatted conversation history>"
     ))

6. LLM inference
   - Send system prompt + rendered chat prompt to llama3.2 via Ollama
   - Receive response

7. Session update
   - Append ConversationTurn("user", question) to session-42
   - Append ConversationTurn("assistant", response) to session-42

8. Return response to client
```

Steps 3-6 are where all the design decisions in this article converge. The prompt
templates come from the registry (versioned, governed, hot-updatable). The RAG context
comes from the embedding pipeline (chunked, embedded, similarity-filtered). The
conversation history comes from the session store (simple, in-memory, adequate). And the
LLM provides the synthesis (local, free, reproducible).

---

## Running the Project

The project is designed to run with a single `docker-compose up`:

```bash
# Clone the repository
git clone https://github.com/Apicurio/apicurio-registry-support-chat.git
cd apicurio-registry-support-chat

# Start all services
docker compose up -d

# Wait for Ollama to pull models (first run only, may take several minutes)
docker compose logs -f ollama

# Test the chat endpoint
curl -X POST http://localhost:8081/support/chat/test-session \
  -H "Content-Type: text/plain" \
  -d "How do I install Apicurio Registry?"

# Test the quick ask endpoint
curl -X POST http://localhost:8081/support/ask \
  -H "Content-Type: text/plain" \
  -d "What artifact types does Apicurio Registry support?"

# Inspect current prompt templates
curl http://localhost:8081/support/prompts/system-prompt

# List available models
curl http://localhost:8081/support/models
```

The Docker Compose file defines three services:

- **apicurio-registry** on port 8080 -- the schema/prompt registry
- **ollama** on port 11434 -- the LLM and embedding model provider
- **support-chat** on port 8081 -- the Quarkus application

On first startup, Ollama needs to download `llama3.2` (~2GB) and `nomic-embed-text`
(~300MB). Subsequent startups are fast since the models are cached in a Docker volume.

---

## Conclusion

The central argument of this article is that prompt management is a solved problem -- we
just need to recognize it as an instance of a problem we have already solved. Schema
registries provide versioning, governance, rollback, and runtime resolution for data
contracts. Prompt templates are data contracts. The connection is direct.

The apicurio-registry-support-chat project demonstrates this by using Apicurio Registry
to manage prompt templates and model metadata for a RAG-powered support chatbot. The
architecture is simple: Quarkus for orchestration, LangChain4j for LLM abstraction,
Ollama for local inference, and Apicurio Registry for prompt governance.

The engineering lessons extend beyond this specific project:

- **Embedding model selection dominates RAG quality.** Spend your evaluation budget on
  embedding models, not chat models.
- **Chunk size is an empirical parameter.** Test it with your specific corpus and query
  distribution. 500 tokens is a reasonable starting point for technical documentation.
- **Prompt versioning is operational hygiene.** The first time you need to roll back a
  prompt change, the investment pays for itself.
- **Similarity thresholds are a safety mechanism.** They prevent the LLM from receiving
  irrelevant context, which is the primary cause of confident but wrong answers in RAG
  systems.
- **Simplicity has value.** A `ConcurrentHashMap` is not a distributed cache. It does not
  need to be. Choose the simplest solution that meets your actual requirements, not your
  hypothetical future requirements.

If you are building an LLM application and struggling with prompt management, consider
whether you already have a schema registry in your architecture. If you do, you might
already have a prompt management system -- you just have not used it that way yet.

---

*Carles Arnal is a Principal Software Engineer at IBM and a core contributor to
[Apicurio Registry](https://github.com/Apicurio/apicurio-registry). The
apicurio-registry-support-chat project is open source and available on
[GitHub](https://github.com/Apicurio/apicurio-registry-support-chat).*
