"""
Metrics for comparing predicted annotations of a message with its ground truth annotations.

Annotations are dicts with the keys 'data_type' (label) and 'value'.
"""
from dataclasses import dataclass
from difflib import SequenceMatcher

DEFAULT_FUZZY_MATCH_THRESHOLD = 0.8


@dataclass
class LabelMetrics:
    """Metrics for a single label/data_type."""
    label: str
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    precision: float = 0.0
    recall: float = 0.0
    f1_score: float = 0.0

    def add(self, other: "LabelMetrics"):
        """Add the counts of another set of metrics for the same label."""
        self.true_positives += other.true_positives
        self.false_positives += other.false_positives
        self.false_negatives += other.false_negatives

    def compute_metrics(self):
        """Compute precision, recall, and F1 from TP, FP, FN counts."""
        if self.true_positives + self.false_positives > 0:
            self.precision = self.true_positives / (self.true_positives + self.false_positives)
        else:
            self.precision = 0.0

        if self.true_positives + self.false_negatives > 0:
            self.recall = self.true_positives / (self.true_positives + self.false_negatives)
        else:
            self.recall = 0.0

        if self.precision + self.recall > 0:
            self.f1_score = 2 * (self.precision * self.recall) / (self.precision + self.recall)
        else:
            self.f1_score = 0.0


@dataclass
class OverallMetrics:
    """Overall aggregated metrics."""
    # Micro-averaged (sum all TP, FP, FN across labels, then compute)
    micro_precision: float = 0.0
    micro_recall: float = 0.0
    micro_f1: float = 0.0

    # Macro-averaged (average of per-label metrics)
    macro_precision: float = 0.0
    macro_recall: float = 0.0
    macro_f1: float = 0.0

    # Exact match accuracy
    exact_match_accuracy: float = 0.0
    exact_match_count: int = 0
    total_samples: int = 0

    # Partial/fuzzy match metrics
    partial_match_accuracy: float = 0.0
    partial_match_count: int = 0
    average_fuzzy_similarity: float = 0.0


def _label_metrics(label: str, tp: int, fp: int, fn: int) -> LabelMetrics:
    metrics = LabelMetrics(label=label, true_positives=tp, false_positives=fp, false_negatives=fn)
    metrics.compute_metrics()
    return metrics


def _instances(annotations: list[dict]) -> list[tuple[str, str]]:
    return [(ann.get('data_type', ''), ann.get('value', '').strip()) for ann in annotations]


def fuzzy_match_score(str1: str, str2: str) -> float:
    """Compute the similarity of two strings using SequenceMatcher, ignoring case and surrounding whitespace."""
    if not str1 or not str2:
        return 0.0
    return SequenceMatcher(None, str1.strip().lower(), str2.strip().lower()).ratio()


def evaluate_label_level(
        ground_truth: list[dict],
        predicted: list[dict]
) -> tuple[dict[str, LabelMetrics], int, int]:
    """
    Evaluate at label level - matches by data_type only (presence-based).

    For each label, we check if it's present in ground truth and/or predictions:
    - TP: Label present in both ground truth and predictions
    - FP: Label present in predictions but not in ground truth
    - FN: Label present in ground truth but not in predictions

    Returns:
        - Per-label metrics
        - 1 if the sets of labels are equal, 0 otherwise
        - 1 if the sets of labels overlap, 0 otherwise
    """
    gt_labels = set(ann.get('data_type', '') for ann in ground_truth)
    pred_labels = set(ann.get('data_type', '') for ann in predicted)

    # Presence-based: each label contributes at most 1 TP/FP/FN
    label_metrics = {
        label: _label_metrics(
            label,
            tp=1 if (label in gt_labels and label in pred_labels) else 0,
            fp=1 if (label in pred_labels and label not in gt_labels) else 0,
            fn=1 if (label in gt_labels and label not in pred_labels) else 0
        )
        for label in gt_labels | pred_labels
    }

    exact_match = 1 if gt_labels == pred_labels else 0
    partial_match = 1 if gt_labels & pred_labels else 0

    return label_metrics, exact_match, partial_match


