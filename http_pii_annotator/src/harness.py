"""
The LLM inference harness that wraps every LLM-based pipeline step: it enriches the model input with retrieved
examples, calls the model, and validates the model output before it is passed to subsequent steps.
"""
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from src.llm_connectors.api_base import GenerationParams, LLMClient, LLMResponse, Prompt, ResponseFormat
from src.llm_connectors.registry import ModelSpec
from src.prompts import PromptBuilder
from src.records import Message

logger = logging.getLogger(__name__)

#: Sent after a response that contains labels which are not part of the taxonomy and could not be corrected
REQUERY_MESSAGE = ("Your response contains labels that are not part of the taxonomy: {labels}. Answer again in the same "
                   "format. Use only labels of the taxonomy, written exactly as in its column \"PII Data type\", and "
                   "leave out what none of these labels applies to.")


@dataclass
class HarnessResult:
    """
    Result of processing one message.

    Attributes:
        raw: The list extracted from the model's response (empty if the response could not be parsed)
        validated: The list after validation
        seconds: Processing time of the successful call
        error: Description of the error if the model could not be called
    """
    raw: list
    validated: list
    seconds: float = 0.0
    error: str | None = None


class Harness:
    """
    Processes messages for one LLM task: retrieve examples -> build prompt -> call model -> parse -> validate.
    If the validation rejects labels, the model is told so and asked again.

    A harness can be called from several threads.
    """

    def __init__(
            self,
            task: str,
            taxonomy_text: str,
            client: LLMClient,
            model: ModelSpec,
            response_key: str,
            build_user_msgs: Callable[[Message], list[str]],
            retrieve_examples: Callable[[Message], list[tuple[str, str]]],
            validate: Callable[[list, list], list],
            log_folder: Path = None,
            params: GenerationParams = GenerationParams(max_tokens=8192),
            response_format: ResponseFormat = 'json_schema',
            max_retries: int = 3,
            retry_delay: float = 1.0,
            max_requeries: int = 1
    ):
        """
        Args:
            task: Name of the task, which is also the name of the task's folder in 'prompts'
            taxonomy_text: Label taxonomy as a Markdown table
            client: API connector
            model: Model to prompt
            response_key: Key of the result list in the JSON object returned by the model
            build_user_msgs: Builds the user messages for a message
            retrieve_examples: Retrieves the few-shot examples for a message
            validate: Validates the result list and appends the labels it rejects to its second argument
            log_folder: Folder in which responses and processing times are logged (None to disable)
            params: Sampling parameters
            response_format: Response format requested from the API
            max_retries: Number of times a failed call is repeated
            retry_delay: Seconds to wait before the first retry; doubled for every further retry
            max_requeries: Number of times the model is asked again after a response with rejected labels
        """
        self.task = task
        self.taxonomy_text = taxonomy_text
        self.client = client
        self.model = model
        self.response_key = response_key
        self.build_user_msgs = build_user_msgs
        self.retrieve_examples = retrieve_examples
        self.validate = validate
        self.params = params
        self.response_format = response_format
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.max_requeries = max_requeries

        self.prompt_builder = PromptBuilder(task)

        self._lock = threading.Lock()
        self._log_folder = None
        if log_folder is not None:
            self._log_folder = Path(log_folder) / f"{re.sub(r'[^A-Za-z0-9._-]', '_', model.name)}_responses"
            (self._log_folder / task).mkdir(parents=True, exist_ok=True)

    def run(self, message: Message) -> HarnessResult:
        prompt = self.prompt_builder.build(
            taxonomy=self.taxonomy_text,
            user_msgs=self.build_user_msgs(message),
            examples=self.retrieve_examples(message)
        )

        result = None
        for query in range(self.max_requeries + 1):
            response, seconds, error = self._complete(prompt, message)
            if response is None:
                if result is not None:
                    # the answer to the first query stands, without the rejected labels
                    logger.warning("%s: re-query for message %s failed (%s), discarding the rejected labels",
                                   self.task, message.message_id, error)
                    break
                logger.error("%s failed for message %s: %s", self.task, message.message_id, error)
                return HarnessResult(raw=[], validated=[], error=error)

            self._log_response(message.message_id, response.text, seconds, query)
            logger.debug("%s response for message %s: %s", self.task, message.message_id, response.text)

            try:
                raw = json.loads(response.text)[self.response_key]
            except Exception as e:
                # A response that cannot be parsed yields an empty result
                logger.warning("Could not parse the %s response for message %s: %s", self.task,
                               message.message_id, e)
                raw = []

            rejected = []
            validated = self.validate(raw, rejected)
            result = HarnessResult(raw=raw, validated=validated,
                                   seconds=seconds + (result.seconds if result else 0.0))
            if not rejected:
                break
            if query < self.max_requeries:
                logger.warning("%s: re-querying message %s because of rejected labels %s", self.task,
                               message.message_id, rejected)
                prompt = replace(prompt, messages=(
                    *prompt.messages,
                    {"role": "assistant", "content": response.text},
                    {"role": "user", "content": REQUERY_MESSAGE.format(labels=json.dumps(rejected))}
                ))
            else:
                logger.warning("%s: discarding rejected labels %s of message %s", self.task, rejected,
                               message.message_id)

        return result

    def _complete(self, prompt: Prompt, message: Message) -> tuple[LLMResponse | None, float, str | None]:
        """Calls the model, repeating failed calls. Returns the response, its processing time and the last error."""
        response, seconds, error = None, 0.0, None
        for attempt in range(self.max_retries + 1):
            start_time = time.monotonic()
            try:
                response = self.client.complete(
                    model=self.model.name,
                    prompt=prompt,
                    response_format=self.response_format,
                    params=self.params
                )
                seconds = time.monotonic() - start_time
                break
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                if attempt < self.max_retries:
                    delay = self.retry_delay * 2 ** attempt
                    logger.warning("%s failed for message %s (%s), retrying in %.0f s", self.task,
                                   message.message_id, error, delay)
                    time.sleep(delay)
        return response, seconds, error

    def _log_response(self, message_id: str, text: str, seconds: float, query: int = 0):
        with self._lock:
            if self._log_folder is None:
                return
            with open(self._log_folder / f"times_{self.task}.csv", "a") as f:
                f.write(f"{message_id},{seconds}\n")
            extension = 'txt' if self.response_format == 'text' else 'json'
            file_name = re.sub(r'[^A-Za-z0-9._-]', '_', message_id) + (f"_requery{query}" if query else "")
            with open(self._log_folder / self.task / f"{file_name}.{extension}", "w") as f:
                f.write(text + '\n')
