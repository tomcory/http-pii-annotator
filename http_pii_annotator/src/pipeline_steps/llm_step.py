import logging
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from src.harness import Harness
from src.llm_connectors.api_base import LLMClient
from src.llm_connectors.registry import ModelSpec
from src.pipeline_steps.pipeline_step import PipelineStep
from src.pipeline_steps.preprocessor import ORIGINAL_BODY, original_annotations
from src.records import Message
from src.retrieval import ExampleRetriever
from src.taxonomy import Taxonomy
from src.validation import Validator

VERIFIED_FILE = 'verified.jsonl'

#: Message field that holds the annotations of a message as they occur in its original body
ORIGINAL_ANNOTATIONS = 'original_annotations'

logger = logging.getLogger(__name__)


@dataclass
class LLMStepContext:
    """
    Everything an LLM step needs besides its task-specific logic.

    Attributes:
        taxonomy: Label taxonomy
        client: API connector
        model: Model to prompt
        validator: Validator of the model output
        retriever: Retriever of few-shot examples (None to prompt without retrieved examples)
        max_examples: Number of examples retrieved by payload similarity
        min_example_similarity: Minimum cosine similarity of examples retrieved by payload similarity
        normalization: Normalisation with which the bodies were pre-processed
        decode_base64: Whether Base64-encoded substrings of the bodies were decoded by the pre-processing
        log_folder: Folder in which responses and processing times are logged
        max_retries: Number of times a failed call of the model is repeated
        retry_delay: Seconds to wait before the first retry
        max_requeries: Number of times the model is asked again after a response with rejected labels
    """
    taxonomy: Taxonomy
    client: LLMClient
    model: ModelSpec
    validator: Validator
    retriever: ExampleRetriever | None = None
    max_examples: int = 3
    min_example_similarity: float = 0.3
    normalization: str = "paper"
    decode_base64: bool = False
    log_folder: Path | None = None
    max_retries: int = 3
    retry_delay: float = 1.0
    max_requeries: int = 1


class LLMStep(PipelineStep):
    """
    Base class for pipeline steps that prompt an LLM once per HTTP message through the inference harness.

    Subclasses define the task and what is specific to it: the user messages of the prompt and the strategy for
    retrieving examples. The model returns a JSON object, of which the list stored under 'response_key' is
    stored in the message field 'message_field' - unvalidated in 'raw_file' and validated in 'verified.jsonl'.
    Validated annotations are also reported as they occur in the original body ('original_annotations').
    """
    response_key: str
    message_field: Literal['predicted_labels', 'annotations']
    raw_file: str

    def __init__(self, context: LLMStepContext):
        self.context = context
        self.output_files = (self.raw_file, VERIFIED_FILE)
        self._warned_about_normalization = False

        validator = context.validator
        self.harness = Harness(
            task=self.task,
            taxonomy_text=context.taxonomy.text,
            client=context.client,
            model=context.model,
            response_key=self.response_key,
            build_user_msgs=self.build_user_msgs,
            retrieve_examples=self.retrieve_examples,
            validate=(validator.validate_labels if self.message_field == 'predicted_labels'
                      else validator.validate_annotations),
            log_folder=context.log_folder,
            max_retries=context.max_retries,
            retry_delay=context.retry_delay,
            max_requeries=context.max_requeries
        )

    @abstractmethod
    def build_user_msgs(self, message: Message) -> list[str]:
        """Build the user messages that follow the system prompt, the taxonomy and the examples."""
        pass

    def retrieve_examples(self, message: Message) -> list[tuple[str, str]]:
        """Retrieve the examples with the most similar payloads for the given message."""
        if not self.context.retriever:
            return []
        return self.context.retriever.get_similar_examples_for_api(
            body=message.body,
            task=self.task,
            top_k=self.context.max_examples,
            min_similarity=self.context.min_example_similarity
        )

    def process(self, message: Message) -> tuple[Message, ...]:
        result = self.harness.run(message)
        # the mapping to the original body made by a previous step does not apply to the result of this step
        message = message.copy(fields={k: v for k, v in message.fields.items() if k != ORIGINAL_ANNOTATIONS})
        verified = message.copy(**{self.message_field: result.validated}, error=result.error)
        if self.message_field == 'annotations' and ORIGINAL_BODY in message.fields:
            mapped = original_annotations(verified, self.context.normalization, self.context.decode_base64)
            if mapped is not None:
                verified.fields[ORIGINAL_ANNOTATIONS] = mapped
            elif not self._warned_about_normalization:
                self._warned_about_normalization = True
                logger.warning("%s: the bodies were pre-processed with other settings than -normalization %s%s, "
                               "annotations are not mapped to the original bodies", self.task,
                               self.context.normalization, " -decode-base64" if self.context.decode_base64 else "")
        return (
            message.copy(**{self.message_field: result.raw}, error=result.error),
            verified
        )
