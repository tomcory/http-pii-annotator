from src.llm_connectors.api_base import Prompt, ResponseFormat
from src.llm_connectors.api_openai import ApiOpenAI


class ApiDeepSeek(ApiOpenAI):
    """
    API connector for DeepSeek's models, which are served through an OpenAI-compatible API.
    """
    api = 'deepseek'
    api_key_name = 'DEEPSEEK_API_KEY'

    def __init__(self, hostname: str = None, timeout: float = None):
        super().__init__(hostname or "https://api.deepseek.com", timeout)

    @property
    def requires_api_key(self) -> bool:
        return True

    def _parse_response_format(self, response_format: ResponseFormat, prompt: Prompt) -> dict:
        # DeepSeek does not support JSON schemas, so the schema is only described in the system prompt
        if response_format == 'text':
            return {'type': 'text'}
        elif response_format in ('json', 'json_schema'):
            return {'type': 'json_object'}
        else:
            raise ValueError(f"response_format '{response_format}' not supported")