def evaluate_instance_level(
        ground_truth: list[dict],
        predicted: list[dict],
        fuzzy_match_threshold: float = DEFAULT_FUZZY_MATCH_THRESHOLD
) -> tuple[dict[str, LabelMetrics], dict[str, LabelMetrics], int, int, float]:
    """
    Evaluate at instance level - matches by data_type AND value.

    Performs two sub-evaluations:
    1. Exact matching: value strings must match exactly (after stripping whitespace)
    2. Fuzzy matching: values match if SequenceMatcher similarity >= threshold

    Returns:
        - exact_metrics: Per-label metrics using exact string matching
        - fuzzy_metrics: Per-label metrics using fuzzy matching
        - exact_match: 1 if all instances match exactly, 0 otherwise
        - fuzzy_match_count: Number of GT instances with a fuzzy match
        - avg_fuzzy_similarity: Average best similarity score across GT instances
    """
    # Lists preserve duplicates for correct counting in fuzzy matching
    gt_instances = _instances(ground_truth)
    pred_instances = _instances(predicted)

    all_labels = set(inst[0] for inst in gt_instances) | set(inst[0] for inst in pred_instances)

    # === EXACT MATCHING ===
    gt_set = set(gt_instances)
    pred_set = set(pred_instances)

    exact_metrics: dict[str, LabelMetrics] = {}
    for label in all_labels:
        gt_values = set(inst[1] for inst in gt_set if inst[0] == label)
        pred_values = set(inst[1] for inst in pred_set if inst[0] == label)

        exact_metrics[label] = _label_metrics(
            label,
            tp=len(gt_values & pred_values),
            fp=len(pred_values - gt_values),
            fn=len(gt_values - pred_values)
        )

    exact_match = 1 if gt_set == pred_set else 0

    # === FUZZY MATCHING ===
    fuzzy_metrics: dict[str, LabelMetrics] = {}
    fuzzy_similarities = []
    fuzzy_match_count = 0

    for label in all_labels:
        gt_values = [inst[1] for inst in gt_instances if inst[0] == label]
        pred_values = [inst[1] for inst in pred_instances if inst[0] == label]

        # Track matched predictions to avoid double-counting
        pred_matched = [False] * len(pred_values)
        tp = 0

        # For each GT value, find the best fuzzy match among the predictions that are not matched yet
        for gt_val in gt_values:
            best_similarity = 0.0
            best_pred_idx = -1

            for pred_idx, pred_val in enumerate(pred_values):
                if pred_matched[pred_idx]:
                    continue
                similarity = fuzzy_match_score(gt_val, pred_val)
                if similarity > best_similarity:
                    best_similarity = similarity
                    best_pred_idx = pred_idx

            fuzzy_similarities.append(best_similarity)

            if best_similarity >= fuzzy_match_threshold and best_pred_idx >= 0:
                tp += 1
                pred_matched[best_pred_idx] = True
                fuzzy_match_count += 1

        fuzzy_metrics[label] = _label_metrics(
            label,
            tp=tp,
            fp=sum(1 for matched in pred_matched if not matched),  # predictions not matched to any GT
            fn=len(gt_values) - tp  # GT values without a fuzzy match
        )

    avg_fuzzy_similarity = sum(fuzzy_similarities) / len(fuzzy_similarities) if fuzzy_similarities else 0.0

    return exact_metrics, fuzzy_metrics, exact_match, fuzzy_match_count, avg_fuzzy_similarity


def find_false_negatives(ground_truth: list[dict], predicted: list[dict], labels_only: bool) -> list[dict]:
    """
    Find the ground truth annotations that are missing in the predictions.

    Compares only data types if labels_only is set (one representative annotation is returned per missed
    label), and data type and exact value otherwise.

    Returns:
        List of false negatives in the format [{"label": str, "value": str}, ...]
    """
    false_negatives = []

    if labels_only:
        gt_labels = set(ann.get('data_type', '') for ann in ground_truth)
        pred_labels = set(ann.get('data_type', '') for ann in predicted)

        for label in sorted(gt_labels - pred_labels):
            for ann in ground_truth:
                if ann.get('data_type', '') == label:
                    false_negatives.append({"label": label, "value": ann.get('value', '')})
                    break
    else:
        pred_instances = set(_instances(predicted))
        for data_type, value in _instances(ground_truth):
            if (data_type, value) not in pred_instances:
                false_negatives.append({"label": data_type, "value": value})

    return false_negatives


def compute_overall_metrics(label_metrics: dict[str, LabelMetrics]) -> OverallMetrics:
    """Compute micro and macro averaged metrics from per-label metrics."""
    overall = OverallMetrics()

    if not label_metrics:
        return overall

    # Micro-averaging: sum all TP, FP, FN
    total_tp = sum(m.true_positives for m in label_metrics.values())
    total_fp = sum(m.false_positives for m in label_metrics.values())
    total_fn = sum(m.false_negatives for m in label_metrics.values())

    if total_tp + total_fp > 0:
        overall.micro_precision = total_tp / (total_tp + total_fp)
    if total_tp + total_fn > 0:
        overall.micro_recall = total_tp / (total_tp + total_fn)
    if overall.micro_precision + overall.micro_recall > 0:
        overall.micro_f1 = 2 * (overall.micro_precision * overall.micro_recall) / \
                           (overall.micro_precision + overall.micro_recall)

    # Macro-averaging: average of per-label metrics
    num_labels = len(label_metrics)
    overall.macro_precision = sum(m.precision for m in label_metrics.values()) / num_labels
    overall.macro_recall = sum(m.recall for m in label_metrics.values()) / num_labels
    overall.macro_f1 = sum(m.f1_score for m in label_metrics.values()) / num_labels

    return overall
