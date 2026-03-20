package examples;

import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Tags;
import io.micrometer.core.instrument.Timer;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Simplified agreement tracking logic extracted from the reddit-realtime-classification
 * pipeline's BaseResource.java. Shows the core pattern for tracking dual-model agreement
 * as a Prometheus gauge.
 *
 * In the real pipeline, this runs inside a Quarkus @Singleton that consumes from
 * the kafka-predictions topic via SmallRye Reactive Messaging.
 */
public class AgreementTracker {

    private final MeterRegistry registry;

    // Rolling counters for agreement rate
    private final AtomicLong totalMessages = new AtomicLong(0);
    private final AtomicLong agreedMessages = new AtomicLong(0);

    // Per-flair counters for category-level analysis
    private final Map<String, Map<String, Integer>> confusionMatrix = new ConcurrentHashMap<>();

    // Confidence threshold for zone classification
    private static final double CONFIDENCE_THRESHOLD = 0.6;

    public AgreementTracker(MeterRegistry registry) {
        this.registry = registry;
    }

    /**
     * Process a dual-model prediction message.
     * Called for every message consumed from the kafka-predictions topic.
     */
    public void trackPrediction(
            String transformerFlair, double transformerConfidence,
            String sklearnFlair, double sklearnConfidence) {

        // Start timing for processing latency histogram
        Timer.Sample sample = Timer.start(registry);

        // --- Agreement Rate ---
        long total = totalMessages.incrementAndGet();
        boolean modelsAgree = transformerFlair.equals(sklearnFlair);
        if (modelsAgree) {
            agreedMessages.incrementAndGet();
        }

        // Expose as a gauge that Prometheus scrapes
        registry.gauge("model_agreement_rate", this, obj -> {
            long t = totalMessages.get();
            return t > 0 ? (double) agreedMessages.get() / t : 0.0;
        });

        // --- Per-Model Counters ---
        registry.counter("flair_messages_total",
                Tags.of("model", "transformer", "flair", transformerFlair)).increment();
        registry.counter("flair_messages_total",
                Tags.of("model", "sklearn", "flair", sklearnFlair)).increment();

        // --- Confidence Tracking ---
        registry.gauge("model_confidence_latest",
                Tags.of("model", "transformer"), transformerConfidence);
        registry.gauge("model_confidence_latest",
                Tags.of("model", "sklearn"), sklearnConfidence);

        // --- Pipeline Stage Counter ---
        registry.counter("pipeline_messages_total", "stage", "consumed").increment();

        // --- Confusion Matrix ---
        confusionMatrix
                .computeIfAbsent(transformerFlair, k -> new ConcurrentHashMap<>())
                .merge(sklearnFlair, 1, Integer::sum);

        // --- Disagreement Zone Classification ---
        boolean transformerConfident = transformerConfidence >= CONFIDENCE_THRESHOLD;
        boolean sklearnConfident = sklearnConfidence >= CONFIDENCE_THRESHOLD;

        String zone;
        if (transformerConfident && sklearnConfident && modelsAgree) {
            zone = "both_confident_agree";       // Zone 1: highest trust
        } else if (transformerConfident && sklearnConfident && !modelsAgree) {
            zone = "both_confident_disagree";    // Zone 2: genuinely ambiguous
        } else if (transformerConfident != sklearnConfident) {
            zone = "one_confident";              // Zone 3: one model uncertain
        } else {
            zone = "both_uncertain";             // Zone 4: hard example
        }

        registry.counter("prediction_zones_total", "zone", zone).increment();

        // Stop the processing latency timer
        sample.stop(registry.timer("flair_processing_latency_seconds"));
    }

    /**
     * Get current agreement rate.
     */
    public double getAgreementRate() {
        long total = totalMessages.get();
        return total > 0 ? (double) agreedMessages.get() / total : 0.0;
    }

    /**
     * Get the confusion matrix for offline analysis.
     * Rows = Transformer predictions, Columns = sklearn predictions.
     */
    public Map<String, Map<String, Integer>> getConfusionMatrix() {
        return confusionMatrix;
    }
}
