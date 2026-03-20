# Model Agreement as a Proxy for Ground Truth in Streaming ML

*How to monitor ML predictions in a streaming pipeline when you don't have labels — using dual-model agreement, disagreement taxonomy, confidence calibration, and drift detection.*

---

## Table of Contents

1. [The Ground Truth Problem in Streaming](#the-ground-truth-problem-in-streaming)
2. [Why Two Models](#why-two-models)
3. [Agreement Rate: What It Tells You](#agreement-rate-what-it-tells-you)
4. [Disagreement Taxonomy](#disagreement-taxonomy)
5. [Per-Category Analysis](#per-category-analysis)
6. [Confidence Calibration](#confidence-calibration)
7. [Detecting Model Drift Without Labels](#detecting-model-drift-without-labels)
8. [When to Stop Running Both Models](#when-to-stop-running-both-models)
9. [Beyond Classification: Generalizing the Pattern](#beyond-classification-generalizing-the-pattern)

---

## The Ground Truth Problem in Streaming

In batch ML, your workflow looks like this:

1. Train a model on labeled data
2. Evaluate on a held-out test set
3. Compute accuracy, precision, recall, F1
4. Deploy if metrics meet threshold
5. Periodically re-evaluate on new labeled data

Step 5 is where things break in streaming. In a real-time pipeline, messages arrive continuously. There's no test set. There's no labeling step. A Reddit post enters the pipeline, gets classified, and is consumed downstream — and nobody tells you whether the prediction was correct.

Traditional ML metrics require labels:

```
Accuracy = correct predictions / total predictions
```

If you don't know which predictions are correct, you can't compute accuracy. Period.

This is the situation in the [reddit-realtime-classification](https://github.com/carlesarnal/reddit-realtime-classification) pipeline. Posts from r/AskEurope flow through Kafka → Spark → Quarkus consumer, getting classified into 13 flair categories. The models were trained on historical data with known flairs, but at inference time, we're predicting flairs for posts that may not have one yet, or whose flair was assigned by the poster (and may be wrong).

So how do you know your model is working?

You can't know with certainty. But you can build **proxies** — signals that correlate with prediction quality without requiring ground truth. The most powerful proxy I've found is **model agreement**.

---

## Why Two Models

The pipeline runs two fundamentally different models on every post:

| Model | Architecture | Strengths | Weaknesses |
|-------|-------------|-----------|------------|
| **DistilBERT** (Transformer) | Fine-tuned neural network, attention-based, 66M parameters | Captures semantic meaning, handles context, understands sarcasm and nuance | Slow (100-500ms per prediction on CPU), high memory (~500MB), overconfident |
| **scikit-learn** (TF-IDF + LSA + classifier) | Bag-of-words → dimensionality reduction → traditional classifier | Fast (1-5ms per prediction), low memory (~50MB), well-calibrated probabilities | Misses context, struggles with negation, can't handle out-of-vocabulary words |

These aren't two versions of the same model. They're architecturally different — they process text in fundamentally different ways. This is critical. Two neural networks with different hyperparameters will tend to agree on the same failure modes. A statistical model and a neural model fail *differently*, and that's what makes their agreement informative.

The dual-model architecture was a deliberate design decision, documented in [ADR-001](https://github.com/carlesarnal/reddit-realtime-classification/blob/main/docs/adr/adr-001-dual-model-inference.md):

> "A single model gives us predictions but no way to gauge trustworthiness. If the model is wrong, we have no signal to detect it without ground truth labels, which aren't available in real-time."

Both models are executed inside a single PySpark UDF, each wrapped in its own OpenTelemetry span for independent latency tracking:

```python
def dual_model_prediction(row):
    content = row["content"]
    post_id = row["id"] or "unknown"

    try:
        with tracer.start_as_current_span("dual-model-inference", attributes={
            "reddit.post.id": post_id,
        }) as span:
            # Transformer inference
            with tracer.start_as_current_span("transformer-inference"):
                inputs = tokenizer(content, return_tensors="pt", truncation=True,
                                   padding=True, max_length=512)
                with torch.no_grad():
                    outputs = transformer_model(**inputs)
                probs = torch.nn.functional.softmax(outputs.logits, dim=-1)
                pred_class = torch.argmax(probs, dim=1).item()
                transformer_flair = flairs[pred_class]
                transformer_conf = float(probs[0][pred_class])

            # sklearn inference
            with tracer.start_as_current_span("sklearn-inference"):
                X = tfidf.transform([content])
                X_reduced = tsvd.transform(X)
                sk_probs = classifier.predict_proba(X_reduced)[0]
                sk_class = sk_probs.argmax()
                sklearn_flair = flairs[sk_class]
                sklearn_conf = float(sk_probs[sk_class])

            span.set_attribute("prediction.models_agree",
                               transformer_flair == sklearn_flair)

        return json.dumps({
            "id": post_id,
            "transformer_flair": transformer_flair,
            "transformer_confidence": transformer_conf,
            "sklearn_flair": sklearn_flair,
            "sklearn_confidence": sklearn_conf
        })
    except Exception as e:
        return json.dumps({"__dlq": True, "id": post_id, "error": str(e)})
```

The output goes to the `kafka-predictions` topic. The Quarkus consumer reads it and tracks agreement.

---

## Agreement Rate: What It Tells You

The agreement rate is simple:

```
agreement_rate = messages_where_both_models_agree / total_messages
```

In the Quarkus consumer, it's tracked as a Prometheus gauge:

```java
long total = totalMessages.incrementAndGet();
if (transformerFlair.equals(sklearnFlair)) {
    agreedMessages.incrementAndGet();
}
registry.gauge("model_agreement_rate", this, obj -> {
    long t = totalMessages.get();
    return t > 0 ? (double) agreedMessages.get() / t : 0.0;
});
```

### What High Agreement Means

When agreement rate is **above 90%**, both models are finding the same signal in the text. This suggests:

- The classification task is relatively unambiguous for most posts
- Both models are well-trained for the current data distribution
- The features that matter (keywords, topics, tone) are captured by both approaches

### What Low Agreement Means

When agreement rate **drops below 80%**, something interesting is happening:

- The data may have shifted — new topics, different writing styles
- One model may be degrading while the other holds steady
- The classification boundaries may be genuinely ambiguous for the current data

### The Probabilistic Argument

Under reasonable independence assumptions, if each model has accuracy `p` and their errors are independent, the probability that *both* are wrong on the same example is approximately `(1-p)²`. For two models with 80% individual accuracy:

```
P(both wrong) ≈ 0.2 × 0.2 = 0.04 = 4%
P(at least one correct | both agree) ≈ 96%
```

This is a simplification — the models aren't truly independent (they're trained on the same data) — but the intuition holds. Agreement is a stronger signal than either model alone.

The key requirement is **architectural diversity**. If both models use the same features and the same approach, their errors will be correlated, and `P(both wrong)` will be much higher than `(1-p)²`. The Transformer and sklearn models process text so differently that their error correlation is low.

---

## Disagreement Taxonomy

Not all disagreements are created equal. The pipeline tracks four zones based on confidence levels:

### Zone 1: Both Confident, Same Prediction

```
Transformer: "Politics" (confidence: 0.92)
sklearn:     "Politics" (confidence: 0.85)
```

**Interpretation:** Highest trust. Both the semantic model and the bag-of-words model found strong signal. This prediction is almost certainly correct.

**Action:** None needed. This is the happy path.

### Zone 2: Both Confident, Different Predictions

```
Transformer: "Culture"  (confidence: 0.88)
sklearn:     "Politics" (confidence: 0.82)
```

**Interpretation:** Genuinely ambiguous post, or one model has a systematic bias. The text has strong signals for both categories. This often happens with posts about cultural aspects of political events.

**Action:** These are the most valuable examples for understanding your classification boundaries. If this happens frequently for the same category pair, your label definitions may be ambiguous, or you need a "both" category.

### Zone 3: One Confident, One Uncertain

```
Transformer: "Travel"   (confidence: 0.91)
sklearn:     "Misc"     (confidence: 0.35)
```

**Interpretation:** The confident model is likely correct. The uncertain model doesn't have the right features for this example. This often happens when the Transformer captures semantic meaning (travel-related discussion) that the bag-of-words model misses because the post doesn't contain obvious travel keywords.

**Action:** Investigate which model is uncertain and why. If it's always the same model for the same category, that model has a blind spot you should understand.

### Zone 4: Both Uncertain

```
Transformer: "Misc"     (confidence: 0.28)
sklearn:     "Personal" (confidence: 0.31)
```

**Interpretation:** Neither model has a strong signal. The post is genuinely hard to classify, or it doesn't fit any of the 13 categories well.

**Action:** These posts are candidates for a DLQ or human review queue. They're also the best candidates for active learning — if you had a labeling budget, these are the examples that would improve both models the most.

### Implementing Zone Tracking

The consumer categorizes each message into a zone based on a confidence threshold (e.g., 0.6):

```java
boolean transformerConfident = transformerConfidence >= CONFIDENCE_THRESHOLD;
boolean sklearnConfident = sklearnConfidence >= CONFIDENCE_THRESHOLD;
boolean modelsAgree = transformerFlair.equals(sklearnFlair);

if (transformerConfident && sklearnConfident && modelsAgree) {
    zone = "both_confident_agree";      // Zone 1
} else if (transformerConfident && sklearnConfident && !modelsAgree) {
    zone = "both_confident_disagree";   // Zone 2
} else if (transformerConfident != sklearnConfident) {
    zone = "one_confident";             // Zone 3
} else {
    zone = "both_uncertain";            // Zone 4
}

registry.counter("prediction_zones_total", "zone", zone).increment();
```

The Grafana dashboard shows the zone distribution over time. A healthy pipeline has Zone 1 dominating. If Zone 2 or Zone 4 grow, it's time to investigate.

---

## Per-Category Analysis

Aggregate agreement rate hides important per-category variation. The 13 flair categories in the pipeline have very different characteristics:

### Categories Where Both Models Agree

**Sports, Travel, Food** — These categories have distinctive vocabulary. A post about a football match contains keywords that both TF-IDF and the Transformer can pick up. Agreement rates for these categories are typically > 95%.

### Categories Where the Transformer Wins

**Culture, Politics** — These require understanding context, tone, and implicit meaning. A post about "how your country's attitude toward X has changed" is clearly cultural to the Transformer but might get classified as "Politics" or "History" by sklearn if it contains political keywords. The Transformer's semantic understanding gives it an edge.

### Categories Where sklearn Struggles

**Meta** — Posts about the subreddit itself ("why do people always ask about..."). These are defined more by intent than vocabulary. sklearn tends to classify them based on the topic words, not the meta-discussion framing.

### The "Misc" Problem

**Misc** is a catch-all category that both models struggle with. Its agreement rate is typically the lowest because:

- It's defined by *not* fitting other categories, which is hard to learn
- The training data for "Misc" is heterogeneous — the posts don't share common features
- Both models default to "Misc" when uncertain, but at different thresholds

Per-flair agreement rates are tracked via the confusion matrix data in the consumer:

```java
protected static final Map<String, Map<String, Integer>> confusionMatrix =
    new ConcurrentHashMap<>();

// In consume():
confusionMatrix
    .computeIfAbsent(transformerFlair, k -> new ConcurrentHashMap<>())
    .merge(sklearnFlair, 1, Integer::sum);
```

The confusion matrix reveals systematic disagreement patterns. If the Transformer says "Culture" and sklearn says "Politics" 40% of the time they disagree, that's a signal about the boundary between those categories, not random noise.

---

## Confidence Calibration

A confidence score of 0.8 does not mean "80% chance of being correct." It means the model's softmax output is 0.8 for the predicted class. These are related but not the same thing.

### Neural Networks Are Overconfident

DistilBERT (and most neural networks) tends to produce high-confidence predictions even when wrong. The softmax function concentrates probability mass on the most activated logit. A Transformer predicting "Politics" with 0.95 confidence might only be correct 85% of the time at that confidence level.

This is well-documented in the ML literature (Guo et al., "On Calibration of Modern Neural Networks", 2017), but it's rarely addressed in production deployments. Most teams treat the softmax output as a probability and build alerting thresholds around it. This leads to missed alerts — the model is confidently wrong, so the confidence threshold never triggers.

### sklearn Classifiers Are Better Calibrated

The sklearn pipeline uses `predict_proba()`, which for most classifiers returns better-calibrated probabilities (especially for logistic regression and calibrated classifiers). A 0.8 confidence from sklearn is closer to an actual 80% accuracy rate.

### What This Means for Agreement Analysis

When comparing confidence scores between models, you're comparing apples and oranges:

```
Transformer confidence 0.85 ≠ sklearn confidence 0.85
```

The Transformer's 0.85 is probably overconfident. The sklearn's 0.85 is closer to reality. This is why the disagreement taxonomy uses a threshold for "confident" rather than comparing raw scores.

To properly calibrate, you'd need a labeled validation set — which brings you back to the ground truth problem. The pragmatic solution is to set confidence thresholds empirically:

- **Transformer "confident" threshold: 0.7** — because the model is overconfident, a lower threshold still captures genuinely confident predictions
- **sklearn "confident" threshold: 0.6** — better calibrated, so the threshold can be lower

These thresholds are tuned by observing the zone distributions and adjusting until the "both confident agree" zone matches expectations.

### Tracking Confidence Distributions

The pipeline tracks per-model confidence as a Prometheus gauge:

```java
registry.gauge("model_confidence_latest",
    Tags.of("model", "transformer"),
    transformerConfidence);
registry.gauge("model_confidence_latest",
    Tags.of("model", "sklearn"),
    sklearnConfidence);
```

The Grafana "Confidence Distribution" dashboard shows histograms of confidence scores for each model. A well-calibrated model has a uniform-ish distribution. An overconfident model shows a spike near 1.0.

If the Transformer's confidence distribution shifts over time — say, from a peak at 0.85 to a peak at 0.95 — that's a sign of increasing overconfidence, possibly due to data drift into the model's comfort zone.

---

## Detecting Model Drift Without Labels

Model drift comes in two flavors:

1. **Data drift** — the input distribution changes. New topics appear, writing styles evolve, the subreddit's focus shifts.
2. **Concept drift** — the relationship between inputs and labels changes. What counts as "Culture" vs "Politics" may shift with current events.

Both are invisible to traditional metrics without labels. But agreement rate can detect them.

### Agreement Rate as a Drift Detector

If the agreement rate drops, something changed. The models were trained on the same historical data, so they should agree at a stable rate on data from the same distribution. A drop means the data is moving away from the training distribution, and the models are responding differently.

```yaml
# Prometheus alerting rule
groups:
  - name: model_drift
    rules:
      - alert: ModelAgreementDrop
        expr: model_agreement_rate < 0.75
        for: 15m
        labels:
          severity: warning
        annotations:
          summary: >
            Model agreement rate has been below 75% for 15 minutes.
            This may indicate data drift or model degradation.

      - alert: ModelAgreementCritical
        expr: model_agreement_rate < 0.60
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: >
            Model agreement rate has dropped below 60%.
            Immediate investigation required.
```

### Why This Works Better Than Confidence-Based Drift Detection

Many drift detection approaches monitor confidence scores — if average confidence drops, the model is less sure, which might indicate drift. But this has two problems:

1. **Overconfident models don't show confidence drops.** The Transformer can be confidently wrong on drifted data because it's pattern-matching on features that no longer mean what they used to.

2. **Confidence can drop for benign reasons.** A batch of genuinely ambiguous posts lowers average confidence without indicating drift.

Agreement rate avoids both problems. Even if both models are confident on drifted data, they'll be confident about *different* things because they process features differently. The Transformer might pick up on new semantic patterns while sklearn's bag-of-words features haven't changed. The disagreement surfaces the drift.

### Differential Diagnosis

When agreement drops, you need to figure out which model is drifting (or if the data itself shifted). The per-category confusion matrix helps:

- **If sklearn's predictions changed but Transformer's didn't**: sklearn's features (word frequencies) shifted, possibly due to new vocabulary. The Transformer's semantic understanding is more robust.
- **If Transformer's predictions changed but sklearn's didn't**: The semantic meaning of certain phrases may have shifted. This is rarer but happens with slang or context-dependent terms.
- **If both changed in the same direction**: The data distribution genuinely shifted. Both models are responding to a real change. This isn't necessarily a problem — it might mean the subreddit's focus evolved.
- **If both changed in different directions**: Something is wrong. One or both models may need retraining.

---

## When to Stop Running Both Models

The dual-model architecture has a cost: 2x inference compute, more complex UDFs, wider output schemas, and more monitoring to maintain. The question is: when does the agreement signal stop being valuable enough to justify the cost?

### Decision Framework

**Agreement consistently > 95%:**
The models agree on almost everything. The agreement signal provides minimal information. You can safely drop the more expensive model (Transformer) and use sklearn alone. You'll save ~10x on inference compute.

But keep monitoring. If you drop the Transformer, you lose the drift detection capability. Consider running it on a sample (e.g., 10% of messages) to maintain the signal at lower cost.

**Agreement between 80-95%:**
This is the sweet spot. The models agree most of the time, but disagreements are frequent enough to be informative. Keep both models. The disagreements tell you about classification boundaries, model-specific weaknesses, and data distribution.

**Agreement below 80%:**
Either the task is genuinely hard, the models are poorly trained for the current data, or the data has drifted significantly. Investigate per-category agreement to identify the problem areas. Consider retraining on more recent data.

**Agreement varies dramatically by category:**
Consider per-category model routing. Use the Transformer only for categories where sklearn's agreement drops below a threshold. For easy categories (Sports, Travel), sklearn alone is sufficient. This hybrid approach saves compute while maintaining quality where it matters.

### The Cost Calculation

For the reddit pipeline:

| Resource | Transformer | sklearn | Savings if dropped |
|----------|-------------|---------|-------------------|
| Inference time | 100-500ms | 1-5ms | ~99% |
| Memory | ~500MB | ~50MB | ~90% |
| CPU per prediction | High | Low | ~90% |

The Transformer dominates resource usage. Dropping it when agreement is stable saves significant compute without losing much prediction quality — because high agreement means the predictions are the same anyway.

### The Meta-Lesson

Running both models isn't just about getting better predictions. It's a **diagnostic tool** for understanding your problem space:

- Which categories are genuinely hard?
- How stable is your data distribution?
- Is the expensive model worth its cost?
- Where should you invest in better training data?

Once you have answers to these questions, you can make informed architecture decisions about model selection, category design, and monitoring strategy. The dual-model architecture earns its keep not through better predictions, but through better *understanding*.

---

## Beyond Classification: Generalizing the Pattern

This pattern — running two architecturally different models and using their agreement as a quality signal — works for any prediction task. The requirements:

### 1. Models Must Be Architecturally Different

Two neural networks with different hyperparameters will agree on the same failure modes. A rule-based system and a neural network will fail differently. The diversity comes from the approach, not the parameters.

| Task | Model A (Statistical/Rule-based) | Model B (Neural/Learned) |
|------|----------------------------------|--------------------------|
| Text classification | TF-IDF + classifier | Transformer |
| Named entity recognition | Rule-based (regex, gazetteers) | Neural NER (spaCy, BERT-NER) |
| Anomaly detection | Statistical (z-score, IQR) | Isolation forest, autoencoder |
| Sentiment analysis | Lexicon-based (VADER) | Fine-tuned BERT |
| Recommendation | Collaborative filtering | Content-based / neural |
| Fraud detection | Rule engine | Gradient boosted trees |

### 2. Both Models Must Be Reasonable

If one model is trivially bad (random baseline), agreement tells you nothing. Both models should have acceptable standalone performance. The dual-model approach improves your *confidence in predictions*, not the predictions themselves.

### 3. The Cost Must Be Justified

Running two models costs more than running one. The agreement signal is most valuable when:

- Ground truth is unavailable or delayed
- Prediction errors have significant consequences
- The data distribution is non-stationary (likely to drift)
- You need to justify model choices to stakeholders

If ground truth labels are available within minutes and errors are cheap to correct, a single model with label-based monitoring is simpler and sufficient.

### 4. Disagreement Is the Signal, Not the Problem

When models disagree, the natural reaction is to "fix" the disagreement. But disagreement is *information*. It tells you:

- Where your problem is ambiguous
- Where each model's approach has limitations
- Where your training data may be insufficient
- Where your category definitions may be unclear

A pipeline with 100% agreement isn't necessarily good — it might mean both models learned the same biases. A pipeline with strategic disagreement is one where you understand the limits of your predictions.

---

## Implementation Checklist

If you're considering this pattern for your own pipeline:

1. **Choose architecturally different models.** Not variants of the same approach.
2. **Run both models on every input.** Sampling reduces the signal quality.
3. **Track agreement rate as a time-series metric.** Use Prometheus, Datadog, or equivalent.
4. **Build a disagreement taxonomy.** Four zones based on confidence levels.
5. **Analyze per-category agreement.** Aggregate rates hide important variation.
6. **Set alerting thresholds empirically.** Start with 75% warning, 60% critical.
7. **Log disagreement details.** Keep the full predictions (both models, both confidences) for offline analysis.
8. **Review disagreements periodically.** They're the best source of labeling candidates for active learning.
9. **Plan your exit criteria.** Define when agreement is stable enough to drop a model.

---

## Conclusion

Ground truth is a luxury that streaming ML pipelines don't have. Model agreement is a practical proxy that gives you:

- A reliability signal for individual predictions (Zone 1 vs Zone 4)
- A drift detector for the overall pipeline (agreement rate trends)
- A diagnostic tool for understanding your problem space (per-category analysis)
- A cost-benefit framework for model selection (when to drop the expensive model)

The dual-model architecture in the [reddit-realtime-classification](https://github.com/carlesarnal/reddit-realtime-classification) pipeline costs 2x inference compute. In return, it provides monitoring signals that would otherwise require a continuous labeling operation — which would cost far more than the extra model.

The most important lesson: **disagreement is information, not a bug.** Build your pipeline to capture it, visualize it, and learn from it.

---

*See the [examples/](examples/) directory for agreement tracking code (Java), Prometheus alerting rules (YAML), and a disagreement analysis script (Python).*
