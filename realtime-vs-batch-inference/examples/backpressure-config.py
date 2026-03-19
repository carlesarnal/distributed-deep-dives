"""
Backpressure and Rate Control Configuration for Spark Structured Streaming
==========================================================================

This file shows the complete Spark Structured Streaming configuration used
in the Reddit flair classification pipeline, with detailed annotations on
how each parameter contributes to backpressure control and memory safety.

The core insight: in this pipeline, maxOffsetsPerTrigger is not a "rate
limiter" -- it is a MEMORY SAFETY VALVE that prevents OOM kills on the
Spark executor.

Key parameters:
  - maxOffsetsPerTrigger=100  : bounds peak memory usage per micro-batch
  - trigger(processingTime="1 minute") : sets processing cadence
  - failOnDataLoss=false      : survives Kafka topic compaction/deletion
  - startingOffsets=earliest  : resumes from beginning on first start
  - checkpointLocation        : enables exactly-once fault tolerance

Usage:
  This file is for educational purposes.  The production version lives in
  runtime/spark/reddit_flair_spark_inference.py
"""

from pyspark.sql import SparkSession
from pyspark.sql.functions import col, from_json, get_json_object
from pyspark.sql.types import StructType, StringType

# ---------------------------------------------------------------------------
# Spark Session Configuration
# ---------------------------------------------------------------------------
spark = SparkSession.builder \
    .appName("RedditDualModelInference") \
    .config("spark.sql.shuffle.partitions", "2") \
    .config("spark.executor.instances", "2") \
    .getOrCreate()

# spark.sql.shuffle.partitions = 2
#   Default is 200, which is wildly excessive for a micro-batch of 100
#   records.  Each shuffle partition creates a task, and each task has
#   scheduling overhead.  2 partitions match our 2 executor cores.
#
# spark.executor.instances = 2
#   In practice, our SparkApplication YAML overrides this to 1 because
#   the cluster is resource-constrained.  Set this based on available
#   resources, not desired parallelism.


# ---------------------------------------------------------------------------
# Kafka Source Configuration
# ---------------------------------------------------------------------------
kafka_bootstrap_servers = "reddit-posts-kafka-bootstrap.reddit-realtime.svc:9093"

df = spark.readStream \
    .format("kafka") \
    .option("kafka.bootstrap.servers", kafka_bootstrap_servers) \
    .option("subscribe", "reddit-stream") \
    .option("startingOffsets", "earliest") \
    .option("failOnDataLoss", "false") \
    .option("maxOffsetsPerTrigger", "100") \
    .load()

# Detailed parameter breakdown:
#
# subscribe = "reddit-stream"
#   Subscribe to a single topic.  For multiple topics, use a comma-separated
#   list or subscribePattern with a regex.
#
# startingOffsets = "earliest"
#   On the FIRST start (no checkpoint), begin reading from the earliest
#   available offset in Kafka.  On subsequent starts, the checkpoint
#   contains the last committed offset, so this parameter is ignored.
#
#   Alternative: "latest" -- skip all historical messages and only process
#   new ones.  Use this if you don't need to backfill.
#
# failOnDataLoss = "false"
#   If Kafka offsets in the checkpoint no longer exist in Kafka (because
#   the retention period expired and messages were deleted), Spark will
#   log a warning and continue from the next available offset instead of
#   failing the query.
#
#   Set this to "true" in pipelines where data loss is unacceptable and
#   you have alerting on Kafka retention/lag.
#
# maxOffsetsPerTrigger = 100
#   THIS IS THE MOST IMPORTANT PARAMETER.
#
#   It caps the number of Kafka messages read per trigger interval.
#   Without it, a single micro-batch could read ALL unconsumed messages.
#   If 10,000 messages accumulated overnight, Spark would try to process
#   all 10,000 in one batch, calling the dual_model_prediction UDF 10,000
#   times.
#
#   Memory impact per post:
#     - Transformer tokenization:  ~1 MB (input tensors for 512 tokens)
#     - Transformer forward pass:  ~50 MB peak (temporary activations)
#     - Transformer output:        ~0.1 MB (logits + softmax)
#     - sklearn vectorization:     ~0.5 MB (sparse TF-IDF matrix)
#     - sklearn prediction:        ~0.1 MB
#     - Total peak per post:       ~52 MB
#
#   With maxOffsetsPerTrigger=100:
#     Peak memory (sequential UDF): ~52 MB  (one post at a time)
#     Peak memory (parallel UDFs):  ~52 MB * parallelism
#
#   With maxOffsetsPerTrigger=10000:
#     Even with sequential execution, the DataFrame holding 10,000 rows
#     of post content consumes significant memory before UDF execution.
#
#   Our executor has 3GB RAM.  100 is a conservative limit that leaves
#   ample headroom for JVM overhead, PySpark worker, and model weights.
#
#   HOW TO TUNE THIS:
#     1. Start with a low value (50-100)
#     2. Monitor executor memory usage in Spark UI
#     3. Increase gradually until you see memory pressure
#     4. Set the production value at 70% of the max that didn't OOM
#     5. Never set it higher than what your executor can handle in
#        the worst case (all posts at max token length)


# ---------------------------------------------------------------------------
# Schema and Parsing
# ---------------------------------------------------------------------------
schema = StructType() \
    .add("id", StringType()) \
    .add("title", StringType()) \
    .add("content", StringType())

parsed_df = df.select(
    from_json(col("value").cast("string"), schema).alias("data")
).select("data.*")


# ---------------------------------------------------------------------------
# Inference (placeholder -- see dual-model-udf.py for the real UDF)
# ---------------------------------------------------------------------------
# predicted_df = parsed_df.withColumn(
#     "value",
#     predict_udf(struct("id", "title", "content"))
# )
predicted_df = parsed_df  # placeholder


