# Why Schema References Break in Avro (And How to Design Around It)

**Carles Arnal** | Principal Software Engineer, IBM
*Contributor to [Apicurio Registry](https://github.com/Apicurio/apicurio-registry)*

---

## Introduction

If you have spent any meaningful time building event-driven systems with Apache Kafka and
Avro, you have probably arrived at the same crossroads I did: your schemas start small
and self-contained, and then one day someone on the team says, "We should share the
`Address` type across all our schemas." That is when things get interesting.

Schema references in Avro look deceptively simple on paper. You define a named type in
one schema, reference it from another, and the registry resolves it all at
serialization time. In practice, this mechanism is one of the most reliable sources of
subtle, hard-to-debug production failures I have encountered in distributed systems
work.

This article is not a tutorial on Avro references. It is a field report. Over the past
few years, I have contributed to the Apicurio Registry project --- specifically around
content canonicalization, reference resolution, and compatibility checking --- and I
built a real-time Reddit classification pipeline that processes posts through Kafka,
Spark, and a Quarkus-based ML inference layer. That pipeline taught me more about schema
governance pain points than any documentation ever could.

What follows is a deep dive into why Avro schema references break, how different schema
registries attempt to solve the problem, and what I actually recommend doing in
production.

---

## 1. The Problem: Named Types and the Illusion of Modularity

### How Avro handles named types

Avro has three categories of "named types": **records**, **enums**, and **fixed**. Each
named type lives in a namespace and is identified by its fully qualified name
(`namespace.name`). When the Avro parser encounters a string where it expects a type, it
looks up that string in a symbol table of previously parsed named types.

Here is a simple example:

```json
{
  "type": "record",
  "name": "Order",
  "namespace": "com.example.commerce",
  "fields": [
    { "name": "orderId", "type": "string" },
    { "name": "amount", "type": "double" },
    {
      "name": "status",
      "type": {
        "type": "enum",
        "name": "OrderStatus",
        "namespace": "com.example.commerce",
        "symbols": ["PENDING", "CONFIRMED", "SHIPPED", "DELIVERED", "CANCELLED"]
      }
    }
  ]
}
```

This is self-contained. The `OrderStatus` enum is defined inline within the `Order`
record. The parser sees the enum definition before it needs to resolve it. Life is good.

Now consider what happens when you want to share `OrderStatus` across multiple schemas.
The natural instinct is to define it once and reference it by name:

```json
{
  "type": "record",
  "name": "Order",
  "namespace": "com.example.commerce",
  "fields": [
    { "name": "orderId", "type": "string" },
    { "name": "amount", "type": "double" },
    { "name": "status", "type": "com.example.commerce.OrderStatus" }
  ]
}
```

This schema, parsed in isolation, will fail. The Avro parser has no `OrderStatus` in its
symbol table. It has never seen the definition. The string
`"com.example.commerce.OrderStatus"` is just a string, and Avro has no built-in
mechanism to fetch the definition from somewhere else.

This is the fundamental tension: **Avro's type system supports references by name, but
Avro's schema parser does not support references across files.**

### What "references" actually mean

Unlike JSON Schema, which has a well-defined `$ref` keyword with URI-based resolution,
Avro references are implicit. You write a fully qualified name as a type, and the parser
expects that name to already exist in its symbol table. There is no `$ref`, no
`$import`, no explicit link.

This means that for cross-schema references to work, something external to Avro must:

1. Know which schemas depend on which other schemas
2. Fetch the dependent schemas
3. Parse them in the correct order so the symbol table is populated before the
   referencing schema is parsed
4. Handle the case where the referenced schema evolves independently

That "something external" is your schema registry. And this is where registries diverge
in fundamental ways.

### The seductive promise

The pitch for schema references is compelling:

- **DRY principle**: Define `Address`, `Money`, `Timestamp` once, use everywhere
- **Consistent evolution**: Update the shared type in one place, all consumers get it
- **Smaller payloads**: Reference by name instead of embedding full definitions

The reality is more nuanced. References introduce coupling between schemas, create
ordering dependencies during registration, and make compatibility checking significantly
more complex. Every benefit above has a corresponding cost that only shows up at scale or
during incident response at 2 AM.

---

## 2. How Schema Registries Resolve References

### Confluent Schema Registry

Confluent's approach is explicit. When you register a schema that references another
schema, you provide a `references` array alongside the schema content:

```bash
# First, register the shared enum
curl -X POST http://localhost:8081/subjects/OrderStatus/versions \
  -H "Content-Type: application/vnd.schemaregistry.v1+json" \
  -d '{
    "schemaType": "AVRO",
    "schema": "{\"type\":\"enum\",\"name\":\"OrderStatus\",\"namespace\":\"com.example.commerce\",\"symbols\":[\"PENDING\",\"CONFIRMED\",\"SHIPPED\",\"DELIVERED\",\"CANCELLED\"]}"
  }'

# Then register the Order schema with an explicit reference
curl -X POST http://localhost:8081/subjects/Order/versions \
  -H "Content-Type: application/vnd.schemaregistry.v1+json" \
  -d '{
    "schemaType": "AVRO",
    "schema": "{\"type\":\"record\",\"name\":\"Order\",\"namespace\":\"com.example.commerce\",\"fields\":[{\"name\":\"orderId\",\"type\":\"string\"},{\"name\":\"amount\",\"type\":\"double\"},{\"name\":\"status\",\"type\":\"com.example.commerce.OrderStatus\"}]}",
    "references": [
      {
        "name": "com.example.commerce.OrderStatus",
        "subject": "OrderStatus",
        "version": 1
      }
    ]
  }'
```

Key characteristics of Confluent's approach:

- **Subject-based resolution**: References point to subjects (which map to Kafka topics
  or explicit names) and specific versions.
- **Global namespace**: All subjects live in a flat namespace (or optionally under a
  context prefix). There is no hierarchy.
- **Explicit version pinning**: Each reference specifies a version number. This is both
  the strength and the weakness of the approach.
- **Transitive resolution**: If schema A references B, and B references C, the registry
  resolves the full chain. But you must declare the full chain at registration time.

### Apicurio Registry

Apicurio Registry takes a different approach, reflecting its heritage as a general-purpose
artifact registry (not just schemas). I have spent a fair amount of time in this
codebase, so I will go into more detail here.

In Apicurio, schemas are stored as **artifacts** within **groups**. References between
artifacts use a combination of `groupId`, `artifactId`, and `version`:

```bash
# Create a group for shared types
curl -X POST http://localhost:8080/apis/registry/v3/groups \
  -H "Content-Type: application/json" \
  -d '{"groupId": "com.example.shared"}'

# Register the shared enum in the shared group
curl -X POST "http://localhost:8080/apis/registry/v3/groups/com.example.shared/artifacts" \
  -H "Content-Type: application/json" \
  -d '{
    "artifactId": "OrderStatus",
    "artifactType": "AVRO",
    "firstVersion": {
      "version": "1.0.0",
      "content": {
        "content": "{\"type\":\"enum\",\"name\":\"OrderStatus\",\"namespace\":\"com.example.commerce\",\"symbols\":[\"PENDING\",\"CONFIRMED\",\"SHIPPED\",\"DELIVERED\",\"CANCELLED\"]}",
        "contentType": "application/json"
      }
    }
  }'

# Register the Order schema with artifact references
curl -X POST "http://localhost:8080/apis/registry/v3/groups/com.example.commerce/artifacts" \
  -H "Content-Type: application/json" \
  -d '{
    "artifactId": "Order",
    "artifactType": "AVRO",
    "firstVersion": {
      "version": "1.0.0",
      "content": {
        "content": "{\"type\":\"record\",\"name\":\"Order\",\"namespace\":\"com.example.commerce\",\"fields\":[{\"name\":\"orderId\",\"type\":\"string\"},{\"name\":\"amount\",\"type\":\"double\"},{\"name\":\"status\",\"type\":\"com.example.commerce.OrderStatus\"}]}",
        "contentType": "application/json",
        "references": [
          {
            "name": "com.example.commerce.OrderStatus",
            "groupId": "com.example.shared",
            "artifactId": "OrderStatus",
            "version": "1.0.0"
          }
        ]
      }
    }
  }'
```

Key characteristics of Apicurio's approach:

- **Group-based organization**: Artifacts live in groups, providing a natural namespace
  hierarchy. This is powerful for multi-team organizations, but it also means references
  must know about the group structure.
- **Artifact references**: References link to specific `groupId/artifactId/version`
  coordinates.
- **Multi-format support**: Apicurio handles Avro, JSON Schema, Protobuf, OpenAPI,
  AsyncAPI, and more. The reference resolution mechanism is format-aware.
- **Semantic versioning**: Apicurio supports semver-style version strings, not just
  integer version numbers. This gives you more expressiveness but also more rope.

### The critical difference: transitive resolution and version pinning

Both registries resolve transitive references, but the mechanics differ in ways that
matter during schema evolution.

**Confluent** resolves references at registration time. When you register schema A with
a reference to B version 2, the registry fetches B version 2, resolves any references B
has, and validates the complete schema. If B version 3 is registered later, schema A
still points to B version 2. This is safe but rigid.

**Apicurio** also resolves at registration time, but the group-based structure means
you can have the same `artifactId` in different groups with different evolution paths.
This is a feature that enables multi-team workflows, but it also means you need to be
very precise about your `groupId` in references. A typo in the group ID does not fail
loudly --- it fails when the serializer tries to resolve the reference at runtime.

Here is a table that summarizes the comparison:

| Aspect | Confluent SR | Apicurio Registry |
|---|---|---|
| Reference target | Subject + version (integer) | GroupId + ArtifactId + version (string) |
| Namespace model | Flat (global subjects) | Hierarchical (groups) |
| Cross-namespace refs | Via subject naming conventions | Explicit groupId in reference |
| Version format | Auto-incrementing integer | Semver or custom strings |
| Transitive resolution | Yes, at registration time | Yes, at registration time |
| Compatibility scope | Per-subject | Per-artifact within group |
| Format awareness | Schema type flag | Full artifact type system |

### What neither registry solves well

Neither registry fully solves the fundamental problem: **the author of a schema does not
control the evolution of the schemas it references.** This is a governance problem
dressed up as a technical one, and no amount of API design will fix it without
organizational discipline.

---

## 3. The Edge Cases That Break

This section covers the specific failure modes I have seen in production or encountered
while working on Apicurio Registry's internals. Each one looked innocent in development
and caused real pain in production.

### 3.1 Circular references between schemas

Consider a domain model where an `Employee` has a `Department`, and a `Department` has a
manager who is an `Employee`:

```json
// employee.avsc
{
  "type": "record",
  "name": "Employee",
  "namespace": "com.example.hr",
  "fields": [
    { "name": "employeeId", "type": "string" },
    { "name": "name", "type": "string" },
    { "name": "department", "type": "com.example.hr.Department" }
  ]
}
```

```json
// department.avsc
{
  "type": "record",
  "name": "Department",
  "namespace": "com.example.hr",
  "fields": [
    { "name": "departmentId", "type": "string" },
    { "name": "name", "type": "string" },
    { "name": "manager", "type": "com.example.hr.Employee" }
  ]
}
```

This is a classic circular dependency. Neither schema can be parsed first because each
requires the other to already be in the symbol table.

**Why this breaks:**

- You cannot register `Employee` without `Department` existing.
- You cannot register `Department` without `Employee` existing.
- Even if the registry allowed forward references, the Avro parser in your Kafka
  serializer will fail because it parses schemas sequentially.

**How to fix it:**

The only clean solution is to break the cycle. In Avro, this means defining both types
in a single schema using the union-of-records pattern, or inlining one type within the
other:

```json
{
  "type": "record",
  "name": "Employee",
  "namespace": "com.example.hr",
  "fields": [
    { "name": "employeeId", "type": "string" },
    { "name": "name", "type": "string" },
    {
      "name": "department",
      "type": {
        "type": "record",
        "name": "Department",
        "fields": [
          { "name": "departmentId", "type": "string" },
          { "name": "name", "type": "string" },
          { "name": "managerId", "type": "string" }
        ]
      }
    }
  ]
}
```

Notice the compromise: `Department.manager` is now `managerId` (a string), breaking the
cycle. The domain model loses some richness, but the schema actually works. This is a
recurring theme: **Avro schemas are not an ORM. Modeling every relationship as a nested
record is a trap.**

Alternatively, if you absolutely must have both types aware of each other, Avro does
support self-referential types within a single schema file:

```json
{
  "type": "record",
  "name": "Employee",
  "namespace": "com.example.hr",
  "fields": [
    { "name": "employeeId", "type": "string" },
    { "name": "name", "type": "string" },
    {
      "name": "department",
      "type": [
        "null",
        {
          "type": "record",
          "name": "Department",
          "fields": [
            { "name": "departmentId", "type": "string" },
            { "name": "name", "type": "string" },
            { "name": "manager", "type": ["null", "Employee"] }
          ]
        }
      ],
      "default": null
    }
  ]
}
```

This works because `Employee` is already in the symbol table by the time
`Department.manager` references it. The `null` union makes the recursion terminable. But
note: this is a single schema file. You cannot split it across two files and have
cross-file references.

### 3.2 Cross-group references in Apicurio

Apicurio's group model is a double-edged sword. Groups are excellent for organizing
schemas by team or domain, but cross-group references introduce a coupling that is easy
to miss.

Consider two teams:

- **Team Payments** owns group `com.example.payments`
- **Team Shared** owns group `com.example.shared`

Team Payments registers a `Payment` schema that references `Money` from the shared group:

```json
{
  "type": "record",
  "name": "Payment",
  "namespace": "com.example.payments",
  "fields": [
    { "name": "paymentId", "type": "string" },
    { "name": "amount", "type": "com.example.shared.Money" },
    { "name": "currency", "type": "string" }
  ]
}
```

With the reference metadata:

```json
{
  "name": "com.example.shared.Money",
  "groupId": "com.example.shared",
  "artifactId": "Money",
  "version": "1.0.0"
}
```

**What breaks:**

1. **Group deletion**: If Team Shared deletes or reorganizes their group, Team Payments'
   schema becomes unresolvable. The registry may still have the schema content cached,
   but new consumers fetching the schema will fail.

2. **Access control**: If the registry enforces group-level permissions, Team Payments
   may not have read access to `com.example.shared`. The schema registers fine (because
   reference validation uses internal access), but the Kafka serializer running in Team
   Payments' service fails at runtime.

3. **Environment drift**: In staging, the shared group might have `Money` at version
   `1.0.0`. In production, it might be at `2.0.0` with different fields. If your CI
   pipeline registers schemas against staging but your services run against production,
   the version mismatch is silent and deadly.

**How to avoid this:**

- Treat cross-group references as cross-team contracts. Document them. Version them
  independently of the schemas themselves.
- Use Apicurio's artifact rules to enforce compatibility on shared artifacts. Set
  `BACKWARD` or `FULL` compatibility on any artifact that other groups reference.
- In CI, validate references against the target environment's registry, not a local or
  staging instance.

### 3.3 Version pinning drift

This is the most common failure mode I have seen, and it is insidious because it does
not fail immediately.

Suppose you register this schema at version 1:

```json
{
  "type": "record",
  "name": "UserEvent",
  "namespace": "com.example.events",
  "fields": [
    { "name": "userId", "type": "string" },
    { "name": "action", "type": "string" },
    {
      "name": "address",
      "type": "com.example.shared.Address"
    }
  ]
}
```

With a reference to `Address` version `1.0.0`:

```json
// Address v1.0.0
{
  "type": "record",
  "name": "Address",
  "namespace": "com.example.shared",
  "fields": [
    { "name": "street", "type": "string" },
    { "name": "city", "type": "string" },
    { "name": "country", "type": "string" }
  ]
}
```

Six months later, Team Shared evolves `Address` to version `2.0.0`:

```json
// Address v2.0.0
{
  "type": "record",
  "name": "Address",
  "namespace": "com.example.shared",
  "fields": [
    { "name": "street", "type": "string" },
    { "name": "city", "type": "string" },
    { "name": "state", "type": ["null", "string"], "default": null },
    { "name": "postalCode", "type": ["null", "string"], "default": null },
    { "name": "country", "type": "string" }
  ]
}
```

This evolution is backward-compatible in isolation. New fields have defaults. But
`UserEvent` still points to `Address` version `1.0.0`. Now you have three problems:

1. **New producers** using Address v2 write `state` and `postalCode` fields. Old
   consumers using UserEvent (which resolves to Address v1) silently drop those fields.
   Data loss, no errors.

2. **Schema compatibility checks** on `UserEvent` pass, because UserEvent itself has not
   changed. The registry checks compatibility of UserEvent against its previous version,
   not the compatibility of the resolved/dereferenced schema. This is correct behavior
   from the registry's perspective, but it means the effective schema change is
   invisible to the compatibility checker.

3. **Updating the reference** in UserEvent to point to Address v2 might seem safe, but
   if you register UserEvent v2 with Address v2, the compatibility check compares
   UserEvent v2 (with Address v2 resolved) against UserEvent v1 (with Address v1
   resolved). Depending on your compatibility mode, this may or may not pass. With
   `BACKWARD` compatibility, it should pass because Address v2 is backward-compatible.
   But with `FULL` compatibility, the addition of new fields in the resolved schema may
   trigger a failure if the resolver does not handle defaults correctly.

**The fix:**

There is no clean fix. This is a fundamental consequence of version pinning in a system
where schemas evolve independently. What I recommend:

- **Audit pinned versions periodically.** Write a script that queries the registry for
  all schemas with references and checks whether those references point to the latest
  version. If they do not, flag them for review.
- **Bump references deliberately.** When a shared schema evolves, update all referencing
  schemas in the same change set. This is tedious but prevents drift.
- **Prefer self-contained schemas.** Yes, you will duplicate the `Address` definition.
  But each schema evolves independently, and there is no hidden coupling. More on this
  in the recommendations section.

### 3.4 Namespace collisions

This one is subtle and usually only shows up in large organizations where multiple teams
independently define schemas.

Suppose Team A defines:

```json
{
  "type": "record",
  "name": "Metadata",
  "namespace": "com.example.common",
  "fields": [
    { "name": "createdAt", "type": "long" },
    { "name": "updatedAt", "type": "long" }
  ]
}
```

And Team B, unaware of Team A's definition, also defines:

```json
{
  "type": "record",
  "name": "Metadata",
  "namespace": "com.example.common",
  "fields": [
    { "name": "source", "type": "string" },
    { "name": "version", "type": "int" }
  ]
}
```

Both types have the fully qualified name `com.example.common.Metadata`, but they are
completely different schemas. When a third schema references `com.example.common.Metadata`,
which one does it get?

In a registry, this depends on which artifact the reference points to. But in the Avro
parser's symbol table, there can only be one entry for a given fully qualified name. If
both definitions somehow end up being parsed (e.g., through transitive references from
different dependency chains), the second one silently overwrites the first.

**The result:** Data serialized with Team A's `Metadata` gets deserialized with Team B's
`Metadata` definition. Fields do not match. Deserialization either fails with cryptic
errors or, worse, succeeds with garbage data.

**Prevention:**

- Enforce naming conventions that include team or domain identifiers:
  `com.example.payments.Metadata` vs `com.example.analytics.Metadata`.
- Use registry search/listing APIs in CI to detect naming collisions before they reach
  production.
- In Apicurio, use groups to scope names. Same `artifactId` in different groups is fine
  at the registry level, but the Avro fully qualified name must still be unique within
  any single schema's dependency tree.

### 3.5 The chicken-and-egg problem

This is a variant of the circular reference problem, but it manifests at the registry
level even when the schemas themselves are not circular.

Consider a schema registration pipeline that registers schemas declaratively from a Git
repository. The pipeline reads all `.avsc` files and registers them. But if schema A
references B, and the pipeline processes A before B, the registration fails because B
does not exist yet.

```
schemas/
  Address.avsc          # no dependencies
  Payment.avsc          # depends on Address, Money
  Money.avsc            # no dependencies
  Order.avsc            # depends on Payment, Address
  Invoice.avsc          # depends on Order, Payment
```

The correct registration order is:

1. `Address.avsc` (leaf)
2. `Money.avsc` (leaf)
3. `Payment.avsc` (depends on Address, Money)
4. `Order.avsc` (depends on Payment, Address)
5. `Invoice.avsc` (depends on Order, Payment)

This is a topological sort of the dependency graph. It sounds simple, but:

- The dependency information is not in the `.avsc` files themselves (remember, Avro
  references are just strings). You need external metadata --- either the `references`
  array from a manifest file, or you have to parse the schemas to extract type
  references.
- If you parse the schemas to extract references, you need to distinguish between
  references to types defined within the same file and references to types defined in
  other files.
- If the dependency graph has a cycle, the topological sort fails, and you are back to
  the circular reference problem.

**Practical solution:**

Build a registration script that:

1. Parses each `.avsc` file to extract referenced type names
2. Builds a dependency graph
3. Performs a topological sort
4. Registers schemas in order, collecting schema IDs as it goes
5. Uses the collected IDs to populate the `references` array for each registration

I have included an example script in the `examples/` directory that demonstrates this
approach. It is not production-grade, but it shows the pattern.

---

## 4. What I Learned Contributing to Apicurio Registry

I have contributed to several areas of the Apicurio Registry project, and working on the
internals gave me a perspective that I would not have gotten from using the registry as a
black box. Here are the specific areas where schema references create complexity inside
the registry itself.

### 4.1 Content canonicalization for Avro

When you register a new version of a schema, the registry needs to determine whether
the content is actually new or whether it is identical to an existing version. This
requires **canonicalization**: transforming the schema into a standard form so that
semantically identical schemas produce the same representation.

For Avro, canonicalization involves:

- Sorting fields in a deterministic order (alphabetical by field name within the schema
  metadata, though field order in records is significant in Avro's binary encoding, so
  you cannot reorder record fields)
- Removing optional attributes like `doc`, `aliases`, `default` (depending on the
  canonicalization mode)
- Normalizing whitespace and formatting
- Resolving named type references to their canonical form

That last point is where references make things hard. Consider this schema:

```json
{
  "type": "record",
  "name": "Event",
  "namespace": "com.example",
  "fields": [
    { "name": "payload", "type": "com.example.shared.Payload" }
  ]
}
```

To canonicalize this, the registry must decide: do we canonicalize just the schema text
as-is (leaving the reference as a string), or do we resolve the reference and
canonicalize the fully dereferenced schema?

If we canonicalize without resolving, then two schemas with the same text but different
reference targets (e.g., pointing to different versions of `Payload`) are considered
identical. That is wrong.

If we canonicalize with full resolution, then registering a new version of `Payload`
effectively changes the canonical form of every schema that references it, even though
those schemas have not been re-registered. That is also problematic, because it means
the "same" schema has different canonical forms at different points in time.

The approach Apicurio takes is to canonicalize the schema content itself without
inlining references, but to include the reference metadata (groupId, artifactId,
version) as part of the identity. Two schemas with identical content but different
references are considered different versions. This is the right trade-off, but it means
that the canonical form is not purely a function of the schema text --- it also depends
on the reference metadata.

### 4.2 Reference resolution during compatibility checks

Compatibility checking is the crown jewel of any schema registry. It is what prevents
you from deploying a schema change that breaks consumers. But compatibility checking
with references is significantly more complex than checking a standalone schema.

The compatibility check needs to:

1. Fetch the new schema
2. Resolve all its references (recursively, for transitive references)
3. Build the complete, dereferenced Avro schema
4. Fetch the previous version(s) of the schema
5. Resolve all their references (which may point to different versions of the referenced
   schemas)
6. Build the complete, dereferenced Avro schemas for the previous versions
7. Run the Avro compatibility checker against the fully resolved schemas

Step 2 and step 5 are where the complexity hides. The new schema might reference
`Address` version `2.0.0`, while the previous schema references `Address` version
`1.0.0`. The compatibility check is not just "is the new schema compatible with the old
schema?" --- it is "is the new schema, with its specific set of resolved references,
compatible with the old schema, with its specific set of resolved references?"

This means the registry must maintain the ability to resolve references at any historical
point. If `Address` version `1.0.0` is deleted from the registry, compatibility checks
against old versions of any schema that referenced it will fail. This is why Apicurio
(and Confluent SR) generally do not allow deleting artifacts that are referenced by other
artifacts. But enforcing this constraint requires maintaining a reverse index of "who
references me," which is another piece of complexity.

I spent time working through edge cases in this area, and I can tell you that the test
matrix is enormous. You need to test every combination of:

- Compatibility mode (BACKWARD, FORWARD, FULL, BACKWARD_TRANSITIVE, FORWARD_TRANSITIVE,
  FULL_TRANSITIVE)
- Reference topology (direct, transitive, diamond-shaped)
- Evolution type (field added, field removed, type changed, default added/removed)
- Whether the evolution happens in the referencing schema, the referenced schema, or both

### 4.3 The complexity of dereferencing nested references

Dereferencing is the process of taking a schema with external references and producing a
single, self-contained schema. This is necessary for:

- Compatibility checking (as described above)
- Sending the full schema to Kafka serializers/deserializers
- Generating code from schemas

For a simple one-level reference, dereferencing is straightforward: replace the type name
with the full type definition. But for nested references, it gets complicated.

Consider:

```
Invoice -> LineItem -> Product -> Category
                   -> Money
        -> Address
```

Dereferencing `Invoice` requires:

1. Fetch `Invoice`, find references to `LineItem` and `Address`
2. Fetch `LineItem`, find references to `Product` and `Money`
3. Fetch `Product`, find reference to `Category`
4. Fetch `Category` (leaf node)
5. Dereference `Product` by inlining `Category`
6. Fetch `Money` (leaf node)
7. Dereference `LineItem` by inlining dereferenced `Product` and `Money`
8. Fetch `Address` (leaf node)
9. Dereference `Invoice` by inlining dereferenced `LineItem` and `Address`

This is a post-order traversal of the dependency tree. But there are edge cases:

- **Diamond dependencies**: If both `Invoice` and `LineItem` reference `Address`, the
  dereferenced schema must include the `Address` definition only once. The first
  occurrence defines it; subsequent occurrences reference it by name.
- **Version conflicts**: If `Invoice` references `Address` v1 and `LineItem` references
  `Address` v2, which one wins? The answer is: this is an error, and it should be
  caught at registration time. But not all registries catch it.
- **Depth limits**: Without a depth limit, a maliciously crafted schema with deep
  nesting could cause the registry to recurse until it runs out of memory. The registry
  needs to enforce a maximum reference depth.

### 4.4 How JSON Schema references differ from Avro references

Having worked on both Avro and JSON Schema support in Apicurio, I want to briefly
highlight why JSON Schema references are fundamentally simpler.

JSON Schema uses `$ref` with URI resolution:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://example.com/schemas/order.json",
  "type": "object",
  "properties": {
    "orderId": { "type": "string" },
    "amount": { "$ref": "https://example.com/schemas/money.json" }
  }
}
```

The `$ref` keyword is explicit. There is no ambiguity about what is a reference and what
is a type name. The URI provides a globally unique identifier and a resolution mechanism.
The JSON Schema specification defines exactly how `$ref` is resolved, including relative
URIs, JSON pointers, and anchors.

When I built the Reddit classification pipeline, I used JSON Schema for the Kafka topic
schemas precisely because of this clarity. The pipeline processes Reddit posts through a
Flair-based ML model, and the schema for the classified output needs to include both the
original post metadata and the classification results. With JSON Schema, I could define
the classification result schema separately and `$ref` it cleanly.

In Avro, the equivalent would require either inlining the classification result record
or setting up the reference machinery described in this article. For a pipeline that
needs to be reliable above all else, the extra complexity was not worth it.

The key differences in the registry implementation:

| Aspect | Avro References | JSON Schema `$ref` |
|---|---|---|
| Explicitness | Implicit (name in type position) | Explicit (`$ref` keyword) |
| Resolution | Name-based (symbol table lookup) | URI-based (fetch and resolve) |
| Scope | Within Avro parser context | Global (URIs are globally unique) |
| Circularity | Not supported across files | Supported (via `$ref` cycles) |
| Registry integration | Requires external metadata | `$ref` URI can point to registry |

This is not to say JSON Schema is universally better for Kafka. Avro's binary encoding
is significantly more compact, and its code generation is more mature. But if your
primary concern is schema governance and reference management, JSON Schema's explicit
`$ref` mechanism is meaningfully easier to work with.

---

## 5. Practical Recommendations

After dealing with all of the above, here is what I actually recommend for teams building
event-driven systems with Avro schemas.

### 5.1 Design schemas as self-contained where possible

This is my strongest recommendation. The DRY principle does not apply to schema
definitions the way it applies to application code. In application code, duplication
means two places to update when something changes. In schema definitions, duplication
means independence --- each schema evolves on its own terms, with its own compatibility
guarantees.

**Instead of this:**

```json
{
  "type": "record",
  "name": "OrderEvent",
  "namespace": "com.example.events",
  "fields": [
    { "name": "orderId", "type": "string" },
    { "name": "timestamp", "type": "long" },
    { "name": "customer", "type": "com.example.shared.Customer" },
    { "name": "shippingAddress", "type": "com.example.shared.Address" },
    { "name": "billingAddress", "type": "com.example.shared.Address" },
    { "name": "payment", "type": "com.example.shared.Payment" }
  ]
}
```

**Do this:**

```json
{
  "type": "record",
  "name": "OrderEvent",
  "namespace": "com.example.events",
  "fields": [
    { "name": "orderId", "type": "string" },
    { "name": "timestamp", "type": "long" },
    {
      "name": "customer",
      "type": {
        "type": "record",
        "name": "OrderCustomer",
        "fields": [
          { "name": "customerId", "type": "string" },
          { "name": "name", "type": "string" },
          { "name": "email", "type": "string" }
        ]
      }
    },
    {
      "name": "shippingAddress",
      "type": {
        "type": "record",
        "name": "OrderAddress",
        "fields": [
          { "name": "street", "type": "string" },
          { "name": "city", "type": "string" },
          { "name": "state", "type": ["null", "string"], "default": null },
          { "name": "postalCode", "type": ["null", "string"], "default": null },
          { "name": "country", "type": "string" }
        ]
      }
    },
    {
      "name": "billingAddress",
      "type": "OrderAddress"
    },
    {
      "name": "payment",
      "type": {
        "type": "record",
        "name": "OrderPayment",
        "fields": [
          { "name": "paymentId", "type": "string" },
          { "name": "method", "type": "string" },
          { "name": "amount", "type": "double" },
          { "name": "currency", "type": "string" }
        ]
      }
    }
  ]
}
```

Yes, this is more verbose. Yes, `OrderAddress` might look a lot like the shared
`Address` type. But this schema:

- Can be registered without dependencies
- Can be compatibility-checked without resolving external references
- Can be parsed by any Avro parser without registry access
- Can evolve its address fields independently of other schemas' address fields

Notice that `billingAddress` reuses `OrderAddress` by name --- this is Avro's intra-schema
reference mechanism, and it works perfectly because both the definition and the reference
are in the same file.

The trade-off is real: if the `Address` format changes (e.g., you add `postalCode`),
you need to update it in every schema that embeds it. But in my experience, shared types
change rarely, and when they do change, the impact on each consuming schema is different
enough that a "change it once, change it everywhere" approach would be wrong anyway. The
`OrderEvent` might need `postalCode` while the `WarehouseEvent` does not care about it.

### 5.2 Use explicit version pinning for references

If you do use references, always pin to a specific version. Never rely on "latest."

```json
// Good: explicit version
{
  "name": "com.example.shared.Address",
  "groupId": "com.example.shared",
  "artifactId": "Address",
  "version": "1.2.0"
}

