# Apicurio Registry as an Apache Iceberg REST Catalog

**Carles Arnal** -- Principal Software Engineer, IBM | Core contributor, Apicurio Registry

---

## Table of Contents

1. [Introduction](#introduction)
2. [Why an Iceberg Catalog Matters](#why-an-iceberg-catalog-matters)
3. [Architecture: Mapping Iceberg to Registry Concepts](#architecture-mapping-iceberg-to-registry-concepts)
4. [The Implementation](#the-implementation)
5. [CommitTable: The Heart of Iceberg's Concurrency Model](#committable-the-heart-of-icebergs-concurrency-model)
6. [Storage Backend Considerations](#storage-backend-considerations)
7. [Query Engine Integration](#query-engine-integration)
8. [How It Compares](#how-it-compares)
9. [Running It](#running-it)
10. [What I Learned](#what-i-learned)
11. [Conclusion](#conclusion)

---

## Introduction

Apache Iceberg has become the table format for data lakehouses. It gives you ACID transactions,
schema evolution, time travel, and partition evolution on top of object storage -- things that
Hive tables never reliably delivered. But Iceberg tables don't exist in a vacuum. Every query
engine that touches an Iceberg table needs to discover it somewhere: a catalog.

The Iceberg community standardized this discovery through the
[REST Catalog specification](https://github.com/apache/iceberg/blob/main/open-api/rest-catalog-open-api.yaml),
an OpenAPI-defined contract that any catalog implementation can adopt. This means Spark, Trino,
DuckDB, ClickHouse, and Flink can all talk to any REST-compliant catalog through the same API.

Apicurio Registry already manages versioned artifacts -- Avro schemas, Protobuf definitions,
OpenAPI specifications, and since recently, prompt templates. Each artifact gets versioning,
compatibility rules, access control, and audit trails. The question we asked ourselves was:
can this same infrastructure manage Iceberg table metadata?

The answer is yes. Starting with version 3.2.0, Apicurio Registry implements the Iceberg REST
Catalog API. Groups become namespaces, artifacts become tables, versions become commits. The
mapping is natural enough that the implementation required no changes to the core storage layer
-- only a new REST endpoint and a thin translation layer.

This article is a deep dive into how the implementation works, the engineering decisions behind
it, the concurrency model that makes CommitTable reliable, and what this means for teams
that already use Apicurio Registry for schema governance.

---

## Why an Iceberg Catalog Matters

### The Catalog Problem

An Iceberg table is, at its core, a pointer. The table's current state is defined by a
`metadata.json` file that references schemas, partition specs, sort orders, and a tree of
snapshots. Each snapshot references manifest lists, which reference manifest files, which
reference the actual data files (Parquet, ORC, Avro).

The catalog's job is to answer one question: given a table name, where is the current
`metadata.json`? And when a writer commits new data, the catalog must atomically swap the
metadata pointer to the new version -- ensuring that concurrent writers don't silently
overwrite each other.

This sounds simple, but getting atomic pointer swaps right across distributed systems is
the hard part. Hive Metastore uses database locks. AWS Glue uses DynamoDB conditional writes.
Project Nessie uses a git-like commit model. Each approach has tradeoffs in latency,
scalability, and operational complexity.

### The Existing Catalog Landscape

The Iceberg catalog space is crowded:

- **Hive Metastore** -- The legacy choice. Thrift-based, heavy to operate, requires a
  dedicated RDBMS. Works everywhere but scales poorly.
- **AWS Glue** -- Zero-ops on AWS, proprietary API. Locks you into the AWS ecosystem.
- **Project Nessie** -- Git-like branching and tagging for table metadata. Powerful but adds
  conceptual complexity. Recently merged with Apache Polaris.
- **Apache Polaris (incubating)** -- Snowflake-donated REST catalog. Iceberg-only, designed
  for multi-tenant SaaS deployments.
- **Gravitino** -- Multi-asset metastore from Datastrato. Manages tables, AI models, and
  messaging topics.
- **Lakekeeper** -- Lean Rust implementation focused on performance.

### Where Apicurio Registry Fits

Apicurio Registry is none of these things. It's a schema and API registry that organizations
already deploy to govern their Kafka schemas, OpenAPI specs, and other contracts. Adding
Iceberg catalog capabilities means these organizations don't need a second service to manage
table metadata.

The value proposition is consolidation, not competition. If you're already running Apicurio
Registry for schema governance, you can now point Spark and Trino at the same registry
instance for Iceberg table discovery. One fewer service to deploy, monitor, and secure.

This also creates a unique integration point: the same registry that validates your Avro
schemas for Kafka topics can now manage the Iceberg tables that your Kafka consumers write
to. Schema consistency across the streaming and lakehouse layers, managed in one place.

---

## Architecture: Mapping Iceberg to Registry Concepts

The implementation maps Iceberg concepts directly to existing Registry primitives. No new
storage abstractions were needed.

| Iceberg Concept | Registry Concept | Notes |
|-----------------|------------------|-------|
| Namespace | Group | Dot-separated hierarchy: `my_db.my_schema` becomes group ID `my_db.my_schema` |
| Table | Artifact (type: `ICEBERG_TABLE`) | The artifact ID is the table name |
| TableMetadata JSON | Artifact content | Complete metadata stored as JSON content |
| Table commit | Artifact version | Each commit creates an immutable new version |
| Namespace properties | Group labels | Key-value pairs stored as group-level labels |
| Table properties | Artifact labels | Key-value pairs stored as artifact-level labels |
| Catalog prefix | Path parameter | Configurable identifier, defaults to `default` |

### Why This Mapping Works

This isn't a forced mapping. Iceberg namespaces are hierarchical containers for tables --
that's exactly what Registry groups are for artifacts. Iceberg tables have metadata that
evolves through commits -- that's exactly what Registry artifact versions model. Iceberg
properties are key-value metadata -- that's exactly what Registry labels store.

The critical insight is that Registry's versioning model is a natural fit for Iceberg's
snapshot isolation. Each Iceberg commit produces a new, immutable metadata state. In the
Registry, each commit produces a new, immutable artifact version. The Registry already
guarantees that versions are ordered and immutable -- properties that Iceberg requires
from its catalog.

### The REST API Surface

The Iceberg REST Catalog API is exposed at `/apis/iceberg/v1`. All endpoints are behind
a feature flag:

```properties
apicurio.features.experimental.enabled=true
apicurio.iceberg.enabled=true
apicurio.iceberg.warehouse=s3://my-bucket/warehouse
apicurio.iceberg.default-prefix=default
```

The API implements 14 endpoints across three resource types:

**Configuration:**
- `GET /config` -- Returns catalog defaults and overrides (warehouse location, prefix)

**Namespace operations:**
- `GET /{prefix}/namespaces` -- List namespaces with pagination
- `POST /{prefix}/namespaces` -- Create a namespace
- `GET /{prefix}/namespaces/{namespace}` -- Load namespace metadata
- `HEAD /{prefix}/namespaces/{namespace}` -- Check existence
- `DELETE /{prefix}/namespaces/{namespace}` -- Drop (must be empty)
- `POST /{prefix}/namespaces/{namespace}/properties` -- Update properties

**Table operations:**
- `GET /{prefix}/namespaces/{namespace}/tables` -- List tables
- `POST /{prefix}/namespaces/{namespace}/tables` -- Create a table
- `GET /{prefix}/namespaces/{namespace}/tables/{table}` -- Load a table
- `HEAD /{prefix}/namespaces/{namespace}/tables/{table}` -- Check existence
- `POST /{prefix}/namespaces/{namespace}/tables/{table}` -- CommitTable (atomic update)
- `DELETE /{prefix}/namespaces/{namespace}/tables/{table}` -- Drop a table
- `POST /{prefix}/tables/rename` -- Rename a table (cross-namespace)

---

## The Implementation

### Core Resource: `IcebergApiResourceImpl`

The entire Iceberg API is implemented in a single JAX-RS resource class. It's a translation
layer: every Iceberg operation maps to one or more calls to the existing `RegistryStorage`
interface.

```java
@Interceptors({ResponseErrorLivenessCheck.class, ResponseTimeoutReadinessCheck.class})
@Logged
public class IcebergApiResourceImpl implements ApisResource {

    @Inject @Current
    RegistryStorage storage;

    @Inject
    IcebergConfig icebergConfig;

    @Inject
    ObjectMapper objectMapper;

    private void requireIcebergEnabled() {
        if (!icebergConfig.isEnabled()) {
            throw new NotFoundException("Iceberg REST Catalog API is disabled");
        }
    }
    // ...
}
```

Every method starts with `requireIcebergEnabled()`, a guard that returns 404 when the feature
flag is off. This means the Iceberg endpoints don't even appear to exist when disabled --
there's no "this feature is disabled" error message that leaks information about capabilities.

### Namespace Translation

Iceberg namespaces are multi-level arrays: `["my_database", "my_schema"]`. Registry groups
are flat strings. The translation uses dot-separated concatenation:

```java
private String namespaceToGroupId(List<String> namespace) {
    return String.join(".", namespace);
}

private List<String> groupIdToNamespace(String groupId) {
    return Arrays.asList(groupId.split("\\."));
}
```

When a namespace is encoded in a URL path parameter, it uses a null character separator
(`\u0000`) which gets URL-encoded. The implementation decodes this before converting to a
group ID.

### Creating Tables

Table creation constructs a complete Iceberg v2 `TableMetadata` JSON document and stores
it as an artifact:

```java
public LoadTableResponse createTable(String prefix, String namespace,
        String xIcebergAccessDelegation, CreateTableRequest data) {
    requireIcebergEnabled();

    String groupId = namespaceToGroupId(namespace);
    String tableName = data.getName();
    String tableUuid = UUID.randomUUID().toString();

    // Auto-generate location if not specified
    String location = data.getLocation();
    if (location == null || location.isEmpty()) {
        String warehouse = icebergConfig.getDefaultWarehouse();
        location = warehouse + "/" + groupId.replace(".", "/") + "/" + tableName;
    }

    Map<String, Object> metadata = new HashMap<>();
    metadata.put("format-version", 2);
    metadata.put("table-uuid", tableUuid);
    metadata.put("location", location);
    metadata.put("schemas", List.of(data.getSchema()));
    metadata.put("current-schema-id", 0);
    metadata.put("partition-specs", data.getPartitionSpec() != null
            ? List.of(data.getPartitionSpec())
            : List.of(Map.of("spec-id", 0, "fields", List.of())));
    metadata.put("current-snapshot-id", -1);
    metadata.put("snapshots", List.of());
    // ...

    String metadataJson = objectMapper.writeValueAsString(metadata);

    storage.createArtifact(groupId, tableName, ArtifactType.ICEBERG_TABLE,
            artifactMetaData, null, content, versionMetaData, null, false, false,
            getCurrentUser());
    // ...
}
```

Key points:

- **Format version 2** is used by default, which supports row-level deletes, hidden
  partitioning, and partition evolution.
- **Table UUID** is generated server-side. This is the canonical identity of the table,
  used by clients to detect if a table was dropped and recreated with the same name.
- **Location auto-generation** follows the convention `{warehouse}/{namespace}/{table}`,
  converting dots in namespace to path separators.
- **Table properties** are stored both in the metadata JSON (for Iceberg clients) and as
  artifact labels (for Registry search and filtering).

### Loading Tables

Loading a table is straightforward: fetch the latest version of the artifact, parse the
JSON content, and return it:

```java
public LoadTableResponse loadTable(String prefix, String namespace, String table, ...) {
    requireIcebergEnabled();
    String groupId = namespaceToGroupId(namespace);

    StoredArtifactVersionDto artifact = storage.getArtifactVersionContent(
            groupId, table,
            storage.getBranchTip(new GA(groupId, table), BranchId.LATEST,
                    RetrievalBehavior.SKIP_DISABLED_LATEST).getRawVersionId());

    TableMetadata metadata = objectMapper.readValue(
            artifact.getContent().content(), TableMetadata.class);

    LoadTableResponse response = new LoadTableResponse();
    response.setMetadata(metadata);
    response.setMetadataLocation(metadata.getLocation() + "/metadata/v1.metadata.json");
    return response;
}
```

The `BranchId.LATEST` with `SKIP_DISABLED_LATEST` retrieval behavior ensures that if a
version was disabled (a Registry concept for soft-deletion), the previous active version
is returned instead.

---

## CommitTable: The Heart of Iceberg's Concurrency Model

The `CommitTable` endpoint is where the real engineering complexity lives. This is the
operation that makes Iceberg's snapshot isolation work: multiple writers can operate
concurrently, and the catalog ensures that only one writer's commit succeeds if there's
a conflict.

### The Commit Flow

```
1. Load current metadata + record base version order
2. Validate all requirements against current metadata
3. Deep-copy metadata and apply all updates
4. Serialize new metadata
5. Store as new version with atomic version-order check
6. Return updated metadata
```

```java
public LoadTableResponse commitTable(String prefix, String namespace, String table,
        CommitTableRequest data) {
    requireIcebergEnabled();

    String groupId = namespaceToGroupId(namespace);

    // Step 1: Load current state and record the version order
    GAV branchTip = storage.getBranchTip(
            new GA(groupId, table), BranchId.LATEST, ...);
    int baseVersionOrder = storage.getArtifactVersionMetaData(
            groupId, table, branchTip.getRawVersionId()).getVersionOrder();

    // Step 2: Validate requirements
    TableRequirementValidator.validate(requirements, currentMetadata, groupId, table);

    // Step 3: Apply updates to a deep copy
    Map<String, Object> newMetadata = deepCopy(currentMetadata);
    TableUpdateApplicator.apply(updates, newMetadata);

    // Step 5: Atomic write with version-order check
    storage.createArtifactVersionIfLatest(groupId, table,
            null, ArtifactType.ICEBERG_TABLE, content,
            EditableVersionMetaDataDto.builder().build(),
            null, false, getCurrentUser(),
            baseVersionOrder,    // ← the concurrency guard
            artifactMetaData);   // ← atomic label update
}
```

The `baseVersionOrder` parameter is the concurrency guard. When the storage layer attempts
to create the new version, it checks whether the current latest version still has the same
order number as when we read it. If another commit was interleaved, the version order will
have changed, and the storage layer throws a `CommitFailedException` (HTTP 409).

### Requirements: Pre-Commit Assertions

The `TableRequirementValidator` validates preconditions before applying any updates.
This is Iceberg's optimistic locking protocol -- clients assert what they believe the
current state to be, and the catalog verifies those assertions.

```java
public static void validate(List<Map<String, Object>> requirements,
        Map<String, Object> currentMetadata, String groupId, String artifactId) {
    for (Map<String, Object> req : requirements) {
        switch ((String) req.get("type")) {
            case "assert-create":
                // Table must NOT exist
                if (currentMetadata != null) throw new CommitFailedException(...);
                break;
            case "assert-table-uuid":
                // Table UUID must match expected value
                if (!expectedUuid.equals(actualUuid))
                    throw new CommitFailedException(...);
                break;
            case "assert-ref-snapshot-id":
                // Named ref must point to expected snapshot
                // Special handling for "main" ref → checks current-snapshot-id
                break;
            case "assert-current-schema-id":
            case "assert-last-assigned-field-id":
            case "assert-default-spec-id":
            case "assert-default-sort-order-id":
            case "assert-last-assigned-partition-id":
                // Numeric field assertions
                break;
        }
    }
}
```

The `assert-ref-snapshot-id` requirement deserves special attention. When the ref is `main`,
it checks the top-level `current-snapshot-id` field. For any other ref (branches, tags), it
looks into the `refs` map. This distinction matters because Spark and other engines use the
`main` ref to track the current state of the table.

### Updates: Atomic Metadata Mutations

The `TableUpdateApplicator` applies mutations to a mutable metadata map. All updates in a
single commit are applied sequentially to produce one new metadata state:

```java
public static void apply(List<Map<String, Object>> updates,
        Map<String, Object> metadata) {
    for (Map<String, Object> update : updates) {
        switch ((String) update.get("action")) {
            case "add-schema":        // Add new schema, update last-column-id
            case "set-current-schema": // Set active schema
            case "add-snapshot":      // Add snapshot, update snapshot-log
            case "set-snapshot-ref":  // Set ref, update current-snapshot-id for "main"
            case "add-spec":          // Add partition spec
            case "set-default-spec":  // Set default partition spec
            case "add-sort-order":    // Add sort order
            case "set-default-sort-order": // Set default sort order
            case "set-location":      // Change table location
            case "set-properties":    // Merge table properties
            case "remove-properties": // Remove table properties
            case "remove-snapshots":  // Remove snapshots by ID
            case "remove-snapshot-ref": // Remove a named ref
            case "assign-uuid":       // Set table UUID
            case "upgrade-format-version": // Upgrade format (cannot downgrade)
        }
    }
    metadata.put("last-updated-ms", System.currentTimeMillis());
}
```

Some updates have side effects:

- `add-schema` also updates `last-column-id` based on the highest field ID in the schema.
- `add-snapshot` also appends to the snapshot log and increments `last-sequence-number`.
- `set-snapshot-ref` for `main` also updates `current-snapshot-id`.
- `add-spec` also updates `last-partition-id`.

These side effects are part of the Iceberg specification -- the catalog must maintain
consistency between related metadata fields within a single commit.

### Atomic Label Updates

A subtle but important detail: when a commit changes the table UUID or location, the
corresponding artifact labels must also be updated. If this happened as a separate
operation after the version creation, there would be a window where the labels are
inconsistent with the content.

The implementation avoids this by computing the label delta before the write and passing
it to `createArtifactVersionIfLatest`, which applies both the version creation and the
label update in a single storage transaction:

```java
EditableArtifactMetaDataDto artifactMetaData =
        buildArtifactMetaDataIfNeeded(currentMetadata, newMetadata);

storage.createArtifactVersionIfLatest(groupId, table,
        null, ArtifactType.ICEBERG_TABLE, content,
        versionMetaData, null, false, getCurrentUser(),
        baseVersionOrder, artifactMetaData);
```

---

## Storage Backend Considerations

Apicurio Registry supports multiple storage backends, and the Iceberg catalog works with
all of them. But the concurrency guarantees differ.

### SQL Storage (PostgreSQL)

The SQL backend uses `SELECT ... FOR UPDATE` row-level locking. When
`createArtifactVersionIfLatest` is called, it:

1. Acquires a row lock on the artifact
2. Checks that the current version order matches `baseVersionOrder`
3. Creates the new version
4. Updates artifact labels if provided
5. Commits the SQL transaction

This is a classic optimistic concurrency pattern with pessimistic locking at the storage
level. The row lock is held only for the duration of the SQL transaction -- typically
microseconds.

### KafkaSQL Storage

The KafkaSQL backend serializes all writes through a Kafka topic. Commits to the same
table are totally ordered through Kafka partitioning. This means:

- The version-order check is always evaluated against the true latest state.
- Failed commits produce Kafka messages that remain in the journal. During replay (e.g.,
  when a new replica starts), the same version-order check fails again, and the message
  is silently discarded.
- The version creation and label updates happen in a single SQL transaction within the
  consumer, maintaining atomicity.

The tradeoff is latency: writes go through Kafka, which adds a round-trip compared to
direct SQL access. For most Iceberg workloads -- where commits happen at most every few
seconds per table -- this latency is negligible.

---

## Query Engine Integration

Because Apicurio implements the standard Iceberg REST Catalog API, any query engine
that supports the REST catalog specification works out of the box.

### Apache Spark

```scala
spark.conf.set("spark.sql.catalog.apicurio", "org.apache.iceberg.spark.SparkCatalog")
spark.conf.set("spark.sql.catalog.apicurio.type", "rest")
spark.conf.set("spark.sql.catalog.apicurio.uri", "http://localhost:8080/apis/iceberg/v1")
spark.conf.set("spark.sql.catalog.apicurio.prefix", "default")
```

```sql
USE apicurio;
CREATE NAMESPACE my_database;

CREATE TABLE apicurio.my_database.events (
  id BIGINT,
  event_type STRING,
  event_time TIMESTAMP
) USING iceberg;

SELECT * FROM apicurio.my_database.events;
```

### Trino

```properties
# etc/catalog/apicurio.properties
connector.name=iceberg
iceberg.catalog.type=rest
iceberg.rest-catalog.uri=http://localhost:8080/apis/iceberg/v1
iceberg.rest-catalog.prefix=default
```

```sql
CREATE SCHEMA apicurio.my_database;

CREATE TABLE apicurio.my_database.products (
  id BIGINT,
  name VARCHAR,
  price DECIMAL(10, 2)
) WITH (format = 'PARQUET');
```

### DuckDB

```sql
INSTALL iceberg;
LOAD iceberg;

ATTACH 'http://localhost:8080/apis/iceberg/v1' AS apicurio (TYPE ICEBERG);

SELECT * FROM apicurio.my_database.users;
```

### ClickHouse

```sql
CREATE DATABASE apicurio_db ENGINE = Iceberg(
  'http://localhost:8080/apis/iceberg/v1',
  'default',
  'my_database'
);

SELECT * FROM apicurio_db.users;
```

---

## How It Compares

| Feature | Apicurio Registry | Apache Polaris | Project Nessie | AWS Glue | Hive Metastore |
|---------|-------------------|---------------|----------------|----------|----------------|
| REST Catalog API | Yes | Yes | Yes | No (proprietary) | No (Thrift) |
| Primary purpose | Schema + API registry | Iceberg-only catalog | Git-like versioned catalog | AWS-managed catalog | Legacy SQL catalog |
| Multi-format schemas | Yes (Avro, Protobuf, JSON Schema, OpenAPI, etc.) | No | No | No | Yes (Hive schemas) |
| Git-like branching | No | No | Yes | No | No |
| Views support | Planned | Yes | Yes | No | Yes |
| Credentials vending | Planned | Yes | Limited | Built-in (IAM) | No |
| RBAC | Via Registry auth | Yes (granular) | Limited | IAM-based | Ranger/Sentry |
| Runtime | Java (Quarkus) | Java | Java | Managed service | Java |
| **Key advantage** | **Unified schema registry + Iceberg catalog** | Iceberg-focused, multi-tenant | Multi-table transactions, branching | Zero-ops on AWS | Broad ecosystem support |

The differentiator is clear: Apicurio is the only solution that combines a full-featured
schema and API registry with an Iceberg catalog. If you need a dedicated, high-performance
Iceberg catalog with credential vending and multi-table transactions, Polaris or Nessie
are better fits. If you're already running Apicurio and want to add Iceberg support
without deploying another service, the calculus changes.

---

## Running It

### Docker Compose

See [`examples/docker-compose.yaml`](examples/docker-compose.yaml) for a complete setup.

```bash
docker compose up -d
```

This starts Apicurio Registry with Iceberg support enabled and a MinIO instance for
S3-compatible object storage.

### Verify the catalog is responding

```bash
curl http://localhost:8080/apis/iceberg/v1/config
```

### Create a namespace and table

```bash
# Create a namespace
curl -X POST http://localhost:8080/apis/iceberg/v1/default/namespaces \
  -H "Content-Type: application/json" \
  -d '{
    "namespace": ["analytics"],
    "properties": {"owner": "data-team"}
  }'

# Create a table
curl -X POST http://localhost:8080/apis/iceberg/v1/default/namespaces/analytics/tables \
  -H "Content-Type: application/json" \
  -d '{
    "name": "page_views",
    "schema": {
      "type": "struct",
      "schema-id": 0,
      "fields": [
        {"id": 1, "name": "user_id", "required": true, "type": "long"},
        {"id": 2, "name": "page_url", "required": true, "type": "string"},
        {"id": 3, "name": "view_time", "required": true, "type": "timestamp"},
        {"id": 4, "name": "duration_ms", "required": false, "type": "long"}
      ]
    },
    "properties": {"write.format.default": "parquet"}
  }'
```

### Evolve the schema

```bash
curl -X POST http://localhost:8080/apis/iceberg/v1/default/namespaces/analytics/tables/page_views \
  -H "Content-Type: application/json" \
  -d '{
    "requirements": [
      {"type": "assert-current-schema-id", "current-schema-id": 0}
    ],
    "updates": [
      {
        "action": "add-schema",
        "schema": {
          "type": "struct",
          "schema-id": 1,
          "fields": [
            {"id": 1, "name": "user_id", "required": true, "type": "long"},
            {"id": 2, "name": "page_url", "required": true, "type": "string"},
            {"id": 3, "name": "view_time", "required": true, "type": "timestamp"},
            {"id": 4, "name": "duration_ms", "required": false, "type": "long"},
            {"id": 5, "name": "referrer_url", "required": false, "type": "string"}
          ]
        }
      },
      {"action": "set-current-schema", "schema-id": 1}
    ]
  }'
```

---

## What I Learned

**The Iceberg REST Catalog spec is well-designed but underspecified in edge cases.** The
OpenAPI spec defines the request/response shapes clearly, but behaviors like how to handle
`null` vs. `-1` for snapshot IDs, or what happens when you set a ref to a snapshot that
doesn't exist in the snapshots list, are left to implementations. We had to make decisions
that we think match what Spark and Trino expect, but there's no conformance test suite.

**Optimistic concurrency is the right model for Iceberg commits.** Iceberg tables typically
have low write contention -- commits happen when data files are written, which is measured
in seconds or minutes, not milliseconds. Optimistic concurrency with version-order checking
gives you conflict detection without lock contention. The retry loop (reload, revalidate,
recommit) is handled by the Iceberg SDK, so clients don't even see it.

**Reusing existing storage abstractions saved months of work.** The entire Iceberg
implementation is ~710 lines in the resource class plus ~540 lines in the commit handling
classes. That's it. Everything else -- versioning, access control, search, audit logging,
multi-tenancy -- comes from the existing Registry infrastructure. If we had built a
standalone Iceberg catalog from scratch, we'd still be writing the storage layer.

**The concept mapping between Registry and Iceberg is not forced.** Groups-to-namespaces and
artifacts-to-tables felt natural from day one. The only awkward spot is namespace hierarchy:
Iceberg namespaces are truly hierarchical (`["db", "schema"]`), while Registry groups are
flat strings. Dot-separated concatenation works but loses the ability to have dots in
individual namespace levels. In practice, nobody uses dots in Iceberg namespace names, so
this hasn't been an issue.

**Schema convergence across streaming and lakehouse is the real win.** The most interesting
use case isn't just "another Iceberg catalog." It's managing the Avro schema that defines
your Kafka topic and the Iceberg table schema that defines your lakehouse table in the same
registry, with the same governance rules. A future schema conversion utility (already in
progress) will bridge these formats automatically.

---

## Conclusion

Apicurio Registry's Iceberg REST Catalog implementation is a bet on consolidation. Instead
of running separate services for schema governance and table catalog management, you get
both in one Quarkus application.

The implementation is deliberately minimal. It does what Iceberg clients need -- namespace
management, table CRUD, atomic commits with optimistic concurrency -- and delegates
everything else (storage, auth, versioning, search) to the existing Registry infrastructure.
Views support and advanced features like credential vending are planned for future releases.

For teams already invested in Apicurio Registry for schema governance, this is a low-friction
way to add Iceberg catalog capabilities. For teams evaluating Iceberg catalogs from scratch,
the value depends on whether you also need schema governance -- if you do, Apicurio gives
you both; if you don't, a purpose-built catalog like Polaris might be a simpler choice.

The feature is experimental in 3.2.0. We're looking for feedback from real deployments to
inform the roadmap for views support, credential vending, and query engine compatibility
testing. If you try it, open an issue -- the implementation is still young enough that
your feedback will shape it.

---

*Docker Compose setup and curl examples: [examples/](examples/)*

*Source code: [Apicurio Registry on GitHub](https://github.com/Apicurio/apicurio-registry)*
