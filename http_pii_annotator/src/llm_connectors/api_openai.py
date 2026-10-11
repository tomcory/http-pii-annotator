import os
from collections.abc import Callable

from openai import OpenAI

from src.errors import ConfigError
from src.llm_connectors.api_base import GenerationParams, LLMClient, LLMResponse, Prompt, ResponseFormat


class ApiOpenAI(LLMClient):
    """
    API connector for OpenAI's GPT models and for OpenAI-compatible endpoints such as llama.cpp.

    Model options: 'max_tokens_parameter' names the request parameter that carries the token limit. It is
    'max_completion_tokens' unless set; servers that only know the older name need 'max_tokens'.
    """
    api = 'openai'
    api_key_name = 'OPENAI_API_KEY'

    def __init__(self, hostname: str = None, timeout: float = None):
        super().__init__(hostname, timeout)

        self.api_key = os.getenv(self.api_key_name)
        if self.requires_api_key and not self.api_key:
            raise ConfigError(f"Please set the {self.api_key_name} environment variable.")

        if self.hostname:
            self.client = OpenAI(
                api_key=self.api_key if self.api_key else "not-needed",  # llama.cpp doesn't need a real key
                base_url=self.hostname
            )
        else:
            self.client = OpenAI(
                api_key=self.api_key
            )

    @property
    def requires_api_key(self) -> bool:
        # The API key is optional when using a custom endpoint (e.g., llama.cpp)
        return not self.hostname

    def close(self):
        self.client.close()

    def complete(
            self,
            model: str,
            prompt: Prompt,
            response_format: ResponseFormat = 'text',
            params: GenerationParams = GenerationParams(),
            model_options: dict = None,
            on_text: Callable[[str], None] = None
    ) -> LLMResponse:
        model_options = model_options or {}
        stream_params = {
            "messages": prompt.with_system_message(),
            "model": model,
            "temperature": params.temperature,
            model_options.get("max_tokens_parameter", "max_completion_tokens"): params.max_tokens,
            "top_p": params.top_p,
            "seed": params.seed,
            "timeout": self.timeout,
            "stream": True,
        }
        if params.top_p is None:
            del stream_params["top_p"]

        # Only include response_format if it's not None
        parsed_response_format = self._parse_response_format(response_format, prompt)
        if parsed_response_format is not None:
            stream_params["response_format"] = parsed_response_format

        # Only include stream_options for the official OpenAI API (llama.cpp may not support it)
        if not self.hostname:
            stream_params["stream_options"] = {"include_usage": True}

        # Use streaming to prevent gateway timeouts
        stream = self.client.chat.completions.create(**stream_params)

        output_parts = []
        input_tokens = 0
        output_tokens = 0

        for chunk in stream:
            if chunk.choices and len(chunk.choices) > 0:
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    output_parts.append(delta.content)
                    if on_text:
                        on_text(delta.content)

            # Token counts are provided in the final chunk with usage info (if available)
            if hasattr(chunk, 'usage') and chunk.usage:
                input_tokens = chunk.usage.prompt_tokens or 0
                output_tokens = chunk.usage.completion_tokens or 0

        output = ''.join(output_parts)

        # If token counts weren't provided (common with llama.cpp), estimate ~4 characters per token
        if output_tokens == 0 and output:
            output_tokens = len(output) // 4

        return LLMResponse(text=output, input_tokens=input_tokens, output_tokens=output_tokens)

    def _parse_response_format(self, response_format: ResponseFormat, prompt: Prompt) -> dict | None:
        json_schema = prompt.response_schema
        if response_format == 'text':
            # For text responses, don't specify response_format (or return None for local servers)
            if self.hostname:
                return None
            return {'type': 'text'}
        elif response_format == 'json':
            # For basic JSON responses without a schema
            return {'type': 'json_object'}
        elif response_format == 'json_schema':
            if json_schema is None:
                raise ValueError("A response schema must be provided when response_format is 'json_schema'")
            # For local servers (llama.cpp), pass the schema directly in json_schema format
            if self.hostname:
                return {
                    'type': 'json_schema',
                    'json_schema': {
                        'name': 'response',
                        'schema': json_schema
                    }
                }
            else:
                # For the official OpenAI API, use strict mode
                return {
                    'type': 'json_schema',
                    'json_schema': {
                        'name': f"response_schema_{prompt.task}",
                        'strict': True,
                        'schema': json_schema
                    }
                }
        else:
            raise ValueError(f"response_format '{response_format}' not supported")
