import os
from collections.abc import Callable

from anthropic import Anthropic

from src.errors import ConfigError
from src.llm_connectors.api_base import GenerationParams, LLMClient, LLMResponse, Prompt, ResponseFormat


class ApiAnthropic(LLMClient):
    """
    API connector for Anthropic's Claude models.

    The response schema is not enforced by the API, it is only described in the system prompt.

    Model options: 'stream' (bool) requests the response as a stream and returns it when it is complete,
    which the API requires for generations that may take long.
    """
    api = 'anthropic'
    api_key_name = 'ANTHROPIC_API_KEY'

    def __init__(self, hostname: str = None, timeout: float = None):
        super().__init__(hostname, timeout)

        self.api_key = os.getenv(self.api_key_name)
        if self.api_key is None:
            raise ConfigError(f"Please set the {self.api_key_name} environment variable.")

        if self.hostname:
            self.client = Anthropic(
                api_key=self.api_key,
                base_url=self.hostname
            )
        else:
            self.client = Anthropic(
                api_key=self.api_key
            )

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
        request = dict(
            model=model,
            messages=list(prompt.messages),
            system=prompt.system,
            max_tokens=params.max_tokens,
            temperature=params.temperature,
            top_p=params.top_p,
            stream=False
        )
        if params.top_p is None:
            del request['top_p']
        if self.timeout is not None:
            request['timeout'] = self.timeout

        if (model_options or {}).get('stream'):
            del request['stream']
            with self.client.messages.stream(**request) as stream:
                completion = stream.get_final_message()
        else:
            completion = self.client.messages.create(**request)

        return LLMResponse(
            text=completion.content[0].text,
            input_tokens=completion.usage.input_tokens,
            output_tokens=completion.usage.output_tokens
        )
