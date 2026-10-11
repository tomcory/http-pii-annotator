import asyncio
import json
import logging
from abc import ABC, abstractmethod

from src.llm import LLM
from src.state_manager import BaseStateManager

logger = logging.getLogger(__name__)


class PipelineStep(ABC):
    """Represents a step in the pipeline."""

    # whether a failure of this step aborts the pipeline (instead of skipping the failed message)
    abort_on_error = False

    def __init__(
            self,
            run_id: str,
            task: str,
            details: str,
            skip: bool,
            is_llm_step: bool,
            in_folder: str,
            out_folder: str,
            state_manager: BaseStateManager,
            model: str = None,
            client: LLM = None,
            schema: str = "",
            schema_name: str = "",
            labels: list[str] = None
    ):
        self.run_id = run_id
        self.task = task
        self.details = details
        self.skip = skip
        self.is_llm_step = is_llm_step
        self.in_folder = in_folder
        self.out_folder = out_folder
        self.state_manager = state_manager
        self.model = model
        self.client = client
        self.schema = schema
        self.schema_name = schema_name
        self.labels = labels
        # limits the number of LLM calls in flight (set by the executor)
        self.llm_semaphore = None

        logger.info(f"Initializing pipeline step '{task}' for run '{run_id}'")
        logger.debug(f"  details: {details}")
        logger.debug(f"  skip: {skip}")
        logger.debug(f"  is_llm_step: {is_llm_step}")
        logger.debug(f"  in folder '{self.in_folder}'")
        logger.debug(f"  out folder '{self.out_folder}'")
        logger.debug(f"  state manager '{self.state_manager}'")

    @abstractmethod
    async def execute(self, message: dict):
        pass

    def start(self):
        """Called once before the first message is processed. Override to reset step state."""
        pass

    def prepare_input(self, messages: list) -> list:
        """Transform the loaded input into the work items passed to execute()."""
        return messages

    async def generate_json_array(
            self,
            msg_id: str,
            system_msg: str,
            user_msgs: list[str],
            schema: str = None,
            temperature: float = 0.8,
            max_tokens: int = 16000,
            timeout: float = 600.0,
            max_retries: int = 0,
            client: LLM = None,
            task: str = None,
    ) -> list:
        """
        Prompt the LLM and parse its response as a JSON array.

        The (blocking) connector call runs in a worker thread. Failed calls and
        unparseable responses are retried up to max_retries times. By default the
        client and the task name of the step are used.
        """
        client = client or self.client
        task = task or self.task
        last_error = None
        for attempt in range(max_retries + 1):
            if attempt > 0:
                logger.info(f"[{task}] Retrying {msg_id} (attempt {attempt + 1}/{max_retries + 1}): {last_error}")
            try:
                if self.llm_semaphore is None:
                    self.llm_semaphore = asyncio.Semaphore(1)
                async with self.llm_semaphore:
                    output, _ = await asyncio.to_thread(
                        client.prompt,
                        msg_id=msg_id,
                        task=task,
                        user_msgs=user_msgs,
                        schema=schema,
                        system_msg=system_msg,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        timeout=timeout,
                    )
                content = self._strip_code_fences(output)
                try:
                    result = json.loads(content)
                except ValueError as parse_error:
                    # an output that was cut off (e.g. at the token limit) still contains complete elements
                    result = self._salvage_json_array(content)
                    if not result:
                        raise parse_error
                    logger.info(f"[{task}] Output of {msg_id} is incomplete ({parse_error}); "
                          f"keeping its {len(result)} complete element(s)")
                if not isinstance(result, list):
                    raise ValueError(f"Expected JSON array, got {type(result).__name__}")
                return result
            except Exception as e:
                last_error = e

        raise RuntimeError(f"LLM generation failed for {msg_id} after {max_retries + 1} attempt(s): {last_error}")

    @staticmethod
    def _salvage_json_array(content: str) -> list:
        """Return the complete leading elements of a JSON array whose end is missing."""
        content = content.lstrip()
        if not content.startswith("["):
            return []
        decoder = json.JSONDecoder()
        elements, position = [], 1
        while True:
            while position < len(content) and content[position] in " \t\r\n,":
                position += 1
            if position >= len(content) or content[position] == "]":
                break
            try:
                element, position = decoder.raw_decode(content, position)
            except ValueError:
                break
            elements.append(element)
        return elements

    @staticmethod
    def _strip_code_fences(content: str) -> str:
        """Strip markdown code fences from LLM response."""
        content = content.strip()
        if content.startswith("```json"):
            content = content[7:]
        elif content.startswith("```"):
            content = content[3:]
        else:
            return content
        if content.endswith("```"):
            content = content[:-3]
        return content.strip()
