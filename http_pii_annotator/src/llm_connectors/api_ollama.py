import logging
import os
from collections.abc import Callable

import ollama
from tqdm import tqdm

from src.llm_connectors.api_base import GenerationParams, LLMClient, LLMResponse, Prompt, ResponseFormat

logger = logging.getLogger(__name__)


class ApiOllama(LLMClient):
    """
    Ollama API connector for interacting with LLMs hosted on Ollama.

    The server is selected with the hostname or, if none is given, with the environment variable
    OLLAMA_HOSTNAME. OLLAMA_API_KEY is only required for servers behind an authenticating proxy.

    Model options: 'think' (bool) switches the reasoning of a thinking-capable model on or off; without it
    the model's default applies.
    """
    api = 'ollama'

    def __init__(self, hostname: str = None, timeout: float = None):
        super().__init__(hostname or os.getenv('OLLAMA_HOSTNAME'), timeout)

        # Configure headers with Bearer token if an API key is available
        self.api_key = os.getenv('OLLAMA_API_KEY')
        headers = {}
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'

        options = {'headers': headers}
        if self.hostname:
            options['host'] = self.hostname
        if self.timeout is not None:
            options['timeout'] = self.timeout
        self.client = ollama.Client(**options)

    def complete(
            self,
            model: str,
            prompt: Prompt,
            response_format: ResponseFormat = 'text',
            params: GenerationParams = GenerationParams(),
            model_options: dict = None,
            on_text: Callable[[str], None] = None
    ) -> LLMResponse:
        if response_format == 'json_schema':
            # Ollama does not accept the '$schema' keyword
            output_format = {k: v for k, v in prompt.response_schema.items() if k != '$schema'}
        elif response_format == 'json':
            output_format = 'json'
        else:
            output_format = None

        options = {'temperature': params.temperature, 'top_p': params.top_p, 'num_predict': params.max_tokens}
        if params.top_p is None:
            del options['top_p']
        if params.seed is not None:
            options['seed'] = params.seed

        extra = {}
        if (model_options or {}).get('think') is not None:
            extra['think'] = model_options['think']

        # Use streaming to prevent gateway timeouts
        stream = self.client.chat(
            model=model,
            messages=prompt.with_system_message(),
            format=output_format,
            options=options,
            stream=True,
            **extra
        )

        output_parts = []
        input_tokens = 0
        output_tokens = 0
        thinking_chars = 0
        finish_reason = None

        for chunk in stream:
            # a thinking-capable model streams its reasoning separately from its answer
            thinking_chars += len(chunk['message'].get('thinking') or '')
            finish_reason = chunk.get('done_reason') or finish_reason
            if chunk['message']['content']:
                output_parts.append(chunk['message']['content'])
                if on_text:
                    on_text(chunk['message']['content'])
            # Token counts are provided in the final chunk
            if chunk.get('prompt_eval_count'):
                input_tokens = chunk['prompt_eval_count']
            if chunk.get('eval_count'):
                output_tokens = chunk['eval_count']

        return LLMResponse(text=''.join(output_parts), input_tokens=input_tokens, output_tokens=output_tokens,
                           finish_reason=finish_reason, thinking_chars=thinking_chars)

    def prepare(self, model: str):
        """Downloads the model if necessary and loads it into memory."""
        logger.info("Loading model %s...", model)
        downloaded_models = [m['model'] for m in self.client.list()['models']]
        if model not in downloaded_models:
            logger.info("Downloading model %s...", model)
            self._pull_model(model)

        # load the model by prompting it with a message
        self.client.chat(model, messages=[{'role': 'user', 'content': 'What model are you?'}])

    def release(self, model: str):
        """Unloads the model from memory."""
        logger.info("Unloading model %s...", model)
        try:
            self.client.chat(model, [{'role': 'user', 'content': ' '}], keep_alive=0)
        except ollama.ResponseError as e:
            logger.warning("Failed to unload model %s: %s", model, e)

    def _pull_model(self, model_name: str):
        current_digest, bars = '', {}
        for progress in self.client.pull(model_name, stream=True):
            digest = progress.get('digest', '')
            if digest != current_digest and current_digest in bars:
                bars[current_digest].close()

            if not digest:
                logger.info("> %s...", progress.get("status"))
                continue

            if digest not in bars and (total := progress.get('total')):
                bars[digest] = tqdm(total=total, desc=f'> pulling {digest[7:19]}', unit='B', unit_scale=True)

            if completed := progress.get('completed'):
                bars[digest].update(completed - bars[digest].n)

            current_digest = digest
