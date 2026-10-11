"""
Writing and formatting of evaluation reports.
"""
import json
from pathlib import Path

LEVELS = (
    ('label_level_evaluation', 'Label level'),
    ('instance_level_fuzzy_evaluation', 'Instance level (fuzzy)'),
    ('instance_level_exact_evaluation', 'Instance level (exact)'),
)


def report_path(folder: Path, step_name: str) -> Path:
    return Path(folder) / f"evaluation_report_{step_name}.json"


def false_negatives_path(folder: Path, step_name: str) -> Path:
    return Path(folder) / f"false_negatives_{step_name}.json"


def write_report(folder: Path, step_name: str, report: dict, false_negatives: list[dict], run_id: str,
                 input_path: str, output_path: str) -> Path:
    """
    Writes the evaluation report of a pipeline step and its list of false negatives to the given folder.

    Returns:
        Path of the evaluation report
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)

    path = report_path(folder, step_name)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({
            'run_id': run_id,
            'step_name': step_name,
            'evaluation_mode': report['evaluation_mode'],
            'input_folder': str(input_path),
            'output_folder': str(output_path),
            **{key: value for key, value in report.items() if key != 'evaluation_mode'}
        }, f, indent=2)

    with open(false_negatives_path(folder, step_name), 'w', encoding='utf-8') as f:
        json.dump(false_negatives, f, indent=2)

    return path


def format_summary(step_name: str, report: dict) -> str:
    """One line per evaluation level with the micro-averaged precision, recall and F1."""
    lines = [f"Evaluation of {step_name}: {report['matched_messages']} of {report['total_messages']} messages "
             f"matched to ground truth"]
    for key, title in LEVELS:
        if report['evaluation_mode'] == 'labels_only' and key != 'label_level_evaluation':
            continue
        overall = report[key]['overall_metrics']
        lines.append(f"  {title + ':':<24} P={overall['micro_precision']:.4f}  R={overall['micro_recall']:.4f}  "
                     f"F1={overall['micro_f1']:.4f}")
    return "\n".join(lines)


def format_per_label_metrics(report: dict) -> str:
    """Tables of the metrics per label for each evaluation level."""
    lines = []
    for key, title in LEVELS:
        per_label = report[key]['per_label_metrics']
        if not per_label:
            continue
        lines.append(f"{title}")
        lines.append(f"  {'Label':<32} {'Precision':>10} {'Recall':>10} {'F1':>10} {'TP':>6} {'FP':>6} {'FN':>6}")
        for label, metrics in sorted(per_label.items()):
            lines.append(f"  {label:<32} {metrics['precision']:>10.4f} {metrics['recall']:>10.4f} "
                         f"{metrics['f1_score']:>10.4f} {metrics['true_positives']:>6} "
                         f"{metrics['false_positives']:>6} {metrics['false_negatives']:>6}")
    return "\n".join(lines)
