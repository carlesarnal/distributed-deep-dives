#!/usr/bin/env python3
"""
Disagreement analysis script for dual-model prediction data.

Reads prediction records (JSON lines) exported from the kafka-predictions topic
and analyzes disagreement patterns between the Transformer and sklearn models.

Usage:
    # Export predictions from Kafka (example using kafkacat):
    kafkacat -b localhost:9093 -t kafka-predictions -C -e -o beginning | \
        python disagreement-analysis.py

    # Or from a file:
    python disagreement-analysis.py < predictions.jsonl

Output:
    - Overall agreement rate
    - Per-flair agreement rates
    - Confusion matrix (Transformer vs sklearn)
    - Disagreement zone distribution
    - Top disagreement patterns (which flair pairs disagree most)
    - Confidence calibration summary
"""

import json
import sys
from collections import Counter, defaultdict


# Confidence threshold for zone classification (match the consumer's threshold)
CONFIDENCE_THRESHOLD = 0.6


def analyze_predictions(predictions):
    """Analyze a list of dual-model prediction records."""

    total = 0
    agreed = 0

    # Per-flair tracking
    flair_total = Counter()
    flair_agreed = Counter()

    # Confusion matrix: confusion[transformer_flair][sklearn_flair] = count
    confusion = defaultdict(Counter)

    # Zone distribution
    zones = Counter()

    # Confidence tracking
    transformer_confidences = []
    sklearn_confidences = []
    agree_confidences = []
    disagree_confidences = []

    # Disagreement patterns: (transformer_flair, sklearn_flair) -> count
    disagreement_patterns = Counter()

    for pred in predictions:
        t_flair = pred.get("transformer_flair", "Unknown")
        t_conf = pred.get("transformer_confidence", 0.0)
        s_flair = pred.get("sklearn_flair", "Unknown")
        s_conf = pred.get("sklearn_confidence", 0.0)

        # Skip DLQ records
        if pred.get("__dlq"):
            continue

        total += 1
        models_agree = t_flair == s_flair

        if models_agree:
            agreed += 1
            flair_agreed[t_flair] += 1
            agree_confidences.append((t_conf, s_conf))
        else:
            disagreement_patterns[(t_flair, s_flair)] += 1
            disagree_confidences.append((t_conf, s_conf))

        flair_total[t_flair] += 1
        confusion[t_flair][s_flair] += 1
        transformer_confidences.append(t_conf)
        sklearn_confidences.append(s_conf)

        # Zone classification
        t_confident = t_conf >= CONFIDENCE_THRESHOLD
        s_confident = s_conf >= CONFIDENCE_THRESHOLD

        if t_confident and s_confident and models_agree:
            zones["both_confident_agree"] += 1
        elif t_confident and s_confident and not models_agree:
            zones["both_confident_disagree"] += 1
        elif t_confident != s_confident:
            zones["one_confident"] += 1
        else:
            zones["both_uncertain"] += 1

    return {
        "total": total,
        "agreed": agreed,
        "flair_total": flair_total,
        "flair_agreed": flair_agreed,
        "confusion": confusion,
        "zones": zones,
        "disagreement_patterns": disagreement_patterns,
        "transformer_confidences": transformer_confidences,
        "sklearn_confidences": sklearn_confidences,
        "agree_confidences": agree_confidences,
        "disagree_confidences": disagree_confidences,
    }


