# Designing Schema Evolution for Event-Driven Systems

**Author:** Carles Arnal, Principal Software Engineer at IBM
**Date:** March 2026
**Series:** Distributed Deep Dives

---

## Table of Contents

1. [Why Schema Evolution Is Hard](#1-why-schema-evolution-is-hard)
2. [Compatibility Modes Explained](#2-compatibility-modes-explained)
3. [Common Mistakes](#3-common-mistakes)
4. [Schema Evolution in the Reddit Pipeline](#4-schema-evolution-in-the-reddit-pipeline)
5. [When to Break Compatibility (And How)](#5-when-to-break-compatibility-and-how)
6. [Multi-Schema Governance for Teams](#6-multi-schema-governance-for-teams)

---

## Introduction

I built a real-time Reddit ML classification pipeline. Posts flow from a Python producer into Kafka, get processed by Spark with multiple ML models, and land in a consumer that serves predictions. Two Kafka topics carry the data:

- **`reddit-stream`** carries raw posts from the producer to Spark: `{id, content}`
- **`kafka-predictions`** carries enriched predictions from Spark to consumers: `{id, title, content, transformer_flair, transformer_confidence, sklearn_flair, sklearn_confidence}`

I use [Apicurio Registry](https://www.apicur.io/registry/) with JSON Schema for schema governance, with BACKWARD compatibility enforced on the model-metadata schema. I documented the rationale for choosing JSON Schema over Avro and Apicurio over Confluent Schema Registry in ADR-004.

This article is about the problem that kept me up at night after the pipeline was running: **what happens when the schemas need to change?**

---

## 1. Why Schema Evolution Is Hard

### The Fundamental Tension

Schema evolution sits at the intersection of two competing forces:

**Producers want to add features.** Your data team wants to enrich events with new fields. The ML team wants to add a third model's predictions. The ops team wants to stamp every event with processing metadata. Every feature request translates to "add more fields to the schema."

**Consumers want stability.** The downstream dashboard expects exactly seven fields. The consumer's deserialization code was compiled against a specific schema. The analytics pipeline has column mappings hardcoded in a SQL query written six months ago by someone who left the company.

In a monolithic application, you change the data structure and update every reference in the same commit. In a distributed system, you cannot deploy all services simultaneously. There is always a window -- minutes, hours, sometimes days -- where different services are running with different expectations about the shape of the data.

### The Deployment Window Problem

Consider a simple scenario. You have three services:

```
Producer (Python) --> Kafka Topic --> Consumer (Java)
                         |
                    Spark Job (Scala)
```

You want to add a `timestamp` field to the events on the `reddit-stream` topic. Here is what happens when you try to deploy this change:

1. **10:00 AM** - You deploy the new Producer that writes `{id, content, timestamp}`
2. **10:05 AM** - The Spark job is still running the old code. It receives messages with `timestamp` and... what? Crashes? Ignores it? Depends on the deserialization configuration.
3. **10:15 AM** - You deploy the new Spark job. But there are still old messages in the topic without `timestamp`. The new Spark job tries to read them and... what?
4. **10:20 AM** - The consumer has not been updated yet. It starts receiving predictions with different shapes depending on whether Spark processed old or new input messages.

This is the fundamental problem. In a distributed system, schema changes are not atomic. There is always a mixed-version window, and your system must handle it gracefully or it will break.

### Why "Just Be Careful" Does Not Work

I have heard teams say: "We will just coordinate deploys." This works right up until it does not. Consider:

- Rolling deployments mean different pods run different code simultaneously
- Consumer lag means old messages coexist with new ones in the same topic
- Replay scenarios (reprocessing from an earlier offset) surface messages from weeks ago
- Multiple teams contribute to the same event stream with different release cycles

Schema evolution is not a theoretical concern. It is an operational reality that bites hardest at 2 AM on a Saturday.

### The Role of a Schema Registry

A schema registry like Apicurio solves this by acting as a contract enforcer. Instead of hoping everyone coordinates, you encode the rules:

1. Every message includes a schema ID in its headers (or uses a magic byte prefix)
2. Producers must register their schema before sending messages
3. The registry checks new schemas against compatibility rules before accepting them
4. Consumers look up the writer's schema by ID and use it to deserialize

The registry does not prevent schema evolution -- it prevents *incompatible* schema evolution. That distinction is everything.

---

## 2. Compatibility Modes Explained

Every schema registry supports some notion of "compatibility modes." The documentation usually gives you a one-line definition and moves on. That is not enough. Let me walk through each mode with concrete JSON Schema examples so you understand what each allows and forbids in practice.

### BACKWARD Compatibility

**Definition:** A new schema can read data written by the old schema.

**What this means in practice:** Consumers using the new schema will successfully deserialize messages that were produced with the old schema. This is the mode you want when you upgrade consumers before producers, or when consumers need to handle a mix of old and new messages during the deployment window.

**Allowed changes:**
- Add a new *optional* field (old messages simply will not have it)
- Remove an existing field (consumers no longer expect it)
- Widen a type constraint (e.g., change minimum from 1 to 0)

**Forbidden changes:**
- Add a new *required* field (old messages lack it, so they fail validation)
- Narrow a type constraint (old data might violate the tighter constraint)

**Example -- valid BACKWARD evolution:**

Original schema (v1):
```json
{
  "type": "object",
  "properties": {
    "id": { "type": "string" },
    "content": { "type": "string" }
  },
  "required": ["id", "content"]
}
```

New schema (v2) -- adds optional `timestamp`:
```json
{
  "type": "object",
  "properties": {
    "id": { "type": "string" },
    "content": { "type": "string" },
    "timestamp": {
      "type": "string",
      "format": "date-time"
    }
  },
  "required": ["id", "content"]
}
```

This is valid because a consumer with v2 can read a v1 message. The `timestamp` field is simply absent, which is fine because it is not required.

**Example -- invalid BACKWARD evolution:**

```json
{
  "type": "object",
  "properties": {
    "id": { "type": "string" },
    "content": { "type": "string" },
    "timestamp": {
      "type": "string",
      "format": "date-time"
    }
  },
  "required": ["id", "content", "timestamp"]
}
```

This fails because a v1 message does not contain `timestamp`. A consumer with v2 would reject the message because a required field is missing. The registry will refuse to register this schema under BACKWARD compatibility.

### FORWARD Compatibility

**Definition:** The old schema can read data written by the new schema.

**What this means in practice:** Consumers using the old schema will successfully deserialize messages produced with the new schema. This is the mode you want when you upgrade producers before consumers -- the common case when you control the producer but have many downstream consumers you cannot force to upgrade.

**Allowed changes:**
- Add a new field *with a default* or that the old schema simply ignores
- Remove an *optional* field (old consumers did not depend on it being present)
- Tighten a type constraint (new data is strictly a subset of what old consumers expect)

**Forbidden changes:**
- Remove a *required* field (old consumers still expect it)
- Widen a type that old consumers cannot handle

**Example -- valid FORWARD evolution:**

Original schema (v1):
```json
{
  "type": "object",
  "properties": {
    "id": { "type": "string" },
    "content": { "type": "string" },
    "debug_info": { "type": "string" }
  },
  "required": ["id", "content"]
}
```

New schema (v2) -- removes the optional `debug_info` field:
```json
{
  "type": "object",
  "properties": {
    "id": { "type": "string" },
    "content": { "type": "string" }
  },
  "required": ["id", "content"]
}
```

This is valid because old consumers with v1 can still read v2 messages. The `debug_info` field was optional, so its absence does not violate the v1 schema.

**Example -- invalid FORWARD evolution:**

New schema (v2) -- removes the required `content` field:
```json
{
  "type": "object",
  "properties": {
    "id": { "type": "string" }
  },
  "required": ["id"]
}
```

This fails because old consumers with v1 expect `content` to be present and required. A v2 message without `content` would be rejected by a v1 consumer.

### FULL Compatibility

**Definition:** Both BACKWARD and FORWARD compatible. New schema can read old data AND old schema can read new data.

**What this means in practice:** This is the strictest useful mode. It means you can deploy producers and consumers in any order and nothing breaks. The tradeoff is that your evolution options are severely limited.

**Allowed changes:**
- Add an optional field (BACKWARD allows it; FORWARD allows it because old consumers ignore unknown fields)
- Remove an optional field (FORWARD allows it; BACKWARD allows it because consumers no longer require it)

**Forbidden changes:**
- Add a required field (breaks BACKWARD)
- Remove a required field (breaks FORWARD)
- Change a field's type (breaks both)
- Rename a field (breaks both)

**Example -- valid FULL evolution:**

The only safe structural change under FULL compatibility is adding or removing optional fields:

```json
{
  "type": "object",
  "properties": {
    "id": { "type": "string" },
    "content": { "type": "string" },
    "metadata": {
      "type": "object",
      "properties": {
        "source": { "type": "string" },
        "version": { "type": "integer" }
      }
    }
  },
  "required": ["id", "content"]
}
```

Adding the optional `metadata` object works in both directions: old consumers ignore it, new consumers treat its absence as expected.

### NONE

**Definition:** No compatibility checking. Any schema can follow any other schema.

**What this means in practice:** The registry accepts anything. This is useful during early development when schemas are in flux and you are the only consumer. It is dangerous in production because a single typo in a schema registration call can break every downstream consumer.

**When to use it:**
- Local development environments
- Prototype/experimental topics
- Topics with a single producer and consumer owned by the same team

**When NOT to use it:**
- Any production topic
- Any topic consumed by more than one service
- Any topic where data replay is a possibility

### Transitive vs. Non-Transitive

Most registries also distinguish between transitive and non-transitive compatibility:

- **Non-transitive (default):** The new schema is checked only against the *immediately previous* version. Schema v3 is checked against v2, but not against v1.
- **Transitive:** The new schema is checked against *all* previous versions. Schema v3 must be compatible with both v2 and v1.

Transitive compatibility matters when consumers might be reading very old messages -- for example, after a consumer reset to the beginning of a topic, or when retention periods are long. In the Reddit pipeline, where we might replay days of data, transitive BACKWARD compatibility is the safer choice.

### Choosing a Compatibility Mode

Here is my decision framework:

| Situation | Mode | Rationale |
|-----------|------|-----------|
| You control both producer and consumer | BACKWARD | Upgrade consumer first, then producer |
| Many consumers you cannot force to upgrade | FORWARD | Producers evolve freely, consumers catch up |
| Multiple teams, shared topics | FULL | No coordination needed, any deploy order works |
| Early development, single developer | NONE | Move fast, schemas will stabilize later |
| Long retention, replay scenarios | BACKWARD_TRANSITIVE | Every version must read every older version |

For the Reddit pipeline, I chose **BACKWARD** compatibility. I control all services, I deploy consumers before producers, and the evolution pattern is almost always "add optional fields to the prediction output."

---

## 3. Common Mistakes

I have reviewed dozens of schema evolution attempts across teams. The same mistakes appear repeatedly. Here are the most common ones, with concrete JSON Schema examples showing exactly why they fail.

### Mistake 1: Adding a Required Field Without a Default

This is the most common mistake. A developer needs a new field and marks it as required because "all new messages will have it." They forget about the old messages still sitting in the topic.

**The broken evolution:**

```json
// v1 -- the current schema
{
  "type": "object",
  "properties": {
    "id": { "type": "string" },
    "content": { "type": "string" }
  },
  "required": ["id", "content"]
}
```

```json
// v2 -- BREAKS BACKWARD compatibility
{
  "type": "object",
  "properties": {
    "id": { "type": "string" },
    "content": { "type": "string" },
    "source_subreddit": { "type": "string" }
  },
  "required": ["id", "content", "source_subreddit"]
}
```

**Why it breaks:** A v1 message like `{"id": "abc123", "content": "Hello world"}` is invalid under v2 because it lacks the required `source_subreddit` field. The registry will reject this evolution under BACKWARD compatibility.

**The fix:** Make the new field optional:

```json
// v2 -- CORRECT
{
  "type": "object",
  "properties": {
    "id": { "type": "string" },
    "content": { "type": "string" },
    "source_subreddit": { "type": "string" }
  },
  "required": ["id", "content"]
}
```

Then handle the absence in your consumer code:

```python
subreddit = message.get("source_subreddit", "unknown")
```

### Mistake 2: Changing a Field's Type

This breaks everything. I have seen this happen when a team decides that `confidence` should be a percentage integer (0-100) instead of a float (0.0-1.0), or when an ID field changes from string to integer.

**The broken evolution:**

```json
// v1
{
  "type": "object",
  "properties": {
    "id": { "type": "string" },
    "confidence": { "type": "number" }
  },
  "required": ["id", "confidence"]
}
```

```json
// v2 -- BREAKS EVERYTHING
{
  "type": "object",
  "properties": {
    "id": { "type": "integer" },
    "confidence": { "type": "string" }
  },
  "required": ["id", "confidence"]
}
```

**Why it breaks:** A v1 message `{"id": "abc123", "confidence": 0.95}` has a string `id` and a number `confidence`. The v2 schema expects an integer `id` and a string `confidence`. Neither direction works.

**The fix:** Never change a field's type. Instead, add a new field with the desired type and deprecate the old one:

```json
// v2 -- CORRECT approach
{
  "type": "object",
  "properties": {
    "id": { "type": "string" },
    "confidence": { "type": "number" },
    "confidence_pct": {
      "type": "integer",
      "minimum": 0,
      "maximum": 100,
      "description": "Confidence as percentage. Preferred over 'confidence' which is deprecated."
    }
  },
  "required": ["id", "confidence"]
}
```

### Mistake 3: Removing a Field That Consumers Depend On

Under FORWARD compatibility, removing a field that old consumers treat as required is a breaking change. Even under BACKWARD compatibility, removing a field can cause runtime failures if consumers assume it exists without checking.

**The broken evolution (under FORWARD compatibility):**

```json
// v1
{
  "type": "object",
  "properties": {
    "id": { "type": "string" },
    "title": { "type": "string" },
    "content": { "type": "string" }
  },
  "required": ["id", "title", "content"]
}
```

```json
// v2 -- BREAKS FORWARD compatibility
{
  "type": "object",
  "properties": {
    "id": { "type": "string" },
    "content": { "type": "string" }
  },
  "required": ["id", "content"]
}
```

**Why it breaks:** A v1 consumer expects `title` to be present. A v2 message without `title` will cause a validation failure or a `NullPointerException` in the consumer, depending on how strictly it validates.

**The fix under FORWARD:** Keep the field but stop populating it (send an empty string or null). Under BACKWARD, the removal is technically valid, but you should still coordinate with consumers:

```json
// v2 -- safe deprecation
{
  "type": "object",
  "properties": {
    "id": { "type": "string" },
    "title": {
      "type": "string",
      "description": "DEPRECATED: Will be removed in v3. Use 'content' instead.",
      "deprecated": true
    },
    "content": { "type": "string" }
  },
  "required": ["id", "content"]
}
```

### Mistake 4: Renaming a Field

Renaming a field seems harmless but is equivalent to removing the old field and adding a new one. It breaks in both directions.

**The broken evolution:**

```json
// v1
{
  "type": "object",
  "properties": {
    "transformer_flair": { "type": "string" },
    "transformer_confidence": { "type": "number" }
  },
  "required": ["transformer_flair", "transformer_confidence"]
}
```

```json
// v2 -- BREAKS BOTH DIRECTIONS
{
  "type": "object",
  "properties": {
    "bert_flair": { "type": "string" },
    "bert_confidence": { "type": "number" }
  },
  "required": ["bert_flair", "bert_confidence"]
}
```

**Why it breaks:**
- BACKWARD: A v1 message has `transformer_flair` but v2 requires `bert_flair`. Deserialization fails.
- FORWARD: A v2 message has `bert_flair` but v1 requires `transformer_flair`. Deserialization fails.

**The fix:** Use the expand-and-contract pattern (covered in detail in [Section 5](#5-when-to-break-compatibility-and-how)):

```json
// v2 -- STEP 1: Expand (add new names, keep old names)
{
  "type": "object",
  "properties": {
    "transformer_flair": { "type": "string" },
    "transformer_confidence": { "type": "number" },
    "bert_flair": { "type": "string" },
    "bert_confidence": { "type": "number" }
  },
  "required": ["transformer_flair", "transformer_confidence"]
}
```

```json
// v3 -- STEP 2: Contract (remove old names, once all consumers use the new ones)
{
  "type": "object",
  "properties": {
    "bert_flair": { "type": "string" },
    "bert_confidence": { "type": "number" }
  },
  "required": ["bert_flair", "bert_confidence"]
}
```

Note that the contract step (v2 -> v3) breaks BACKWARD compatibility. You either accept this (if you have drained old messages) or use topic versioning.

### Mistake 5: The "Optional Field Trap"

After getting burned by required-field problems, some teams swing to the other extreme: they make every field optional. This avoids compatibility failures but creates a different problem -- you lose all type safety guarantees.

**The antipattern:**

```json
{
  "type": "object",
  "properties": {
    "id": { "type": "string" },
    "content": { "type": "string" },
    "transformer_flair": { "type": "string" },
    "transformer_confidence": { "type": "number" },
    "sklearn_flair": { "type": "string" },
    "sklearn_confidence": { "type": "number" }
  },
  "required": []
}
```

**Why this is a problem:**

1. **No validation at the producer.** A message like `{"id": "abc123"}` is valid. Missing all the prediction fields? Valid. Empty object `{}`? Also valid. The schema accepts everything, which means it validates nothing.

2. **Every consumer must defensively check every field.** Your consumer code becomes a forest of null checks:

```python
# This is what "everything optional" looks like in practice
def process_prediction(msg):
    post_id = msg.get("id")
    if post_id is None:
        log.error("Missing id -- skipping message")
        return

    transformer_flair = msg.get("transformer_flair")
    if transformer_flair is None:
        log.warning("Missing transformer_flair")
        transformer_flair = "UNKNOWN"

    transformer_conf = msg.get("transformer_confidence")
    if transformer_conf is None:
        log.warning("Missing transformer_confidence")
        transformer_conf = 0.0

    # ... repeat for every field
```

3. **Schema documentation becomes meaningless.** If the schema says every field is optional, how does a new team member know which fields are actually always present? They have to read the producer source code, which defeats the purpose of having a schema.

**The fix:** Be intentional about which fields are required and which are optional. Required fields are your contract -- they are the fields that every message is guaranteed to contain. Optional fields are extensions that may or may not be present depending on the schema version.

A good rule of thumb: **fields that existed in v1 should remain required forever.** New fields added in later versions are optional. This naturally produces BACKWARD-compatible schemas.

```json
{
  "type": "object",
  "properties": {
    "id": { "type": "string" },
    "content": { "type": "string" },
    "transformer_flair": { "type": "string" },
    "transformer_confidence": { "type": "number" },
    "sklearn_flair": { "type": "string" },
    "sklearn_confidence": { "type": "number" },
    "processing_time_ms": {
      "type": "integer",
      "description": "Added in v2. Optional for backward compatibility."
    }
  },
  "required": ["id", "transformer_flair", "transformer_confidence",
               "sklearn_flair", "sklearn_confidence"]
}
```

---

## 4. Schema Evolution in the Reddit Pipeline

Let me walk through the actual schemas from my Reddit classification pipeline and show how they evolve in practice. This is not a toy example -- these are the real schemas running in a pipeline that ingests Reddit posts, classifies them with multiple ML models, and serves predictions in real time.

### The Starting Point

The pipeline has two Kafka topics with two schemas.

**`reddit-stream` -- Producer to Spark:**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:reddit:stream:value",
  "title": "RedditStreamValue",
  "type": "object",
  "properties": {
    "id": {
      "type": "string",
      "description": "Reddit post ID"
    },
    "content": {
      "type": "string",
      "description": "Cleaned and concatenated post content (title + body + comments + domain)"
    }
  },
  "required": ["id", "content"]
}
```

This schema is deliberately minimal. The `content` field concatenates everything (title, body, comments, domain) into a single string because the ML models expect a single text input. The `id` field is the Reddit post identifier used to correlate predictions back to the original post.

**`kafka-predictions` -- Spark to Consumer:**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:reddit:predictions:value",
  "title": "KafkaPredictionsValue",
  "type": "object",
  "properties": {
    "id": {
      "type": "string",
      "description": "Reddit post ID"
    },
    "title": {
      "type": "string",
      "description": "Original post title"
    },
    "content": {
      "type": "string",
      "description": "Cleaned post content"
    },
    "transformer_flair": {
      "type": "string",
      "description": "Flair predicted by Transformer model"
    },
    "transformer_confidence": {
      "type": "number",
      "minimum": 0,
      "maximum": 1,
      "description": "Transformer prediction confidence"
    },
    "sklearn_flair": {
      "type": "string",
      "description": "Flair predicted by scikit-learn model"
    },
    "sklearn_confidence": {
      "type": "number",
      "minimum": 0,
      "maximum": 1,
      "description": "scikit-learn prediction confidence"
    }
  },
  "required": [
    "id",
    "transformer_flair",
    "transformer_confidence",
    "sklearn_flair",
    "sklearn_confidence"
  ]
}
```

Notice that `title` and `content` are *not* in the `required` array. They were added after the initial version as optional enrichment fields. The five required fields (`id` plus both models' predictions) form the stable contract that every consumer can depend on.

### Evolution Scenario 1: Enriching the Input Stream

The Spark job wants more context to improve classification accuracy. We want to add three fields to `reddit-stream`:

- `timestamp` -- when the post was ingested
- `flair_hint` -- the author's self-assigned flair, if any
- `source_subreddit` -- which subreddit the post came from

**Valid evolution (BACKWARD compatible):**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:reddit:stream:value",
  "title": "RedditStreamValue",
  "type": "object",
  "properties": {
    "id": {
      "type": "string",
      "description": "Reddit post ID"
    },
    "content": {
      "type": "string",
      "description": "Cleaned and concatenated post content (title + body + comments + domain)"
    },
    "timestamp": {
      "type": "string",
      "format": "date-time",
      "description": "ISO 8601 timestamp of when the post was ingested"
    },
    "flair_hint": {
      "type": "string",
      "description": "Author-assigned flair hint from the original Reddit post, if available"
    },
    "source_subreddit": {
      "type": "string",
      "description": "The subreddit from which this post was scraped"
    }
  },
  "required": ["id", "content"]
}
```

The key detail: `required` remains `["id", "content"]`. The three new fields are optional. This means:

1. The Apicurio Registry accepts this as a valid BACKWARD evolution
2. The Spark job (consumer of this topic) can be deployed before or after the producer
3. If Spark receives an old message without `timestamp`, it simply does not have that field -- no crash, no deserialization error
4. Spark's code handles the optionality:

```scala
val timestamp = Option(record.get("timestamp"))
  .map(Instant.parse)
  .getOrElse(Instant.now())

val subreddit = Option(record.get("source_subreddit"))
  .getOrElse("unknown")
```

**Invalid evolution (would be rejected by Apicurio):**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:reddit:stream:value",
  "title": "RedditStreamValue",
  "type": "object",
  "properties": {
    "id": {
      "type": "string",
      "description": "Reddit post ID"
    },
    "content": {
      "type": "string",
      "description": "Cleaned and concatenated post content"
    },
    "timestamp": {
      "type": "string",
      "format": "date-time",
      "description": "ISO 8601 timestamp of when the post was ingested"
    },
    "source_subreddit": {
      "type": "string",
      "description": "The subreddit from which this post was scraped"
    }
  },
  "required": ["id", "content", "timestamp", "source_subreddit"],
  "additionalProperties": false
}
```

Two problems here:

1. **`timestamp` and `source_subreddit` are required.** Old messages do not have these fields, so they fail validation under the new schema. Breaks BACKWARD.
2. **`additionalProperties: false`** is a compatibility killer. It means the schema rejects any field not explicitly listed. If a future v3 adds more fields, v2 consumers with `additionalProperties: false` would reject them. Avoid this in event schemas.

### Evolution Scenario 2: Adding a Third ML Model

The most interesting evolution in the pipeline. We trained an LLM-based classifier and want to add its predictions alongside the existing Transformer and scikit-learn results. We also want to add processing-time metrics and an ensemble voting result.

**Valid evolution of `kafka-predictions` (v2):**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:reddit:predictions:value",
  "title": "KafkaPredictionsValue",
  "type": "object",
  "properties": {
    "id": {
      "type": "string",
      "description": "Reddit post ID"
    },
    "title": {
      "type": "string",
      "description": "Original post title"
    },
    "content": {
      "type": "string",
      "description": "Cleaned post content"
    },
    "transformer_flair": {
      "type": "string",
      "description": "Flair predicted by Transformer model"
    },
    "transformer_confidence": {
      "type": "number",
      "minimum": 0,
      "maximum": 1,
      "description": "Transformer prediction confidence"
    },
    "sklearn_flair": {
      "type": "string",
      "description": "Flair predicted by scikit-learn model"
    },
    "sklearn_confidence": {
      "type": "number",
      "minimum": 0,
      "maximum": 1,
      "description": "scikit-learn prediction confidence"
    },
    "llm_flair": {
      "type": "string",
      "description": "Flair predicted by the LLM-based model"
    },
    "llm_confidence": {
      "type": "number",
      "minimum": 0,
      "maximum": 1,
      "description": "LLM prediction confidence"
    },
    "processing_time_ms": {
      "type": "integer",
      "minimum": 0,
      "description": "Total processing time in milliseconds from ingestion to prediction"
    },
    "ensemble_flair": {
      "type": "string",
      "description": "Final flair chosen by the ensemble voting strategy across all models"
    },
    "ensemble_agreement": {
      "type": "number",
      "minimum": 0,
      "maximum": 1,
      "description": "Fraction of models that agreed on the ensemble flair"
    }
  },
  "required": [
    "id",
    "transformer_flair",
    "transformer_confidence",
    "sklearn_flair",
    "sklearn_confidence"
  ]
}
```

This evolution adds five new fields, all optional:

| Field | Type | Why optional |
|-------|------|-------------|
| `llm_flair` | string | LLM model may not be deployed on all Spark workers yet |
| `llm_confidence` | number | Same as above |
| `processing_time_ms` | integer | Operational metric, not all code paths populate it |
| `ensemble_flair` | string | Only meaningful when all three models have run |
| `ensemble_agreement` | number | Same as above |

The `required` array is unchanged. This is the golden rule: **never add to the required array in a BACKWARD-compatible evolution.**

### Deployment Order for the Third Model

Here is the actual deployment sequence I would follow:

1. **Register the v2 schema** with Apicurio Registry. The registry validates BACKWARD compatibility and accepts it.

2. **Deploy the updated consumer** (Flair consumer). It now understands `llm_flair`, `llm_confidence`, `ensemble_flair`, `ensemble_agreement`, and `processing_time_ms`. But it handles their absence gracefully because they are optional.

3. **Deploy the updated Spark job.** It starts running the LLM model alongside the existing two models and populates the new fields. Old messages in the topic (without the LLM fields) are still valid under v2 because those fields are optional.

4. **Monitor.** Check that the consumer correctly handles both old-format and new-format messages during the transition window.

Consumer first, producer second. This is the BACKWARD compatibility deployment pattern.

### Validating Compatibility with Apicurio Registry

Before registering a new schema version, you can use the Apicurio Registry API to test compatibility without actually committing the change. The `examples/validate-compatibility.sh` script in this repository demonstrates how:

```bash
# Test compatibility of a new schema against the existing version
./examples/validate-compatibility.sh \
    http://localhost:8080 \
    reddit-pipeline \
    reddit-stream-value \
    ./examples/reddit-stream-v2-valid.json
```

The script uses Apicurio Registry's v3 API with the `X-Registry-DryRun: true` header to perform a compatibility check without registering:

```bash
curl -s -X POST \
    -H "Content-Type: application/json" \
    -H "X-Registry-DryRun: true" \
    -d "{
        \"content\": {
            \"content\": \"$(cat schema.json)\",
            \"contentType\": \"application/json\"
        }
    }" \
    "${REGISTRY_URL}/apis/registry/v3/groups/${GROUP_ID}/artifacts/${ARTIFACT_ID}/versions"
```

If the schema is compatible, you get a 200 response with the version metadata. If not, you get a 409 Conflict with details about what broke.

### Schema Registration Workflow

Here is the complete workflow I use for evolving schemas in the Reddit pipeline:

```
1. Edit the schema JSON file locally
2. Run validate-compatibility.sh to check against Apicurio
3. If incompatible, adjust the schema (usually: make the new field optional)
4. If compatible, commit the schema file to version control
5. CI/CD pipeline registers the schema with Apicurio as part of the build
6. Deploy the consumer (reads new + old format)
7. Deploy the producer (writes new format)
8. Verify in monitoring/observability
```

Step 5 is crucial -- the schema file lives in version control alongside the service code. It is not a side artifact managed through a UI. Schema changes go through code review like any other code change.

---

## 5. When to Break Compatibility (And How)

Sometimes you must make an incompatible change. The field type is wrong. The entire structure needs to change. You are migrating from one serialization format to another. Compatibility modes exist to prevent accidental breakage, but intentional breakage is sometimes the right call.

Here are the patterns for doing it safely.

### Pattern 1: Topic Versioning

The simplest approach. Create a new topic with the new schema and migrate consumers.

```
reddit-stream      (v1 schema: {id, content})
reddit-stream-v2   (v2 schema: {id, content, timestamp, subreddit, metadata: {...}})
```

**How it works:**

1. Create `reddit-stream-v2` with the new schema
2. Update the producer to write to both `reddit-stream` and `reddit-stream-v2` (dual-write)
3. Migrate consumers one by one from `reddit-stream` to `reddit-stream-v2`
4. Once all consumers are migrated, stop writing to `reddit-stream`
5. After the retention period expires, delete `reddit-stream`

**Pros:**
- Clean separation, no ambiguity
- Consumers can migrate at their own pace
- Easy rollback (just switch back to the old topic)

**Cons:**
- Dual-write increases producer complexity and resource usage
- Topic proliferation if done frequently
- Consumer group offsets do not transfer (consumers restart from the beginning or a specified offset)

**When to use it:** Major structural changes, format migrations, or when the old and new schemas are so different that no compatibility mode could bridge them.

### Pattern 2: Dual-Write During Migration

A refinement of topic versioning where both schemas coexist on the same topic for a transition period. This requires consumers that can detect the schema version and handle both.

```python
# Producer writes both formats during migration
def produce_message(post):
    # Old format for backward compatibility
    v1_message = {
        "id": post.id,
        "content": post.content
    }

    # New format with enriched data
    v2_message = {
        "id": post.id,
        "content": post.content,
        "timestamp": post.ingested_at.isoformat(),
        "source_subreddit": post.subreddit,
        "metadata": {
            "producer_version": "2.0",
            "schema_version": 2
        }
    }

    # Write v2 format -- v1 consumers ignore unknown fields
    producer.send("reddit-stream", value=v2_message)
```

This works when the evolution is BACKWARD compatible but you want an explicit version marker. The `metadata.schema_version` field lets consumers branch on the version:

```python
def consume_message(msg):
    version = msg.get("metadata", {}).get("schema_version", 1)
    if version >= 2:
        process_v2(msg)
    else:
        process_v1(msg)
```

### Pattern 3: Consumer Version Detection via Schema ID

This is the most elegant approach when using a schema registry. Every message carries the schema ID in its headers (or in a magic byte prefix). The consumer retrieves the schema from the registry and uses it to deserialize.

```
Message Header:
  apicurio.value.globalId: 42    # Schema ID in Apicurio Registry

Consumer logic:
  1. Read the schema ID from the message header
  2. Fetch the schema from Apicurio Registry (with caching)
  3. Deserialize the message using the writer's schema
  4. Apply any necessary transformations based on the schema version
```

With Apicurio's JSON Schema serdes (serializer/deserializer), this happens automatically:

```java
// Consumer configuration for Apicurio Registry
props.put(ConsumerConfig.VALUE_DESERIALIZER_CLASS_CONFIG,
    JsonSchemaKafkaDeserializer.class.getName());
props.put(SerdeConfig.REGISTRY_URL, "http://registry:8080/apis/registry/v3");
props.put(JsonSchemaKafkaDeserializer.SCHEMA_RESOLVER,
    TopicIdStrategy.class.getName());
```

The deserializer fetches the writer's schema from the registry using the global ID embedded in the message, then validates and deserializes the payload. If the consumer's expected schema differs from the writer's schema, the registry's compatibility guarantee ensures the data is still readable.

### Pattern 4: The Expand and Contract Pattern

This is the most disciplined approach for making breaking changes incrementally while maintaining compatibility at every step:

**Phase 1 -- Expand:**
Add the new fields alongside the old fields. Both old and new consumers work.

```json
// v2: Expand -- add new fields, keep old fields
{
  "type": "object",
  "properties": {
    "transformer_flair": { "type": "string" },
    "transformer_confidence": { "type": "number" },
    "bert_flair": { "type": "string", "description": "New name for transformer_flair" },
    "bert_confidence": { "type": "number", "description": "New name for transformer_confidence" }
  },
  "required": ["transformer_flair", "transformer_confidence"]
}
```

**Phase 2 -- Migrate:**
Update all producers to populate both old and new fields. Update all consumers to read from new fields (falling back to old fields if absent).

```python
# Producer populates both during migration
message = {
    "transformer_flair": prediction.flair,
    "transformer_confidence": prediction.confidence,
    "bert_flair": prediction.flair,          # same value, new field name
    "bert_confidence": prediction.confidence  # same value, new field name
}
```

```python
# Consumer reads from new field, falls back to old
flair = msg.get("bert_flair") or msg.get("transformer_flair")
confidence = msg.get("bert_confidence") or msg.get("transformer_confidence")
```

**Phase 3 -- Contract:**
Once all consumers have been migrated (verified through monitoring and code review), remove the old fields.

```json
// v3: Contract -- remove old fields
{
  "type": "object",
  "properties": {
    "bert_flair": { "type": "string" },
    "bert_confidence": { "type": "number" }
  },
  "required": ["bert_flair", "bert_confidence"]
}
```

**Important:** The v2->v3 transition breaks BACKWARD compatibility (old messages still have only `transformer_flair`). You must ensure all old messages have been consumed or the topic has been compacted/expired before making this change. Alternatively, temporarily set the compatibility mode to NONE for the v3 registration, then restore BACKWARD.

### When Each Pattern Applies

| Pattern | Use when | Downtime required |
|---------|----------|-------------------|
| Topic versioning | Major structural changes, format migration | No |
| Dual-write | Gradual migration with explicit versioning | No |
| Schema ID detection | Registry-managed evolution, automatic handling | No |
| Expand and contract | Field renames, type changes (multi-step) | No |

My preference: use BACKWARD-compatible additions (optional fields) for 90% of changes. Reserve the expand-and-contract pattern for field renames. Use topic versioning as a last resort for fundamental structural changes.

---

## 6. Multi-Schema Governance for Teams

Schema evolution gets harder when multiple teams are involved. The Reddit pipeline is currently a single-developer project, but the governance patterns I use are designed to scale to team environments. Here is what I have learned from operating schema registries across teams at IBM.

### Schema Ownership: The Producer Owns the Schema

This is the single most important governance decision. **The team that owns the producer owns the schema.** Period.

Why? Because the producer is the authority on what the data looks like. The producer knows what fields it can populate, what types are correct, and what constraints are valid. Consumers adapt to the producer's schema, not the other way around.

In the Reddit pipeline:
- The Python producer team owns `urn:reddit:stream:value`
- The Spark processing team owns `urn:reddit:predictions:value`

If the consumer team wants a new field, they file a request with the producer team. The producer team evaluates whether they can provide it, adds it to the schema as an optional field, and deploys. The consumer team then updates their code to use the new field.

This prevents the antipattern where a consumer team adds a required field to a schema they do not produce, breaking the producer that has no idea the schema changed.

### Schema Review in CI

Schema changes should be validated in CI before they are merged. Here is a CI pipeline stage that checks compatibility:

```yaml
# .github/workflows/schema-check.yml
name: Schema Compatibility Check

on:
  pull_request:
    paths:
      - 'schemas/**'

jobs:
  check-compatibility:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Start Apicurio Registry
        run: |
          docker run -d \
            --name registry \
            -p 8080:8080 \
            -e REGISTRY_APIS_V2_DATE_FORMAT="yyyy-MM-dd'T'HH:mm:ss'Z'" \
            quay.io/apicurio/apicurio-registry:latest-snapshot
          sleep 10

      - name: Seed registry with current schemas
        run: |
          # Register the current production schemas
          for schema in schemas/current/*.json; do
            ARTIFACT_ID=$(basename "$schema" .json)
            curl -X POST \
              -H "Content-Type: application/json" \
              -d "{
                \"artifactId\": \"${ARTIFACT_ID}\",
                \"artifactType\": \"JSON\",
                \"firstVersion\": {
                  \"version\": \"1\",
                  \"content\": {
                    \"content\": $(python3 -c "import sys,json; print(json.dumps(open('$schema').read()))"),
                    \"contentType\": \"application/json\"
                  }
                }
              }" \
              "http://localhost:8080/apis/registry/v3/groups/reddit-pipeline/artifacts"
          done

      - name: Set compatibility rules
        run: |
          # Set BACKWARD compatibility on all artifacts
          for schema in schemas/current/*.json; do
            ARTIFACT_ID=$(basename "$schema" .json)
            curl -X PUT \
              -H "Content-Type: application/json" \
              -d '{"type": "COMPATIBILITY", "config": "BACKWARD"}' \
              "http://localhost:8080/apis/registry/v3/groups/reddit-pipeline/artifacts/${ARTIFACT_ID}/rules"
          done

      - name: Validate new schemas
        run: |
          FAILED=0
          for schema in schemas/proposed/*.json; do
            ARTIFACT_ID=$(basename "$schema" .json)
            echo "Checking compatibility for ${ARTIFACT_ID}..."

            RESPONSE=$(curl -s -w "\n%{http_code}" \
              -X POST \
              -H "Content-Type: application/json" \
              -H "X-Registry-DryRun: true" \
              -d "{
                \"content\": {
                  \"content\": $(python3 -c "import sys,json; print(json.dumps(open('$schema').read()))"),
                  \"contentType\": \"application/json\"
                }
              }" \
              "http://localhost:8080/apis/registry/v3/groups/reddit-pipeline/artifacts/${ARTIFACT_ID}/versions")

            HTTP_CODE=$(echo "$RESPONSE" | tail -1)
            if [[ ! "$HTTP_CODE" =~ ^2 ]]; then
              echo "FAIL: ${ARTIFACT_ID} is NOT compatible"
              echo "$RESPONSE" | sed '$d'
              FAILED=1
            else
              echo "PASS: ${ARTIFACT_ID} is compatible"
            fi
          done

          if [[ $FAILED -eq 1 ]]; then
            echo ""
            echo "Schema compatibility check FAILED."
            echo "Fix the incompatible schemas before merging."
            exit 1
          fi
```

This workflow:
1. Spins up a temporary Apicurio Registry instance
2. Seeds it with the current production schemas
3. Configures BACKWARD compatibility rules
4. Tests each proposed schema change against the current version
5. Fails the PR if any schema is incompatible

The developer gets feedback before the merge, not after a production deployment fails.

### Apicurio Groups for Domain Organization

Apicurio Registry supports "groups" as a namespacing mechanism. Instead of dumping all schemas into the default group, organize them by domain or team:

```
Group: reddit-pipeline
  Artifacts:
    - reddit-stream-value       (Producer: ingestion-team)
    - kafka-predictions-value   (Producer: ml-team)

Group: user-events
  Artifacts:
    - user-signup-value         (Producer: auth-team)
    - user-profile-update-value (Producer: profile-team)

Group: infrastructure
  Artifacts:
    - service-heartbeat-value   (Producer: platform-team)
    - deployment-event-value    (Producer: platform-team)
```

Groups provide several benefits:

1. **Access control.** You can configure different compatibility rules per group. The `reddit-pipeline` group uses BACKWARD; the `infrastructure` group might use NONE during rapid iteration.

2. **Discovery.** New team members can browse the registry by group to find all schemas relevant to their domain.

3. **Blast radius.** A misconfigured compatibility rule in one group does not affect other groups.

In the Reddit pipeline, I use a single group (`reddit-pipeline`) because it is a single-domain project. But the structure is ready for expansion.

### Schema Documentation as a Living Contract

A schema is not just a validation artifact -- it is documentation. JSON Schema supports `description` fields at every level, and you should use them extensively:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:reddit:predictions:value",
  "title": "KafkaPredictionsValue",
  "description": "Prediction results for a Reddit post, produced by the Spark ML pipeline. Contains predictions from multiple models (Transformer, scikit-learn, and optionally LLM) along with ensemble results. Consumers should treat fields not in the 'required' array as optional -- they may be absent in older messages or when a model is unavailable.",
  "type": "object",
  "properties": {
    "id": {
      "type": "string",
      "description": "Reddit post ID. Matches the 'id' field from the reddit-stream topic. Format: Reddit's base-36 post identifier (e.g., 'abc123')."
    },
    "transformer_confidence": {
      "type": "number",
      "minimum": 0,
      "maximum": 1,
      "description": "Transformer model's confidence in its prediction. Range: [0.0, 1.0]. Values below 0.5 indicate low confidence -- consumers may want to fall back to ensemble_flair in these cases."
    }
  }
}
```

Good schema descriptions answer three questions:
1. **What is this field?** (the obvious part)
2. **What are the constraints?** (format, range, allowed values)
3. **How should consumers use it?** (fallback behavior, deprecation notices, relationships to other fields)

The schema in the registry becomes the single source of truth. When a new team member needs to build a consumer, they do not need to read the producer's source code or track down a wiki page. They query the registry, read the schema descriptions, and know exactly what to expect.

### Versioning Strategy

For the Reddit pipeline, I follow this versioning approach:

1. **Schema versions are sequential integers** (1, 2, 3, ...) managed by Apicurio Registry. I do not use semantic versioning for schemas -- it implies a compatibility promise that the registry already enforces.

2. **Schema files live in version control** alongside the service that produces the data. The `reddit-stream-value.json` file lives in the producer's repository, not in a separate "schema repository."

3. **The CI/CD pipeline registers schemas** during the build. The schema is registered before the service is deployed, ensuring the registry is always ahead of the producers.

4. **Breaking changes get a new artifact ID**, not just a new version. If the `reddit-stream-value` schema needs an incompatible change, I create `reddit-stream-value-v2` as a new artifact. This preserves the history and compatibility chain of the original artifact.

### Governance Checklist

Before approving a schema change in code review, I check:

- [ ] Is the change BACKWARD compatible? (Run `validate-compatibility.sh`)
- [ ] Are new fields optional? (Check the `required` array)
- [ ] Do new fields have descriptions? (Read the `description` values)
- [ ] Is `additionalProperties` absent or set to `true`? (Reject `false`)
- [ ] Are field types unchanged? (No string-to-integer conversions)
- [ ] Is the consumer code updated to handle the new fields' absence?
- [ ] Is there a migration plan if this is a breaking change?

---

## Conclusion

Schema evolution is not a feature you implement once. It is a discipline you practice continuously. The rules are simple:

1. **Choose a compatibility mode and stick to it.** BACKWARD for most pipelines.
2. **New fields are optional.** Always. No exceptions.
3. **Never change a field's type.** Add a new field instead.
4. **The producer owns the schema.** Consumers adapt.
5. **Validate in CI.** Catch incompatible changes before they reach production.
6. **Document in the schema.** Descriptions are part of the contract.

The Reddit pipeline has evolved its prediction schema three times without a single consumer outage. Every evolution was an optional field addition. Every change was validated by Apicurio Registry before deployment. Every consumer was deployed before the producer.

That is not because schema evolution is easy. It is because the guardrails -- the registry, the compatibility rules, the CI checks -- catch the mistakes before they reach production. Build the guardrails first. Then evolve with confidence.

---

## Files in This Repository

```
schema-evolution-events/
  README.md                                    # This article
  examples/
    reddit-stream-v1.json                      # Original reddit-stream schema
    reddit-stream-v2-valid.json                # Valid BACKWARD evolution (optional fields)
    reddit-stream-v2-invalid.json              # Invalid evolution (required fields + additionalProperties)
    kafka-predictions-v2.json                  # Adding third model + ensemble fields
    validate-compatibility.sh                  # Script to check compatibility via Apicurio API
```

---

## References

- [Apicurio Registry Documentation](https://www.apicur.io/registry/docs/)
- [JSON Schema Specification (2020-12)](https://json-schema.org/draft/2020-12/json-schema-core)
- [Confluent Schema Evolution and Compatibility](https://docs.confluent.io/platform/current/schema-registry/fundamentals/schema-evolution.html) -- useful conceptual reference even when using Apicurio
- [Martin Kleppmann - "Designing Data-Intensive Applications"](https://dataintensive.net/) -- Chapter 4 covers encoding and evolution in depth
- ADR-004: Schema Format and Registry Selection (internal)

---

*This article is part of the [Distributed Deep Dives](https://github.com/carlesarnal/distributed-deep-dives) series, exploring the hard problems in distributed systems through real-world case studies.*
