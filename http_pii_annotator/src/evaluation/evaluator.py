"""
Evaluation of the output of a pipeline step against the ground truth annotations of the input messages.
"""
from dataclasses import asdict

from src.evaluation.metrics import (
    DEFAULT_FUZZY_MATCH_THRESHOLD,
    LabelMetrics,
    compute_overall_metrics,
    evaluate_instance_level,
    evaluate_label_level,
    find_false_negatives,
)
from src.records import MESSAGE_ID, positional_id

GROUND_TRUTH_FIELD = 'pii_annotations'


def has_ground_truth(input_records: list[dict]) -> bool:
    return any(GROUND_TRUTH_FIELD in record for record in input_records)


def ground_truth_annotations(input_record: dict) -> list[dict]:
    """
    The ground truth annotations of an input message. Only annotations with the placement 'body' are returned,
    since the annotation pipeline only examines the message body.
    """
    return [ann for ann in input_record.get(GROUND_TRUTH_FIELD, []) if ann.get('placement', 'body') == 'body']


def predicted_annotations(output_record: dict) -> list[dict]:
    """
    The predicted annotations of a processed message. The predicted labels of a message that has only been
    classified are converted to annotations without values.
    """
    if output_record.get('annotations'):
        return output_record['annotations']
    if 'predicted_labels' in output_record:
        return [{'data_type': label, 'value': ''} for label in output_record['predicted_labels']]
    return []


def _method_and_path(record: dict) -> tuple:
    """Method and path of a message, which may be wrapped together with its ground truth ('http_request')."""
    method = record.get('method', '')
    path = record.get('path', '')
    if not method or not path:
        http_request = record.get('http_request', {})
        method = http_request.get('method', '')
        path = http_request.get('path', '')
    return method, path


def _same_request(input_record: dict, output_record: dict) -> bool:
    input_id = _method_and_path(input_record)
    output_id = _method_and_path(output_record)
    if not input_id[0] or not input_id[1] or not output_id[0] or not output_id[1]:
        return False
    return input_id == output_id


class GroundTruthMatcher:
    """
    Finds the input message, and thereby the ground truth, of a processed message.

    Processed messages are matched by their message identifier. Output of earlier versions of the pipeline does
    not contain identifiers; such messages are matched to the input message at the same position if method and
    path are equal, and otherwise to the first input message with the same method and path.
    """

    def __init__(self, input_records: list[dict]):
        self.input_records = input_records
        # messages are identified by their position, optionally followed by their own 'id'
        self.by_id = {}
        for index, record in enumerate(input_records):
            self.by_id[positional_id(index, {})] = record
            self.by_id[positional_id(index, record)] = record

    def match(self, index: int, output_record: dict) -> dict | None:
        if MESSAGE_ID in output_record:
            return self.by_id.get(output_record[MESSAGE_ID])

        if index < len(self.input_records):
            if _same_request(self.input_records[index], output_record):
                return self.input_records[index]
            for candidate in self.input_records:
                if _same_request(candidate, output_record):
                    return candidate
        return None


def _accumulate(aggregated: dict[str, LabelMetrics], metrics: dict[str, LabelMetrics]):
    for label, label_metrics in metrics.items():
        aggregated.setdefault(label, LabelMetrics(label=label)).add(label_metrics)


def _finalize(aggregated: dict[str, LabelMetrics]) -> dict[str, dict]:
    for label_metrics in aggregated.values():
        label_metrics.compute_metrics()
    return {label: asdict(label_metrics) for label, label_metrics in aggregated.items()}


