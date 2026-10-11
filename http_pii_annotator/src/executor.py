import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from src.config import PipelineConfig
from src.embedding import EmbeddingModel
from src.errors import PipelineError
from src.evaluation import report as evaluation_report
from src.evaluation.evaluator import evaluate, has_ground_truth
from src.llm_connectors.api_base import LLMClient
from src.llm_connectors.registry import ModelSpec, create_client
from src.pipeline import EVALUATION, PREPROCESSED, Checkpoint, checkpoints_for_mode
from src.pipeline_steps.llm_step import VERIFIED_FILE, LLMStep, LLMStepContext
from src.pipeline_steps.pipeline_step import PipelineStep
from src.pipeline_steps.preprocessor import Preprocessor
from src.progress import Progress
from src.records import MESSAGE_ID, Message, append_jsonl, read_messages, read_records, write_jsonl
from src.retrieval import ExampleRetriever
from src.taxonomy import Taxonomy
from src.validation import Validator

logger = logging.getLogger(__name__)

# An LLM step is aborted if this many messages in a row fail despite retries
MAX_CONSECUTIVE_FAILURES = 5


@dataclass
class StepResult:
    """
    Outcome of running a pipeline step.

    Attributes:
        result_path: File containing the result of the step for all messages
        processed: Number of messages processed in this run
        reused: Number of messages whose result of a previous execution of the run was kept
        failed: Number of messages that could not be processed
        changed: Identifiers of the messages whose result differs from the previous execution of the run
    """
    result_path: Path
    processed: int = 0
    reused: int = 0
    failed: int = 0
    changed: set = field(default_factory=set)


@dataclass
class RunSummary:
    """Outcome of a pipeline run: the results of the executed steps by name, and the evaluation reports."""
    steps: dict[str, StepResult] = field(default_factory=dict)
    reports: dict[str, Path] = field(default_factory=dict)

    @property
    def failed_messages(self) -> int:
        return sum(step.failed for step in self.steps.values())


