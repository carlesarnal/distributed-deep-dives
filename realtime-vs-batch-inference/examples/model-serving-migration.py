"""
Model Serving Migration: Inline Inference -> HTTP Endpoint
==========================================================

This file shows the hypothetical refactor for scaling the Reddit flair
classification pipeline to 100x throughput.  The key change: move the
Transformer model from inline Spark UDF execution to a dedicated model
serving endpoint (NVIDIA Triton Inference Server).

The scikit-learn model stays inline because it is fast enough (~3ms)
that the overhead of an HTTP call would be slower than the inference itself.

Before (current architecture):
  Spark UDF -> loads DistilBERT in-process -> runs on CPU -> ~200ms/post

After (scaled architecture):
  Spark UDF -> HTTP POST to Triton -> runs on GPU -> ~5ms/post
  Spark UDF -> inline sklearn (unchanged) -> runs on CPU -> ~3ms/post

Benefits:
  - GPU acceleration for the Transformer (40x speedup)
  - Independent scaling of model serving and stream processing
  - Model versioning via Triton config, not Docker image rebuilds
  - Dynamic batching in Triton (groups concurrent requests)

Tradeoffs:
  - Network hop adds ~2ms latency (negligible vs. 200ms savings)
  - New failure mode: Triton endpoint unavailable
  - Need to manage Triton infrastructure (GPU nodes, model repository)
  - HTTP serialization overhead for large inputs

Usage:
  This file is for educational purposes.  It demonstrates the migration
  pattern, not production-ready code.
"""

import os
import json
import pickle
import requests

from pyspark.sql import SparkSession
from pyspark.sql.functions import col, from_json, udf, struct, get_json_object
from pyspark.sql.types import StructType, StringType

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource

# ---------------------------------------------------------------------------
# OpenTelemetry Setup (same as before)
# ---------------------------------------------------------------------------
resource = Resource.create({"service.name": "spark-inference"})
provider = TracerProvider(resource=resource)
otlp_endpoint = os.environ.get(
    "OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger.reddit-realtime.svc:4317"
)
exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
provider.add_span_processor(BatchSpanProcessor(exporter))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("spark-inference")

# ---------------------------------------------------------------------------
# Triton Inference Server Configuration
# ---------------------------------------------------------------------------
# Triton runs on a GPU node as a Kubernetes Deployment + Service.
# The model repository is mounted from a PersistentVolume or object storage.
#
# Deployment topology:
#   spark-inference (CPU pods) --HTTP--> triton (GPU pod, port 8000)
#
# Triton model config (models/distilbert-reddit/config.pbtxt):
#   name: "distilbert-reddit"
#   platform: "pytorch_libtorch"  # or "onnxruntime_onnx" for ONNX export
#   max_batch_size: 32
#   dynamic_batching {
#     preferred_batch_size: [ 8, 16 ]
#     max_queue_delay_microseconds: 50000  # 50ms -- wait to fill a batch
#   }
#   input [
#     { name: "text", data_type: TYPE_STRING, dims: [ 1 ] }
#   ]
#   output [
#     { name: "predicted_class", data_type: TYPE_INT32, dims: [ 1 ] }
#     { name: "confidence", data_type: TYPE_FP32, dims: [ 1 ] }
#   ]

TRITON_URL = os.environ.get(
    "TRITON_URL",
    "http://triton.model-serving.svc:8000/v2/models/distilbert-reddit/infer",
)
TRITON_TIMEOUT_SECONDS = 5.0
TRITON_MAX_RETRIES = 2

# ---------------------------------------------------------------------------
# scikit-learn Model (still loaded inline -- it's fast enough)
# ---------------------------------------------------------------------------
with open("vectorizer.pkl", "rb") as f:
    tfidf = pickle.load(f)
with open("LSA_topics.pkl", "rb") as f:
    tsvd = pickle.load(f)
with open("reddit_classifier.pkl", "rb") as f:
    classifier = pickle.load(f)

