"""
Dual-Model Prediction UDF for Spark Structured Streaming
=========================================================

This is a simplified, annotated version of the dual_model_prediction UDF used
in the Reddit flair classification pipeline.  It shows the pattern for running
two models (DistilBERT Transformer + scikit-learn TF-IDF/LSA) side-by-side
inside a single PySpark UDF, with OpenTelemetry tracing and DLQ routing.

Key engineering decisions embedded in this code:
  1. Models are loaded at module level (not inside the UDF) because Transformer
     models are not picklable and cannot be serialized to Spark executors.
  2. torch.no_grad() is mandatory -- without it, PyTorch builds computation
     graphs that OOM the executor on our 3GB memory budget.
  3. The UDF never raises exceptions. Failures are returned as DLQ-flagged
     JSON records so that a single bad input does not fail the entire micro-batch.
  4. Each model gets its own OTel span for independent latency tracking.
  5. Trace correlation is done via the reddit.post.id attribute because Spark
     UDFs do not inherit the driver's OTel trace context.

Usage:
  This file is for educational purposes.  The production version lives in
  runtime/spark/reddit_flair_spark_inference.py
"""

import os
import json
import joblib
import pickle
import torch

from pyspark.sql.functions import udf, struct
from pyspark.sql.types import StringType

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource

# ---------------------------------------------------------------------------
# OpenTelemetry Setup
# ---------------------------------------------------------------------------
# The tracer is initialized once at module level.  Spans created inside the
# UDF will be exported to Jaeger via OTLP/gRPC.
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
# Model Loading (Module Level)
# ---------------------------------------------------------------------------
# WHY MODULE LEVEL?
# PySpark serializes UDF closures with pickle.  Hugging Face Transformer
# models contain non-picklable objects (CUDA handles, compiled C extensions).
# Loading at module level avoids serialization: the models exist in the
# Python process that runs the UDF, not in a serialized closure.
#
# CONSTRAINT: This only works with a single executor.  For multi-executor
# deployments, use mapPartitions() to load models per-partition.

from transformers import AutoTokenizer, AutoModelForSequenceClassification

MODEL_DIR = "/opt/spark/models/reddit_flairs"

# Transformer model -- DistilBERT fine-tuned on r/AskEurope posts
transformer_model = AutoModelForSequenceClassification.from_pretrained(MODEL_DIR)
tokenizer = AutoTokenizer.from_pretrained(
    MODEL_DIR,
    local_files_only=True,
    use_fast=False  # Fast tokenizer has Rust objects that are even harder to pickle
)
label_encoder = joblib.load(os.path.join(MODEL_DIR, "reddit_flair_label_encoder.joblib"))

transformer_model.to("cpu")
transformer_model.eval()  # Disable dropout for deterministic inference

# scikit-learn pipeline -- TF-IDF + Truncated SVD (LSA) + classifier
with open("vectorizer.pkl", "rb") as f:
    tfidf = pickle.load(f)
with open("LSA_topics.pkl", "rb") as f:
    tsvd = pickle.load(f)
with open("reddit_classifier.pkl", "rb") as f:
    classifier = pickle.load(f)

# The 13 flair categories for r/AskEurope
flairs = label_encoder.classes_.tolist()

# DLQ topic name for failed inference records
DLQ_TOPIC = "reddit-inference-dlq"


