# Multi-Tool Workflow: Schema Compliance Audit

This document walks through a real example of an AI agent (Claude Code) chaining five
Apicurio Registry MCP tools to answer a complex compliance question. Each step shows
the tool call, the parameters, the response, and the AI's reasoning.

---

## The Question

**Developer asks:** "Before our quarterly compliance review, I need to know: which
schemas in our reddit-realtime group are missing descriptions, and for those that
have descriptions, are any of them outdated (last modified more than 6 months ago)?"

This question requires the AI to:
1. List all artifacts in the group
2. Retrieve metadata for each artifact
3. Check for missing descriptions
4. Check modification dates
5. Compile and present findings

---

## Step 1: List All Artifacts in the Group

The AI starts by discovering what artifacts exist in the target group.

**Tool call:**
```json
{
  "tool": "list_artifacts",
  "arguments": {
    "groupId": "reddit-realtime",
    "limit": 100
  }
}
```

**AI reasoning:** "I need to enumerate all artifacts in the reddit-realtime group.
I set a high limit to avoid pagination on the first call — I expect fewer than 100
artifacts in a single group."

**Response:**
```json
{
  "artifacts": [
    {
      "groupId": "reddit-realtime",
      "artifactId": "reddit-post-raw",
      "artifactType": "AVRO",
      "createdOn": "2025-11-15T10:30:00Z",
      "modifiedOn": "2025-11-15T10:30:00Z"
    },
    {
      "groupId": "reddit-realtime",
      "artifactId": "reddit-post-enriched",
      "artifactType": "AVRO",
      "createdOn": "2025-11-20T09:00:00Z",
      "modifiedOn": "2026-02-01T11:30:00Z"
    },
    {
      "groupId": "reddit-realtime",
      "artifactId": "classification-result",
      "artifactType": "AVRO",
      "createdOn": "2025-12-01T14:00:00Z",
      "modifiedOn": "2025-12-01T14:00:00Z"
    },
    {
      "groupId": "reddit-realtime",
      "artifactId": "predictions-event",
      "artifactType": "JSON",
      "createdOn": "2026-01-10T16:45:00Z",
      "modifiedOn": "2026-03-05T09:20:00Z"
    },
    {
      "groupId": "reddit-realtime",
      "artifactId": "user-session-event",
      "artifactType": "AVRO",
      "createdOn": "2025-08-20T08:00:00Z",
      "modifiedOn": "2025-08-20T08:00:00Z"
    }
  ],
  "count": 5
}
```

**AI reasoning:** "There are 5 artifacts. I now need the metadata for each one to check
descriptions. I note that `modifiedOn` is visible here, but descriptions are not
included in the list response — I need to call `get_artifact_metadata` for each."

---

## Step 2: Retrieve Metadata for Each Artifact (Parallel Calls)

The AI makes five parallel tool calls to fetch full metadata for each artifact.

**Tool calls (all issued simultaneously):**

```json
{"tool": "get_artifact_metadata", "arguments": {"groupId": "reddit-realtime", "artifactId": "reddit-post-raw"}}
{"tool": "get_artifact_metadata", "arguments": {"groupId": "reddit-realtime", "artifactId": "reddit-post-enriched"}}
{"tool": "get_artifact_metadata", "arguments": {"groupId": "reddit-realtime", "artifactId": "classification-result"}}
{"tool": "get_artifact_metadata", "arguments": {"groupId": "reddit-realtime", "artifactId": "predictions-event"}}
{"tool": "get_artifact_metadata", "arguments": {"groupId": "reddit-realtime", "artifactId": "user-session-event"}}
```

**Responses:**

