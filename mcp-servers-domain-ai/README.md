# MCP Servers for Domain-Specific AI Tooling: Lessons from Apicurio Registry

**Author:** Carles Arnal — Principal Software Engineer at IBM, core contributor to Apicurio Registry
**Date:** March 2026

---

## Table of Contents

1. [Introduction](#introduction)
2. [What MCP Is and Why It Matters](#what-mcp-is-and-why-it-matters)
3. [Designing the Tool Surface](#designing-the-tool-surface)
4. [Registry Operations as AI Tools](#registry-operations-as-ai-tools)
5. [Prompt Templates via MCP](#prompt-templates-via-mcp)
6. [Security Considerations](#security-considerations)
7. [Patterns for Wrapping Any REST API as an MCP Server](#patterns-for-wrapping-any-rest-api-as-an-mcp-server)
8. [Conclusion](#conclusion)

---

## Introduction

Large language models are remarkably capable generalists. They can write code, explain
algorithms, and reason about system design. But ask one to check whether your latest
Avro schema is backward compatible with the version currently deployed in your staging
registry, and you hit a wall. The model does not know what schemas you have. It does not
have access to your registry. It cannot call your API. It can only guess.

This is the fundamental gap that the **Model Context Protocol (MCP)** was designed to
close. MCP, introduced by Anthropic, provides a standardized way for AI agents to
discover and invoke tools exposed by external systems — schema registries, databases,
CI/CD pipelines, observability platforms, anything with an API.

Over the past year, I built and iterated on an MCP server that wraps the
[Apicurio Registry](https://www.apicur.io/registry/) REST API, exposing schema
governance operations as tools that AI agents like Claude Code can call directly. The
server is not a prototype sitting in a branch somewhere. It is part of my daily
development workflow. When I need to inspect schemas, validate compatibility, search
for artifacts, or manage versions across groups, I do it through natural language
backed by real API calls.

This article distills the lessons from that experience. It covers the architecture
of MCP, the design decisions behind the tool surface, concrete examples of AI-driven
registry operations, prompt template feedback loops, security considerations, and
generalizable patterns for wrapping any REST API as an MCP server.

If you maintain a domain-specific system and want to make it accessible to AI agents,
this is a field report from the front lines.

---

## What MCP Is and Why It Matters

### The Problem: Islands of Context

Modern software engineering involves dozens of specialized systems. A single team might
use Kafka for messaging, Apicurio Registry for schema governance, ArgoCD for
deployments, Prometheus for monitoring, and PostgreSQL for persistence. Each system has
its own API, its own data model, its own operational semantics.

AI coding assistants — even powerful ones — operate in a vacuum unless you give them
context. They can read your code. They can read your terminal output. But they cannot
reach into your schema registry to check what version of a schema is deployed, or
query your CI/CD system to see why a pipeline failed. The developer becomes a manual
bridge, copy-pasting data between the AI and their tools.

This is a workflow bottleneck that scales poorly. The more systems you use, the more
context-switching overhead the developer absorbs.

### MCP: A Standard Protocol for Tool Exposure

The Model Context Protocol addresses this by defining a standard interface between AI
clients (like Claude Code, Cursor, or any MCP-compatible agent) and tool servers. The
protocol specifies three core primitives:

**Tools** are functions the AI can call. Each tool has a name, a natural-language
description, and a typed parameter schema (JSON Schema). The AI reads the description
to decide when to use the tool, constructs the parameters, calls it, and interprets the
result. Tools are the workhorses of MCP — they are how the AI *acts* on external
systems.

```
Tool: search_artifacts
Description: "Search for artifacts in the registry using keywords, labels, or other criteria"
Parameters: { name: string?, description: string?, labels: string[]?, groupId: string?, ... }
Returns: { artifacts: [...], count: number }
```

**Resources** are data endpoints the AI can read, analogous to GET endpoints in REST.
They provide contextual information — configuration files, documentation, system state
— without side effects. Resources are how the AI *learns* about the current state of
an external system.

**Prompts** are reusable prompt templates that the server can provide to the AI client.
They allow the server to guide the AI's behavior with domain-specific instructions,
creating a powerful feedback loop where the system being managed helps shape how the AI
manages it.

### Transport: How Client and Server Communicate

MCP supports multiple transport mechanisms:

- **stdio** — The AI client spawns the MCP server as a subprocess and communicates over
  standard input/output. This is the most common transport for local development tools.
  The Apicurio Registry MCP server uses this approach: Claude Code launches it as a
  child process, sends JSON-RPC messages to stdin, and reads responses from stdout.

- **SSE (Server-Sent Events)** — The client connects to the server over HTTP with a
  persistent SSE connection for server-to-client messages and regular HTTP POST for
  client-to-server messages. Useful for remote servers.

- **Streamable HTTP** — A newer transport option that uses standard HTTP requests with
  optional streaming, offering better compatibility with existing infrastructure like
  load balancers and API gateways.

For the Apicurio Registry MCP server, stdio is the natural choice. The server runs
locally, starts in under a second, and has direct access to the registry instance
(which may be local or remote). There is no need for a separate deployment.

### How MCP Compares to Alternatives

Before MCP, several approaches existed for giving AI models access to external tools.
Each had significant limitations:

| Approach | Scope | Limitations |
|----------|-------|-------------|
| **OpenAI Function Calling** | Model-specific | Tied to OpenAI's API. Not portable across providers. No standard discovery. |
| **LangChain Tools** | Framework-specific | Requires adopting the LangChain framework. Python-centric. Tight coupling. |
| **OpenAI Plugins** | Platform-specific | Deprecated. Required OpenAI's marketplace. No local development story. |
| **Custom integrations** | One-off | Every AI client reinvents the wheel. No reuse across tools. |
| **MCP** | Universal | Provider-agnostic. Client-agnostic. Standard discovery. Local-first. |

The key insight behind MCP is that tool exposure should be a *protocol*, not a
*feature* of any single model or framework. A well-built MCP server works with any
MCP-compatible client — today that includes Claude Code, Claude Desktop, Cursor,
Windsurf, and a growing ecosystem of other tools.

This is what makes investing in an MCP server worthwhile. You build it once, and every
MCP-compatible AI agent can use it.

---

## Designing the Tool Surface

### The Most Critical Decision

When wrapping a REST API as an MCP server, the most consequential decision is not how
to implement the tools — it is *which operations to expose*. Not every API endpoint
should become a tool. The tool surface is the contract between your system and the AI
agent, and getting it wrong creates either a crippled agent or a dangerous one.

The Apicurio Registry REST API has dozens of endpoints. The MCP server exposes 25+
of them as tools, organized into six categories:

| Category | Tools | Purpose |
|----------|-------|---------|
| **Schema/Artifact Management** | `create_artifact`, `list_artifacts`, `search_artifacts`, `get_artifact_metadata`, `update_artifact_metadata` | Core CRUD and discovery for schemas and other artifacts |
| **Version Management** | `create_version`, `list_versions`, `get_version_content`, `get_version_metadata`, `update_version_content`, `update_version_metadata`, `update_version_state` | Lifecycle management of artifact versions |
| **Group Management** | `create_group`, `list_groups`, `search_groups`, `get_group_metadata`, `update_group_metadata` | Organizational structure for artifacts |
| **Configuration** | `list_configuration_properties`, `get_configuration_property`, `update_configuration_property` | Registry-wide settings |
| **Server Info** | `get_server_info`, `get_artifact_types` | Introspection and capability discovery |
| **Prompt Templates** | `list_mcp_prompts`, `get_mcp_prompt`, `render_prompt_template` | Server-provided prompt management |

### Design Principles for Tool Selection

After iterating on this tool surface across several months of daily use, a set of
principles emerged:

**1. Expose operations that benefit from AI reasoning.**

Search, analysis, and comparison operations are high-value tools because the AI can
interpret results, correlate information across multiple calls, and synthesize answers
to complex questions. `search_artifacts` is one of the most-used tools because the AI
can take a vague user request ("find all the Kafka-related schemas") and translate it
into the right search parameters.

**2. Expose read operations liberally.**

Read operations are safe — they do not mutate state. Exposing `get_version_content`,
`get_artifact_metadata`, `list_versions`, and similar tools gives the AI deep
visibility into the registry. The more context the AI has, the better its reasoning.
There is no downside to letting it read.

**3. Expose write operations deliberately.**

Write operations require more care. The Apicurio MCP server exposes `create_artifact`,
`create_version`, `update_artifact_metadata`, and similar tools, but each one is
designed with guardrails. For example, `create_version` can be used with a dry-run
flag for compatibility validation without actually creating the version.

**4. Omit destructive operations unless there is a clear use case.**

The MCP server does not expose `delete_artifact` or `delete_version`. These operations
are irreversible and rarely needed in an AI-assisted workflow. If a developer truly
needs to delete something, they can use the registry UI or CLI directly. The AI agent
should not have a convenient "delete everything" button.

**5. Limit blast radius operations.**

Operations that affect the entire registry — bulk deletes, configuration resets, rule
changes that affect all artifacts — are either omitted or gated behind additional
confirmation patterns.

### Naming Tools for Discoverability

The AI agent discovers tools by reading their names and descriptions. Good naming is
not cosmetic — it directly affects whether the AI selects the right tool for a task.

Principles that work well in practice:

- **Use verb_noun format**: `search_artifacts`, `get_version_content`,
  `create_group`. This mirrors natural language and makes the action clear.

- **Be specific over generic**: `get_version_content` is better than `get_content`
  because the AI can distinguish it from other content-retrieval tools in a
  multi-server setup.

- **Use domain vocabulary**: `artifact`, `version`, `group` are Apicurio Registry
  concepts. Using them in tool names teaches the AI the domain model implicitly.

- **Prefix related tools consistently**: All version tools start with a version-related
  verb and include `version` in the name. This helps the AI see the tool groups.

### Parameter Design: Typed Schemas

Every tool parameter has a JSON Schema definition. This is not optional decoration — it
is the mechanism by which the AI knows what data to provide.

Effective parameter design:

```json
{
  "name": "search_artifacts",
  "description": "Search for artifacts in the Apicurio Registry. Supports filtering by name, description, labels, group, and content type. Returns paginated results.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "name": {
        "type": "string",
        "description": "Filter by artifact name (substring match)"
      },
      "groupId": {
        "type": "string",
        "description": "Filter by group ID. Use 'default' for the default group."
      },
      "labels": {
        "type": "array",
        "items": { "type": "string" },
        "description": "Filter by labels. Artifacts must have ALL specified labels."
      },
      "offset": {
        "type": "integer",
        "description": "Pagination offset (0-based). Default: 0",
        "default": 0
      },
      "limit": {
        "type": "integer",
        "description": "Maximum number of results to return. Default: 20, Max: 500",
        "default": 20
      },
      "orderby": {
        "type": "string",
        "enum": ["name", "createdOn", "modifiedOn"],
        "description": "Field to order results by"
      },
      "order": {
        "type": "string",
        "enum": ["asc", "desc"],
        "description": "Sort order direction"
      }
    },
    "required": []
  }
}
```

Key observations:

- **Descriptions are instructions.** The `groupId` description tells the AI to use
  `'default'` for the default group. Without this, the AI would not know that convention.

- **Enums constrain choices.** The `orderby` and `order` fields use enums so the AI
  cannot send invalid values.

- **Defaults reduce friction.** The AI does not need to specify `offset` and `limit`
  for simple queries.

- **Array types with item schemas.** The `labels` field specifies both the array type
  and the item type, ensuring the AI sends the right structure.

- **No required fields for search.** All search parameters are optional, letting the
  AI use as few or as many filters as needed.

See [`examples/mcp-tool-definition.json`](examples/mcp-tool-definition.json) for a
complete, annotated example of a well-designed tool definition.

---

## Registry Operations as AI Tools

The real power of MCP tools emerges not from individual tool calls, but from the AI's
ability to *chain* multiple tools to answer complex questions. Here are concrete
examples drawn from real usage of the Apicurio Registry MCP server.

### Example 1: Discovering Schemas in a Group

**User prompt:** "What schemas do we have in the reddit-realtime group?"

**AI behavior:**

1. Calls `list_artifacts` with `groupId: "reddit-realtime"`
2. Receives a list of artifacts with their IDs, names, types, and creation dates
3. Synthesizes a human-readable summary

```
The reddit-realtime group contains 4 artifacts:

1. reddit-post-raw (AVRO) - Raw Reddit post schema, created 2025-11-15
2. reddit-post-enriched (AVRO) - Enriched post with sentiment scores, created 2025-11-20
3. classification-result (AVRO) - ML classification output, created 2025-12-01
4. predictions-event (JSON) - Prediction event for downstream consumers, created 2026-01-10
```

This is a single-tool interaction, but the AI adds value by formatting the output
and highlighting relevant details like artifact types and creation dates.

### Example 2: Inspecting a Specific Schema Version

**User prompt:** "Show me version 2 of the predictions schema and explain the changes from version 1."

**AI behavior:**

1. Calls `get_version_content` with `groupId: "reddit-realtime"`, `artifactId: "predictions-event"`, `versionExpression: "2"`
2. Calls `get_version_content` with the same artifact but `versionExpression: "1"`
3. Compares the two schemas
4. Explains the differences in natural language

```
Version 2 of predictions-event added two fields:

- "confidence_score" (number): A float between 0 and 1 indicating model confidence
- "model_version" (string): The version of the ML model that produced the prediction

Both fields are optional (nullable), making this a backward-compatible change.
No fields were removed or renamed.
```

This is a two-tool interaction where the AI uses its reasoning capabilities to
perform a semantic diff — not just a textual diff, but an understanding of what
the structural changes mean for compatibility.

### Example 3: Validating Schema Compatibility

**User prompt:** "Is this schema change backward compatible?"

The user pastes a modified Avro schema. The AI needs to determine:
- Which artifact this schema belongs to
- What the current version looks like
- Whether the proposed change is compatible

**AI behavior:**

1. Calls `search_artifacts` to find the artifact matching the schema name/namespace
2. Calls `get_version_content` to retrieve the latest version
3. Calls `create_version` with the new schema content and `isDryRun: true`
4. Interprets the result: if the dry-run succeeds, the change is compatible

The dry-run pattern is particularly powerful here. The AI validates compatibility
*using the registry's own compatibility engine* without actually creating a new version.
The registry enforces whatever compatibility rules are configured (BACKWARD, FORWARD,
FULL, NONE), and the AI reports the result.

If the dry-run fails, the AI receives the registry's error message — which includes
details about which fields or types are incompatible — and can explain the issue to the
developer in plain language.

### Example 4: Exploring Registry Capabilities

**User prompt:** "What artifact types does this registry support?"

**AI behavior:**

1. Calls `get_artifact_types`
2. Returns the list of supported types

```
This Apicurio Registry instance supports the following artifact types:

- AVRO - Apache Avro schemas (.avsc)
- PROTOBUF - Protocol Buffer definitions (.proto)
- JSON - JSON Schema
- OPENAPI - OpenAPI specifications (v2 and v3)
- ASYNCAPI - AsyncAPI specifications
- GRAPHQL - GraphQL schema definitions
- KCONNECT - Kafka Connect schemas
- WSDL - Web Services Description Language
- XSD - XML Schema Definition
- XML - Raw XML documents
- PROMPT_TEMPLATE - MCP prompt templates (custom type)
```

This is a simple introspection call, but it is valuable when the AI needs to validate
that a particular artifact type is supported before attempting to create an artifact.

### Example 5: Cross-Group Schema Search

**User prompt:** "Find all schemas across all groups that reference user IDs."

**AI behavior:**

1. Calls `list_groups` to enumerate all groups
2. For each group, calls `search_artifacts` or `list_artifacts`
3. For promising artifacts, calls `get_version_content` to inspect the schema body
4. Filters results to those containing user ID fields
5. Presents a consolidated view

This multi-step workflow demonstrates the AI's ability to perform exhaustive searches
that would be tedious for a developer to do manually. The AI handles pagination,
iterates across groups, and applies semantic filtering (understanding that
`user_id`, `userId`, `USER_ID`, and `author_id` might all be relevant matches).

### Example 6: Auditing Schema Metadata

**User prompt:** "Which schemas are missing descriptions? We need to fix that before the compliance review."

**AI behavior:**

1. Calls `list_groups` to get all groups
2. For each group, calls `list_artifacts` to get all artifacts
3. For each artifact, calls `get_artifact_metadata` to check for descriptions
4. Compiles a list of artifacts with empty or missing description fields

```
Found 7 artifacts without descriptions:

Group: reddit-realtime
  - reddit-post-raw (AVRO)
  - classification-result (AVRO)

Group: internal-events
  - audit-log-entry (JSON)
  - user-session-event (AVRO)
  - notification-payload (JSON)

Group: default
  - test-schema-1 (AVRO)
  - legacy-format (PROTOBUF)

Would you like me to generate descriptions for these schemas based on their content?
```

The AI even offers to take the next step — reading each schema's content and generating
appropriate descriptions that can be set via `update_artifact_metadata`. This turns a
multi-hour audit task into a five-minute interaction.

### The Chaining Pattern

All of these examples share a common pattern: the AI decomposes a high-level question
into a sequence of tool calls, accumulates context across calls, and synthesizes a
response that is more than the sum of individual API responses.

This is fundamentally different from a traditional CLI or UI workflow where the
developer must:
1. Know which API endpoints to call
2. Remember the parameter names and formats
3. Make each call manually
4. Mentally correlate the results
5. Draw conclusions

With MCP, the developer states the intent and the AI handles the execution. The
developer's expertise shifts from "knowing how to use the API" to "knowing what
questions to ask."

See [`examples/multi-tool-workflow.md`](examples/multi-tool-workflow.md) for a detailed
step-by-step walkthrough of an AI agent chaining five registry tools to answer a complex
compliance question.

---

## Prompt Templates via MCP

### The Feedback Loop

One of the most distinctive features of the Apicurio Registry MCP server is its
support for prompt templates as a first-class MCP primitive. This creates a unique
feedback loop: the registry stores prompt templates that help the AI use the registry
more effectively.

Apicurio Registry supports a custom artifact type called `PROMPT_TEMPLATE`. These are
artifacts stored in the registry just like Avro schemas or OpenAPI specs, but their
content is a prompt template with variable placeholders. The MCP server exposes three
tools for working with them:

- **`list_mcp_prompts`** — Lists all `PROMPT_TEMPLATE` artifacts that are flagged as
  MCP-enabled in the registry. Returns their names, descriptions, and argument lists.

- **`get_mcp_prompt`** — Retrieves a specific prompt template by name, returning the
  raw template content with its variable placeholders.

- **`render_prompt_template`** — Takes a prompt template name and a set of variable
  values, renders the template with those values substituted, and returns the final
  prompt text.

### How It Works in Practice

Consider a team that frequently needs to review schemas for compliance with internal
standards. They create a prompt template artifact in the registry:

```
Artifact ID: schema-review-prompt
Type: PROMPT_TEMPLATE
Group: mcp-prompts

Content:
---
Review the following {{schema_type}} schema for compliance with our internal standards:

Schema name: {{schema_name}}
Schema content:
{{schema_content}}

Check for:
1. All fields must have documentation/descriptions
2. Naming convention: snake_case for field names
3. Required fields must include: id, created_at, version
4. No use of generic types (e.g., "string" for dates — use logical types)
5. Backward compatibility with the previous version

Provide a compliance score (0-100) and list specific violations.
---
```

An AI agent can then:

1. Call `list_mcp_prompts` to discover available prompt templates
2. Call `get_version_content` to retrieve a schema that needs review
3. Call `render_prompt_template` with the schema content as a variable
4. Use the rendered prompt to perform the review with domain-specific criteria

The prompt template encodes organizational knowledge — what "compliance" means for
this team, what naming conventions they follow, what fields are required. This
knowledge lives in the registry alongside the schemas it governs, versioned and
managed with the same lifecycle tools.

### Why This Matters

Prompt templates stored in the registry provide several advantages over prompts
hardcoded in client configurations or scattered across developer notes:

**Versioning.** Prompt templates are artifacts with versions. When the compliance
criteria change, the team creates a new version of the template. Previous versions
are preserved for audit purposes.

**Discoverability.** Any AI agent connected to the registry can discover available
prompt templates via `list_mcp_prompts`. New team members do not need to know where
the prompts are stored — they are part of the registry's content.

**Consistency.** All team members and all AI agents use the same prompt templates.
There is no drift between one developer's custom prompt and another's.

**Governance.** The same access controls, approval workflows, and audit logging that
apply to schemas also apply to prompt templates. Changing a compliance review prompt
is a governed action, not an ad-hoc edit.

**Composability.** The AI can chain prompt template rendering with other registry
operations. Retrieve a schema, render a review prompt with the schema content, execute
the review, and then update the artifact metadata with the results — all in a single
conversational flow.

### Prompt Template Design Patterns

Through experimentation, several patterns have emerged for effective prompt templates:

**Parameterized analysis prompts.** Templates that take a schema or artifact as input
and produce structured analysis. The compliance review example above is one instance.
Others include migration analysis ("Given the schema {{old_schema}}, generate a
migration plan to {{new_schema}}") and documentation generation.

**Workflow guidance prompts.** Templates that guide the AI through a multi-step
workflow. For example: "To onboard a new event type, follow these steps: 1) Create a
group if one does not exist for the domain, 2) Create the schema artifact with the
correct naming convention, 3) Set the compatibility rule to BACKWARD, 4) Add required
labels..."

**Context injection prompts.** Templates that provide domain context the AI would not
otherwise have. "Our registry uses the following group naming convention:
{{team}}-{{environment}}. Artifact IDs follow the pattern {{domain}}-{{event_type}}.
When searching for schemas, use these conventions."

These patterns blur the line between "documentation" and "executable instructions."
The prompt template is documentation that the AI can directly consume and act on.

---

## Security Considerations

### The Trust Boundary

An MCP server is a trust boundary. On one side is the AI agent — powerful but
potentially unpredictable. On the other side is your production infrastructure —
a schema registry that your Kafka consumers depend on. The MCP server mediates
between them.

Getting security right is not optional. A misconfigured MCP server could allow an
AI agent to delete production schemas, change compatibility rules, or exfiltrate
sensitive data. These are not theoretical risks — they are direct consequences of
exposing write operations without appropriate controls.

### Read-Only vs. Read-Write Access

The simplest security model is to make the MCP server read-only. Expose only
tools that read data: `list_artifacts`, `get_version_content`, `search_artifacts`,
`get_artifact_metadata`, and so on. Remove all create, update, and delete tools.

This is a good starting point for production registries. An AI agent with read-only
access can still answer questions, analyze schemas, perform compliance checks, and
compare versions. It just cannot modify anything.

For development and staging environments, read-write access is appropriate and
unlocks the full power of AI-assisted schema management. The Apicurio MCP server
supports this configuration through the underlying registry's authentication and
authorization mechanisms.

A middle ground is **read-plus-validate** access: the AI can read everything and
can use dry-run operations to validate changes, but cannot actually commit changes.
This gives the AI the ability to say "yes, this schema change is compatible" without
the ability to push it.

### The Dry-Run Pattern

The dry-run pattern deserves special attention because it is one of the most effective
security controls for MCP servers.

When the AI calls `create_version` with `isDryRun: true`, the registry validates the
new version against its compatibility rules, checks the content for syntactic
correctness, and returns either a success response or a detailed error — but it does
not actually create the version.

This pattern provides:

- **Validation without mutation.** The AI can test proposed changes without risk.
- **Rich error messages.** The registry's compatibility engine provides detailed
  explanations of why a change is incompatible.
- **Confidence for the developer.** The developer can see the AI's validation results
  and then decide whether to apply the change.

Other domains can adopt this pattern. A CI/CD MCP server could support dry-run
deployments. A database MCP server could support dry-run migrations. The key insight
is that *validation is almost always safe*, even when mutation is not.

### Rate Limiting

AI agents can be aggressive consumers of APIs. A single user prompt like "audit all
schemas across all groups" can trigger dozens or hundreds of API calls as the AI
iterates through groups, artifacts, and versions.

Rate limiting should be applied at the MCP server level, not just at the registry
level. The server can:

- Limit the total number of tool calls per conversation or per time window
- Throttle calls to write operations more aggressively than read operations
- Reject requests that would exceed a pagination threshold (e.g., refuse to iterate
  through more than 1,000 artifacts in a single workflow)

In practice, the Apicurio Registry MCP server relies on the registry's built-in
rate limiting and the AI client's own request management. But for production
deployments, additional server-side controls are advisable.

### Audit Logging

Every tool invocation by an AI agent should be logged. The audit log should capture:

- **Who:** Which user's AI agent made the call
- **What:** Which tool was called with which parameters
- **When:** Timestamp
- **Result:** Success or failure, with error details
- **Context:** The conversation ID or session ID, so related calls can be correlated

This is particularly important for write operations. If an AI agent creates a schema
version that breaks a consumer, the audit log should make it straightforward to trace
the change back to the specific AI interaction that caused it.

Apicurio Registry provides audit logging capabilities that can be leveraged by the
MCP server. The server passes the user's identity through to the registry API, so
all operations are logged under the correct user — not under a generic "mcp-server"
service account.

### The Principle of Least Privilege

Apply the principle of least privilege at every layer:

- **Tool level:** Only expose the tools the AI needs. If your workflow does not require
  creating artifacts, do not expose `create_artifact`.

- **Parameter level:** If certain parameter values are dangerous (e.g., setting
  compatibility to NONE), validate them in the MCP server before forwarding to the
  registry.

- **Scope level:** If the AI should only manage schemas in certain groups, filter
  the results and reject operations on other groups.

- **Identity level:** The MCP server should authenticate to the registry with
  credentials that have the minimum necessary permissions. A read-only service account
  for production; a read-write account for development.

### What Can Go Wrong

It is worth being explicit about failure modes:

| Failure Mode | Cause | Mitigation |
|-------------|-------|------------|
| Mass deletion | Delete tools exposed without guardrails | Do not expose delete operations |
| Compatibility bypass | AI sets compatibility rule to NONE | Validate configuration changes in the MCP server |
| Schema pollution | AI creates test artifacts in production | Restrict create operations by environment |
| Data exfiltration | AI reads schemas and sends content externally | The AI client's context stays within the conversation |
| Denial of service | AI floods the registry with API calls | Rate limiting at the MCP server level |
| Privilege escalation | AI modifies configuration to grant broader access | Limit configuration tools to non-security settings |

None of these are unique to AI agents — a developer with API access can cause all the
same problems. But AI agents operate at machine speed and may not exercise the same
judgment a human would. Defense in depth is essential.

---

## Patterns for Wrapping Any REST API as an MCP Server

The Apicurio Registry MCP server is one instance of a general pattern: taking a
domain-specific REST API and exposing it through MCP. The lessons learned apply to
any API — databases, CI/CD systems, monitoring platforms, cloud providers.

### Pattern 1: Map REST Resources to Tool Groups

REST APIs are organized around resources (nouns). MCP tools are organized around
operations (verbs on nouns). The mapping is natural:

```
REST Resource          MCP Tool Group
--------------------   -------------------------
/groups                create_group, list_groups, search_groups, get_group_metadata
/groups/{id}/artifacts create_artifact, list_artifacts, search_artifacts
/artifacts/{id}/versions create_version, list_versions, get_version_content
/admin/config          list_configuration_properties, get_configuration_property
```

Do not create a tool for every HTTP method on every endpoint. Instead, identify the
operations that are meaningful for an AI workflow and expose those.

Guidelines:
- **GET /collection** becomes a `list_*` or `search_*` tool
- **GET /resource/{id}** becomes a `get_*` tool
- **POST /collection** becomes a `create_*` tool
- **PUT /resource/{id}** becomes an `update_*` tool
- **DELETE /resource/{id}** — consider omitting entirely, or gate behind confirmation

### Pattern 2: Use the API's Own Error Messages

When a tool call results in an API error, pass the error message through to the AI
*verbatim*. Do not sanitize it, summarize it, or replace it with a generic message.

API error messages are written for developers. They contain information about what
went wrong, which field was invalid, what the expected format was. The AI can read
and interpret these messages, often providing a helpful explanation to the user.

```
// Bad: hiding the error
return { error: "Schema creation failed" }

// Good: passing the API error through
return {
  error: "Schema creation failed",
  statusCode: 409,
  detail: "Artifact with ID 'user-event' already exists in group 'default'. Use create_version to add a new version to the existing artifact."
}
```

The AI can read the detailed error and suggest the correct next step ("It looks like
this artifact already exists. Would you like me to create a new version instead?").

### Pattern 3: Provide Rich Descriptions

Tool descriptions are the primary mechanism by which the AI decides which tool to
use. They should be comprehensive enough for the AI to make correct decisions without
external documentation.

A good tool description includes:

- **What the tool does** in one sentence
- **When to use it** vs. similar tools
- **What the parameters mean** with examples
- **What the return value contains**
- **Edge cases and limitations**

```json
{
  "name": "search_artifacts",
  "description": "Search for artifacts in the Apicurio Registry using various filter criteria. Use this tool when you need to find artifacts by name, description, labels, or other properties. Unlike list_artifacts which returns all artifacts in a group, search_artifacts works across all groups and supports text-based search. Returns paginated results — use offset and limit for large result sets. Maximum 500 results per request."
}
```

Compare with a poor description:

```json
{
  "name": "search_artifacts",
  "description": "Searches artifacts."
}
```

The AI cannot distinguish "search artifacts" from "list artifacts" with the poor
description. It cannot determine pagination limits or cross-group behavior. It will
make wrong choices.

### Pattern 4: Include Examples in Descriptions

For complex parameters, include examples directly in the description:

```json
{
  "name": "labels",
  "type": "array",
  "items": { "type": "string" },
  "description": "Filter by labels. Artifacts must have ALL specified labels. Example: [\"kafka\", \"production\"] returns only artifacts labeled with both 'kafka' AND 'production'."
}
```

Examples are worth more than explanations for AI models. The model can pattern-match
on examples much more reliably than it can parse complex specifications.

### Pattern 5: Handle Pagination Transparently

REST APIs typically paginate large result sets. The MCP server should handle pagination
in a way that is useful to the AI without overwhelming it.

Options (in order of preference):

1. **Expose pagination parameters.** Let the AI control offset and limit. This is what
   the Apicurio MCP server does. The AI can request the first page, check the total
   count, and decide whether to fetch more.

2. **Auto-paginate for small result sets.** If the total count is below a threshold
   (e.g., 100), fetch all pages and return the complete result. If above, return the
   first page with a count so the AI knows there is more.

3. **Never auto-paginate.** Always return exactly one page and let the AI decide
   whether to fetch more. This is the safest option for large datasets.

Avoid silently returning partial results without indicating that pagination is
available. The AI needs to know when it has seen everything and when there is more.

### Pattern 6: Consider Caching for Frequently-Read Data

Some data changes infrequently and is read on almost every interaction:

- Server info and capabilities (`get_server_info`, `get_artifact_types`)
- Group lists (typically stable within a session)
- Configuration properties

The MCP server can cache these responses for the duration of a session (or with a
short TTL) to reduce API load. This is especially valuable when the AI chains
multiple tools — if `list_groups` is called three times in a single workflow, there
is no need to hit the API three times.

Be cautious with caching write operations or data that changes frequently (artifact
versions, search results).

### Pattern 7: Structure Return Values for AI Consumption

The AI reads your tool's return values and makes decisions based on them. Structure
them for clarity:

```json
{
  "artifacts": [
    {
      "groupId": "reddit-realtime",
      "artifactId": "reddit-post-raw",
      "artifactType": "AVRO",
      "name": "Reddit Post Raw Schema",
      "createdOn": "2025-11-15T10:30:00Z",
      "modifiedOn": "2026-01-20T14:15:00Z",
      "labels": ["kafka", "reddit", "raw-event"]
    }
  ],
  "count": 1,
  "totalCount": 47,
  "hasMore": true,
  "nextOffset": 20
}
```

Key elements:
- **Include metadata** (types, dates, labels) so the AI can filter and reason without
  additional calls.
- **Include pagination info** (`totalCount`, `hasMore`, `nextOffset`) so the AI knows
  its position in the result set.
- **Use consistent field names** across tools so the AI can correlate data (e.g.,
  `groupId` and `artifactId` always mean the same thing).

### Pattern 8: The Transport Layer Decision

Choosing the right transport depends on your deployment model:

| Transport | Best For | Trade-offs |
|-----------|----------|------------|
| **stdio** | Local development tools, CLI integrations | Simple. Fast startup. No networking. Requires local installation. |
| **SSE** | Remote servers, shared team infrastructure | Persistent connection. Server push. More complex deployment. |
| **Streamable HTTP** | Cloud-hosted services, load-balanced deployments | Standard HTTP infra. Stateless. Best for horizontal scaling. |

For most domain-specific tools, **stdio is the right starting point**. It is the
simplest to implement, requires no server infrastructure, and works naturally with
AI clients like Claude Code that already spawn subprocesses.

If you need to share the MCP server across a team or run it as a centralized service,
SSE or Streamable HTTP is the way to go. But start with stdio and migrate only when
you have a concrete need.

### Pattern 9: Group Tools by Domain Concept

When you have many tools (the Apicurio MCP server has 25+), grouping them by domain
concept helps the AI navigate the tool surface. The grouping is implicit — expressed
through naming conventions and description language — but it is effective.

The AI builds a mental model of the tool surface from names and descriptions. When
tools are grouped logically, the AI can reason about them as categories: "I need to
work with versions, so I should look at the version tools."

If you have more than 30-40 tools, consider splitting them across multiple MCP servers.
Each server becomes a coherent domain: one for schema management, one for CI/CD, one
for monitoring. The AI client can connect to multiple MCP servers simultaneously.

### Pattern 10: Test with Real AI Interactions

Unit testing MCP tools against mock data catches bugs, but it does not catch
usability issues. The only way to know whether your tool descriptions, parameter
names, and return values work well is to test them with a real AI agent.

Keep a list of representative queries and check that the AI selects the right tools,
constructs correct parameters, and interprets results correctly. Common failure modes:

- **AI picks the wrong tool.** Description is ambiguous or too similar to another tool.
- **AI sends invalid parameters.** Parameter descriptions or types are unclear.
- **AI misinterprets results.** Return value structure is confusing.
- **AI does not know pagination exists.** Total count or next-page info is missing.
- **AI calls tools in wrong order.** Dependencies between tools are not documented.

Fix these by iterating on descriptions and parameter schemas — not by changing the
AI prompt. The tool definitions are your API for the AI, and like any API, they need
user testing.

---

## Building an MCP Server: Implementation Considerations

### Choosing a Language and SDK

MCP server SDKs are available in several languages:

- **TypeScript/JavaScript** — The most mature SDK. First-party support from Anthropic.
  Good choice for teams already using Node.js.
- **Python** — Strong SDK with good async support. Natural choice for ML/data teams.
- **Java/Kotlin** — Growing ecosystem. The Apicurio Registry MCP server is implemented
  in Java, leveraging the existing Apicurio client libraries.
- **Go, Rust, C#** — Community SDKs available with varying maturity levels.

The language choice often follows the API you are wrapping. If your service already has
a Java client library (as Apicurio Registry does), building the MCP server in Java
means you can reuse the client directly rather than reimplementing HTTP calls.

### Server Lifecycle

An stdio-based MCP server has a straightforward lifecycle:

1. **Startup.** The AI client spawns the server process with configuration (e.g.,
   registry URL) passed via environment variables or command-line arguments.
2. **Initialization.** The server sends its capabilities to the client: which tools it
   provides, which resources and prompts it exposes.
3. **Request handling.** The client sends JSON-RPC requests (tool calls); the server
   executes them and returns results.
4. **Shutdown.** The client terminates the server process when the session ends.

The initialization phase is critical. The client reads the tool list during
initialization and uses it for the entire session. If your tools change based on
server state (e.g., permissions), resolve that during initialization.

### Configuration

MCP server configuration in a Claude Code settings file looks like this:

```json
{
  "mcpServers": {
    "apicurio-registry": {
      "command": "java",
      "args": [
        "-jar",
        "/path/to/apicurio-registry-mcp-server.jar"
      ],
      "env": {
        "REGISTRY_URL": "http://localhost:8080/apis/registry/v3",
        "REGISTRY_AUTH_TOKEN": "${REGISTRY_TOKEN}"
      }
    }
  }
}
```

See [`examples/mcp-config.json`](examples/mcp-config.json) for a complete
configuration example with annotations.

Key configuration practices:

- **Use environment variables for secrets.** Never hardcode tokens or passwords in
  the configuration file. Use variable references that resolve from the shell
  environment.
- **Specify the full path to the server binary.** Relative paths are fragile across
  different working directories.
- **Keep the configuration minimal.** The server should have sensible defaults for
  everything except the registry URL and credentials.

### Error Handling Strategy

MCP tools should handle errors gracefully and informatively:

```
1. Network errors → Return a clear message: "Could not connect to registry at
   http://localhost:8080. Is the registry running?"

2. Authentication errors → "Authentication failed. Check that REGISTRY_AUTH_TOKEN
   is set and valid."

3. Not found errors → "Artifact 'user-event' not found in group 'default'.
   Available artifacts: [list if feasible]"

4. Validation errors → Pass through the registry's validation message verbatim.

5. Rate limit errors → "Rate limit exceeded. Try again in {N} seconds."

6. Server errors → "Registry returned an internal error (500). This may be
   transient — try again."
```

The goal is to give the AI enough information to either recover automatically (retry,
try a different approach) or explain the issue to the user clearly.

---

## Real-World Workflow: Schema Lifecycle with AI Assistance

To illustrate how all these pieces come together, here is a complete workflow that
demonstrates the Apicurio Registry MCP server in a realistic development scenario.

### Scenario

A developer is adding a new event type to a Kafka-based streaming pipeline. The event
represents a machine learning prediction result. The developer needs to:

1. Check what schemas already exist in the relevant group
2. Design a new schema that is consistent with existing schemas
3. Validate the schema against registry rules
4. Register the schema
5. Verify the registration

### The Conversation

**Developer:** "I need to add a new prediction-result schema to the reddit-realtime
group. What schemas are already there?"

*AI calls `list_artifacts` with groupId "reddit-realtime" and summarizes the results.*

**Developer:** "Show me the structure of reddit-post-enriched — I want the new schema
to follow the same conventions."

*AI calls `get_version_content` and presents the schema with annotations about
naming conventions, type choices, and documentation patterns.*

**Developer:** "Great. Here is my draft schema: [pastes Avro schema]. Does it follow
the same conventions?"

*AI analyzes the schema against the patterns observed in the existing schemas, without
needing a tool call. Notes that the field names use camelCase while existing schemas
use snake_case.*

**Developer:** "Good catch. Let me fix that. [pastes corrected schema]. Is this
backward compatible?"

*AI calls `create_version` with isDryRun: true to validate. The dry-run succeeds,
confirming the schema is valid and compatible.*

**Developer:** "Register it."

*AI calls `create_artifact` (or `create_version` if the artifact already exists) with
the schema content. Reports success with the artifact ID and version number.*

**Developer:** "Verify it is there and looks right."

*AI calls `get_version_content` and `get_artifact_metadata` to confirm the schema was
registered correctly, comparing the stored content with what was submitted.*

This workflow involves six tool calls across five conversational turns. Without MCP,
each step would require the developer to switch to a registry CLI or UI, construct the
right command, execute it, interpret the output, and relay relevant information back to
the conversation. With MCP, the entire workflow stays in a single context.

---

## The Broader Landscape: MCP Servers for Every Domain

The Apicurio Registry MCP server is one data point in a rapidly expanding landscape.
As of early 2026, MCP servers exist (or are being built) for:

- **Databases** — Query execution, schema introspection, migration management
- **Cloud providers** — AWS, GCP, Azure resource management
- **CI/CD** — GitHub Actions, Jenkins, ArgoCD pipeline management
- **Monitoring** — Prometheus, Grafana, Datadog query and alert management
- **Messaging** — Kafka topic management, consumer group monitoring
- **API gateways** — Route configuration, traffic management
- **Identity** — Keycloak, Auth0 user and policy management
- **Version control** — Git operations, PR management, code search

The pattern is the same in every case: take a domain-specific API, design a thoughtful
tool surface, write rich descriptions, handle errors transparently, and expose it
through MCP.

What varies is the security posture (a database MCP server needs much stricter
controls than a documentation MCP server), the tool granularity (a cloud provider
API has thousands of operations — only a fraction should be exposed), and the
interaction patterns (some tools are stateless queries, others are multi-step
workflows).

### Composability: The Emerging Superpower

The most powerful pattern is not any single MCP server — it is the composition of
multiple MCP servers in a single AI agent session. An AI agent connected to both
the Apicurio Registry MCP server and a Kafka MCP server can:

1. Search the registry for all Avro schemas in a group
2. Check which Kafka topics reference those schemas
3. Identify topics where the producer is using an outdated schema version
4. Suggest a migration plan

No single tool provides this capability. It emerges from the composition of two
domain-specific tool sets, mediated by the AI's ability to reason across domains.

This is the promise of MCP: not a single "AI for everything" tool, but an ecosystem
of focused, domain-specific servers that AI agents can compose into arbitrarily complex
workflows.

---

## Conclusion

Building the Apicurio Registry MCP server taught me that the hardest part of
AI-assisted tooling is not the AI — it is the interface design. Which operations to
expose, how to describe them, what parameters to require, how to structure return
values, where to draw the security boundary. These are traditional API design
decisions, applied to a new consumer: an AI agent that reads your descriptions,
infers intent, and acts autonomously.

The core lessons:

1. **Be selective about your tool surface.** Not every endpoint needs to be a tool.
   Expose operations that benefit from AI reasoning; omit those that could cause
   irreversible damage.

2. **Invest in descriptions.** The AI's ability to use your tools correctly is directly
   proportional to the quality of your descriptions. This is your documentation, your
   API contract, and your user interface — all in one.

3. **Embrace the dry-run pattern.** Validation without mutation is the sweet spot for
   AI-assisted workflows. It gives the AI the power to verify without the power to
   break.

4. **Use prompt templates as living documentation.** Store domain knowledge as prompt
   templates in the system itself. The AI discovers and uses them automatically.

5. **Start with stdio, start with read-only.** Get the basics working in the simplest
   possible configuration. Add write operations and remote transport when you have
   confidence in the security model.

6. **Test with real AI interactions.** Synthetic tests catch bugs; real AI interactions
   catch usability issues. Iterate on the tool surface based on actual usage patterns.

MCP servers for domain-specific tooling are still early. The protocol is young, the
ecosystem is growing, and best practices are being discovered through real-world
experience. But the trajectory is clear: every system with an API will eventually
have an MCP server, and the developers who understand how to build good ones will
shape how AI interacts with the infrastructure that runs our software.

The schema registry was just the beginning.

---

## References and Further Reading

- [Model Context Protocol Specification](https://spec.modelcontextprotocol.io/)
- [Apicurio Registry Documentation](https://www.apicur.io/registry/docs/)
- [MCP TypeScript SDK](https://github.com/modelcontextprotocol/typescript-sdk)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [Claude Code Documentation](https://docs.anthropic.com/en/docs/claude-code)
- [Building MCP Servers (Anthropic Guide)](https://modelcontextprotocol.io/docs/concepts/servers)

---

*This article is part of the [Distributed Deep Dives](https://github.com/carlesarnal/distributed-deep-dives) series exploring distributed systems, event-driven architecture, and AI-assisted infrastructure management.*