class PipelineExecutor:
    """
    Runs the pipeline in two phases:

    1. Annotation: the pre-processor and the LLM steps of the configured mode process the input messages. Each
       step reads the result of an earlier step and writes its own result to its output folder.
    2. Evaluation: the result of every LLM step is compared with the ground truth of the input messages.
    """

    def __init__(self, config: PipelineConfig, progress: Progress = None, retry_delay: float = 1.0):
        self.config = config
        self.progress = progress or Progress()
        self.retry_delay = retry_delay

        self.output_folder = config.run_folder / "output"
        self.log_folder = config.run_folder / "log"
        self.checkpoints = checkpoints_for_mode(config.mode)

        self._clients: dict[str, LLMClient] = {}
        self._loaded_models: dict[str, str] = {}  # API -> name of the model that was prepared last
        self._steps: list[LLMStep] = []

    def execute(self) -> RunSummary:
        summary = RunSummary()

        if self.config.eval_only:
            logger.info("Evaluation-only mode: reading the existing output of run '%s'", self.config.run_id)
        else:
            try:
                self._annotate(summary)
            finally:
                self._release_clients()

        if not self.config.skip_evaluate:
            self._evaluate(summary)

        return summary

    # ------------------------------------------------------------------ annotation

    def _result_path(self, checkpoint_name: str) -> Path:
        result_file = Preprocessor.output_files[-1] if checkpoint_name == PREPROCESSED else VERIFIED_FILE
        return self.output_folder / checkpoint_name / result_file

    def _annotate(self, summary: RunSummary):
        config = self.config
        taxonomy = Taxonomy.load(config.schema_path)

        active_checkpoints = [c for c in self.checkpoints if not getattr(config, c.skip_option)]

        # resolve all models before the first message is processed
        llm_checkpoints = [c for c in active_checkpoints if issubclass(c.step, LLMStep)]
        models = {checkpoint.name: self._resolve_model(checkpoint) for checkpoint in llm_checkpoints}

        # identifiers of the messages whose result has changed, per checkpoint
        changed: dict[str, set] = {}

        if not config.skip_preprocess:
            messages = read_messages(config.input_path, config.input_encoding)
            if not messages:
                raise PipelineError(f"No messages found in {config.input_path}")
            result = self._run_step(Preprocessor(config.normalization, config.decode_base64), messages,
                                    self.output_folder / PREPROCESSED, resume=False)
            summary.steps[PREPROCESSED] = result
            changed[PREPROCESSED] = result.changed

        if not active_checkpoints:
            return

        embedding_model = EmbeddingModel()
        validator = Validator(list(taxonomy.labels), embedding_model, config.min_label_similarity)
        retriever = None
        if config.use_rag and llm_checkpoints:
            if config.examples_path.exists():
                retriever = ExampleRetriever(config.examples_path, embedding_model)
            else:
                logger.warning("Examples file %s not found, prompting without retrieved examples",
                               config.examples_path)

        for checkpoint in active_checkpoints:
            source_path = self._result_path(checkpoint.source)
            if not source_path.exists():
                raise PipelineError(f"Step '{checkpoint.name}' requires the output of '{checkpoint.source}', "
                                    f"but {source_path} does not exist")

            model = models[checkpoint.name]
            step = checkpoint.step(LLMStepContext(
                taxonomy=taxonomy,
                client=self._client(model.api),
                model=model,
                validator=validator,
                retriever=retriever,
                max_examples=config.max_examples,
                min_example_similarity=config.min_example_similarity,
                normalization=config.normalization,
                decode_base64=config.decode_base64,
                log_folder=self.log_folder,
                max_retries=config.max_retries,
                max_requeries=config.max_label_requeries,
                retry_delay=self.retry_delay
            ))
            self._steps.append(step)

            logger.info("%s: using %s model '%s'", checkpoint.name, model.api, model.name)
            result = self._run_step(
                step,
                messages=read_messages(source_path),
                out_folder=self.output_folder / checkpoint.name,
                resume=not config.restart,
                changed_upstream=changed.get(checkpoint.source, set()),
                before_processing=lambda model=model: self._prepare_model(model)
            )
            summary.steps[checkpoint.name] = result
            changed[checkpoint.name] = result.changed

    def _resolve_model(self, checkpoint: Checkpoint) -> ModelSpec:
        model = self.config.model_for(checkpoint.step.task)
        if model is None:
            raise PipelineError(
                f"No model provided for step {checkpoint.step.task}. Please provide a default model with the "
                f"-model argument or the model field in the configuration file, or specify a model for each "
                f"pipeline step with the -model-<step> arguments or the models field in the configuration file.")
        return ModelSpec(api=self.config.api, name=model)

    def _client(self, api: str) -> LLMClient:
        if api not in self._clients:
            self._clients[api] = create_client(api, self.config.hostname, self.config.request_timeout)
        return self._clients[api]

    def _prepare_model(self, model: ModelSpec):
        """Makes the model of a step available, releasing the model that the API served before."""
        loaded_model = self._loaded_models.get(model.api)
        if loaded_model == model.name:
            return
        client = self._client(model.api)
        if loaded_model is not None:
            client.release(loaded_model)
        try:
            client.prepare(model.name)
        except Exception as e:
            raise PipelineError(f"Could not load model '{model.name}': {type(e).__name__}: {e}")
        self._loaded_models[model.api] = model.name

    def _release_clients(self):
        for api, client in self._clients.items():
            loaded_model = self._loaded_models.get(api)
            if loaded_model is not None and not self.config.keep_model_loaded:
                client.release(loaded_model)
            client.close()
        self._clients = {}
        self._loaded_models = {}

    def _run_step(
            self,
            step: PipelineStep,
            messages: list[Message],
            out_folder: Path,
            resume: bool,
            changed_upstream: set = frozenset(),
            before_processing=None
    ) -> StepResult:
        """
        Processes the messages with a step and writes the returned messages to the step's output files, in the
        order of the input.

        If resuming, the results of a previous execution are kept for all messages that were processed
        successfully and whose input has not changed since; only the remaining messages are processed.
        """
        out_folder.mkdir(parents=True, exist_ok=True)
        paths = [out_folder / file_name for file_name in step.output_files]
        order = {message.message_id: position for position, message in enumerate(messages)}

        previous = [self._records_by_id(path) for path in paths]
        previous_results = previous[-1]

        kept_ids = set()
        if resume:
            kept_ids = {
                message_id for message_id in order
                if all(message_id in records for records in previous)
                and 'error' not in previous_results[message_id]
                and message_id not in changed_upstream
            }

        pending = [message for message in messages if message.message_id not in kept_ids]
        result = StepResult(result_path=paths[-1], reused=len(kept_ids))

        # start from the kept results only; the results of the pending messages are appended as they complete
        for path, records in zip(paths, previous):
            write_jsonl(path, [records[m.message_id] for m in messages if m.message_id in kept_ids])

        if pending and before_processing is not None:
            before_processing()

        self.progress.start_step(step.task, total=len(messages), done=len(kept_ids))
        consecutive_failures = 0
        pool = ThreadPoolExecutor(max_workers=self.config.concurrency)
        try:
            # map() returns the results in the order of the pending messages
            for message, outputs in zip(pending, pool.map(step.process, pending)):
                records = [output.to_dict() for output in outputs]
                for path, record in zip(paths, records):
                    append_jsonl(path, record)

                result.processed += 1
                if records[-1] != previous_results.get(message.message_id):
                    result.changed.add(message.message_id)

                if outputs[-1].error is not None:
                    result.failed += 1
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        raise PipelineError(
                            f"Step '{step.task}' failed for {consecutive_failures} messages in a row, last error: "
                            f"{outputs[-1].error}")
                else:
                    consecutive_failures = 0
                self.progress.advance()
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            self.progress.finish_step()
            if kept_ids and result.processed:
                # restore the order of the input, which is lost when resuming after failed messages
                for path in paths:
                    records = self._records_by_id(path)
                    write_jsonl(path, [records[m.message_id] for m in messages if m.message_id in records])

        logger.info("%s: %d messages processed, %d reused from the previous execution, %d failed", step.task,
                    result.processed, result.reused, result.failed)
        return result

    @staticmethod
    def _records_by_id(path: Path) -> dict[str, dict]:
        """The records of an output file by message identifier. Records without identifier are ignored."""
        return {record[MESSAGE_ID]: record for record in read_records(path)
                if isinstance(record, dict) and record.get(MESSAGE_ID) is not None}

    # ------------------------------------------------------------------ evaluation

    def _evaluate(self, summary: RunSummary):
        config = self.config
        input_records = read_records(config.input_path, config.input_encoding)
        if not has_ground_truth(input_records):
            logger.info("Skipping the evaluation: the input messages do not contain ground truth annotations "
                        "('pii_annotations')")
            return

        for checkpoint in self.checkpoints:
            result_path = self._result_path(checkpoint.name)
            output_records = read_records(result_path)
            if not output_records:
                logger.info("Skipping the evaluation of %s: no output found in %s", checkpoint.name, result_path)
                continue

            report, false_negatives = evaluate(output_records, input_records, checkpoint.evaluation_mode)
            summary.reports[checkpoint.name] = evaluation_report.write_report(
                folder=self.output_folder / EVALUATION,
                step_name=checkpoint.name,
                report=report,
                false_negatives=false_negatives,
                run_id=config.run_id,
                input_path=config.input_path,
                output_path=result_path
            )
            logger.info(evaluation_report.format_summary(checkpoint.name, report))
            logger.debug(evaluation_report.format_per_label_metrics(report))
