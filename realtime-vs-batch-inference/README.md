# Real-Time vs. Batch ML Inference: Engineering Tradeoffs You Won't Find in Tutorials

**Carles Arnal** | Principal Software Engineer, IBM

---

Every ML tutorial ends the same way: you train a model, call `model.predict()`, and celebrate.
Nobody tells you what happens when that model needs to run continuously against a live data
stream, on a cluster with no GPUs, while two different models race to classify the same input
and you have no ground truth to tell you which one is right.

This article is about the engineering reality of ML inference in production. Not the theory.
Not the "just use SageMaker" hand-wave. The actual decisions you make when you are building a
real-time classification pipeline on Kubernetes with Kafka, Spark Structured Streaming, and
OpenTelemetry -- and you have to justify every architectural choice to your team.

The pipeline I will dissect classifies Reddit posts from r/AskEurope into 13 flair categories
using two models simultaneously: a fine-tuned DistilBERT Transformer and a scikit-learn
TF-IDF/LSA pipeline. The dual-model approach is not a luxury. It is the only way to get a
reliability signal when you have no labeled data at inference time.

Let me walk you through the decisions that shaped this system, the mistakes that informed them,
and the tradeoffs that no tutorial will prepare you for.

---

## Table of Contents

1. [The Inference Spectrum](#1-the-inference-spectrum)
2. [Why Micro-Batch Was the Right Choice](#2-why-micro-batch-was-the-right-choice)
3. [Dual-Model Inference: The Engineering Reality](#3-dual-model-inference-the-engineering-reality)
4. [Production Concerns Nobody Talks About](#4-production-concerns-nobody-talks-about)
5. [What I Would Change at 100x Scale](#5-what-i-would-change-at-100x-scale)
6. [Cost-Accuracy Tradeoff Framework](#6-cost-accuracy-tradeoff-framework)

---

## 1. The Inference Spectrum

The industry loves to frame ML inference as a binary choice: real-time or batch. This framing
is wrong, and it leads to bad architecture decisions. The reality is a spectrum with at least
four distinct operating points, each with fundamentally different infrastructure requirements,
failure modes, and cost profiles.

### True Real-Time (< 100ms)

This is what people mean when they say "real-time inference" -- a model serving endpoint that
responds to HTTP/gRPC requests within a strict latency SLA. Think recommendation engines behind
an API gateway, fraud detection on payment transactions, or ad-ranking models that need to
respond before the page renders.

**Infrastructure:** TensorFlow Serving, NVIDIA Triton Inference Server, KServe on Kubernetes,
or managed services like SageMaker Endpoints.

**Key characteristics:**
- Model is loaded into memory once and serves many requests
- GPU is almost always required for Transformer-class models
- Horizontal scaling via replicas behind a load balancer
- Request-response pattern -- the caller blocks until inference completes
- Autoscaling based on request queue depth or latency percentiles

**When to use it:** When a human or downstream system is waiting for the prediction. Search
ranking, content moderation at upload time, real-time bidding, chatbot responses.

**What they do not tell you:** True real-time inference at scale requires GPU cluster management,
model versioning with zero-downtime rollouts (canary deployments for models), and a model
registry that tracks which version is serving in which environment. The infrastructure cost
alone can dwarf your training costs.

### Near-Real-Time (seconds to minutes)

Stream processing frameworks that consume events from a message broker and apply inference
as part of the stream topology. The latency is bounded by the stream processing framework's
internal buffering and commit intervals, not by user-facing SLA requirements.

**Infrastructure:** Apache Flink, Kafka Streams, Apache Beam, or custom consumers with
embedded models.

**Key characteristics:**
- Event-at-a-time or small-buffer processing
- Model is embedded in the stream processor (loaded at startup)
- Exactly-once or at-least-once semantics depending on the framework
- State management for windowed aggregations or sessionization
- Backpressure is handled by the framework's flow control

**When to use it:** Anomaly detection on IoT sensor data, real-time feature computation for
ML pipelines, event enrichment where latency of a few seconds is acceptable.

**What they do not tell you:** Embedding a large model (like a Transformer) inside a Flink
operator or Kafka Streams topology creates memory pressure that is hard to reason about.
The JVM-based frameworks (Flink, Kafka Streams) add complexity when your model is in Python.
You end up with Python subprocesses, gRPC sidecars, or the infamous Py4J bridge.

### Micro-Batch (minutes)

This is what Spark Structured Streaming does. It is not truly streaming -- it polls the source
at a configurable trigger interval, collects a batch of records, processes them as a DataFrame,
and writes the results. The "streaming" API is a convenience layer over repeated batch jobs.

**Infrastructure:** Apache Spark (Structured Streaming), scheduled Spark jobs with short intervals.

**Key characteristics:**
- Trigger interval defines the processing cadence (our pipeline uses 1 minute)
- `maxOffsetsPerTrigger` caps the batch size for backpressure control
- Full DataFrame API available for feature engineering
- Checkpoint-based fault tolerance (write-ahead log)
- Models loaded once on the driver, executed via UDFs

**This is what our pipeline uses.** More on why in the next section.

**When to use it:** When your data source is not latency-sensitive, when you need DataFrame
operations for feature engineering, or when your team already knows Spark and does not want
to learn Flink.

### Batch (hours)

Traditional batch processing. A scheduled job reads all accumulated data, runs inference on
the full dataset, and writes results to a data warehouse or object store. This is what most
ML pipelines in production actually look like, despite the industry's obsession with real-time.

**Infrastructure:** Spark batch jobs, Airflow DAGs, dbt + Python models, or simple cron jobs.

**Key characteristics:**
- Runs on a schedule (hourly, daily, weekly)
- Can leverage spot instances or preemptible VMs for cost savings
- Retry logic is straightforward (just rerun the whole job)
- No state management complexity
- Results are available after the job completes, not incrementally

**When to use it:** Daily churn prediction, weekly report generation, model retraining
pipelines, any use case where the consumer of predictions can tolerate hours of latency.

**What they do not tell you:** Batch is underrated. Most ML use cases do not need real-time
inference. The engineering complexity of streaming is significant, and the cost of maintaining
a streaming pipeline is ongoing. Batch jobs are easier to debug, easier to test, and easier
to reason about. If your use case can tolerate batch latency, use batch. Seriously.

### The Decision Matrix

Here is how these operating points compare across the dimensions that actually matter:

```
                    True RT     Near-RT     Micro-Batch    Batch
                    --------    --------    -----------    ------
Latency             < 100ms     1-30s       1-10 min       hours
Infra complexity    High        High        Medium         Low
Failure handling    Hard        Hard        Medium         Easy
GPU requirement     Usually     Sometimes   Rarely         Rarely
State management    None*       Complex     Checkpoint     None
Cost (steady)       High        Medium      Medium         Low
Cost (burst)        Very High   High        Medium         Low
Team expertise      ML Eng      Data Eng    Data Eng       Anyone
Debugging           Hard        Very Hard   Medium         Easy

* Model serving endpoints are stateless; state lives in the caller.
```

The key insight: **moving left on this spectrum always costs more than you think, in every
dimension.** The infrastructure gets more complex, the failure modes get subtler, the debugging
gets harder, and the operational burden grows. Move left only when the use case genuinely
demands it.

---

## 2. Why Micro-Batch Was the Right Choice

When I started designing this pipeline, the temptation was to build a "proper" streaming
system. Flink with embedded models, event-at-a-time processing, sub-second latency. It would
look great in an architecture diagram.

Then I asked myself: who is waiting for these predictions?

Nobody. Reddit posts are not time-critical events. A post that was written 5 minutes ago will
still be relevant 6 minutes from now. There is no user staring at a loading spinner, no
downstream system that will timeout, no SLA that demands sub-second inference. The posts
arrive in waves when the producer polls the Reddit API every 5 minutes:

```python
# Producer polling configuration
POLL_INTERVAL = 300    # 5 minutes between successful cycles
```

If your data source polls every 5 minutes, building sub-second inference is architectural
vanity.

### The Arguments for Micro-Batch

**1. Spark gives us DataFrames for free.**

The prediction output is a structured record with fields from both models. With Spark, we
parse the Kafka message into a DataFrame, apply the UDF, and get back a DataFrame we can
filter, transform, and route:

```python
schema = StructType() \
    .add("id", StringType()) \
    .add("title", StringType()) \
    .add("content", StringType())

parsed_df = df.select(
    from_json(col("value").cast("string"), schema).alias("data")
).select("data.*")

# Apply dual inference
predicted_df = parsed_df.withColumn(
    "value",
    predict_udf(struct("id", "title", "content"))
)
```

Try doing that in a raw Kafka consumer. You will end up reimplementing half of Spark's
DataFrame operations by hand.

**2. Failure handling is dramatically simpler.**

In Spark Structured Streaming, if a micro-batch fails, the framework retries it from the
last checkpoint. The checkpoint contains the Kafka offsets that were successfully processed.
No messages are lost, no messages are duplicated (assuming idempotent writes to the sink).

Compare this to a Flink job where you need to manage savepoints, configure state backends,
and reason about exactly-once semantics across multiple operators. Or a raw Kafka consumer
where you manually commit offsets and hope your error handling covers every edge case.

**3. Backpressure is a configuration parameter, not a design problem.**

```python
df = spark.readStream \
    .format("kafka") \
    .option("kafka.bootstrap.servers", kafka_bootstrap_servers) \
    .option("subscribe", "reddit-stream") \
    .option("startingOffsets", "earliest") \
    .option("failOnDataLoss", "false") \
    .option("maxOffsetsPerTrigger", "100") \
    .load()
```

`maxOffsetsPerTrigger=100` means each micro-batch processes at most 100 Kafka messages. If
inference takes longer than the trigger interval, the next batch simply waits. No OOM, no
cascading failures, no complex flow control logic. The unconsumed messages stay in Kafka
(which is designed for exactly this).

**4. The trigger interval is a knob, not a commitment.**

```python
query_success = success_df.select("value") \
    .writeStream \
    .format("kafka") \
    .option("kafka.bootstrap.servers", kafka_bootstrap_servers) \
    .option("topic", "kafka-predictions") \
    .option("checkpointLocation", "/tmp/spark-checkpoints/predictions") \
    .trigger(processingTime="1 minute") \
    .start()
```

One minute today, 10 seconds tomorrow, 5 minutes next week. Changing the trigger interval
is a configuration change, not a code change. Try changing the processing semantics of a
Flink job without rewriting operator logic.

### What We Considered and Rejected

**Flink:** Better latency characteristics, but the team had no Flink expertise. The PyFlink
API was immature at the time we made this decision. The operational overhead of running a
Flink cluster on Kubernetes (via the Flink Operator) was significant. Flink's state management
is powerful but adds complexity we did not need -- we have no windowed aggregations or
sessionization in the inference path.

**Kafka Streams:** Attractive because it runs as a regular Java application (no cluster
manager). But our models are in Python. Running Python models from a JVM-based Kafka Streams
application means either gRPC calls to a sidecar (adding network hops and failure modes) or
embedding a Python runtime via ProcessBuilder (fragile and hard to monitor).

**Raw Kafka consumer with embedded models:** We actually prototyped this. A Python consumer
that reads from Kafka, runs inference, and produces to another topic. It worked for the
single-model case. But when we added the second model, the consumer got complex. When we
added DLQ routing, it got messy. When we added observability, it became unmaintainable.
Spark gave us all of this for free (or at least, for cheap).

**Direct model serving (KServe/Triton):** The producer could call a model serving endpoint
directly instead of sending data through Kafka. This eliminates the streaming infrastructure
entirely. But it couples the producer to the model serving endpoint, makes it harder to
replay data through new model versions, and loses the decoupling benefits of Kafka as a
buffer. For our use case -- where we want to experiment with models independently of data
collection -- the decoupled architecture was worth the complexity.

### The Honest Assessment

Micro-batch is a compromise. It is not the lowest latency, not the simplest infrastructure,
not the cheapest to run. But it hits a sweet spot for our specific requirements:

- **Freshness:** 1-minute latency is more than adequate
- **Throughput:** 100 posts per trigger is more than Reddit produces for r/AskEurope
- **Complexity:** Spark is well-understood technology with good tooling
- **Team skills:** Everyone on the team knows Python and Spark
- **Failure handling:** Checkpoints handle 90% of failure scenarios automatically

The best architecture is the one your team can operate at 3 AM when something breaks. For
this team, that is Spark.

---

## 3. Dual-Model Inference: The Engineering Reality

This is where the article gets interesting. Running two ML models side by side in a Spark
Structured Streaming pipeline sounds straightforward. It is not. Every layer of the stack
fights you in subtle ways.

### Why Two Models?

The canonical answer is "ensemble for better accuracy." That is not why we do it.

We run two models because **we have no ground truth at inference time.** When a Reddit post
arrives, we do not know its true flair. The actual flair was assigned by the post author, but
by the time we run inference, we are working with the post content and need to predict the
flair independently. We cannot compute accuracy, precision, recall, or F1 in real-time because
there is no label to compare against.

The agreement rate between the two models serves as a proxy for prediction reliability:

- **Both models agree, high confidence:** The prediction is probably correct. Both a deep
  neural network and a classical ML pipeline reached the same conclusion.
- **Both models agree, low confidence:** The prediction might be correct but the input is
  ambiguous. Worth flagging for review.
- **Models disagree:** At least one model is wrong. The prediction is unreliable. This is
  the signal that matters.

The Quarkus consumer tracks this in real-time with Prometheus metrics:

```java
// Model agreement gauge (rolling rate)
long total = totalMessages.incrementAndGet();
if (transformerFlair.equals(sklearnFlair)) {
    agreedMessages.incrementAndGet();
}
registry.gauge("model_agreement_rate", this, obj -> {
    long t = totalMessages.get();
    return t > 0 ? (double) agreedMessages.get() / t : 0.0;
});
```

This metric is on a Grafana dashboard. When the agreement rate drops below a threshold, it
means one of the models is drifting -- or the data distribution has shifted. Either way,
something needs human attention.

The consumer also tracks per-flair agreement and builds a confusion matrix between the two
models:

```java
private void updateConfusionMatrix(String transformerFlair, String sklearnFlair) {
    confusionMatrix
        .computeIfAbsent(transformerFlair, k -> new ConcurrentHashMap<>())
        .merge(sklearnFlair, 1, Integer::sum);
}
```

This is not a traditional confusion matrix (predicted vs. actual). It is a model-vs-model
confusion matrix. It tells you which flair categories the models disagree on most frequently,
which is invaluable for targeted model improvement.

### The Dual-Model UDF

Here is the actual UDF from the pipeline, annotated with the engineering decisions embedded
in each line:

```python
def dual_model_prediction(row):
    content = row["content"]
    post_id = row["id"] or "unknown"

    try:
        transformer_flair = "Unknown"
        transformer_conf = 0.0
        sklearn_flair = "Unknown"
        sklearn_conf = 0.0

        if content and content.strip():
            with tracer.start_as_current_span("dual-model-inference", attributes={
                "reddit.post.id": post_id,
            }) as span:
                # Transformer inference
                with tracer.start_as_current_span("transformer-inference"):
                    inputs = tokenizer(
                        content,
                        return_tensors="pt",
                        truncation=True,
                        padding=True,
                        max_length=512
                    )
                    inputs = {k: v.to("cpu") for k, v in inputs.items()}
                    with torch.no_grad():
                        outputs = transformer_model(**inputs)
                    probs = torch.nn.functional.softmax(outputs.logits, dim=-1)
                    pred_class = torch.argmax(probs, dim=1).item()
                    transformer_flair = flairs[pred_class]
                    transformer_conf = float(probs[0][pred_class])

                # Sklearn inference
                with tracer.start_as_current_span("sklearn-inference"):
                    X = tfidf.transform([content])
                    X_reduced = tsvd.transform(X)
                    sk_probs = classifier.predict_proba(X_reduced)[0]
                    sk_class = sk_probs.argmax()
                    sklearn_flair = flairs[sk_class]
                    sklearn_conf = float(sk_probs[sk_class])

                span.set_attribute("prediction.transformer_flair", transformer_flair)
                span.set_attribute("prediction.transformer_confidence", transformer_conf)
                span.set_attribute("prediction.sklearn_flair", sklearn_flair)
                span.set_attribute("prediction.sklearn_confidence", sklearn_conf)
                span.set_attribute("prediction.models_agree",
                                   transformer_flair == sklearn_flair)

        return json.dumps({
            "id": post_id,
            "title": row["title"],
            "content": content,
            "transformer_flair": transformer_flair,
            "transformer_confidence": transformer_conf,
            "sklearn_flair": sklearn_flair,
            "sklearn_confidence": sklearn_conf
        })
    except Exception as e:
        return json.dumps({
            "__dlq": True,
            "id": post_id,
            "title": row["title"],
            "content": content,
            "error": str(e)
        })

predict_udf = udf(dual_model_prediction, StringType())
```

Let me unpack the non-obvious decisions.

### Decision: Models Loaded at Module Level

```python
# Load Transformer model
transformer_model = AutoModelForSequenceClassification.from_pretrained(model_dir)
tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True, use_fast=False)
label_encoder = joblib.load(os.path.join(model_dir, "reddit_flair_label_encoder.joblib"))

transformer_model.to("cpu")
transformer_model.eval()

# Load scikit-learn pipeline
with open("vectorizer.pkl", "rb") as f:
    tfidf = pickle.load(f)
with open("LSA_topics.pkl", "rb") as f:
    tsvd = pickle.load(f)
with open("reddit_classifier.pkl", "rb") as f:
    classifier = pickle.load(f)
```

The models are loaded at the top of the script, outside any function. This is deliberate.

In Spark, PySpark UDFs are serialized using Python's `pickle` module and sent to executors.
But Hugging Face Transformer models are **not picklable** -- they contain CUDA handles,
tokenizer state, and compiled C extensions that cannot be serialized.

The solution in our single-executor deployment: load the models at module level on the
driver. Because we configure `spark.executor.instances=1` and the UDF closure captures
references to the module-level variables, the models are available in the UDF without
serialization.

**This is a deployment constraint.** If you scale to multiple executors, each executor
needs its own copy of the models. The standard pattern is to use a `mapPartitions` function
that loads the model once per partition, or to use Spark's `broadcast` mechanism for
serializable models (which the Transformer model is not).

Here is what the scaling pattern looks like:

```python
# This pattern works for multi-executor deployments
def predict_partition(iterator):
    # Models loaded once per partition (per executor)
    model = AutoModelForSequenceClassification.from_pretrained(model_dir)
    tokenizer = AutoTokenizer.from_pretrained(model_dir)
    model.eval()
    model.to("cpu")

    for row in iterator:
        # ... inference logic ...
        yield result

predicted_df = parsed_df.mapPartitions(predict_partition)
```

We did not use this pattern because:
1. We have a single executor (resource-constrained cluster)
2. `mapPartitions` returns an RDD, not a DataFrame, so we lose the structured streaming sink API
3. The module-level approach is simpler and works for our deployment topology

Trade engineering purity for operational simplicity when the constraints allow it.

### Decision: `use_fast=False` for the Tokenizer

```python
tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True, use_fast=False)
```

This looks like a performance mistake. The "fast" tokenizer (based on Rust via the
`tokenizers` library) is significantly faster than the Python implementation. Why disable it?

Two reasons:

1. **Serialization:** The fast tokenizer has Rust-backed objects that are even harder to
   pickle than the Python-based tokenizer. In some PySpark configurations, even module-level
   closures get serialized during task scheduling, and the fast tokenizer causes
   `PicklingError` exceptions.

2. **Reproducibility:** The fast and slow tokenizers can produce slightly different token
   sequences for edge cases (Unicode handling, special token insertion). When you are
   comparing two models' predictions, you want the tokenization to be deterministic and
   reproducible across environments.

The performance cost is negligible for our workload. Tokenizing a single Reddit post takes
microseconds even with the slow tokenizer. The bottleneck is the model forward pass, not
tokenization.

### Decision: `torch.no_grad()` is Not Optional

```python
with torch.no_grad():
    outputs = transformer_model(**inputs)
```

This is not a performance optimization -- it is a memory requirement. Without `no_grad()`,
PyTorch builds a computation graph for backpropagation, which allocates memory proportional
to the model size and input length. On our resource-constrained executor (3GB memory limit
from the Kubernetes resource spec), skipping `no_grad()` causes OOM kills on long posts.

From the SparkApplication manifest:

```yaml
executor:
  cores: 1
  instances: 1
  memory: 3g
  resources:
    requests:
      memory: "3Gi"
      cpu: "1"
    limits:
      memory: "4Gi"
      cpu: "2"
```

3GB of executor memory holds the DistilBERT model (~260MB), the tokenizer (~50MB), the
scikit-learn pipeline (~30MB), the JVM overhead, PySpark worker overhead, and the data
being processed. There is no room for gradient computation graphs.

### The OTel Tracing Problem

OpenTelemetry tracing in Spark UDFs is architecturally broken, and nobody talks about it.

In a normal application, spans form a tree: a parent span creates child spans, and the
trace context propagates automatically via thread-local storage. The trace context is set
when you call `tracer.start_as_current_span()`, and child spans inherit the parent's trace
ID and span ID.

In Spark, the story is different:

1. The **producer** creates a span when sending a message to Kafka and injects the trace
   context into Kafka headers via W3C TraceContext propagation:

```python
# In the producer
with tracer.start_as_current_span("produce-reddit-post", attributes={
    "reddit.post.id": post_data["id"],
    "kafka.topic": KAFKA_TOPIC,
}) as span:
    headers = []
    carrier = {}
    propagator.inject(carrier)
    for k, v in carrier.items():
        headers.append((k, v.encode("utf-8")))
    producer.send(KAFKA_TOPIC, value=post_data, headers=headers)
```

2. The **Spark job** reads from Kafka, but Spark's Kafka source **strips the headers.** The
   DataFrame only contains `key`, `value`, `topic`, `partition`, `offset`, and `timestamp`.
   The trace context in the Kafka headers is lost.

3. Even if we extracted the trace context, UDFs run in a different execution context than
   the driver. Thread-local trace context does not propagate to UDF execution.

**Our solution:** We do not try to maintain distributed trace continuity from producer
through Spark to consumer. Instead, we create independent spans in the Spark UDF and
correlate them using the `reddit.post.id` attribute:

```python
with tracer.start_as_current_span("dual-model-inference", attributes={
    "reddit.post.id": post_id,
}) as span:
```

In Jaeger, you can search for all spans with `reddit.post.id=abc123` and see the producer
span, the Spark inference span, and the consumer span. They are not linked as parent-child,
but they are correlated by the post ID. This is a pragmatic compromise: you lose automatic
trace tree visualization but gain the ability to track a post through the entire pipeline.

The consumer, built with Quarkus, gets automatic OTel instrumentation via the Quarkus
OpenTelemetry extension:

```properties
# OpenTelemetry tracing
quarkus.application.name=flair-consumer
quarkus.otel.exporter.otlp.endpoint=http://jaeger.reddit-realtime.svc:4317
quarkus.otel.exporter.otlp.traces.protocol=grpc
```

Quarkus automatically creates spans for Kafka message consumption and REST endpoint calls.
No manual instrumentation needed. The contrast with the Spark UDF is stark -- frameworks
with first-class OTel support make observability trivial; frameworks without it make it a
research project.

### The Two Models Side by Side

Understanding the tradeoffs between the two models is essential for understanding the
architecture.

**DistilBERT Transformer:**
- Architecture: 6-layer Transformer, ~66M parameters, fine-tuned on r/AskEurope posts
- Input: Raw text, tokenized into subwords, max 512 tokens
- Feature extraction: Learned contextual embeddings (attention-based)
- Strengths: Understands context, word order, and semantic nuance
- Weaknesses: Slow on CPU (~100-500ms per inference), large memory footprint
- Confidence calibration: Softmax probabilities tend to be overconfident

**scikit-learn TF-IDF/LSA Pipeline:**
- Architecture: TF-IDF vectorization -> Truncated SVD (LSA) dimensionality reduction -> classifier
- Input: Raw text, tokenized into whitespace-separated words
- Feature extraction: Bag-of-words statistics, projected to lower-dimensional space
- Strengths: Very fast (~1-5ms per inference), tiny memory footprint, interpretable
- Weaknesses: No understanding of word order, context, or semantics. "not happy" and "happy" are similar.
- Confidence calibration: `predict_proba` gives well-calibrated probabilities for logistic regression

The Transformer model is the "better" model in terms of accuracy on held-out test data. But
"better" comes with 100x the latency and 10x the memory. The scikit-learn model is "good
enough" for many categories -- especially the ones with distinctive vocabulary (Sports, Food,
Travel) -- and it serves as a fast sanity check on the Transformer's predictions.

### Wrapping Each Model in Its Own Span

Each model gets its own OTel span:

```python
# Transformer inference
with tracer.start_as_current_span("transformer-inference"):
    # ... transformer code ...

# Sklearn inference
with tracer.start_as_current_span("sklearn-inference"):
    # ... sklearn code ...
```

This is not just for tracing aesthetics. In Jaeger, you can see:

- **transformer-inference** spans averaging 200ms
- **sklearn-inference** spans averaging 3ms

This data informs the scaling conversation. If you need to optimize latency, you know exactly
which model is the bottleneck. If the Transformer span suddenly jumps from 200ms to 2000ms,
you know something changed -- maybe the input distribution shifted to longer posts that hit
the 512-token maximum.

The span attributes capture the prediction details:

```python
span.set_attribute("prediction.transformer_flair", transformer_flair)
span.set_attribute("prediction.transformer_confidence", transformer_conf)
span.set_attribute("prediction.sklearn_flair", sklearn_flair)
span.set_attribute("prediction.sklearn_confidence", sklearn_conf)
span.set_attribute("prediction.models_agree", transformer_flair == sklearn_flair)
```

This means you can query Jaeger for "all spans where models disagreed" or "all spans where
Transformer confidence was below 0.5." This is operational intelligence that you cannot get
from logs.

---

## 4. Production Concerns Nobody Talks About

Every streaming ML system has the same set of operational challenges. The difference between
a demo and a production system is how you handle them.

### Model Versioning and Deployment

Here is the brutal truth about model versioning in Spark Structured Streaming: **there is no
hot-reload mechanism.** When you want to deploy a new model version, you:

1. Build a new Docker image with the new model files baked in
2. Update the SparkApplication manifest with the new image tag
3. Stop the running Spark job (this is a restart, not a rolling update)
4. Start the new SparkApplication
5. The new job resumes from the last checkpoint and processes with the new model

```yaml
spec:
  type: Python
  pythonVersion: "3"
  mode: cluster
  image: quay.io/carlesarnal/spark-inference:sha-2e4c7d5  # <-- new image = new model
  imagePullPolicy: IfNotPresent
  mainApplicationFile: local:///opt/spark/jobs/reddit_flair_spark_inference.py
```

During the restart, no inference happens. Messages accumulate in Kafka. When the new job
starts, it processes the backlog. This is acceptable for our use case (minutes of latency
are fine), but it would be unacceptable for a system with real-time SLAs.

**What I would do differently at scale:** Decouple the model from the compute. Store model
artifacts in object storage (S3, GCS), have the Spark job check for new model versions
periodically, and reload the model without restarting the job. This requires careful handling
of in-flight predictions (you do not want half a micro-batch processed with the old model and
half with the new one), but it eliminates the downtime window.

### Model Drift Detection

Model drift is insidious. The model does not fail -- it just gets slowly worse. In a batch
system, you can compute accuracy against ground truth labels. In a streaming system with no
labels, you need proxy metrics.

Our proxy metrics, exposed via the Quarkus consumer to Prometheus:

**1. Agreement rate between models:**
```java
registry.gauge("model_agreement_rate", this, obj -> {
    long t = totalMessages.get();
    return t > 0 ? (double) agreedMessages.get() / t : 0.0;
});
```

If the agreement rate is dropping, at least one model's behavior is changing. This does not
tell you which model is drifting, but it tells you something is happening.

**2. Per-model confidence distributions:**
```java
registry.gauge("model_confidence_latest",
    io.micrometer.core.instrument.Tags.of("model", "transformer"),
    transformerConfidence);
registry.gauge("model_confidence_latest",
    io.micrometer.core.instrument.Tags.of("model", "sklearn"),
    sklearnConfidence);
```

A model that is becoming less confident over time is seeing data that looks different from
its training data. This is a leading indicator of drift.

**3. Per-flair prediction counts:**
```java
registry.counter("flair_messages_total", "model", "transformer", "flair", transformerFlair)
    .increment();
registry.counter("flair_messages_total", "model", "sklearn", "flair", sklearnFlair)
    .increment();
```

If the distribution of predicted flairs shifts significantly (e.g., "Politics" predictions
double while "Sports" predictions halve), the data distribution has changed. This could be
a real change in the subreddit's content or a sign of model degradation.

**What to alert on:**
- Agreement rate drops below 60% (historical baseline is ~70-75%)
- Average confidence drops below 0.4 for either model
- Any single flair category accounts for more than 30% of predictions (distribution skew)
- DLQ message rate exceeds 5% of total messages

These thresholds are empirically derived, not theoretically justified. You tune them based
on your baseline and your tolerance for false alerts.

### The Uncertainty Zone

The consumer tracks a particularly useful derived metric -- the "uncertainty zone" that
categorizes each prediction into one of three states:

```java
private static final double CONFIDENCE_THRESHOLD = 0.6;

private void updateModelUncertaintyZone(
        double transformerConfidence, double sklearnConfidence) {
    if (transformerConfidence >= CONFIDENCE_THRESHOLD
            && sklearnConfidence >= CONFIDENCE_THRESHOLD) {
        bothConfident++;
    }
    else if (transformerConfidence < CONFIDENCE_THRESHOLD
            && sklearnConfidence < CONFIDENCE_THRESHOLD) {
        bothUncertain++;
    }
    else {
        disagreement++;
    }
}
```

- **Both confident:** The prediction is reliable. Both models are confident and (usually) agree.
- **Both uncertain:** The input is genuinely ambiguous. Maybe the post is short, off-topic, or
  multi-category. No amount of model improvement will fix this.
- **Disagreement zone:** One model is confident, the other is not. This is the most interesting
  region -- it often reveals category boundaries where the models have learned different
  decision surfaces.

A rising "both uncertain" rate over time is a strong signal that the subreddit's content is
becoming harder to classify, or that the training data no longer represents the current
content distribution.

### Memory Management

Running a Transformer model and a scikit-learn pipeline simultaneously in a single process
is a memory management exercise. Here is the approximate memory budget:

```
DistilBERT model weights:      ~260 MB
Tokenizer vocabulary:           ~50 MB
scikit-learn TF-IDF vectorizer: ~15 MB
Truncated SVD (LSA) model:       ~5 MB
Classifier:                     ~10 MB
PyTorch runtime overhead:      ~200 MB
PySpark worker overhead:       ~300 MB
JVM overhead (Spark executor): ~500 MB
Data buffers (micro-batch):    ~100 MB
---------------------------------------
Total:                        ~1440 MB
```

Our executor has 3GB of memory with a 4GB limit. That leaves roughly 1.5GB of headroom for
garbage collection spikes, PyTorch temporary tensors, and the occasional large post that
produces oversized intermediate representations.

The `maxOffsetsPerTrigger=100` configuration is not just about processing cadence -- it is a
memory protection mechanism. Processing 100 posts at a time bounds the peak memory usage.
If we removed this limit and a burst of 10,000 posts arrived, the UDF would be called 10,000
times in a single micro-batch, and the cumulative temporary tensor allocations would OOM the
executor.

```python
.option("maxOffsetsPerTrigger", "100")
```

This is the most important configuration parameter in the pipeline and nobody would know it
from reading the Spark documentation. The docs describe it as a "rate limit." It is actually
a **memory safety valve.**

### Backpressure in Practice

What happens when inference takes longer than the trigger interval?

With `trigger(processingTime="1 minute")`, Spark tries to start a new micro-batch every 60
seconds. If the previous micro-batch is still running (because the Transformer model is
slow on a batch of long posts), Spark simply waits. The next trigger fires after the current
batch completes.

This is graceful degradation by design. The system does not crash, does not drop messages,
and does not accumulate unbounded state. The latency increases temporarily, and the backlog
in Kafka grows, but the system remains stable.

The key insight: **Kafka is the backpressure buffer.** Messages are retained in Kafka for
the configured retention period (7 days in our setup). The Spark job consumes at whatever
rate it can sustain. If it falls behind, the messages wait in Kafka. If it catches up, it
processes the backlog.

This only works because Kafka's retention policy is longer than any reasonable backlog
period. If your retention is 1 hour and your processing falls behind by 2 hours, you lose
data. Set your retention generously.

### DLQ Design for ML Inference

The Dead Letter Queue (DLQ) pattern is well-known in messaging systems, but applying it to
ML inference requires some thought about what "failure" means.

In traditional messaging, a message fails because it is malformed, the downstream system is
unavailable, or the processing logic throws an exception. In ML inference, a message can
"fail" for ML-specific reasons:

- The input text is empty or whitespace-only (not a processing error, but inference is meaningless)
- The tokenizer produces an input that exceeds the model's maximum sequence length
- The model returns NaN probabilities (rare but possible with extreme inputs)
- A CUDA/CPU error during the forward pass (more common than you would think)

Our DLQ design handles this within the UDF itself:

```python
except Exception as e:
    # Return DLQ-flagged record so it can be routed to the DLQ topic
    return json.dumps({
        "__dlq": True,
        "id": post_id,
        "title": row["title"],
        "content": content,
        "error": str(e)
    })
```

The UDF never throws an exception. Instead, it returns a JSON payload with a `__dlq` flag.
The streaming query then routes these records to the DLQ topic:

```python
# Split successful predictions from DLQ records
is_dlq = get_json_object(col("value"), "$.__dlq") == "true"
success_df = predicted_df.filter(~is_dlq)
dlq_df = predicted_df.filter(is_dlq)

# Kafka sink -- successful predictions
query_success = success_df.select("value") \
    .writeStream \
    .format("kafka") \
    .option("topic", "kafka-predictions") \
    .option("checkpointLocation", "/tmp/spark-checkpoints/predictions") \
    .trigger(processingTime="1 minute") \
    .start()

# Kafka sink -- DLQ for failed inference
query_dlq = dlq_df.select("value") \
    .writeStream \
    .format("kafka") \
    .option("topic", DLQ_TOPIC) \
    .option("checkpointLocation", "/tmp/spark-checkpoints/dlq") \
    .trigger(processingTime="1 minute") \
    .start()
```

**Why not throw exceptions from the UDF?** Because a single exception in a UDF fails the
entire micro-batch. If one out of 100 posts causes a tokenizer error, you do not want to
retry the other 99 posts. By catching exceptions inside the UDF and routing failures to the
DLQ, the successful predictions still make it to the output topic.

The DLQ topics are configured with 7-day retention:

```yaml
apiVersion: kafka.strimzi.io/v1beta2
kind: KafkaTopic
metadata:
  name: reddit-inference-dlq
  namespace: reddit-realtime
  labels:
    strimzi.io/cluster: reddit-posts
spec:
  partitions: 1
  replicas: 1
  config:
    retention.ms: 604800000  # 7 days
```

Seven days gives you time to investigate failures, fix the root cause, and replay the DLQ
messages if needed. The DLQ records include the original content and the error message, so
you can reproduce the failure locally.

The consumer also has its own DLQ for messages that fail during consumption:

```properties
# Dead Letter Queue -- failed messages go here instead of being dropped
mp.messaging.incoming.kafka-predictions.failure-strategy=dead-letter-queue
mp.messaging.incoming.kafka-predictions.dead-letter-queue.topic=predictions-dlq
mp.messaging.incoming.kafka-predictions.dead-letter-queue.value.serializer=\
    org.apache.kafka.common.serialization.StringSerializer
```

Two DLQs -- one for inference failures, one for consumption failures -- because the failure
modes are different and the remediation strategies are different.

### The Spark Checkpoint Trap

This one will bite you exactly once, and you will never forget it.

Spark Structured Streaming checkpoints store:
- Kafka offsets (which messages have been processed)
- Query progress metadata
- **The output schema of the streaming query**

That last one is the trap. If you change the UDF's output schema -- say, by adding a new
field to the prediction JSON -- and you resume from an existing checkpoint, Spark will fail
with an `AnalysisException` because the checkpoint's schema does not match the current
query's schema.

The fix: **wipe the checkpoints and restart from scratch.**

```bash
# The nuclear option
rm -rf /tmp/spark-checkpoints/predictions
rm -rf /tmp/spark-checkpoints/dlq
```

This means reprocessing all messages from the `startingOffsets` position (which we set to
`earliest`). For our pipeline, this is acceptable because inference is idempotent and the
consumer is designed to handle duplicate predictions gracefully.

For pipelines where reprocessing is expensive or has side effects, you need a migration
strategy:
- Write a one-time migration job that reads the old checkpoint, translates the schema,
  and writes a new checkpoint
- Or, maintain backward-compatible output schemas and add new fields as optional

We chose the nuclear option because our pipeline is small and reprocessing is cheap. At
scale, you would need the migration strategy.

### Schema Validation with Apicurio Registry

The prediction output schema is registered in Apicurio Registry, which ensures that schema
changes are intentional and validated:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:reddit:predictions:value",
  "title": "KafkaPredictionsValue",
  "type": "object",
  "properties": {
    "id": {"type": "string", "description": "Reddit post ID"},
    "title": {"type": "string", "description": "Original post title"},
    "content": {"type": "string", "description": "Cleaned post content"},
    "transformer_flair": {"type": "string", "description": "Flair predicted by Transformer model"},
    "transformer_confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "sklearn_flair": {"type": "string", "description": "Flair predicted by scikit-learn model"},
    "sklearn_confidence": {"type": "number", "minimum": 0, "maximum": 1}
  },
  "required": ["id", "transformer_flair", "transformer_confidence",
                "sklearn_flair", "sklearn_confidence"]
}
```

The model-metadata service validates model context payloads against schemas stored in Apicurio
before accepting them:

```java
JsonValidationResult validationResult = validator.validate(record);

if (validationResult.success()) {
    String modelId = newModel.get("name").asText();
    store.put(modelId, newModel);
    return Response.status(Response.Status.CREATED)
        .entity(Map.of("modelId", modelId))
        .build();
} else {
    return Response.status(Response.Status.BAD_REQUEST)
        .entity(Map.of("error", "Model validation failed",
                        "details", validationResult.getValidationErrors()))
        .build();
}
```

This is a production-grade pattern: schema validation at the boundary prevents malformed
data from propagating through the pipeline. If someone deploys a new model version that
produces output with missing fields, the validation catches it before it poisons the
downstream consumer's metrics.

### Exponential Backoff in the Producer

The producer implements exponential backoff with jitter for Reddit API errors:

```python
INITIAL_BACKOFF = 1    # seconds
MAX_BACKOFF = 300      # 5 minutes cap
consecutive_errors = 0

while True:
    try:
        # ... fetch and send posts ...
        consecutive_errors = 0
        time.sleep(POLL_INTERVAL)
    except Exception as e:
        consecutive_errors += 1
        backoff = min(INITIAL_BACKOFF * (2 ** consecutive_errors), MAX_BACKOFF)
        jitter = random.uniform(0, backoff * 0.25)
        wait_time = backoff + jitter
        time.sleep(wait_time)
```

The jitter is critical. Without it, if multiple producer instances hit a rate limit
simultaneously, they all retry at the same time and hit the rate limit again. Jitter
desynchronizes the retries.

The 5-minute cap prevents the backoff from growing unboundedly. After 8 consecutive errors,
the backoff reaches the cap: `min(1 * 2^8, 300) = min(256, 300) = 256 seconds`. The 9th
error would be 512 seconds, but the cap keeps it at 300.

The producer also has a file-based DLQ for messages that fail to send to Kafka:

```python
DLQ_FILE = os.environ.get("DLQ_FILE", "/tmp/producer-dlq.jsonl")

def on_send_error(excp, post_data=None):
    if post_data:
        with open(DLQ_FILE, "a") as f:
            dlq_record = {
                "error": str(excp),
                "data": post_data,
                "timestamp": dt.datetime.now().isoformat()
            }
            f.write(json.dumps(dlq_record) + "\n")
```

This is a last-resort mechanism. If Kafka is down, the posts are not lost -- they are written
to a JSONL file that can be replayed later. The file-based DLQ is crude but effective: it
survives Kafka outages and does not depend on any external system.

---

## 5. What I Would Change at 100x Scale

Our pipeline processes hundreds of posts per day from a single subreddit. What happens when
you need to handle tens of thousands of posts per minute across hundreds of subreddits?

Everything breaks, and it breaks in predictable ways. Here is the scaling plan.

### Move the Transformer to a Dedicated Model Serving Endpoint

At 100x scale, running the Transformer model inside a Spark UDF is untenable. The model is
too slow on CPU, and you cannot add GPUs to Spark executors without significant cluster
reconfiguration.

**The change:** Deploy the Transformer model behind a Triton Inference Server or TF Serving
endpoint. The Spark UDF calls the endpoint via HTTP instead of running inference locally:

```python
import requests

TRITON_URL = "http://triton.model-serving.svc:8000/v2/models/distilbert-reddit/infer"

def transformer_inference_remote(content, post_id):
    payload = {
        "inputs": [{
            "name": "text",
            "shape": [1],
            "datatype": "BYTES",
            "data": [content]
        }]
    }
    response = requests.post(TRITON_URL, json=payload, timeout=5.0)
    result = response.json()
    predicted_class = result["outputs"][0]["data"][0]
    confidence = result["outputs"][1]["data"][0]
    return flairs[predicted_class], confidence
```

**Why this works at scale:**
- Triton runs on GPU nodes, making Transformer inference 10-50x faster
- Triton handles batching internally (dynamic batching of concurrent requests)
- The Spark job stays on CPU-only nodes (cheaper)
- Model versioning becomes a Triton configuration change, not a Docker image rebuild
- You can independently scale the model serving tier and the stream processing tier

**Keep scikit-learn inline.** The scikit-learn pipeline runs in ~3ms. There is no reason to
add a network hop for something that fast. The overhead of serializing the request, sending
it over HTTP, and deserializing the response would be longer than the inference itself.

### Add GPU Nodes for Transformer Serving

On CPU, DistilBERT inference takes ~200ms per post. On a T4 GPU, it takes ~5ms. On an A10G,
it takes ~2ms. At 100x scale, the difference between 200ms and 5ms is the difference between
needing 200 CPU cores and needing 1 GPU.

The cost math is straightforward:
- 200 CPU cores on AWS (c6i.xlarge instances): ~$600/month
- 1 T4 GPU on AWS (g4dn.xlarge): ~$380/month

GPUs win on cost at scale for Transformer inference. They lose at small scale because of the
minimum cost floor (you pay for the GPU even when it is idle).

### Replace Spark Structured Streaming with Flink

At 100x scale, the 1-minute trigger interval becomes a bottleneck. Not because the inference
is slow, but because the micro-batch overhead (planning, scheduling, checkpointing) adds
latency that compounds with scale.

Flink's advantages at scale:
- **True streaming:** Event-at-a-time processing, no micro-batch overhead
- **Better state management:** RocksDB state backend for large state, incremental checkpointing
- **Lower latency:** Sub-second end-to-end latency is achievable
- **Better backpressure:** Credit-based flow control, not "just wait for the batch to finish"
- **Native Python support:** PyFlink has matured significantly (though it is still not as
  polished as PySpark)

The migration path:
1. Extract the model serving to Triton (do this first, regardless of Spark vs. Flink)
2. Rewrite the stream processing in Flink (the logic is simple: read from Kafka, call Triton,
   write to Kafka)
3. Keep the Quarkus consumer as-is (it reads from Kafka, it does not care what wrote to Kafka)
4. Migrate observability from Spark's OTel setup to Flink's metrics system

### Use Feature Stores for Pre-Computed Features

At scale, you do not want to recompute features on every inference request. Features that
depend on historical data (e.g., "average post length in this subreddit over the last 30
days") should be pre-computed and stored in a feature store.

For our pipeline, the text cleaning (URL removal, stopword removal, lemmatization) is done
in the producer:

```python
cleaning.clean_text(data, 'title')
cleaning.clean_text(data, 'body')
cleaning.clean_text(data, 'comments')

data['content'] = data.title + ' ' + data.body + ' ' + data.comments + ' ' + data.domain
```

At scale, this cleaning should be a feature transformation registered in a feature store
(Feast, Tecton, or AWS Feature Store), with versioning and lineage tracking. This way, if
you change the cleaning logic, you can recompute features for the training data and retrain
the model consistently.

### Add A/B Testing Infrastructure

With a single pipeline, you cannot compare model versions in production. You know the new
model scores better on offline metrics, but does it actually produce better predictions on
live data?

At scale, you split traffic:
- 90% of posts go through Model A (current production model)
- 10% go through Model B (candidate model)
- Both produce predictions, both are tracked in Prometheus
- After N posts, compare agreement rates, confidence distributions, and (if you have
  delayed ground truth) accuracy

This requires:
- A traffic router (built into the Spark job or Flink operator)
- Model version metadata attached to each prediction
- Dashboard panels that compare metrics by model version
- Automated rollback if Model B's metrics are significantly worse

### Schema Versioning for Model Outputs

We already have this with Apicurio Registry. At scale, it becomes critical:
- Each model version defines an output schema version
- Consumers declare which schema versions they support
- Schema evolution rules (backward/forward compatibility) are enforced by the registry
- Breaking schema changes require a migration period with dual writes

This is the one piece of our current architecture that scales well without modification. The
schema registry is the contract between the model and its consumers. As long as both sides
honor the contract, the system is composable.

---

## 6. Cost-Accuracy Tradeoff Framework

This is the section where I give you a framework for deciding when to spend money on better
models and when to stop.

### When Is the scikit-learn Model "Good Enough"?

The agreement rate between the two models tells you when the simpler model suffices. Here is
the decision tree:

```
Agreement rate > 90%:
  The sklearn model is probably good enough for this category.
  Consider dropping the Transformer for cost savings.

Agreement rate 70-90%:
  The models have meaningful disagreements.
  Both models add value. Keep the dual-model setup.

Agreement rate < 70%:
  The models are producing substantially different predictions.
  At least one model is wrong most of the time.
  Investigate: is the data distribution shifting, or is one model fundamentally
  unsuited for this category?
```

But agreement rate alone is not sufficient. You also need to consider **which model is
wrong when they disagree.** If the Transformer is right 90% of the time on disagreements
and sklearn is right 10%, the Transformer is clearly superior. If they are each right 50%
of the time on disagreements, both models are equally bad at the hard cases, and the simpler
model is sufficient for the easy ones.

Without ground truth, you cannot compute this directly. But you can use confidence as a
proxy:

```
When models disagree:
  If Transformer confidence > 0.8 and sklearn confidence < 0.3:
    Transformer is probably right. The sklearn model cannot handle this category.
  If sklearn confidence > 0.8 and Transformer confidence < 0.3:
    sklearn is probably right. The Transformer is confused (rare, but investigate).
  If both have moderate confidence (0.4-0.6):
    Neither model is confident. The input is genuinely ambiguous.
```

### When to Drop a Model

If the agreement rate is consistently above 95% across all categories for a sustained period
(at least 2 weeks of data), you have strong evidence that the simpler model produces the same
predictions as the complex model. In this case:

**Drop the Transformer.** You save:
- ~260MB of memory per executor
- ~200ms of latency per inference
- CPU cycles that could process more posts per micro-batch
- Docker image size (the Transformer model artifacts are ~500MB)
- Complexity in the UDF, the tracing, and the consumer

**Keep the sklearn model.** It is fast, small, and apparently just as accurate as the
Transformer for your data distribution.

But there is a catch. A 95% agreement rate on historical data does not guarantee future
agreement. If the data distribution shifts (new flair categories, different writing styles,
influx of non-English posts), the models may diverge again. You need a monitoring period
after dropping the Transformer where you run both models in shadow mode (both predict, but
only the sklearn prediction is used downstream) to verify that agreement remains high.

### Infrastructure Cost Comparison

Here is the real cost breakdown for our pipeline running on a Kubernetes cluster:

```
Current setup (CPU-only, dual-model):
  Spark driver:   1 CPU, 3GB RAM     ~$25/month
  Spark executor: 1 CPU, 3GB RAM     ~$25/month
  Kafka broker:   2 CPU, 4GB RAM     ~$50/month
  Quarkus consumer: 0.5 CPU, 512MB   ~$10/month
  Prometheus:     0.25 CPU, 512MB    ~$5/month
  Grafana:        0.25 CPU, 256MB    ~$3/month
  Jaeger:         0.5 CPU, 1GB       ~$8/month
  Apicurio:       0.5 CPU, 512MB     ~$8/month
  ----------------------------------------
  Total:                              ~$134/month

Hypothetical: sklearn only (drop Transformer):
  Spark driver:   0.5 CPU, 1GB RAM   ~$12/month
  Spark executor: 0.5 CPU, 1GB RAM   ~$12/month
  (rest stays the same)
  ----------------------------------------
  Total:                              ~$108/month
  Savings:                             ~19%

Hypothetical: GPU for Transformer (100x scale):
  Spark driver:   2 CPU, 4GB RAM     ~$40/month
  Spark executors (x4): 2 CPU, 4GB   ~$160/month
  Triton on GPU:  T4 GPU, 16GB RAM   ~$380/month
  Kafka brokers (x3): 4 CPU, 8GB     ~$300/month
  (rest stays the same, scaled up)
  ----------------------------------------
  Total:                              ~$930/month
```

The jump from $134/month to $930/month for 100x scale is a 7x cost increase for 100x
throughput. That is actually a good scaling ratio. Most real-time ML systems have
super-linear cost scaling because GPUs have minimum cost floors and Kafka broker costs
grow with partition count.

### The Real Cost Is Engineering Complexity

Here is the uncomfortable truth: the compute cost is the smallest line item. The real
costs are:

1. **Engineering time to build and maintain the pipeline.** A dual-model streaming pipeline
   with full observability took roughly 3 months of part-time engineering effort. A batch
   pipeline with a single model would have taken 2 weeks.

2. **Operational burden.** Streaming pipelines need monitoring, alerting, and on-call rotation.
   Batch pipelines need a cron job and an email alert if it fails.

3. **Debugging cost.** When a streaming pipeline produces wrong predictions, you need to trace
   the message through Kafka, Spark, and the consumer. When a batch pipeline produces wrong
   predictions, you re-run it with logging enabled.

4. **Opportunity cost.** The time spent maintaining the streaming infrastructure is time not
   spent on improving the model, exploring new features, or building new products.

The framework for deciding whether the complexity is worth it:

```
Does the use case require sub-minute freshness?
  No  --> Use batch. Stop here.
  Yes --> Continue.

Does the use case require sub-second latency?
  No  --> Use micro-batch (Spark Structured Streaming). Stop here.
  Yes --> Continue.

Is there a user-facing latency SLA?
  No  --> Use near-real-time (Flink/Kafka Streams). Stop here.
  Yes --> Use true real-time (model serving endpoint).

At each level, ask:
  Can my team operate this at 3 AM?
  Do we have the monitoring to detect failures?
  Can we debug issues without the original developer?

If the answer to any of these is "no," move one step to the right
(toward batch) until the answers are all "yes."
```

### Practical Takeaways

**1. Start with batch.** Always. Even if you "know" you need real-time. Build the batch
pipeline first, validate the model, establish baseline metrics, and then decide if the
latency is actually a problem.

**2. Agreement rate is your best friend in the absence of ground truth.** Two models that
agree are more trustworthy than one model with high confidence. The models have different
inductive biases, different failure modes, and different strengths. When they converge on
the same prediction, the prediction is robust.

**3. `maxOffsetsPerTrigger` is a safety valve, not a rate limiter.** Set it based on your
memory budget, not your desired throughput. The throughput will be whatever the system can
sustain; the rate limiter prevents it from exceeding what the system can handle.

**4. DLQ everything.** Every stage of the pipeline should have a DLQ. The producer has a
file-based DLQ. The Spark job has a Kafka DLQ topic. The consumer has a Kafka DLQ topic.
When something fails, you want the data preserved for investigation and replay.

**5. Schema registries are not optional at scale.** The Apicurio Registry integration
caught a schema incompatibility during development that would have been a production
incident. The cost of running a schema registry is negligible compared to the cost of
debugging a schema mismatch in production.

**6. OTel in Spark UDFs is a hack, and that is fine.** Perfect distributed tracing through
Spark is not achievable without framework-level support. Pragmatic correlation via custom
attributes (`reddit.post.id`) gives you 80% of the value with 20% of the effort.

**7. The best model is the one you can deploy, monitor, and maintain.** A scikit-learn model
that is in production with full observability is worth more than a state-of-the-art
Transformer that runs in a notebook.

---

## Architecture Reference

For reference, here is the complete data flow through the pipeline:

```
Reddit API
    |
    v
Python Producer (polls every 5 min)
    |  - Cleans text (URL removal, stopwords, lemmatization)
    |  - Injects OTel trace context into Kafka headers
    |  - Exponential backoff on API errors
    |  - File-based DLQ for Kafka send failures
    |
    v
Kafka Topic: reddit-stream
    |  - Schema: {id, content}
    |  - Validated by Apicurio Registry
    |
    v
Spark Structured Streaming (1-min trigger, maxOffsets=100)
    |  - Reads from Kafka
    |  - Parses JSON into DataFrame
    |  - Applies dual_model_prediction UDF:
    |      - DistilBERT Transformer (CPU, ~200ms/post)
    |      - scikit-learn TF-IDF/LSA (~3ms/post)
    |      - OTel spans per model
    |  - Routes failures to DLQ
    |
    +---> Kafka Topic: kafka-predictions
    |         |  - Schema: {id, title, content,
    |         |     transformer_flair, transformer_confidence,
    |         |     sklearn_flair, sklearn_confidence}
    |         |
    |         v
    |     Quarkus Consumer
    |         - Tracks per-flair statistics
    |         - Computes agreement rate
    |         - Builds confusion matrix
    |         - Exposes Prometheus metrics
    |         - DLQ for consumption failures
    |         |
    |         v
    |     Prometheus --> Grafana Dashboards
    |
    +---> Kafka Topic: reddit-inference-dlq
              - Failed inference records
              - Original content + error reason
              - 7-day retention

OTel Traces --> Jaeger
    - Producer spans
    - Spark inference spans (correlated by reddit.post.id)
    - Consumer spans (auto-instrumented by Quarkus)
```

---

## Closing Thoughts

The ML inference spectrum is not a technology choice -- it is a set of engineering tradeoffs
that depend on your latency requirements, your team's skills, your budget, and your
tolerance for operational complexity.

For this pipeline, micro-batch with Spark Structured Streaming was the right choice. Not
because it is the most technically impressive option. Because it is the one we can build,
deploy, monitor, debug, and maintain with the resources we have.

The dual-model approach is not a luxury -- it is the only practical way to get a reliability
signal when you have no ground truth. The agreement rate, confidence distributions, and
per-flair statistics give us enough signal to detect model drift, identify hard categories,
and decide when the simpler model is sufficient.

The observability stack (OTel, Prometheus, Grafana, Jaeger) is not overhead -- it is what
turns a pipeline into a production system. Without it, you are shipping predictions into
the void and hoping they are correct.

Every architectural decision in this pipeline was made under constraints: no GPUs, limited
memory, a small team, and a data source that does not demand real-time processing. Different
constraints would lead to different decisions. The framework matters more than the specific
choices.

Build for the constraints you have, not the scale you might need. Move left on the inference
spectrum only when the use case demands it and your team can handle the operational
consequences.

---

*This article is part of the Distributed Deep Dives series, where we explore the
engineering decisions behind real distributed systems -- with real code, real tradeoffs,
and no hand-waving.*