def print_report(stats):
    """Print a formatted analysis report."""

    total = stats["total"]
    agreed = stats["agreed"]

    if total == 0:
        print("No predictions to analyze.")
        return

    print("=" * 70)
    print("DUAL-MODEL DISAGREEMENT ANALYSIS")
    print("=" * 70)

    # --- Overall Agreement ---
    print(f"\nTotal predictions: {total}")
    print(f"Models agreed:    {agreed} ({agreed/total*100:.1f}%)")
    print(f"Models disagreed: {total - agreed} ({(total-agreed)/total*100:.1f}%)")

    # --- Zone Distribution ---
    print(f"\n{'Zone Distribution':}")
    print("-" * 50)
    for zone in ["both_confident_agree", "both_confident_disagree",
                 "one_confident", "both_uncertain"]:
        count = stats["zones"].get(zone, 0)
        pct = count / total * 100 if total > 0 else 0
        bar = "#" * int(pct / 2)
        print(f"  {zone:30s} {count:5d} ({pct:5.1f}%) {bar}")

    # --- Per-Flair Agreement ---
    print(f"\nPer-Flair Agreement Rates:")
    print("-" * 50)
    print(f"  {'Flair':15s} {'Total':>7s} {'Agreed':>7s} {'Rate':>7s}")
    for flair in sorted(stats["flair_total"].keys()):
        ft = stats["flair_total"][flair]
        fa = stats["flair_agreed"].get(flair, 0)
        rate = fa / ft * 100 if ft > 0 else 0
        indicator = " !" if rate < 80 else ""
        print(f"  {flair:15s} {ft:7d} {fa:7d} {rate:6.1f}%{indicator}")

    # --- Top Disagreement Patterns ---
    print(f"\nTop Disagreement Patterns (Transformer -> sklearn):")
    print("-" * 50)
    top_patterns = stats["disagreement_patterns"].most_common(10)
    for (t_flair, s_flair), count in top_patterns:
        pct = count / (total - agreed) * 100 if (total - agreed) > 0 else 0
        print(f"  {t_flair:15s} -> {s_flair:15s}  {count:5d} ({pct:5.1f}% of disagreements)")

    # --- Confusion Matrix ---
    all_flairs = sorted(set(stats["flair_total"].keys()) |
                        {s for row in stats["confusion"].values() for s in row})
    if len(all_flairs) <= 15:  # Only print if manageable size
        print(f"\nConfusion Matrix (rows=Transformer, cols=sklearn):")
        print("-" * 50)
        header = f"  {'':15s}" + "".join(f"{f[:6]:>7s}" for f in all_flairs)
        print(header)
        for t_flair in all_flairs:
            row = f"  {t_flair:15s}"
            for s_flair in all_flairs:
                count = stats["confusion"].get(t_flair, {}).get(s_flair, 0)
                row += f"{count:7d}"
            print(row)

    # --- Confidence Summary ---
    print(f"\nConfidence Summary:")
    print("-" * 50)
    if stats["transformer_confidences"]:
        t_mean = sum(stats["transformer_confidences"]) / len(stats["transformer_confidences"])
        s_mean = sum(stats["sklearn_confidences"]) / len(stats["sklearn_confidences"])
        print(f"  Transformer mean confidence: {t_mean:.3f}")
        print(f"  sklearn mean confidence:     {s_mean:.3f}")

    if stats["agree_confidences"]:
        t_agree = sum(c[0] for c in stats["agree_confidences"]) / len(stats["agree_confidences"])
        s_agree = sum(c[1] for c in stats["agree_confidences"]) / len(stats["agree_confidences"])
        print(f"  Mean confidence when agreeing:    T={t_agree:.3f}, S={s_agree:.3f}")

    if stats["disagree_confidences"]:
        t_disagree = sum(c[0] for c in stats["disagree_confidences"]) / len(stats["disagree_confidences"])
        s_disagree = sum(c[1] for c in stats["disagree_confidences"]) / len(stats["disagree_confidences"])
        print(f"  Mean confidence when disagreeing: T={t_disagree:.3f}, S={s_disagree:.3f}")

    # --- Recommendations ---
    print(f"\nRecommendations:")
    print("-" * 50)
    agreement_rate = agreed / total * 100

    if agreement_rate > 95:
        print("  - Agreement > 95%: Consider dropping the Transformer to save compute.")
        print("    The sklearn model alone provides equivalent predictions.")
    elif agreement_rate > 80:
        print("  - Agreement 80-95%: Sweet spot. Keep both models.")
        print("    Disagreements provide valuable diagnostic information.")
    else:
        print("  - Agreement < 80%: Investigate per-flair disagreements.")
        print("    Consider retraining on more recent data.")

    # Flag problematic categories
    for flair in sorted(stats["flair_total"].keys()):
        ft = stats["flair_total"][flair]
        fa = stats["flair_agreed"].get(flair, 0)
        if ft >= 10 and fa / ft < 0.7:
            print(f"  - '{flair}' has low agreement ({fa/ft*100:.0f}%). "
                  f"Review category definition and training data.")

    print()


def main():
    """Read predictions from stdin and analyze."""
    predictions = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            pred = json.loads(line)
            predictions.append(pred)
        except json.JSONDecodeError:
            continue

    if not predictions:
        print("No predictions found. Pipe JSON lines to stdin.")
        print("Example: kafkacat -b localhost:9093 -t kafka-predictions -C -e | python disagreement-analysis.py")
        sys.exit(1)

    stats = analyze_predictions(predictions)
    print_report(stats)


if __name__ == "__main__":
    main()
