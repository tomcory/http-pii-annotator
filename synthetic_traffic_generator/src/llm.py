"""
The model behind the pipeline's LLM steps.

The transport to the APIs is that of the connectors in llm_connectors, which are the same as the annotator's.
This module adds what a run of the pipeline needs around it:
the conversation is assembled from a system prompt, the taxonomy and the user messages, every response is
logged under the run's log folder, and the request options are those the generator has always used.
"""

import logging
import timeit

from src import util
from src.llm_connectors.api_base import GenerationParams, Prompt
from src.llm_connectors.registry import APIS, ModelSpec
from src.llm_connectors.registry import create_client as create_connector

logger = logging.getLogger(__name__)

SUPPORTED_APIS = APIS


class LLM:
    """One model of one API, as the pipeline steps prompt it."""

    def __init__(
        self,
        api: str,
        run_id: str,
        model: str,
        hostname: str = None,
        log_dir: str = None,
        thinking: bool = None,
    ):
        """
        Args:
            api: Name of the API (one of SUPPORTED_APIS)
            run_id: Identifier of the run
            model: Name of the model as the API expects it
            hostname: Base URL of a custom API endpoint
            log_dir: Folder for the response logs (no logging if None)
            thinking: Switches the reasoning of thinking-capable models on or off (None: the model's default);
                only Ollama has such a switch
        """
        if api not in SUPPORTED_APIS:
            raise ValueError(f"Unknown API '{api}'. Supported: {', '.join(SUPPORTED_APIS)}")
        self.api = api
        self.run_id = run_id
        self.log_dir = log_dir
        self.model = ModelSpec(api=api, name=model)
        # the timeout is set per prompt; a client for Ollama is created without one and never gets one
        self.client = create_connector(api, hostname=hostname or None)

        self.model_options = {}
        if api == "ollama" and thinking is not None:
            self.model_options["think"] = thinking
        if api in ("openai", "deepseek") and self.client.hostname:
            # OpenAI-compatible servers (e.g. llama.cpp) expect the older max_tokens parameter
            self.model_options["max_tokens_parameter"] = "max_tokens"
        if api == "anthropic":
            # streaming prevents timeouts on long generations
            self.model_options["stream"] = True

    def close(self):
        # only unload from a local server; a remote server may be shared with other users
        if self.api == "ollama" and self.client.hostname is None:
            self.client.release(self.model.name)
        self.client.close()

    def prompt(
        self,
        msg_id: str,
        task: str,
        user_msgs: list[str],
        schema: str = None,
        system_msg: str = None,
        temperature: float = 1.0,
        max_tokens: int = 2048,
        timeout: float = None,
    ) -> tuple[str, float, float]:
        """
        Send a prompt to the model and return its response.

        Args:
            msg_id: Name of the call, which names its entry in the response log
            task: Name of the task
            user_msgs: The user messages
            schema: The label taxonomy, sent as the first user message
            system_msg: The system prompt
            temperature: Sampling temperature
            max_tokens: Maximum number of tokens to generate
            timeout: Seconds after which the request is aborted (not applied to Ollama)

        Returns:
            Tuple of (response text, processing time)
        """
        if not system_msg:
            raise ValueError(f"No system prompt specified for task '{task}'.")
        start_time = timeit.default_timer()

        messages = [{"role": "user", "content": schema}] if schema is not None else []
        messages += [{"role": "user", "content": user_msg} for user_msg in user_msgs or []]
        prompt = Prompt(task=task, system=system_msg, messages=tuple(messages))

        streams = self.api != "anthropic"
        if self.api != "ollama":
            self.client.timeout = timeout
        response = self.client.complete(
            self.model.name,
            prompt,
            # Ollama and Anthropic are left to their own top_p
            params=GenerationParams(
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=1 if self.api in ("openai", "deepseek") else None,
            ),
            model_options=self.model_options,
            on_text=(lambda text: print(text, end="", flush=True)) if streams else None,
        )
        if streams:
            print()  # newline after the streamed output
        if response.finish_reason == "length":
            logger.warning(f"[{self.api}] WARNING: output of {msg_id} was cut off at the token limit "
                f"(max_tokens={max_tokens}, {response.output_tokens} tokens generated, of which about "
                f"{response.thinking_chars // 4} for thinking)")

        processing_time = timeit.default_timer() - start_time
        if self.log_dir is not None and msg_id is not None and task is not None:
            util.log_prompt_result(
                log_dir=self.log_dir,
                task=task,
                pkg=msg_id,
                model_name=self.model.name,
                output_format="txt",
                processing_time=processing_time,
                outputs=[response.text],
            )
        return response.text, processing_time


def create_client(
    api: str,
    run_id: str,
    model: str,
    hostname: str = None,
    log_dir: str = None,
    thinking: bool = None,
) -> LLM:
    """Create the model client of a run."""
    return LLM(api, run_id, model, hostname, log_dir, thinking)
