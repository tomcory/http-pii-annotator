"""
Definition of the pipeline: its checkpoints, the steps that produce them and the modes in which they are run.
"""
from dataclasses import dataclass

from src.pipeline_steps.annotator import Annotator
from src.pipeline_steps.llm_classifier import LLMClassifier
from src.pipeline_steps.llm_step import LLMStep
from src.pipeline_steps.pipeline_step import PipelineStep
from src.pipeline_steps.reviewer import Reviewer
from src.pipeline_steps.targeted_annotator import TargetedAnnotator

MODES = ("two-stage", "single-stage", "ablation")

# Output folders within '<root_dir>/<run_id>/output'
PREPROCESSED = "1_preprocessed"
EVALUATION = "evaluation"


@dataclass(frozen=True)
class Checkpoint:
    """
    A point of the pipeline at which the result of an annotation step is stored and evaluated.

    Attributes:
        name: Name of the checkpoint, used as output folder and to identify the evaluation report
        step: Class of the LLM step that produces the checkpoint
        source: Name of the checkpoint (or PREPROCESSED) whose result is the input of the step
        modes: Pipeline modes in which the checkpoint is produced
        skip_option: Name of the configuration option that skips the step
        evaluation_mode: "annotations" to evaluate labels and values, "labels_only" to evaluate labels only
    """
    name: str
    step: type[PipelineStep]
    source: str
    modes: tuple[str, ...]
    skip_option: str
    evaluation_mode: str = "annotations"


CHECKPOINTS = (
    Checkpoint("2_classified", LLMClassifier, PREPROCESSED, ("two-stage", "ablation"), "skip_classify",
               evaluation_mode="labels_only"),
    Checkpoint("3_annotated_single_stage", Annotator, PREPROCESSED, ("single-stage", "ablation"), "skip_annotate"),
    Checkpoint("4_annotated_two_stage", TargetedAnnotator, "2_classified", ("two-stage", "ablation"),
               "skip_annotate"),
    Checkpoint("5_reviewed_single_stage", Reviewer, "3_annotated_single_stage", ("single-stage", "ablation"),
               "skip_review"),
    Checkpoint("6_reviewed_two_stage", Reviewer, "4_annotated_two_stage", ("two-stage", "ablation"), "skip_review"),
)

LLM_TASKS = tuple(dict.fromkeys(checkpoint.step.task for checkpoint in CHECKPOINTS
                                if issubclass(checkpoint.step, LLMStep)))


def checkpoints_for_mode(mode: str) -> list[Checkpoint]:
    """The checkpoints of a pipeline mode in the order in which they are produced."""
    return [checkpoint for checkpoint in CHECKPOINTS if mode in checkpoint.modes]