# The 13 flair categories
flairs = [
    "Work", "Misc", "Food", "Personal", "Meta", "Sports", "Travel",
    "Politics", "Culture", "History", "Education", "Language", "Foreign",
]

DLQ_TOPIC = "reddit-inference-dlq"


# ---------------------------------------------------------------------------
# Remote Transformer Inference via Triton
# ---------------------------------------------------------------------------
def transformer_inference_remote(content, post_id):
    """
    Call the Triton Inference Server to get the Transformer prediction.

    Triton handles:
      - Tokenization (via a custom Python backend or preprocessing step)
      - GPU inference on DistilBERT
      - Dynamic batching (groups concurrent requests for throughput)

    Returns:
      (flair_label: str, confidence: float) on success
      ("Unknown", 0.0) on failure (with error logged in the span)
    """
    # Triton V2 Inference Protocol payload
    # See: https://github.com/kserve/kserve/blob/master/docs/predict-api/v2
    payload = {
        "inputs": [
            {
                "name": "text",
                "shape": [1],
                "datatype": "BYTES",
                "data": [content],
            }
        ],
        "outputs": [
            {"name": "predicted_class"},
            {"name": "confidence"},
        ],
    }

    last_error = None
    for attempt in range(1, TRITON_MAX_RETRIES + 1):
        try:
            response = requests.post(
                TRITON_URL,
                json=payload,
                timeout=TRITON_TIMEOUT_SECONDS,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()

            result = response.json()
            predicted_class = int(result["outputs"][0]["data"][0])
            confidence = float(result["outputs"][1]["data"][0])

            return flairs[predicted_class], confidence

        except requests.exceptions.Timeout:
            last_error = f"Triton timeout after {TRITON_TIMEOUT_SECONDS}s (attempt {attempt})"
        except requests.exceptions.ConnectionError:
            last_error = f"Triton connection refused (attempt {attempt})"
        except requests.exceptions.HTTPError as e:
            last_error = f"Triton HTTP error: {e.response.status_code} (attempt {attempt})"
            # Don't retry on 4xx errors -- the request itself is bad
            if e.response.status_code < 500:
                break
        except (KeyError, IndexError, ValueError) as e:
            last_error = f"Triton response parse error: {e}"
            break  # Don't retry parse errors

    # All retries exhausted or non-retryable error
    # Log the error in the current span and return defaults
    current_span = trace.get_current_span()
    current_span.set_attribute("transformer.error", last_error)
    current_span.set_attribute("transformer.fallback", True)

    return "Unknown", 0.0


# ---------------------------------------------------------------------------
# scikit-learn Inference (unchanged -- runs inline)
# ---------------------------------------------------------------------------
def sklearn_inference_local(content):
    """
    Run the scikit-learn TF-IDF/LSA pipeline locally.

    This stays inline because:
      1. It runs in ~3ms (the HTTP overhead would be longer)
      2. It's CPU-only and uses minimal memory
      3. It's serializable (pure Python/NumPy objects)
      4. It serves as a fast fallback when Triton is unavailable
    """
    X = tfidf.transform([content])
    X_reduced = tsvd.transform(X)
    sk_probs = classifier.predict_proba(X_reduced)[0]
    sk_class = sk_probs.argmax()
    return flairs[sk_class], float(sk_probs[sk_class])


# ---------------------------------------------------------------------------
# Migrated Dual-Model UDF
# ---------------------------------------------------------------------------
def dual_model_prediction_migrated(row):
    """
    Same interface as the original UDF, but the Transformer inference
    is delegated to Triton via HTTP instead of running inline.

    Changes from the original:
      - No torch import, no model loading, no tokenizer
      - Transformer inference is an HTTP call to Triton
      - scikit-learn inference is unchanged (still inline)
      - DLQ pattern is unchanged
      - OTel tracing is unchanged (spans still created per model)
      - New: Triton failure is handled gracefully (returns "Unknown")
        instead of failing the UDF

    Memory impact:
      Before: ~260 MB (DistilBERT) + ~50 MB (tokenizer) + ~200 MB (PyTorch)
      After:  ~30 MB (sklearn pipeline) + ~10 MB (requests library)
      Savings: ~470 MB per executor -- enough to increase maxOffsetsPerTrigger
    """
    content = row["content"]
    post_id = row["id"] or "unknown"

    try:
        transformer_flair = "Unknown"
        transformer_conf = 0.0
        sklearn_flair = "Unknown"
        sklearn_conf = 0.0

        if content and content.strip():
            with tracer.start_as_current_span(
                "dual-model-inference",
                attributes={"reddit.post.id": post_id},
            ) as span:

                # Transformer inference via Triton (GPU-accelerated)
                with tracer.start_as_current_span(
                    "transformer-inference",
                    attributes={"inference.mode": "remote", "inference.endpoint": "triton"},
                ):
                    transformer_flair, transformer_conf = transformer_inference_remote(
                        content, post_id
                    )

                # scikit-learn inference (inline, unchanged)
                with tracer.start_as_current_span(
                    "sklearn-inference",
                    attributes={"inference.mode": "local"},
                ):
                    sklearn_flair, sklearn_conf = sklearn_inference_local(content)

                span.set_attribute("prediction.transformer_flair", transformer_flair)
                span.set_attribute("prediction.transformer_confidence", transformer_conf)
                span.set_attribute("prediction.sklearn_flair", sklearn_flair)
                span.set_attribute("prediction.sklearn_confidence", sklearn_conf)
                span.set_attribute(
                    "prediction.models_agree",
                    transformer_flair == sklearn_flair,
                )

        return json.dumps({
            "id": post_id,
            "title": row["title"],
            "content": content,
            "transformer_flair": transformer_flair,
            "transformer_confidence": transformer_conf,
            "sklearn_flair": sklearn_flair,
            "sklearn_confidence": sklearn_conf,
        })

    except Exception as e:
        return json.dumps({
            "__dlq": True,
            "id": post_id,
            "title": row["title"],
            "content": content,
            "error": str(e),
        })


predict_udf = udf(dual_model_prediction_migrated, StringType())


# ---------------------------------------------------------------------------
# Streaming Query (same structure, different configs for scale)
# ---------------------------------------------------------------------------
spark = SparkSession.builder \
    .appName("RedditDualModelInference-Scaled") \
    .config("spark.sql.shuffle.partitions", "8") \
    .config("spark.executor.instances", "4") \
    .getOrCreate()

kafka_bootstrap_servers = "reddit-posts-kafka-bootstrap.reddit-realtime.svc:9093"

df = spark.readStream \
    .format("kafka") \
    .option("kafka.bootstrap.servers", kafka_bootstrap_servers) \
    .option("subscribe", "reddit-stream") \
    .option("startingOffsets", "earliest") \
    .option("failOnDataLoss", "false") \
    .option("maxOffsetsPerTrigger", "1000") \
    .load()

# maxOffsetsPerTrigger increased from 100 to 1000
# WHY: Without the Transformer model in memory (~470 MB savings), each
# executor has much more headroom.  And the Transformer inference is
# now an HTTP call (~2ms) instead of a local forward pass (~200ms),
# so 1000 posts can be processed in the same wall-clock time that
# 100 posts took before.

schema = StructType() \
    .add("id", StringType()) \
    .add("title", StringType()) \
    .add("content", StringType())

parsed_df = df.select(
    from_json(col("value").cast("string"), schema).alias("data")
).select("data.*")

predicted_df = parsed_df.withColumn(
    "value",
    predict_udf(struct("id", "title", "content")),
)

is_dlq = get_json_object(col("value"), "$.__dlq") == "true"
success_df = predicted_df.filter(~is_dlq)
dlq_df = predicted_df.filter(is_dlq)

# Trigger interval reduced from 1 minute to 15 seconds
# WHY: With faster Transformer inference (HTTP to GPU vs. local CPU),
# each batch completes much faster, so we can trigger more frequently
# without backpressure.
query_success = success_df.select("value") \
    .writeStream \
    .format("kafka") \
    .option("kafka.bootstrap.servers", kafka_bootstrap_servers) \
    .option("topic", "kafka-predictions") \
    .option("checkpointLocation", "/tmp/spark-checkpoints/predictions") \
    .trigger(processingTime="15 seconds") \
    .start()

query_dlq = dlq_df.select("value") \
    .writeStream \
    .format("kafka") \
    .option("kafka.bootstrap.servers", kafka_bootstrap_servers) \
    .option("topic", "reddit-inference-dlq") \
    .option("checkpointLocation", "/tmp/spark-checkpoints/dlq") \
    .trigger(processingTime="15 seconds") \
    .start()

spark.streams.awaitAnyTermination()


# ---------------------------------------------------------------------------
# Migration Checklist
# ---------------------------------------------------------------------------
#
# 1. Export the DistilBERT model to ONNX or TorchScript for Triton:
#      python -m transformers.onnx --model=reddit_flair_classifier onnx/
#
# 2. Create the Triton model repository:
#      models/
#        distilbert-reddit/
#          config.pbtxt
#          1/            <-- version 1
#            model.onnx
#
# 3. Deploy Triton on a GPU node:
#      kubectl apply -f triton-deployment.yaml
#      (Ensure GPU resource requests in the pod spec)
#
# 4. Verify Triton is serving:
#      curl http://triton:8000/v2/health/ready
#      curl http://triton:8000/v2/models/distilbert-reddit
#
# 5. Update the Spark Docker image:
#      - Remove: torch, transformers, huggingface_hub (saves ~2GB image size)
#      - Add: requests (if not already present)
#      - Remove: model weight files from /opt/spark/models/
#
# 6. Update SparkApplication resource requests:
#      executor.memory: 3g -> 1g  (no longer need Transformer in memory)
#      executor.instances: 1 -> 4  (can run more executors with less memory)
#
# 7. Delete Spark checkpoints (schema hasn't changed, but clean start
#    is recommended for major architecture changes):
#      rm -rf /tmp/spark-checkpoints/*
#
# 8. Deploy and monitor:
#      - Watch Triton metrics (inference latency, queue depth, GPU utilization)
#      - Watch Spark metrics (batch duration, processing rate)
#      - Compare agreement rates before/after migration (should be identical)
#      - Verify OTel spans include inference.mode=remote attribute
#
# 9. Model versioning workflow (post-migration):
#      Before: Build new Docker image -> restart Spark job
#      After:  Copy new model to Triton repository -> Triton hot-reloads
#              (Spark job keeps running, no restart needed)
#
# ---------------------------------------------------------------------------
# Cost Comparison (approximate, AWS us-east-1, on-demand)
# ---------------------------------------------------------------------------
#
# BEFORE (CPU-only, inline Transformer):
#   Spark driver:    c6i.large  (2 vCPU, 4GB)   = $62/month
#   Spark executor:  c6i.large  (2 vCPU, 4GB)   = $62/month
#   Total compute:                                 $124/month
#   Throughput:      ~100 posts/minute
#   Per-post cost:   $0.000029
#
# AFTER (GPU for Transformer, CPU for Spark + sklearn):
#   Spark driver:    c6i.medium (1 vCPU, 2GB)   = $31/month
#   Spark executors: c6i.medium x4               = $124/month
#   Triton (GPU):    g4dn.xlarge (T4, 16GB)      = $380/month
#   Total compute:                                 $535/month
#   Throughput:      ~10,000 posts/minute
#   Per-post cost:   $0.0000012
#
# The per-post cost drops by 24x while throughput increases by 100x.
# The absolute cost increases by 4.3x, but the cost EFFICIENCY improves
# dramatically.  This is the classic scaling tradeoff: you pay more in
# total but less per unit of work.