def evaluate(
        output_records: list[dict],
        input_records: list[dict],
        evaluation_mode: str = "annotations",
        fuzzy_match_threshold: float = DEFAULT_FUZZY_MATCH_THRESHOLD
) -> tuple[dict, list[dict]]:
    """
    Compares the processed messages with the ground truth of the input messages.

    Args:
        output_records: Messages processed by a pipeline step
        input_records: Input messages of the pipeline with their ground truth annotations
        evaluation_mode: "annotations" to evaluate labels and values, "labels_only" to evaluate labels only
        fuzzy_match_threshold: Minimum similarity of two values to be considered a fuzzy match

    Returns:
        - The evaluation report: label-level, exact instance-level and fuzzy instance-level metrics per label and
          overall, and the results per message
        - The false negatives in the format [{"label": str, "value": str}, ...]
    """
    labels_only = evaluation_mode == "labels_only"
    matcher = GroundTruthMatcher(input_records)

    matched_messages = 0
    mismatched_messages = 0
    per_sample_results = []
    false_negatives = []

    label_level: dict[str, LabelMetrics] = {}
    label_level_exact_matches = 0
    label_level_partial_matches = 0

    instance_exact: dict[str, LabelMetrics] = {}
    instance_exact_match_count = 0

    instance_fuzzy: dict[str, LabelMetrics] = {}
    instance_fuzzy_match_count = 0
    all_fuzzy_similarities = []

    for index, output_record in enumerate(output_records):
        input_record = matcher.match(index, output_record)
        if input_record is None:
            mismatched_messages += 1
            continue
        matched_messages += 1

        gt_annotations = ground_truth_annotations(input_record)
        pred_annotations = predicted_annotations(output_record)

        label_metrics, exact_match, partial_match = evaluate_label_level(gt_annotations, pred_annotations)
        label_level_exact_matches += exact_match
        label_level_partial_matches += partial_match
        _accumulate(label_level, label_metrics)

        instance_exact_match = None
        avg_fuzzy_similarity = None
        if not labels_only:
            exact_metrics, fuzzy_metrics, instance_match, fuzzy_match_count, avg_fuzzy_similarity = \
                evaluate_instance_level(gt_annotations, pred_annotations, fuzzy_match_threshold)
            instance_exact_match_count += instance_match
            instance_fuzzy_match_count += fuzzy_match_count
            all_fuzzy_similarities.append(avg_fuzzy_similarity)
            _accumulate(instance_exact, exact_metrics)
            _accumulate(instance_fuzzy, fuzzy_metrics)
            instance_exact_match = instance_match == 1

        method, path = _method_and_path(output_record)
        per_sample_results.append({
            'index': index,
            'method': method,
            'path': path,
            'ground_truth_count': len(gt_annotations),
            'predicted_count': len(pred_annotations),
            'label_level_exact_match': exact_match == 1,
            'instance_level_exact_match': instance_exact_match,
            'fuzzy_similarity': avg_fuzzy_similarity
        })

        false_negatives.extend(find_false_negatives(gt_annotations, pred_annotations, labels_only))

    label_level_metrics = _finalize(label_level)
    instance_exact_metrics = _finalize(instance_exact)
    instance_fuzzy_metrics = _finalize(instance_fuzzy)

    total_samples = matched_messages
    total_gt_instances = sum(len(ground_truth_annotations(record)) for record in input_records[:total_samples])

    label_overall = compute_overall_metrics(label_level)
    label_overall.total_samples = total_samples
    label_overall.exact_match_count = label_level_exact_matches
    label_overall.exact_match_accuracy = label_level_exact_matches / total_samples if total_samples > 0 else 0.0
    label_overall.partial_match_count = label_level_partial_matches
    label_overall.partial_match_accuracy = label_level_partial_matches / total_samples if total_samples > 0 else 0.0

    exact_overall = compute_overall_metrics(instance_exact)
    exact_overall.total_samples = total_samples
    exact_overall.exact_match_count = instance_exact_match_count
    exact_overall.exact_match_accuracy = instance_exact_match_count / total_samples if total_samples > 0 else 0.0

    fuzzy_overall = compute_overall_metrics(instance_fuzzy)
    fuzzy_overall.total_samples = total_samples
    fuzzy_overall.partial_match_count = instance_fuzzy_match_count
    fuzzy_overall.partial_match_accuracy = \
        instance_fuzzy_match_count / total_gt_instances if total_gt_instances > 0 else 0.0
    fuzzy_overall.average_fuzzy_similarity = \
        sum(all_fuzzy_similarities) / len(all_fuzzy_similarities) if all_fuzzy_similarities else 0.0

    report = {
        'evaluation_mode': evaluation_mode,
        'total_messages': len(output_records),
        'matched_messages': matched_messages,
        'mismatched_messages': mismatched_messages,
        'label_level_evaluation': {
            'per_label_metrics': label_level_metrics,
            'overall_metrics': asdict(label_overall)
        },
        'instance_level_exact_evaluation': {
            'description': 'Instance-level evaluation using exact string matching',
            'per_label_metrics': instance_exact_metrics,
            'overall_metrics': asdict(exact_overall)
        },
        'instance_level_fuzzy_evaluation': {
            'description': f'Instance-level evaluation using fuzzy matching (threshold: {fuzzy_match_threshold})',
            'per_label_metrics': instance_fuzzy_metrics,
            'overall_metrics': asdict(fuzzy_overall)
        },
        'per_sample_results': per_sample_results
    }
    return report, false_negatives