```json
// reddit-post-raw
{
  "groupId": "reddit-realtime",
  "artifactId": "reddit-post-raw",
  "name": "Reddit Post Raw Schema",
  "description": "",
  "artifactType": "AVRO",
  "createdOn": "2025-11-15T10:30:00Z",
  "modifiedOn": "2025-11-15T10:30:00Z",
  "labels": {"kafka": "", "reddit": ""}
}

// reddit-post-enriched
{
  "groupId": "reddit-realtime",
  "artifactId": "reddit-post-enriched",
  "name": "Reddit Post Enriched Schema",
  "description": "Reddit post enriched with sentiment analysis scores and topic classification from the Flair NLP model. Used by downstream consumers for filtering and aggregation.",
  "artifactType": "AVRO",
  "createdOn": "2025-11-20T09:00:00Z",
  "modifiedOn": "2026-02-01T11:30:00Z",
  "labels": {"kafka": "", "reddit": "", "enriched": "", "ml-pipeline": ""}
}

// classification-result
{
  "groupId": "reddit-realtime",
  "artifactId": "classification-result",
  "name": "Classification Result",
  "description": "",
  "artifactType": "AVRO",
  "createdOn": "2025-12-01T14:00:00Z",
  "modifiedOn": "2025-12-01T14:00:00Z",
  "labels": {"kafka": ""}
}

// predictions-event
{
  "groupId": "reddit-realtime",
  "artifactId": "predictions-event",
  "name": "Predictions Event",
  "description": "ML prediction results for Reddit post classification. Includes predicted category, confidence score, and model version. Schema follows the team's JSON Schema conventions.",
  "artifactType": "JSON",
  "createdOn": "2026-01-10T16:45:00Z",
  "modifiedOn": "2026-03-05T09:20:00Z",
  "labels": {"kafka": "", "ml-pipeline": "", "predictions": ""}
}

// user-session-event
{
  "groupId": "reddit-realtime",
  "artifactId": "user-session-event",
  "name": "User Session Event",
  "description": "Tracks user session start/end events for the Reddit real-time dashboard.",
  "artifactType": "AVRO",
  "createdOn": "2025-08-20T08:00:00Z",
  "modifiedOn": "2025-08-20T08:00:00Z",
  "labels": {"sessions": "", "dashboard": ""}
}
```

**AI reasoning:** "Now I have all the metadata. Let me analyze:

- **Missing descriptions:** `reddit-post-raw` and `classification-result` have empty
  descriptions.
- **Stale descriptions:** I need to check which described artifacts were last modified
  more than 6 months ago. Today is March 2026, so the cutoff is September 2025.
  - `user-session-event` was last modified 2025-08-20 — that is over 6 months ago, and
    it does have a description.
  - The other described artifacts (`reddit-post-enriched`, `predictions-event`) were
    modified recently."

---

## Step 3: Investigate Schema Content for Missing Descriptions

The AI decides to read the actual schema content of the artifacts missing descriptions
so it can suggest descriptions.

**Tool calls (parallel):**

```json
{"tool": "get_version_content", "arguments": {"groupId": "reddit-realtime", "artifactId": "reddit-post-raw", "versionExpression": "branch=latest"}}
{"tool": "get_version_content", "arguments": {"groupId": "reddit-realtime", "artifactId": "classification-result", "versionExpression": "branch=latest"}}
```

**Responses:**

```json
// reddit-post-raw content
{
  "type": "record",
  "name": "RedditPostRaw",
  "namespace": "io.apicurio.reddit.raw",
  "fields": [
    {"name": "post_id", "type": "string", "doc": "Unique Reddit post identifier"},
    {"name": "subreddit", "type": "string", "doc": "Subreddit where the post was made"},
    {"name": "title", "type": "string", "doc": "Post title"},
    {"name": "body", "type": ["null", "string"], "doc": "Post body text, null for link posts"},
    {"name": "author", "type": "string", "doc": "Reddit username of the post author"},
    {"name": "created_utc", "type": "long", "doc": "Unix timestamp of post creation"},
    {"name": "score", "type": "int", "doc": "Net upvote score at time of ingestion"},
    {"name": "url", "type": ["null", "string"], "doc": "URL for link posts"}
  ]
}

// classification-result content
{
  "type": "record",
  "name": "ClassificationResult",
  "namespace": "io.apicurio.reddit.classification",
  "fields": [
    {"name": "post_id", "type": "string"},
    {"name": "category", "type": "string"},
    {"name": "subcategory", "type": ["null", "string"]},
    {"name": "confidence", "type": "double"},
    {"name": "model_name", "type": "string"},
    {"name": "classified_at", "type": "long"}
  ]
}
```

**AI reasoning:** "Now I can generate meaningful descriptions based on the schema
content. I also notice that `classification-result` is missing field-level
documentation (`doc` attributes), unlike `reddit-post-raw` which has them. This is
an additional compliance issue worth flagging."