# ---------------------------------------------------------------------------
# DLQ Routing
# ---------------------------------------------------------------------------
# Split successful predictions from DLQ records based on the __dlq flag.
# This pattern keeps the UDF simple (it never throws) and lets Spark's
# streaming engine handle the routing.

DLQ_TOPIC = "reddit-inference-dlq"

is_dlq = get_json_object(col("value"), "$.__dlq") == "true"
success_df = predicted_df.filter(~is_dlq)
dlq_df = predicted_df.filter(is_dlq)


# ---------------------------------------------------------------------------
# Kafka Sink -- Successful Predictions
# ---------------------------------------------------------------------------
query_success = success_df.select("value") \
    .writeStream \
    .format("kafka") \
    .option("kafka.bootstrap.servers", kafka_bootstrap_servers) \
    .option("topic", "kafka-predictions") \
    .option("checkpointLocation", "/tmp/spark-checkpoints/predictions") \
    .trigger(processingTime="1 minute") \
    .start()

# trigger(processingTime="1 minute")
#   Start a new micro-batch every 60 seconds.  If the previous batch is
#   still running, the next trigger waits until it finishes.
#
#   This means:
#     - Best case: 1 batch per minute, each processing up to 100 messages
#     - Steady state: ~100 messages/minute throughput (if that many exist)
#     - Backpressure case: if inference takes > 60s for 100 messages,
#       throughput drops and Kafka lag increases.  But the system stays
#       stable -- no OOM, no cascading failure.
#
#   Alternative trigger modes:
#     .trigger(once=True)             -- process all available data, then stop
#     .trigger(continuous="1 second") -- experimental continuous processing
#     .trigger(availableNow=True)     -- like once=True but with rate limiting
#
# checkpointLocation = "/tmp/spark-checkpoints/predictions"
#   Stores Kafka offsets and query metadata for fault tolerance.
#
#   CHECKPOINT TRAP:
#   If you change the UDF output schema (add/remove fields), Spark will
#   fail on restart because the checkpoint contains the old schema.
#   The fix: delete the checkpoint directory and restart.
#   This means reprocessing from startingOffsets, which is "earliest" in
#   our case -- all messages get re-processed.
#
#   For production pipelines where reprocessing is expensive:
#     - Maintain backward-compatible output schemas
#     - Or write a checkpoint migration tool


# ---------------------------------------------------------------------------
# Kafka Sink -- DLQ for Failed Inference
# ---------------------------------------------------------------------------
query_dlq = dlq_df.select("value") \
    .writeStream \
    .format("kafka") \
    .option("kafka.bootstrap.servers", kafka_bootstrap_servers) \
    .option("topic", DLQ_TOPIC) \
    .option("checkpointLocation", "/tmp/spark-checkpoints/dlq") \
    .trigger(processingTime="1 minute") \
    .start()

# The DLQ sink has its own checkpoint.  This is important: if the main
# predictions sink fails (e.g., Kafka topic for predictions is unavailable),
# the DLQ sink can still operate independently.  They are separate streaming
# queries with separate fault tolerance.


# ---------------------------------------------------------------------------
# Await Termination
# ---------------------------------------------------------------------------
spark.streams.awaitAnyTermination()

# awaitAnyTermination() blocks until any streaming query terminates (due to
# error or explicit stop).  In a production deployment, you would want to
# handle the termination gracefully -- log the reason, alert, and potentially
# restart.
#
# Alternative: awaitTermination() on a specific query blocks until that
# query terminates.  With two sinks, awaitAnyTermination() is more
# appropriate because you want to know if either one fails.


# ---------------------------------------------------------------------------
# Backpressure Behavior Summary
# ---------------------------------------------------------------------------
#
# Scenario 1: Normal operation
#   - Kafka has 50 new messages
#   - Trigger fires, reads 50 messages (< maxOffsetsPerTrigger)
#   - Inference takes 15 seconds
#   - Results written to Kafka
#   - Waits 45 seconds for next trigger
#
# Scenario 2: Burst of data
#   - Kafka has 5,000 new messages (overnight backlog)
#   - Trigger fires, reads 100 messages (= maxOffsetsPerTrigger)
#   - Inference takes 30 seconds
#   - Next trigger reads another 100
#   - Pipeline works through the backlog at ~100 messages/minute
#   - Kafka retains the remaining messages safely (7-day retention)
#
# Scenario 3: Slow inference
#   - 100 messages include unusually long posts
#   - Transformer inference takes 500ms per post instead of 200ms
#   - Total batch time: 50 seconds (still < 1 minute trigger)
#   - System stays healthy, just uses more CPU time
#
# Scenario 4: Very slow inference (backpressure kicks in)
#   - 100 messages, each at max token length (512 tokens)
#   - Transformer inference takes 500ms * 100 = 50 seconds
#   - But DataFrame operations add overhead, total = 70 seconds
#   - Next trigger can't start until current batch finishes
#   - Effective trigger interval becomes 70 seconds instead of 60
#   - Kafka lag increases by ~17% per cycle
#   - System self-heals when shorter posts return to normal latency
#
# Scenario 5: Executor OOM (without maxOffsetsPerTrigger)
#   - 10,000 messages in Kafka
#   - No maxOffsetsPerTrigger -- reads all 10,000
#   - DataFrame with 10,000 text records: ~500 MB
#   - UDF executions: even sequential, temporary tensors accumulate
#   - Executor hits 4GB Kubernetes limit, gets OOM-killed
#   - Spark restarts executor, retries the same batch, OOM again
#   - Death spiral until manual intervention
#
# maxOffsetsPerTrigger=100 prevents Scenario 5.  That's its job.