// Dangerous: implicit latest (if your tooling supports it)
{
  "name": "com.example.shared.Address",
  "groupId": "com.example.shared",
  "artifactId": "Address"
}
```

Version pinning ensures reproducibility. The same schema definition produces the same
resolved schema at any point in time. Without pinning, a deploy on Tuesday might resolve
to a different `Address` schema than a deploy on Wednesday, even though your code has not
changed.

Pair version pinning with a periodic audit. Write a script that:

1. Lists all schemas in the registry
2. For each schema, lists its references
3. For each reference, checks whether a newer version of the referenced artifact exists
4. Outputs a report of "stale" references

This script should run in CI weekly. Treat stale references as tech debt, not as
emergencies.

### 5.3 Prefer flat schemas over deeply nested references for Kafka topics

The deeper your reference chain, the more things can go wrong. A schema with three
levels of references has three opportunities for version drift, three opportunities for
resolution failure, and three times the complexity in compatibility checking.

My rule of thumb: **if a schema has more than one level of references, flatten it.**

A one-level reference (e.g., `Order` references `Address`) is manageable. A two-level
reference (e.g., `Invoice` references `Order` which references `Address`) is a yellow
flag. Three or more levels is a red flag that your schema design is trying to replicate a
relational data model in a schema format that was designed for flat, denormalized
messages.

Kafka messages are not database rows. They are events. Events should be self-describing
snapshots of state at a point in time, not normalized entities with foreign key
references. Denormalize aggressively.

### 5.4 When references are necessary, register leaf schemas first (bottom-up)

If you must use references, always register schemas in bottom-up order:

1. Identify all leaf schemas (those with no outgoing references)
2. Register them first
3. Work upward through the dependency tree
4. Register the root schemas last

This is a topological sort of the dependency graph, and it is the only order that
guarantees each schema's references are resolvable at registration time.

Automate this. Do not rely on humans to get the order right. A registration script that
performs the topological sort for you is worth the investment. See the example in
`examples/register-order.sh`.

### 5.5 Test schema evolution with references in CI before deploying

This is the advice that is easy to agree with and hard to follow. Testing schema
evolution requires:

1. A running schema registry in CI (Apicurio Registry runs easily in a container)
2. A script that registers the current production schemas (your baseline)
3. A script that attempts to register the new schemas (your candidate)
4. Verification that compatibility checks pass for all affected schemas

Here is a minimal CI step:

```bash
#!/bin/bash
set -euo pipefail