---

## Step 4: Present Findings

The AI compiles all gathered information into a structured report.

**AI response to the developer:**

```
Compliance Audit Results for reddit-realtime Group
===================================================

SCHEMAS MISSING DESCRIPTIONS (2 of 5):

  1. reddit-post-raw (AVRO)
     - Last modified: 2025-11-15 (4 months ago)
     - Suggested description: "Raw Reddit post data ingested from the Reddit API.
       Contains post ID, subreddit, title, body text, author, creation timestamp,
       score, and URL. Used as the entry point for the real-time classification
       pipeline."

  2. classification-result (AVRO)
     - Last modified: 2025-12-01 (3.5 months ago)
     - Suggested description: "Output of the ML classification model applied to
       Reddit posts. Contains the predicted category, subcategory, confidence score,
       model name, and classification timestamp."
     - Additional issue: This schema is missing field-level documentation (doc
       attributes on Avro fields), unlike other schemas in this group.

SCHEMAS WITH POTENTIALLY OUTDATED DESCRIPTIONS (1 of 5):

  3. user-session-event (AVRO)
     - Last modified: 2025-08-20 (7 months ago — exceeds 6-month threshold)
     - Current description: "Tracks user session start/end events for the Reddit
       real-time dashboard."
     - Recommendation: Review this description to ensure it still accurately
       reflects the schema's purpose and usage.

SCHEMAS IN GOOD STANDING (2 of 5):

  4. reddit-post-enriched (AVRO) — Last modified 2026-02-01, description present
  5. predictions-event (JSON) — Last modified 2026-03-05, description present

Would you like me to:
  a) Update the descriptions for reddit-post-raw and classification-result
     using the suggestions above?
  b) Show the full content of user-session-event so you can review its description?
  c) Generate field-level documentation for classification-result?
```

---

## Step 5 (Optional): Apply Fixes

If the developer says "yes, update the descriptions," the AI makes two more tool calls:

**Tool calls:**

```json
{
  "tool": "update_artifact_metadata",
  "arguments": {
    "groupId": "reddit-realtime",
    "artifactId": "reddit-post-raw",
    "description": "Raw Reddit post data ingested from the Reddit API. Contains post ID, subreddit, title, body text, author, creation timestamp, score, and URL. Used as the entry point for the real-time classification pipeline."
  }
}

{
  "tool": "update_artifact_metadata",
  "arguments": {
    "groupId": "reddit-realtime",
    "artifactId": "classification-result",
    "description": "Output of the ML classification model applied to Reddit posts. Contains the predicted category, subcategory, confidence score, model name, and classification timestamp."
  }
}
```

---

## Summary

| Step | Tool | Purpose |
|------|------|---------|
| 1 | `list_artifacts` | Discover all artifacts in the group |
| 2 | `get_artifact_metadata` (x5) | Retrieve descriptions and modification dates |
| 3 | `get_version_content` (x2) | Read schema content to generate descriptions |
| 4 | *(AI reasoning)* | Analyze, compare dates, compile report |
| 5 | `update_artifact_metadata` (x2) | Apply description fixes |

**Total tool calls:** 9 (or 7 if the developer does not request fixes)
**Total time:** Under 30 seconds for the full workflow
**Manual equivalent:** 15-20 minutes of clicking through a registry UI, copy-pasting
schemas, writing descriptions, and updating metadata fields individually

---

## Key Takeaways

1. **Parallel tool calls matter.** Steps 2 and 3 issue multiple tool calls
   simultaneously. Sequential execution would double the wall-clock time.

2. **The AI adds reasoning, not just execution.** The AI did not just list artifacts —
   it identified compliance gaps, generated descriptions from schema content, flagged
   additional issues (missing field docs), and offered next steps.

3. **Domain knowledge compounds.** The AI's understanding of Avro schemas (knowing that
   `doc` attributes are best practice) combines with the registry metadata to produce
   insights that neither the API nor the AI could generate alone.

4. **The workflow is conversational.** The developer stays in the conversation, guiding
   the AI's actions. The AI proposes; the developer disposes. This keeps humans in the
   loop for write operations while automating the tedious read-and-analyze work.