# ---------------------------------------------------------------------------
# The Dual-Model Prediction UDF
# ---------------------------------------------------------------------------
def dual_model_prediction(row):
    """
    Run both models on a single Reddit post and return a JSON string with
    predictions from both.  If inference fails, return a DLQ-flagged record.

    Input row fields:
      - id:      Reddit post ID
      - title:   Post title (included in output for downstream consumers)
      - content: Cleaned post text (title + body + comments + domain)

    Output (success):
      {
        "id": "abc123",
        "title": "What is your favorite food?",
        "content": "...",
        "transformer_flair": "Food",
        "transformer_confidence": 0.87,
        "sklearn_flair": "Food",
        "sklearn_confidence": 0.72
      }

    Output (failure -- routed to DLQ):
      {
        "__dlq": true,
        "id": "abc123",
        "title": "...",
        "content": "...",
        "error": "RuntimeError: CUDA error: ..."
      }
    """
    content = row["content"]
    post_id = row["id"] or "unknown"

    try:
        # Default values for empty/missing content
        transformer_flair = "Unknown"
        transformer_conf = 0.0
        sklearn_flair = "Unknown"
        sklearn_conf = 0.0

        if content and content.strip():
            # Parent span for the entire dual-model inference
            # The reddit.post.id attribute enables correlation across services
            # in Jaeger (producer -> spark -> consumer spans are linked by post ID)
            with tracer.start_as_current_span(
                "dual-model-inference",
                attributes={"reddit.post.id": post_id},
            ) as span:

                # ----- Transformer Inference -----
                # Wrapped in its own span so we can track latency independently.
                # Typical latency: ~200ms on CPU for a medium-length post.
                with tracer.start_as_current_span("transformer-inference"):
                    # Tokenize with truncation at 512 tokens (DistilBERT max)
                    inputs = tokenizer(
                        content,
                        return_tensors="pt",
                        truncation=True,
                        padding=True,
                        max_length=512,
                    )
                    # Explicit CPU placement (no GPU in the cluster)
                    inputs = {k: v.to("cpu") for k, v in inputs.items()}

                    # torch.no_grad() is MANDATORY here.
                    # Without it, PyTorch builds a computation graph for
                    # backpropagation, allocating memory proportional to
                    # model size * input length.  On our 3GB executor,
                    # this causes OOM kills on long posts.
                    with torch.no_grad():
                        outputs = transformer_model(**inputs)

                    # Convert logits to probabilities
                    probs = torch.nn.functional.softmax(outputs.logits, dim=-1)
                    pred_class = torch.argmax(probs, dim=1).item()
                    transformer_flair = flairs[pred_class]
                    transformer_conf = float(probs[0][pred_class])

                # ----- scikit-learn Inference -----
                # Wrapped in its own span.  Typical latency: ~3ms.
                # The 60x speed difference vs. the Transformer is why we keep
                # sklearn even when the Transformer is "better" -- it provides
                # a fast cross-check.
                with tracer.start_as_current_span("sklearn-inference"):
                    X = tfidf.transform([content])       # TF-IDF vectorization
                    X_reduced = tsvd.transform(X)        # LSA dimensionality reduction
                    sk_probs = classifier.predict_proba(X_reduced)[0]
                    sk_class = sk_probs.argmax()
                    sklearn_flair = flairs[sk_class]
                    sklearn_conf = float(sk_probs[sk_class])

                # Attach prediction details to the parent span for Jaeger queries
                span.set_attribute("prediction.transformer_flair", transformer_flair)
                span.set_attribute("prediction.transformer_confidence", transformer_conf)
                span.set_attribute("prediction.sklearn_flair", sklearn_flair)
                span.set_attribute("prediction.sklearn_confidence", sklearn_conf)
                span.set_attribute(
                    "prediction.models_agree",
                    transformer_flair == sklearn_flair,
                )

        # Return successful prediction as JSON
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
        # CRITICAL: Do NOT raise the exception.
        # A raised exception in a UDF fails the entire micro-batch (all 100
        # records).  Instead, return a DLQ-flagged record.  The streaming
        # query will route it to the reddit-inference-dlq Kafka topic.
        return json.dumps({
            "__dlq": True,
            "id": post_id,
            "title": row["title"],
            "content": content,
            "error": str(e),
        })


# Register as a PySpark UDF
# The return type is StringType because we serialize the output as JSON.
# An alternative is to return a StructType, but that makes DLQ routing harder
# (the DLQ record has different fields than the success record).
predict_udf = udf(dual_model_prediction, StringType())


# ---------------------------------------------------------------------------
# Usage in the Streaming Query
# ---------------------------------------------------------------------------
# predicted_df = parsed_df.withColumn(
#     "value",
#     predict_udf(struct("id", "title", "content"))
# )
#
# # Split successful predictions from DLQ records
# from pyspark.sql.functions import get_json_object, col
# is_dlq = get_json_object(col("value"), "$.__dlq") == "true"
# success_df = predicted_df.filter(~is_dlq)
# dlq_df = predicted_df.filter(is_dlq)