REGISTRY_URL="http://localhost:8080/apis/registry/v3"

# Start Apicurio Registry in a container
docker run -d --name registry -p 8080:8080 apicurio/apicurio-registry:latest-release

# Wait for the registry to be ready
until curl -sf "$REGISTRY_URL/system/info" > /dev/null 2>&1; do
  sleep 1
done

# Register baseline schemas (from main branch)
git stash
./scripts/register-schemas.sh "$REGISTRY_URL"
git stash pop

# Attempt to register candidate schemas (from feature branch)
# This will fail if compatibility checks fail
./scripts/register-schemas.sh "$REGISTRY_URL"

echo "All schema compatibility checks passed."
```

The key insight is that you need to register schemas twice: once with the current
production schemas to establish the baseline, and once with the new schemas to test
compatibility. This catches not just syntax errors but also backward/forward
compatibility violations.

In the Reddit classification pipeline I built, I used this approach with JSON Schema
governance via Apicurio Registry. Every PR that touched a schema file triggered a CI job
that spun up a registry, registered the main-branch schemas, and then attempted to
register the branch schemas. It caught several breaking changes before they reached
production, including one where a field type change in the classification result schema
would have broken the Spark consumer.

### 5.6 Additional tactical advice

A few more recommendations based on hard-won experience:

**Name your types with the schema's purpose, not the domain concept.** Instead of
`Address`, use `OrderShippingAddress` or `UserProfileAddress`. This prevents namespace
collisions and makes it clear that the type belongs to a specific schema's context.

**Use unions with null for optional referenced types.** If a field references an external
type, make it nullable with a default of null:

```json
{
  "name": "shippingAddress",
  "type": ["null", "com.example.shared.Address"],
  "default": null
}
```

This way, if the reference fails to resolve at runtime (it happens), the deserializer
can at least produce a record with null instead of crashing.

**Document your reference topology.** Maintain a diagram (even a simple text file) that
shows which schemas reference which other schemas. When an incident occurs, this
diagram is the difference between a 10-minute resolution and a 2-hour investigation.

**Separate schema registration from application deployment.** Schema changes should be
deployed before the application code that produces/consumes the new schema. This gives
you a window to verify that the schema is valid and compatible before any data flows
through it.

---

## Conclusion

Avro schema references are a feature that looks simple in documentation and becomes
complex in production. The core issue is that Avro was designed for self-contained
schemas, and the reference mechanism was bolted on through schema registries rather than
being a first-class feature of the format.

If you take one thing away from this article, let it be this: **the complexity of schema
references grows superlinearly with the number of schemas.** Two schemas with one
reference between them are manageable. Ten schemas with a web of references are a
governance nightmare. And the costs are paid not when you set it up, but six months later
when someone needs to evolve a shared type and discovers the blast radius.

Design for simplicity. Inline your types. Denormalize your events. Use references only
when the organizational benefit of a single source of truth genuinely outweighs the
operational cost of managing the dependency graph.

The best schema is one that a new team member can read, understand, and evolve without
consulting a dependency graph.

---

*Carles Arnal is a Principal Software Engineer at IBM and a contributor to the Apicurio
Registry project. He works on schema governance, event-driven architectures, and ML
inference pipelines. The opinions in this article are his own.*
